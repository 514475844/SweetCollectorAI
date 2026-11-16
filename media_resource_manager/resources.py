"""Resolve bundled resource paths (works from source AND from the frozen exe).

Drop a custom icon at media_resource_manager/resources/icon.ico and it is used
automatically - app window, tray icon, and the built .exe all pick it up.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _base() -> Path:
    # PyInstaller one-folder: bundled files sit under sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parents[1]


def icon_path() -> Path:
    p = _base() / "media_resource_manager" / "resources" / "icon.ico"
    if not p.exists():
        p = Path(__file__).resolve().parent / "resources" / "icon.ico"
    return p


def resources_dir() -> Path:
    """打包内的资源目录（frozen 时在 sys._MEIPASS，源码运行时在包内）。"""
    base = _base()
    p = base / "media_resource_manager" / "resources"
    if p.exists():
        return p
    return Path(__file__).resolve().parent / "resources"


def logo_path() -> Path | None:
    """右下角水印用的 logo（用户提供的 PNG）。

    找 resources/logo.png；没有就返回 None，由调用方退回 icon.ico。
    与 icon 不同：logo 必须是**大尺寸 PNG**，因为 .ico 里的帧最小只有 16x16，
    放大成水印后完全糊掉（这是 r40 一开始「加了水印却看不见」的根因之一）。
    """
    for d in (resources_dir(), _project_resources_safe()):
        if not d:
            continue
        for name in ("logo.png", "sweet_logo.png"):
            p = Path(d) / name
            try:
                if p.exists():
                    return p
            except Exception:
                continue
    return None


def _project_resources_safe():
    try:
        return _project_resources()
    except Exception:
        return None


def has_icon() -> bool:
    try:
        return icon_path().exists()
    except Exception:
        return False


def _project_resources() -> Path:
    """Resources dir of the SOURCE tree (for the next rebuild).

    V0.0.36 交付结构：<交付目录>/SweetWeb.exe（唯一 exe），资源在
    <交付目录>/_internal/media_resource_manager/resources 下。这里只给
    「源码树里重新构建」用，冻结态走 _MEIPASS 解析，不依赖本函数。
    """
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        return exe_dir.parent.parent / "media_resource_manager" / "resources"
    return Path(__file__).resolve().parent / "resources"


def apply_custom_icon(src: str | Path) -> list[Path]:
    """Convert any image the user picks into our .ico and install it.

    Writes BOTH locations:
      * runtime copy (frozen: _internal/...) -> window/tray icon after restart
      * source copy -> picked up by the next `pyinstaller` build (exe icon)
    Returns the list of written paths.
    """
    from PIL import Image
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(src)
    im = Image.open(src).convert("RGBA")
    out = []
    targets = [icon_path().parent]
    try:
        targets.append(_project_resources())
    except Exception:
        pass
    for d in targets:
        d.mkdir(parents=True, exist_ok=True)
        dst = d / "icon.ico"
        im.save(dst, format="ICO",
                sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        out.append(dst)
    return out
