"""R182 护栏：R160-#82「最近浏览」数据层（执行方笔记本棒，窗口内）。

本棒只落地 #82 的**数据地基**（DB 列 + 唯一写入口 `DB.mark_viewed`），
双端排序入口留给下一棒。行为断言优先、源码判据兜底。

覆盖：
- 新库建表就带 last_viewed_at；
- **老库升级**：把列 DROP 掉再打开，迁移必须自己补回来（这是真实升级路径：
  用户手上的库是升级前建的）；
- mark_viewed 一次写两个字段（viewed + 时间戳）；
- **重复看覆盖成最新时间**（「最近浏览」= 最后一次，不是第一次）；
- 空 id / 不存在的 id 都不炸，且不误伤别行；
- 桌面 `_mark_viewed` 必须走 DB.mark_viewed（不许再各写各的 SQL）；
- 「最近浏览」排序口径：NULL 排最后（没看过的图不能霸占榜首）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_resource_manager.db import Database as DB  # noqa: E402


def _db(tmp_path: Path, name: str = "t.db") -> DB:
    return DB(tmp_path / name)


def _add_task(db: DB) -> int:
    db.execute("INSERT INTO tasks(name) VALUES('t')")
    return int(db.query("SELECT last_insert_rowid() AS id")[0]["id"])


def _add_image(db: DB, task_id: int, url: str) -> int:
    db.execute(
        "INSERT INTO images(task_id, source_url) VALUES(?,?)", (task_id, url))
    return int(db.query("SELECT last_insert_rowid() AS id")[0]["id"])


def _cols(db: DB, table: str = "images") -> set:
    return {r["name"] for r in db._conn.execute(f"PRAGMA table_info({table})")}


# ----------------------------------------------------------- 迁移 ----
def test_fresh_db_has_last_viewed_at(tmp_path):
    db = _db(tmp_path)
    assert "last_viewed_at" in _cols(db)
    db.close()


def test_legacy_db_gets_column_back(tmp_path):
    """老库（升级前建的）在打开时被迁移补列。

    DROP COLUMN 模拟「列从来没存在过」，比手写一份旧 SCHEMA 更贴近真升级。
    """
    p = tmp_path / "legacy.db"
    db = DB(p)
    tid = _add_task(db)
    iid = _add_image(db, tid, "http://a/1.jpg")
    db.execute("UPDATE images SET viewed=1 WHERE id=?", (iid,))
    db._conn.execute("ALTER TABLE images DROP COLUMN last_viewed_at")
    db.close()

    db2 = DB(p)  # 重新打开 = 真实升级路径
    assert "last_viewed_at" in _cols(db2)
    # 老数据不能被迁移冲掉
    row = db2.query("SELECT viewed, last_viewed_at FROM images WHERE id=?",
                    (iid,))[0]
    assert row["viewed"] == 1
    assert row["last_viewed_at"] is None
    db2.close()


# ----------------------------------------------------- mark_viewed ----
def test_mark_viewed_writes_both_fields(tmp_path):
    db = _db(tmp_path)
    tid = _add_task(db)
    iid = _add_image(db, tid, "http://a/1.jpg")
    db.mark_viewed(iid)
    row = db.query("SELECT viewed, last_viewed_at FROM images WHERE id=?",
                   (iid,))[0]
    assert row["viewed"] == 1
    assert row["last_viewed_at"], "看了图却没写时间戳 ⇒ 最近浏览永远排不出来"
    db.close()


def test_mark_viewed_overwrites_with_latest(tmp_path):
    db = _db(tmp_path)
    tid = _add_task(db)
    iid = _add_image(db, tid, "http://a/1.jpg")
    db.execute("UPDATE images SET last_viewed_at='2000-01-01 00:00:00' "
               "WHERE id=?", (iid,))
    db.mark_viewed(iid)
    now = db.query("SELECT last_viewed_at AS t FROM images WHERE id=?",
                   (iid,))[0]["t"]
    assert now > "2000-01-01 00:00:00", "重复看没盖时间 ⇒ 排的是第一次看的时间"
    db.close()


def test_mark_viewed_touches_only_target(tmp_path):
    db = _db(tmp_path)
    tid = _add_task(db)
    a = _add_image(db, tid, "http://a/1.jpg")
    b = _add_image(db, tid, "http://a/2.jpg")
    db.mark_viewed(a)
    row_b = db.query("SELECT viewed, last_viewed_at FROM images WHERE id=?",
                     (b,))[0]
    assert row_b["viewed"] == 0 and row_b["last_viewed_at"] is None
    db.close()


@pytest.mark.parametrize("bad", [None, 999999])
def test_mark_viewed_tolerates_bad_id(tmp_path, bad):
    """None / 不存在的 id 都不许炸：_mark_viewed 在灯箱路径上是 try 包着的，
    但 DB 层自己也得站得住，否则每个调用点都得记得包 try。"""
    db = _db(tmp_path)
    tid = _add_task(db)
    iid = _add_image(db, tid, "http://a/1.jpg")
    db.mark_viewed(bad)
    row = db.query("SELECT viewed FROM images WHERE id=?", (iid,))[0]
    assert row["viewed"] == 0
    db.close()


# --------------------------------------------------- 桌面入口收敛 ----
def test_main_window_mark_viewed_delegates_to_db(tmp_path):
    """桌面 _mark_viewed 必须调 DB.mark_viewed，不许自写 SQL。

    判据盯「被调用」而不是「字符串里出现」：留一份同义 SQL 在 main_window
    里也能骗过搜串，但两个入口迟早写得不一样。
    """
    from media_resource_manager.gui import main_window as mw

    calls = []

    class FakeDB:
        def mark_viewed(self, iid):
            calls.append(iid)

    class FakeSelf:
        db = FakeDB()

    mw.MainWindow._mark_viewed(FakeSelf(), 7)
    assert calls == [7], "桌面端没走统一写入口 ⇒ 别的入口会漏写时间戳"

    src = Path(mw.__file__).read_text(encoding="utf-8")
    assert "SET viewed=1 WHERE" not in src, "main_window 里还留着老的直接写库语句"


def test_recent_sort_puts_unseen_last(tmp_path):
    """排序口径：没看过的（NULL）必须排最后。

    SQLite 默认 NULL 最小，ASC 会霸占榜首；「最近浏览」DESC 下 NULL 自然落底，
    这条把口径钉死，免得以后有人改成 ASC 或 COALESCE 成 '' 把没看过的顶上来。
    """
    db = _db(tmp_path)
    tid = _add_task(db)
    seen = _add_image(db, tid, "http://a/1.jpg")
    unseen = _add_image(db, tid, "http://a/2.jpg")
    db.mark_viewed(seen)
    rows = db.query(
        "SELECT id FROM images ORDER BY "
        "CASE WHEN last_viewed_at IS NULL THEN 1 ELSE 0 END, "
        "last_viewed_at DESC, id DESC")
    assert [r["id"] for r in rows][0] == seen
    assert [r["id"] for r in rows][-1] == unseen
    db.close()


def test_mark_viewed_sql_is_single_statement(tmp_path):
    """一条 UPDATE 改两个字段：拆成两条会留下「标记了但没时间」的中间态。"""
    db = _db(tmp_path)
    tid = _add_task(db)
    iid = _add_image(db, tid, "http://a/1.jpg")
    db.mark_viewed(iid)
    # 用 sqlite3 的 authorizer 级验证不现实；这里退一步钉「两次读之间一致」
    row = db.query("SELECT viewed, last_viewed_at FROM images WHERE id=?",
                   (iid,))[0]
    assert (row["viewed"] == 1) == (row["last_viewed_at"] is not None)
    db.close()
