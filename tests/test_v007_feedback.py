"""V0.0.7：验收「把下错的图拉黑 → 软件真的学会躲开这类图」。

用户最初讲这个软件时说的最后一步：偶尔下错了，把这张图放进黑名单，
软件又学习一次，越用越准。以前拉黑只写一条负权重，可「下次别找什么」
那张词表根本不收模型自动认出来的标签（这是防止模型认错封死主题的防线，
必须保留），于是学的那笔权重永远没机会在采集时生效 —— 最后一环是断的。

这组测试钉住补齐后的行为：
  · 拒收会记账，且只记「明确不要」的动作；
  · 一次只软拦（送审核），两次才硬拦（下载前就挡）；
  · 用户亲手填的「要找什么」永远不会被反馈词封死；
  · 账本看得见、删得掉、清得空；
  · 学习开关关着，拉黑照样生效。
"""
from __future__ import annotations

from media_resource_manager import feedback as fb
from media_resource_manager import find_terms
from media_resource_manager.classify import preference as pref
from media_resource_manager.models import TaskConfig


def _task(**kw):
    base = dict(
        id=1, name="t", adapter_name="",
        entry_urls=["https://danbooru.donmai.us/"],
        ai_labels=["日本动漫"], keywords=[], blacklist=[],
        ai_blacklist=[], ai_enabled=False)
    base.update(kw)
    return TaskConfig(**base)


def test_reject_records_clean_words_only(tmp_db):
    """拉黑一张图 = 记下它身上的词；文件名/碎片/泛词不算一类图。"""
    r = fb.record_rejection(
        tmp_db, ["Cosplay 真人", "a", "IMG_1234.jpg", "", "general", "真人照"],
        "blacklist")
    assert r["ok"] and r["added"] == 2
    words = {x["word"] for x in fb.list_all(tmp_db)}
    assert "cosplay 真人" in words and "真人照" in words
    assert "a" not in words and "img_1234.jpg" not in words
    assert "general" not in words


def test_non_reject_actions_leave_ledger_alone(tmp_db):
    for act in ("like", "favorite", "restore", "skip"):
        assert not fb.record_rejection(tmp_db, ["猫"], act)["ok"]
    assert fb.list_all(tmp_db) == []


def test_first_hit_is_soft_second_hit_is_hard(tmp_db, tmp_config):
    """一次只观察，两次才升级成下载前就拦。"""
    fb.record_rejection(tmp_db, ["cosplay"], "dislike")
    t = find_terms.collect(tmp_db, _task(), tmp_config)
    assert "cosplay" not in [w.lower() for w in t.avoid]
    assert "cosplay" in [w.lower() for w in t.soft_avoid]

    r = fb.record_rejection(tmp_db, ["cosplay"], "blacklist")
    assert "cosplay" in r["hardened"]
    t = find_terms.collect(tmp_db, _task(), tmp_config)
    assert "cosplay" in [w.lower() for w in t.avoid]
    assert "cosplay" not in [w.lower() for w in t.soft_avoid]
    assert "cosplay" in [w.lower() for w in t.avoid_feedback]


def test_hard_feedback_really_blocks_before_download(tmp_db, tmp_config):
    """硬反馈词必须真的让「下载前比对」判成不抓，不能只是躺在看板里。"""
    fb.record_rejection(tmp_db, ["cosplay"], "purge")
    fb.record_rejection(tmp_db, ["cosplay"], "dislike")
    t = find_terms.collect(tmp_db, _task(), tmp_config)
    verdict, _why = find_terms.prefilter(t, "https://site/img/123.jpg", "Cosplay 写真")
    assert verdict == "skip"


def test_hand_typed_find_word_is_never_frozen(tmp_db, tmp_config):
    """模型认错把用户手填的主题词打在拒收图上时，不许把整个方向封死。"""
    for _ in range(3):
        fb.record_rejection(tmp_db, ["日本动漫"], "blacklist")
    t = find_terms.collect(tmp_db, _task(), tmp_config)
    assert "日本动漫" not in [w.lower() for w in t.avoid]
    assert "日本动漫" in [w.lower() for w in t.want]
    assert "日本动漫" not in [w.lower() for w in t.soft_avoid]


def test_prefilter_regex_is_case_insensitive(tmp_db, tmp_config):
    """用户写 /Cosplay/ 时，网址里小写 cosplay 也要拦住。"""
    t = find_terms.collect(tmp_db, _task(blacklist=["/Cosplay/"]), tmp_config)
    verdict, _ = find_terms.prefilter(t, "https://site/cosplay/1.jpg")
    assert verdict == "skip"


def test_remove_and_clear(tmp_db):
    fb.record_rejection(tmp_db, ["cosplay", "真人照"], "dislike")
    assert len(fb.list_all(tmp_db)) == 2
    assert fb.remove(tmp_db, "COSPLAY") is True
    assert [x["word"] for x in fb.list_all(tmp_db)] == ["真人照"]
    assert fb.clear(tmp_db) == 1
    assert fb.list_all(tmp_db) == []


def test_learning_switch_off_does_not_disable_rejection(tmp_db):
    """学习开关只管口味权重；亲手点拉黑表达的「别再下」不该被关掉。"""
    pref.set_learning_enabled(False)
    try:
        assert pref.PreferenceLearner(tmp_db, 0).learn_action(
            "blacklist", ["cosplay"], source="test") is False
    finally:
        pref.set_learning_enabled(True)
    assert [x["word"] for x in fb.list_all(tmp_db)] == ["cosplay"]


def test_global_terms_exposes_feedback_for_dashboard(tmp_db, tmp_config):
    fb.record_rejection(tmp_db, ["cosplay"], "dislike")
    fb.record_rejection(tmp_db, ["cosplay"], "dislike")
    fb.record_rejection(tmp_db, ["真人照"], "dislike")
    g = find_terms.global_terms(tmp_db, tmp_config)
    assert "cosplay" in [w.lower() for w in g.avoid]
    assert "真人照" in [w.lower() for w in g.soft_avoid]


def test_web_and_desktop_use_the_same_ledger(tmp_db):
    """网页端点与桌面看板都调同一个模块，不另写一份口径。"""
    import inspect
    from pathlib import Path
    root = Path(inspect.getfile(fb)).parent
    web = (root / "webui.py").read_text(encoding="utf-8")
    board = (root / "gui" / "learning_board.py").read_text(encoding="utf-8")
    assert "/api/feedback" in web and "fb.list_all(self.db)" in web
    assert "feedback as _fb" in board and "_fb.list_all(self.db)" in board
