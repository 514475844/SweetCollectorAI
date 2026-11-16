"""子进程统一登记 + 「不弹黑控制台窗口」的 spawn（R184 / R185）。

为什么单独一个**不依赖 Qt** 的模块：
  * 登记处必须是**进程级单例**：桌面 GUI、engine 的 gallery-dl 快路径、
    未来的 Docker 无头端，谁 spawn 的子进程都要汇总到一处，退出时才能一次
    杀干净（`gui/lifecycle.force_quit` 就是靠这张表）。
  * 它放在 `media_resource_manager/` 根而不是 `gui/` 下：`gui/__init__.py`
    会带进 PySide6，而 Docker 无头端与 CLI **没有** Qt —— 放在 gui 下会让
    无头端一 import engine 就炸（R130 的 A1 红线「不碰 PySide6」）。

R184（黑控制台窗口）：Windows 下 `subprocess` 默认会让控制台程序新建一个
黑色 conhost 窗口。gallery-dl.exe 就是控制台程序 ⇒ 用户每次采集都被黑窗打扰，
而且**黑窗一弹出就抢走键盘焦点**，这正是 R182「打游戏时 Ctrl 失灵」的物理
成因之一（不是全局钩子，是焦点被抢）。所以所有 spawn 一律带
`CREATE_NO_WINDOW`(+`STARTF_USESHOWWINDOW`)，并在 PyInstaller 的
`console=False` 之外再加一道保险。

R185（退出不彻底）：子进程不登记 = 退出时杀不掉 = 「托盘点退出后黑窗还在跑」。
`live_children()` / `kill_children()` 就是给 `force_quit()` 用的。
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Any, Iterable, Optional

#: Windows 控制台隐藏标志（PEB 级，不新建 conhost 窗口）
CREATE_NO_WINDOW = 0x08000000

#: R185 兜底清扫的进程名（只可能是本程序拉起的那些）
SWEEP_NAMES: tuple[str, ...] = ("gallery-dl.exe",)

_LOCK = threading.RLock()
_CHILDREN: list[Any] = []          # 活着的 subprocess.Popen


# ─────────────────────────────────────────────────────────────────────
# 1) 隐藏窗口的 spawn 参数
# ─────────────────────────────────────────────────────────────────────
def no_window_kwargs() -> dict:
    """给 `subprocess.Popen/run` 用的「别弹黑窗」参数（非 Windows 返回空）。

    ⚠ 两条一起给才稳：`creationflags=CREATE_NO_WINDOW` 管大多数情况；
    `STARTUPINFO.dwFlags|=STARTF_USESHOWWINDOW` + `wShowWindow=0` 是
    「父进程自己就是控制台程序」时的补充路径（那时 creationflags 不生效）。
    """
    kw: dict[str, Any] = {}
    if os.name != "nt":
        return kw
    kw["creationflags"] = int(CREATE_NO_WINDOW)
    try:
        si = subprocess.STARTUPINFO()                     # type: ignore[attr-defined]
        si.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0x1))
        si.wShowWindow = 0
        kw["startupinfo"] = si
    except Exception:
        # 拿不到 STARTUPINFO（非 Windows / 被裁剪）就只靠 creationflags
        kw.pop("startupinfo", None)
    return kw


def spawn_hidden(cmd: list[str], register: bool = True, **kw) -> subprocess.Popen:
    """起子进程：**不弹黑窗** + 默认 DEVNULL + 默认登记。

    R185 第 3 条：`stdout/stderr` 不接管时一律 DEVNULL —— 继承 pipe 会让
    父进程在退出时等子进程写端 EOF，卡住退出路径。
    """
    for k, v in no_window_kwargs().items():
        kw.setdefault(k, v)
    kw.setdefault("stdin", subprocess.DEVNULL)
    if "stdout" not in kw:
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
    proc = subprocess.Popen(cmd, **kw)                    # noqa: S603
    if register:
        register_child(proc)
    return proc


# ─────────────────────────────────────────────────────────────────────
# 2) 子进程登记表
# ─────────────────────────────────────────────────────────────────────
def _prune_locked() -> None:
    """把已经退出的 Popen 摘掉（否则表会随采集时长无限增长）。"""
    for p in list(_CHILDREN):
        try:
            if p.poll() is not None:
                _CHILDREN.remove(p)
        except Exception:
            try:
                _CHILDREN.remove(p)
            except ValueError:
                pass


def register_child(proc) -> None:
    if proc is None:
        return
    with _LOCK:
        _prune_locked()
        if proc not in _CHILDREN:
            _CHILDREN.append(proc)


def unregister_child(proc) -> None:
    if proc is None:
        return
    with _LOCK:
        try:
            _CHILDREN.remove(proc)
        except ValueError:
            pass


def live_children() -> list:
    """还活着的子进程快照（已退出的会被顺手清掉）。"""
    with _LOCK:
        _prune_locked()
        return list(_CHILDREN)


def clear_children() -> None:
    """只清表不杀进程（测试用；真要杀请用 kill_children）。"""
    with _LOCK:
        _CHILDREN.clear()


def kill_children(grace: float = 0.6) -> int:
    """终止所有登记过的子进程：先 terminate，宽限期后 kill。返回处理个数。

    两段式是必要的：`terminate()` 在 Windows 上对没在跑消息循环的控制台
    程序（gallery-dl 正在下载时）可能停在原地，只 terminate 的话「退出后
    黑窗还在」照样复现。
    """
    victims = live_children()
    n = 0
    for p in victims:
        try:
            if p.poll() is None:
                p.terminate()
                n += 1
        except Exception:
            pass
    if n and grace > 0:
        deadline = time.monotonic() + float(grace)
        while time.monotonic() < deadline:
            if all(_done(p) for p in victims):
                break
            time.sleep(0.02)
    for p in victims:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    with _LOCK:
        _prune_locked()
    return n


def _done(p) -> bool:
    try:
        return p.poll() is not None
    except Exception:
        return True


def taskkill_cmd(name: str) -> Optional[list[str]]:
    """R185 兜底：按进程名强杀的命令（只在 Windows 有意义）。

    ⚠ 必须带 `/T`（杀进程树）：直接 spawn 的 gallery-dl.exe 自己还会再
    拉子进程，只杀父进程等于没杀。
    """
    if os.name != "nt" or not name:
        return None
    return ["taskkill", "/F", "/T", "/IM", str(name)]


def sweep_named(names: Iterable[str] = SWEEP_NAMES, timeout: float = 2.0) -> int:
    """按名字兜底清扫残留（登记表漏掉的那些）。返回下过的命令条数。

    只在 Windows 上真跑；失败一律吞掉 —— 兜底动作绝不能反过来阻止退出。
    """
    if os.name != "nt":
        return 0
    ran = 0
    for nm in tuple(names or ()):
        cmd = taskkill_cmd(nm)
        if not cmd:
            continue
        try:
            subprocess.run(cmd, timeout=max(0.5, float(timeout)),
                           stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           **no_window_kwargs())
            ran += 1
        except Exception:
            pass
    return ran
