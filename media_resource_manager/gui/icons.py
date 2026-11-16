# -*- coding: utf-8 -*-
"""SVG 图标（来自 iconify.design 的 lucide 集，MIT 许可）渲染为 QIcon。
用法:  icons.icon("play")  /  icons.icon("stop", color="#ffffff", size=16)
所有图标内嵌于此，运行时不联网。
"""
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_SVGS = {
    "play": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/></svg>',
    "stop": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" rx="2"/></svg>',
    "edit": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497zM15 5l4 4"/></svg>',
    "plus": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 12h14m-7-7v14"/></svg>',
    "download": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M12 15V3m9 12v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10l5 5l5-5"/></g></svg>',
    "image": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15l-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></g></svg>',
    "search": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="m21 21l-4.34-4.34"/><circle cx="11" cy="11" r="8"/></g></svg>',
    "checklist": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="m3 17l2 2 4-4m6 2h6M3 7l2 2 4-4m6 2h6"/></svg>',
    # ---- 以下为补充图标（菜单 / 审核 / 窗口控制 等） ----
    "pause": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><line x1="6" y1="4" x2="6" y2="20"/><line x1="18" y1="4" x2="18" y2="20"/></g></svg>',
    "minimize": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><line x1="5" y1="12" x2="19" y2="12"/></g></svg>',
    "x": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></g></svg>',
    "heart": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.29 1.51 4.04 3 5.5l7 7Z"/></g></svg>',
    "thumbs-down": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22a3.13 3.13 0 0 1-3-3.88Z"/></g></svg>',
    "skip-forward": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/></g></svg>',
    "refresh": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></g></svg>',
    "log-out": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></g></svg>',
    "settings": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></g></svg>',
    "file": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/></g></svg>',
    "save": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></g></svg>',
    "trash": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></g></svg>',
    "shield": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></g></svg>',
    "maximize": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></g></svg>',
    "list": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></g></svg>',
    "grid": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><rect width="18" height="18" x="3" y="3" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/><line x1="15" y1="3" x2="15" y2="21"/></g></svg>',
    "restore": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><rect width="12" height="12" x="9" y="9" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></g></svg>',
    "bulb": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M9 18h6"/><path d="M10 22h4"/><path d="M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14"/></g></svg>',
    # V0.0.9：首页（学习看板主界面化）导航图标，lucide "house"。
    "home": '<svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2"><path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.994a2 2 0 0 1 2.582 0l7 5.994A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></g></svg>',
}

_CACHE: dict = {}


def icon(name: str, color: str = "", size: int = 16) -> QIcon:
    """把内嵌 SVG 按指定颜色/尺寸渲染成 QIcon（2x 抗锯齿，HiDPI 清晰）。

    ⚠ P1-4：这里的 `color` **必须是真实色值**，不支持 `{key}` ——
    QIcon 在构造那一刻就把颜色烤进 QPixmap 了，之后刷 QSS / unpolish
    全都动不了它（深色主题下默认那个深色字直接看不见）。
    要「跟随皮肤」的图标请用 `set_themed_icon(控件, 名字, "{text}")`，
    它会登记 (控件, 名字, 尺寸, 模板) 并在切肤时重烤一遍。

    `color` 留空 = **按当前皮肤取 text 色**（以前这里是写死的深色字，
    任何没显式传色的调用点在深色主题下都是看不见的图标）。
    """
    if not color:
        color = _resolve_key("{text}")
    return _render(name, color, size)


def _render(name: str, color: str, size: int) -> QIcon:
    key = (name, color, size)
    if key in _CACHE:
        return _CACHE[key]
    svg = _SVGS[name].replace("currentColor", color)
    r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    img = QImage(size * 2, size * 2, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    r.render(p)
    p.end()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(2.0)
    ic = QIcon(pm)
    _CACHE[key] = ic
    return ic


# ══════════════════════════════════════════════════════════════════
# 跟随皮肤的主题化图标（P1-4）
#
# 为什么不能只把颜色换成 theme.c()：QIcon 在 setIcon() 那一刻就把颜色
# 烤进 QPixmap 了。之后换皮肤刷 QSS / unpolish 全都动不了它 ——
# 用户看到的还是旧颜色。唯一可靠的办法是**重新渲染 QIcon 再 setIcon 回去**。
#
# 为什么用「弱引用登记表」而不是 QIcon 子类代理：给 C++ 类套Python 子类
# 再回头重init 它，是已知的危险动作（PySide6 里 QThread 析构会直接 abort
# 整进程，同一类风险）。这里坚持用**普通 QIcon**，只额外记一份
# (控件, 名字, 尺寸, 模板)，切肤时重建 —— 没有生命周期上的花招。
# ══════════════════════════════════════════════════════════════════
_THEMED: list = []


def _resolve_key(key: str) -> str:
    """`{text_dim}` -> 当前皮肤的 `#aab1bd`。取不到就退回浅色字。

    ⚠ 兜底值必须是**浅色**（`#e7eaf0`）：这个函数只在「theme 取不到色」时
    兜底，而取不到色最常见的原因正是深色主题 —— 原来兜底给深色字，
    等于「在最需要它的场景下恰好失效」。
    """
    try:
        from . import theme
        out = theme.css(theme._CURRENT_DARK, "x{%s}" % key)
        return out[2:-1]
    except Exception:
        return "#e7eaf0"


def set_themed_icon(widget, name: str, key: str = "{text}", size: int = 16):
    """给控件装一个**会跟随皮肤**的图标（`key` 形如 `"{text}"`/`"{danger}"`）。

    调用点从此不用关心换肤 —— 切深浅/换皮肤时 `restyle_themed_icons()`
    会把所有登记过的图标重烤一遍。
    """
    import weakref
    try:
        from . import theme
        if theme.native_mode():
            # R60 §3.4：真原生模式不设自绘图标（退回纯文字按钮），
            # 但**仍然登记** —— 用户切回非原生皮肤时要把图标烤回来，
            # 不登记就等于永久丢失（与 theme._restyle_registered 同一条理）。
            widget.setIcon(QIcon())
            _THEMED.append((weakref.ref(widget), name, key, size))
            return widget
    except Exception:
        pass
    try:
        widget.setIcon(_render(name, _resolve_key(key), size))
        _THEMED.append((weakref.ref(widget), name, key, size))
    except Exception:
        pass
    return widget


def clear_themed_icons():
    """R60：真原生模式 —— 清掉所有主题化图标，按钮退回纯文字。

    ⚠ **保留登记**（不清 _THEMED）：切回非原生皮肤时由
      `restyle_themed_icons()` 重新烤一遍。清掉登记就等于永久丢图标。
    """
    for entry in list(_THEMED):
        ref = entry[0]
        w = ref()
        if w is None:
            continue
        try:
            w.setIcon(QIcon())
        except Exception:
            pass


def restyle_themed_icons():
    """切皮肤/深浅后由 `theme.apply_theme()` 调用，重烤所有主题化图标。"""
    alive = []
    for ref, name, key, size in _THEMED:
        w = ref()
        if w is None:
            continue                    # 控件已销毁，丢掉登记
        try:
            w.setIcon(_render(name, _resolve_key(key), size))
            alive.append((ref, name, key, size))
        except Exception:
            pass
    _THEMED[:] = alive


def _reset_themed_registry():
    """只给测试用：清空登记表。"""
    _THEMED.clear()
