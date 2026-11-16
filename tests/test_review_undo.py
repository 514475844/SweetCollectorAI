"""#12 审核撤销：状态 / 文件位置 / 学习权重一起回滚，批量作为一个事务。"""
import pytest

from media_resource_manager.db import Database
from media_resource_manager.models import ReviewDecision
from media_resource_manager.repo import upsert_image
from media_resource_manager.review.queue import ReviewQueue

from media_resource_manager.models import ImageRecord  # noqa: E402


def _mk_image(db: Database, path, task_id=1, tags=None, quarantine=False):
    rec = ImageRecord(
        id=None, source_url=f"https://x.com/{path.name}", task_id=task_id,
        path=str(path), tags=list(tags or ["cat"]),
        in_quarantine=quarantine, needs_review=True)
    return upsert_image(db, rec)


def _mk_file(tmp_path, name="a.jpg"):
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    return p


def _get(db, iid):
    from media_resource_manager.repo import get_image
    return get_image(db, iid)


def _pref(db, tid, label):
    # 学习记录现在统一存到全局（task_id=0），tid 仅作兼容保留。
    rows = db.query("SELECT weight, seen FROM preferences WHERE task_id=0 "
                    "AND label=?", (label,))
    return (rows[0]["weight"], rows[0]["seen"]) if rows else None


def test_undo_like_restores_state(tmp_config):
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir)
    iid = _mk_image(db, f, quarantine=True)
    # V0.0.6：审核队列必须拿到配置才能判断「哪些文件是软件自己的」；
    # 少传配置时一律不动磁盘（安全侧）。生产三处调用点都传 cfg。
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)

    q.apply(iid, ReviewDecision.LIKE,
            on_learn=lambda kind, tags: db.execute(
                "INSERT INTO preferences(task_id,label,weight,seen) "
                "VALUES(?,?,?,?)", (0, tags[0], 1.0, 1)))
    rec = _get(db, iid)
    assert rec.favorite and not rec.in_quarantine

    assert q.can_undo()
    q.undo()
    rec = _get(db, iid)
    assert not rec.favorite, "撤销后 favorite 必须回到操作前"
    assert rec.in_quarantine is True
    assert rec.needs_review is True


def test_undo_dislike_moves_file_back(tmp_config):
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir)
    iid = _mk_image(db, f, quarantine=True)
    # V0.0.6：审核队列必须拿到配置才能判断「哪些文件是软件自己的」；
    # 少传配置时一律不动磁盘（安全侧）。生产三处调用点都传 cfg。
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)

    q.apply(iid, ReviewDecision.DISLIKE)
    assert not f.exists(), "不喜欢应把文件移进隔离区"
    moved = _get(db, iid).path

    q.undo()
    assert f.exists(), "撤销后文件必须回到原来的保存位置"
    assert _get(db, iid).path == str(f)


def test_undo_reverts_learned_weight(tmp_config):
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir)
    iid = _mk_image(db, f, quarantine=True, tags=["cat"])
    # V0.0.6：审核队列必须拿到配置才能判断「哪些文件是软件自己的」；
    # 少传配置时一律不动磁盘（安全侧）。生产三处调用点都传 cfg。
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)

    def learn(kind, tags):
        for t in tags or []:
            db.execute(
                "INSERT INTO preferences(task_id,label,weight,seen) "
                "VALUES(?,?,?,?) ON CONFLICT(task_id,label) DO UPDATE SET "
                "weight=weight+excluded.weight, seen=seen+excluded.seen",
                (0, t, 0.5, 1))

    db.execute("INSERT INTO preferences(task_id,label,weight,seen) "
               "VALUES(?,?,?,?)", (0, "cat", 0.25, 2))
    q.apply(iid, ReviewDecision.LIKE, on_learn=learn)
    assert _pref(db, 0, "cat") == (0.75, 3)

    q.undo()
    assert _pref(db, 0, "cat") == (0.25, 2), "学习权重必须回滚到操作前"


def test_undo_removes_new_preference_rows(tmp_config):
    """操作前没有的权重，撤销后要删掉（不能留 residual）。"""
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir)
    iid = _mk_image(db, f, quarantine=True, tags=["dog"])
    # V0.0.6：审核队列必须拿到配置才能判断「哪些文件是软件自己的」；
    # 少传配置时一律不动磁盘（安全侧）。生产三处调用点都传 cfg。
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)
    q.apply(iid, ReviewDecision.LIKE,
            on_learn=lambda k, tags: db.execute(
                "INSERT INTO preferences(task_id,label,weight,seen) "
                "VALUES(?,?,?,?)", (0, tags[0], 1.0, 1)))
    assert _pref(db, 0, "dog") is not None
    q.undo()
    assert _pref(db, 0, "dog") is None


def test_batch_undo_is_one_transaction(tmp_config):
    db = Database(tmp_config.db_path)
    # V0.0.6：审核队列必须拿到配置才能判断「哪些文件是软件自己的」；
    # 少传配置时一律不动磁盘（安全侧）。生产三处调用点都传 cfg。
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)
    ids = []
    for i in range(4):
        f = _mk_file(tmp_config.library_dir, f"p{i}.jpg")
        ids.append(_mk_image(db, f, quarantine=True))
    n = q.apply_batch(ids, ReviewDecision.LIKE)
    assert n == 4
    assert all(_get(db, i).favorite for i in ids)

    assert q.undo_depth() == 1, "批量必须只占一步撤销栈"
    q.undo()
    assert not any(_get(db, i).favorite for i in ids), "一次撤销要回滚整批"


def test_undo_removes_review_row(tmp_config):
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir)
    iid = _mk_image(db, f, quarantine=True)
    # V0.0.6：审核队列必须拿到配置才能判断「哪些文件是软件自己的」；
    # 少传配置时一律不动磁盘（安全侧）。生产三处调用点都传 cfg。
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)
    q.apply(iid, ReviewDecision.SKIP)
    assert db.query("SELECT 1 FROM reviews WHERE image_id=?", (iid,))
    q.undo()
    assert not db.query("SELECT 1 FROM reviews WHERE image_id=?", (iid,))


def test_undo_empty_stack_returns_none(tmp_config):
    db = Database(tmp_config.db_path)
    # V0.0.6：审核队列必须拿到配置才能判断「哪些文件是软件自己的」；
    # 少传配置时一律不动磁盘（安全侧）。生产三处调用点都传 cfg。
    q = ReviewQueue(db, tmp_config.isolate_dir, cfg=tmp_config)
    assert not q.can_undo()
    assert q.undo() is None
    assert q.undo_label() == ""


def test_undo_stack_capped(tmp_config):
    db = Database(tmp_config.db_path)
    q = ReviewQueue(db, tmp_config.isolate_dir, undo_limit=3)
    for i in range(6):
        f = _mk_file(tmp_config.library_dir, f"c{i}.jpg")
        q.apply(_mk_image(db, f, quarantine=True), ReviewDecision.SKIP)
    assert q.undo_depth() == 3, "撤销栈必须限制在最近 N 步"
