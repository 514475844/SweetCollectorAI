"""R152-C4（同任务双跑竞态）+ R152-B2（批量撤销学习权重残留）行为护栏。

C4 作业单原文：scheduler/queue.py submit 在 loop 未起时把任务存 _pending，
_ready.wait(2) 后又**无条件** run_coroutine_threadsafe 直投一次 ⇒ _main flush
一遍 + 直投一遍 = 同一任务 put 两次（双跑）；另外 `_do_submit` 对重复 submit
毫无防御，用户连点两次「开始」也会 put 两遍。修法：submit 幂等（task_id 去重）。
订单要求「先补测试再修」——本文件 1/2/3 号测试在修复前的源码上必须红。

B2 作业单原文：review/queue.py apply_batch 每项快照在**前项学习之后**才取
（后项 snap["prefs"] 含前项学习结果），undo() 正序恢复 ⇒ 最后落笔的是后项
恢复，批前已存在的词被留在「前项学习后」的值上（残留）；且 _undo_one 只回滚
weight/seen，不回滚 hit_pos/hit_neg。修法：批量条目倒序恢复 + 快照/回滚扩到
四字段。（探针实证：批内**新建**词在正序下被 DELETE+UPDATE落空碰巧清掉，
复现必须用批前已存在的词。）
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from media_resource_manager.db import Database
from media_resource_manager.models import ImageRecord, ReviewDecision
from media_resource_manager.repo import set_preference, upsert_image


# ══════════════════════════════════════════════ 公共小工具
def _wait_until(pred, timeout=8.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def _pref_row(db, label):
    rows = db.query(
        "SELECT weight, seen, hit_pos, hit_neg FROM preferences "
        "WHERE task_id=0 AND label=?", (label,))
    return rows[0] if rows else None


def _mk_image(db, path, task_id=1, tags=None, quarantine=True):
    rec = ImageRecord(
        id=None, source_url=f"https://x.com/{path.name}", task_id=task_id,
        path=str(path), tags=list(tags or ["cat"]),
        in_quarantine=quarantine, needs_review=True)
    return upsert_image(db, rec)


def _mk_file(tmp_path, name="a.jpg"):
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    return p


# ══════════════════════════════════════════════ C4 · scheduler/queue.py
def _mk_queue(cfg, max_concurrent=1, on_event=None):
    from media_resource_manager.scheduler.queue import TaskQueue
    return TaskQueue(cfg, max_concurrent=max_concurrent, on_event=on_event)


def _mk_task(q, name="任务"):
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    t = TaskConfig(id=None, name=name, adapter_name="generic-gallery",
                   entry_urls=["https://e.com/"], max_depth=1)
    db = Database(q.cfg.db_path)
    try:
        return save_task(db, t)
    finally:
        db.close()


class _GateEngine:
    """run() 挂起直到放行，用来把任务钉在 running 态。"""

    def __init__(self, ev):
        self._ev = ev

    async def run(self, task_id):
        from media_resource_manager.engine import RunReport
        while not self._ev.is_set():
            await asyncio.sleep(0.02)
        return RunReport(task_id=task_id)

    def shutdown(self):
        pass


class _FastEngine:
    def __init__(self, ev=None):
        pass

    async def run(self, task_id):
        from media_resource_manager.engine import RunReport
        return RunReport(task_id=task_id)

    def shutdown(self):
        pass


def test_c4_duplicate_submit_enqueued_once(tmp_config):
    """同任务连投两次 → 队列里只允许出现一条（修复前 put 两遍）。"""
    import media_resource_manager.scheduler.queue as qmod
    events = []
    q = _mk_queue(tmp_config, on_event=lambda k, a, b: events.append((k, a)))
    tid = _mk_task(q)
    q.pause()                    # worker 不出队，条目留在 _pq 里好观察
    q.start()
    try:
        assert _wait_until(lambda: q._loop is not None and q.queued() == [])
        q.submit(tid, 1)
        q.submit(tid, 1)         # ← 双投：flush/直投或用户连点
        assert _wait_until(lambda: len(q.queued()) >= 1), "任务没入队"
        time.sleep(0.5)          # 给第二次投递留出落地窗口
        assert q.queued() == [(1, tid)], \
            f"同一任务必须只入队一次，实际 {q.queued()}"
        assert events.count(("queued", tid)) == 1, \
            f"queued 事件只许发一次，实际 {events}"
    finally:
        q.stop()


def test_c4_resubmit_while_running_ignored(tmp_config, monkeypatch):
    """任务正在跑时再 submit → 不允许二次入队二次跑（双跑竞态本体）。"""
    import media_resource_manager.scheduler.queue as qmod
    ev = threading.Event()
    monkeypatch.setattr(qmod, "Engine", lambda cfg, db: _GateEngine(ev))
    events = []
    q = _mk_queue(tmp_config, on_event=lambda k, a, b: events.append((k, a)))
    tid = _mk_task(q)
    q.start()
    try:
        q.submit(tid, 1)
        assert _wait_until(lambda: tid in q.running), "任务没跑起来"
        q.submit(tid, 1)         # running 中再投
        time.sleep(0.3)
        assert tid not in [t for _, t in q.queued()], \
            "正在跑的任务不许再入队"
        ev.set()                 # 放行
        assert _wait_until(lambda: not q.running and not q.queued(),
                           timeout=10), "队列没收干净"
        time.sleep(0.2)
        runs = events.count(("running", tid))
        assert runs == 1, f"同一任务只许真跑一次，实际跑了 {runs} 次"
    finally:
        ev.set()
        q.stop()


def test_c4_pre_start_buffer_not_double_fired(tmp_config):
    """loop 未起时 submit（进 _pending），随后 loop 起来：flush 已投的
    不许再被直投一次；之后重复 submit 也要被去重。"""
    import media_resource_manager.scheduler.queue as qmod
    events = []
    q = _mk_queue(tmp_config, on_event=lambda k, a, b: events.append((k, a)))
    tid = _mk_task(q)
    q.pause()
    t = threading.Timer(0.3, q.start)
    t.start()
    try:
        q.submit(tid, 1)         # loop 未起 → 缓冲进 _pending
        assert _wait_until(lambda: len(q.queued()) >= 1, timeout=6), \
            "缓冲的任务没被 flush 入队"
        time.sleep(0.5)          # 直投（若存在）落地窗口
        assert q.queued() == [(1, tid)], \
            f"缓冲任务只许入队一次，实际 {q.queued()}"
        q.submit(tid, 1)         # 起来之后再投一次 → 必须去重
        time.sleep(0.4)
        assert q.queued() == [(1, tid)], \
            f"重复 submit 必须被去重，实际 {q.queued()}"
    finally:
        t.cancel()
        q.stop()


def test_c4_rerun_after_finish_allowed(tmp_config, monkeypatch):
    """防过度修复：跑完（含失败）之后允许再次 submit（重试是正当需求）。
    若实现把 id 永久占住，这条必须红。"""
    import media_resource_manager.scheduler.queue as qmod
    monkeypatch.setattr(qmod, "Engine", lambda cfg, db: _FastEngine())
    events = []
    q = _mk_queue(tmp_config, on_event=lambda k, a, b: events.append((k, a)))
    tid = _mk_task(q)
    q.start()
    try:
        q.submit(tid, 1)
        assert _wait_until(lambda: tid in q.reports, timeout=10), "第一轮没跑"
        assert _wait_until(lambda: not q.running, timeout=10)
        q.submit(tid, 1)         # 跑完再投 → 必须允许
        assert _wait_until(lambda: events.count(("running", tid)) >= 2,
                           timeout=10), "跑完的任务必须允许重新入队"
    finally:
        q.stop()


def test_c4_main_clears_queued_ids_before_flush():
    """结构护栏（R84 函数块切）：_main 必须先清 _queued_ids 再 flush ——
    防止上一轮 loop 残留的占位 id 挡住新一轮的正当提交。"""
    import media_resource_manager.scheduler.queue as qmod
    src = open(qmod.__file__, "r", encoding="utf-8").read()
    lines = src.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if re.match(r"\s*async def _main\(", ln):
            indent = len(ln) - len(ln.lstrip())
            start = i
            j = i + 1
            while j < len(lines):
                s = lines[j]
                if s.strip() and (len(s) - len(s.lstrip())) <= indent:
                    break
                j += 1
            block = "\n".join(lines[start:j])
            break
    assert start is not None, "_main 不见了"
    assert "self._queued_ids.clear()" in block, \
        "_main 必须清空 _queued_ids（新一轮队列是新的）"
    assert "self._queued_ids.clear()" in block.split(
        "for task_id, prio in", 1)[0], "clear 必须发生在 flush 之前"


# ══════════════════════════════════════════════ B2 · review/queue.py
def test_b2_batch_undo_no_earlier_learn_residual(tmp_config):
    """批量撤销不许残留前项的学习：后项快照含前项学习结果，正序恢复时
    「后项恢复」最后落笔 ⇒ 最终值 = 前项学习后的值（残留）。
    ⚠ 复现前提：词**批前就存在**（新建词会被前项的 DELETE 分支清掉、
    后项 UPDATE 落空，碰巧正确——探针实证，别用新建词做复现）。"""
    db = Database(tmp_config.db_path)
    f1 = _mk_file(tmp_config.library_dir, "a.jpg")
    f2 = _mk_file(tmp_config.library_dir, "b.jpg")
    i1 = _mk_image(db, f1, tags=["kw"])
    i2 = _mk_image(db, f2, tags=["kw"])
    set_preference(db, 0, "kw", 0.5, kind="pos")   # 批前：w0.5 seen1 hp1
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)

    def learn(kind, tags):
        set_preference(db, 0, tags[0], 0.3, kind="pos")

    n = q.apply_batch([i1, i2], ReviewDecision.DISLIKE,
                      on_learn_factory=lambda rec: learn)
    assert n == 2
    row = _pref_row(db, "kw")
    assert float(row["weight"]) == pytest.approx(1.1)
    assert int(row["hit_pos"]) == 3

    q.undo()
    row = _pref_row(db, "kw")
    assert row is not None, "批前就存在的词不许被撤销删掉"
    assert float(row["weight"]) == pytest.approx(0.5), \
        f"权重必须回到批前，实际 {row['weight']}（前项学习残留）"
    assert int(row["seen"]) == 1
    assert int(row["hit_pos"]) == 1, \
        f"hit_pos 必须回到批前，实际 {row['hit_pos']}"


def test_b2_batch_undo_removes_label_created_by_batch(tmp_config):
    """批内新建的词（批前不存在）：整批撤销后必须消失，不许残留。
    （正序时这条靠 DELETE+UPDATE落空碰巧对，倒序后由 DELETE 分支真正保证。）"""
    db = Database(tmp_config.db_path)
    f1 = _mk_file(tmp_config.library_dir, "a.jpg")
    f2 = _mk_file(tmp_config.library_dir, "b.jpg")
    i1 = _mk_image(db, f1, tags=["kw"])
    i2 = _mk_image(db, f2, tags=["kw"])
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)

    def learn(kind, tags):
        set_preference(db, 0, tags[0], 0.5, kind="pos")

    n = q.apply_batch([i1, i2], ReviewDecision.DISLIKE,
                      on_learn_factory=lambda rec: learn)
    assert n == 2
    assert _pref_row(db, "kw") is not None

    q.undo()
    assert _pref_row(db, "kw") is None, \
        "批内新建的学习词必须在整批撤销后消失"


def test_b2_single_undo_restores_hit_counters(tmp_config):
    """单条撤销：weight/seen 之外，hit_pos/hit_neg 也必须回滚
    （学习写入时 hit_pos+1，旧 _undo_one 只回 weight/seen）。"""
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir, "a.jpg")
    iid = _mk_image(db, f, tags=["kw"])
    set_preference(db, 0, "kw", 0.5, kind="pos")   # 批前已有：w0.5 seen1 hp1
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)

    q.apply(iid, ReviewDecision.DISLIKE,
            on_learn=lambda kind, tags: set_preference(db, 0, tags[0], 0.3,
                                                       kind="pos"))
    row = _pref_row(db, "kw")
    assert float(row["weight"]) == pytest.approx(0.8)
    assert int(row["hit_pos"]) == 2

    q.undo()
    row = _pref_row(db, "kw")
    assert row is not None, "撤销不该把批前就存在的词整个删掉"
    assert float(row["weight"]) == pytest.approx(0.5)
    assert int(row["seen"]) == 1
    assert int(row["hit_pos"]) == 1, \
        f"hit_pos 必须回滚到操作前，实际 {row['hit_pos']}"
    assert int(row["hit_neg"]) == 0


def test_b2_batch_undo_files_still_restored(tmp_config):
    """防过度修复：倒序恢复不许破坏 B1 已有的文件/状态回滚。"""
    db = Database(tmp_config.db_path)
    f1 = _mk_file(tmp_config.library_dir, "a.jpg")
    f2 = _mk_file(tmp_config.library_dir, "b.jpg")
    i1 = _mk_image(db, f1, tags=["cat"])
    i2 = _mk_image(db, f2, tags=["dog"])
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)
    n = q.apply_batch([i1, i2], ReviewDecision.DISLIKE)
    assert n == 2
    assert not f1.exists() and not f2.exists()
    q.undo()
    assert f1.exists(), "撤销后文件必须回到原位"
    assert f2.exists(), "撤销后文件必须回到原位"
    from media_resource_manager.repo import get_image
    for iid, f in ((i1, f1), (i2, f2)):
        rec = get_image(db, iid)
        assert rec.in_quarantine is True and rec.needs_review is True
        assert rec.path == str(f)


from media_resource_manager.review.queue import ReviewQueue  # noqa: E402
