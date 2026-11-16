"""V0.0.8 回归：审核页 = 纠错反馈口。

钉住四件事：
1) 送审原因写库（引擎/手动移入/抽检），并且**空原因不覆盖已有原因**；
2) 原因翻译纯函数对引擎真实备注格式全覆盖；
3) 网页接口：待审核列表带 reason、判退响应带「学到了什么」、move 写原因；
4) 撤销一步判退：图片状态、学习权重、**「别找」词账本**一起回滚。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_resource_manager.config import AppConfig            # noqa: E402
from media_resource_manager.db import Database                 # noqa: E402
from media_resource_manager import feedback as fb              # noqa: E402
from media_resource_manager.models import ImageRecord, ReviewDecision  # noqa: E402
from media_resource_manager.repo import get_image, upsert_image  # noqa: E402
from media_resource_manager.review import reasons as R         # noqa: E402
from media_resource_manager.review.queue import ReviewQueue    # noqa: E402


@pytest.fixture()
def env(tmp_path):
    cfg = AppConfig(db_path=tmp_path / "m.db", library_dir=tmp_path / "lib",
                    rules_dir=tmp_path / "rules", isolate_dir=tmp_path / "iso",
                    log_dir=tmp_path / "logs", review_dir=tmp_path / "rv")
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    yield db, cfg
    db.close()


def _img(db, tmp_path, name="a.jpg", url="u1", tags=("猫",), reason=""):
    p = tmp_path / name
    p.write_bytes(b"x" * 10)
    rec = ImageRecord(id=None, source_url=url, task_id=1, path=str(p),
                      tags=list(tags), needs_review=bool(reason),
                      in_quarantine=bool(reason), review_reason=reason)
    return upsert_image(db, rec)


# ---------------------------------------------------------------- 1 写库

def test_column_exists(env):
    db, _ = env
    cols = {c["name"] for c in db._conn.execute("PRAGMA table_info(images)")}
    assert "review_reason" in cols


def test_empty_reason_does_not_overwrite(env, tmp_path):
    db, _ = env
    iid = _img(db, tmp_path, reason="AI 拿不准：分数正好卡在阈值附近")
    rec = get_image(db, iid)
    rec.review_reason = ""          # 别的路径 upsert 同一行
    upsert_image(db, rec)
    assert get_image(db, iid).review_reason == \
        "AI 拿不准：分数正好卡在阈值附近"


def test_new_reason_overwrites(env, tmp_path):
    db, _ = env
    iid = _img(db, tmp_path, reason="旧原因")
    rec = get_image(db, iid)
    rec.review_reason = "新原因"
    upsert_image(db, rec)
    assert get_image(db, iid).review_reason == "新原因"


# ---------------------------------------------------------------- 2 翻译

@pytest.mark.parametrize("note,expect", [
    ("ai-exclude uncertain=cosplay 真人 score=0.38", "排除词"),
    ("ai-exclude hit=灌篮高手 score=0.71", "命中「别找」的排除词"),
    ("ai uncertain for ['猫'] (best=猫 0.42, mode=any)", "拿不准"),
    ("ai: no match for ['猫'] (best=动物 0.21, mode=all)", "没匹配上「要找」"),
    ("ai preference-down for ['猫'] (best=猫 0.50, pref_eff=0.28)",
     "判过这类不喜欢"),
    ("ai 细分层未命中 for ['动漫'] (levels=2, best=动漫 0.55, thr=0.35)",
     "细分词还没对上"),
    ("4194304 > 2097152", "文件太大"),
    ("grayscale/keyword", "命中黑名单规则"),
])
def test_humanize_covers_engine_notes(note, expect):
    out = R.humanize(note)
    assert expect in out, f"{note!r} → {out!r} 没翻成含「{expect}」"


def test_humanize_unknown_and_empty():
    assert R.humanize("") == ""
    assert R.humanize("something weird") == "something weird"


# ---------------------------------------------------------------- 3 队列入口

def test_sample_and_smart_set_reason(env, tmp_path):
    db, cfg = env
    for i in range(6):
        p = tmp_path / f"s{i}.jpg"
        p.write_bytes(b"x" * 10)
        upsert_image(db, ImageRecord(id=None, source_url=f"u{i}", task_id=1,
                                     path=str(p), in_quarantine=True,
                                     score=0.3 + i * 0.01))
    q = ReviewQueue(db, isolate_dir=cfg.isolate_dir, cfg=cfg)
    ids = q.sample_quarantine(3)
    for iid in ids:
        assert get_image(db, iid).review_reason == R.SAMPLE
    ids2 = q.smart_sample(2, task_id=1)
    for iid in ids2:
        assert get_image(db, iid).review_reason == R.SMART


# ---------------------------------------------------------------- 4 撤销回滚

def test_undo_rolls_back_feedback_ledger(env, tmp_path):
    db, cfg = env
    iid = _img(db, tmp_path, reason="AI 拿不准")
    q = ReviewQueue(db, isolate_dir=cfg.isolate_dir, cfg=cfg)

    def learn(kind, tags):
        fb.record_rejection(db, tags, "dislike")

    q.apply(iid, ReviewDecision.DISLIKE, on_learn=learn)
    assert "猫" in {x["word"] for x in fb.list_all(db)}
    msg = q.undo()
    assert msg and "已撤销" in msg
    assert "猫" not in {x["word"] for x in fb.list_all(db)}, \
        "撤销判退后「别找」词必须一起退回（否则 Ctrl+Z 名不副实）"
    r = get_image(db, iid)
    assert r.needs_review and r.review_reason


def test_rollback_only_touches_diff_words(env, tmp_path):
    """并发安全：快照后别的词新加，撤销本次不该抹掉它。"""
    db, cfg = env
    iid = _img(db, tmp_path, tags=("猫",), reason="r")
    q = ReviewQueue(db, isolate_dir=cfg.isolate_dir, cfg=cfg)
    q.apply(iid, ReviewDecision.DISLIKE,
            on_learn=lambda k, t: fb.record_rejection(db, t, "dislike"))
    # 撤销前先"并发"加另一个词
    fb.record_rejection(db, ["狗"], "dislike")
    q.undo()
    words = {x["word"] for x in fb.list_all(db)}
    assert "猫" not in words
    assert "狗" in words, "别人新加的词不该被这次撤销连带删掉"


# ---------------------------------------------------------------- 5 网页接口

def _web(env, tmp_path):
    from media_resource_manager import webui

    class H(webui._Handler):
        """只测业务方法：不走真 socket（与 test_r48 同款替身）。"""

        def __init__(self, cfg, db):
            self._cfg = cfg
            self._db = db

        @property
        def cfg(self):
            return self._cfg

        @property
        def db(self):
            return self._db

    db, cfg = env
    return H(cfg, db)


def test_web_pending_list_carries_reason(env, tmp_path):
    db, cfg = env
    h = _web(env, tmp_path)
    _img(db, tmp_path, reason="文件太大（3.0MB，超过上限 2.0MB）")
    r = h._api_images("pending", 0, 50)
    assert r["ok"] and r["items"], "待审核列表为空？"
    assert r["items"][0]["reason"].startswith("文件太大")


def test_web_gallery_no_reason_field(env, tmp_path):
    db, cfg = env
    h = _web(env, tmp_path)
    iid = _img(db, tmp_path, reason="x")
    db.execute("UPDATE images SET needs_review=0, in_quarantine=0 WHERE id=?",
               (iid,))
    r = h._api_images("gallery", 0, 50)
    assert r["items"] and not r["items"][0]["reason"], \
        "非待审核视图不该带送审原因"


def test_web_move_sets_manual_reason(env, tmp_path):
    db, cfg = env
    h = _web(env, tmp_path)
    iid = _img(db, tmp_path, reason="")
    r = h._api_image_action({"ids": [iid], "act": "move"})
    assert r["ok"] and r["done"] == 1
    assert get_image(db, iid).needs_review
    assert get_image(db, iid).review_reason == R.MOVE_MANUAL


def test_web_dislike_reports_learned(env, tmp_path):
    db, cfg = env
    h = _web(env, tmp_path)
    iid = _img(db, tmp_path, tags=["海边日落"], reason="AI 拿不准")
    db.execute("UPDATE images SET tags='[\"海边日落\"]' WHERE id=?", (iid,))
    r = h._api_image_action({"ids": [iid], "act": "dislike"})
    assert r["ok"]
    L = r.get("learned")
    assert L and L["total"] >= 1, f"判退要如实播报学到了什么：{r}"
    assert "海边日落" in {x["word"] for x in fb.list_all(db)}


# ---------------------------------------------------------------- 6 桌面护栏

def test_desktop_grid_card_shows_reason():
    src = Path(__file__).resolve().parents[1] / \
        "media_resource_manager/gui/review_tab.py"
    t = src.read_text(encoding="utf-8")
    assert "card_base_and_tip" in t
    assert "送审原因" in t
    # 网格卡片与预标刷新共用同一个渲染函数（否则刷徽标会把原因刷丢）
    assert t.count("= card_base_and_tip(") >= 2


def test_desktop_lightbox_learns_inside_undo_transaction():
    src = Path(__file__).resolve().parents[1] / \
        "media_resource_manager/gui/review_tab.py"
    t = src.read_text(encoding="utf-8")
    assert "on_learn=_lb_learn" in t, \
        "灯箱判定必须把学习交给 queue.apply 的回调（快照后执行才可撤销）"
    assert ".queue.apply(iid, decision, on_learn=_lb_learn)" in t


def test_reasons_module_has_single_source_of_fixed_texts():
    # 固定场景文案只允许在 reasons.py 有一份出处
    import re
    root = Path(__file__).resolve().parents[1] / "media_resource_manager"
    for f in (root / "webui.py", root / "engine.py",
              root / "review" / "queue.py"):
        t = f.read_text(encoding="utf-8")
        for phrase in ("AI 自动隔离过的图，随机抽检拉进来复核",
                       "你手动把它移进了待审核"):
            assert phrase not in t or f.name == "reasons.py", \
                f"{f.name} 手打了固定文案，应引用 reasons 常量"
