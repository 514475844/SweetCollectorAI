"""Auto-start registration.

Preferred method: a launcher .bat inside the user's Windows Startup folder
  %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\
This needs no admin rights, no COM automation and no scheduled-task service.

Note: `schtasks.exe` is blocked by this machine's security policy, so the
Task Scheduler path is intentionally NOT used here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# 命名沿用历史（已装过自启的机器上还能被 status / remove 找到），
# 但 V0.0.36 起 bat 里写的命令只指向 SweetWeb.exe，不再有旧桌面 exe。
TASK_NAME = "MediaResourceManager"
BAT_NAME = "MediaResourceManager.bat"


def _exe() -> Path:
    """Locate the executable (frozen build) or fall back to python -m."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    p = Path(sys.prefix) / "Scripts" / "mrm.exe"
    if p.exists():
        return p
    return Path(sys.executable)


def _command(interval: int = 3600, concurrency: int = 2) -> str:
    exe = _exe()
    if getattr(sys, "frozen", False):
        # V0.0.36：唯一可执行体是 SweetWeb.exe，开机自启一律「带界面地进托盘」。
        # 旧桌面 exe 已废除，原来的 `serve` 分支（无窗口、无托盘，用户看不见
        # 它在跑）不再有对应程序，直接去掉。`--tray` 只影响这一次启动。
        return f'"{exe}" --tray'
    if exe.name == "mrm.exe":
        return f'"{exe}" serve --interval {interval} --concurrency {concurrency}'
    return (f'"{exe}" -m media_resource_manager serve '
            f'--interval {interval} --concurrency {concurrency}')


def startup_dir() -> Path | None:
    if not sys.platform.startswith("win"):
        return None
    base = os.environ.get("APPDATA")
    if not base:
        return None
    return (Path(base) / "Microsoft" / "Windows" / "Start Menu" /
            "Programs" / "Startup")


def bat_path() -> Path | None:
    d = startup_dir()
    return (d / BAT_NAME) if d else None


def supported() -> bool:
    return startup_dir() is not None


def install(interval: int = 3600, concurrency: int = 2) -> tuple[bool, str]:
    """Create the startup launcher. Returns (ok, detail)."""
    p = bat_path()
    if not p:
        return False, "unsupported platform (Windows only)"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        exe = _exe()
        workdir = str(Path(exe).parent)
        content = (
            "@echo off\r\n"
            # chcp 65001 makes cmd read this UTF-8 file correctly (needed when
            # the install path contains non-ASCII characters such as Chinese).
            # 与 shortcut.py 的 .bat 兜底保持同一口径（R152-F5）。
            "chcp 65001 >nul\r\n"
            f'cd /d "{workdir}"\r\n'
            f"start \"\" /min {_command(interval, concurrency)}\r\n"
            "exit /b 0\r\n"
        )
        # newline=""：内容自带 \r\n，文本模式会把 \n 再转译成 \r\n，
        # 写出 \r\r\n（cmd 侧表现为空行/行为偏移）。必须二进制级精确写。
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        return True, f"created {p}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def remove() -> tuple[bool, str]:
    p = bat_path()
    if not p:
        return False, "unsupported platform (Windows only)"
    try:
        if p.exists():
            p.unlink()
            return True, f"removed {p}"
        return True, "not installed"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def status() -> tuple[bool, str]:
    """Return (installed, detail)."""
    p = bat_path()
    if not p:
        return False, "unsupported platform (Windows only)"
    if p.exists():
        return True, f"{p} -> {_command()}"
    return False, "not installed"
