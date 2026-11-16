"""监视文件夹（编号 13）：把网址清单丢进一个目录，自动建任务。

用户场景（这是 qBittorrent「自动从此处添加」的等价物）：
  手机上看到一个站，随手把网址存成 txt 丢进共享文件夹 → 电脑自动开工，
  不用再打开软件、点新建、粘贴、点保存。

约定：
* 只认 `.txt` 文件；一行一个网址；`#` 开头是注释；空行忽略；
* 只接受 `http://` / `https://`（其它行当作说明文字跳过）；
* 处理完的文件移动到 `<监视目录>/已处理/`，**不删除**（用户可能想复查）；
* 处理出错（没有可用网址）移到 `<监视目录>/失败/`，并在文件名后加原因后缀；
* 同一个文件不会被处理两次（移动即出队，天然去重）；
* 线程是 daemon，`stop()` 置事件后最多等一个扫描间隔就退出。

设计取舍：
* **不做文件监听（watchdog）**：轮询 30 秒足够（用户手动丢文件的场景），
  而 watchdog 要额外的 OS 句柄与依赖，出问题还难排查。
* 解析失败**不抛异常**：一个乱七八糟的 txt 不该让整个监视器停摆。
"""
from __future__ import annotations

import queue
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

_URL_RE = re.compile(r"^https?://[^\s]+$", re.I)


def parse_urls(text: str) -> list[str]:
    """从文本里挑出网址（保持顺序、去重）。"""
    out, seen = [], set()
    for raw in str(text or "").splitlines():
        s = raw.split("#", 1)[0].strip()
        if not s or not _URL_RE.match(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


class WatchFolder:
    """监视目录的轮询线程。

    `on_urls(urls: list[str], name: str) -> int` 由宿主提供：拿网址列表建任务，
    返回成功建了几个（用于日志）。回调里**自己处理异常**，不要往这边抛。
    """

    def __init__(self, directory: str | Path, on_urls: Callable[[list, str], int],
                 interval: float = 30.0, log: Optional[Callable[[str], None]] = None):
        self.dir = Path(directory) if directory else None
        self.on_urls = on_urls
        self.interval = max(5.0, float(interval or 30))
        self.log = log or (lambda m: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.processed = 0
        self.failed = 0

    # ---- 生命周期 ----
    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self.dir:
            return False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as ex:                       # noqa: BLE001
            self.log(f"监视文件夹不可用：{type(ex).__name__}: {ex}")
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mrm-watchdir",
                                        daemon=True)
        self._thread.start()
        self.log(f"已开始监视文件夹：{self.dir}（每 {int(self.interval)} 秒扫一次）")
        return True

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- 主循环 ----
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as ex:                   # noqa: BLE001
                self.log(f"监视文件夹扫描出错：{type(ex).__name__}: {ex}")
            self._stop.wait(self.interval)

    def scan_once(self) -> int:
        """扫一遍，返回处理了几个文件（测试直接调它，不用等轮询）。"""
        if not self.dir or not self.dir.exists():
            return 0
        n = 0
        try:
            files = sorted([p for p in self.dir.iterdir()
                            if p.is_file() and p.suffix.lower() == ".txt"])
        except Exception:
            return 0
        for f in files:
            if self._stop.is_set():
                break
            self._handle(f)
            n += 1
        return n

    def _handle(self, f: Path) -> None:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as ex:                       # noqa: BLE001
            self._move(f, "失败", f"读不出内容_{type(ex).__name__}")
            self.failed += 1
            return
        urls = parse_urls(text)
        if not urls:
            self._move(f, "失败", "没有可用网址")
            self.failed += 1
            self.log(f"监视文件夹：{f.name} 里没有可用网址，已移到「失败」")
            return
        made = 0
        try:
            made = int(self.on_urls(urls, f.stem) or 0)
        except Exception as ex:                       # noqa: BLE001
            self._move(f, "失败", f"建任务失败_{type(ex).__name__}")
            self.failed += 1
            self.log(f"监视文件夹：{f.name} 建任务失败：{ex}")
            return
        self._move(f, "已处理", None)
        self.processed += 1
        self.log(f"监视文件夹：{f.name} → 新建 {made} 个任务（共 {len(urls)} 个网址）")

    def _move(self, f: Path, sub: str, reason: Optional[str]) -> None:
        """把处理过的清单移入子目录（不删除：用户可能想复查）。"""
        try:
            dst_dir = (self.dir or f.parent) / sub
            dst_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%H%M%S")
            name = f"{f.stem}_{stamp}{('_' + reason) if reason else ''}{f.suffix}"
            shutil.move(str(f), str(dst_dir / name))
        except Exception as ex:                       # noqa: BLE001
            self.log(f"监视文件夹：移动 {f.name} 失败（{type(ex).__name__}），忽略")
