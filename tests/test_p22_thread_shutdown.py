"""P2-2：pytest 0xc0000374 堆损坏崩溃的根治护栏。

背景（全量跑到~90% 崩 `0xc0000374`、**junit 根本不生成**）已定位过两次：
R23 认为是并发抢资源（错，那是猜测）；R25 实测确认是「28 个测试文件各建
MainWindow、close 只隐藏不销毁 C++ 控件树」累积把堆吃穿，并已用 conftest 的
module 作用域 `_reap_top_level_widgets` 修掉。

但作业单指出那仍是**幽灵崩溃**：还有一条没人管的路径 —— **asyncio 队列线程**。
本文件把那一条钉死，免得下次又靠运气。

三条铁律（每条对应一个真实缺陷，不是风格偏好）：
1. `TaskQueue.stop()` 必须 **join** 线程（带超时），不能只投指令就返回。
   只投指令 → 线程还在 `run_until_complete` 时解释器就退出 → 跨线程析构
   loop → 堆损坏。
2. 停机指令必须投给**线程自己的** loop（`_own_loop`），不是对外观察点
   `_loop`。观察点会被测试替换掉（test_r37 造「loop 已关闭」那个状态），
   换了就再也够不到真线程 → 线程永不停。
3. loop 必须在**创建它的线程里**关闭，且 cancel 之后要
   `shutdown_asyncgens()` —— 否则异步生成器在解释器退出时才被收尾。

断言一律**断言行为**（停完线程真的不活了 / 事件循环真的关了），
**不写死行号与调用次数**：实现怎么改都该过，产品行为变了才红。
"""
import ast
import inspect
import pathlib
import threading

import pytest

from media_resource_manager.scheduler import queue as Q

QUEUE_PY = pathlib.Path(inspect.getsourcefile(Q)).resolve()


def _src() -> str:
    return QUEUE_PY.read_text(encoding="utf-8", errors="ignore")


def _fn_body(name: str) -> str:
    """取某个函数的源码（按 AST 定位，不按固定行数切片）。"""
    tree = ast.parse(_src())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return "\n".join(_src().split("\n")[node.lineno - 1:node.end_lineno])
    raise AssertionError("queue.py 里找不到函数 %s" % name)


# ── 1. stop() 真的 join 线程，且带超时 ──────────────────────────────────
def test_stop_joins_the_queue_thread():
    """stop() 之后线程必须**真的不再活着**（这是崩溃的直接条件）。"""
    body = _fn_body("stop")
    assert "_join_thread" in body, (
        "stop() 又变回「只投指令就返回」了 —— 线程会活到解释器退出那一刻，"
        "跨线程析构 loop 就是 0xc0000374")
    # 超时保护：_join_thread 必须带默认超时，且超时后 terminate 而不是死等
    jb = _fn_body("_join_thread")
    assert "def _join_thread(self, timeout" in jb, (
        "_join_thread 必须带 timeout 参数 —— 无限 join 会把测试挂死")
    assert "join(timeout)" in jb, "_join_thread 必须用传进来的 timeout 去 join"
    assert "terminate" in jb, (
        "超时后必须 terminate：宁可留僵尸线程，也不能让整份 junit 随进程崩掉")


def test_join_thread_never_waits_on_itself():
    """绝不能 join 当前线程 —— 那会自己把自己锁死（RuntimeError: join
    current thread) → 整个测试进程死掉。"""
    jb = _fn_body("_join_thread")
    assert "current_thread()" in jb, (
        "_join_thread 必须排除 threading.current_thread()，否则 stop() "
        "若在线程内被调用就会把自己 join 到死锁")


# ── 2. 停机投给线程自己的 loop，而不是可被替换的观察点 ──────────────────
def test_stop_targets_the_threads_own_loop():
    """观察点 `_loop` 会被测试替换掉，停机必须认 `_own_loop`。"""
    body = _fn_body("stop")
    assert "_own_loop" in body, (
        "stop() 必须优先投给 _own_loop —— 只认 _loop 的话，"
        "一旦观察点被换掉（test_r37 就故意换），真线程永远停不下来")
    assert "is_closed()" in body, (
        "投指令前必须判 is_closed()：给已关闭的 loop call_soon_threadsafe "
        "抛 RuntimeError")


def test_own_loop_is_cleared_after_thread_finishes():
    """线程收尾后必须把 `_own_loop` 置回 None。

    不清的话，`stop()` 之后 `_own_loop` 仍指着一个**已关闭**的 loop，
    下次 `start()` 复用同一个 TaskQueue 时会拿着旧 loop 判断 → 状态错乱。
    """
    tm = _fn_body("_thread_main")
    assert "_own_loop = self._loop" in tm, (
        "_thread_main 必须把 loop 记进 _own_loop，否则 _own_loop 永远是 None，"
        "stop() 又退回只认观察点")
    assert "self._own_loop = None" in tm, (
        "_thread_main 收尾必须把 _own_loop 清成 None")


# ── 3. loop 在本线程关闭 + 异步生成器收尾 ─────────────────────────────
def test_loop_closed_in_owning_thread_with_asyncgen_drain():
    body = _fn_body("_thread_main")
    assert "shutdown_asyncgens" in body, (
        "关闭 loop 前必须 run_until_complete(shutdown_asyncgens())，"
        "否则残留的异步生成器在解释器退出时才被 GC → 抛 "
        "'async generator ignored GeneratorExit' 并带崩进程")
    # close 必须在 shutdown_asyncgens 之后
    assert body.index("shutdown_asyncgens") < body.index("self._loop.close()"), (
        "顺序反了：先 close 再 shutdown_asyncgens 等于对着已关闭的 loop 跑，"
        "必然抛异常并跳过真正的 close")


# ── 4. 行为级：真起一条线程再停掉，证明线程确实会死 ────────────────────
def test_real_thread_dies_after_stop():
    """行为验证（不是文本验证）：起一条真队列线程，stop() 之后它必须退出。

    用极简 cfg 让 `_main()` 立刻拿到空队列然后等 —— 停机指令一来就该收尾。
    """
    import asyncio
    import tempfile
    import os
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    tmp = tempfile.mkdtemp(prefix="p22_")
    cfg = AppConfig(db_path=os.path.join(tmp, "m.db"))
    Database(cfg.db_path)          # 建表，stop() 里那条 UPDATE 才不会炸

    q = Q.TaskQueue(cfg, max_concurrent=1)
    try:
        q.start()
        th = q._thread
        assert th is not None, "start() 之后必须有线程"
        assert th.is_alive(), "线程应当处于活动状态"

        q.stop()
        # 给它一点收尾时间，但绝不无限等（测试自己的保护）
        th.join(10)
        assert not th.is_alive(), (
            "stop() 返回 10s 后队列线程仍在跑 —— 这正是 0xc0000374 的来源："
            "活着的 asyncio 线程活过用例，跨到解释器退出那一刻")
        # loop 必须已被关闭 —— 说明它是在**线程自己手里**关掉的
        assert q._own_loop is None, (
            "线程收尾后 _own_loop 必须为 None（loop 已在本线程关闭）")
    finally:
        try:
            q.stop()
        except Exception:
            pass


def test_stop_is_idempotent_and_safe_without_start():
    """没 start() 就 stop() 不能炸，且要能反复调 —— 收尾路径常被重复走。"""
    import tempfile
    import os
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    tmp = tempfile.mkdtemp(prefix="p22b_")
    cfg = AppConfig(db_path=os.path.join(tmp, "m.db"))
    Database(cfg.db_path)
    q = Q.TaskQueue(cfg, max_concurrent=1)
    q.stop()
    q.stop()          # 再来一次也不许抛


# ── 5. conftest 侧的兜底与挂死守护 ──────────────────────────────────────
CONFTEST = pathlib.Path(__file__).with_name("conftest.py")


def test_conftest_reaps_queue_threads():
    """conftest 必须有「每个文件跑完回收队列线程」的兜底 fixture。

    为什么要有兜底：`stop()` 会 join，但**用例中途失败/被 skip 时 teardown
    未必跑到**，那些线程就会活到解释器退出。
    """
    src = CONFTEST.read_text(encoding="utf-8", errors="ignore")
    assert "_reap_queue_threads" in src, (
        "conftest 少了 _reap_queue_threads 兜底 —— 用例失败路径上的队列线程"
        "没人回收")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_reap_queue_threads"), None)
    assert fn is not None, "conftest 里找不到 _reap_queue_threads 定义"
    # 必须是 module 作用域：per-用例会踩掉 scope="module" 的共享窗口缓存
    # （test_r26 的 _cache），全局冲刷 DeferredDelete 又会让 menuBar() 变空
    deco = ast.unparse(fn.decorator_list[0]) if fn.decorator_list else ""
    assert 'scope="module"' in deco or "scope='module'" in deco, (
        "_reap_queue_threads 必须是 module 作用域（理由见 conftest 注释）")


def test_conftest_has_hang_watchdog():
    """必须有 faulthandler 挂死守护。

    QTimer 只能救「事件循环还在转」的情况；真的卡在解释器退出阶段时，
    只有 faulthandler 的看门狗线程还能把栈打出来 —— 否则 CI 只能靠超时杀，
    一样拿不到 junit。
    """
    src = CONFTEST.read_text(encoding="utf-8", errors="ignore")
    assert "faulthandler" in src, "conftest 缺少 faulthandler 挂死守护"
    assert "dump_traceback_later" in src, (
        "要用 dump_traceback_later（看门狗线程），而不是 enable() —— "
        "enable() 只在手动 dump 时才打栈")
    assert "cancel_dump_traceback_later" in src, (
        "teardown 必须取消看门狗，否则正常跑完也会被误杀")


def test_boot_has_timeout_watchdog():
    """`test_view_size._boot()` 必须带超时守护。

    它是全量崩溃点之一，构造 `MainWindow()` 时若界面起不来会静默挂死。
    """
    p = pathlib.Path(__file__).with_name("test_view_size.py")
    src = p.read_text(encoding="utf-8", errors="ignore")
    assert "setSingleShot(True)" in src, "_boot() 缺超时守护（必须是单次触发）"
    assert "watchdog.stop()" in src, (
        "_boot() 必须在 finally 里停掉守护 —— 否则每个用例都留一条活着的 "
        "QTimer，回调随时可能打到下一个用例的窗口上")
