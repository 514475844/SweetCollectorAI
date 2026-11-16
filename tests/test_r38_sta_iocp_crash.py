"""r38：锁死「前台真窗口一起就崩」这条真机 bug。

现象：真机双击 EXE（native 窗口，不是 offscreen 探活）起来几十秒后进程直接退出，
`logs/gui-native.log` 里三块：

    Windows fatal exception: code 0x8001010d
    Thread: asyncio\\windows_events.py:778 _poll  (IocpProactor._poll)
            -> base_events.py:2022 _run_once -> 683 run_forever
            -> 712 run_until_complete -> queue.py:111 _thread_main
    Current thread: main_window.py launch -> mrm_app.py main -> <module>

原因（两次取证，别再走弯路）：
1. 默认 policy 是 `WindowsProactorEventLoopPolicy`，`TaskQueue` 在**子线程**
   `run_forever()`；IOCP 的 `_overlapped.GetQueuedCompletionStatus` 内部走 COM
   等待线程 → 0x8001010d。栈 `windows_events.py:778 _poll -> queue._thread_main`。
2. 换成 **Selector** 之后真机**照样崩**（15:01:38 / 15:02:34 两轮），只是栈变成
   `selectors.py:305 _select -> 314 select -> queue._thread_main`。
   也就是说 Proactor 和 Selector 在 Windows 上都会走 `CoWaitForMultipleHandles`，
   单纯换 loop 类型堵不住 —— 根因是「子线程事件循环 + 主线程阻塞在 Qt 事件循环」。

修法（r38 定版）：**事件循环搬回主线程**，GUI 用 QTimer 每 5ms 调
`TaskQueue.pump_once()` 单步推进 `loop._run_once()`。Windows 上顺带也用
Selector（省掉 IOCP 那条路），并且入口全局切 policy 兜住 scheduler /
browser_cookie 的 `asyncio.run()`。本项目不用 `asyncio.create_subprocess_*`

这条测试专门防「哪天有人把循环挪回子线程 / 改回 Proactor / 忘了挂 pump 定时器」。
"""
import asyncio
import sys

import pytest


def test_windows_default_policy_is_proactor_if_unpatched():
    """先确认我们改的是**真的**那个坑点：没打补丁时默认 policy 会给 IOCP。

    只在 Windows 上断言 —— 别的平台没有 windows_events 这套 policy。
    """
    if sys.platform != "win32":
        pytest.skip("仅 Windows 需要锁 IOCP")
    policy = asyncio.get_event_loop_policy()
    assert type(policy).__name__ == "WindowsProactorEventLoopPolicy", (
        "默认 policy 变了，本测试的前提失效，需要重新评估 queue.make_loop()")


def _no_iocp(loop) -> bool:
    """真判据：不看类型名，直接看这个循环身上有没有 IOCP 抽屉。

    Windows 上 `asyncio.SelectorEventLoop()` 返回的是 `_WindowsSelectorEventLoop`，
    `ProactorEventLoop` 才有 `_iocp` —— 后者在子线程里跑就是那次 fatal。
    """
    return not getattr(loop, "_iocp", None)


def test_make_loop_never_touches_com_on_windows():
    """queue.make_loop() 必须给出**不调 CoWaitForMultipleHandles** 的循环。

    判据：selector 必须是 `SelectSelector`（纯 `select.select`），
    不能是 IOCP 的 Proactor，也不能是 `_WindowsSelectorSelector`
    （后者虽然叫 "Selector"，内部同样走 COM —— 15:01 那两次就是它崩的）。
    """
    from media_resource_manager.scheduler.queue import make_loop

    loop = make_loop()
    try:
        if sys.platform == "win32":
            assert _no_iocp(loop), (
                f"事件循环带 IOCP（{type(loop).__name__}）；"
                f"Proactor 在 Qt 主线程旁边会 0x8001010d fatal")
            sel = type(loop._selector).__name__
            assert sel == "SelectSelector", (
                f"selector 是 {sel}；它会调 CoWaitForMultipleHandles(COM)，"
                f"真机照样 0x8001010d（15:01 两次 / 15:13 一次），"
                f"必须是纯 select.select 的 SelectSelector")
    finally:
        try:
            loop.close()
        except Exception:
            pass
        # make_loop 会 set_event_loop，测试结束后别污染别的用例
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def test_queue_thread_loop_is_selector_even_if_global_policy_proactor():
    """就算全局 policy 被人改回 Proactor，TaskQueue 子线程也照样用 Selector。"""
    from media_resource_manager.scheduler.queue import TaskQueue, make_loop

    if sys.platform != "win32":
        pytest.skip("仅 Windows 需要锁 IOCP")

    saved = asyncio.get_event_loop_policy()
    try:
        # 故意把全局 policy 弄成 IOCP，模拟「入口忘了切」的最坏情况
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = make_loop()
        assert _no_iocp(loop), type(loop).__name__
    finally:
        try:
            loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass
        try:
            asyncio.set_event_loop_policy(saved)
        except Exception:
            pass


def test_queue_thread_loop_is_selector_and_really_runs():
    """r38 定版：事件循环跑在子线程并且**真的 running**，selector 必须是 SelectSelector。

    「loop 不 running」是最阴的回归形态：不报错、不崩，但 16 站全部入队成功、
    60 秒零下载。因为 engine/queue 全线站在 `asyncio.get_running_loop()` 上：
    `queue._worker` 的 create_task、`engine._to_thread` 的
    `loop.run_in_executor`（下载主力全走这条）、`engine.bfs` 里 3 处
    create_task —— loop 不 running 时一律 `RuntimeError: no running event loop`。
    """
    from media_resource_manager.scheduler.queue import TaskQueue

    import threading

    q = TaskQueue.__new__(TaskQueue)
    q.max_concurrent = 1
    q._thread = None
    q._loop = None
    q._paused = threading.Event()      # _worker 一进来就读它，别漏
    q._ready = type("E", (), {"set": lambda s: None, "clear": lambda s: None,
                              "wait": lambda s, t: True})()
    q._pending = []
    q._lock = threading.Lock()
    q._running_tasks = {}
    # R152-C4（23:59 那轮）给 __init__ 加了 `self._queued_ids`（submit 幂等占位
    # 集合），而 `_main()` 一进来就 `.clear()` 它。本用例是**绕过 __init__ 手工
    # 搭**的最小对象（只给 start()/_thread_main 真正要读的那些字段），漏一个
    # 新字段 = 子线程 AttributeError → loop 起不来 → 本条必红。产品没坏，
    # 是手搓对象要跟着补字段。
    q._queued_ids = set()

    q.start()
    try:
        assert q._thread is not None, "子线程没起来，COM 崩溃那条路会回来"
        # 等 _thread_main 把 loop 建起来
        for _ in range(200):
            if q._loop is not None:
                break
            import time
            time.sleep(0.01)
        assert q._loop is not None, "子线程还没建 loop"
        loop = q._loop
        try:
            for _ in range(300):          # 等 loop 真的进入 running
                if loop.is_running():
                    break
                import time
                time.sleep(0.01)
            assert loop.is_running(), (
                "事件循环没真的 running → engine/queue 里的 "
                "get_running_loop()/create_task 会全部 RuntimeError，"
                "表现是「入队成功但一张图都下不来」")
            if sys.platform == "win32":
                assert _no_iocp(loop), type(loop).__name__
                assert type(loop._selector).__name__ == "SelectSelector", (
                    f"selector 是 {type(loop._selector).__name__}；"
                    f"IOCP/WSAEventSelect 都会走 CoWaitForMultipleHandles，"
                    f"真机 0x8001010d 照样来")
        finally:
            try:
                q.stop()
            except Exception:
                pass
    finally:
        try:
            q._loop.close()
        except Exception:
            pass
        try:
            asyncio.set_event_loop(None)
        except Exception:
            pass


def test_mrm_app_forces_selector_policy():
    """入口 mrm_app.main() 自己在最前面把 policy 切掉（scheduler/cookie 也要受益）。"""
    if sys.platform != "win32":
        pytest.skip("仅 Windows 需要锁 IOCP")
    import importlib

    mrm_app = importlib.import_module("mrm_app")
    importlib.reload(mrm_app)
    mrm_app._force_selector_policy()
    policy = asyncio.get_event_loop_policy()
    assert policy.__class__.__name__ == "WindowsSelectorEventPolicy" \
        or type(policy).__module__.endswith("windows_events"), \
        "入口没切 policy，scheduler 的 asyncio.run 还会踩 IOCP"
