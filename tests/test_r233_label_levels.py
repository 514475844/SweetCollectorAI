"""主题词无限分级 + 每级独立严格模式（2026-10-28）。

用户的原始要求：「白名单要能无限细分，1、2、3、4、5、6、7，甚至八、九、十，
一直向下」，并且「每一级单独的严格模式」。

这里锁死三件事：
1. 老任务（只有一/二/三级 + 一个全局严格）读出来行为**完全不变**；
2. 新任务能存任意层数，尾部的空层会被裁掉；
3. 逐层判定的四种走法：逐层命中 / 严格层没命中就丢 / 不严格就停在上一层 /
   细分层全没命中就送审。
"""

from __future__ import annotations

from media_resource_manager.label_levels import (
    MAX_LEVELS,
    apply_levels,
    deep_words,
    describe,
    flat_words,
    levels_of,
    stricts_of,
    walk_levels,
)
from media_resource_manager.models import TaskConfig


def _task(**kw) -> TaskConfig:
    """TaskConfig 的 id/name/adapter_name 是必填的，这里统一给个哑值。"""
    kw.setdefault("id", 1)
    kw.setdefault("name", "t")
    kw.setdefault("adapter_name", "generic")
    return TaskConfig(**kw)


# --------------------------------------------------------------------------- #
# 读：老结构零影响
# --------------------------------------------------------------------------- #
def test_legacy_three_columns_still_read():
    t = _task(ai_labels=["白丝"], ai_labels_l2=["丝袜"],
                   ai_labels_l3=["黑丝"])
    assert levels_of(t) == [["白丝"], ["丝袜"], ["黑丝"]]
    # 老任务没填 l2/l3 → 只有一级，且尾部空层被裁掉
    old = _task(ai_labels=["白丝"])
    assert levels_of(old) == [["白丝"]]
    assert levels_of(_task()) == [[]]


def test_legacy_global_strict_expands_to_every_deep_level():
    """老 ai_label_strict=True 当年的含义就是「二三级都没命中就丢」，
    展开成「第 2 层起全部严格」正好等价。"""
    t = _task(ai_labels=["a"], ai_labels_l2=["b"], ai_labels_l3=["c"],
                   ai_label_strict=True)
    assert stricts_of(t) == [False, True, True]
    t2 = _task(ai_labels=["a"], ai_labels_l2=["b"], ai_labels_l3=["c"],
                    ai_label_strict=False)
    assert stricts_of(t2) == [False, False, False]
    # 第 1 层永远是入门票，没有"严格"可言
    assert stricts_of(t)[0] is False


# --------------------------------------------------------------------------- #
# 写：任意层数
# --------------------------------------------------------------------------- #
def test_apply_six_levels_and_sync_back_to_legacy_columns():
    t = _task()
    levels = [["a"], ["b"], ["c"], ["d"], ["e"], ["f"]]
    stricts = [False, False, True, False, False, True]
    apply_levels(t, levels, stricts)
    assert levels_of(t) == levels
    assert stricts_of(t) == stricts
    # 旧三列同步前 3 层（老界面/老测试读得到）
    assert (t.ai_labels, t.ai_labels_l2, t.ai_labels_l3) == (["a"], ["b"], ["c"])
    # 旧全局开关 = 细分层里是否有严格层
    assert t.ai_label_strict is True
    assert flat_words(t) == ["a", "b", "c", "d", "e", "f"]
    assert deep_words(t) == ["b", "c", "d", "e", "f"]


def test_trailing_empty_levels_are_trimmed():
    t = _task()
    apply_levels(t, [["a"], ["b"], [], [], []], [])
    assert levels_of(t) == [["a"], ["b"]]
    # 中间的空层**不能**裁：用户可能故意只填第 1 和第 4 层
    apply_levels(t, [["a"], [], [], ["d"]], [])
    assert levels_of(t) == [["a"], [], [], ["d"]]


def test_levels_capped():
    t = _task()
    many = [[f"w{i}"] for i in range(MAX_LEVELS + 8)]
    apply_levels(t, many, [])
    assert len(levels_of(t)) == MAX_LEVELS


def test_first_level_is_never_strict():
    t = _task()
    apply_levels(t, [["a"], ["b"]], [True, True])
    assert stricts_of(t) == [False, True]


# --------------------------------------------------------------------------- #
# 判定：四种走法
# --------------------------------------------------------------------------- #
def _hit_set(words):
    return lambda w: w in words


def test_walk_all_levels_hit():
    levels = [["a"], ["b"], ["c"], ["d"]]
    stricts = [False, False, False, False]
    kind, deepest, hits, _ = walk_levels(levels, stricts, _hit_set({"a", "b", "c", "d"}))
    assert kind == "keep"
    assert deepest == "d"          # 最深的命中词当 {keyword} 目录名
    assert hits == ["b", "c", "d"]


def test_walk_stops_at_first_missing_level_when_not_strict():
    """第 3 层没命中但没勾严格 → 停在第 2 层，保留（不送审）。
    这是"无限细分"能用的关键：否则填了 6 层的任务会把 5 层以下的图全送审。"""
    levels = [["a"], ["b"], ["c"], ["d"]]
    stricts = [False, False, False, False]
    kind, deepest, hits, why = walk_levels(levels, stricts, _hit_set({"a", "b"}))
    assert kind == "keep"
    assert deepest == "b"
    assert hits == ["b"]
    assert "strict" not in why


def test_walk_strict_level_missing_drops_even_if_upper_level_hit():
    """第 2 层命中、第 3 层勾了严格却没命中 → 整图不命中。"""
    levels = [["a"], ["b"], ["c"]]
    stricts = [False, False, True]
    kind, _, _, why = walk_levels(levels, stricts, _hit_set({"a", "b"}))
    assert kind == "drop"
    assert "level-3 strict" in why


def test_walk_level1_only_goes_to_review():
    levels = [["a"], ["b"], ["c"]]
    kind, deepest, hits, why = walk_levels(levels, [False, False, False],
                                           _hit_set({"a"}))
    assert (kind, deepest, hits) == ("review", "", [])
    assert "level-1 only" in why


def test_walk_level1_only_with_any_strict_drops():
    """全部细分层没命中 + 有严格层 → 丢（老「严格模式」的行为）。"""
    levels = [["a"], ["b"]]
    kind, _, _, _ = walk_levels(levels, [False, True], _hit_set({"a"}))
    assert kind == "drop"


def test_walk_empty_middle_level_is_skipped():
    """中间空层当"这层没设条件"，直接跳过继续往下。"""
    levels = [["a"], [], ["c"]]
    kind, deepest, _, _ = walk_levels(levels, [False, False, False],
                                      _hit_set({"a", "c"}))
    assert kind == "keep"
    assert deepest == "c"


def test_walk_no_level1_never_uses_strict():
    """一级留空 = 整套分级不参与（老铁律：没有入门票不许严格模式乱杀）。"""
    levels = [[], ["b"]]
    kind, _, _, why = walk_levels(levels, [False, True], _hit_set(set()))
    assert kind == "keep"
    assert why == "no-level-1"


def test_walk_single_level_is_noop():
    kind, deepest, hits, _ = walk_levels([["a"]], [False], _hit_set({"a"}))
    assert (kind, deepest, hits) == ("keep", "", [])


def test_describe_mentions_strict_levels():
    s = describe([["a"], ["b"], ["c"]], [False, True, False])
    assert "3 级" in s and "第2级严格" in s
    assert describe([], []) == "未分级"
