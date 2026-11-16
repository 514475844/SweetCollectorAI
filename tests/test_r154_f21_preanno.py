"""R154-F21 AI 预标注批量确认护栏（2026-10-22 R216 棒）。

订单原文（R154_FEATURES_ALL.md §F21）：
  · 审核页顶部开关：CLIP 对当前页预判 top-1（喜欢倾向/不喜欢倾向）；
  · 界面显示预标结果与置信度，人工只改错（「全部采纳本页预标」按钮）；
  · 预判仅在本地跑，不写学习权重，采纳后才写。

分层钉契约（与 F17/F19 同款三层）：

  L1 纯函数（classify/preannotate.py，零 Qt 零库）：
    - parse_tags 容错：list / JSON / 老库逗号串 / 裸词 / None
    - predict 口径 = 命中标签权重均值（与 PreferenceLearner.score 同款）；
      符号定倾向；无标签/零权重 → 不表态（None，不冒充 0 分）
    - 预判只读：weights / tags 进出不被改（纯展示层的底线）
    - badge/detail：不表态显示「预判 —」绝不显示 0%

  L2 源码判据（防「功能被顺手删」，剥注释防文档骗判据）：
    - 顶部开关「AI 预标」+「全部采纳本页预标」按钮 + 两条信号接线
    - reload 里预判缓存重算 + 网格落定后刷徽标
    - 采纳路径零额外写入：adopt/预判两块源码里不许出现
      set_preference / PreferenceLearner / record_learn_sample（写权重的
      唯一咽喉在 _batch_decide_ids 的学习工厂，R166 契约）
    - _batch_decide_ids 把 apply_batch 的张数 return 回去（采纳如实记账）
    - preannotate.py 不 import Qt / 不 import db（纯逻辑层定位不许漂）

  L3 真审核页行为（离屏真建 ReviewTab + 真库 + 真文件）：
    - 开关开 → 网格条目出现「预判 喜欢/不喜欢 —」徽标；关 → 干净恢复
    - 采纳：喜欢倾向 → LIKE 判定（出队列+收藏位）、不喜欢 → DISLIKE
      （进隔离区）、不表态 → 原地不动；两个撤销事务（undo_depth==2）
    - 采纳后权重才动：like 方向标签权重上升、dislike 方向下降
    - 预判路径零写入：没有偏好/没有判定时，预判前后库一字不改
    - 开关没开就点采纳 → 只提示不动库（QMessageBox 打桩）

变异自检见 `.workbuddy/_r216_mut_f21.py`（R73 纪律：判据必须变异确认会红）。
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from media_resource_manager.classify import preannotate
from media_resource_manager.classify.preannotate import (
    PreAnnotation, badge, detail, parse_tags, predict)

RT_PATH = os.path.join(_ROOT, "media_resource_manager", "gui",
                       "review_tab.py").replace("\\", "/")
PA_PATH = os.path.join(_ROOT, "media_resource_manager", "classify",
                       "preannotate.py").replace("\\", "/")
RT_SRC = open(RT_PATH, encoding="utf-8").read()
PA_SRC = open(PA_PATH, encoding="utf-8").read()


# ═══════════════════════════ 工具 ═══════════════════════════

def _code_only(src: str) -> str:
    """去掉注释行再断言 —— 注释/文档里常含被修掉的关键字，不剥会
    「自己写的文档自己红」（本仓 R69/R73/R84 三次实测）。"""
    return "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))


def _block(src: str, name: str) -> str:
    """取类中某个方法的源码块（到下一个同级 def 为止）。"""
    i = src.index(f"def {name}(")
    j = src.find("\n    def ", i + 1)
    return src[i:j if j > 0 else len(src)]


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ═══════════════════════ L1 · 纯函数 ═══════════════════════

def test_parse_tags_tolerates_all_legacy_shapes():
    assert parse_tags(["a", "b"]) == ["a", "b"]
    assert parse_tags('["a", "b"]') == ["a", "b"]
    assert parse_tags("白丝,黑丝") == ["白丝", "黑丝"]   # 老库逗号裸串
    assert parse_tags("cat") == ["cat"]                   # 单个裸词
    assert parse_tags(None) == []
    assert parse_tags("") == []
    assert parse_tags('["a", "", " b "]') == ["a", "b"]   # 空档剔除


def test_predict_sign_maps_to_tendency():
    w = {"cat": 1.0, "dog": -1.0}
    assert predict(1, w, ["cat"]).tendency == "like"
    assert predict(2, w, ["dog"]).tendency == "dislike"


def test_predict_no_signal_is_none_not_zero():
    """没标签 / 没权重 / 均值恰为 0 → 不表态；与「算出 0 分」必须分开。"""
    assert predict(1, {}, ["cat"]).tendency is None
    assert predict(2, {"cat": 1.0}, []).tendency is None
    assert predict(3, {"cat": 1.0, "dog": -1.0},
                   ["cat", "dog"]).tendency is None


def test_predict_score_is_mean_over_all_tags():
    """口径 = 命中权重均值，没命中的标签按 0 计入分母（与
    PreferenceLearner.score 同款）；1.0/2 = 0.5。"""
    pre = predict(1, {"cat": 1.0}, ["cat", "bird"])
    assert pre.score == pytest.approx(0.5)
    assert pre.matched == (("cat", 1.0),)


def test_predict_confidence_display_only_and_capped():
    """|score|/2 封顶 1.0；单调不减；纯展示字段不参与任何门槛判断。"""
    assert predict(1, {"a": -2.0}, ["a"]).confidence == pytest.approx(1.0)
    assert predict(2, {"a": 1.0}, ["a"]).confidence == pytest.approx(0.5)
    c1 = predict(3, {"a": 0.4}, ["a"]).confidence
    c2 = predict(4, {"a": 0.8}, ["a"]).confidence
    assert c2 >= c1


def test_predict_is_read_only():
    """预判只读：weights / tags 进出不被改 —— 展示层不许有副作用。"""
    w = {"cat": 1.0}
    tags = ["cat", " bird "]
    w_before, tags_before = dict(w), list(tags)
    predict(1, w, tags)
    assert w == w_before and tags == tags_before


def test_badge_none_tendency_is_dash_never_zero_percent():
    """「没算过/不表态」显示「预判 —」；绝不显示 0%（0% 会被读成
    「算过了，五五开」——None 与 0 必须分开，label_bridge 同款铁律）。"""
    assert badge(None) == "预判 —"
    assert badge(predict(1, {}, ["x"])).startswith("预判 —")
    assert "0%" not in badge(predict(1, {}, ["x"]))


def test_badge_shows_tendency_and_percent():
    pre = PreAnnotation(image_id=1, tendency="like", score=1.24,
                        confidence=0.62, matched=(("cat", 1.0),))
    assert badge(pre) == "预判 喜欢 62%"
    pre_d = PreAnnotation(image_id=2, tendency="dislike", score=-1.56,
                          confidence=0.78, matched=(("dog", -1.0),))
    assert badge(pre_d) == "预判 不喜欢 78%"


def test_detail_names_labels_and_states_the_contract():
    pre = predict(1, {"cat": 1.0, "dog": -0.5}, ["cat", "dog"])
    d = detail(pre)
    assert "cat" in d and "dog" in d and "+1" in d
    assert "采纳" in d, "明细必须写明「采纳才写权重」这条契约"
    assert "不表态" in detail(predict(2, {}, ["x"]))


# ═══════════════════════ L2 · 源码判据 ═══════════════════════

def _rt_code() -> str:
    return _code_only(RT_SRC)


def test_topbar_has_switch_and_adopt_button_wired():
    """顶部开关 + 采纳按钮都建了、信号都接了（缺一就是功能被拔）。"""
    build = _block(_rt_code(), "_build")
    assert '"AI 预标"' in build
    assert '"全部采纳本页预标"' in build
    assert "cb_preanno.toggled.connect" in build
    assert "b_adopt_preanno.clicked.connect" in build


def test_reload_recomputes_cache_and_applies_badges():
    reload_blk = _block(_rt_code(), "reload")
    assert "_preanno_compute_for_ids" in reload_blk
    assert "_apply_preanno_to_grid" in reload_blk


def test_adopt_and_predict_paths_have_zero_writes():
    """「预判不写权重、采纳后才写」的源码级钉死：采纳/预判两块代码里
    不许出现任何学习写入器 —— 写权重的唯一咽喉是 _batch_decide_ids 的
    学习工厂（R166 契约）。"""
    for fn in ("adopt_preanno", "_preanno_compute_for_ids",
               "_preanno_weights", "_apply_preanno_to_grid",
               "_on_preanno_toggled"):
        blk = _block(_rt_code(), fn)
        for writer in ("set_preference", "PreferenceLearner",
                       "record_learn_sample", ".learn("):
            assert writer not in blk, f"{fn} 里不许出现 {writer}"


def test_adopt_reuses_batch_decide_for_both_tendencies():
    """采纳必须走 _batch_decide_ids（学习/断点/补满/撤销的唯一咽喉），
    且喜欢、不喜欢两组都要落。"""
    blk = _block(_rt_code(), "adopt_preanno")
    assert "_batch_decide_ids(like_ids, ReviewDecision.LIKE)" in blk
    assert "_batch_decide_ids(dislike_ids" in blk
    assert "ReviewDecision.DISLIKE" in blk


def test_batch_decide_returns_applied_count():
    blk = _block(_rt_code(), "_batch_decide_ids")
    assert "return n" in blk, "采纳记账要拿 apply_batch 的真实张数"


def test_preannotate_module_stays_pure():
    """纯逻辑层定位不许漂：不 import Qt、不 import db（可离线单测）。"""
    assert "PySide6" not in PA_SRC and "QtWidgets" not in PA_SRC
    assert "from ..db" not in PA_SRC and "Database" not in PA_SRC


def test_preannotate_badge_is_used_in_grid_render():
    """网格文案必须经 preannotate.badge（不许在界面层另写一套口径）。"""
    blk = _block(_rt_code(), "_apply_preanno_to_grid")
    assert "preannotate.badge(" in blk
    assert "preannotate.detail(" in blk


# ═══════════════════════ L3 · 真审核页行为 ═══════════════════════

@pytest.fixture
def env(tmp_config):
    """真库 + 4 张图：like 倾向(cat) / dislike 倾向(dog) / 无信号(bird) / 无标签。"""
    from media_resource_manager.db import Database
    from media_resource_manager.models import ImageRecord
    from media_resource_manager.repo import upsert_image
    _app()
    db = Database(tmp_config.db_path)
    ids = {}
    for key, tags in (("like", ["cat"]), ("dislike", ["dog"]),
                      ("neutral", ["bird"]), ("notags", [])):
        p = tmp_config.library_dir / f"f21_{key}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
        ids[key] = upsert_image(db, ImageRecord(
            id=None, source_url=f"https://x.com/{key}.jpg", task_id=1,
            path=str(p), tags=list(tags), in_quarantine=True,
            needs_review=True))
    return tmp_config, db, ids


def _mk_tab(env):
    from media_resource_manager.gui.review_tab import ReviewTab
    tmp_config, db, ids = env
    tab = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    tab.reload()
    return tab


def _item_text(tab, iid):
    from PySide6.QtCore import Qt
    for i in range(tab.grid.count()):
        it = tab.grid.item(i)
        if it.data(Qt.ItemDataRole.UserRole) == iid:
            return it.text()
    return None


def _pending(env):
    _cfg, db, _ids = env
    return db.query("SELECT id FROM images WHERE needs_review=1 "
                    "AND in_quarantine=1") or []


def _count(db, table):
    rows = db.query(f"SELECT COUNT(*) AS c FROM {table}") or []
    return int(rows[0]["c"]) if rows else 0


def test_toggle_shows_badges_and_restores_clean_text(env):
    from media_resource_manager.repo import set_preference
    tmp_config, db, ids = env
    set_preference(db, 0, "cat", 1.0, kind="pos")
    set_preference(db, 0, "dog", -1.0, kind="neg")
    tab = _mk_tab(env)
    try:
        tab.cb_preanno.setChecked(True)
        t_like = _item_text(tab, ids["like"])
        t_dis = _item_text(tab, ids["dislike"])
        t_neu = _item_text(tab, ids["neutral"])
        assert "预判 喜欢" in t_like and "%" in t_like
        assert "预判 不喜欢" in t_dis
        assert "预判 —" in t_neu, "无信号图显示「预判 —」，不冒充 0%"
        tab.cb_preanno.setChecked(False)
        t0 = _item_text(tab, ids["like"])
        assert "预判" not in t0, "开关关掉必须干净恢复基础文案"
    finally:
        tab.deleteLater()


def test_preanno_compute_matches_expected_tendencies(env):
    tmp_config, db, ids = env
    from media_resource_manager.repo import set_preference
    set_preference(db, 0, "cat", 1.0, kind="pos")
    set_preference(db, 0, "dog", -1.0, kind="neg")
    tab = _mk_tab(env)
    try:
        cache = tab._preanno_cache
        assert cache[ids["like"]].tendency == "like"
        assert cache[ids["dislike"]].tendency == "dislike"
        assert cache[ids["neutral"]].tendency is None
        assert cache[ids["notags"]].tendency is None
    finally:
        tab.deleteLater()


def test_adopt_applies_like_and_dislike_leaves_neutral(env, monkeypatch):
    """核心行为：一键采纳 = 喜欢→LIKE、不喜欢→DISLIKE、不表态→不动；
    两个撤销事务；采纳后权重才动（预判本身零写入）。"""
    from PySide6.QtCore import Qt
    from media_resource_manager.gui import review_tab as rt
    from media_resource_manager.repo import get_preferences, set_preference
    tmp_config, db, ids = env
    set_preference(db, 0, "cat", 1.0, kind="pos")
    set_preference(db, 0, "dog", -1.0, kind="neg")
    w_before = dict(get_preferences(db, 0))
    tab = _mk_tab(env)
    box_calls = []
    monkeypatch.setattr(rt.QMessageBox, "information",
                        lambda *a, **k: box_calls.append(a))
    try:
        tab.cb_preanno.setChecked(True)
        tab.adopt_preanno()
        assert tab.queue.can_undo() and tab.queue.undo_depth() == 2, (
            "喜欢、不喜欢各成一个撤销事务")
        pend = {r["id"] for r in _pending(env)}
        assert ids["like"] not in pend and ids["dislike"] not in pend
        assert ids["neutral"] in pend, "不表态的图一律不动"
        assert ids["notags"] in pend
        # 采纳后才写权重：like 方向上升、dislike 方向下降（累加语义）
        w_after = get_preferences(db, 0)
        assert w_after["cat"] > w_before["cat"]
        assert w_after["dog"] < w_before["dog"]
        assert not box_calls, "有可采纳预判时不应弹提示框"
    finally:
        tab.deleteLater()


def test_predict_path_writes_nothing_to_db(env):
    """预判路径零写入：无偏好无判定的库，预判前后一字不改。"""
    tmp_config, db, ids = env
    from media_resource_manager.repo import get_preferences
    tab = _mk_tab(env)
    try:
        prefs_before, samples_before = _count(db, "preferences"), _count(db, "learn_samples")
        tab.cb_preanno.setChecked(True)     # 触发整条预判渲染路径
        assert all(p.tendency is None for p in tab._preanno_cache.values())
        assert _count(db, "preferences") == prefs_before
        assert _count(db, "learn_samples") == samples_before
        assert get_preferences(db, 0) == {}
    finally:
        tab.deleteLater()


def test_adopt_without_switch_only_prompts(env, monkeypatch):
    from media_resource_manager.gui import review_tab as rt
    tmp_config, db, ids = env
    tab = _mk_tab(env)
    box_calls = []
    monkeypatch.setattr(rt.QMessageBox, "information",
                        lambda *a, **k: box_calls.append(a))
    try:
        assert tab.cb_preanno.isChecked() is False, "开关默认关（不影响现状）"
        tab.adopt_preanno()
        assert box_calls, "开关没开要提示，不能静默"
        assert len(_pending(env)) == 4, "什么都没判"
        assert tab.queue.can_undo() is False
    finally:
        tab.deleteLater()


def test_adopt_with_no_predictions_only_prompts(env, monkeypatch):
    """全页都不表态（无偏好权重）→ 只提示，不动任何图。"""
    from media_resource_manager.gui import review_tab as rt
    tmp_config, db, ids = env
    tab = _mk_tab(env)
    box_calls = []
    monkeypatch.setattr(rt.QMessageBox, "information",
                        lambda *a, **k: box_calls.append(a))
    try:
        tab.cb_preanno.setChecked(True)
        tab.adopt_preanno()
        assert box_calls, "没有可采纳预判要提示"
        assert len(_pending(env)) == 4
        assert tab.queue.can_undo() is False
    finally:
        tab.deleteLater()
