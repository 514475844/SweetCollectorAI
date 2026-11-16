"""C1 · P1-1「部分失败任务进 self.failed + UI 能读到失败数」的独立验证。

作业单：`.workbuddy/LEGACY_DEBT_ORDER.md` C1
「P1-1 代码（fail_ratio>=0.5 计入可重试 + UI 显示失败数）R26 轮已改，
台式机侧尚未独立复验。要求：在 tests/ 补一个针对 retry_failed / 完成判定
_fail_ratio 的单元测试（部分失败任务进 self.failed 且 UI 能读到失败数），
跑红→绿，把状态钉死为 ✅。」

**与 test_r59 的分工**：r59 那份是**源码文本断言**（AST 找函数体里有没有
`_fail_ratio` 字样），防的是「有人把这段代码删了」。本份是**行为断言** ——
真的跑一遍队列，喂一份「100 张里99 张 404」的报告，然后断言：
  ① 任务被判成 error（不是 DONE）；
  ② 它进了 ``self.failed``（这是 P1-1 的核心 —— 不进的话「重试」遍历空集，
     用户点重试永远没反应，#6 死路的另一半）；
  ③ UI 能读到失败数（``snapshot()["failed"]`` 有它、状态栏那行文案能显示）。

**为什么必须行为断言**：源码里写着 `self.failed[task_id] = _note` 只说明
代码存在，不说明它真被执行到 —— R26 那轮就出现过「hard_error 只改状态、
从不进 failed」的同类bug。文本断言对此完全无感。

⚠ **严禁 shutil.rmtree / rmtree**：会触发 safe-delete 守卫，把真失败和
守卫噪音混在一起（MEMORY 铁律8）。需要清理时用 os.unlink + os.rmdir。
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ══════════════════════════════════════════════════════════ 夹具
def _mk_queue(tmpdir):
    """造一个 TaskQueue（不启动事件循环）。"""
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager.scheduler.queue import TaskQueue

    cfg = AppConfig()
    cfg.db_path = os.path.join(tmpdir, "t.db")
    Database(cfg.db_path).close()
    return TaskQueue(cfg)


def _mk_task(q, name="任务"):
    """往库里插一条真任务，返回 task_id。

    ⚠ TaskQueue **没有 .db 属性**（它每次操作都按 cfg.db_path 现开现关，
    见 _set_status），所以这里自己开一个 Database 写，写完即关。
    """
    from media_resource_manager.db import Database
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    t = TaskConfig(id=None, name=name, adapter_name="generic-gallery",
                   entry_urls=["https://e.com/"], max_depth=1)
    db = Database(q.cfg.db_path)
    try:
        return save_task(db, t)
    finally:
        db.close()


class _StubEngine:
    """顶替 Engine，只为让 ``_run_one`` 拿到我们指定的 RunReport。

    直接调 ``_run_one``（而不是 submit/run）是为了不牵出事件循环与线程 ——
    P1-1 的判定逻辑全在这一个函数里，单独驱动它最省事也最稳。
    """

    def __init__(self, report):
        self._report = report

    async def run(self, task_id):
        from media_resource_manager.engine import RunReport
        rep = self._report
        if rep is None:
            rep = RunReport(task_id=task_id)
        else:
            rep.task_id = task_id
        return rep

    def shutdown(self):
        pass


def _drive(q, monkeypatch, report):
    """把 _run_one 跑一遍（引擎换成桩），返回跑完后的队列。"""
    import media_resource_manager.scheduler.queue as qmod
    monkeypatch.setattr(qmod, "Engine", lambda cfg, db: _StubEngine(report))
    tid = _mk_task(q)
    asyncio.run(q._run_one(tid))
    return tid


# ══════════════════════════════════════════════════════════ 1. _fail_ratio 判定
@pytest.mark.parametrize("found,failed,downloaded,errors,expect_error", [
    # (images_found, failed, downloaded, errors) -> 是否判硬失败
    (100, 99, 1, ["404 x99"], True),      # P1-1 的核心场景：99% 404
    (100, 50, 50, ["部分 404"], True),    # 恰好 50% → 硬失败（>= 阈值）
    (100, 49, 51, ["部分 404"], False),   # 49% → 不算硬失败
    (10, 1, 9, ["1 张 404"], False),      # 少量失败 → 完成但有备注
    (100, 100, 0, ["全 404"], True),      # 一张没下下来
    (0, 0, 0, [], True),                  # R188：0 图不再算完成 —— 必判 error
    (100, 99, 1, [], False),              # ⚠ 没有 errors 就不算硬失败
])
def test_c1_fail_ratio_decides_error_status(tmp_path, monkeypatch,
                                            found, failed, downloaded,
                                            errors, expect_error):
    """【验收】部分失败（>=50%）必须判成可重试的 error，而不是 DONE。"""
    from media_resource_manager.engine import RunReport
    from media_resource_manager.models import TaskStatus
    q = _mk_queue(str(tmp_path))
    rep = RunReport(task_id=0, images_found=found, failed=failed,
                    downloaded=downloaded, errors=list(errors))
    tid = _drive(q, monkeypatch, rep)

    from media_resource_manager.repo import get_task
    from media_resource_manager.db import Database as _D
    _db = _D(q.cfg.db_path)
    try:
        t = get_task(_db, tid)
    finally:
        _db.close()
    if expect_error:
        assert t.status is TaskStatus.ERROR, (
            f"found={found} failed={failed} downloaded={downloaded} "
            f"errors={errors} 应判 error，实际 {t.status}")
    else:
        assert t.status is TaskStatus.DONE, (
            f"同上组合应判 done，实际 {t.status}（note={t.last_error!r}）")


def test_c1_partial_failure_enters_failed_dict(tmp_path, monkeypatch):
    """【验收 · P1-1 核心】部分失败的任务必须登记进 ``self.failed``。

    不登记的后果：用户点「重试失败任务」，``retry_failed`` 遍历空的
    ``self.failed`` → 返回 0 → 界面提示「没有失败任务」，而那 99 张 404
    永远下不下来。这就是 #6 死路的另一半。
    """
    from media_resource_manager.engine import RunReport
    q = _mk_queue(str(tmp_path))
    rep = RunReport(task_id=0, images_found=100, failed=99, downloaded=1,
                    errors=["404 x99"])
    tid = _drive(q, monkeypatch, rep)
    assert tid in q.failed, (
        f"部分失败任务 #{tid} 没进 self.failed —— 重试菜单遍历不到它")
    assert q.failed[tid], "进 failed 了但原因是空字符串，UI 会显示空白"


def test_c1_minor_failure_not_in_failed_but_has_note(tmp_path, monkeypatch):
    """少量失败（<50%）**不该**进 failed（否则重试菜单被噪音淹没），
    但必须在 last_error 里留下「完成但有 N 张失败」供用户看到。"""
    from media_resource_manager.engine import RunReport
    from media_resource_manager.repo import get_task
    q = _mk_queue(str(tmp_path))
    rep = RunReport(task_id=0, images_found=10, failed=1, downloaded=9,
                    errors=["1 张 404"])
    tid = _drive(q, monkeypatch, rep)
    assert tid not in q.failed, "少量失败不该进重试列表"
    from media_resource_manager.db import Database as _D
    _db = _D(q.cfg.db_path)
    try:
        t = get_task(_db, tid)
    finally:
        _db.close()
    assert t.last_error, "少量失败也该在 last_error 留一句（用户要看见）"
    assert "1" in t.last_error


# ══════════════════════════════════════════════════════════ 2. UI 能读到失败数
def test_c1_snapshot_exposes_failed_for_ui(tmp_path, monkeypatch):
    """【验收】UI 的失败数来自 ``snapshot()["failed"]``，这里必须能读到。"""
    from media_resource_manager.engine import RunReport
    q = _mk_queue(str(tmp_path))
    rep = RunReport(task_id=0, images_found=100, failed=99, downloaded=1,
                    errors=["404 x99"])
    tid = _drive(q, monkeypatch, rep)
    snap = q.snapshot()
    assert tid in snap["failed"], (
        f"snapshot()['failed'] 里看不到 #{tid} —— 状态栏/统计面板读不到失败数")
    # 状态栏那行文案就是这个 len()
    assert len(snap["failed"]) == 1


def test_c1_failed_count_clears_after_success(tmp_path, monkeypatch):
    """重跑成功后必须从 failed 里移除（否则失败数只增不减）。"""
    from media_resource_manager.engine import RunReport
    q = _mk_queue(str(tmp_path))
    tid = _drive(q, monkeypatch,
                 RunReport(task_id=0, images_found=100, failed=99,
                           downloaded=1, errors=["404 x99"]))
    assert tid in q.failed
    # 再跑一遍，这次成功
    import media_resource_manager.scheduler.queue as qmod
    monkeypatch.setattr(qmod, "Engine", lambda cfg, db: _StubEngine(
        RunReport(task_id=0, images_found=10, failed=0, downloaded=10,
                  errors=[])))
    asyncio.run(q._run_one(tid))
    assert tid not in q.failed, "成功重跑后仍在 failed 里 → 失败数只增不减"
    assert tid not in q.snapshot()["failed"]


def test_c1_retry_failed_picks_up_partial_failures(tmp_path, monkeypatch):
    """端到端：部分失败 → 可被 retry_failed 捞起来重排。

    这是 C1 要求的最后一环：前两环只证「进了 failed」，这环证「重试真的
    能捞到它」。之前r59 那份测的是「空 failed 时返回 0」，没覆盖这条。
    """
    from media_resource_manager.engine import RunReport
    q = _mk_queue(str(tmp_path))
    rep = RunReport(task_id=0, images_found=100, failed=99, downloaded=1,
                    errors=["404 x99"])
    tid = _drive(q, monkeypatch, rep)
    assert tid in q.failed

    # submit_many 是公开 API；这里只验「它被收集进待重试集合」这一层，
    # 不真启动 worker（那会把测试拖进事件循环与线程）。
    collected = []

    def _fake_submit_many(items):
        collected.extend(items)
        return [tid for tid, _ in items]

    monkeypatch.setattr(q, "submit_many", _fake_submit_many)
    q.failed = {tid: "x"}
    n = q.retry_failed()
    assert n == 1, f"retry_failed 应捞到 1 个，实际 {n}"
    assert collected and collected[0][0] == tid, "重试的不是那个失败任务"
    assert tid not in q.failed, "已入队的任务应从 failed 移除"


# ══════════════════════════════════════════════════════════ 3. 护栏
def test_c1_partial_fail_ratio_is_module_constant():
    """阈值必须是模块级常量（多处引用，改一处漏一处会让判定自相矛盾）。"""
    from media_resource_manager.scheduler import queue as qmod
    assert hasattr(qmod, "PARTIAL_FAIL_RATIO")
    v = qmod.PARTIAL_FAIL_RATIO
    # 断言关系不断言数值本身：0.5 是业务决定，产品调整阈值时这条不该红
    assert 0 < v <= 1, "阈值应是 0~1 的比例"


def test_c1_hard_error_branch_registers_failed():
    """静态护栏：hard_error 分支里必须有写 self.failed 的那一行。

    这条防的是「有人重构时把P1-1 那行删掉」—— 行为测试能抓到，但会等到
    跑完整条队列才红；这条立刻红。
    """
    import ast
    import inspect
    src = inspect.getsource(
        __import__("media_resource_manager.scheduler.queue",
                   fromlist=["x"]))
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_one"]
    assert fn, "找不到 _run_one"
    body = ast.unparse(fn[0])
    assert "hard_error" in body
    assert "self.failed[task_id]" in body, (
        "_run_one 里没有写 self.failed[task_id] —— P1-1 被回退了")