"""r35：库被关闭后，引擎 worker 必须提前收工（不再刷「closed database」）。

背景（用户贴日志诊断的真实故障）
--------------------------------
关窗口 → `closeEvent` 里 `self.db.close()`；但引擎内部用 `asyncio.create_task`
起的 worker 协程不在被 cancel 的范围里，它继续从队列里取图、下载、处理，
每张都撞 `sqlite3.ProgrammingError: Cannot operate on a closed database`，
实测一次刷了 280 条 ERROR、图还白下载了（没入库），进程吃了 1.4 GB 内存还挂着。

本文件锁两件事：
1. `Database.is_closed()` 语义（关闭有标记、可重复调用不抛）；
2. 库一旦被关，worker 立刻收工（不再继续处理），且 run 的 errors 里有说明。
"""
from types import SimpleNamespace

import asyncio

from pathlib import Path

from media_resource_manager.db import Database
from media_resource_manager.engine import Engine
from media_resource_manager.models import TaskConfig
from media_resource_manager.repo import save_task

import media_resource_manager.engine as engine_mod

# 走 BFS（适配器）路径而不是 gallery-dl 快路径：真实故障（刷 280 条
# closed database）就发生在这条路上。给一个假域名，gallery-dl 不认识它，
# 路由判定就会落到适配器分支。
_DEAD_HOST = "https://nosuch-zzz.example/a"
# parents[1] = 项目根（engine.py 在 <根>/media_resource_manager/ 里）
_EXAMPLE_RULE = (Path(engine_mod.__file__).resolve().parents[1]
                 / "examples" / "example-adapter.yaml")


class _NullFetcher:
    """什么都不干的 fetcher（只提供 run 收尾会 await 的 close）。"""

    async def close(self):
        return None


class _FakeDiscovery:
    """假发现器：每 0.15s 吐一张图，模拟「发现阶段长时间不结束」。

    这样 worker 会持续从队列取图；只要它不再继续处理，就说明早退逻辑生效。
    """

    def __init__(self, *args, **kwargs):
        self.on_page = kwargs.get("on_page")
        self.stop_evt = asyncio.Event()

    async def run(self, entry_urls):
        for i in range(30):
            if self.stop_evt.is_set():
                break
            await asyncio.sleep(0.15)
            self.on_page(SimpleNamespace(url=f"https://x.com/p{i}",
                                         image_urls=[f"https://img.com/{i}.jpg"]))
        self.stop_evt.set()


def test_db_is_closed_flag_is_idempotent(tmp_config):
    """关闭要有标记，且可重复调用（关窗口流程可能调两次）。"""
    db = Database(tmp_config.db_path)
    assert db.is_closed() is False
    db.close()
    assert db.is_closed() is True
    db.close()                       # 幂等：不再抛
    assert db.is_closed() is True


def test_engine_worker_gives_up_when_db_is_closed(tmp_config):
    """库被关 → worker 立刻收工，不再继续处理剩余图片（也不刷 Traceback）。

    R186：本用例的目的是「库关闭放弃」，不是合规 —— off 档直接跳过合规门，
    免得任务在门上就被拦、根本走不到要测的路径（auto_authorize 自动插行
    已随 R186 删除，老写法会卡在 compliance not cleared）。
    """
    tmp_config.compliance_level = "off"
    db = Database(tmp_config.db_path)
    tid = save_task(
        db,
        TaskConfig(id=None, name="r35", adapter_name="r35test",
                   entry_urls=[_DEAD_HOST]))

    calls = []

    async def fake_process(task, rule, dl, img, album, page, rep):
        calls.append(img)
        await asyncio.sleep(0.02)

    eng = Engine(tmp_config, db,
                 fetcher_factory=lambda cfg, proxy: _NullFetcher())
    eng._resolve_adapter = lambda name: _EXAMPLE_RULE
    eng._process_image = fake_process

    orig = engine_mod.Discovery
    engine_mod.Discovery = _FakeDiscovery
    try:
        async def main():
            task = asyncio.create_task(eng.run(tid))
            await asyncio.sleep(0.4)            # 让它跑一会儿（已经处理几张）
            db.close()                          # ← 模拟关窗口关库
            await asyncio.wait_for(task, timeout=10)
            return task.result()

        rep = asyncio.run(main())
    finally:
        engine_mod.Discovery = orig

    n = len(calls)
    # 库关掉之后 worker 不该再吞新图（发现的节奏是 0.15s 一张，0.4s 内约 3 张）
    assert n <= 4, f"库已关闭还在继续处理，又跑了 {n} 张（旧 BUG 会一直跑）"
    assert any("数据库已关闭" in e for e in rep.errors), f"errors={rep.errors}"


def test_engine_cancel_stops_workers_quickly(tmp_config):
    """点「停止」：顶层任务被 cancel 后，run 必须快速结束、不留僵尸 worker。

    旧版 worker 死等 `await queue.get()`，外层 cancel 到不了它 → 关窗口后它
    还继续跑图，撞已关的库刷屏。现在靠 `stopped` 事件 + 0.5s 轮询收工。
    """
    db = Database(tmp_config.db_path)
    tid = save_task(
        db,
        TaskConfig(id=None, name="r35-stop", adapter_name="r35test",
                   entry_urls=[_DEAD_HOST]))

    async def fake_process(task, rule, dl, img, album, page, rep):
        await asyncio.sleep(0.05)

    eng = Engine(tmp_config, db,
                 fetcher_factory=lambda cfg, proxy: _NullFetcher())
    eng._process_image = fake_process

    orig = engine_mod.Discovery
    engine_mod.Discovery = _FakeDiscovery
    try:
        async def main():
            task = asyncio.create_task(eng.run(tid))
            await asyncio.sleep(0.6)        # 让 worker 进入稳定取图节奏
            task.cancel()
            t0 = asyncio.get_event_loop().time()
            try:
                await task                  # 期望：几秒内被 cancel 掉
            except asyncio.CancelledError:
                pass
            return asyncio.get_event_loop().time() - t0

        elapsed = asyncio.run(main())
        assert elapsed < 3, f"停止后 {elapsed:.1f}s 才结束，worker 没真收工"
    finally:
        engine_mod.Discovery = orig
