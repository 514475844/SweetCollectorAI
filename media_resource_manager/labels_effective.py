"""V0.0.3：全软件共用的一张词表。

以前导入、学习、采集、偏好学习各用各的词表：有的用原始中文词直接喂给
识图模型（认不准），有的会补英文候选（认得准），有的空着就干脆不打标。
结果同一张图在不同入口贴出不同标签，学到的权重也对不上号。

现在统一成一条：任务词表 → 全局生效词表 → 曾用词历史三级 → 内置通用词，
最后统一补中英候选。任何入口都调 ``effective_labels()``。
"""
from __future__ import annotations

from typing import Any


def _clean(words) -> list[str]:
    out: list[str] = []
    for w in words or []:
        s = str(w or "").strip()
        if not s or s in out:
            continue
        # 明显是调试留下的占位词，别混进正式词表
        if s in {"测试词", "test", "TEST"}:
            continue
        out.append(s)
    return out


def effective_labels(task: Any = None, cfg: Any = None) -> list[str]:
    """算出当前真正该用的标签词表（已含中英双语候选）。

    顺序有意义：任务自己配的优先，其次是全局生效词表，再是用户曾经用过
    的词（下拉框历史），最后才退到内置通用词。任何一级有值就不再往后取，
    免得通用词把用户的精细词冲淡；但历史三级会一并并进来，因为那是用户
    亲手用过的口味，比通用词更贴近他。
    """
    from .classify.labels_zh import expand_labels
    from .learn_folder import DEFAULT_GENERIC_LABELS

    task_words = _clean(getattr(task, "ai_labels", None))
    global_words = _clean(getattr(cfg, "ai_labels", None))
    history: list[str] = []
    for attr in ("ai_label_history", "ai_label_history_l2",
                 "ai_label_history_l3"):
        history.extend(_clean(getattr(cfg, attr, None)))

    base = task_words or global_words
    if base:
        # 用户明确配了词：历史里没重复的词也并进来，多几个候选不亏
        merged = list(base)
        for w in history:
            if w not in merged:
                merged.append(w)
    else:
        # 没配词：历史口味 + 内置通用词兜底，保证至少能贴出标签
        merged = list(dict.fromkeys(history + list(DEFAULT_GENERIC_LABELS)))

    return expand_labels(merged)
