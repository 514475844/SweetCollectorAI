"""backfill_local_files 的后台友好改造回归：进度回调 / 取消 / 快速路径。"""
from __future__ import annotations

import threading
from pathlib import Path

from media_resource_manager.db import Database
from media_resource_manager.engine import backfill_local_files
from media_resource_manager.models import TaskConfig, TaskStatus


def _make_task(db, out: Path) -> int:
    t = TaskConfig(id=None, name="bf", adapter_name="",
                   output_dir=str(out))
    from media_resource_manager.repo import save_task
    return save_task(db, t)


def _new_db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "t.db")
    return db


def test_backfill_fast_path_and_progress(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.jpg").write_bytes(b"\xff\xd8\xff\xe0realimagebytes")
    (out / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nrealimagebytes")
    (out / "empty.jpg").write_bytes(b"")          # 0 字节应被跳过
    (out / "note.txt").write_bytes(b"not an image")  # 非图片后缀应被跳过

    db = _new_db(tmp_path)
    tid = _make_task(db, out)

    calls = []
    added = backfill_local_files(
        db, tid, on_progress=lambda s, a, c: calls.append((s, a, c)))

    # 只补写 2 张真图；0 字节 / 非图片后缀都被忽略
    assert added == 2, added
    rows = db.query("SELECT source_url FROM images WHERE task_id=?", (tid,))
    assert len(rows) == 2
    # 进度回调至少被调用一次（任务结束时）
    assert any(a >= 2 for _, a, _ in calls), calls


def test_backfill_stop_event_short_circuits(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    for i in range(5):
        (out / f"img{i}.jpg").write_bytes(b"x" * 10)
    db = _new_db(tmp_path)
    tid = _make_task(db, out)

    stop = threading.Event()
    stop.set()  # 一开始就要取消
    added = backfill_local_files(db, tid, stop=stop)
    # 取消后不应写入任何行
    assert added == 0, added
    rows = db.query("SELECT 1 FROM images WHERE task_id=?", (tid,))
    assert rows == []


def test_backfill_idempotent(tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.jpg").write_bytes(b"abc")
    db = _new_db(tmp_path)
    tid = _make_task(db, out)
    n1 = backfill_local_files(db, tid)
    n2 = backfill_local_files(db, tid)  # 第二次跑：已全部在库，应跳过
    assert n1 == 1 and n2 == 0, (n1, n2)
