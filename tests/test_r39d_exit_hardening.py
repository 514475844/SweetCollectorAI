"""r39d：锁死「退出收尾」这几条回归。

真机现场（r39c 五轮都没根治的 0xC0000409）：

    进程退出码 3221226505 = 0xC0000409（STATUS_STACK_BUFFER_OVERRUN），
    gui-crash.log / gui-native.log 都是空的（C 层炸的，不是 Python 抛异常）。
    看门狗扒到的主线程栈：
        threading.join <- concurrent.futures.thread._python_exit
        <- threading._shutdown
    即解释器收尾正在 join 一个**还在跑**的后台 worker，那个 worker 收工时
    要碰已经清空的模块全局 / 已经关掉的库 → 整个进程被带走。

根因是三条，本文件逐条上锁：

1. `ThreadPoolExecutor.shutdown()` **没有 timeout 参数**
   （签名 `shutdown(wait=True, *, cancel_futures=False)`）。
   上一版收尾写的是 `bg.shutdown(wait=True, timeout=5)` → TypeError →
   被 `except Exception: pass` 吞掉 → **池子压根没关**。
2. 开着池子还不算，engine 每个任务 new 一个 `ThreadPoolExecutor(8)`，用完
   连 shutdown 都不调（`scheduler/queue.py::_run_one`），8 个 daemon 线程赖着
   不死，退出期全被 `_python_exit` 捞去 join。
3. 窗口上还挂着 8 个后台 QThread（去重 / 代理探测 / 修复图库 / 回填 / …），
   它们的 `run()` 直接读 `self.db`，退出流程却只等队列线程，不等它们。

修法见 `_join_threadpool` / `_wait_bg_qthreads` / `_run_one` 的 finally。
"""
import concurrent.futures
import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

MW_REL = "media_resource_manager/gui/main_window.py"


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _new_window(qapp):
    from media_resource_manager.gui.main_window import MainWindow

    win = MainWindow()
    win._ensure_single_instance = lambda *a, **k: True
    return win


# ---------------------------------------------------------------- 1. 有界等池子


def test_shutdown_has_no_timeout_kwarg_so_helper_is_mandatory():
    """事实上证：`shutdown(wait=True, timeout=…)` 一定会 TypeError。

    这条不是测主窗口，是把「为什么必须自己写 _join_threadpool」钉死 ——
    哪天有人把 timeout 传进去，这里立刻红。
    """
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    with pytest.raises(TypeError):
        pool.shutdown(wait=True, timeout=0.1)      # noqa: B018
    pool.shutdown(wait=True)


def test_join_threadpool_waits_for_slow_worker(qapp):
    """_join_threadpool 必须真的等 worker 干完（原来只是「调了个不存在参数」）。"""
    win = _new_window(qapp)
    try:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        barrier = threading.Event()
        pool.submit(lambda: (time.sleep(0.6), barrier.set()))

        t0 = time.monotonic()
        win._join_threadpool(pool, timeout=5.0)
        cost = time.monotonic() - t0

        assert barrier.is_set(), "worker 还没跑完就返回了"
        assert cost >= 0.5, f"看起来是没等（只花了 {cost:.3f}s）"
        assert not any(th.is_alive() for th in threading.enumerate()
                       if getattr(th, "name", "").startswith("cp_executor"))
    finally:
        win.close()


def test_join_threadpool_respects_budget_and_never_hangs(qapp):
    """超时预算要生效：工人赖着不走，也不能把关窗口卡死。"""
    win = _new_window(qapp)
    try:
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        done = threading.Event()

        def hang():
            done.wait(30)

        pool.submit(hang)
        t0 = time.monotonic()
        win._join_threadpool(pool, timeout=0.4)
        cost = time.monotonic() - t0
        assert 0.3 <= cost < 3.0, f"预算没生效，花了 {cost:.2f}s"
        done.set()
    finally:
        win.close()


def test_join_threadpool_tolerates_partial_pool(qapp):
    """没跑过任何任务的池子（_threads 为空）不能炸。"""
    win = _new_window(qapp)
    try:
        win._join_threadpool(concurrent.futures.ThreadPoolExecutor(max_workers=2),
                             timeout=0.2)
    finally:
        win.close()


# ------------------------------------------------------------------ 2. QThread


class _SlowThread(QThread):
    def __init__(self, ev):
        super().__init__()
        self.ev = ev

    def run(self):
        time.sleep(0.5)
        self.ev.set()


def test_wait_bg_qthreads_waits_until_finished(qapp):
    """挂在主窗口上的后台 QThread 必须等它收工，再谈关库。"""
    win = _new_window(qapp)
    ev = threading.Event()
    try:
        win._slow = _SlowThread(ev)
        win._slow.start()
        assert win._slow.isRunning()

        t0 = time.monotonic()
        win._wait_bg_qthreads(budget=5.0)
        cost = time.monotonic() - t0

        assert ev.is_set(), "worker 的 run() 还没跑完就放行了"
        assert not win._slow.isRunning()
        assert cost >= 0.45, f"看起来是没等（只花了 {cost:.3f}s）"
    finally:
        win._slow.wait(3000)
        win.close()


def test_wait_bg_qthreads_ignores_plain_attributes(qapp):
    """只认 QThread，别把普通属性（QTimer / 线程池 / 列表）也 wait 一遍。"""
    win = _new_window(qapp)
    try:
        win.not_a_thread = object()
        win.also_not = [1, 2, 3]
        win._wait_bg_qthreads(budget=1.0)     # 不该抛、也不该卡
    finally:
        win.close()


# ------------------------------------------------------- 3. engine 线程池不泄漏

def test_queue_run_one_shuts_engine_pool_down():
    """`_run_one` 收尾必须把 Engine 的线程池退掉。

    用源码断言（engine worker 是 queue 线程里 asyncio 的子协程，单测很难
    真跑一遍完整任务），但这条断言盯的是「引用留住了 + 收尾真的调 shutdown」。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    src = (root / "media_resource_manager/scheduler/queue.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)

    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_one":
            fn = node
            break
    assert fn is not None, "找不到 _run_one"

    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    # `eng.shutdown()` 是 ast.Attribute，得把 attr 名也收进来
    attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    assert "eng" in names, "_run_one 里得留一份 Engine 的引用（原来是匿名 new）"
    assert "shutdown" in attrs, (
        "收尾没有退 Engine 的线程池 —— 退出期解释器会 join 这些赖着不死的 worker")

    # 顺序也钉一下：必须「先退池子、再关库」，反过来 worker 会撞上已关的 conn
    src_lines = ast.get_source_segment(src, fn).splitlines()
    i_shut = next(i for i, l in enumerate(src_lines) if "eng.shutdown" in l)
    i_dbclose = next(i for i, l in enumerate(src_lines) if "db.close()" in l)
    assert i_shut < i_dbclose, "先关库再退池子 = worker 撞上已关的库"
