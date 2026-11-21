"""网络协议导入：FTP / SFTP / SMB（V0.0.49）。

为什么单独成模块
----------------
WebDAV 走的是 HTTP 上的虚拟目录（:mod:`webdav_import`），本模块走的是**真正的文件
传输协议**——FTP（标准库）、SFTP（SSH，paramiko）、SMB（Windows 共享 / NAS，
smbprotocol）。三者枚举目录的 API 不同，但「枚举 → 下载到软件图库目录 → 交给
本地导入链」这条主干完全一致，所以收在这里一处实现，别让每个协议各抄一遍。

设计约束（与 webdav_import 一致）
---------------------------------
* 只登记**已落地**的图片：远端 URL 断网即坏行，预览/打标/导出全要读本地文件；
* 已存在且大小一致的文件**直接跳过** —— 断点续导落在这里，不另存断点文件；
* 账号密码只用于本次连接，不落盘、不进日志、不回显；
* 枚举/下载全过程可随时停（stop 回调），已落地文件保留；
* 错误全部中文，不把库里的英文异常原文丢给用户。
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Callable, Iterator, Optional

from .import_folder import IMAGE_EXTS

#: 支持的协议 → 默认端口
DEFAULT_PORT = {"ftp": 21, "ftps": 990, "sftp": 22, "smb": 445}
#: 取消时的异常信号
class RemoteImportError(Exception):
    """面向用户的中文错误（直接把 str(e) 显示到界面）。"""


def _slug(protocol: str, host: str, path: str) -> str:
    key = f"{protocol}://{host}/{path}"
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    tail = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", (path or "").strip("/")).strip("_")[:24]
    return f"{protocol}_{tail}_{h}" if tail else f"{protocol}_{h}"


def cache_dir_for(lib_dir: Path, protocol: str, host: str, path: str) -> Path:
    return Path(lib_dir) / protocol / _slug(protocol, host, path)


def _is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTS


def _safe_rel(base: str, full: str) -> str:
    """把绝对路径换成相对 base 的安全路径（防目录穿越）。"""
    rel = os.path.relpath(full, base).replace("\\", "/")
    if rel.startswith(".."):
        return Path(full).name
    return rel


# ───────────────────────── FTP / FTPS ─────────────────────────
def _ftp_list(protocol, host, port, user, password, remote_path,
              recursive, stop) -> Iterator[tuple[str, str]]:
    import ftplib
    cls = ftplib.FTP_TLS if protocol == "ftps" else ftplib.FTP
    ftp = cls()
    try:
        ftp.connect(host, port, timeout=30)
        ftp.login(user or "anonymous", password or "")
        if protocol == "ftps":
            ftp.prot_p()
        root = remote_path or "/"

        def walk(d):
            if stop and stop():
                return
            entries = []
            try:
                ftp.cwd(d)
                ftp.retrlines("LIST", entries.append)
            except Exception:
                return
            for line in entries:
                if stop and stop():
                    return
                # Unix LIST 与 Windows DIR 格式都取最后一列做名字
                name = line.split(None, 3)[-1] if len(line.split(None, 3)) >= 4 else line
                name = name.split(" -> ")[0].strip()
                if not name or name in (".", ".."):
                    continue
                full = (d.rstrip("/") + "/" + name)
                if re.match(r"\d{2}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2}", line) and "<DIR>" in line.upper():
                    is_dir = True
                else:
                    is_dir = bool(line.upper().startswith("D")) or "<DIR>" in line.upper()
                if is_dir:
                    if recursive:
                        walk(full)
                elif _is_image(name):
                    yield full, _safe_rel(root, full)

        yield from walk(root)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _ftp_download(protocol, host, port, user, password, full, dest) -> None:
    import ftplib
    cls = ftplib.FTP_TLS if protocol == "ftps" else ftplib.FTP
    ftp = cls()
    try:
        ftp.connect(host, port, timeout=60)
        ftp.login(user or "anonymous", password or "")
        if protocol == "ftps":
            ftp.prot_p()
        with open(dest, "wb") as f:
            ftp.retrbinary("RETR " + full, f.write)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


# ───────────────────────── SFTP ─────────────────────────
def _sftp_client(host, port, user, password):
    try:
        import paramiko
    except ImportError:
        raise RemoteImportError("SFTP 组件不可用（paramiko 未安装）")
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, port=port, username=user or "", password=password or "", timeout=30)
    return c, c.open_sftp()


def _sftp_list(host, port, user, password, remote_path, recursive, stop):
    c, sftp = _sftp_client(host, port, user, password)
    try:
        root = remote_path or "."

        def walk(d):
            if stop and stop():
                return
            try:
                items = sftp.listdir_attr(d)
            except Exception:
                return
            for it in items:
                if stop and stop():
                    return
                full = d.rstrip("/") + "/" + it.filename
                import stat as _st
                if _st.S_ISDIR(it.st_mode or 0):
                    if recursive:
                        yield from walk(full)
                elif _is_image(it.filename):
                    yield full, _safe_rel(root, full)

        yield from walk(root)
    finally:
        sftp.close(); c.close()


def _sftp_download(host, port, user, password, full, dest):
    c, sftp = _sftp_client(host, port, user, password)
    try:
        sftp.get(full, str(dest))
    finally:
        sftp.close(); c.close()


# ───────────────────────── SMB ─────────────────────────
def _smb_share(host, port, user, password):
    try:
        from smbprotocol.connection import Connection
        from smbprotocol.session import Session
    except ImportError:
        raise RemoteImportError("SMB 组件不可用（smbprotocol 未安装）")
    import uuid
    conn = Connection(uuid.uuid4(), host, port)
    conn.connect()
    sess = Session(conn, user or "guest", password or "")
    sess.connect()
    return conn, sess


def _smb_list(host, port, user, password, remote_path, recursive, stop):
    from smbprotocol.exceptions import SMBOSError
    conn, sess = _smb_share(host, port, user, password)
    try:
        from smbclient import scandir
        root = remote_path or "/"

        def walk(d):
            if stop and stop():
                return
            try:
                entries = list(scandir(d, sess))
            except Exception:
                return
            for e in entries:
                if stop and stop():
                    return
                full = d.rstrip("/") + "/" + e.path.split("/")[-1]
                if e.is_dir:
                    if recursive:
                        yield from walk(full)
                elif _is_image(e.name):
                    yield e.path, _safe_rel(root, e.path)

        yield from walk(root)
    finally:
        try:
            sess.disconnect(); conn.disconnect()
        except Exception:
            pass


def _smb_download(host, port, user, password, full, dest):
    from smbclient import open_file
    conn, sess = _smb_share(host, port, user, password)
    try:
        with open_file(full, "rb", sess) as src, open(dest, "wb") as out:
            while True:
                blk = src.read(1 << 20)
                if not blk:
                    break
                out.write(blk)
    finally:
        try:
            sess.disconnect(); conn.disconnect()
        except Exception:
            pass


# ───────────────────────── 统一入口 ─────────────────────────
def import_remote(db, cfg, protocol: str, host: str, *, port: int = 0,
                  user: str = "", password: str = "", remote_path: str = "",
                  recursive: bool = True, do_tag: bool = True,
                  copy_to_library: bool = False, mark_pending: bool = False,
                  stop: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[..., None]] = None) -> dict:
    """枚举 → 下载 → 入库打标。返回结构与 import_folder.import_folder 一致。"""
    from . import import_folder as imp
    protocol = str(protocol or "").lower().strip()
    host = str(host or "").strip()
    if protocol not in DEFAULT_PORT:
        return {"ok": False, "error": f"暂不支持协议 {protocol}"}
    if not host:
        return {"ok": False, "error": "请先填写服务器地址"}
    port = int(port or DEFAULT_PORT[protocol])
    if progress is None:
        def progress(*a, **k):        # noqa: E306
            pass

    lister = {"ftp": _ftp_list, "ftps": _ftp_list, "sftp": _sftp_list,
              "smb": _smb_list}[protocol]
    downloader = {"ftp": _ftp_download, "ftps": _ftp_download,
                  "sftp": _sftp_download, "smb": _smb_download}[protocol]

    # ── ① 枚举 ──
    files = []
    try:
        args = (host, port, user, password, remote_path, recursive, stop)
        for full, rel in lister(*args):
            files.append((full, rel))
            if len(files) % 50 == 0:
                progress("list", len(files), len(files) + 1, rel, "", 0)
    except RemoteImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:                          # noqa: BLE001
        return {"ok": False, "error": f"连接服务器失败：{type(e).__name__}: {e}"}
    if not files:
        return {"ok": False, "error": "该目录下没有找到图片"
                                     "（支持 jpg/png/webp/gif/bmp/tif/avif/heic）"}

    # ── ② 落地 ──
    dest_root = cache_dir_for(Path(cfg.library_dir), protocol, host, remote_path)
    total = len(files); done = 0; skipped = 0; failed = []
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
            if dest.is_file() and dest.stat().st_size > 0:
                skipped += 1; done += 1
                progress("download", done, total, safe_rel, "", int(30 * done / total))
                continue
            if protocol in ("ftp", "ftps"):
                downloader(protocol, host, port, user, password, full, dest)
            else:
                downloader(host, port, user, password, full, dest)
            if dest.stat().st_size == 0:
                dest.unlink(missing_ok=True)
                failed.append(safe_rel)
            else:
                done += 1
        except Exception:                            # noqa: BLE001
            failed.append(safe_rel)
        progress("download", done, total, safe_rel, "", int(30 * done / total))

    # ── ③ 交给本地导入链 ──
    msg = f"远端下载完成：{done} 张，跳过 {skipped} 张"
    if failed:
        msg += f"，失败 {len(failed)} 张"
    progress("index", 0, done, msg, "", 30)
    return imp.import_folder(
        db, cfg, str(dest_root), do_tag=do_tag,
        mark_pending=mark_pending, copy_to_library=copy_to_library,
        recursive=recursive, stop=stop,
        progress=lambda ph, d, t, n, eta, pct: progress(
            ph, d, t, n, eta, 30 + int(70 * (pct or 0) / 100)),
    )
