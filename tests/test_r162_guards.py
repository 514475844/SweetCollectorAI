"""R162 棒护栏：R152 的 D4（top-10 截断）+ C8（用户正则/搜索框崩溃）。

三条都是「用户输入/模型输出直接冒异常或语义失真」类缺陷：
- D4：classify 截断 top-10 → engine 的 all 匹配模式只看前 10 个标签；
- C8a：图库搜索 `w>=abc` → int() ValueError 冒到 GUI；
- C8b：任务自填正则写错 → re.error 冒到 discovery/worker。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from media_resource_manager.classify.clip_model import LocalClassifier
from media_resource_manager.search import parse_query
from media_resource_manager.utils.url import allowed_by_regex


# ---------------- D4：classify 不得截断，all 模式要看全量标签 ----------------

class _FakeClip:
    """假 CLIP：按 2026-10-30 之后的真实调用面提供 encode_image/encode_text。

    生产流程是 `ie @ te.T` 拿余弦、再过 clip_scores(softmax)。
    替身让点积恰好等于预设余弦：ie 是第一个基向量，te 的每一行
    第 0 维 = 该标签余弦、其余维补模长成正交分量。
    """

    def __init__(self, cos):
        self._cos = cos

    def encode_image(self, pil):
        import numpy as np
        return np.array([[1.0, 0.0]], dtype=np.float64)

    def encode_text(self, prompts):
        import numpy as np
        # prompts = labels + CLIP_ANCHORS；锚点统一给 -0.3（比末位
        # 标签余弦 -0.55 高，softmax 温度调缓后仍挡在 0.5 线下）。
        out = []
        for i, _p in enumerate(prompts):
            c = (self._cos[i] if i < len(self._cos) else -0.3)
            out.append([c, np.sqrt(max(0.0, 1.0 - c * c))])
        return np.array(out, dtype=np.float64)


def _make_clip_classifier(cos):
    """不跑 __init__（会真加载模型），直接搭一个 clip 后端替身。"""
    clf = object.__new__(LocalClassifier)
    clf._backend = "clip"
    clf._clip = _FakeClip(cos)
    return clf


def _linear_scores(raw_cos, labels, scale=1.0):
    """受控打分函数：cos → (cos+1)/2 线性映射。

    ⚠ 2026-11-09：生产 clip_scores 在 10-30 换成 softmax（×100 温度），
    概率分布变成「第一名≈1、其余≈0」——这是打分口径，和 D4 钉的
    「不许截断 top-10」无关。原测试的分数期望建立在裸余弦线性映射上，
    softmax 下永远凑不出 top10 全过线的形状（假红）。这里把打分函数
    monkeypatch 成受控线性映射，只留「返回条数 = 全量、all() 看得到
    低分词」这两件 D4 本征事实在被测链路上。
    """
    return [(c + 1.0) / 2.0 for c in list(raw_cos)[: len(labels)]]


def test_d4_classify_returns_all_labels_not_top10(monkeypatch):
    """12 个标签必须全部返回（原来被 [:10] 砍掉最低 2 个）。"""
    import media_resource_manager.classify.clip_model as cm
    monkeypatch.setattr(cm, "clip_scores", _linear_scores)
    labels = [f"L{i:02d}" for i in range(12)]
    # 余弦值：前 10 个高（0.6→映射 0.8），后 2 个低（-0.6→映射 0.2）
    cos = [0.6 - 0.01 * i for i in range(10)] + [-0.6, -0.55]
    clf = _make_clip_classifier(cos)
    clf.labels = labels
    monkeypatch.setattr(LocalClassifier, "_open", staticmethod(lambda p: object()))

    pairs = clf.classify(Path("x.jpg"))

    assert len(pairs) == 12, "D4 回归：classify 又把标签截断了"
    # 排序仍是分数降序（注意 L11 的余弦 -0.55 映射 0.225 > L10 的 0.2，
    # 所以末两个是 L11、L10 —— 断言排序性质而非固定顺序）
    scores = [s for _, s in pairs]
    assert scores == sorted(scores, reverse=True)
    assert {l for l, _ in pairs} == set(labels)


def test_d4_all_mode_sees_low_score_labels(monkeypatch):
    """行为本质：all(s>=thr) 在**全量**上为 False（截断版会误判 True）。"""
    import media_resource_manager.classify.clip_model as cm
    monkeypatch.setattr(cm, "clip_scores", _linear_scores)
    labels = [f"L{i:02d}" for i in range(12)]
    cos = [0.6 - 0.01 * i for i in range(10)] + [-0.6, -0.55]
    clf = _make_clip_classifier(cos)
    clf.labels = labels

    import media_resource_manager.classify.clip_model as cm
    orig_open = cm.LocalClassifier._open
    cm.LocalClassifier._open = staticmethod(lambda p: object())
    try:
        pairs = clf.classify(Path("x.jpg"))
    finally:
        cm.LocalClassifier._open = orig_open

    thr = 0.5
    full = all(s >= thr for _, s in pairs)
    top10 = all(s >= thr for _, s in pairs[:10])  # 旧行为的错误结论
    assert full is False, "低分标签必须参与 all 判定（D4 修复点）"
    assert top10 is True, "若这条不为 True，说明没复现出旧缺陷"


def test_d4_onnx_path_not_truncated():
    """onnx 后端同样不许截断（源码判据，防止只修一半）。"""
    src = Path(cm_src())
    text = src.read_text(encoding="utf-8")
    clip_fn = text.split("def _classify_onnx")[1].split("def ")[0]
    assert "[:10]" not in clip_fn, "onnx 分类又出现 top-10 截断"
    assert "R152-D4" in clip_fn


def cm_src() -> str:
    import media_resource_manager.classify.clip_model as cm
    return cm.__file__


# ---------------- C8a：图库搜索非法数值不再崩 ----------------

def test_c8_parse_query_bad_width_does_not_raise():
    """`w>=abc` 原来 int() ValueError 直冒 GUI；现在忽略该条件。"""
    c = parse_query("w>=abc h>=600 tag:猫")
    assert c["w_min"] is None
    assert c["h_min"] == 600            # 同串里的合法条件照常生效
    assert c["tags_include"] == ["猫"]


def test_c8_parse_query_bad_score_does_not_raise():
    c = parse_query("score>abc score<0.9")
    assert c["score_min"] is None
    assert c["score_max"] == 0.9


def test_c8_parse_query_valid_still_works():
    c = parse_query("w>=800 h>=600 score>0.5 fav -黑丝")
    assert c["w_min"] == 800 and c["h_min"] == 600
    assert c["score_min"] == 0.5 and c["fav"] is True
    assert c["tags_exclude"] == ["黑丝"]


# ---------------- C8b：用户正则写错不再冒 re.error ----------------

def test_c8_bad_deny_regex_does_not_block_or_raise():
    """坏 deny 正则 → 按不存在处理（不拦），且绝不抛 re.error。"""
    assert allowed_by_regex("https://pictoa.com/1", None, ["[unclosed"]) is True
    assert allowed_by_regex("https://pictoa.com/1", None, ["(bad"]) is True


def test_c8_bad_allow_regex_all_bad_means_allow_all():
    """allow 全坏 → 等价于没配 allow（放行），不抛异常。"""
    assert allowed_by_regex("https://pictoa.com/1", ["[oops"], None) is True


def test_c8_mixed_allow_still_matches_good_pattern():
    assert allowed_by_regex("https://pictoa.com/1",
                            ["[bad", "pictoa"], None) is True
    assert allowed_by_regex("https://other.com/1",
                            ["[bad", "pictoa"], None) is False


def test_c8_good_patterns_unchanged():
    assert allowed_by_regex("https://pictoa.com/a",
                            [r"pictoa\.com"], [r"/admin/"]) is True
    assert allowed_by_regex("https://pictoa.com/admin/x",
                            [r"pictoa\.com"], [r"/admin/"]) is False
