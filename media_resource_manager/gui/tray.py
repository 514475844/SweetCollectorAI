"""System tray icon (PySide6): show window, start/stop all, pause, quit.

R137 根因（真机实测「右键没可用菜单」）：
    菜单原来是 `build_tray()` 里的**局部变量**且没有 parent。Qt 明确不接管
    托盘右键菜单的所有权（`QSystemTrayIcon.setContextMenu` 的文档写着
    "The menu is not owned by the tray icon"），`build_tray` 一返回，Python 侧
    引用计数归零 ⇒ C++ QMenu 被销毁 ⇒ 右键要么不弹、要么弹出来点了没反应。
修法：QMenu **挂到 tray 下**（Qt 父子保活）+ 在 tray 上留一份 Python 引用，
      动作对象同样留引用，杜绝被 GC。

R138：
    · 单击（Trigger）/ 双击（DoubleClick）都唤回主窗口（原来只认双击）；
    · 可选「隐藏到托盘」菜单项；
    · `update_tray_tooltip()` 刷新托盘提示（任务数 / 后台常驻）。
"""
from __future__ import annotations

from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction

from ..resources import icon_path

_APP_TITLE = "Sweet采集AI版"

# 单击 / 双击 / 中键都算「唤回主窗口」——用户怎么点都能把窗口叫回来。
_SHOW_REASONS = (
    QSystemTrayIcon.ActivationReason.Trigger,
    QSystemTrayIcon.ActivationReason.DoubleClick,
    QSystemTrayIcon.ActivationReason.MiddleClick,
)


def build_tray(app, on_show, on_pause, on_quit,
               on_start_all=None, on_stop_all=None,
               on_hide=None) -> QSystemTrayIcon:
    """Build the tray icon.

    ``on_start_all`` / ``on_stop_all`` are optional; when supplied the tray
    menu gains 「全部开始」/「全部停止」, so the most common actions no longer
    require opening the window first.

    ``on_hide`` (R138) is optional; when supplied the menu gains
    「隐藏到托盘」, i.e. 手动把窗口收进后台而不退出进程。
    """
    tray = QSystemTrayIcon(app)
    p = icon_path()
    if p.exists():
        tray.setIcon(QIcon(str(p)))
    tray.setToolTip(_APP_TITLE)
    # ★ R137：菜单必须有**常驻引用**才活得过 build_tray 的返回。
    # 注意 QMenu 的 parent 只能是 QWidget，而 QSystemTrayIcon 是 QObject 不是
    # QWidget —— 挂不上去（`QMenu(tray)` 会 TypeError），所以保活靠下面这两行：
    # Qt 侧 tray 不接管菜单所有权，Python 侧我们自己在 tray 上留一份引用。
    menu = QMenu()
    acts = {}
    a_show = QAction("显示主窗口")
    a_show.triggered.connect(on_show)
    menu.addAction(a_show)
    acts["show"] = a_show
    if on_hide is not None:
        a_hide = QAction("隐藏到托盘")
        a_hide.setToolTip("把窗口收进后台继续运行（不退出程序）")
        a_hide.triggered.connect(on_hide)
        menu.addAction(a_hide)
        acts["hide"] = a_hide
    menu.addSeparator()
    if on_start_all is not None:
        a_start = QAction("全部开始")
        a_start.setToolTip("把列表里所有未在运行的任务一次性入队")
        a_start.triggered.connect(on_start_all)
        menu.addAction(a_start)
        acts["start_all"] = a_start
    if on_stop_all is not None:
        a_stop = QAction("全部停止")
        a_stop.setToolTip("停止当前所有正在运行的任务")
        a_stop.triggered.connect(on_stop_all)
        menu.addAction(a_stop)
        acts["stop_all"] = a_stop
    if on_start_all is not None or on_stop_all is not None:
        menu.addSeparator()
    a_pause = QAction("暂停 / 继续队列")
    a_pause.triggered.connect(on_pause)
    menu.addAction(a_pause)
    acts["pause"] = a_pause
    menu.addSeparator()
    a_quit = QAction("退出")
    a_quit.setToolTip("真正退出程序（会先安全收尾）")
    a_quit.triggered.connect(on_quit)
    menu.addAction(a_quit)
    acts["quit"] = a_quit
    # ★ 保活：Qt 不接管菜单所有权，Python 侧留一份引用（双保险，防 GC）。
    tray._mrm_menu = menu
    tray._mrm_actions = acts
    tray.setContextMenu(menu)

    def _on_activated(reason):
        # Context（右键）交给 Qt 自己弹菜单，这里不要碰，否则会把它吞掉。
        if reason in _SHOW_REASONS:
            try:
                on_show()
            except Exception:
                pass

    tray.activated.connect(_on_activated)
    return tray


def update_tray_tooltip(tray, running: int = 0, resident: bool = False) -> str:
    """R138 P1-C：刷新托盘提示文本，返回写进去的那串。

    图标角标要在托盘上自绘数字（多平台 DPI/深色模式都不好伺候），成本高、
    收益小，这里只做**文本反馈**：任务数 + 是否后台常驻。
    """
    tip = _APP_TITLE
    try:
        n = int(running or 0)
    except Exception:
        n = 0
    if n > 0:
        tip += f"\n{n} 个任务运行中"
    elif resident:
        tip += "\n（后台常驻中）"
    try:
        if tray is not None:
            tray.setToolTip(tip)
    except Exception:
        pass
    return tip
