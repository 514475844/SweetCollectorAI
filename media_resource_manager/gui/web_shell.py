"""V0.0.21 —— Web 壳（SweetWeb.exe）的服务编排与界面。

形态（用户拍板，对标 QQNT 的壳窗口体验，但不引入 Electron）：
  起数据库 + 任务队列 + 订阅监控 + Web 服务（同一套引擎口径）→ 优先用
  内置 QtWebEngine 渲染内核开**原生窗口**（标准最小化 /
  最大化 / 关闭三键）；内核不可用时回退本机 Chromium 系 ``--app`` 独立
  窗口，再找不到才用普通浏览器打开 + 控制小窗。

V0.0.27：壳窗口补上托盘（用户实测反馈「启动后最小化到托盘功能呢？」）——
  · 托盘常驻：显示主界面 / 隐藏到托盘 / 暂停继续队列 / 退出；
  · 设置里勾了「启动后最小化到托盘」⇒ 启动不弹窗，只留托盘图标；
  · 窗口用**系统标题栏**（V0.0.39 按用户要求从自绘窗口改回），关闭键＝
    彻底退出（V0.0.36 起的口径没变）；「收进托盘」走托盘菜单
    （右键托盘图标 →「隐藏到托盘」）或网页右下角那排里的退出按钮；
  · 网页右下角状态栏的「完全退出」＝两步确认后退出整个软件；
  · 再双击一次程序 = 把已在运行的窗口叫出来（单实例唤回）。

内置窗口不只是「去掉地址栏」：浏览器进程级的功能键、右键菜单、外链导航、
新窗口、拖放、下载、页面缩放、开发者工具、拼写检查提示全部按桌面软件
口径接管；常用键改为软件动作（Ctrl+A 全选列表、Ctrl+F 定位搜索、
Ctrl+S 保存、F5 刷新数据）。决策表只有一份，在 webapp/src/nativeShell.js，
内核层拦下浏览器动作后转发到同一张表，避免两端各写一套。

纪律：引擎/调度绝不另写一套；本模块内无硬退出调用（r71 护栏），进程级
收尾在入口 mrm_web_app.py。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import webbrowser


# 冒烟开关：置 1 时不弹浏览器窗口（自动化验证用，普通用户不会碰到）
_NO_BROWSER = bool(os.environ.get("MRM_SHELL_NO_BROWSER"))

# 常见 Chromium 系浏览器可执行文件（按优先级）。注册表查找失败时的兜底。
_BROWSER_CANDIDATES = (
    r"%LOCALAPPDATA%\360ChromeX\Chrome\Application\360ChromeX.exe",
    r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"%LOCALAPPDATA%\360Chrome\Chrome\Application\360chrome.exe",
    r"%LOCALAPPDATA%\Tencent\QQBrowser\QQBrowser.exe",
)


def find_browser() -> str:
    """返回可用的 Chromium 系浏览器 exe 全路径；找不到返回空串。"""
    # 1) 注册表 InstallLocation（360/Chrome/Edge 都登记）
    try:
        import winreg
        for key in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
            try:
                k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                try:
                    k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key)
                except OSError:
                    continue
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i); i += 1
                except OSError:
                    break
                try:
                    sk = winreg.OpenKey(k, sub)
                    name = str(winreg.QueryValueEx(sk, "DisplayName")[0]).lower()
                    loc = str(winreg.QueryValueEx(sk, "InstallLocation")[0])
                except OSError:
                    continue
                if not loc:
                    continue
                for pat in ("360chromex.exe", "chrome.exe", "msedge.exe",
                            "360chrome.exe", "qqbrowser.exe"):
                    cand = os.path.join(loc, pat)
                    if os.path.isfile(cand):
                        return cand
    except Exception:                              # noqa: BLE001
        pass
    # 2) 常见路径兜底
    for c in _BROWSER_CANDIDATES:
        p = os.path.expandvars(c)
        if os.path.isfile(p):
            return p
    return ""


def _open_app_window(url: str) -> subprocess.Popen | None:
    """以应用模式开独立窗口（专用 profile，不掺和日常浏览器）。

    返回 Popen（用于轮询窗口是否被关闭）；失败返回 None，调用方回退
    普通打开方式。
    """
    exe = find_browser()
    if not exe:
        return None
    from ..config import user_data_dir
    profile = user_data_dir() / "shell_profile"
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe,
            f"--app={url}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--window-size=1360,860"]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except Exception:                              # noqa: BLE001
        return None


def _start_engine():
    """拉起数据库 + 任务队列 + 订阅监控 + Web 服务。

    返回 (ok, 说明, ctx)；ctx 里有 db / queue / server，供退出时收尾。
    任何一步失败都返回 ok=False 并给中文原因，不留半启动的资源
    （已启动的部分会在失败分支里就地停掉）。
    """
    ctx = {"db": None, "queue": None, "server": None}
    try:
        from ..config import (load_config, set_active_config,
                              set_db_master_password)
        from ..db import Database
        from ..logutil import setup_logging, get_logger

        cfg = load_config()
        cfg.ensure_dirs()
        setup_logging(cfg.log_dir)
        log = get_logger("mrm.webshell")
        set_active_config(cfg)
        set_db_master_password(os.environ.get("MRM_DB_PASSWORD", "") or "")

        from ..scheduler.queue import TaskQueue, enqueue_auto_tasks
        from ..webui import WebServer

        db = Database(cfg.db_path)
        ctx["db"] = db
        q = TaskQueue(cfg, max_concurrent=int(
            getattr(cfg, "parallel_tasks", 3) or 3))
        q.start()
        ctx["queue"] = q
        try:
            from ..subscribe import start_global_watcher
            start_global_watcher(cfg, cfg.db_path, q)
        except Exception as _ex:                       # noqa: BLE001
            log.warning("订阅监控启动失败（不影响采集）: %s", _ex)
        # 启动即巡检一轮：勾了自动运行的任务不必等周期
        try:
            enqueue_auto_tasks(q, cfg)
        except Exception:                              # noqa: BLE001
            pass

        server = WebServer(cfg, db, queue=q)
        ctx["server"] = server
        ctx["cfg"] = cfg                     # 供托盘/启动行为读配置
        ok, msg = server.start()
        if not ok:
            _shutdown(ctx)
            return False, msg, ctx
        log.info("Web 壳已启动：%s", msg)
        return True, msg, ctx
    except Exception as e:                             # noqa: BLE001
        _shutdown(ctx)
        return False, f"服务启动失败：{type(e).__name__}: {e}", ctx


def _shutdown(ctx) -> None:
    """有界收尾：停浏览器窗口 → 停队列 → 停服务 → 关库，每步失败只吞不抛。"""
    bw = ctx.get("browser")
    if bw is not None:
        try:
            if bw.poll() is None:
                bw.terminate()
        except Exception:                              # noqa: BLE001
            pass
    q = ctx.get("queue")
    if q is not None:
        try:
            q.stop()
        except Exception:                              # noqa: BLE001
            pass
    srv = ctx.get("server")
    if srv is not None:
        try:
            srv.stop()
        except Exception:                              # noqa: BLE001
            pass
    db = ctx.get("db")
    if db is not None:
        try:
            db.close()
        except Exception:                              # noqa: BLE001
            pass


# ── V0.0.21 · 内置窗口桌面化：浏览器痕迹按类收口 ───────────────────
# 下列按键由浏览器进程处理，网页里的 keydown 收不到或拦不住（F5 刷新、
# Ctrl+P 打印、F12 开发者工具、Ctrl+L 地址栏…）。内核层统一吞掉；其中
# 「软件里确实有对应功能」的键吞掉后转发给前端同一张决策表，变成软件动作。
# 键名口径与 webapp/src/nativeShell.js 的 seqOf 完全一致。
_SHELL_FORWARD_SEQS = frozenset({
    "F5", "Ctrl+R", "Ctrl+Shift+R",   # 刷新当前数据，不重载界面
    "Ctrl+F", "F3",                   # 定位全局搜索框
    "Ctrl+S",                         # 保存当前设置或表单
})
_SHELL_BLOCK_SEQS = frozenset({
    # 打印与另存为
    "Ctrl+P", "Ctrl+Shift+P", "Ctrl+Shift+S",
    # 查找、地址栏、历史、下载、收藏
    "Ctrl+G", "Ctrl+K", "Ctrl+L", "Ctrl+E", "F6",
    "Ctrl+H", "Ctrl+J", "Ctrl+D", "Alt+Home", "Alt+←", "Alt+→",
    # 查看源代码与开发者工具
    "Ctrl+U", "F12", "Ctrl+Shift+I", "Ctrl+Shift+J", "Ctrl+Shift+C",
    "Ctrl+Shift+K", "Ctrl+Shift+Delete", "Ctrl+Shift+Del",
    # 标签与窗口
    "Ctrl+T", "Ctrl+N", "Ctrl+W", "Ctrl+Q", "Ctrl+Shift+T",
    "Ctrl+Shift+N", "Ctrl+Shift+W", "Ctrl+Tab", "Ctrl+Shift+Tab",
    *[f"Ctrl+{i}" for i in range(1, 10)],
    # 页面形态、光标浏览与全屏
    "F7", "F11", "Ctrl+O",
    # 缩放（内核级，界面字号只跟皮肤走）
    "Ctrl+=", "Ctrl++", "Ctrl+-", "Ctrl+_", "Ctrl+0",
    "Ctrl+Shift+=", "Ctrl+Shift++", "Ctrl+Shift+-", "Ctrl+Shift+_",
    "Ctrl+Shift+0",
})
# 需要清掉默认 shortcut 的内核动作：这些是浏览器动作，不是软件功能。
# 编辑类（Cut/Copy/Paste/Undo/Redo）保留；SelectAll 单独清空，让 Ctrl+A
# 完全交给网页决策表（输入框内全选文字 / 列表页全选当前项）。
_SHELL_CLEAR_ACTION_NAMES = (
    "Back", "Forward", "Reload", "ReloadAndBypassCache", "Stop",
    "SelectAll", "SavePage", "InspectElement", "OpenLinkInNewTab",
    "OpenLinkInNewWindow", "OpenLinkInNewBackgroundTab",
    "OpenLinkInThisWindow", "DownloadLinkToDisk", "DownloadImageToDisk",
    "DownloadMediaToDisk", "CopyLinkToClipboard", "CopyImageToClipboard",
    "CopyImageUrlToClipboard", "CopyMediaUrlToClipboard",
    "ExitFullScreen", "RequestClose", "ToggleMediaControls",
)

# ── 启动画面配色（V0.0.45 收口）────────────────────────────────────────
# 这几个色是 QPainter 一个像素一个像素画上去的，不经过 QSS 也拿不到主题
# 变量（此刻主窗口还没出来、用户也还没选皮肤）。按 P1-4 护栏的约定，
# 它们必须收进模块级 _C_* 兜底常量，不许散落在绘制代码里 —— 否则
# 「色值唯一真值源」这条规矩会被一行行 QColor("#xxxxxx") 慢慢蛀空。
_C_SPLASH_CARD = "#ffffff"      # 启动卡片底
_C_SPLASH_G1 = "#8b7bff"        # 顶部品牌渐变（紫）
_C_SPLASH_G2 = "#6aa8ff"        # 顶部品牌渐变（蓝）
_C_SPLASH_G3 = "#5ad2f0"        # 顶部品牌渐变（青）
_C_SPLASH_TITLE = "#3a3f52"     # 字标缺失时的纯文字标题
_C_SPLASH_TIP = "#8a8fa3"       # 「正在启动…」


def _qt_key_seq(event) -> str:
    """Qt 键盘事件 → 与前端 seqOf 同口径的序列串。"""
    from PySide6.QtCore import Qt
    k = event.key()
    mods = event.modifiers()
    out = []
    if mods & Qt.KeyboardModifier.ControlModifier:
        out.append("Ctrl")
    if mods & Qt.KeyboardModifier.AltModifier:
        out.append("Alt")
    if mods & Qt.KeyboardModifier.ShiftModifier:
        out.append("Shift")
    if mods & Qt.KeyboardModifier.MetaModifier:
        out.append("Meta")
    special = {
        Qt.Key.Key_Left: "←", Qt.Key.Key_Right: "→",
        Qt.Key.Key_Up: "↑", Qt.Key.Key_Down: "↓",
        Qt.Key.Key_Escape: "Esc", Qt.Key.Key_Space: "Space",
        Qt.Key.Key_Delete: "Delete", Qt.Key.Key_Backspace: "Backspace",
        Qt.Key.Key_Tab: "Tab", Qt.Key.Key_Home: "Home",
        Qt.Key.Key_End: "End", Qt.Key.Key_Return: "Enter",
        Qt.Key.Key_Enter: "Enter", Qt.Key.Key_PageUp: "PageUp",
        Qt.Key.Key_PageDown: "PageDown",
        Qt.Key.Key_Plus: "+", Qt.Key.Key_Equal: "=",
        Qt.Key.Key_Minus: "-", Qt.Key.Key_Underscore: "_",
    }
    if k in special:
        key = special[k]
    elif Qt.Key.Key_F1 <= k <= Qt.Key.Key_F12:
        key = f"F{k - Qt.Key.Key_F1 + 1}"
    elif Qt.Key.Key_A <= k <= Qt.Key.Key_Z:
        key = chr(k)
    elif Qt.Key.Key_0 <= k <= Qt.Key.Key_9:
        key = chr(k)
    else:
        return ""
    out.append(key)
    return "+".join(out)


def _seq_detail(seq: str) -> dict:
    """序列串 → 前端 shellState 需要的字段。"""
    parts = seq.split("+")
    # "Ctrl++" 拆开后末段为空；键名本身是 "+"，单独还原。
    key = parts[-1] if parts[-1] else "+"
    d = {"key": key, "ctrlKey": "Ctrl" in parts,
         "altKey": "Alt" in parts, "shiftKey": "Shift" in parts,
         "metaKey": "Meta" in parts}
    if d["key"] == "←":
        d["key"] = "ArrowLeft"
    elif d["key"] == "→":
        d["key"] = "ArrowRight"
    elif len(d["key"]) == 1 and d["key"].isalpha():
        d["key"] = d["key"].upper()
    return d



def _app_icon():
    """软件自己的窗口/托盘图标（找不到返回 None，交给 Qt 用默认值）。"""
    try:
        from PySide6.QtGui import QIcon
        from ..resources import icon_path
        p = icon_path()
        if p.exists():
            return QIcon(str(p))
    except Exception:                                  # noqa: BLE001
        pass
    return None



def _fade(widget, start: float, end: float, ms: int, on_done=None):
    """窗口透明度过渡（启动/收场的「动起来」，不位移、不抖动）。"""
    try:
        from PySide6.QtCore import QEasingCurve, QPropertyAnimation
        anim = QPropertyAnimation(widget, b"windowOpacity", widget)
        anim.setDuration(int(ms))
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if on_done is not None:
            anim.finished.connect(on_done)
        widget._mrm_fade = anim          # 保引用，否则动画对象被回收 = 不播
        anim.start()
        return anim
    except Exception:                                  # noqa: BLE001
        try:
            widget.setWindowOpacity(float(end))
        except Exception:                              # noqa: BLE001
            pass
        if on_done is not None:
            on_done()
        return None


def _show_splash(app):
    """启动画面：白卡片 + 用户字标 + 「正在启动…」。

    起服务要读库、起队列、拉订阅，中间那几秒原来是一块白屏，用户会以为
    没点着又双击一次。设置里「显示启动画面」关掉就完全不闪。
    """
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
        from PySide6.QtWidgets import QSplashScreen
        pm = QPixmap(460, 268)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 卡片本体
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_C_SPLASH_CARD))
        p.drawRoundedRect(0, 0, 460, 268, 16, 16)
        # 顶部一条品牌渐变细带（紫 → 蓝，跟网页里的强调色一致）
        grad = QLinearGradient(0, 0, 460, 0)
        grad.setColorAt(0.0, QColor(_C_SPLASH_G1))
        grad.setColorAt(0.55, QColor(_C_SPLASH_G2))
        grad.setColorAt(1.0, QColor(_C_SPLASH_G3))
        p.setBrush(grad)
        p.drawRoundedRect(40, 34, 380, 4, 2, 2)
        # 字标（用户给的 PNG 原图，没有就退回文字）
        drew = False
        try:
            from ..resources import resources_dir
            word = resources_dir() / "logo_wordmark.png"
            if word.is_file():
                raw = QPixmap(str(word))
                if not raw.isNull():
                    logo = raw.scaledToWidth(
                        250, Qt.TransformationMode.SmoothTransformation)
                    # 字标自带星点装饰，位置要往上留，别和下面那行字挤在一起
                    p.drawPixmap((460 - logo.width()) // 2, 78, logo)
                    drew = True
        except Exception:                          # noqa: BLE001
            drew = False
        if not drew:
            f = QFont()
            f.setPointSize(20)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(_C_SPLASH_TITLE))
            p.drawText(pm.rect().adjusted(0, -30, 0, 0),
                       int(Qt.AlignmentFlag.AlignCenter), "Sweet 采集AI版")
        # 正在启动…
        f2 = QFont()
        f2.setPointSize(10)
        p.setFont(f2)
        p.setPen(QColor(_C_SPLASH_TIP))
        p.drawText(0, 196, 460, 30, int(Qt.AlignmentFlag.AlignCenter),
                   "正在启动…")
        p.end()
        splash = QSplashScreen(pm)
        splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        splash.setWindowOpacity(0.0)
        import time as _time
        splash._mrm_shown_at = _time.monotonic()      # V0.0.46：满 3 秒才收
        splash.show()
        app.processEvents()
        _fade(splash, 0.0, 1.0, 180)
        return splash
    except Exception:                                  # noqa: BLE001
        return None


def _finish_splash(splash, win):
    """收掉启动画面，并让主窗口淡入（这就是「启动窗口动画」）。

    V0.0.46：用户要求**启动画面至少满 3 秒**才拉主窗口 —— 原来页面首帧
    一出来就收，本机通常 1 秒多点，动画一闪而过等于没有。首帧触发和
    4 秒超时兜底都汇到这一个入口，所以只在这里补等待：还差几秒就延迟
    几秒，已经够 3 秒就直接收。
    """
    import time as _time
    try:
        shown = _time.monotonic() - getattr(splash, "_mrm_shown_at", 0.0) \
            if splash is not None else 999.0
    except Exception:                                  # noqa: BLE001
        shown = 999.0
    wait_ms = max(0, int((3.0 - shown) * 1000))
    if wait_ms:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(wait_ms, lambda: _finish_splash(splash, win))
        return

    try:
        if win is not None and win.isVisible():
            win.setWindowOpacity(0.0)
    except Exception:                                  # noqa: BLE001
        pass

    def _after():
        try:
            if splash is not None:
                splash.close()
        except Exception:                              # noqa: BLE001
            pass
        if win is not None:
            try:
                if win.isVisible():
                    _fade(win, 0.0, 1.0, 220)
                else:
                    win.setWindowOpacity(1.0)
            except Exception:                          # noqa: BLE001
                pass

    if splash is None:
        _after()
        return
    try:
        _fade(splash, 1.0, 0.0, 160, on_done=_after)
    except Exception:                                  # noqa: BLE001
        _after()


def _open_embedded_window(url: str, on_close, start_hidden: bool = False):
    """内置渲染内核开原生窗口，并收口全部浏览器痕迹。

    返回 (QMainWindow | None, 说明)。不可用（缺模块/初始化失败）返回
    None 并给中文原因，调用方回退外部浏览器应用窗口。
    """
    try:
        from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl
        from PySide6.QtGui import (QAction, QDesktopServices, QIcon,
                                   QKeySequence)
        from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                            QWebEngineScript, QWebEngineSettings)
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import (QApplication, QFileDialog, QHBoxLayout,
                                      QLabel, QMainWindow, QMenu, QMessageBox,
                                      QWidget)
    except Exception as e:                             # noqa: BLE001
        return None, f"内置渲染内核不可用：{e}"

    base = QUrl(url)
    base_host, base_port = base.host(), base.port()

    def _is_local(u) -> bool:
        if u.scheme() in ("data", "blob", "about"):
            return True
        return (u.scheme() in ("http", "https") and
                u.host() in ("127.0.0.1", "localhost", base_host) and
                (u.port() == base_port or u.port() == -1))

    class _ShellPage(QWebEnginePage):
        """只留在软件自己的界面里；外链交给系统默认浏览器。"""

        def acceptNavigationRequest(self, u, nav_type, is_main_frame):
            if _is_local(u):
                return super().acceptNavigationRequest(u, nav_type, is_main_frame)
            if u.scheme() in ("http", "https"):
                try:
                    QDesktopServices.openUrl(u)
                except Exception:                      # noqa: BLE001
                    pass
            return False          # 拖入文件 / 外链 / 外部跳转一律不导航

        # 不重写 createWindow：重写后 newWindowRequested 信号不再发出，
        # 外链反而会变成「点了没反应」。统一在信号里交给系统浏览器，
        # 且不调用 openIn() ⇒ 内核不会创建第二个窗口。

        def javaScriptAlert(self, origin, msg):
            try:
                QMessageBox.information(None, "提示", str(msg))
            except Exception:                          # noqa: BLE001
                pass


    class _Win(QMainWindow):

        def closeEvent(self, event):
            # 真正退出（托盘「退出」/ 网页右下角「退出软件」）：正常关页面。
            if getattr(self, "_mrm_quitting", False):
                super().closeEvent(event)
                return
            # V0.0.45 用户定稿（推翻 V0.0.36 的「✕ = 彻底关闭」）：
            # 窗口关闭按钮 / Alt+F4 = **收进托盘**，采集继续在后台跑；
            # 彻底退出只留两个入口 —— 网页右下角那颗红「退出软件」、托盘菜单「退出」。
            # 原话：「还有右上角的关闭，现在变成彻底关闭了。没有最小化的托盘。」
            hide = getattr(self, "_mrm_hide_to_tray", None)
            if hide is not None:
                try:
                    event.ignore()
                    hide()
                    on_hide = getattr(self, "_mrm_on_hide", None)
                    if on_hide is not None:
                        on_hide()          # 第一次收进托盘弹一条气泡，免得以为关掉了
                    return
                except Exception:                      # noqa: BLE001
                    pass
            # 托盘不可用（极少数精简系统 / 远程会话）：藏起来就再也叫不回来，
            # 这时退回「关闭 = 退出」，至少用户能重开。
            on_close = getattr(self, "_mrm_on_close", None)
            if on_close is not None:
                event.accept()
                try:
                    on_close()
                except Exception:                      # noqa: BLE001
                    super().closeEvent(event)
                return
            super().closeEvent(event)

    class _ShellInputFilter(QObject):
        """窗口内事件过滤：装在 view、焦点代理与全部子控件上。

        ⚠ 不装在 QApplication 上 —— 全局过滤器会吞掉所有窗口的所有按键
        （r202 护栏钉过的历史故障）。这里只作用于本窗口的子控件，且
        弹窗（保存对话框等）激活时不生效，输入照常可用。
        """

        def __init__(self, win, page):
            super().__init__(win)
            self.win = win
            self.page = page

        def eventFilter(self, obj, event):
            try:
                if QApplication.activeWindow() is not self.win:
                    return False
                t = event.type()
                if t == QEvent.Type.Wheel and (
                        event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                    return True                       # Ctrl+滚轮 / 捏合缩放
                if t == QEvent.Type.MouseButtonPress and event.button() in (
                        Qt.MouseButton.MiddleButton, Qt.MouseButton.XButton1,
                        Qt.MouseButton.XButton2):
                    return True                       # 中键自动滚动、侧键后退
                if t in (QEvent.Type.KeyPress, QEvent.Type.ShortcutOverride):
                    seq = _qt_key_seq(event)
                    if not seq:
                        return False
                    # 浏览器功能键一律由内核吞掉，再转发给网页的同一张
                    # 决策表：录键态与用户自定义键位都还能拿到这个键，
                    # 而浏览器自身的打印、刷新、查找等行为不会发生。
                    if seq in _SHELL_FORWARD_SEQS or seq in _SHELL_BLOCK_SEQS:
                        if t == QEvent.Type.KeyPress:
                            try:
                                js = ("window.dispatchEvent(new CustomEvent("
                                      "'mrm-shell-key',{detail:%s}))" %
                                      json.dumps(_seq_detail(seq),
                                                 ensure_ascii=False))
                                self.page.runJavaScript(js)
                            except Exception:          # noqa: BLE001
                                pass
                        return True
            except Exception:                          # noqa: BLE001
                pass
            return False

    try:
        win = _Win()
        win.setWindowTitle("Sweet采集AI版")
        # V0.0.27：窗口左上角（标题栏 / 任务栏）用软件自己的图标。
        # 原来没设 ⇒ Qt 的默认图标（那个灰底方块），用户原话「怎么成了
        # 那么难看的一个图标」。
        _ic = _app_icon()
        if _ic is not None:
            win.setWindowIcon(_ic)
        win._mrm_on_close = on_close            # closeEvent 据此走彻底退出

        # ⚠ 不能用 QWebEngineProfile.defaultProfile()：实测它
        # isOffTheRecord() == True（内存 profile），localStorage 永远不落盘
        # —— 表现就是「新手引导每次打开都弹一遍、皮肤选了下次又回去」。
        # 自建一个具名 profile 才是持久化的（实测 offTheRecord == False，
        # 关窗后 data/webengine/Local Storage/leveldb 正常生成）。
        profile = QWebEngineProfile("mrm-shell")
        win._profile = profile          # 保引用：profile 被回收会让页面崩
        # V0.0.24：本地存储落到 data 下（跟浏览器借壳模式的 shell_profile
        # 同一处），这样随整机备份一起带走。
        try:
            from ..config import user_data_dir
            store = user_data_dir() / "webengine"
            (store / "cache").mkdir(parents=True, exist_ok=True)
            profile.setPersistentStoragePath(str(store))
            profile.setCachePath(str(store / "cache"))
        except Exception:                              # noqa: BLE001
            pass
        try:
            profile.setSpellCheckEnabled(False)        # 红色波浪线是浏览器痕迹
        except Exception:                              # noqa: BLE001
            pass
        # V0.0.52：缓存默认按可用内存比例增长，长时间看图时内核进程会被
        # 缓存撑到几个 GB。强制落盘并封顶 256 MB，进程常驻内存稳定得多。
        try:
            profile.setHttpCacheType(
                QWebEngineProfile.HttpCacheType.DiskHttpCache)
            profile.setHttpCacheMaximumSize(256 * 1024 * 1024)
        except Exception:                              # noqa: BLE001
            pass

        view = QWebEngineView()
        # 让前端知道「这是软件窗口」，从而启用桌面键位与选择策略；
        # 普通浏览器访问同一个服务时没有这个标记，行为完全不变。
        try:
            script = QWebEngineScript()
            script.setName("mrm-native-shell")
            script.setInjectionPoint(
                QWebEngineScript.InjectionPoint.DocumentCreation)
            script.setWorldId(int(QWebEngineScript.ScriptWorldId.MainWorld))
            script.setRunsOnSubFrames(True)
            script.setSourceCode("window.__MRM_NATIVE_SHELL__ = true;")
            profile.scripts().insert(script)
        except Exception:                              # noqa: BLE001
            pass   # 注入失败时前端按普通网页运行，不让整个窗口起不来

        page = _ShellPage(profile, view)
        view.setPage(page)
        settings = page.settings()
        for name, value in (
                ("NavigateOnDropEnabled", False),      # 拖文件进窗口不导航
                ("JavascriptCanOpenWindows", False),   # 不弹新窗口
                ("FullScreenSupportEnabled", False),   # F11 不隐藏窗口边框
                ("PdfViewerEnabled", False),           # 不在软件里开浏览器 PDF 视图
                ("HyperlinkAuditingEnabled", False),
                ("AllowWindowActivationFromJavaScript", False),
                ("ErrorPageEnabled", False),           # 自己出中文提示，不露英文错误页
        ):
            attr = getattr(QWebEngineSettings.WebAttribute, name, None)
            if attr is not None:
                try:
                    settings.setAttribute(attr, value)
                except Exception:                      # noqa: BLE001
                    pass
        # 清掉浏览器动作自带的快捷键；编辑键（复制/粘贴/撤销等）保留。
        for name in _SHELL_CLEAR_ACTION_NAMES:
            enum = getattr(QWebEnginePage.WebAction, name, None)
            if enum is None:
                continue
            try:
                act = page.action(enum)
                if act is not None:
                    act.setShortcut(QKeySequence())
                    act.setVisible(False)              # 右键菜单里也不出现
            except Exception:                          # noqa: BLE001
                pass

        # 外链 / target=_blank：交给系统默认浏览器，软件窗口不跳走、不弹窗。
        def _on_new_window(req):
            try:
                u = req.requestedUrl()
                if u.isValid() and not _is_local(u) and u.scheme() in ("http", "https"):
                    QDesktopServices.openUrl(u)
            except Exception:                          # noqa: BLE001
                pass
        page.newWindowRequested.connect(_on_new_window)

        # 下载：浏览器内核默认静默丢弃，导出与备份在壳里会「点了没反应」。
        # 按桌面软件口径弹保存对话框，未选择时取消下载。
        def _on_download(dl):
            try:
                from PySide6.QtCore import QStandardPaths
                dl_dir = QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.DownloadLocation) or ""
                suggested = dl.suggestedFileName() or "download"
                path, _ = QFileDialog.getSaveFileName(
                    win, "保存文件", os.path.join(dl_dir, suggested))
                if not path:
                    dl.cancel()
                    return
                dl.setDownloadDirectory(os.path.dirname(path))
                dl.setDownloadFileName(os.path.basename(path))
                dl.accept()
            except Exception:                          # noqa: BLE001
                try:
                    dl.cancel()
                except Exception:                      # noqa: BLE001
                    pass
        profile.downloadRequested.connect(_on_download)
        win._download_handler = _on_download          # 保引用防回收

        # 窗口标题固定，不跟随网页 <title> 变化；加载失败给中文提示。
        view.titleChanged.connect(lambda _t: win.setWindowTitle("Sweet采集AI版"))

        def _on_load_finished(ok):
            try:
                page.setZoomFactor(1.0)                # 不残留上次的页面缩放
            except Exception:                          # noqa: BLE001
                pass
            if not ok:
                try:
                    page.setHtml(
                        "<!doctype html><meta charset='utf-8'>"
                        "<div style='font:15px/1.8 system-ui;padding:40px'>"
                        "<h2>界面加载失败</h2>"
                        "<p>本机服务尚未就绪。请关闭窗口后重新启动软件；"
                        "若反复出现，请查看日志目录中的运行日志。</p></div>",
                        QUrl(url))
                except Exception:                      # noqa: BLE001
                    pass
        page.loadFinished.connect(_on_load_finished)

        # 右键菜单：只保留文本编辑动作，没有「在新标签页打开」「检查元素」
        # 这类浏览器项目；链接与图片的右键交给网页自己的业务菜单。
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def _on_context_menu(pos):
            try:
                req = view.lastContextMenuRequest()
                if req is None:
                    return
                editable = bool(req.isContentEditable())
                has_text = bool(req.selectedText())
                if not editable and not has_text:
                    return                            # 业务菜单由网页自己弹
                menu = QMenu(win)
                names = (("剪切", "Cut"), ("复制", "Copy"),
                         ("粘贴", "Paste"), ("全选", "SelectAll"))
                for label, act_name in names:
                    enum = getattr(QWebEnginePage.WebAction, act_name, None)
                    if enum is None:
                        continue
                    action = QAction(label, menu)
                    target = page.action(enum)
                    if target is None:
                        continue
                    action.setEnabled(target.isEnabled())
                    action.triggered.connect(target.trigger)
                    menu.addAction(action)
                if menu.actions():
                    menu.exec(view.mapToGlobal(pos))
            except Exception:                          # noqa: BLE001
                pass
        view.customContextMenuRequested.connect(_on_context_menu)

        # 过滤器只装在本窗口的控件树上，覆盖渲染代理与后续创建的子控件；
        # 不装到 QApplication 上（全局过滤器会吞掉所有窗口的按键）。
        from PySide6.QtWidgets import QWidget
        win._input_filter = _ShellInputFilter(win, page)

        def _install_input_filters():
            targets = [view, view.focusProxy()] + view.findChildren(QWidget)
            for w in targets:
                if w is None or w.property("_mrm_shell_filtered"):
                    continue
                try:
                    w.setProperty("_mrm_shell_filtered", True)
                    w.installEventFilter(win._input_filter)
                except Exception:                      # noqa: BLE001
                    pass
        win._install_input_filters = _install_input_filters
        _install_input_filters()
        # 渲染代理在首次导航后才创建，再补装一次。
        from PySide6.QtCore import QTimer
        QTimer.singleShot(600, _install_input_filters)
        view.setAcceptDrops(False)

        # 首帧渲染完通知外面收启动画面（页面可能比窗口慢一两秒）
        def _on_loaded(ok_):
            try:
                if not ok_:
                    return
                cb = getattr(win, "_mrm_on_ready", None)
                if cb is not None:
                    cb()
            except Exception:                          # noqa: BLE001
                pass
        try:
            view.loadFinished.connect(_on_loaded)
        except Exception:                              # noqa: BLE001
            pass

        view.setUrl(QUrl(url))
        # 系统标题栏回来了，渲染区直接铺满客户区。
        win.setCentralWidget(view)
        # V0.0.24：753 是侧栏「一次显示全部导航」的最小高度 —— 15 个一级项
        # + 任务 7 个状态 + 底部统计与皮肤行，实测内容高 677，视口 721 时
        # 还余 44px；再矮就得滚侧栏，用户明确不接受。锁成下限，宽度同时
        # 给一个下限，避免把任务表挤到没法看。
        min_h = 753
        try:
            from PySide6.QtGui import QGuiApplication
            scr = QGuiApplication.primaryScreen()
            avail = int(scr.availableGeometry().height()) if scr else 0
            # 小屏（可用高连标题栏都塞不下）才降级，正常屏一律 753。
            if avail and avail < min_h + 60:
                min_h = max(620, avail - 60)
        except Exception:                              # noqa: BLE001
            pass
        win.setMinimumSize(1024, min_h)
        win.resize(1360, 860)
        if start_hidden:
            win.hide()          # 启动进托盘：不弹窗，等托盘唤回
        else:
            win.show()
        return win, ""
    except Exception as e:                             # noqa: BLE001
        return None, f"内置渲染窗口创建失败：{e}"


def _make_control_window(url: str, on_exit, app_mode: bool):
    """极简控制窗（默认收进系统不参与日常；界面在应用窗口里）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel,
                                   QPushButton, QVBoxLayout)

    class _Dlg(QDialog):
        def closeEvent(self, event):
            # 用户点 ✕（spontaneous）＝退出软件；程序化关闭不拦。
            if event.spontaneous():
                on_exit()
            super().closeEvent(event)

    dlg = _Dlg()
    dlg.setWindowTitle("Sweet采集AI版 · 后台服务")
    dlg.setWindowFlags(dlg.windowFlags()
                       | Qt.WindowType.WindowMinimizeButtonHint)

    v = QVBoxLayout(dlg)
    v.setContentsMargins(20, 16, 20, 14)

    title = QLabel("后台服务运行中")
    f = QFont()
    f.setPointSize(13)
    f.setBold(True)
    title.setFont(f)
    v.addWidget(title)

    tip = QLabel(
        "界面窗口已独立打开，关闭那个窗口即退出本软件。"
        if app_mode else
        f'界面地址：<a href="{url}">{url}</a>　在浏览器里随时重新打开。')
    tip.setWordWrap(True)
    if not app_mode:
        tip.setTextFormat(Qt.TextFormat.RichText)
        tip.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        tip.setOpenExternalLinks(False)
        tip.linkActivated.connect(lambda _u: webbrowser.open(url))
    v.addWidget(tip)

    row = QHBoxLayout()
    row.addStretch(1)
    b_open = QPushButton("重新打开界面窗口")
    row.addWidget(b_open)
    b_quit = QPushButton("退出软件")
    b_quit.clicked.connect(on_exit)
    row.addWidget(b_quit)
    v.addLayout(row)
    dlg._b_open = b_open                              # type: ignore[attr-defined]
    dlg.resize(420, 150)
    return dlg


def _install_tray(app, win, queue, on_quit, on_show, on_hide):
    """V0.0.27：给壳窗口建一个常驻托盘（能显示/隐藏/暂停/退出）。

    复用桌面端 ``gui.tray.build_tray``（同一套菜单与保活逻辑，不另写一份），
    这里只把回调换成本窗口的动作。托盘不可用（部分精简系统/远程会话）
    返回 None，调用方据此退回「点 X 就退出」的老行为。
    """
    try:
        from PySide6.QtWidgets import QSystemTrayIcon
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        from .tray import build_tray

        state = {"paused": False}

        def _toggle_pause():
            if queue is None:
                return
            try:
                if state["paused"]:
                    queue.resume()
                else:
                    queue.pause("从托盘暂停")
                state["paused"] = not state["paused"]
            except Exception:                          # noqa: BLE001
                pass

        tray = build_tray(app, on_show=on_show, on_pause=_toggle_pause,
                          on_quit=on_quit, on_hide=on_hide)
        tray.show()
        return tray
    except Exception:                                  # noqa: BLE001
        return None


def _ensure_single_instance(app) -> bool:
    """单实例：已有实例在跑时返回 False，并连上它准备「叫窗口」。"""
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    key = "sweet-caiji-webshell-single-instance"
    sock = QLocalSocket()
    sock.connectToServer(key)
    if sock.waitForConnected(300):
        app._single_sock = sock
        return False
    QLocalServer.removeServer(key)          # 清理上次异常退出残留
    srv = QLocalServer()
    srv.listen(key)
    app._single_srv = srv                   # 持引用防 GC
    return True


def _wake_existing(app) -> None:
    """让已在运行的实例把窗口叫出来，然后本进程安静退出。"""
    sock = getattr(app, "_single_sock", None)
    if sock is None:
        return
    try:
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
    except Exception:                                  # noqa: BLE001
        pass


def _attach_single_instance_wakeup(app, win) -> None:
    """第二个实例连上来 ⇒ 把窗口显示到最前（从托盘唤回）。"""
    srv = getattr(app, "_single_srv", None)
    if srv is None or win is None:
        return

    def _on_new_connection():
        # 连接必须收干净，否则第二次双击就连不上了
        try:
            while srv.hasPendingConnections():
                c = srv.nextPendingConnection()
                try:
                    c.readAll()
                except Exception:                      # noqa: BLE001
                    pass
                try:
                    c.disconnectFromServer()
                except Exception:                      # noqa: BLE001
                    pass
        except Exception:                              # noqa: BLE001
            pass
        try:
            win.showNormal()
            win.raise_()
            win.activateWindow()
        except Exception:                              # noqa: BLE001
            pass

    try:
        srv.newConnection.connect(_on_new_connection)
        app._single_wake = _on_new_connection   # Qt 侧不保活 Python 函数
    except Exception:                              # noqa: BLE001
        pass


def shell_main(start_in_tray: bool = False) -> int:
    """Web 壳主流程。返回退出码（0 正常；2 服务起不来）。

    窗口三档（依次尝试）：
      ① 内置渲染内核原生窗口（标准最小化/最大化/关闭 + 托盘常驻）；
      ② 外部 Chromium 浏览器 --app 独立窗口（关窗即退出）；
      ③ 普通浏览器打开 + 控制小窗。
    ``start_in_tray=True``（开机自启传的 ``--tray``）时，这一次启动不弹窗。
    事件循环结束后完成收尾再返回——进程级硬退出由入口脚本负责
    （r71 护栏：产品包内禁止该调用）。
    """
    # ⚠ QtWebEngineWidgets 必须在 QApplication 创建**之前**导入（Qt 硬性
    # 要求，晚导入直接 abort）。先导再判断能不能用。
    webengine_ok = False
    if not _NO_BROWSER:
        try:
            import PySide6.QtWebEngineWidgets  # noqa: F401
            webengine_ok = True
        except Exception:                              # noqa: BLE001
            webengine_ok = False

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Sweet采集AI版")
    _ic = _app_icon()
    if _ic is not None:
        app.setWindowIcon(_ic)              # 任务栏/弹窗统一用软件图标

    # 单实例判断必须在起服务之前：第二个实例绑 8080 会失败并弹「起不来」，
    # 而它真正该做的是把已有窗口叫出来。
    if not _ensure_single_instance(app):
        _wake_existing(app)
        return 0

    # ── 启动画面（V0.0.38）──────────────────────────────────────────
    # 起服务要读库 / 起队列 / 拉订阅，中间几秒原来是白屏；用户原话
    # 「我的启动窗口动画呢」。设置里「显示启动画面」关掉就完全不闪。
    splash = None
    if not start_in_tray:
        try:
            from ..config import load_config
            if bool(getattr(load_config(), "splash_enabled", True)):
                splash = _show_splash(app)
        except Exception:                              # noqa: BLE001
            splash = None

    ok, msg, ctx = _start_engine()
    if not ok:
        shell_main.error = msg                         # type: ignore[attr-defined]
        return 2
    url = ctx["server"].url.rstrip("/")
    cfg = ctx.get("cfg")

    exiting = {"done": False}
    win = None

    def _quit():
        if exiting["done"]:
            return
        exiting["done"] = True
        # 先关窗口（走正常 closeEvent，释放渲染页面），再结束事件循环。
        # 不这样做的话隐藏中的窗口会在进程退出时被硬拆，日志里会留异常。
        try:
            if win is not None:
                win._mrm_quitting = True
                win.close()
        except Exception:                              # noqa: BLE001
            pass
        app.quit()

    # V0.0.39：网页右下角那个「完全退出」——页面 POST /api/app/quit，
    # 这里只被 set 一个 threading.Event，再在 Qt 线程里轮询后走上面同一条
    # 退出链（收尾、关窗、停队列一步不少）。跨线程直接调 app.quit() 会炸。
    quit_evt = threading.Event()
    try:
        ctx["server"].quit_hook = quit_evt.set
    except Exception:                                  # noqa: BLE001
        pass

    def _poll_quit():
        if quit_evt.is_set():
            _quit()

    _quit_poll = QTimer(app)                           # 挂 app 上随进程回收
    _quit_poll.timeout.connect(_poll_quit)
    _quit_poll.start(300)

    # 「启动后最小化到托盘」：只有托盘真的可用才敢藏窗口，否则叫不回来。
    # 开机自启那条路（--tray）无论如何都藏，那是它存在的意义。
    start_min = bool(start_in_tray or
                     getattr(cfg, "tray_start_minimized", False))
    tray_ok = False
    if start_min:
        try:
            from PySide6.QtWidgets import QSystemTrayIcon
            tray_ok = bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:                              # noqa: BLE001
            tray_ok = False
    start_hidden = bool(start_min and tray_ok)

    # ── ① 内置原生窗口 ────────────────────────────────────────────
    mode = ""
    if webengine_ok:
        win, err = _open_embedded_window(url, _quit, start_hidden=start_hidden)
        if win is not None:
            mode = "embedded"
        else:
            from ..logutil import get_logger
            get_logger("mrm.webshell").warning("内置窗口不可用，转外部浏览器：%s", err)

    if win is not None:
        def _show_win():
            try:
                win.showNormal()
                win.raise_()
                win.activateWindow()
            except Exception:                          # noqa: BLE001
                pass

        def _hide_win():
            try:
                win.hide()
            except Exception:                          # noqa: BLE001
                pass

        tray = _install_tray(app, win, ctx.get("queue"), _quit,
                             _show_win, _hide_win)
        win._mrm_tray = tray
        win._mrm_quitting = False
        # V0.0.45：托盘可用 ⇒ 窗口的 ✕ 只收进托盘；托盘没建起来 ⇒ 保持
        # 「✕ = 退出」（否则窗口一藏就既没界面也没图标，只能任务管理器杀）。
        win._mrm_hide_to_tray = _hide_win if tray is not None else None

        # 启动收尾：页面首帧出来（或最多等 4 秒）收掉启动画面，主窗口淡入。
        def _ready():
            if getattr(win, "_mrm_ready_done", False):
                return
            win._mrm_ready_done = True
            _finish_splash(splash, win)

        win._mrm_on_ready = _ready
        if splash is None:
            QTimer.singleShot(0, _ready)
        else:
            QTimer.singleShot(4000, _ready)
        # V0.0.45：系统标题栏的 ✕ 已经改回「收进托盘」（见 _Win.closeEvent），
        # 这里只补一条兜底与说明：托盘没建起来时窗口必须亮着，不能藏。
        # 兜底：判定「托盘可用」但托盘其实没建起来（极少数远程会话/被系统禁用），
        # 这时候窗口还藏着 = 既没界面也没图标，只能任务管理器杀。必须亮出来。
        if tray is None and start_hidden:
            try:
                win.show()
            except Exception:                          # noqa: BLE001
                pass

        def _balloon(title: str, text: str):
            if tray is None:
                return
            try:
                from PySide6.QtWidgets import QSystemTrayIcon
                tray.showMessage(title, text,
                                 QSystemTrayIcon.MessageIcon.Information, 5000)
            except Exception:                          # noqa: BLE001
                pass

        def _balloon_once():
            if getattr(win, "_mrm_ballooned", False):
                return
            win._mrm_ballooned = True
            _balloon("Sweet采集AI版仍在后台运行",
                     "窗口已收进托盘，采集不会中断。\n"
                     "重新打开：单击托盘图标。\n"
                     "彻底退出：页面右下角的「退出软件」，或右键托盘图标 → 退出")
        win._mrm_on_hide = _balloon_once
        _attach_single_instance_wakeup(app, win)

        # 启动即进托盘：给一条气泡说明，否则用户以为没启动成功。
        if start_hidden and tray is not None:
            win._mrm_ballooned = True
            _balloon("Sweet采集AI版已在后台启动",
                     "按设置「启动后最小化到托盘」，窗口没有弹出。\n"
                     "单击托盘图标打开主界面")

    # ── ② 外部浏览器应用窗口 ──────────────────────────────────────
    browser = None
    if mode == "" and not _NO_BROWSER:
        browser = _open_app_window(url)
        if browser is not None:
            mode = "app"
    # ── ③ 普通打开 ───────────────────────────────────────────────
    if mode == "":
        webbrowser.open(url)
    ctx["browser"] = browser

    # 控制窗：②③ 形态需要"重新打开界面"的落点；① 形态窗口本身带
    # 最小化/关闭，不再多一个小窗。
    if mode != "embedded":
        dlg = _make_control_window(url, _quit, mode == "app")

        def _reopen():
            if mode == "app":
                b = _open_app_window(url)
                if b is not None:
                    ctx["browser"] = b
            else:
                webbrowser.open(url)
        dlg._b_open.clicked.connect(_reopen)           # type: ignore[attr-defined]
        if mode == "app":
            dlg.show()
            dlg.hide()      # 隐藏但存在：保活事件循环
        else:
            dlg.show()

        # 轮询界面窗口：被用户关掉 ⇒ 控制窗亮出来并退出。
        if mode == "app":
            def _watch():
                if exiting["done"]:
                    return
                b = ctx.get("browser")
                if b is not None and b.poll() is not None:
                    dlg.showNormal()
                    dlg.raise_()
                    _quit()
                    return
                QTimer.singleShot(1500, _watch)
            QTimer.singleShot(3000, _watch)

    # 回退到浏览器形态时窗口不是我们的：启动画面别一直挂着
    if win is None and splash is not None:
        def _drop_splash():
            try:
                splash.close()
            except Exception:                          # noqa: BLE001
                pass
        QTimer.singleShot(1500, _drop_splash)

    app.exec() if hasattr(app, "exec") else app.exec_()
    _shutdown(ctx)
    return 0
