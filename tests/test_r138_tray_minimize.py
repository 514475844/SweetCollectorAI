"""R137 + R138 + R155 托盘护栏：右键菜单真在、单击/双击能唤回、点 X 缩托盘不真退。

R137 根因（台式机真机实测「托盘右键没有可用功能」）：
    `build_tray()` 里的 QMenu 是**局部变量且无 parent**，Qt 不接管托盘右键菜单
    的所有权，函数一返回就被 GC ⇒ 右键不弹 / 弹了点了没反应。
    本文件钉的是**行为**（菜单活得过 gc、每项点了真回调），不是数值。

R138：
    · 点 X ⇒ 窗口藏起来、**进程继续跑**（不关库、不给任务发停止信号）；
    · 明确退出（托盘「退出」/ 设置菜单「完全退出」）⇒ 仍走四层安全收尾；
    · `ActivationReason.Trigger`（单击）/ `DoubleClick`（双击）都能唤回窗口。

R155（用户实测反馈「最小化和关闭都是最小化到托盘，这个不对」）：
    · **最小化（-）= 标准行为，缩任务栏**，不再藏托盘（changeEvent 劫持已删）；
    · 关闭（X）保持缩托盘；设置菜单退出项改名「完全退出」并补 tooltip。

反向可变：把 `event.ignore()+hide()` 删掉 → 第 4 条立刻红；把 QMenu 改成无
parent 的局部变量 → 第 1/2 条立刻红；给 changeEvent 加回「最小化→藏托盘」
→ R155 两条立刻红。
"""

import gc
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _FakeTray:
    """托盘替身：只记录被调用了什么（offscreen 下不依赖真托盘可用）。"""

    def __init__(self):
        self.messages = []
        self.tooltips = []

    def showMessage(self, *a, **k):
        self.messages.append(a)

    def setToolTip(self, t):
        self.tooltips.append(t)


class _StubQueue:
    def __init__(self, running=()):
        self.stop_all_calls = 0
        self.stop_calls = 0
        self.running = set(running)

    def stop_all(self):
        self.stop_all_calls += 1

    def stop(self):
        self.stop_calls += 1


class _StubDB:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _boot(monkeypatch, tmp_path):
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    monkeypatch.setenv("MRM_NO_QUEUE_LOOP", "1")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    for old in app.topLevelWidgets():
        try:
            for tmr in old.findChildren(QTimer):
                tmr.stop()
            old.close()
        except Exception:
            pass
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    import media_resource_manager.gui.main_window as mw
    return mw, app


def _make_stub_win(mw):
    """纯 Python 替身：不跑 MainWindow.__init__（r117 已证明那样会段错误）。"""

    class _Plain:
        def closeEvent(self, event):        # noqa: N802 占位
            return None

    win = _Plain()
    win._CLOSE_BUDGET_S = mw.MainWindow._CLOSE_BUDGET_S
    win.tray = None
    win._explicit_quit = False
    win._queue = _StubQueue()
    win.db = _StubDB()
    win.calls = []
    win._thumb_pool = None
    win._bg_pool = None
    win._stop_thumb_pump = lambda *a, **k: win.calls.append("thumb_pump")
    win._save_ui_state = lambda *a, **k: win.calls.append("save_ui")
    win._join_threadpool = lambda pool, timeout=5.0: win.calls.append(
        ("join_bg", timeout))
    win._wait_bg_qthreads = lambda budget=8.0: win.calls.append(
        ("wait_bg", budget))

    def _run_close(ev):
        try:
            mw.MainWindow.closeEvent(win, ev)
        except TypeError:
            pass
        except AttributeError:
            pass

    win.closeEvent = _run_close
    return win


def _build(mw_or_app, app, calls):
    from media_resource_manager.gui.tray import build_tray
    return build_tray(
        app,
        on_show=lambda: calls.append("show"),
        on_pause=lambda: calls.append("pause"),
        on_quit=lambda: calls.append("quit"),
        on_start_all=lambda: calls.append("start"),
        on_stop_all=lambda: calls.append("stop"),
        on_hide=lambda: calls.append("hide"))


# ------------------------------------------------------------ R137 右键菜单


def test_tray_menu_outlives_build_tray_and_is_parented(monkeypatch, tmp_path):
    """★ R137 根因：菜单必须活得过 build_tray() 返回，且挂在 tray 下保活。"""
    mw, app = _boot(monkeypatch, tmp_path)
    calls = []
    tray = _build(mw, app, calls)
    gc.collect()
    menu = getattr(tray, "_mrm_menu", None)
    assert menu is not None, (
        "托盘菜单没有常驻引用 —— build_tray 返回后就会被 GC，"
        "真机右键不弹菜单（R137 根因）")
    try:
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    except RuntimeError:
        texts = []
    assert texts, (
        "托盘菜单已经被销毁（C++ 对象没了）—— Qt 不接管它的所有权，"
        "必须自己留引用保活")
    assert tray.contextMenu() is not None, "setContextMenu 没生效"
    assert getattr(tray, "_mrm_actions", None), "菜单动作没有常驻引用"


def test_tray_menu_has_all_items_and_they_are_wired(monkeypatch, tmp_path):
    """菜单项全中文齐全，且**每项点了真回调**（不是摆设）。"""
    mw, app = _boot(monkeypatch, tmp_path)
    calls = []
    tray = _build(mw, app, calls)
    menu = tray._mrm_menu
    acts = {a.text(): a for a in menu.actions() if not a.isSeparator()}
    for label in ("显示主窗口", "全部开始", "全部停止", "暂停 / 继续队列", "退出"):
        assert label in acts, f"托盘菜单缺「{label}」，现有：{sorted(acts)}"
    acts["显示主窗口"].trigger()
    acts["全部开始"].trigger()
    acts["全部停止"].trigger()
    acts["暂停 / 继续队列"].trigger()
    acts["退出"].trigger()
    assert calls == ["show", "start", "stop", "pause", "quit"], (
        f"菜单项回调没接通：{calls}")


def test_tray_single_click_and_double_click_both_show(monkeypatch, tmp_path):
    """R138 P0-B：单击（Trigger）和双击（DoubleClick）都能唤回窗口。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtWidgets import QSystemTrayIcon
    calls = []
    tray = _build(mw, app, calls)
    R = QSystemTrayIcon.ActivationReason
    tray.activated.emit(R.Trigger)
    tray.activated.emit(R.DoubleClick)
    assert calls == ["show", "show"], f"单击/双击唤回没都接通：{calls}"
    # 右键交给 Qt 弹菜单，不能在这里被吞掉（不得触发唤回）
    tray.activated.emit(R.Context)
    assert calls == ["show", "show"], "右键被当成唤回处理了（会把菜单吞掉）"


def test_launch_routes_quit_through_four_layer_teardown(monkeypatch, tmp_path):
    """R137 红线 5：退出项不能挂 app.quit（它不派发 closeEvent ⇒ 四层收尾被跳过）。"""
    import inspect
    mw, app = _boot(monkeypatch, tmp_path)
    src = inspect.getsource(mw.launch)
    assert "win.request_quit" in src, (
        "launch() 仍然把 app.quit 挂给托盘「退出」—— app.quit 不走 closeEvent，"
        "四层安全收尾会被整段跳过")
    assert "setQuitOnLastWindowClosed" in src, (
        "launch() 没处理 setQuitOnLastWindowClosed —— 缩托盘后 Qt 会顺手结束"
        "事件循环，常驻就没意义了")
    assert "_resident" in src, (
        "launch() 没判托盘是否真的可用 —— 托盘不可用时也缩托盘，"
        "窗口一藏程序就叫不回来也退不掉（只能任务管理器杀）")


def test_no_tray_means_close_still_quits(monkeypatch, tmp_path):
    """没有可用托盘 ⇒ 关闭还是真退出（四层收尾照跑），不许变成幽灵进程。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtGui import QCloseEvent
    win = _make_stub_win(mw)
    win.tray = None
    win.closeEvent(QCloseEvent())
    assert win.db.closed == 1, "没有托盘时点 X 竟然没走四层收尾"


def test_tray_resident_gate(monkeypatch, tmp_path):
    """常驻开关：有托盘且不要求真退 → 可常驻；任一不满足 → 真退。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from media_resource_manager.gui.main_window import _tray_can_reside

    class _S:
        pass

    s = _S()
    assert _tray_can_reside(s) is False, "没托盘也判定成能常驻"
    s.tray = _FakeTray()
    assert _tray_can_reside(s) is True, "有托盘却不让常驻"
    s._explicit_quit = True
    assert _tray_can_reside(s) is False, "明确退出时还能缩托盘（收尾会被吃掉）"


# ------------------------------------------------------- R138 常驻 / 唤回


def test_close_hides_to_tray_and_keeps_running(monkeypatch, tmp_path):
    """★ P0-A：点 X ⇒ 窗口藏起来、进程继续跑（不关库、不停任务）。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtWidgets import QMainWindow

    class _Win(QMainWindow):
        def closeEvent(self, ev):           # noqa: N802
            mw.MainWindow.closeEvent(self, ev)

    win = _Win()
    win.tray = _FakeTray()
    win._explicit_quit = False
    win._queue = _StubQueue(running={1, 2})
    win.db = _StubDB()
    win.show()
    assert win.isVisible(), "前置条件：窗口得先显示出来"
    win.close()
    assert not win.isVisible(), (
        "点 X 之后窗口没藏进托盘 —— 要么还是真退出了，要么 hide 没生效")
    assert win.db.closed == 0, (
        "缩托盘却把数据库关了 —— 后台任务会写已关闭的库（R35 老坑）")
    assert win._queue.stop_all_calls == 0, (
        "缩托盘却给任务发了停止信号 —— 说好的「后台继续跑」没兑现")
    # 唤回：单击/双击托盘走的就是 win.show
    mw.MainWindow.show(win)
    assert win.isVisible(), "从托盘唤不回窗口"
    assert not win.isMinimized(), "唤回后窗口仍是最小化（用户看不见）"


def test_minimize_stays_in_taskbar(monkeypatch, tmp_path):
    """R155-A：点最小化（-）= 标准行为——缩任务栏，**不再**藏托盘。

    用户原话：「最小化和关闭都是最小化到托盘，这个不对。最小化就是最小化到
    任务栏，关闭才是最小化到托盘。」
    """
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtWidgets import QMainWindow
    # ① 产品类里不许再有「窗口状态改变→藏托盘」的劫持（changeEvent 已删除）
    assert "changeEvent" not in vars(mw.MainWindow), (
        "MainWindow 又自己接了 changeEvent —— R155 要求最小化走 Qt 标准行为"
        "（缩任务栏），任何「状态改变→藏托盘」的劫持都必须删掉")
    # ② 行为级：派发一次窗口状态变化，窗口不许被藏
    class _Win(QMainWindow):
        pass

    win = _Win()
    win.tray = _FakeTray()
    hidden = []
    win._hide_to_tray = lambda: hidden.append("hid")
    win.show()
    win.showMinimized()
    app.processEvents()
    assert hidden == [], (
        f"最小化又藏托盘了（R155：最小化=任务栏，关闭=托盘）：{hidden}")
    assert win.isVisible(), (
        "最小化后窗口不可见 —— 标准语义里最小化仍算可见（在任务栏），"
        "被 hide 掉才是藏托盘")


def test_explicit_quit_still_runs_four_layer_teardown(monkeypatch, tmp_path):
    """P0-A 反向：明确退出时**即使有托盘**也必须走四层收尾（不能缩托盘了事）。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtGui import QCloseEvent
    win = _make_stub_win(mw)
    win.tray = _FakeTray()          # 有托盘也不能缩
    win._explicit_quit = True
    win.closeEvent(QCloseEvent())
    assert win._queue.stop_all_calls >= 1, "明确退出时没给任务发停止信号"
    assert "thumb_pump" in win.calls, "明确退出时没停缩略图泵"
    assert win.db.closed == 1, "明确退出时没关库 —— 四层收尾被缩托盘分支吃掉了"


def test_request_quit_sets_explicit_flag(monkeypatch, tmp_path):
    """托盘「退出」/ 设置菜单「完全退出」→ request_quit：先标记再 close。

    ⚠ R185 改判（不是放宽）：R185 之后 request_quit 收尾完会调
    `lifecycle.force_quit(win)`，而它正常路径是 **`os._exit(0)`
    —— 不返回**。测试若不拦，pytest 进程会在这一条上**当场消失**
    （表现为「跑到一半没总结行、junit 也没写」的假 abort，本棒实测
    RC=0 静默退出 / 混跑时 139）。所以这里把 force_quit 换成记录器，
    既保住原判据（标记 + close），又新增「必须走 force_quit」这一条。
    """
    from media_resource_manager.gui import lifecycle

    mw, app = _boot(monkeypatch, tmp_path)
    win = _make_stub_win(mw)
    closed = []
    seen = []
    win.close = lambda: closed.append("close")
    monkeypatch.setattr(lifecycle, "force_quit",
                        lambda obj=None, **kw: seen.append(obj))
    mw.MainWindow.request_quit(win)
    assert win._explicit_quit is True, "request_quit 没标记「明确退出」"
    assert closed == ["close"], "request_quit 没走 self.close()（closeEvent 收尾）"
    assert seen and seen[0] is win, "request_quit 没收尾到 force_quit（R185）"


def test_tray_tooltip_reflects_state(monkeypatch, tmp_path):
    """P1-C：托盘提示反映「几个任务在跑 / 是否后台常驻」。"""
    mw, app = _boot(monkeypatch, tmp_path)
    win = _make_stub_win(mw)
    fake = _FakeTray()
    win.tray = fake
    win._queue = _StubQueue(running={1, 2})
    win.isVisible = lambda: False
    mw.MainWindow._refresh_tray_tooltip(win)
    assert fake.tooltips and "2 个任务运行中" in fake.tooltips[-1], (
        f"托盘提示没反映运行任务数：{fake.tooltips}")
    win._queue = _StubQueue(running=set())
    mw.MainWindow._refresh_tray_tooltip(win)
    assert "后台常驻中" in fake.tooltips[-1], (
        f"空闲且藏起来时提示没说常驻：{fake.tooltips}")
    # 没有托盘时静默跳过，不能抛
    win2 = _make_stub_win(mw)
    mw.MainWindow._refresh_tray_tooltip(win2)
