"""r39f：锁死「统计面板每次刷新都全盘重扫」这个卡顿回归。

真机现场（30 分钟前台全按钮扫描）：

    看门狗在「新增任务」上打出 STALL，扒到的主线程栈是
        pathlib._local.exists
        media_resource_manager/stats.py:30 library_stats
        main_window.py refresh_stats
        main_window.py refresh / refresh_list
        queue._on_queue_event

链路是：`refresh()` 的**尾巴**调 `refresh_stats()`，而 `refresh()` 被
每一个队列事件驱动（任务状态一变就整跑一遍）。`library_stats()` 干的是

    SELECT ... FROM images            ← 4700+ 行全拉出来
    + 对每张有 path 的记录 Path(path).exists()   ← 几千次 stat()

采集期间队列事件一个接一个 ⇒ 不停重扫全盘，单次看着不多、连起来把 Qt
事件循环堵了 17 秒（观测值「新增任务 blocked the qt loop for 17.0s」）。

修法（本文件上锁）：refresh_stats() 加时间节流 —— 连着来了就只真算一次，
静下来后用一个 single-shot QTimer 补算一次；手动点「刷新统计」按钮
走 force=True，按下去那一刻一定重算。

窗口构造沿用 r39e 那套：`_ensure_single_instance = lambda *a: True` 免得
撞上单实例检查。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _new_window(qapp):
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    # 别把窗口真的挂到屏幕上，省得测试环境里弹出来
    try:
        w.hide()
    except Exception:
        pass
    return w


class _FakeStats:
    """替掉真正的 library_stats，只数调用次数和它看到的参数。"""

    def __init__(self):
        self.calls = 0

    def __call__(self, db, library_dir):
        self.calls += 1
        return {
            "total_records": 10,
            "on_disk": 10,
            "quarantined": 1,
            "blacklisted": 0,
            "favorites": 2,
            "bytes_human": "1.0 MB",
            "by_ext": {"JPEG": 10},
            "per_task": {1: 10},
            "duplicate_rate_pct": 0.0,
            "duplicate_files": 0,
            "avg_score": 0.5,
        }


@pytest.fixture
def win(qapp, monkeypatch):
    w = _new_window(qapp)
    # ⚠ R160 适配 R152-A7 新契约（不是放水）：A7 起 stats_text 已按需求 21
    #   撤出界面（setHidden(True)），refresh_stats 见控件藏着直接 return。
    #   不把它显示出来，下面所有节流断言都会变成「一次都没真算」的假红
    #   （R159 全量分段跑 03:1x 实测命中 3 条）。A7 的隐藏闸本身由
    #   test_hidden_stats_label_skips_compute 单独守着。
    lbl = getattr(w, "stats_text", None)
    if lbl is not None:
        try:
            lbl.setVisible(True)
        except Exception:
            pass
    fake = _FakeStats()
    import media_resource_manager.gui.main_window as mwm

    monkeypatch.setattr(mwm, "library_stats", fake)
    # MainWindow.__init__ 里就会调一次 refresh_stats() 填初始统计，那一下
    # 已经把 _stats_at 写掉了。不清干净的话，测试里紧接着的第一次调用会被
    # 节流挡掉（**这本身正是节流生效的证明**，但会把本用例的断言搅乱）。
    # 补算定时器也停掉，免得它到点又真算一次、干扰计数。
    t = getattr(w, "_stats_defer_timer", None)
    if t is not None:
        try:
            t.stop()
        except Exception:
            pass
    w._stats_defer_timer = None
    w._stats_at = None
    yield w, fake
    # 清掉可能还挂着的补算定时器，免得泄漏到下一个用例
    t = getattr(w, "_stats_defer_timer", None)
    if t is not None:
        try:
            t.stop()
        except Exception:
            pass
        w._stats_defer_timer = None


def test_consecutive_refreshes_compute_once(win):
    """连着刷 5 次，真算只该发生 1 次（其余走节流，排尾部补算）。"""
    _w, fake = win
    for _ in range(5):
        _w.refresh_stats()
    # 第一次是真算；第二次起被节流掉
    assert fake.calls == 1, (
        f"连刷 5 次却真算了 {fake.calls} 次 —— 节流没生效，采集时又会"
        f"每个队列事件重扫一次全库")
    # 被节流掉的那几次应该排了一个尾部补算，而不是直接丢掉
    assert getattr(_w, "_stats_defer_timer", None) is not None, (
        "被节流掉后没有排尾部补算，静下来时统计面板会永远停在旧数")


def test_force_recomputes_immediately(win):
    """点「刷新统计」必须立刻重算，不能等节流窗口。"""
    _w, fake = win
    _w.refresh_stats()
    assert fake.calls == 1
    _w.refresh_stats(force=True)
    assert fake.calls == 2, "force=True 没跳过节流"


def test_deferred_timer_fires_after_window(win):
    """节流窗口过后的那条补算会真的执行（不是只排了个空定时器）。"""
    import time as _t

    _w, fake = win
    # 把节流窗口伪装成「刚算过」，这一次才会被挡下并排尾部补算
    _w._stats_at = _t.monotonic()
    _w.refresh_stats()
    timer = getattr(_w, "_stats_defer_timer", None)
    assert timer is not None
    assert isinstance(timer, QTimer)
    assert timer.isSingleShot()
    # 真等 3 秒不划算，直接发超时信号走一遍 _fire 那条路径
    # （「确实排了一个补算定时器」已由上一条断言锁住）
    timer.timeout.emit()
    assert fake.calls == 1, (
        f"尾部补算没跑（真算次数还是 {fake.calls}）—— 静下来后统计面板不会更新")
    # 补算自己会把句柄清掉，避免下一轮又排一次
    assert getattr(_w, "_stats_defer_timer", None) is None, (
        "补算跑完没清 _stats_defer_timer，会一直拦着后续的尾部补算")


def test_button_is_wired_to_forced_refresh(win):
    """「刷新统计」必须接到 force=True 版本，否则点了不刷新。

    r45：那个 QPushButton 连同整排按钮从日志页搬进了顶部菜单
    「维护 → 日志统计」，所以断言的**位置**从 _build_log_stats_tab
    挪到了 _build_ui（动作在那儿建、槽在那儿接）。契约本身没变。
    """
    import inspect

    from media_resource_manager.gui.main_window import MainWindow

    # r40：日志与统计已合并成 _build_log_stats_tab（旧 _build_stats_tab 已删）
    # r45：按钮行整体撤出该页，force=True 的连接挪到菜单构建里
    src = inspect.getsource(MainWindow._build_ui)
    assert "refresh_stats(force=True)" in src, (
        "「刷新统计」没接 force=True，点它会被节流挡掉、看不到新数")
    #顺带守住：日志页里不许又把按钮 new 回来
    # （只看代码行，别把注释里提到的那几个字也算进去）
    page_src = inspect.getsource(MainWindow._build_log_stats_tab)
    code = [ln for ln in page_src.splitlines()
            if "QPushButton" in ln and not ln.lstrip().startswith("#")]
    assert code == [], (
        f"r45 已把日志页的按钮行撤走，页面里不该再 new QPushButton：{code}")


def test_hidden_stats_label_skips_compute(win):
    """R152-A7：stats_text 隐藏时 refresh_stats 直接 return，一次都不真算。

    A7 原文：「stats 控件隐藏即跳过计算；顺带 refresh_stats 在
    setHidden(True) 时直接 return」—— 控件按需求 21 撤出界面后，
    每 3s 的全表 SELECT + scandir 纯属空转卡顿。export_report 自己
    现算 library_stats，不依赖这条路，所以隐藏闸连 force 也一并挡。
    """
    _w, fake = win
    lbl = getattr(_w, "stats_text", None)
    if lbl is None:
        import pytest
        pytest.skip("该窗口没有 stats_text 控件，无从验隐藏闸")
    lbl.setVisible(False)
    assert lbl.isHidden(), "控件没真藏起来，这个用例就没在测 A7"
    _w.refresh_stats()
    assert fake.calls == 0, (
        f"控件藏着还真算了 {fake.calls} 次 —— R152-A7 的隐藏闸失效，"
        "大库上又是每 3s 一次全表 SELECT + scandir")
    _w.refresh_stats(force=True)
    assert fake.calls == 0, "force 也不能绕过隐藏闸（导出报告走另一条路）"
