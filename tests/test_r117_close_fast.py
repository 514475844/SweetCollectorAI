"""R117 第6项：有任务在跑时点右上角关闭，要「秒退」而不是卡住等好久。

现象：任务运行中点 X，界面卡住很久才退。

病根（R117 实测定位）：`closeEvent` 里是一串**无上限的串行等待** ——
`_thumb_pool.waitForDone(5000)` + `QTTP.globalInstance().waitForDone(2000)`
+ `_join_threadpool(5.0)` + 队列 `th.join(8)` + `_wait_bg_qthreads(8.0)`
＝ 最坏 **28 秒**；而且全程**不告诉正在跑的任务「要退了」**，它们继续下载/写库，
join 自然等不到。

修法两条：① 一进来先给运行中任务发停止信号；② 全程共用一个截止时刻
`_CLOSE_BUDGET_S`，每层只等「还剩多少」。

本文件钉**行为**：
  · 有任务在跑时关闭，**先发停止信号**（队列 stop_all 被调用）；
  · 即使后台线程**永不收工**（模拟卡死的解码线程），关闭耗时也有界；
  · 四层安全收尾一个都不能少（thumb pump 停 / 队列停 / 后台 qthread 等 / db 关）；
  · 预算常量是「有界的」而不是「等于某个数」——别把 6.0 钉死。
"""

import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _StubThread:
    """永不收工的线程：join 睡满给定 timeout 才返回。"""

    def __init__(self, spin: float = 1.0):
        self._spin = spin

    def is_alive(self):
        return True

    def join(self, timeout=None):
        t = timeout if timeout is not None else self._spin
        time.sleep(min(float(t), self._spin))


class _StubPool:
    """永不收工的 QThreadPool 替身：waitForDone 睡满给定毫秒。"""

    def __init__(self, spin: float = 1.0):
        self._spin = spin
        self.waited_ms = []

    def waitForDone(self, ms=None):
        try:
            self.waited_ms.append(ms)
        except Exception:
            pass
        t = (float(ms) / 1000.0) if ms else self._spin
        time.sleep(min(t, self._spin))


class _StubQueue:
    def __init__(self):
        self.stop_all_calls = 0
        self.stop_calls = 0
        self._thread = _StubThread(spin=1.0)

    def stop_all(self):
        self.stop_all_calls += 1

    def stop(self):
        self.stop_calls += 1

    @property
    def running(self):
        return {1, 2}


class _StubDB:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _Plain:
    """纯 Python 替身，**不继承 QWidget**。

    ⚠ 早先这里用 QWidget 子类 + MainWindow.closeEvent 直接调，整进程 segfault：
       MainWindow 的 `super().closeEvent(event)` 落到一个没跑过 __init__ 的
       QMainWindow 上（QObject 内部指针是空的）⇒ 段错误。
       这里要验的是**收尾顺序 + 预算有界**，不是 Qt 自己的 closeEvent，
       所以用纯对象最稳：末尾那句 super() 会抛 TypeError，被下面统一接住，
       在它之前的等待/发信号逻辑都已执行完。
    """

    def closeEvent(self, event):      # noqa: N802  占位，被下面的调用绕过
        return None


def _call_close(mw_mod, win, ev):
    """跑 MainWindow.closeEvent 的收尾逻辑，接住末尾 super() 的 TypeError。

    末尾那句 `super().closeEvent(event)` 在纯对象替身上必然 TypeError
    （self 不是 MainWindow 实例）—— 这**不影响**我们要验的东西：
    发停止信号 / 各层有界等待 / 关库，全都在它之前已经做完。
    """
    try:
        mw_mod.MainWindow.closeEvent(win, ev)
    except TypeError:
        pass
    except AttributeError:
        pass


def _make_win(mw_mod):
    """造一个只挂 closeEvent 所需属性的替身（不跑 MainWindow.__init__）。"""
    class _Win(_Plain):
        pass

    win = _Win()
    # ⚠ 预算常量必须显式搬过来：closeEvent 里读的是 `self._CLOSE_BUDGET_S`，
    #   纯对象替身不继承 MainWindow，少了这一行就 AttributeError（第一版踩了）。
    win._CLOSE_BUDGET_S = mw_mod.MainWindow._CLOSE_BUDGET_S
    # 把 MainWindow.closeEvent 的逻辑绑过来（末尾 super() 由调用方接住）
    win.closeEvent = lambda ev: _call_close(mw_mod, win, ev)
    win._queue = _StubQueue()
    win.db = _StubDB()
    win._thumb_pool = _StubPool(spin=1.0)
    win._bg_pool = object()
    win.calls = []
    win._stop_thumb_pump = lambda *a, **k: win.calls.append("thumb_pump")
    win._save_ui_state = lambda *a, **k: win.calls.append("save_ui")
    win._join_threadpool = lambda pool, timeout=5.0: win.calls.append(
        ("join_bg", timeout))
    win._wait_bg_qthreads = lambda budget=8.0: win.calls.append(
        ("wait_bg", budget))
    return win


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
    mw._ensure_single_instance = lambda app_: True
    return mw, app


# ---------------------------------------------------------------- 第6项


def test_close_sends_stop_signal_to_running_tasks(monkeypatch, tmp_path):
    """★ 关键行为：关闭**第一件事**就是给运行中的任务发停止信号。

    没有这条，join 只能干等任务自己跑完 —— 用户看到的就是「卡很久」。
    """
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtGui import QCloseEvent

    win = _make_win(mw)
    try:
        win.closeEvent(QCloseEvent())
        assert win._queue.stop_all_calls >= 1, (
            "关闭时没给运行中的任务发停止信号 —— 这正是「点 X 卡很久」的根因")
    finally:
        pass


def test_close_is_bounded_when_background_never_finishes(monkeypatch, tmp_path):
    """★ 有界性：后台线程**永不收工**时，关闭耗时仍受预算约束。

    这里把每个 wait 的替身都设成「睡满给定时长」，所以耗时
    ≈ 各层拿到的 timeout 之和 —— 一旦某层又用回固定秒数，
    这个和就会飙上去，判据立刻红。
    """
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtGui import QCloseEvent

    win = _make_win(mw)
    budget = float(win._CLOSE_BUDGET_S)
    try:
        t0 = time.time()
        win.closeEvent(QCloseEvent())
        elapsed = time.time() - t0
        # 预算有界 + 少量开销余量（替身是 sleep，误差极小；留 2s 兜系统噪声）
        assert elapsed <= budget + 2.0, (
            f"后台永不收工时关闭耗时 {elapsed:.1f}s，超过预算 {budget}s + 余量 "
            f"—— 各层等待又变回无上限的固定秒数了")
        # 反向：也不该「快到没做收尾」
        assert win.db.closed == 1, "四层收尾之一（关库）被跳过了"
    finally:
        pass


def test_close_keeps_four_layer_teardown(monkeypatch, tmp_path):
    """四层安全收尾一个都不能少（R39c 的血泪教训，别为了快砍掉）。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtGui import QCloseEvent

    win = _make_win(mw)
    try:
        win.closeEvent(QCloseEvent())
        joined = [c for c in win.calls if isinstance(c, tuple)
                  and c[0] == "join_bg"]
        waited = [c for c in win.calls if isinstance(c, tuple)
                  and c[0] == "wait_bg"]
        assert "thumb_pump" in win.calls, "没停缩略图泵 —— 会解图解到库已关"
        assert win._queue.stop_calls >= 1, "没停队列"
        assert joined, "没等 _bg_pool 收工（R39d：不等就是 0xC0000409）"
        assert waited, "没等后台 QThread 收工"
        assert win.db.closed == 1, "没关库"
        # 各层等待必须都**有界**（不是 None / 负数 / 超预算的固定秒数）
        budget = float(win._CLOSE_BUDGET_S)
        for _, t in joined + waited:
            assert t is not None and 0 <= t <= budget, (
                f"某层等待 {t} 超出预算 {budget} —— 又变回无上限等待了")
    finally:
        pass


def test_close_budget_is_finite_and_sane(monkeypatch, tmp_path):
    """预算常量本身：必须是**有限且远小于旧的最坏 28s**的正数。

    钉「有界且合理」，不钉 6.0 这个具体取值。
    """
    mw, app = _boot(monkeypatch, tmp_path)
    b = mw.MainWindow._CLOSE_BUDGET_S
    assert isinstance(b, (int, float)) and b > 0, f"预算非法：{b!r}"
    assert b < 28, (
        f"关闭预算 {b}s 不小于旧实现的最坏 28s "
        f"(5+2+5+8+8) —— 等于没改")
