"""Task queue: priority ordering, concurrency control, failure queue + retry.

Runs its own asyncio loop in a background thread, so the GUI thread never
blocks. All state transitions are persisted into the `tasks` table, which
means a queue survives restarts (tasks left as 'queued'/'error' are visible
and re-runnable).
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import queue as _threadsafe_queue
import re
import threading
import time
from typing import Callable, Optional

LOG = logging.getLogger("mrm.queue")

# #8（生产测试报告 P1-1）部分失败也算「可重试」的失败率阈值。
# 旧判定只看 `downloaded == 0`：100 张里成 1 张、99 张 404 反而被标成 DONE，
# 不进 self.failed → 用户点「重试」没反应（那 99 张永远下不下来）。
# 与 _maybe_auto_throttle 的 0.6 刻意不同：那个是「暂停队列」的保守线（样本
# ≥10 才判），这个是「单任务标不可重试」的线，样本小也要拦住。
PARTIAL_FAIL_RATIO = 0.5

from ..config import AppConfig
from ..db import Database
from ..engine import Engine
from ..models import TaskStatus
from ..repo import get_task, list_tasks, save_task


def make_loop():
    """Windows 上必须绕开 **COM 等待**，一条路都不许留。

    r38 真机取证，三次崩溃三次不同的栈，结论：

    1. `asyncio.SelectorEventLoop()`（默认 policy 是 Proactor/IOCP）
       → `windows_events.py:778 _poll`，即 `_overlapped.GetQueuedCompletionStatus`，
         内部走 COM 等待线程。栈在子线程 `queue._thread_main`。
    2. 换成 Selector 之后照样崩（15:01:38 / 15:02:34 两轮），栈变成
       `selectors.py:305 _select`。
    3. 再把事件循环搬回主线程（Qt QTimer 每帧 pump）之后**还是崩**
       （15:13:53），这次 Current thread 直接落在 `main_window.launch`。

    也就是说：Windows 上 asyncio 的两套 selector（IOCP 的 Proactor、WSAEventSelect
    的 `_WindowsSelectorSelector`）**都会调 `CoWaitForMultipleHandles`**，只要旁边
    有 Qt 的 STA 主线程在跑事件循环/系统托盘消息，那个完成包就可能被派发回到错的
    apartment，`RPC_E_WRONGTHREAD (0x8001010d)` → CPython 没法转成普通异常，直接
    **fatal exception 带走整个进程**。离屏/后台探活一律看不出来。

    唯一不碰 COM 的是**纯 `select.select()`** 的 `selectors.SelectSelector`。
    本项目全链路不用 `asyncio.create_subprocess_*`（gallery-dl 快路径走同步
    `subprocess.run/Popen`）、也不搞 pipe，所以 SelectSelector 那点 Windows 短板
    （非 socket fd）根本用不到。
    """
    try:
        import selectors
        loop = asyncio.SelectorEventLoop(selectors.SelectSelector())
    except (AttributeError, NotImplementedError, RuntimeError):
        loop = asyncio.new_event_loop()      # 非 Windows / 兜底
    asyncio.set_event_loop(loop)
    return loop


def reset_stale_running(cfg: AppConfig) -> list:
    """On startup, any task left as 'running' is a leftover from a previous
    session — the app is NOT a daemon, so a closed process cannot still be
    crawling. Flip those back to 'idle' so the user can simply re-run them.

    V0.0.51：返回被重置的任务编号，交给「启动后自动恢复」按用户勾选的
    类别重新入队。（只重置不恢复 = 老行为，用户还得一条条手点「开始」。）
    """
    ids = []
    db = Database(cfg.db_path)
    try:
        rows = db.query("SELECT id FROM tasks WHERE status='running' "
                        "AND is_template=0")
        for r in rows:
            ids.append(int(r["id"]))
            db.execute(
                "UPDATE tasks SET status='idle', updated_at=datetime('now') "
                "WHERE id=?", (r["id"],))
            db.audit("queue", "reset-stale-running", str(r["id"]),
                     "was 'running' at startup -> idle")
    finally:
        db.close()
    return ids


# V0.0.34：队列事件 → 通知中心的白名单。只挑「用户不在场也该知道」的终态，
# running/queued/yielded 这类过程事件不落库（否则通知中心会被刷屏）。
_NOTIFY_EVENTS = frozenset({"finished", "error", "low_disk", "auto_throttle"})
_AUTH_HINT_RE = re.compile(
    r"\b(401|403|407|unauthorized|forbidden|cookie|login|log\s?in|"
    r"expired|session|auth)\b|需要登录|登录失效|登录过期|未授权|无权限|鉴权",
    re.IGNORECASE)


class TaskQueue:
    def __init__(self, cfg: AppConfig, max_concurrent: int = 2,
                 on_event: Optional[Callable] = None):
        self.cfg = cfg
        self.max_concurrent = max(1, max_concurrent)
        # 包装一层而不是替换调用形状：桌面端 _queue_event_emit 与 headless 的
        # 回调都按原参数收，*args/**kwargs 原样透传；通知记不上绝不影响业务。
        _user_cb = on_event or (lambda *a, **k: None)

        def _emit(*args, **kwargs):
            try:
                _user_cb(*args, **kwargs)
            except Exception:
                LOG.debug("on_event 回调异常", exc_info=True)
            try:
                ev = args[0] if args else kwargs.get("event", "")
                tid = args[1] if len(args) > 1 else None
                pay = args[2] if len(args) > 2 else None
                self._record_notification(ev, tid, pay)
            except Exception:
                pass

        self.on_event = _emit

        self._cmdq: _threadsafe_queue.Queue = _threadsafe_queue.Queue()
        self._lock = threading.Lock()
        self._paused = threading.Event()
        self.pause_reason = ""      # #54 暂停原因（磁盘不足等），UI 要显示
        self._stopping = False

        self.running: dict[int, float] = {}       # task_id -> started_at
        self._running_tasks: dict[int, asyncio.Task] = {}   # 可取消的任务句柄
        self.reports: dict[int, dict] = {}        # task_id -> last report dict
        self.failed: dict[int, str] = {}          # task_id -> reason

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # P2-2：`_own_loop` = **线程自己真正在跑的那个 loop**，与 `_loop` 分开。
        # 为什么要分：`_loop` 是对外的观察点，测试（test_r37 造「loop 已关闭 +
        # 队列线程还在」那个状态）和 `stop()` 都会改它/依赖它；一旦它被换成
        # 别的 loop，`stop()` 就再也够不到真正跑着的那个 → 线程永不停 →
        # 每个这样的用例都在全量里留下一条**活的 asyncio 线程 + selector**。
        # 几十条叠到解释器退出那一刻，就是 heap corruption `0xc0000374`。
        # 停线程只认 `_own_loop`，观察点随便改都不影响收尾。
        self._own_loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._seq = itertools.count()
        # submissions arriving before the loop exists must not be dropped
        self._ready = threading.Event()
        self._pending: list[tuple[int, Optional[int]]] = []
        # R152-C4：占位集合 —— 已入队（等跑）/正在跑的 task_id。
        # 只在事件循环线程上读写（_main flush / _do_submit / _worker），
        # 用于 submit 幂等：同一个任务不许被 put 两遍（双跑竞态）。
        self._queued_ids: set[int] = set()
        # #10：每个任务还剩几次自动重试（跑成功即清零）
        self._auto_left: dict[int, int] = {}
        # 编号 35：慢任务让位。`_prog` = task_id → (采样时刻, 页数, 图数)；
        # `_yielded` = 本轮**因让位而停**的任务（用来和"用户点停止"区分）。
        self._prog: dict[int, tuple[float, int, int]] = {}
        self._yielded: set[int] = set()
        self._yield_log: list[dict] = []

    # ---------------- 通知中心桥接（V0.0.34）----------------
    def _record_notification(self, event, task_id, payload):
        """把队列终态落进 notifications 表，关掉软件/关掉页面也能回头看。

        纪律与 notify.add 一致：全程吞异常，通知写不进去绝不反伤采集。
        这里用独立短连接而不是 _run_one 的 db —— 事件回调可能在 db.close()
        之后才触发，且通知表与任务表不该抢同一个事务。
        """
        if event not in _NOTIFY_EVENTS:
            return
        db = None
        try:
            from ..notify import add
            db = Database(self.cfg.db_path)
            try:
                tid = int(task_id or 0)
            except (TypeError, ValueError):
                tid = 0
            name = ""
            if tid:
                try:
                    t = get_task(db, tid)
                    name = str(getattr(t, "name", "") or "")[:60]
                except Exception:
                    pass
            label = name or (f"任务 #{tid}" if tid else "任务")
            text = str(payload or "")[:200]
            rep = payload if isinstance(payload, dict) else {}

            if event == "finished":
                got = int(rep.get("downloaded") or 0)
                secs = rep.get("elapsed")
                add(db, "task-finished", f"{label} 采集完成",
                    f"本轮下载 {got} 张" + (f" · 耗时 {secs} 秒" if secs else ""),
                    link="/tasks", level="info")
            elif event == "low_disk":
                add(db, "disk-low", "磁盘剩余空间不足，队列已暂停",
                    text or "请清理磁盘后再继续采集。",
                    link="/settings?tab=store", level="error")
            elif event == "auto_throttle":
                add(db, "auto-throttle", f"{label} 已自动降速",
                    text or "失败率过高，疑似被限流或需要登录。",
                    link="/tasks", level="warn")
            elif event == "error":
                add(db, "task-error", f"{label} 采集出现异常", text,
                    link="/tasks", level="warn")
                # 报错命中鉴权特征 ⇒ 单独给一条能采取行动的建议（Cookie 缺口之一）
                if _AUTH_HINT_RE.search(text):
                    add(db, "cookie-expired", f"{label} 疑似登录失效",
                        "报错含鉴权特征，建议检查该站点的 Cookie 是否过期。",
                        link="/settings?tab=store", level="warn")
        except Exception:
            pass
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    # ---------------- lifecycle ----------------
    def start(self):
        """启动队列：事件循环跑在自己的子线程里 `run_forever()`。

        r38 定版（推翻 r38 前半案的「主线程 pump」）：

        * **loop 必须真的 running**。engine/queue 全线站在
          `asyncio.get_running_loop()` 上干活 —— `queue._worker` 的
          `create_task(self._run_one(...))`、`engine._to_thread` 的
          `loop.run_in_executor`（下载主力全走这条）、`engine.bfs` 里 3 处
          `create_task`。「主线程 pump」那种每帧手动 `loop._run_once()` 的写法
          让 loop 永远不 running，调用方当场 `RuntimeError: no running event
          loop`；真机 soak 实测 16 站全部入队成功、60 秒零下载 —— 不崩，但
          完全不干活，比原来的 0x8001010d 更致命（用户只会以为软件坏了）。
        * **0x8001010d 靠 selector 解决，不靠搬线程**。IOCP
          （`windows_events.py:778 _poll`）和 WSAEventSelect
          （`selectors.py:305 _select`，也就是名字里带 Selector 的
          `_WindowsSelectorSelector`）在 Windows 上都会走
          `CoWaitForMultipleHandles` → 真机两轮照样 fatal。
          只有纯 `select.select()` 的 `selectors.SelectSelector` 不碰 COM，
          这才是 `make_loop()` 必须硬指定它的原因（见 make_loop 注释）。

        所以：子线程 `run_forever` + 纯 `SelectSelector`，Qt 侧完全不参与
        推循环。下载重活跑在 engine 自持的 `ThreadPoolExecutor(8)` 里，
        主线程只收信号，不会因为搬 loop 变卡。
        """
        if self._thread and self._thread.is_alive():
            return
        # A previous session may have died mid-run; never leave tasks stuck as
        # 'running' (the user explicitly complained about this).
        try:
            stale_ids = reset_stale_running(self.cfg)
        except Exception:
            stale_ids = []
        # V0.0.51：记下被中断的下载任务，等事件循环真的跑起来再恢复
        # （submit_many 在 loop 未就绪时会 buffer，但这里放到 start 末尾
        #  更稳）。用户没勾「启动后自动恢复」就保持老行为：任务回 idle。
        self._stale_ids = stale_ids
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        self._ready.wait(10)
        try:
            from ..startup_resume import restore_all
            restore_all(self.cfg, self, stale_ids)
        except Exception:
            pass

    def pump_once(self):
        """单步推进事件循环一轮。从 GUI 线程（Qt 定时器）调用，绝不阻塞。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            # 塞一个空回调：让 _run_once 走 timeout=0 的轮询分支，
            # 绝不能让 selector.select 在主线程Qt里阻塞等网络。
            loop.call_soon_threadsafe(lambda: None)
            loop._run_once()
        except Exception:
            pass

    def stop(self):
        self._stopping = True
        # Mark any in-flight tasks as interrupted so they don't appear stuck as
        # 'running' after the process exits.
        try:
            db = Database(self.cfg.db_path)
            db.execute("UPDATE tasks SET status='idle' WHERE status='running'")
            db.close()
        except Exception:
            pass
        # P2-2：向**线程自己的** loop 投停机指令（不是 `_loop`，见 __init__ 注释）。
        loop = self._own_loop or self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._shutdown)
            except RuntimeError:
                pass
        self._join_thread()

    def _join_thread(self, timeout: float = 5.0):
        """等队列线程真正退出（带超时，绝不无限等）。

        P2-2 根治点之二：以前 `stop()` 只投指令就返回，**从不 join**。
        线程还在跑 `run_until_complete` 时解释器就要退出了 → 它的 loop 在
        **另一个线程**上被析构 → 跨线程的 Qt/CPython 堆清理撞车 → 崩。
        超时后仍活着就强行 terminate：宁可留下一个僵尸线程，也不能让整个
        测试进程带 `0xc0000374` 走掉（崩了这份 junit 直接不生成）。
        """
        th = self._thread
        if th is None or th is threading.current_thread():
            return
        th.join(timeout)
        if th.is_alive():
            try:
                th.terminate()
            except Exception:
                pass
            th.join(1.0)
        self._thread = None

    def _shutdown(self):
        for t in asyncio.all_tasks(self._loop):
            if t is not asyncio.current_task():
                t.cancel()

    def _thread_main(self):
        # r38：Selector 而非 Proactor —— 见 make_loop 的注释（0x8001010d STA 崩溃）
        self._loop = make_loop()
        # P2-2：`_own_loop` 记线程真正跑的那个，`_loop` 是对外观察点。
        self._own_loop = self._loop
        self._ready.set()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            # P2-2 根治点之三：loop 必须**在创建它的这个线程里**关掉。
            # 以前这里只 cancel + close，若解释器抢先退出，close 会落到别的
            # 线程上 → selector 的 fd 与 Qt 堆在同一时刻被两个线程动 → 崩。
            # 顺序也是定的：先 cancel 并**等它们真的结束**，再
            # shutdown_asyncgens（异步生成器不在这里收尾就会在解释器退出时
            # 抛 "async generator ignored GeneratorExit"），最后 close。
            try:
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            finally:
                try:
                    self._loop.run_until_complete(
                        self._loop.shutdown_asyncgens())
                except Exception:
                    pass
                try:
                    self._loop.close()
                except Exception:
                    pass
                self._own_loop = None

    async def _main(self):
        self._pq: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._sem = asyncio.Semaphore(self.max_concurrent)
        # R152-C4：新一轮 loop 的队列是新的，清掉上一轮残留的占位 id，
        # 否则 stop→start（或 _rebuild）后那些任务会被幂等检查挡住再也投不进。
        self._queued_ids.clear()
        # R152-C4：list+clear 必须在锁内原子完成——旧写法「先 list 再逐条
        # await、最后 clear」会把 await 期间新到的缓冲条目直接清掉（丢任务）。
        with self._lock:
            early = list(self._pending)
            self._pending.clear()
        for task_id, prio in early:                   # flush early submits
            await self._do_submit(task_id, prio)
        workers = [asyncio.create_task(self._worker(i))
                   for i in range(self.max_concurrent)]
        # 编号 35：慢任务让位的看门狗（关着时这个协程只 sleep，不做任何查询）
        watchdog = asyncio.create_task(self._slow_yield_watchdog())
        try:
            await asyncio.gather(*workers)
        except asyncio.CancelledError:
            pass
        finally:
            watchdog.cancel()

    # NOTE: submissions go through run_coroutine_threadsafe rather than
    # run_in_executor(queue.get) - a blocking executor job left pending at exit
    # makes the interpreter hang forever joining the ThreadPoolExecutor.

    # ---------------- public (thread-safe) API ----------------
    def submit(self, task_id: int, priority: Optional[int] = None):
        if not self._loop:
            # loop not up yet (or not started): buffer, flushed by _main()
            with self._lock:
                self._pending.append((task_id, priority))
            self._ready.wait(2)
            if not self._loop:
                return
        try:
            asyncio.run_coroutine_threadsafe(
                self._do_submit(task_id, priority), self._loop)
        except RuntimeError:
            pass

    def start_task(self, task_id: int):
        """start_task = submit 的兼容别名（默认优先级）。

        2026-10-26：某次队列重构把入队方法统一成 submit，但这个别名被
        漏掉了——webui 的 _api_task_ctl/_api_task_reset 一直在调
        q.start_task()，真机上 AttributeError 被上层兜成 500 ⇒
        **Web 端「开始/全部开始/重试」从来是坏的**（test_r81 两条红
        的根因：测试桩 _Queue 也只认 start_task）。别名加回来，
        webui 与测试契约不变。
        """
        self.submit(task_id)

    def submit_many(self, items: list[tuple[int, Optional[int]]]):
        """Enqueue several tasks ATOMICALLY so priority order is respected.

        Submitting one-by-one can let a worker grab the first item before the
        higher-priority one arrives; a single coroutine drains all puts first.
        """
        if not self._loop:
            with self._lock:
                self._pending.extend(items)
            self._ready.wait(2)
            if not self._loop:
                return
        try:
            asyncio.run_coroutine_threadsafe(self._do_submit_many(items),
                                             self._loop)
        except RuntimeError:
            pass

    async def _do_submit_many(self, items):
        for task_id, prio in items:
            await self._do_submit(task_id, prio)

    async def _do_submit(self, task_id: int, priority: Optional[int] = None):
        # R152-C4：submit 幂等 —— 已在队列等跑 / 正在跑的同一任务不再二次入队。
        # 旧代码两条双投路：① loop 未起时 submit 进了 _pending，_ready.wait
        # 返回后又 run_coroutine_threadsafe 直投一次（flush+直投=双投）；
        # ② 用户连点两次「开始」/ retry_failed 与手动 submit 撞车。
        # 每条路最终都汇到这里，所以在入队口一次性去重。
        if task_id in self._queued_ids or task_id in self.running:
            return
        # r36：任务找不到就**明确记一条**，别静默 return。
        # Database(cfg.db_path) 对不存在的库会凭空建一个空库（比如外部把
        # MRM_HOME 指到 exe 所在目录而不是 data/ 目录），此时所有任务都找不到，
        # 点「开始」完全没反应、连一条日志都没有 —— 用户只会以为软件坏了。
        db = Database(self.cfg.db_path)
        t = get_task(db, task_id)
        db.close()
        if t is None:
            LOG.warning("⚠ 提交任务 #%s 找不到（cfg.db_path=%s 里没有这条任务），"
                        "已跳过", task_id, self.cfg.db_path)
            return
        p = priority if priority is not None else t.priority
        await self._pq.put((p, next(self._seq), task_id))
        self._queued_ids.add(task_id)    # R152-C4：占位，worker 跑完才释放
        self._set_status(task_id, TaskStatus.IDLE, f"queued (p{p})")
        # ⚠ 这行 on_event("queued") 曾被 F7 插入误吞（R170 全量里
        # test_r38 抓到：GUI 收不到 queued 事件）—— 护住它。
        self.on_event("queued", task_id, p)

    # ---------------- F7（R154）：按 URL 粒度重试失败网址 ----------------
    #
    # 与 submit() 的整任务重跑是两条路：这个只跑失败清单里的单条 URL，
    # 复用 engine._process_image（单 URL 下载+管线），不重新发现页面。
    def retry_failed_urls(self, task_id: int,
                          urls: Optional[list[str]] = None):
        """把「重试失败网址」投到队列线程的事件循环上执行。

        返回 concurrent.futures.Future（.result() 拿摘要 dict）；
        任务正在运行 / 队列没起来时返回 None（调用方给中文提示）。
        """
        if not self._loop:
            return None
        with self._lock:
            busy = task_id in self.running
        if busy:
            return None          # 同一个任务正在跑：别两个 Engine 叠在一处
        try:
            return asyncio.run_coroutine_threadsafe(
                self._do_retry_failed_urls(task_id, urls), self._loop)
        except RuntimeError:
            return None

    async def _do_retry_failed_urls(self, task_id: int, urls):
        # 占一个并发额度：重试也是真下载，不该绕过 max_concurrent 另起一队。
        sem = getattr(self, "_sem", None)
        if sem is not None:
            async with sem:
                return await self._retry_failed_urls_inner(task_id, urls)
        return await self._retry_failed_urls_inner(task_id, urls)

    async def _retry_failed_urls_inner(self, task_id: int, urls):
        db = Database(self.cfg.db_path)
        eng = None
        try:
            eng = Engine(self.cfg, db)
            result = await eng.retry_failed_urls(task_id, urls)
            self.on_event("retry-urls", task_id, result)
            return result
        finally:
            # 与 _run_one 同款收尾顺序：先关引擎线程池，再关库。
            if eng is not None:
                eng.shutdown()
            db.close()

    def retry_failed(self) -> int:
        """把失败任务重新入队，返回**真实入队条数**（#6 生产测试报告）。

        旧实现是 `self._pq.put_nowait(...)`：`_pq` 只在 `_main()` 里首次创建
        （asyncio.PriorityQueue），事件循环没起来时**属性根本不存在** → 每次
        抛 AttributeError 却被 `except Exception: pass` 吞掉 → 点「重试」完全
        没反应；更糟的是结尾还 `self.failed.clear()`，UI 上失败标记消失，
        用户以为重试成功，任务其实永远不会重跑（比不修更糟）。

        现在统一走线程安全的 `submit()`（loop 未起时进 `_pending`，
        由 `_main()` flush），并且不静默吞异常。
        """
        tids = list(self.failed)
        if not tids:
            return 0
        enqueued: list[int] = []
        try:
            # 一次批量入队（原子、保优先级序）。别逐条 submit()：loop 未起时
            # 每条都要 _ready.wait(2)，失败任务一多就按 N×2 秒线性卡住 UI。
            self.submit_many([(tid, None) for tid in tids])
            enqueued = list(tids)
        except Exception as e:
            LOG.warning("重试入队失败（%s 条）: %s", len(tids), e)
        for tid in enqueued:
            self._set_status(tid, TaskStatus.IDLE, "重试已入队")
        # 只清掉真正入队的那些。留在 self.failed 里的必须能再次重试。
        for tid in enqueued:
            self.failed.pop(tid, None)
        return len(enqueued)

    def stop_task(self, task_id: int) -> bool:
        """真正停止一个正在运行的任务（取消爬取/下载，状态记为 stopped）。

        暂停只是不让队列取新任务；停止才是把正在跑的这个掐掉。
        返回 False 表示该任务当前并没有在运行。
        """
        with self._lock:
            t = self._running_tasks.get(task_id)
        if t is not None and not t.done():
            t.cancel()
            return True
        return False

    def stop_all(self):
        with self._lock:
            handles = list(self._running_tasks.values())
        for t in handles:
            if not t.done():
                t.cancel()

    def pause(self, reason: str = ""):
        self._paused.set()
        if reason:
            with self._lock:
                self.pause_reason = reason
        self.on_event("paused", None, reason or None)

    def resume(self):
        self._paused.clear()
        with self._lock:
            self.pause_reason = ""
        self.on_event("resumed")

    def set_concurrency(self, n: int):
        """Restart with a new worker count (cheap + avoids semaphore churn).

        r37：这里以前只判过 `not self._loop`。主窗口关库后事件循环会 close，
        此时从 GUI 线程再改「同时运行任务数」就会在 call_soon_threadsafe 里
        抛 ``RuntimeError: Event loop is closed`` —— 实测刷三连异常（gui-crash
        12:12:30），现象就是「软件不能正常使用」。现在三道闸：loop 不存在、
        loop 已关闭、调度本身抛错，全都静默吞掉，配置照存。
        """
        self.max_concurrent = max(1, int(n))
        if not self._loop:
            return
        try:
            if self._loop.is_closed():
                return
            self._loop.call_soon_threadsafe(self._rebuild)
        except RuntimeError:
            # 关库/关窗口竞态：重建排不上就算了，别把 GUI 线程炸掉。
            pass
        except Exception:
            pass

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": dict(self.running),
                "failed": dict(self.failed),
                "paused": self._paused.is_set(),
                "pause_reason": self.pause_reason,
                "concurrency": self.max_concurrent,
                "reports": dict(self.reports),
                # 编号 35：最近让位记录（界面"为什么它停了"的解释）
                "yields": list(self._yield_log[-10:]),
            }

    def queued(self) -> list[tuple[int, int]]:
        """Best-effort view of pending items (priority, task_id)."""
        try:
            items = sorted(self._pq._queue)  # internal but read-only peek
            return [(p, tid) for (p, _, tid) in items]
        except Exception:
            return []

    # ---------------- internals ----------------
    def _rebuild(self):
        for t in asyncio.all_tasks(self._loop):
            if t is not asyncio.current_task():
                t.cancel()
        self._loop.create_task(self._main())

    async def _worker(self, wid: int):
        while True:
            while self._paused.is_set():
                await asyncio.sleep(0.5)
            try:
                _p, _s, task_id = await self._pq.get()
            except asyncio.CancelledError:
                return
            async with self._sem:
                # 单独建 task 以便 stop_task() 能精确取消这一个运行
                runner = asyncio.create_task(self._run_one(task_id))
                with self._lock:
                    self._running_tasks[task_id] = runner
                try:
                    await runner
                except asyncio.CancelledError:
                    pass          # stop_task() 或退出时取消，属正常流程
                finally:
                    with self._lock:
                        self._running_tasks.pop(task_id, None)
                    # R152-C4：跑完（含取消/异常）才释放占位 —— 出队瞬间就
                    # 释放的话，「出队→running 登记」之间的空窗里重复 submit
                    # 会漏过去造成双跑；占住整个运行期，重跑由 running 挡。
                    self._queued_ids.discard(task_id)

    async def _run_one(self, task_id: int):
        # 编号 6：任务开始即申请"保持唤醒"，结束（含异常/取消）在 finally 撤销。
        # 放这里而不是 GUI：Web 与 headless 跑长任务时同样怕系统睡着，
        # 而它们根本不经过 GUI。
        try:
            from ..syspower import prevent_sleep
            _blk = prevent_sleep(self.cfg)
            if _blk.start():
                if not hasattr(self, "_sleep_blockers"):
                    self._sleep_blockers = {}
                self._sleep_blockers[task_id] = _blk
        except Exception:
            pass
        self._set_status(task_id, TaskStatus.RUNNING, "")
        with self._lock:
            self.running[task_id] = time.time()
        self.on_event("running", task_id, None)
        db = Database(self.cfg.db_path)
        # r39d：Engine 自持一个 ThreadPoolExecutor(8)（engine._to_thread 走它）。
        # 以前 eng 用完就被丢掉，池子却从没 shutdown —— 那 8 个 daemon 线程一直
        # 赖着不死，进程收尾时 concurrent.futures.thread._python_exit 要 join 它们，
        # 一旦有 worker 正好碰上「已被清空的模块全局 / 已关的库」就把整个进程带走
        # （实测 0xC0000409，gui-crash.log 一行都不写）。这里把引用留住，收尾显式关。
        eng = None
        try:
            eng = Engine(self.cfg, db)
            rep = await eng.run(task_id)
            with self._lock:
                self.reports[task_id] = {
                    "task_id": rep.task_id, "pages": rep.pages,
                    # F9（R154）：断点续采账 —— 本轮跳过的已抓页数
                    "pages_resumed": int(
                        getattr(rep, "pages_resumed", 0) or 0),
                    "images_found": rep.images_found,
                    "downloaded": rep.downloaded, "dedup": rep.dedup,
                    "blacklist_hit": rep.blacklist_hit, "failed": rep.failed,
                    "ai_filtered": rep.ai_filtered,
                    "elapsed": round(rep.elapsed, 2),
                    "errors": rep.errors[:5],
                    "low_disk": bool(getattr(rep, "low_disk", False)),
                }
                self.failed.pop(task_id, None)
            # #54 磁盘空间不足：立刻暂停整个队列（不再继续下载，防止写满磁盘）
            if getattr(rep, "low_disk", False):
                why = getattr(rep, "low_disk_detail", "") or "磁盘剩余空间不足"
                self.pause(reason=why)
                self.on_event("low_disk", task_id, why)
            # 关键修复：抓到了页面但一张都没下下来、且真有报错，才算真失败。
            # 旧逻辑还要求 pages==0，导致「页面有、图全 404」时任务被偷偷标成
            # DONE、UI 一直显示 0/0/0「扫描中」，用户永远看不到真实错误。
            #
            # P1-1：只判 downloaded==0 也不够 —— 100 张里成 1 张、99 张 404
            # 照样被标 DONE、不进 failed，用户点「重试」没反应，那 99 张永远
            # 下不下来。失败率过半就算硬失败，允许重试。
            _found = int(getattr(rep, "images_found", 0) or 0)
            _fail = int(getattr(rep, "failed", 0) or 0)
            # ⚠⚠ **分母是_found，不是 _found + _fail**（这是本轮 C1 抓到的真bug）。
            # engine 里 `images_found = len(seen)`，而 seen 是**发现**的图片；
            # 每一张被发现��图最终要么 downloaded、要么 failed、要么被去重/排除，
            # 所以 failed 是 found 的**子集**，不是并列的另一部分。
            # 原式 `_total = _found + _fail` 把子集又加了一遍 → 分母虚高，
            # 失败率被系统性低估：注释自己举的例子「100 张里 99 张 404」
            # 算出来是 99/199 = 49.7%，**差一点点够不到 50% 阈值**，
            # 于是照样标 DONE、不进 failed —— P1-1 想修的那个 case 根本没修掉。
            # 分母只兜一个下限 1，避免 0/0。
            _total = max(1, _found)
            _fail_ratio = min(1.0, _fail / _total) if _total else 0.0
            # gallery-dl 分支（engine 里另一条路径）不设 images_found，
            # 只累 downloaded/failed；这时用 downloaded+failed 兜底当分母，
            # 否则那条路径的失败率恒为 0、partial-fail 判定整体失效。
            if not _found:
                _total = max(1, int(getattr(rep, "downloaded", 0) or 0) + _fail)
                _fail_ratio = min(1.0, _fail / _total) if _total else 0.0
            hard_error = bool(rep.errors) and (
                rep.downloaded == 0 or _fail_ratio >= PARTIAL_FAIL_RATIO)
            # ⚠ R188（用户反馈"任务直接完成了"）：0 图绝不算「完成」。
            # gallery-dl 快路径站点被拦截/JS 挑战页/403 时，常干净退出、0 图、
            # rep.errors 为空；旧逻辑只判 `bool(rep.errors)` 才标 ERROR，于是
            # 0 图被标成 DONE（完成）→ 用户看到"任务直接完成"。现改：
            # 一轮跑下来"一张都没发现、一张都没下到"一律判 ERROR（失败），
            # 并登记进 self.failed 让「重试」可用，状态栏写明是没下到图而非完成。
            _no_result = (not _found and not rep.downloaded)
            if _fail and not hard_error and not _no_result:
                _note = f"完成但有 {_fail} 张失败（{_fail_ratio:.0%}）"
            elif _no_result:
                _note = ("未获取到任何结果（站点可能拒绝了抓取/返回了拦截页，"
                         "或该主题确实无图）"
                         if not rep.pages else
                         "抓到页面但没有发现任何图片（站点可能拦截了抓取，"
                         "或适配器选择器未命中）")
            else:
                # hard_error 已经保证 rep.errors 非空，这层三元是纯防御：
                # 判定逻辑以后若放宽（有失败就算），这里不会变成 IndexError。
                _note = ((rep.errors or [""])[0] if hard_error else "")[:200]
            # #10（2026-10-27 用户要求）：失败自动重试。
            # 只在"硬失败/零结果"时触发；用户手动停止/暂停不算（那种情况
            # 重试等于跟用户对着干）。每次等待翻倍，成功一次即清零。
            if (hard_error or _no_result):
                _want = int(getattr(self.cfg, "auto_retry", 0) or 0)
                _base = int(getattr(self.cfg, "auto_retry_delay", 60) or 60)
                _left = self._auto_left.get(task_id, _want)
                if _left > 0 and not self._paused.is_set():
                    self._auto_left[task_id] = _left - 1
                    _n = _want - _left + 1
                    _wait = max(5, _base * (2 ** (_n - 1)))
                    # ⚠ R188 的硬约束：0 图/硬失败**必须保持 ERROR**（用户要能
                    # 一眼看到"这个任务没成"）。自动重试只是附加动作，不能用
                    # IDLE 把红色藏起来 —— 那是把失败伪装成"在排队"。
                    self._set_status(
                        task_id, TaskStatus.ERROR,
                        f"{_note}｜{_wait} 秒后自动重试（第 {_n}/{_want} 次）")
                    LOG.warning("任务 #%s 失败，%s 秒后自动重试（第 %s/%s 次）：%s",
                                task_id, _wait, _n, _want, str(_note)[:120])
                    try:
                        asyncio.get_running_loop().create_task(
                            self._auto_retry_later(task_id, _wait))
                    except RuntimeError:
                        pass
                    # ⚠ R188：即使要自动重试，也必须**登记进 self.failed**，
                    # 否则「重试」菜单遍历 failed 时摸不到这个任务（实测整组挂）。
                    self.on_event("error", task_id, _note)
                    with self._lock:
                        self.failed[task_id] = _note or "未获取到任何结果"
                    return
                # 重试用完了：清计数，落正常失败状态
                self._auto_left.pop(task_id, None)
            else:
                # 这轮成功了：清自动重试计数 + 从失败清单摘掉（重试菜单不该留它）
                self._auto_left.pop(task_id, None)
                with self._lock:
                    self.failed.pop(task_id, None)
            self._set_status(
                task_id,
                TaskStatus.ERROR if (hard_error or _no_result) else TaskStatus.DONE,
                _note)
            if hard_error or _no_result:
                # surface the real reason to the UI（0 图也明确报错，不再伪装成完成）
                self.on_event("error", task_id, _note)
                # P1-1：失败任务必须登记进 self.failed，否则「重试」菜单遍历
                # failed 是空的 → 用户点重试永远没反应。0 图同样登记。
                with self._lock:
                    self.failed[task_id] = _note or (rep.errors[0] if rep.errors
                                                     else "未获取到任何结果")
            # r38：失败原因必须**落到日志里**。engine 把原因塞进 rep.errors 就
            # return，自己一句 log 不打；而 rep.errors 又曾经根本不落库
            # （save_task 的字段表里没有 last_error）→ 用户点「全部开始」后
            # 16 站齐刷刷变 error、UI 底部日志干干净净，只能干瞪眼猜。
            # 这里统一兜底打一条，UI 日志区（GuiSink）和 mrm.log 同时可见。
            for _e in getattr(rep, "errors", [])[:3]:
                if _e:
                    LOG.warning("任务 #%s 未产出图：%s", task_id, str(_e)[:200])
            # #66 任务健康度与自动降速：失败率过高（疑似被限流/需登录）自动暂停
            self._maybe_auto_throttle(task_id, rep, db)
            if rep.failed:
                # 部分失败（下了一些、另一些 404）：不打断，但在 UI 日志里
                # 显式带出失败数与样例错误，方便诊断，而不是静默吞掉。
                #
                # ⚠ errors **可能为空**：engine 的 0 字节文件分支只 `rep.failed += 1`
                # 不往 errors 里加东西（见 engine 里 gallery-dl 的 sz==0 处理）。
                # 这里原来直接 `rep.errors[0]` → IndexError，而它被上面那个
                # 宽except Exception 吞掉后，症状是「任务莫名变成 error，
                # 失败原因写着 IndexError: list index out of range」——
                # 用户完全看不懂，且真正的失败原因被吃掉了。
                self.on_event(
                    "error", task_id,
                    f"部分下载失败 {rep.failed} 张"
                    + (f": {rep.errors[0]}" if rep.errors else ""))
            self.on_event("finished", task_id,
                          self.reports.get(task_id))
        except asyncio.CancelledError:
            if task_id in self._yielded:
                # 编号 35：这是"让位"而不是用户停止 —— 状态回 idle 并重新
                # 排队（断点续采的账还在 task_pages 里，下次接着爬）。
                self._yielded.discard(task_id)
                self._set_status(
                    task_id, TaskStatus.IDLE,
                    "已让位给其它任务（长时间无进展），稍后自动继续")
                self.on_event("yielded", task_id,
                              "已让位给排队的任务，稍后自动继续")
                try:
                    asyncio.get_running_loop().call_later(
                        1.5, lambda: self.submit(task_id))
                except RuntimeError:
                    pass
                self._clean_parts(task_id, db)
                raise
            # 用户点了「停止」：状态记为 stopped（而不是永远卡在 running）
            self._set_status(task_id, TaskStatus.STOPPED, "手动停止")
            self.on_event("stopped", task_id, None)
            # 2026-10-27（编号 11）：停止时清 .part 残留。
            # 只清 .part 类临时文件，绝不碰已下完的图（见 cleanup 里的安全边界）。
            self._clean_parts(task_id, db)
            raise
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"[:200]
            with self._lock:
                self.failed[task_id] = reason
            self._set_status(task_id, TaskStatus.ERROR, reason)
            self.on_event("error", task_id, reason)
            self._clean_parts(task_id, db)
        finally:
            # 编号 6：撤掉"别睡"申请（与开头 start() 配对）。
            try:
                _blk = getattr(self, "_sleep_blockers", {}).pop(task_id, None)
                if _blk is not None:
                    _blk.stop()
            except Exception:
                pass
            with self._lock:
                self.running.pop(task_id, None)
            # 编号 35：采样与"让位"标记跟着一轮跑完就清（下一轮重新采样）
            self._prog.pop(task_id, None)
            self._yielded.discard(task_id)
            # 先退池子（wait=False：当前这一项跑完 worker 就退出，别堵住事件循环），
            # 再关库。顺序反了会出现 worker 正用着 conn 我们把库关了。
            if eng is not None:
                try:
                    eng.shutdown()
                except Exception:
                    pass
            db.close()

    # ---------------- 编号 35：慢任务自动让位 ----------------
    #
    # 场景（用户原话）："前面的任务卡在某个被墙的站上一点点挪，后面排队的
    # 任务干着急"。qBittorrent 的等价物是队列轮转；我们这里做成**让位**：
    # 正在跑的任务连续 N 分钟既没新页面也没新图片，就把并发位让给后面的。
    #
    # 三条安全边界（少一条就会变成"好好的任务被莫名停掉"）：
    #   ① 只在**真有人排队**时才动手（`_pq` 非空 / 有任务在等并发位）；
    #   ② 每个任务**只让一次**，再次跑起来就以它为主（否则会无限让位循环）；
    #   ③ 让位不是失败：状态回 `idle` 并**重新入队**，断点账（task_pages）
    #      还在，下次接着爬 —— 绝不当成"用户停止"或"出错"。

    def _progress_of(self, db: Database, task_id: int) -> tuple[int, int]:
        """(已完成页数, 已有图片数)。两个都为 0 时也算"没进展"。"""
        pages = imgs = 0
        try:
            r = db.query("SELECT COUNT(*) AS n FROM task_pages "
                         "WHERE task_id=? AND status='done'", (task_id,))
            pages = int(r[0]["n"] or 0) if r else 0
        except Exception:
            pass
        try:
            r = db.query("SELECT COUNT(*) AS n FROM images WHERE task_id=?",
                         (task_id,))
            imgs = int(r[0]["n"] or 0) if r else 0
        except Exception:
            pass
        return pages, imgs

    async def _slow_yield_watchdog(self):
        """每 60 秒采一次样；只有 cfg.slow_yield_enabled 才真正干活。"""
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
            try:
                if not bool(getattr(self.cfg, "slow_yield_enabled", False)):
                    self._prog.clear()
                    continue
                if self._paused.is_set() or self._stopping:
                    continue
                # 只有在并发位被占满、还有任务在等的时候才考虑让位
                waiting = self._pq.qsize() if self._pq is not None else 0
                if waiting <= 0:
                    self._prog.clear()
                    continue
                need_min = int(getattr(self.cfg, "slow_yield_min", 10) or 10)
                need_min = max(1, need_min)
                db = Database(self.cfg.db_path)
                now = time.monotonic()
                try:
                    for task_id in list(self.running.keys()):
                        if task_id in self._yielded:
                            continue
                        pages, imgs = self._progress_of(db, task_id)
                        prev = self._prog.get(task_id)
                        if prev is None:
                            self._prog[task_id] = (now, pages, imgs)
                            continue
                        t0, p0, i0 = prev
                        if (pages, imgs) != (p0, i0):
                            self._prog[task_id] = (now, pages, imgs)
                            continue
                        idle_min = (now - t0) / 60.0
                        started = self.running.get(task_id, 0) or 0
                        ran_min = ((time.time() - started) / 60.0) if started else 0
                        # 必须"跑了够久"且"静了够久"——刚起的任务还没热身
                        if idle_min >= need_min and ran_min >= need_min:
                            self._yield_one(task_id, idle_min, pages, imgs)
                finally:
                    try:
                        db.close()
                    except Exception:
                        pass
            except Exception as ex:                      # noqa: BLE001
                LOG.warning("慢任务让位检查异常（忽略，不影响任务）: %s", ex)

    def _yield_one(self, task_id: int, idle_min: float,
                   pages: int, imgs: int) -> None:
        """取消这个任务的运行句柄，并把它放回队尾（不是失败）。"""
        runner = self._running_tasks.get(task_id)
        if runner is None:
            return
        self._yielded.add(task_id)
        self._prog.pop(task_id, None)
        self._yield_log.append({
            "task_id": task_id, "idle_min": round(idle_min, 1),
            "pages": pages, "images": imgs,
            "at": time.strftime("%H:%M:%S"),
        })
        del self._yield_log[:-20]
        LOG.info("任务 #%s 已 %.0f 分钟无进展（页 %s / 图 %s），让位给排队的任务",
                 task_id, idle_min, pages, imgs)
        try:
            self.on_event("yielded", task_id,
                          f"已 {idle_min:.0f} 分钟无进展，先让位给排队的任务")
        except Exception:
            pass
        runner.cancel()

    async def _auto_retry_later(self, task_id: int, wait: float):
        """等 wait 秒后重新入队（#10）。用户手动停止/暂停时放弃这次重试。"""
        try:
            await asyncio.sleep(float(wait))
        except asyncio.CancelledError:
            return
        if self._paused.is_set():
            return
        with self._lock:
            if task_id in self.running:
                return
        LOG.info("自动重试任务 #%s（等待 %.0fs 结束）", task_id, wait)
        try:
            self.submit(task_id)
        except Exception:
            pass

    def _maybe_auto_throttle(self, task_id: int, rep, db: Database):
        """#66 失败率过高自动暂停。

        只下载到 0 张且失败率 ≥ 60%（样本够大才判）→ 多半是被站点限流、
        或缺 Cookie/登录态，继续跑只会大量失败浪费流量，直接暂停并提示。
        """
        failed = getattr(rep, "failed", 0) or 0
        # ⚠ 分母必须与 _run_one 同一口径（注：_run_one 里对同一类 bug 已修过，
        #   当时遗漏了这里）：
        #   · 自研路径 images_found = len(seen) 已**包含** failed（每张被发现的
        #     图最终要么 downloaded、要么 failed），所以分母就是 images_found，
        #     再 +failed 会让分母虚高、失败率被系统性低估 —— 100 张发现、
        #     60 张失败（真实 60%），`found+failed=160` 算成 37.5%，够不着
        #     自动暂停的 60% 线，暂停/降频功能整体失效。
        #   · gallery-dl 分支不设 images_found，只有 downloaded/failed，
        #     分母退化成 downloaded+failed。
        _found = int(getattr(rep, "images_found", 0) or 0)
        if _found:
            _total = max(1, _found)
        else:
            _total = max(1, int(getattr(rep, "downloaded", 0) or 0) + failed)
        if failed == 0 or _total < 10:
            return
        ratio = failed / _total
        # #11（2026-10-27 用户要求）：失败率高但还没到"该停"的程度 →
        # 自动**降频**（把该任务的 delay 调大），而不是硬跑或直接暂停。
        # 1.5 倍递增、封顶 10 秒；成功率恢复后用户可手动调回。
        if ratio >= 0.3 and bool(getattr(self.cfg, "auto_slowdown", True)):
            try:
                from ..repo import get_task as _get_task, save_task as _save_task
                _t = _get_task(db, task_id)
                if _t is not None:
                    _old = float(_t.delay or 1.0)
                    _new = min(10.0, max(_old * 1.5, _old + 0.5))
                    if _new > _old + 0.01:
                        _t.delay = round(_new, 2)
                        _save_task(db, _t)
                        LOG.warning("任务 #%s 失败率 %.0f%%，已自动降频："
                                    "请求间隔 %.2fs → %.2fs",
                                    task_id, ratio * 100, _old, _t.delay)
            except Exception:
                pass
        if ratio >= 0.6:
            base = (f"任务 #{task_id} 失败率 {ratio:.0%} 过高"
                    f"（疑似被限流，或需要 Cookie/登录态）")
            # #21 cookie 失效提示：域名存有 Cookie 却仍大面积失败 → 多半已失效
            # ⚠ 这条提示必须同时出现在「暂停理由」和「只告警」两条文案里 ——
            # 它是整句话里唯一可操作的部分，只拼给暂停态等于白拼。
            hint = ""
            try:
                import json as _json
                from ..utils.url import domain_of
                from ..cookies import get_cookie
                t = get_task(db, task_id)
                entries = getattr(t, "entry_urls", None) or []
                if isinstance(entries, str):
                    entries = _json.loads(entries or "[]")
                dom = domain_of(entries[0]) if entries else ""
                if dom and get_cookie(dom):
                    hint = (f"；{dom} 已存 Cookie 仍大面积失败，Cookie 可能"
                            f"已失效，请到「维护 → 浏览器插件」重新取一次")
            except Exception:
                pass
            # R152-C9：默认**不**自动暂停 —— 把整条队列按住是重动作，
            # 一个站抽风就停掉用户排好的十几个任务不合适；默认给告警 +
            # 可操作建议，开关留给用户在设置/config 里自己决定。
            if not getattr(self.cfg, "auto_pause_on_failure", False):
                warn = (base + hint + "：队列继续运行，建议检查该站点；"
                        "开启「失败率过高自动暂停队列」后会自动停下")
                self.on_event("auto_throttle", task_id, warn)
                LOG.warning("%s", warn)
                return
            why = base + "，已自动暂停" + hint
            self.pause(reason=why)
            self.on_event("auto_throttle", task_id, why)

    def _clean_parts(self, task_id: int, db) -> None:
        """编号 11：清该任务的 .part 残留（失败/手动停止时调）。"""
        try:
            if not bool(getattr(self.cfg, "clean_part_on_cancel", True)):
                return
            from ..cleanup import clean_task_parts
            # ⚠ get_task 在 repo，不在 Database 上 —— 早年 `db.get_task()` 的
            # AttributeError 被下面 except 吞掉 ⇒ `clean_part_on_cancel` 开关
            # 形同虚设：停止/失败时 .part 临时文件从不清理，一直吃盘。
            row = get_task(db, task_id)
            out_dir = getattr(row, "output_dir", None) if row is not None else None
            res = clean_task_parts(self.cfg, out_dir)
            if res.get("files"):
                LOG.info("已清理任务 #%s 的临时文件：%s 个，释放 %.1f MB",
                         task_id, res["files"], res.get("bytes", 0) / 1024 / 1024)
        except Exception:
            pass                      # 清理是加分项，绝不能影响状态机

    def _set_status(self, task_id: int, status: TaskStatus, err: str):
        try:
            db = Database(self.cfg.db_path)
            t = get_task(db, task_id)
            if t:
                t.status = status
                t.last_error = err or ""
                if status in (TaskStatus.DONE, TaskStatus.ERROR,
                              TaskStatus.STOPPED):
                    t.last_run_at = time.strftime("%Y-%m-%d %H:%M:%S")
                save_task(db, t)
            db.close()
        except Exception:
            pass


def enqueue_auto_tasks(q: TaskQueue, cfg: AppConfig,
                       only_auto: bool = True) -> int:
    """Enqueue every task flagged auto_run (used by `serve`)."""
    db = Database(cfg.db_path)
    tasks = list_tasks(db)
    db.close()
    batch: list[tuple[int, Optional[int]]] = []
    for t in tasks:
        if only_auto and not t.auto_run:
            continue
        if t.status == TaskStatus.RUNNING:
            continue
        if t.id in q.failed:
            continue          # leave failures for explicit retry
        batch.append((t.id, t.priority))
    if batch:
        q.submit_many(batch)   # atomic -> priority order is actually honoured
    return len(batch)
