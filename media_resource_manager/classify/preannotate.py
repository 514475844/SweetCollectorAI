# -*- coding: utf-8 -*-
"""R154-F21 · AI 预标注**纯逻辑层**（不碰 Qt / 不碰库 / 零写入）。

## 订单原文（R154_FEATURES_ALL.md §F21）

    审核页顶部开关：CLIP 对当前页预判 top-1（喜欢倾向/不喜欢倾向）；
    界面显示预标结果与置信度，人工只改错（「全部采纳本页预标」按钮）；
    预判仅在本地跑，不写学习权重，采纳后才写。

## 预判的依据从哪来

图上**已经有** CLIP 打的标签——采集时 ``engine._apply_ai`` 跑过本地模型，
命中标签写进 ``images.tags``。预判不重新跑模型（GUI 主线程跑 CLIP 一次
几百毫秒～几秒，一页 50 张会直接把界面卡死；且 CPU-only 机器没装权重的
常态下模型根本不可用），而是拿**存好的标签** × **已学的偏好权重**算：

    score = 命中标签权重的均值      （与 ``PreferenceLearner.score`` 同一口径）
    score > 0  → like 倾向；< 0 → dislike 倾向；== 0 → 不表态（None）

两个铁律继承自本仓既有约定：

* **标签缺失一律「不表态」，不冒充 0 分**——「没学到/没标签」和「算出来
  恰好为零」必须分开（label_bridge「相似度缺失是 None 不是 0.0」同款）。
* **纯展示**：预判路径绝不写 preferences / reviews / learn_samples，
  权重只在人工「采纳」后由 ``review_tab._batch_decide_ids`` →
  ``queue.apply_batch`` 的学习工厂写（R166 收口的唯一咽喉）。

## 置信度是「展示口径」，不是门槛

权重值域 like=+1、dislike=-1、blacklist/purge=-2（见
``classify.preference``），所以 |score| ∈ [0, 2]。置信度 = |score| / 2
封顶 1.0，给界面一个 0~100% 的直观读数。**界面和测试都不许拿它当
「采不采纳」的判据**——采纳是人的决定，一键全收，改错靠人工复核。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

#: 置信度归一分母 = 偏好权重的最大 |值|（blacklist/purge 的 2.0）。
CONF_DIV = 2.0

#: 倾向取值（与 decisions 表的决策值同词，方便对账）。
LIKE = "like"
DISLIKE = "dislike"


@dataclass(frozen=True)
class PreAnnotation:
    """一张图的预判结果。

    ``tendency`` 为 None 表示**不表态**（无标签 / 无权重 / 均值恰为 0），
    界面显示「预判 —」，采纳时这类图一律不动。
    ``matched`` 是有非零权重的命中标签 ``((label, weight), ...)``，保原序。
    """

    image_id: int
    tendency: Optional[str]
    score: float
    confidence: float
    matched: tuple


def parse_tags(raw) -> list:
    """``images.tags`` 容错解析：list / JSON 串 / 老库逗号串 / None → list。

    老库存在逗号分隔裸串（F17 实测过），``json.loads`` 炸了就退回逗号切；
    单个裸词（无逗号无括号）也要能出来。空档一律空列表，不抛异常。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x or "").strip()]
    s = str(raw).strip()
    if not s:
        return []
    try:
        v = json.loads(s)
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x or "").strip()]
    except Exception:
        pass
    return [p.strip() for p in s.split(",") if p.strip()]


def predict(image_id, weights, tags, conf_div: float = CONF_DIV) -> PreAnnotation:
    """单图预判。**只读**：weights / tags 都不被改动，零副作用。

    score 口径与 ``PreferenceLearner.score`` 一致：命中标签权重的**均值**
    （没命中的标签按 0 计入分母——「标签在但模型没学过它」本身就是一种
    中性证据，与「图上压根没这个标签」不同，但两者都不足以表态时 score
    自然归零/近零，由符号决定倾向，这里不做第二套口径）。
    """
    w = dict(weights or {})
    tag_list = parse_tags(tags)
    matched = []
    total = 0.0
    for t in tag_list:
        try:
            val = float(w.get(t, 0.0) or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        if val != 0.0:
            matched.append((t, val))
        total += val
    score = (total / len(tag_list)) if tag_list else 0.0
    tendency = None
    if score > 0:
        tendency = LIKE
    elif score < 0:
        tendency = DISLIKE
    try:
        div = float(conf_div) if conf_div else CONF_DIV
        conf = min(1.0, abs(score) / div)
    except (TypeError, ValueError):
        conf = 0.0
    try:
        iid = int(image_id)
    except (TypeError, ValueError):
        iid = -1
    return PreAnnotation(image_id=iid, tendency=tendency,
                         score=float(score), confidence=round(conf, 4),
                         matched=tuple(matched))


def badge(pre: Optional[PreAnnotation]) -> str:
    """网格条目的一行徽标：``预判 喜欢 62%`` / ``预判 不喜欢 78%`` / ``预判 —``。

    百分比四舍五入到整数；不表态（含 ``pre is None``）恒显示「预判 —」，
    绝不显示 0%——0% 会被读成「算过了，五五开」（None 与 0 必须分开）。
    """
    if pre is None or pre.tendency is None:
        return "预判 —"
    zh = "喜欢" if pre.tendency == LIKE else "不喜欢"
    return f"预判 {zh} {round(pre.confidence * 100)}%"


def detail(pre: Optional[PreAnnotation]) -> str:
    """tooltip 明细：倾向 + 置信度 + 依据标签（含各自权重）+ 口径说明。"""
    if pre is None:
        return ""
    if pre.tendency is None:
        return "预判：不表态（无带权重的标签，模型尚未学习此类图片）"
    zh = "喜欢" if pre.tendency == LIKE else "不喜欢"
    hits = "、".join(f"{lab}({w:+g})" for lab, w in pre.matched) or "（无非零命中）"
    return (f"AI 预判：{zh}倾向，置信度 {round(pre.confidence * 100)}%\n"
            f"依据标签：{hits}\n"
            f"口径：命中标签权重均值 {pre.score:+.2f}；预判只展示，"
            f"点「全部采纳本页预标」才按真实判定写学习权重")
