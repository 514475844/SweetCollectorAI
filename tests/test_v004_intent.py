"""V0.0.4：按用户当初设计这个软件的初衷验收。

用户原话意思：填「日本动漫」这个大类，用黑名单把「灌篮高手」挡掉；
把本地存的哆啦A梦图导进来，软件要从中学会「我喜欢哆啦A梦」；
然后拿学到的词 + 手填的词 + 给的网站，去站上找对味的图下载；
下错了把那张放进黑名单，软件再学一次。

这几条测试钉死的正是以前断掉的那一环：学到的词必须变成「要找什么」，
并且黑名单必须在下载前就拦住。
"""
from __future__ import annotations

import json

from media_resource_manager import find_terms
from media_resource_manager.learn_imported import MIN_HITS, learn_from_imported
from media_resource_manager.models import TaskConfig
from media_resource_manager.repo import (PREF_ORIGIN_IMPORT,
                                         set_preference,
                                         get_preferences)


def _task(**kw):
    base = dict(
        id=1, name="t", adapter_name="",
        entry_urls=["https://danbooru.donmai.us/"],
        ai_labels=["日本动漫"], keywords=[], blacklist=["灌篮高手"],
        ai_blacklist=[], ai_enabled=False)
    base.update(kw)
    return TaskConfig(**base)


def test_learned_word_becomes_a_find_word(tmp_db, tmp_config):
    """从本地图库学会「哆啦A梦」之后，这个词必须出现在「要找什么」里。"""
    set_preference(tmp_db, 0, "哆啦A梦", 2.0, origin=PREF_ORIGIN_IMPORT)
    terms = find_terms.collect(tmp_db, _task(), tmp_config)
    lowered = [w.lower() for w in terms.want]
    assert "日本动漫" in terms.want          # 手填的
    # 口味账为了读写对得上会统一小写（哆啦A梦 → 哆啦a梦），比对时忽略大小写
    assert "哆啦a梦" in lowered              # 学到的（以前完全没有）
    assert "灌篮高手" in terms.avoid         # 手填黑名单
    assert "哆啦a梦" in [w.lower() for w in terms.learned]


def test_liked_word_wins_over_manual_words(tmp_db, tmp_config):
    """拉黑过的词优先：同一个词既在要找、又在别找时，以别找为准。"""
    set_preference(tmp_db, 0, "cosplay", 2.0, origin=PREF_ORIGIN_IMPORT)
    t = _task(ai_blacklist=["cosplay"])
    terms = find_terms.collect(tmp_db, t, tmp_config)
    assert "cosplay" in terms.avoid
    assert "cosplay" not in terms.want


def test_blacklist_blocks_before_download(tmp_db, tmp_config):
    """标题/相册里写了「灌篮高手」→ 下载前就别抓。"""
    set_preference(tmp_db, 0, "哆啦A梦", 2.0, origin=PREF_ORIGIN_IMPORT)
    terms = find_terms.collect(tmp_db, _task(), tmp_config)
    verdict, why = find_terms.prefilter(
        terms, "https://site/123.jpg", "灌篮高手 图集合集", "")
    assert verdict == "skip", why
    assert "灌篮高手" in why


def test_wanted_word_matches_and_numbers_are_not_dropped(tmp_db, tmp_config):
    """命中要找的词算明确对味；纯数字文件名看不出来 → 放行交给看图判定。

    若「没提到词就跳过」，整站文件名是数字的图会全被判掉，一张都收不到。
    """
    set_preference(tmp_db, 0, "哆啦A梦", 2.0, origin=PREF_ORIGIN_IMPORT)
    terms = find_terms.collect(tmp_db, _task(), tmp_config)
    assert find_terms.prefilter(terms, "https://site/哆啦A梦_01.jpg")[0] == "match"
    assert find_terms.prefilter(terms, "https://site/12345.jpg")[0] == "unknown"


def test_learned_negative_word_becomes_hard_exclusion(tmp_db, tmp_config):
    """用户明确拉黑过一次 → 下一轮采集下载前就硬拦。"""
    # 用户手填的负权词（不喜欢弹窗写的就是这种）
    tmp_db.execute(
        "INSERT INTO preferences(task_id,label,weight,seen,source) "
        "VALUES (0,?,?,?,?)",
        ("真人cos", -2.0, 1, "user_typed"))
    terms = find_terms.collect(tmp_db, _task(), tmp_config)
    assert "真人cos" in terms.avoid
    assert find_terms.prefilter(terms, "https://site/真人cos.jpg")[0] == "skip"


def test_search_entries_turn_words_into_site_search(tmp_db, tmp_config):
    """填整站首页时，按「要找什么」的词生成站内按词找图的入口。"""
    set_preference(tmp_db, 0, "哆啦A梦", 2.0, origin=PREF_ORIGIN_IMPORT)
    terms = find_terms.collect(tmp_db, _task(), tmp_config)
    made, note = find_terms.search_entries(
        ["https://danbooru.donmai.us/"], terms.want)
    assert len(made) >= 2
    assert any("tags=" in u for u in made)
    assert note
    # 用户给了具体帖子网址 → 一个字都不许改
    concrete = ["https://danbooru.donmai.us/posts/12345"]
    same, _ = find_terms.search_entries(concrete, terms.want)
    assert same == concrete


def test_import_learns_single_label_even_if_learning_switch_off(tmp_db):
    """导入一张只带一个词的图也要学会；导入学习不受学习开关管。"""
    assert MIN_HITS == 1
    tmp_db.execute(
        "INSERT INTO images(source_url, task_id, tags, path) "
        "VALUES (?,?,?,?)",
        ("local://doraemon.jpg", 1,
         json.dumps(["哆啦A梦"], ensure_ascii=False), "F:/原图/doraemon.jpg"))
    out = learn_from_imported(tmp_db, 1)
    assert out["ok"] is True, out
    assert out["labels"] >= 1
    prefs = get_preferences(tmp_db)
    assert prefs.get("哆啦a梦", 0) > 0
    row = tmp_db.query("SELECT source FROM preferences WHERE label=?",
                       ("哆啦a梦",))
    assert row and row[0]["source"] == PREF_ORIGIN_IMPORT
    # 重复导入不把权重堆高（幂等）
    again = learn_from_imported(tmp_db, 1)
    assert again["skipped"] is True
    assert get_preferences(tmp_db)["哆啦a梦"] == prefs["哆啦a梦"]


def test_task_form_no_longer_wipes_blacklist():
    """桌面保存任务不许再把用户填的关键词/黑名单抹成空。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "media_resource_manager" / "gui" / "main_window.py")
    text = src.read_text(encoding="utf-8")
    assert "t.keywords = []" not in text
    assert "t.blacklist = []" not in text
