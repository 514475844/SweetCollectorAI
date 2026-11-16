"""r38：锁死「入队成功 ≠ 真的在跑」这条静默回归。

真机现场（soak3，前台真窗口，16 站全部建好任务）：
    BASE  tasks_status={'idle': 16} images=0 pic_files=793
    T+60s tasks={'idle': 16}        images=0 pic=793 (+0 new)

不报错、不崩、日志干净，就是一张图都下不来。坑在哪：

1. r38 前半案把事件循环搬进主线程、由 QTimer 每帧 `loop._run_once()` 手动推。
   这种写法下 loop **永远不处于 running 状态**，于是：
   * `queue._main()` 里的 `asyncio.create_task(self._worker(i))` →
     RuntimeError: no running event loop → worker 一个都建不出来；
   * `_do_submit()` 里 `await self._pq.put(...)`（_pq 压根没被初始化）
     → AttributeError，而提交走的是 `run_coroutine_threadsafe`，
     **异常被 future 静默吞掉**，连一条 warning 都没有；
   * `engine._to_thread` 里的 `asyncio.get_running_loop()` → 每条下载都炸。
2. 即使给 `_main` 补一个 `create_task` 也只是把它推到下一步，
   第 (1) 条里那批 create_task 依旧炸。

结论（r38 定版）：**GUI 必须走子线程 `run_forever()`**，0x8001010d 由
`make_loop()` 的纯 `select.select()` SelectSelector 解决，而不是靠搬线程。
本文件防的就是哪天有人又把 loop 搬回主线程 / 从 GUI 传 pump 进来。
"""
import asyncio
import inspect
import pathlib
import threading

import pytest


@pytest.fixture
def queued_task(tmp_path):
    """真库 + 真任务，让 _do_submit 能找到任务并真的入队。"""
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task

    cfg = AppConfig()
    cfg.db_path = tmp_path / "probe.db"
    db = Database(cfg.db_path)
    tid = save_task(db, TaskConfig(id=None, adapter_name="generic",
                                   name="probe-task"))
    db.close()
    return cfg, tid


def _stub_ready(q):
    """不跑 TaskQueue.__init__（那要真 cfg/Engine），只补 start/_worker 用到的字段。"""
    import itertools
    q.max_concurrent = 1
    q._thread = None
    q._loop = None
    q._paused = threading.Event()          # _worker 一进来就读它
    q._pending = []
    q._lock = threading.Lock()
    q._running_tasks = {}
    q._seq = itertools.count()
    q._ready = threading.Event()
    return q


def test_gui_must_not_pump_the_loop_from_qt():
    """GUI 侧不许再出现「start(pump=...)」或每帧推循环的 QTimer。

    这两个写法会让 loop 永远不 running → engine 全线 RuntimeError →
    「入队成功、零下载」这种静默故障，比崩了还难查。
    """
    from media_resource_manager.gui import main_window as mw

    src = pathlib.Path(inspect.getsourcefile(mw)).read_text(
        encoding="utf-8", errors="ignore")
    assert "start(pump=" not in src, (
        "main_window 又在往 TaskQueue.start() 传 pump —— 事件循环不 running，"
        "engine 的 get_running_loop()/create_task 会全部 RuntimeError")
    assert "_queue_pump" not in src, (
        "Qt 侧还在每帧 pump 事件循环，同上，会把 loop 卡成非 running")
    assert "_queue_timer" not in src, "r38 起的 pump QTimer 还在挂"


def test_thread_mode_boots_main_and_queue(queued_task):
    """子线程跑起来后：_main 挂上（_pq 有值）、worker 建出来、任务真能入队。"""
    from media_resource_manager.scheduler.queue import TaskQueue

    cfg, tid = queued_task
    events = []
    q = TaskQueue(cfg, max_concurrent=2,
                  on_event=lambda ev, *a: events.append(ev))
    q._main  # 存在性自检，防止后面改名
    q.start()
    try:
        import time
        for _ in range(300):
            if getattr(q, "_pq", None) is not None and q._loop.is_running():
                break
            time.sleep(0.01)
        assert getattr(q, "_pq", None) is not None, (
            "_main 没跑起来 → _pq 是 None → _do_submit 的 _pq.put 抛 "
            "AttributeError 还会被 future 静默吞掉，表现是零下载零日志")
        assert q._sem is not None, "Semaphore 没建，并发上限失效"
        assert q._loop.is_running(), "loop 没真的 running"
        # worker 协程真的建出来了（1 个 _main + max_concurrent 个 worker）
        alive = [t for t in asyncio.all_tasks(q._loop) if not t.done()]
        assert len(alive) >= 1 + q.max_concurrent, (
            f"只有 {len(alive)} 个存活协程，应该有 "
            f"{1 + q.max_concurrent} 个 —— worker 没建出来，任务会一直排队")
        # 真提交一个任务：既要进队（queued 事件），也要被 worker 真的取走开工
        q.submit_many([(tid, 0)])
        for _ in range(300):
            if "queued" in events:
                break
            time.sleep(0.01)
        assert "queued" in events, (
            f"submit_many 后没有任何事件（events={events}）—— 任务被静默吞了")
        for _ in range(500):
            if len(events) >= 2 or q.reports.get(tid):
                break
            time.sleep(0.01)
        assert len(events) >= 2 or q.reports.get(tid), (
            f"任务入队了但 worker 没把它取走（events={events} "
            f"reports={q.reports} queued={q.queued()} running={q.running}）")
    finally:
        try:
            q.stop()
        except Exception:
            pass
        loop = getattr(q, "_loop", None)
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def test_loop_not_running_is_the_silent_failure_mode():
    """把「loop 不 running」这条设计约束讲清楚并锁住它。

    这条是上面几条失败原因的总闸：只要 loop 不 running，
    `asyncio.create_task(coro)` 就 RuntimeError，而这类协程通常是从
    子线程经 `run_coroutine_threadsafe` 提交的 —— 没人 .result()，
    **异常永远不浮出来**，故障表现就是「软件点了开始但什么都没发生」。
    """
    loop = asyncio.new_event_loop()
    try:

        async def _never():
            await asyncio.sleep(0)

        with pytest.raises(RuntimeError):
            asyncio.create_task(_never())
        # 同样地，get_running_loop() 在非 running 的 loop 上直接炸
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
    finally:
        try:
            loop.close()
        except Exception:
            pass
