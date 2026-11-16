"""Optional high-quality download backend powered by gallery-dl.

Why: gallery-dl already implements multi-level gallery discovery, original
image resolution picking, retries, rate limits, proxies, filename templating
and a download archive for hundreds of extractors. Reinventing that is a waste
of time, so we CALL it instead of crawling those sites ourselves.

Requires: pip install gallery-dl  (optional but strongly recommended)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import queue as _queue
import threading as _threading
import time as _time
from pathlib import Path
from typing import Optional

from ..logutil import get_logger

LOG = get_logger("mrm.gallery_dl")

from .. import childproc


GALLERY_DL_CMD = None  # resolved lazily


# ── R173 §3：gallery-dl 快路径的代理稳健化 ──────────────────────────────
#
# 真机事实（订单 §3 点名）：gallery-dl 1.32.16 的 socks5 支持要 `socksio`，
# 而打进包的那个二进制**没带**它 ⇒ 传 `socks5://…` 进去必然
# `InvalidSchema: Missing dependencies for SOCKS support`。用户看到的现象是
# 「快路径=True、一张图都没下、任务却只写着 error」——比直接报错更难查。
#
# 两个动作（缺一不可）：
#   ① 同 host:port 的 `http://` 明显可用时（本机混合端口场景）→ 换成
#      `http://` 传给 gallery-dl，代理照走，最理想；
#   ② 换不了（用户确实只开了 SOCKS 端口）→ **降级为不用代理** + 中文日志，
#      绝不让任务直接死（与 crawler/fetcher.py 的 R48-D1 同一个处理哲学）。
_SOCKS_SCHEMES = ("socks5://", "socks5h://", "socks4://", "socks4a://")

#: 降级时给用户看的中文说明（与 fetcher.py 的 SOCKS_HINT_ZH 同口径）
GD_SOCKS_HINT_ZH = ("gallery-dl 自带的 socks 支持不可用（缺 socksio 组件），"
                    "已将快路径降级为不使用代理，任务继续")


# ── R152-C2：流式下载的「总超时」看门狗 ──────────────────────────────
#
# 真机病根：`download_iter` 用 `for raw in proc.stdout` 逐行读 —— 只要子进程
# 既不退出也不再吐字（站点挂起 / 网络黑洞 / gallery-dl 卡在自己的重试里），
# 这个 for **永远不返回**。任务卡片一直显示"进行中"，日志干干净净，
# 用户只能靠杀进程收场（非流式的 download() 有 subprocess.run(timeout=180)
# 兜着，流式这条没有）。
#
# 对策：stdout 静默超过 idle_timeout 秒即判死 → terminate/kill → 正常收尾
# （已经落盘的图照常入库；重跑靠 skip=True 不会重复下）。
DEFAULT_IDLE_TIMEOUT = 120.0

GD_IDLE_HINT_ZH = ("gallery-dl 已连续 {sec} 秒没有任何输出，判定为卡死并终止本次"
                   "下载；已下载的文件照常入库，重新运行会跳过它们")

# R152-C5：扫盘兜底的 mtime 容差（秒）。文件系统时间戳精度/时钟抖动会让
# 「刚开始那一刻写的文件」的 mtime 略早于我们记录的开始时刻。
_SWEEP_MTIME_SLACK = 2.0

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


def iter_stdout_lines(proc, idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                      state: Optional[dict] = None):
    """逐行读子进程 stdout，静默超时即收工（R152-C2 看门狗）。

    `state["idle"]=True` 表示**静默超时**（不是正常 EOF），调用方据此决定
    要不要杀进程、要不要给用户留中文提示。

    为什么必须绕开 `for line in proc.stdout`：那个循环会一路阻塞到子进程
    退出，卡死的子进程永远等不到 —— 看门狗不是加在循环里，而是得把读行
    这件事搬到后台线程，主线程才腾得出手掐时间。
    """
    st = state if state is not None else {}
    st.setdefault("idle", False)
    st.setdefault("eof", False)
    if getattr(proc, "stdout", None) is None:
        st["eof"] = True
        return
    q: "_queue.Queue" = _queue.Queue()

    def _pump():
        try:
            for raw in proc.stdout:
                q.put(raw)
        except Exception as e:
            # V0.0.6：以前读 stdout 出错被 `pass` 吞掉，仍然发「正常结束」
            # 哨兵 —— 真因凭空消失，用户只看到「这个站一张图都没抓到」。
            # 现在把错误交给消费端记下来。
            q.put(e)
        finally:
            q.put(None)                      # EOF 哨兵

    _threading.Thread(target=_pump, daemon=True).start()
    while True:
        try:
            raw = q.get(timeout=idle_timeout)
        except Exception:                    # queue.Empty（超时）
            st["idle"] = True
            return
        if raw is None:
            st["eof"] = True
            return
        if isinstance(raw, Exception):
            st["eof"] = True
            st["error"] = str(raw)
            try:
                from ..logutil import get_logger
                get_logger().warning("gallery-dl 输出读取失败：%s", raw)
            except Exception:
                pass
            return
        yield raw


def sweep_new_files(dest: Path, seen: set, since: float, exts=_IMAGE_SUFFIXES):
    """收尾扫盘：只 yield **本轮**（since 之后）落盘且没在 stdout 里出现过的图。

    R152-C5：以前这里是无差别 `dest.rglob("*")` —— 前几轮跑过、被
    skip=True 跳过的历史文件会被当成"新下载"再 yield 一遍，于是重跑 =
    全量重处理（去重、AI 打分、入库全做第二遍，慢得毫无道理）。
    """
    for p in sorted(dest.rglob("*")):
        try:
            if not p.is_file() or str(p) in seen:
                continue
            if p.suffix.lower() not in exts:
                continue
            if p.stat().st_mtime < since:
                continue
        except OSError:
            continue
        seen.add(str(p))
        yield p


def is_socks_url(url: Optional[str]) -> bool:
    """是不是 SOCKS 代理 URL（决定要不要做转换/降级）。"""
    return bool(url) and str(url).strip().lower().startswith(_SOCKS_SCHEMES)


def socks_to_http(url: Optional[str]) -> Optional[str]:
    """SOCKS 代理 → 同 host:port 的 http:// 代理（本机混合端口场景）。

    只在**端口真的能连上**时才换 —— 换成一个没人听的端口比不换更糟
    （用户会看到一堆连接超时，而不是一句「已降级」）。返回 None = 换不了。
    """
    if not is_socks_url(url):
        return url
    s = str(url).strip()
    tail = s.split("://", 1)[1] if "://" in s else s
    hostport = tail.split("@", 1)[-1]          # 去掉可能的 user:pass@
    if ":" not in hostport:
        return None
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        return None
    auth = (tail.split("@", 1)[0] + "@") if "@" in tail else ""
    try:
        import socket
        sock = socket.create_connection((host, int(port)), timeout=0.5)
        sock.close()
    except Exception:
        return None
    return f"http://{auth}{host}:{port}"


# ── R184：入口 URL 过滤（裸首页不喂给 gallery-dl）──────────────────
#
# 真机事实：任务里存的入口 URL 常常是**站点首页**（用户从收藏夹拖的）。
# 这种 URL 交给 gallery-dl 基本无图可下，却一定会拉起一次 gallery-dl.exe
# ⇒ 弹黑控制台窗口（用户 10-22 截图点名的那个），还抢走键盘焦点。
#
# ⚠ 判定**宁松勿紧**：只有路径真的是根（`""` / `"/"` / `/index.html` 这类）
# 才算「裸首页」。带任何路径段的（列表页 / 频道页 / 搜索页 / 具体图集）
# 一律照旧交给 gallery-dl —— 一刀切会把「首页就是图集列表」的站点打死，
# 那种损失比弹一次黑窗大得多。
_INDEX_NAMES = {
    "index.html", "index.htm", "index.php", "index.asp", "index.aspx",
    "default.html", "default.htm", "default.aspx", "home.html", "home.htm",
}


def is_bare_homepage(url: Optional[str]) -> bool:
    """入口 URL 是不是站点裸首页（没有指向任何具体图集/帖子）。"""
    if not url:
        return True
    s = str(url).strip()
    if not s:
        return True
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(s if "//" in s else "//" + s)
        path = (parts.path or "").strip()
    except Exception:
        return False
    while path.endswith("/"):
        path = path[:-1]
    if not path:
        return True
    last = path.rsplit("/", 1)[-1].lower()
    return last in _INDEX_NAMES


def _exe() -> Optional[str]:
    global GALLERY_DL_CMD
    if GALLERY_DL_CMD:
        return GALLERY_DL_CMD
    # 1) frozen one-folder build: a gallery-dl.exe sits next to the app exe
    #    (built via PyInstaller entry_point=gallery_dl.__main__:main).
    frozen = Path(sys.executable).with_name("gallery-dl.exe")
    if frozen.exists():
        GALLERY_DL_CMD = str(frozen)
        return GALLERY_DL_CMD
    exe = shutil.which("gallery-dl")
    if not exe:
        # installed into the same venv -> .Scripts/gallery-dl.exe
        cand = Path(sys.prefix) / "Scripts" / "gallery-dl.exe"
        if cand.exists():
            exe = str(cand)
        cand2 = Path(sys.prefix) / "bin" / "gallery-dl"
        if not exe and cand2.exists():
            exe = str(cand2)
    GALLERY_DL_CMD = exe
    return exe


def installed() -> bool:
    return _exe() is not None


def supports(url: str) -> bool:
    """Offline check: does gallery-dl have an extractor for this URL?"""
    try:
        from gallery_dl import extractor  # type: ignore
        return extractor.find(url) is not None
    except Exception:
        return False


class GalleryDLBackend:
    def __init__(self, dest_root: Path, proxy: Optional[str] = None,
                 retries: int = 5, timeout: int = 30, sleep: float = 1.0,
                 rate_limit: Optional[str] = None,
                 directory: Optional[list[str]] = None,
                 filename: str = "{filename}.{extension}",
                 skip_existing: bool = True,
                 write_metadata: bool = False,
                 image_only: bool = True,
                 user_agent: str = "",
                 cookie_browser: str = "",
                 idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                 verify: bool = True):
        self.dest_root = Path(dest_root)
        # R152-C2：流式下载的静默上限（秒）。<=0 表示不设看门狗（＝旧行为）。
        self.idle_timeout = float(idle_timeout)
        # R189：真实浏览器 UA + 浏览器 Cookie。站点（danbooru/konachan 等）
        # 按 UA 挡机器人 —— 默认的 MediaResourceManager UA 直接 403。
        self.user_agent = user_agent or ""
        self.cookie_browser = cookie_browser or ""
        # 2026-10-28（编号 33）：证书校验。gallery-dl 的开关叫 extractor.verify，
        # 关掉=不校验证书（自签站点唯一能让快路径走通的办法）。
        self.verify = bool(verify)
        # ---- R173 §3：socks → http 转换 / 缺 socksio 时降级（单处接线）----
        # `self.proxy` 保持**用户填的原值**（UI/日志要显示），真正传给
        # gallery-dl 的是 `self.effective_proxy`。降级/转换时都要留中文提示，
        # 否则用户只会看到「快路径=True、一张图都没下」。
        self.proxy = proxy
        self.notice: str = ""
        self.degraded: bool = False
        if is_socks_url(proxy):
            _http = socks_to_http(proxy)
            if _http:
                self.effective_proxy = _http
                self.notice = (f"SOCKS 代理已转为 HTTP 代理交给 gallery-dl："
                               f"{_http}（gallery-dl 自身不支持 socks，"
                               f"同端口 http 可用）")
            else:
                self.effective_proxy = None
                self.degraded = True
                self.notice = GD_SOCKS_HINT_ZH
        else:
            self.effective_proxy = proxy
        self.retries = retries
        self.timeout = timeout
        self.sleep = sleep
        self.rate_limit = rate_limit
        self.directory = directory or ["{category}", "{subcategory}"]
        self.filename = filename
        self.skip_existing = skip_existing
        # r36：download_iter()->_build_cmd()->_make_config() 会读 self.write_metadata，
        # 但 __init__ 以前只赋了 self.skip_existing，漏了这一行 —— 结果所有走
        # gallery-dl 快路径的任务在 download_iter 里直接 AttributeError，
        # 一张图都没落、任务却只写了个 error（抽图本身其实是好的）。
        self.write_metadata = write_metadata
        # r36：本工具是「图片/图库」采集器。gallery-dl 对 erome 这类站会把
        # 同相册里的 .mp4/.webm 视频一起列出来，落下来就是一堆视频垃圾。
        # 默认只收图片扩展名（缺 extension 元数据时 filter 为空表达式反而安全：
        # gallery-dl 的 filter 为空字符串等于不过滤）。
        self.image_only = image_only
        self._proc: Optional[subprocess.Popen] = None

    def stop(self):
        """终止正在运行的 gallery-dl 子进程（任务停止/取消时调用）。

        R185：只 `terminate()` 不够 —— 正在下载的 gallery-dl 不理这个信号时
        进程会一直挂着（用户看到的就是「停止了但黑窗还在跑」）。给一小段
        宽限期后补 `kill()`。⚠ 只针对**自己这一个**子进程，不能用
        `childproc.kill_children()`（那会把别的任务的下载一起杀掉）。
        """
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
            try:
                p.wait(timeout=0.6)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def _make_config(self) -> dict:
        ex: dict = {
            "base-directory": str(self.dest_root),
            "directory": self.directory,
            "filename": self.filename,
            "retries": self.retries,
            "timeout": self.timeout,
            "verify": self.verify,
        }
        if self.effective_proxy:
            ex["proxy"] = self.effective_proxy
        if self.sleep:
            # polite pacing between requests
            ex["sleep"] = self.sleep
        if self.rate_limit:
            ex["rate"] = self.rate_limit
        if self.skip_existing:
            # downloads already present are skipped (idempotent re-runs)
            ex["skip"] = True
        cfg = {"extractor": ex}
        # R189：把 UA / Cookie 传进 gallery-dl 配置。
        # - "user-agent" 是 gallery-dl 的**全局**选项（extractor 段不认它）；
        # - "cookies-from-browser" 是**extractor 级**选项：从指定浏览器的
        #   cookie 库里带登录态。冻结版 gallery-dl 若不支持该选项，会在
        #   子进程 stderr 里报 config 错误 —— 那只影响 Cookie，不影响 UA，
        #   任务照跑（0 图时 R188 兜底会标红，不会假绿）。
        if self.user_agent:
            cfg["user-agent"] = self.user_agent
        if self.cookie_browser:
            ex["cookies-from-browser"] = self.cookie_browser
        if self.image_only:
            ex["filter"] = ("extension in ('jpg','jpeg','png','webp',"
                            "'gif','bmp')")
        if self.write_metadata:
            # write per-file .json next to each image to preserve provenance
            cfg["postprocessor"] = [{"name": "metadata", "mode": "json",
                                     "event": "file"}]
        return cfg

    def download(self, url: str, dest_subdir: str = "",
                 extra_args: Optional[list[str]] = None) -> tuple[list[Path], str]:
        """Download via gallery-dl. Returns (list of NEWLY created files, log)."""
        if not installed():
            return [], "gallery-dl not installed (pip install gallery-dl)"
        dest = self.dest_root / dest_subdir if dest_subdir else self.dest_root
        dest.mkdir(parents=True, exist_ok=True)
        before = {str(p) for p in dest.rglob("*") if p.is_file()}

        cfg = self._make_config()
        cfg["extractor"]["base-directory"] = str(dest)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(cfg, f)
            cfg_path = f.name

        cmd = [_exe(), "-c", cfg_path]
        if extra_args:
            cmd += extra_args
        cmd.append(url)

        try:
            # R184：不弹黑控制台窗口（creationflags + STARTUPINFO 双保险）
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=180, encoding="utf-8",
                                  errors="replace",
                                  **childproc.no_window_kwargs())
            log = (proc.stdout or "") + (proc.stderr or "")
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            log, rc = "timeout", 1
        except (OSError, ValueError) as e:
            # V0.0.6：程序路径没权限 / 文件被删 / 打包版缺 exe 时，
            # subprocess.run 抛 FileNotFoundError / PermissionError，
            # 以前直接穿出调用链，任务没任何中文提示就中断。
            log, rc = f"启动下载程序失败：{type(e).__name__}: {e}", 1
        finally:
            try:
                os.unlink(cfg_path)
            except Exception:
                pass

        after = {str(p) for p in dest.rglob("*") if p.is_file()}
        new = sorted(after - before)
        return [Path(p) for p in new], f"rc={rc} {log[-2000:]}"

    def _build_cmd(self, url: str, dest: Path,
                   extra_args: Optional[list[str]] = None) -> tuple[list[str], Path]:
        dest.mkdir(parents=True, exist_ok=True)
        cfg = self._make_config()
        cfg["extractor"]["base-directory"] = str(dest)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(cfg, f)
            cfg_path = f.name
        cmd = [_exe(), "-c", cfg_path]
        if extra_args:
            cmd += extra_args
        cmd.append(url)
        return cmd, Path(cfg_path)

    def download_iter(self, url: str, dest_subdir: str = "",
                      extra_args: Optional[list[str]] = None):
        """增量下载：gallery-dl 每落一个文件就立即 yield 它的路径。

        与 download() 的区别：不等整次任务跑完才返回（一个首页会翻几百页，
        同步等到最后意味着整个任务期间数据库里一张图都没有）。
        在 worker 线程里迭代；stop() 可终止子进程。
        """
        import re as _re
        if not installed():
            return
        dest = self.dest_root / dest_subdir if dest_subdir else self.dest_root
        cmd, cfg_path = self._build_cmd(url, dest, extra_args)
        seen: set[str] = set()
        ansi = _re.compile(r"\x1b\[[0-9;]*m")
        # R152-C5：本轮开始时刻（含精度容差）。收尾扫盘只认这一刻之后落盘的文件。
        started = _time.time() - _SWEEP_MTIME_SLACK
        proc = None
        try:
            # R184：不弹黑控制台窗口；R185：登记到进程级表，退出时杀得掉。
            # stdout 必须留 PIPE（我们靠它逐行读落盘文件），所以不能用
            # spawn_hidden 的 DEVNULL 默认值，这里手工拼参数。
            proc = childproc.spawn_hidden(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
            self._proc = proc
            assert proc.stdout is not None
            _st: dict = {}
            for raw in iter_stdout_lines(proc, self.idle_timeout, _st):
                line = ansi.sub("", raw).strip().strip('"')
                if not line or " " in line and not Path(line).exists():
                    pass
                # 2026-10-26：gallery-dl 的 [error]/[warning] 行以前被直接
                # 丢弃 → 真因（Cloudflare 403 等）只在子进程里一闪而过，用户
                # 只能看到「未获取到任何结果」。现在原样记进日志便于诊断。
                if line.startswith("[") and ("error" in line.lower()
                                             or "warning" in line.lower()):
                    try:
                        LOG.warning("gallery-dl: %s", line[:300])
                    except Exception:
                        pass
                p = Path(line) if not line.startswith(("#", "[")) else None
                try:
                    if p is not None and p.exists() and p.is_file():
                        if str(p) not in seen:
                            seen.add(str(p))
                            yield p
                except OSError:
                    continue
            if _st.get("idle"):
                # R152-C2：看门狗触发（子进程卡死）。杀掉并给用户留一句中文，
                # **不抛异常** —— 已经落盘的图照常走下面的收尾扫盘入库。
                self.stop()
                self.notice = GD_IDLE_HINT_ZH.format(sec=int(self.idle_timeout))
            else:
                # 正常 EOF：给个有界的回收等待（极端情况下 wait 也会挂）。
                try:
                    proc.wait(timeout=5)
                except Exception:
                    self.stop()
            # 收尾兜底：补上没在 stdout 里出现的**本轮新文件**（R152-C5）
            for p in sweep_new_files(dest, seen, started):
                yield p
        finally:
            self._proc = None
            if proc is not None:
                try:
                    childproc.unregister_child(proc)
                except Exception:
                    pass
            try:
                os.unlink(cfg_path)
            except Exception:
                pass
