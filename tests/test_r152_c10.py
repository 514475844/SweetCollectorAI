# -*- coding: utf-8 -*-
"""R152-C10（DB 健壮性）行为护栏 —— R221 棒。

订单原文：`db.py` 开 foreign_keys；synchronous 评估（WAL 下 NORMAL 可接受则
在注释写明）；repo upsert 多步写包事务。

落地口径（决策留档）：
· images.task_id **刻意不设** tasks 外键 —— 删任务的现存语义是「保留用户
  已下载的图库图片」，而 task_id NOT NULL 又不能 SET NULL，加外键等于逼
  「删任务先删图」或「任务删不掉」。其余三张表（reviews→images、
  failed_urls→tasks、task_pages→tasks）的 FK 照常强制。
· 老库（建表带 images→tasks FK）开库时跑一次表重建迁移摘掉它；迁移失败则
  foreign_keys 保持关闭，绝不半开。
· 所有「DELETE FROM images」调用点先删 reviews；所有删任务/模板路径先清
  failed_urls/task_pages；两处批量清理固定「子表先于父表」顺序。
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from media_resource_manager.db import SCHEMA, Database
from media_resource_manager.models import ImageRecord
from media_resource_manager.repo import upsert_image

SRC_DB = os.path.join(ROOT, "media_resource_manager", "db.py")
SRC_MW = os.path.join(ROOT, "media_resource_manager", "gui", "main_window.py")
SRC_WEB = os.path.join(ROOT, "media_resource_manager", "webui.py")
SRC_CLN = os.path.join(ROOT, "media_resource_manager", "cleanup.py")

#: 老库建表语句 = 现 SCHEMA 把 images.task_id 的外键加回去（迁移前的形态）
LEGACY_SCHEMA = SCHEMA.replace(
    "task_id       INTEGER NOT NULL,",
    "task_id       INTEGER NOT NULL REFERENCES tasks(id),", 1)


def _mkdb(path) -> Database:
    return Database(path)


def _seed_task(db: Database, tid: int = 1) -> None:
    db.execute("INSERT INTO tasks(id, name) VALUES (?,?)", (tid, f"t{tid}"))


# ══════════════════════════ 一、外键强制（新库）
def test_foreign_keys_pragma_is_on(tmp_path):
    db = _mkdb(tmp_path / "a.db")
    try:
        row = db.query("PRAGMA foreign_keys")
        assert row and int(row[0][0]) == 1
    finally:
        db.close()


def test_images_task_fk_dropped_other_three_kept(tmp_path):
    db = _mkdb(tmp_path / "b.db")
    try:
        fk_images = db.query("PRAGMA foreign_key_list(images)")
        assert not [r for r in fk_images if r[2] == "tasks"], \
            "images→tasks 外键必须已退役（删任务保留图片的语义优先）"
        for tbl, parent in (("reviews", "images"),
                            ("failed_urls", "tasks"),
                            ("task_pages", "tasks")):
            rows = db.query(f"PRAGMA foreign_key_list({tbl})")
            assert [r for r in rows if r[2] == parent], \
                f"{tbl}→{parent} 外键必须保留且被强制"
    finally:
        db.close()


def test_reviews_fk_blocks_image_delete_until_review_gone(tmp_path):
    """行为本质：FK 真开着 —— 有 reviews 行的图片删不动，删掉 reviews 行才能删。"""
    db = _mkdb(tmp_path / "c.db")
    try:
        _seed_task(db)
        db.execute("INSERT INTO images(task_id, source_url) VALUES (1,'u1')")
        iid = int(db.query("SELECT id FROM images")[0]["id"])
        db.execute("INSERT INTO reviews(image_id, decision) VALUES (?, 'like')",
                   (iid,))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM images WHERE id=?", (iid,))
        db.execute("DELETE FROM reviews WHERE image_id=?", (iid,))
        db.execute("DELETE FROM images WHERE id=?", (iid,))
        assert db.query("SELECT id FROM images") == []
    finally:
        db.close()


def test_task_children_fk_blocks_task_delete(tmp_path):
    """failed_urls/task_pages 引用 tasks：不清子表就删不掉任务。"""
    db = _mkdb(tmp_path / "d.db")
    try:
        _seed_task(db, tid=2)
        db.execute("INSERT INTO failed_urls(task_id, url) VALUES (2,'http://x/')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM tasks WHERE id=?", (2,))
        assert db.clear_failed_urls(2) == 1
        db.execute("DELETE FROM tasks WHERE id=?", (2,))
        assert db.query("SELECT id FROM tasks WHERE id=2") == []
    finally:
        db.close()


# ══════════════════════════ 二、老库迁移
def _make_legacy_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO tasks(id, name) VALUES (1,'legacy')")
    conn.execute("INSERT INTO images(id, task_id, source_url, path) "
                 "VALUES (5,1,'http://a/1.jpg','C:/x/1.jpg')")
    conn.execute("INSERT INTO images(id, task_id, source_url, path) "
                 "VALUES (9,1,'http://a/2.jpg','C:/x/2.jpg')")
    conn.execute("INSERT INTO reviews(image_id, decision) VALUES (5,'like')")
    conn.commit()
    conn.close()


def test_legacy_db_migrated_fk_dropped_rows_intact_ids_continue(tmp_path):
    p = tmp_path / "legacy.db"
    _make_legacy_db(p)
    db = Database(p)
    try:
        # ① 外键摘干净了，且强制开着
        left = db.query("PRAGMA foreign_key_list(images)")
        assert not [r for r in left if r[2] == "tasks"]
        assert int(db.query("PRAGMA foreign_keys")[0][0]) == 1
        # ② 数据一根毛不少（含 id 与 reviews 引用关系）
        ids = [int(r["id"]) for r in db.query("SELECT id FROM images ORDER BY id")]
        assert ids == [5, 9]
        assert db.query("SELECT COUNT(*) AS n FROM reviews")[0]["n"] == 1
        # ③ 坑③：AUTOINCREMENT 计数补回 —— 新插入必须拿 10，不能撞 id=5/9
        nid = upsert_image(db, ImageRecord(
            id=None, task_id=1, source_url="http://a/3.jpg", path="C:/x/3.jpg"))
        assert nid == 10
        # ④ reviews→images 的 FK 迁移后照常强制（reviews 行挂在 id=5 上）
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM images WHERE id=?", (5,))
        db.execute("DELETE FROM reviews WHERE image_id=?", (5,))
        db.execute("DELETE FROM images WHERE id=?", (5,))
    finally:
        db.close()


def test_migration_is_idempotent(tmp_path):
    p = tmp_path / "legacy2.db"
    _make_legacy_db(p)
    db = Database(p)
    db.close()
    db2 = Database(p)          # 第二次开库：迁移应识别为「已迁移」直接放行
    try:
        assert int(db2.query("PRAGMA foreign_keys")[0][0]) == 1
        ids = [int(r["id"]) for r in db2.query("SELECT id FROM images ORDER BY id")]
        assert ids == [5, 9]
    finally:
        db2.close()


# ══════════════════════════ 三、upsert 事务
def test_upsert_concurrent_same_key_single_row(tmp_path):
    """并发 upsert 同一 (task_id, source_url)：不炸、不插重行。"""
    db = _mkdb(tmp_path / "race.db")
    try:
        _seed_task(db)
        errs: list = []

        def worker():
            try:
                for i in range(15):
                    upsert_image(db, ImageRecord(
                        id=None, task_id=1, source_url="http://same/1.jpg",
                        path=f"C:/p{i}.jpg", tags=["a"]))
            except Exception as e:      # pragma: no cover - 失败即红
                errs.append(e)

        ths = [threading.Thread(target=worker) for _ in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(timeout=30)
        assert not errs, f"并发 upsert 抛异常：{errs[:3]}"
        rows = db.query("SELECT * FROM images")
        assert len(rows) == 1, f"同一键被插了 {len(rows)} 行"
    finally:
        db.close()


def test_upsert_failure_rolls_back_no_partial_row(tmp_path):
    """INSERT 成功但随后抛错 ⇒ 整本账 ROLLBACK，库里不留半行。"""
    db = _mkdb(tmp_path / "rb.db")
    try:
        _seed_task(db)
        orig = db.execute

        def boom(sql, params=()):
            cur = orig(sql, params)
            if sql.strip().upper().startswith("INSERT INTO IMAGES"):
                raise RuntimeError("boom")
            return cur

        db.execute = boom               # 实例属性 shadow，只影响 upsert 路径
        with pytest.raises(RuntimeError):
            upsert_image(db, ImageRecord(
                id=None, task_id=1, source_url="http://r/1.jpg"))
        assert db.query("SELECT * FROM images") == [], "事务没回滚，留了半行"
    finally:
        db.close()


def test_transaction_commit_rollback_and_nested(tmp_path):
    db = _mkdb(tmp_path / "tx.db")
    try:
        with db.transaction():
            db.execute("INSERT INTO tasks(id,name) VALUES (1,'a')")
            with db.transaction():       # 嵌套加入外层，不另开不抢提交
                db.execute("INSERT INTO tasks(id,name) VALUES (2,'b')")
        assert len(db.query("SELECT id FROM tasks")) == 2
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.execute("INSERT INTO tasks(id,name) VALUES (3,'c')")
                raise RuntimeError("x")
        assert db.query("SELECT id FROM tasks WHERE id=3") == []
        assert len(db.query("SELECT id FROM tasks")) == 2
    finally:
        db.close()


# ══════════════════════════ 四、源码级判据
def test_synchronous_normal_documented_with_wal_rationale():
    """订单原话「WAL 下 NORMAL 可接受则在注释写明」——注释必须真在。"""
    with open(SRC_DB, encoding="utf-8") as f:
        src = f.read()
    i = src.find("PRAGMA synchronous=NORMAL")
    assert i > 0, "synchronous=NORMAL 不见了"
    window = src[max(0, i - 900):i]
    assert "WAL" in window and "可接受" in window, \
        "synchronous=NORMAL 旁边必须有 WAL 下可接受的论证注释"


#: 这些文件里每一处字面量 DELETE FROM images，同一函数体内必须先出现
#: 字面量 DELETE FROM reviews（行号更小）。
_DELETE_IMAGE_FILES = (
    "media_resource_manager/db.py",
    "media_resource_manager/gui/main_window.py",
    "media_resource_manager/webui.py",
    "media_resource_manager/review/queue.py",
    "media_resource_manager/tidy.py",
    "media_resource_manager/cleanup.py",
)


def _literal_sql(call: ast.Call) -> str:
    if not call.args:
        return ""
    a = call.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    return ""


def test_every_delete_images_site_deletes_reviews_first():
    """AST 巡检：DELETE FROM images 的调用点，reviews 必须删在同一个函数里、
    且行号更早。防的是「FK 一开，删图连带 reviews 忘删」的复发。"""
    checked = 0
    for rel in _DELETE_IMAGE_FILES:
        path = os.path.join(ROOT, rel.replace("/", os.sep))
        with open(path, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "execute"]
            img_calls = [c for c in calls
                         if _literal_sql(c).strip().upper()
                         .startswith("DELETE FROM IMAGES")]
            if not img_calls:
                continue
            rev_lines = [c.lineno for c in calls
                         if _literal_sql(c).strip().upper()
                         .startswith("DELETE FROM REVIEWS")]
            for c in img_calls:
                checked += 1
                assert any(rl < c.lineno for rl in rev_lines), (
                    f"{rel}:{c.lineno} 处 DELETE FROM images 之前"
                    f"（同函数内）没有 DELETE FROM reviews")
    assert checked >= 10, f"巡检覆盖面异常：只查到 {checked} 处"


def test_bulk_cleanup_paths_order_children_first():
    """两处批量清理（webui / cleanup.py）必须固定「子表先于父表」顺序。"""
    with open(SRC_WEB, encoding="utf-8") as f:
        web = f.read()
    assert "_fk_order" in web, "webui 批量清理丢了固定顺序"
    i = web.find("_fk_order = ")
    seg = web[i:i + 200]
    assert seg.find("reviews") < seg.find("images") < seg.find("tasks"), \
        "webui 批量清理顺序必须是 reviews → images → tasks"
    with open(SRC_CLN, encoding="utf-8") as f:
        cln = f.read()
    assert "_ordered" in cln and '"review"' in cln, \
        "cleanup 全面清理必须固定 review 先于 library"


def test_cleanup_library_only_cascades_reviews(tmp_path):
    """行为：桌面全面清理只勾 library 也必须真清掉 images。

    FK 下 reviews 是 images 的子表 —— 不清子表就删不动父表，异常再被
    except 吞掉就是「ok 但没清」（R67 同族）。级联清 reviews 与 app 里
    所有删图路径同口径；学习样本自 R166 起在 learn_samples，不受影响。
    """
    from types import SimpleNamespace

    from media_resource_manager.cleanup import perform_full_cleanup
    lib, rev, iso = (tmp_path / n for n in ("lib", "rev", "iso"))
    for d in (lib, rev, iso):
        d.mkdir()
    cfg = SimpleNamespace(library_dir=str(lib), review_dir=str(rev),
                          isolate_dir=str(iso))
    db = _mkdb(tmp_path / "cl.db")
    try:
        _seed_task(db)
        db.execute("INSERT INTO images(task_id, source_url) VALUES (1,'u')")
        iid = int(db.query("SELECT id FROM images")[0]["id"])
        db.execute("INSERT INTO reviews(image_id, decision) VALUES (?, 'like')",
                   (iid,))
        perform_full_cleanup(db, cfg, root=tmp_path, plan={"library"})
        assert db.query("SELECT COUNT(*) AS n FROM images")[0]["n"] == 0, \
            "只勾 library 没清掉 images"
        assert db.query("SELECT COUNT(*) AS n FROM reviews")[0]["n"] == 0, \
            "images 清了但 reviews 子表没级联清掉"
    finally:
        db.close()
