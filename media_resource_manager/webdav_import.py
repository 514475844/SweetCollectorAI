"""从 WebDAV 目录导入图片（V0.0.44）。

为什么单独成模块
----------------
用户要「填一个地址就把 NAS / 网盘上的图片导进来」，而本地文件夹导入那条链
（:mod:`import_folder`）认的是**文件系统路径**，WebDAV 是 HTTP 上的虚拟目录。
把两件事硬塞进一个函数，最终会变成「一会儿 stat、一会儿 GET」的怪物。

所以这里只做三件明确的事：

1. **枚举**：PROPFIND 递归列出图片（只认扩展名在 :data:`IMAGE_EXTS` 里的）；
2. **落地**：逐张 GET 下来，存进软件图库目录下的 ``webdav/<来源标识>/``，
   文件名与层级照原样保留；已存在且大小一致的**直接跳过** —— 这正是
   「断了再导一次能接着来」在本模块的落点，不需要另存断点文件；
3. **入库 + 打标**：把落好的目录交给 :func:`import_folder.import_folder`，
   复用索引 / 去重 / 打标 / 学习那一整套，行为与本地导入完全一致。

为什么不直接登记远端 URL
------------------------
图库里的每一行都对应一个**能打开的文件**（预览、缩略图、导出、打标全要读盘）。
远端 URL 一旦断网就变成坏行。落地成本可接受，收益是整条链路不用改。

安全边界
--------
* 只接受 http / https；
* 账号密码只用于本次请求的 Basic 头，不落盘、不进日志、不回显；
* 下载体积不设上限，但按张计数、可随时停（停止后已落地的文件保留）。
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from xml.etree import ElementTree as ET

from .import_folder import IMAGE_EXTS

#: 单次 PROPFIND 的超时（秒）。NAS 睡眠唤醒可能要十几秒。
TIMEOUT = 30
#: 下载单张的超时（秒）：大图 / 慢速网盘要放宽。
DL_TIMEOUT = 120
_NS = {"d": "DAV:"}
_PROPFIND_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/>"
    b"</d:prop></d:propfind>"
)


class WebDAVError(Exception):
    """带中文原因的 WebDAV 错误（界面直接显示）。"""


def _auth(user: str, password: str) -> dict:
    if not user and not password:
        return {}
    raw = f"{user}:{password}".encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _open(url: str, *, method: str = "GET", headers: Optional[dict] = None,
          data: Optional[bytes] = None, timeout: int = TIMEOUT):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    return urllib.request.urlopen(req, timeout=timeout)


def _explain(e: Exception, url: str) -> WebDAVError:
    """把 urllib 的异常翻成人话（界面要显示，不能是 URLError: ...）。"""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return WebDAVError("账号或密码不对（服务器返回 401）")
        if e.code == 403:
            return WebDAVError("服务器拒绝访问（403），这个账号没有读权限")
        if e.code == 404:
            return WebDAVError(f"地址不存在（404）：{url}")
        if e.code == 405:
            return WebDAVError("这个地址不支持 WebDAV（405），"
                              "确认填的是 WebDAV 目录而不是普通网页")
        return WebDAVError(f"服务器返回 {e.code}：{e.reason}")
    if isinstance(e, urllib.error.URLError):
        return WebDAVError(f"连不上：{e.reason}")
    if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
        return WebDAVError("请求超时，检查地址与网络（NAS 可能正在唤醒）")
    return WebDAVError(f"{type(e).__name__}: {e}")


def _norm(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _parse_propfind(raw: bytes) -> list[dict]:
    """解析 PROPFIND 响应 → [{href, is_dir, size}]。

    只取 **200 段** 的 propstat：有些服务端会同时返回 404 的 propstat
    （比如不提供 getcontentlength），混在一起会把目录误判成文件。
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise WebDAVError(f"服务器返回的不是 WebDAV 目录信息（{e}）") from e
    out: list[dict] = []
    for resp in root.findall("d:response", _NS):
        href = (resp.findtext("d:href", default="", namespaces=_NS) or "").strip()
        if not href:
            continue
        is_dir = False
        size = 0
        for ps in resp.findall("d:propstat", _NS):
            status = (ps.findtext("d:status", default="", namespaces=_NS) or "")
            if " 200 " not in status:
                continue
            rt = ps.find("d:prop/d:resourcetype", _NS)
            if rt is not None and rt.find("d:collection", _NS) is not None:
                is_dir = True
            txt = ps.findtext("d:prop/d:getcontentlength", default="",
                              namespaces=_NS)
            try:
                size = int(txt) if txt else 0
            except ValueError:
                size = 0
        out.append({"href": href, "is_dir": is_dir, "size": size})
    return out


def list_images(url: str, *, user: str = "", password: str = "",
                recursive: bool = True,
                stop: Optional[Callable[[], bool]] = None,
                progress: Optional[Callable[[int, int, str], None]] = None
                ) -> list[tuple[str, str]]:
    """递归列出全部图片，返回 [(下载地址, 相对路径)]。

    用 BFS + ``Depth: 1``（而不是 ``Depth: infinity``）：后者在群晖 / 飞牛上
    被大量服务端禁用（返回 403 或 501），一次请求深度遍历还会让大目录超时。
    """
    base = _norm(url)
    headers = _auth(user, password)
    seen: set[str] = set()
    queue: list[tuple[str, str]] = [(base, "")]     # (目录 URL, 相对前缀)
    out: list[tuple[str, str]] = []
    seen.add(base)
    while queue:
        if stop and stop():
            break
        cur, prefix = queue.pop(0)
        try:
            with _open(cur, method="PROPFIND", headers=headers,
                       data=_PROPFIND_BODY) as r:
                raw = r.read()
        except Exception as e:                      # noqa: BLE001
            raise _explain(e, cur) from e
        entries = _parse_propfind(raw)
        dirs: list[tuple[str, str]] = []
        for it in entries:
            href = it["href"]
            full = urllib.parse.urljoin(cur, href)
            key = full.rstrip("/")
            if key in seen:
                continue
            seen.add(key)
            name = urllib.parse.unquote(
                urllib.parse.urlparse(full).path.rstrip("/").rsplit("/", 1)[-1])
            if it["is_dir"]:
                # 跳过以 . 或 @ 开头的目录（与本地导入同一口径：@eaDir 之类
                # 是 NAS 自己生成的缩略图目录）
                if name.startswith(".") or name.startswith("@"):
                    continue
                if recursive or not prefix:
                    dirs.append((_norm(full), (prefix + "/" + name) if prefix else name))
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_EXTS:
                rel = (prefix + "/" + name) if prefix else name
                out.append((full, rel))
        if progress:
            try:
                progress(len(out), len(queue) + len(dirs), prefix or "/")
            except Exception:
                pass
        if recursive:
            queue.extend(dirs)
        # 不递归时 dirs 直接丢掉：只导这一层的图片，子目录不进去。
    out.sort(key=lambda x: x[1])
    return out


def _slug(url: str, name: str = "") -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    tail = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_",
                  urllib.parse.unquote(name or "")).strip("_")[:24]
    return f"{tail}_{h}" if tail else f"webdav_{h}"


def cache_dir_for(lib_dir: Path, url: str) -> Path:
    """这类来源在软件图库里的落点：``library/webdav/<标识>/``。"""
    return Path(lib_dir) / "webdav" / _slug(url)


def import_webdav(db, cfg, url: str, *, user: str = "", password: str = "",
                  recursive: bool = True, do_tag: bool = True,
                  copy_to_library: bool = False,
                  stop: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[..., None]] = None) -> dict:
    """枚举 → 下载 → 入库打标。返回结构与 :func:`import_folder.import_folder` 一致。"""
    from . import import_folder as imp

    url = str(url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return {"ok": False, "error": "地址必须以 http:// 或 https:// 开头"}
    if progress is None:
        def progress(*a, **k):        # noqa: E306 —— 占位，允许不传
            pass

    # ── ① 枚举 ──
    try:
        files = list_images(url, user=user, password=password,
                            recursive=recursive, stop=stop,
                            progress=lambda d, q, n: progress(
                                "list", d, q or d + 1, n, "", 0))
    except WebDAVError as e:
        return {"ok": False, "error": str(e)}
    if not files:
        return {"ok": False, "error": "这个地址下没有找到图片"
                                     "（支持 jpg/png/webp/gif/bmp/tif/avif/heic）"}

    # ── ② 落地到软件图库目录 ──
    dest_root = cache_dir_for(Path(cfg.library_dir), url)
    headers = _auth(user, password)
    total = len(files)
    done = 0
    skipped = 0
    failed: list[str] = []
    for full, rel in files:
        if stop and stop():
            break
        safe_rel = rel.lstrip("/").replace("\\", "/")
        dest = dest_root / safe_rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"ok": False, "error": f"建目录失败：{e}"}
        try:
            # 已在本地且大小一致 ⇒ 跳过（断点续导落在这里）
            if dest.is_file() and dest.stat().st_size > 0:
                skipped += 1
                done += 1
                progress("download", done, total, safe_rel, "", int(30 * done / total))
                continue
            with _open(full, headers=headers, timeout=DL_TIMEOUT) as r, \
                    open(dest, "wb") as f:
                while True:
                    blk = r.read(1 << 20)
                    if not blk:
                        break
                    f.write(blk)
            if dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)
                failed.append(safe_rel)
            else:
                done += 1
        except Exception as e:                      # noqa: BLE001
            failed.append(safe_rel)
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
            _ = e
        progress("download", done, total, safe_rel, "", int(30 * done / total))

    if stop and stop():
        return {"ok": False, "error": "已停止（已下载的图片保留，下次同一地址会接着来）"}

    # ── ③ 交给本地导入那条链（索引 / 去重 / 打标 / 学习全复用）──
    def _map(phase, dn, tt, name, eta, pct):
        progress(phase, dn, tt, name, eta, 30 + int(pct * 0.7))

    res = imp.import_folder(db, cfg, dest_root, force=False,
                            do_tag=do_tag, stop=stop,
                            copy_to_library=copy_to_library,
                            recursive=True, progress=_map)
    if not res.get("ok"):
        return res
    res["source"] = "webdav"
    res["webdav_url"] = url
    res["downloaded"] = done - skipped
    res["already_here"] = skipped
    res["download_failed"] = len(failed)
    if failed:
        res["broken"] = (list(res.get("broken") or []) + failed)[:50]
    note = str(res.get("ai_note") or "")
    if skipped:
        note = (note + "；" if note else "") + f"上次已下载 {skipped} 张，直接复用"
    res["ai_note"] = note
    return res
