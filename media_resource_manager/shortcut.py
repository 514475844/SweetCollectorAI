"""Desktop launcher creation (no COM, no admin).

Why not a real .lnk: writing .lnk needs IShellLink/WScript.Shell COM, and
WScript.Shell instantiation is blocked by this machine's security policy.
So we create a Windows `.url` internet-shortcut instead, which:
  * double-clicks like a shortcut
  * supports IconFile so it shows the app icon
  * produces NO console window
  * needs neither COM nor administrator rights

A .bat launcher is available as a fallback for exotic setups.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_TITLE = "Sweet采集AI版"


def desktop_dir() -> Path | None:
    home = Path(os.path.expanduser("~"))
    # honour relocated shell folders when possible
    try:
        import ctypes
        from ctypes import wintypes
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOPDIRECTORY = 0
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf) == 0:
            p = Path(buf.value)
            if p.exists():
                return p
    except Exception:
        pass
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop"):
        if cand.exists():
            return cand
    return None


def _exe() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(sys.executable)


def launcher_path(title: str = APP_TITLE) -> Path | None:
    d = desktop_dir()
    return (d / f"{title}.url") if d else None


def _create_lnk_via_vbs(exe: Path, lnk: Path, workdir: Path) -> tuple[bool, str]:
    """用系统自带的 WScript.Shell（经临时 .vbs 调用）创建真正的 .lnk。

    注意：.vbs 必须以系统 ANSI 代码页(mbcs)写入，否则中文路径会被 cscript
    读成乱码；这是此前 .lnk 失败改用 .bat 的真正原因。
    """
    import subprocess
    import tempfile
    vbs = Path(tempfile.gettempdir()) / "mrm_create_shortcut.vbs"
    script = (
        'Set ws = CreateObject("WScript.Shell")\n'
        f'Set sc = ws.CreateShortcut("{lnk}")\n'
        f'sc.TargetPath = "{exe}"\n'
        f'sc.WorkingDirectory = "{workdir}"\n'
        f'sc.IconLocation = "{exe},0"\n'
        "sc.Save()\n"
    )
    try:
        # mbcs = Windows 系统 ANSI 代码页，cscript 默认按此读取 .vbs
        vbs.write_text(script, encoding="mbcs")
        r = subprocess.run(
            ["cscript", "//nologo", str(vbs)],
            capture_output=True, text=True, timeout=30,
            encoding="mbcs", errors="replace")
        ok = lnk.exists() and r.returncode == 0
        return ok, (f"lnk={lnk} rc={r.returncode} {r.stderr[:120] if not ok else ''}")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        try:
            vbs.unlink(missing_ok=True)
        except Exception:
            pass


def create_desktop_launcher(target: Path | str | None = None,
                            title: str = APP_TITLE,
                            args: str = "",
                            kind: str = "auto") -> tuple[bool, str]:
    """创建桌面快捷方式。优先真正的 .lnk（和右键发送到桌面一样），
    失败才退回 .bat。Returns (ok, detail).

    跨平台：非 Windows（Linux / ARM docker）直接返回不支持，不去碰 cscript。
    """
    if not sys.platform.startswith("win"):
        return False, f"unsupported platform: {sys.platform}"
    exe = Path(target) if target else _exe()
    if not exe.exists():
        return False, f"target not found: {exe}"
    d = desktop_dir()
    if not d:
        return False, "cannot locate Desktop folder"

    if kind in ("auto", "lnk"):
        lnk = d / f"{title}.lnk"
        ok, detail = _create_lnk_via_vbs(exe, lnk, exe.parent)
        if ok:
            return True, f"created {lnk}"
        if kind == "lnk":
            return False, detail
        # auto -> 继续尝试 .bat 兜底

    # 兜底: .bat 启动器
    p = d / f"{title}.bat"
    workdir = str(exe.parent)
    cmd = f'"{exe}"'
    if args:
        cmd += f" {args}"
    # chcp 65001 makes cmd read this UTF-8 file correctly (needed when the
    # install path contains non-ASCII characters such as Chinese).
    # newline=""：内容自带 \r\n，文本模式会二次转译成 \r\r\n（R152-F5 同款）。
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            f'cd /d "{workdir}"\r\n'
            f'start "" {cmd}\r\n'
            "exit /b 0\r\n")
    return True, f"created {p} (lnk fallback: {detail if kind == 'auto' else ''})"


def remove_desktop_launcher(title: str = APP_TITLE) -> tuple[bool, str]:
    d = desktop_dir()
    if not d:
        return False, "cannot locate Desktop folder"
    removed = []
    for ext in (".url", ".bat", ".lnk"):
        p = d / f"{title}{ext}"
        if p.exists():
            try:
                p.unlink()
                removed.append(p.name)
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
    return True, (", ".join(removed) or "not found")


def launcher_status(title: str = APP_TITLE) -> tuple[bool, str]:
    d = desktop_dir()
    if not d:
        return False, "unsupported platform"
    for ext in (".lnk", ".url", ".bat"):
        p = d / f"{title}{ext}"
        if p.exists():
            return True, str(p)
    return False, "not installed"
