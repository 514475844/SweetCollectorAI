"""请求1：取消隔离区，用审核替代——AI 不命中/排除/黑名单命中都进审核（文件保留）。
请求3：去重落败方直接删除，不再占磁盘。
"""
import pytest

from media_resource_manager.db import Database
from media_resource_manager.engine import Engine
from media_resource_manager.models import ImageRecord, ReviewDecision
from media_resource_manager.repo import upsert_image, get_image
from media_resource_manager.review.queue import ReviewQueue


def _mk_file(d, name="a.jpg"):
    p = d / name
    p.write_bytes(b"x" * 100)
    return p


def test_review_queue_shows_blacklist_review(tmp_config):
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir, "bw.jpg")
    rec = ImageRecord(id=None, source_url="https://x/bw.jpg", task_id=1,
                      path=str(f), blacklisted=True, in_quarantine=True,
                      needs_review=True)
    iid = upsert_image(db, rec)
    q = ReviewQueue(db, tmp_config.isolate_dir)
    pend = q.pending()
    assert any(r.id == iid for r in pend), "黑名单待审核项应出现在审核队列"


def test_review_like_clears_blacklisted(tmp_config):
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir, "bw.jpg")
    rec = ImageRecord(id=None, source_url="https://x/bw.jpg", task_id=1,
                      path=str(f), blacklisted=True, in_quarantine=True,
                      needs_review=True)
    iid = upsert_image(db, rec)
    q = ReviewQueue(db, tmp_config.isolate_dir)
    q.apply(iid, ReviewDecision.LIKE)
    r = get_image(db, iid)
    assert r.blacklisted is False
    assert r.in_quarantine is False


def test_review_skip_keeps_blacklisted(tmp_config):
    """「跳过」= 这页先不看，不该改变这张图的归属。

    老行为是 SKIP 和 LIKE 一起清 blacklisted —— 等于点一下「跳过」就能把
    一张命中黑名单的图放回图库。现在改成：SKIP 照样退出审核队列，但保留
    blacklisted 标记；只有「喜欢」才否决黑名单。
    """
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir, "bw.jpg")
    rec = ImageRecord(id=None, source_url="https://x/bw.jpg", task_id=1,
                      path=str(f), blacklisted=True, in_quarantine=True,
                      needs_review=True)
    iid = upsert_image(db, rec)
    q = ReviewQueue(db, tmp_config.isolate_dir)
    q.apply(iid, ReviewDecision.SKIP)
    r = get_image(db, iid)
    assert r.in_quarantine is False, "跳过应退出审核队列"
    assert r.blacklisted is True, "跳过不该否决黑名单，只有「喜欢」才可以"


def test_review_like_still_clears_blacklisted(tmp_config):
    """反向锁：确认「喜欢」仍然否决黑名单（上面那条不能顺手改坏 LIKE）。"""
    db = Database(tmp_config.db_path)
    f = _mk_file(tmp_config.library_dir, "bw.jpg")
    rec = ImageRecord(id=None, source_url="https://x/bw.jpg", task_id=1,
                      path=str(f), blacklisted=True, in_quarantine=True,
                      needs_review=True)
    iid = upsert_image(db, rec)
    q = ReviewQueue(db, tmp_config.isolate_dir)
    q.apply(iid, ReviewDecision.LIKE)
    r = get_image(db, iid)
    assert r.in_quarantine is False
    assert r.blacklisted is False


def test_keep_largest_removed_per_user_decision(tmp_config):
    """V0.0.5：留大删小彻底取消（用户拍板：重复图片功能不做，别造轮子）。

    原 test_keep_largest_* 两条钉的是「新图大就删旧图」，正是被废除的
    行为。现在换成反向锁：引擎**不许**再有 _keep_largest / cluster_dedupe
    这两个方法，且库里旧图在任何重复场景下都不会被删。
    """
    eng = Engine(tmp_config, db := Database(tmp_config.db_path))
    assert not hasattr(eng, "_keep_largest"), "留大删小必须彻底移除"
    assert not hasattr(eng, "cluster_dedupe"), "聚类去重必须彻底移除"
    old = _mk_file(tmp_config.library_dir, "old.jpg")
    rec = ImageRecord(id=None, source_url="https://x/old.jpg", task_id=1,
                      path=str(old), width=100, height=100, bytes=100)
    upsert_image(db, rec)
    assert old.exists()
