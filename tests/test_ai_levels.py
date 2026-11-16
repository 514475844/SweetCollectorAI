"""主题词一级/二级/三级分级（r29）的防回归测试。

规则（r29 定稿）：
  一级 = 入门票，决定「留 / 送审 / 丢」，逻辑跟以前一模一样；
  二级 + 三级 = 细分标签，只有一级命中了才参与：
    · 有细分词过阈值 → 入库，命中的细分词写进标签，最深命中当 {keyword}；
    · 一个都没过阈值 → 默认 **送审核区** 让人手点标签（方案2）；
      开严格模式（ai_label_strict）才当「不命中」直接隔离（方案1）。
  一级为空 / 没填二三级 → 行为跟 r29 之前完全一致（老任务零影响）。

这里还要锁一条铁律：细分词没出现在模型 top-K 里 = 「不知道」，
绝不能当成「不匹配」去 drop —— 宁可漏放不可误杀。
"""

import media_resource_manager

from media_resource_manager.engine import Engine, _best_score, _level_labels
from media_resource_manager.models import ImageRecord, TaskConfig


class _FakeClf:
    def __init__(self, pairs):
        self._pairs = pairs

    def classify(self, path):
        return self._pairs


def _eng(pairs):
    e = Engine(None, None)
    e._clf = _FakeClf(pairs)
    e._excl_clf = None
    return e


def _rec():
    return ImageRecord(id=None, source_url="u", task_id=1)


def _task(**kw):
    d = dict(id=None, name="t", adapter_name="s", ai_threshold=0.5,
             ai_match_mode="any")
    d.update(kw)
    return TaskConfig(**d)


# ---------------------------------------------------- 老行为零影响 --------
def test_no_deep_labels_behaves_exactly_like_before():
    """一级命中、没填二三级 → 跟 r29 之前一样 keep，标签只有一级相关的。"""
    e = _eng([("cat", 0.8), ("dog", 0.3)])
    rec = _rec()
    t = _task(ai_labels=["cat", "dog"])
    assert e._apply_ai(t, rec, "p.jpg")[0] == "keep"
    assert "cat" in rec.tags
    assert "dog" not in rec.tags


def test_deep_words_empty_never_review():
    """没填二三级词 → 一级命中就 straight 入库，不会平白多送审核区。"""
    e = _eng([("cat", 0.75)])          # 离阈值远一点，避开「拿不准」的 margin
    rec = _rec()
    t = _task(ai_labels=["cat"])
    assert e._apply_ai(t, rec, "p.jpg")[0] == "keep"


def test_level_labels_splits_the_three_levels():
    t = _task(ai_labels=["白丝"], ai_labels_l2=["jk"], ai_labels_l3=["黑长直"])
    l1, l2, l3 = _level_labels(t)
    assert (l1, l2, l3) == (["白丝"], ["jk"], ["黑长直"])

    old = _task(ai_labels=["白丝"])          # 老任务：没有 l2/l3
    assert _level_labels(old) == (["白丝"], [], [])


def test_best_score_takes_max_over_translations():
    """「白丝」翻译成 white stockings 后分数更高 → 认这个分数；翻译不在 top-K
    里就返回 0（=不知道），绝不当 0 分命中。"
    """
    score_of = {"白丝": 0.2, "white stockings": 0.66}
    assert _best_score(score_of, ["白丝"]) == ("白丝", 0.66)
    assert _best_score({"白丝": 0.2}, ["白丝"]) == ("白丝", 0.2)
    assert _best_score({}, ["白丝"]) == ("", 0.0)


# ------------------------------------- 1) 二级/三级命中 → 入库带标签 ----
def test_both_levels_hit_keeps_with_deep_tag():
    e = _eng([("cat", 0.8), ("kitten", 0.7)])
    rec = _rec()
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten"])
    assert e._apply_ai(t, rec, "p.jpg")[0] == "keep"
    assert "kitten" in rec.tags and "cat" in rec.tags


def test_third_level_hit_becomes_the_deepest_tag():
    """三级也命中时，最深命中的细分词写进标签（并当 {keyword} 子目录名）。"""
    e = _eng([("cat", 0.9), ("kitten", 0.7), ("tabby kitten", 0.66)])
    rec = _rec()
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten"], ai_labels_l3=["tabby kitten"])
    assert e._apply_ai(t, rec, "p.jpg")[0] == "keep"
    assert "tabby kitten" in rec.tags and "kitten" in rec.tags
    # 分子目录是「看 URL/相册文字里命中哪个词」：文字里同时出现时，越深越优先
    assert e._matched_keyword(t, "https://x/album/my tabby kitten/") == "tabby kitten"
    assert e._matched_keyword(t, "https://x/album/kitten only/") == "kitten"


def test_matched_keyword_prefers_the_deeper_hit():
    e = Engine(None, None)
    t = _task(ai_labels=["白丝"], ai_labels_l2=["jk uniform"])
    assert e._matched_keyword(t, "https://x/p/jk uniform/123") == "jk uniform"
    assert e._matched_keyword(t, "https://x/p/白丝/123") == "白丝"
    assert e._matched_keyword(t, "https://x/p/nothing") == ""


# --------------- 2) 只命中一级 → 送审核（方案2 默认，不误杀） ----------
def test_level1_only_goes_to_review_not_drop():
    e = _eng([("cat", 0.8), ("dog", 0.1)])
    rec = _rec()
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten"])
    state, note = e._apply_ai(t, rec, "p.jpg")
    assert state == "review"
    assert "level-1 only" in note


def test_level1_only_does_not_write_tags():
    """送审的图不该偷偷留着「命中」标签，避免它看起来像已经入库了。"""
    e = _eng([("cat", 0.8)])
    rec = _rec()
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten"])
    e._apply_ai(t, rec, "p.jpg")
    assert not (rec.tags or [])


def test_strict_mode_drops_instead_of_review():
    e = _eng([("cat", 0.8), ("dog", 0.1)])
    rec = _rec()
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten"], ai_label_strict=True)
    state, note = e._apply_ai(t, rec, "p.jpg")
    assert state == "drop"
    assert "no match" in note


def test_strict_mode_is_off_by_default():
    e = _eng([("cat", 0.8)])
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten"])
    assert not t.ai_label_strict
    assert e._apply_ai(t, _rec(), "p.jpg")[0] == "review"


def test_deep_word_outside_topk_is_never_treated_as_drop():
    """二级词压根没出现在模型返回里（top-10 截断）→ 必须送人工，不许 drop。"""
    e = _eng([("cat", 0.99)])
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten", "tabby"])
    assert e._apply_ai(t, _rec(), "p.jpg")[0] == "review"


# -------------------------------------------- 3) 一级没命中 → 老逻辑 ----
def test_level1_miss_drops_as_before():
    """一级没命中（模型给的所有候选都不过阈值）→ 老逻辑 drop。"""
    e = _eng([("dog", 0.2)])
    t = _task(ai_labels=["cat"], ai_labels_l2=["kitten"])
    assert e._apply_ai(t, _rec(), "p.jpg")[0] == "drop"


def test_no_level1_at_all_does_not_trigger_level_logic():
    """只填了三级词、一级留空：不该请来严格模式把全库干掉。"""
    e = _eng([("cat", 0.9)])
    t = _task(ai_labels=[], ai_labels_l3=["tabby"], ai_label_strict=True)
    assert e._apply_ai(t, _rec(), "p.jpg")[0] == "keep"


# --------------------------------------------- 4) 候选集要带上二三级 ----
def test_deep_words_join_the_classifier_candidates(tmp_db, tmp_config):
    """所有层的主题词都要进候选集，否则模型永远不给它们分数（伪不命中）。

    V0.0.4：候选集的组装从 engine 挪到了 find_terms —— 合成的一张词表要
    同时供下载前挑链接、站内找图、看图判定三处用，不能只在 engine 里拼
    一份。断言跟着改成运行时语义检查：四层词一个都不能少。
    """
    import inspect
    import media_resource_manager.engine as eng_mod
    from media_resource_manager import find_terms
    from media_resource_manager.models import TaskConfig
    t = TaskConfig(
        id=1, name="t", adapter_name="",
        ai_label_levels=[["cat"], ["kitten"], ["white kitten"], ["blue eyes"]])
    terms = find_terms.collect(tmp_db, t, tmp_config)
    for w in ("cat", "kitten", "white kitten", "blue eyes"):
        assert w in terms.want, f"{w} 没进候选集"
    # engine 仍要真的把这张表交给看图模型，不能只算不用
    src = inspect.getsource(eng_mod)
    assert "_terms.want" in src
    assert "_level_labels" in src


def test_candidates_source_is_consistent_with_levels():
    """二三级进候选集，才能被 _best_score 查到分数。"""
    score_of = {"kitten": 0.6}
    assert _best_score(score_of, ["kitten"])[1] == 0.6
