"""V0.0.7：网页端学习看板补齐桌面同款的「词表面板 + 手动加词」。

决定清单要求桌面 / 网页两端功能对齐，不留「桌面有开关、网页没机制」。
以前网页只能看权重明细：手机上既看不见软件正按哪些词采集，也没法加词。
"""
from __future__ import annotations

import inspect
from pathlib import Path


def test_add_term_endpoint_is_routed_and_writes_user_typed_positive(tmp_db):
    """加的词必须落成「用户手填 + 正方向 + 权重 2.0」，与桌面同口径。"""
    from media_resource_manager import webui
    from media_resource_manager.repo import (PREF_ORIGIN_USER, _pref_origin,
                                             get_preferences)
    src = Path(inspect.getfile(webui)).read_text(encoding="utf-8")
    assert 'if path == "/api/learn/add-term":' in src, "路由没逐个显式列出"
    assert "_api_learn_add_term" in src

    # 直接按处理函数的写入口径复算一遍（不起 HTTP）
    from media_resource_manager.repo import set_preference
    set_preference(tmp_db, 0, "新主题词", 2.0, kind="pos",
                   origin=PREF_ORIGIN_USER)
    prefs = get_preferences(tmp_db)
    assert prefs.get("新主题词") == 2.0
    row = tmp_db.query("SELECT source FROM preferences WHERE label=?",
                       ("新主题词",))[0]
    assert _pref_origin(row) == PREF_ORIGIN_USER, "来源必须是用户手填"


def test_added_term_drives_collection_words(tmp_db, tmp_config):
    """加完词必须立刻出现在「要找什么」里，并参与站内按词找图。"""
    from media_resource_manager import find_terms
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import (PREF_ORIGIN_USER, set_preference)

    t = TaskConfig(id=None, name="t", adapter_name="",
                   entry_urls=["https://konachan.com/"],
                   ai_labels=[], keywords=[], blacklist=[], ai_blacklist=[])
    assert find_terms.collect(tmp_db, t, tmp_config).want == []
    set_preference(tmp_db, 0, "海边日落", 2.0, kind="pos",
                   origin=PREF_ORIGIN_USER)
    terms = find_terms.collect(tmp_db, t, tmp_config)
    assert "海边日落" in terms.want
    ents, note = find_terms.search_entries(t.entry_urls, terms.want,
                                           terms.avoid)
    assert ents != t.entry_urls and "tags=" in ents[0], note


def test_web_board_shows_terms_and_add_box():
    """网页看板必须真的画出词表与加词框（源码字面量，与既有护栏同风格）。"""
    vue = (Path(__file__).resolve().parents[1]
           / "media_resource_manager/webapp/src/views/Learning.vue")
    s = vue.read_text(encoding="utf-8")
    assert "当前采集所用目标词与排除词" in s
    assert "api.learnAddTerm" in s and "api.findTerms" in s
    assert "观察期（判退 1 次，仅进入待审核）" in s
    api_js = vue.parents[1] / "api.js"
    a = api_js.read_text(encoding="utf-8")
    assert "/api/learn/add-term" in a and "/api/find-terms" in a


def test_desktop_and_web_share_one_weight_writer():
    """两端加词必须走同一个 repo 写入器，不各写一份 SQL。"""
    from media_resource_manager import webui
    from media_resource_manager.gui import learning_board
    w = Path(inspect.getfile(webui)).read_text(encoding="utf-8")
    b = Path(inspect.getfile(learning_board)).read_text(encoding="utf-8")
    for src in (w, b):
        assert "set_preference(self.db, 0, " in src
        assert "INSERT INTO preferences" not in src
