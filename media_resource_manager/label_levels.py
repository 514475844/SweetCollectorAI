"""主题词层级：任意层数（1..N）的统一读写与判定。

**为什么要有这个模块**：原来主题词只支持「一级 / 二级 / 三级」三列固定字段
（``ai_labels`` / ``ai_labels_l2`` / ``ai_labels_l3``），严格模式也只有一个全局
开关 ``ai_label_strict``。用户明确要求「白名单要能无限细分，1、2、3、4、5、6…
一直往下，而且**每一级都能单独设严格模式**」。

于是新增两个字段（都用 JSON 文本落库）：

* ``ai_label_levels``：二维数组，``[["一级词…"], ["二级词…"], …]``，层数不限；
* ``ai_label_strict_levels``：布尔数组，与上面逐层对齐，``True`` = 这一级必须
  命中，否则整张图判不命中。

**读优先新结构、空则回退旧三列** —— 老库老任务不迁移、行为零变化；
**写同时更新旧三列（前 3 层）** —— 老界面 / 老探针 / 老测试都还能正常读。

判定语义（``walk_levels``）：
    第 1 层 = 入门票（决定留/送审/丢，逻辑与以前完全一致）；
    第 2..N 层 = 逐级往下走：
        * 这一层有词且命中 → 记下最深命中词，继续下一层；
        * 这一层有词但没命中 → 勾了严格就「整图不命中」，
                              没勾就停在上一层（保留，不送审）；
    所有细分层都没命中 → 维持老行为：送审核区（除非有严格层已判丢）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, List, Tuple

#: 层数上限。只是防御 —— 每层词都会进候选集喂给模型，层数太多会拖慢判定，
#: 也会让界面长到没法用。20 层已经很夸张，正常用户 3~6 层。
MAX_LEVELS = 20


# --------------------------------------------------------------------------- #
# 解析小工具（JSON 文本 / list 都吃）
# --------------------------------------------------------------------------- #
def _as_levels(raw: Any) -> List[List[str]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except Exception:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[List[str]] = []
    for item in raw:
        if isinstance(item, str):
            # 宽容：万一有人把 "a b c" 当一层塞进来
            out.append([w for w in item.replace(",", " ").split() if w])
        elif isinstance(item, (list, tuple)):
            out.append([str(w).strip() for w in item if str(w).strip()])
        else:
            out.append([])
    return out


def _as_bools(raw: Any) -> List[bool]:
    if raw is None:
        return []
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            raw = json.loads(s)
        except Exception:
            return []
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[bool] = []
    for x in raw:
        if isinstance(x, str):
            out.append(x.strip().lower() in ("1", "true", "yes", "on", "是"))
        else:
            out.append(bool(x))
    return out


def _clean(words: Iterable[str]) -> List[str]:
    out: List[str] = []
    for w in words or []:
        t = str(w or "").strip()
        if t and t not in out:
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def levels_of(task) -> List[List[str]]:
    """任务的主题词按层拆开（第 1 层是入门票）。至少返回 ``[[]]``。

    新字段为空 → 回退旧的 一/二/三级 三列（老库老任务走这条，行为不变）。
    尾部连续的空层会被去掉（用户清空最后两级，不该算还留着那两级）。
    """
    lv = _as_levels(getattr(task, "ai_label_levels", None))
    if not lv:
        lv = [
            _clean(getattr(task, "ai_labels", None) or []),
            _clean(getattr(task, "ai_labels_l2", None) or []),
            _clean(getattr(task, "ai_labels_l3", None) or []),
        ]
    else:
        lv = [_clean(x) for x in lv]
    lv = lv[:MAX_LEVELS]
    while len(lv) > 1 and not lv[-1]:
        lv.pop()
    return lv or [[]]


def stricts_of(task) -> List[bool]:
    """与 ``levels_of`` 逐层对齐的严格开关。老任务的 ``ai_label_strict`` 会
    被展开成「第 2 层起全部严格」—— 正好等于它当年的含义。"""
    n = len(levels_of(task))
    st = _as_bools(getattr(task, "ai_label_strict_levels", None))
    if not st:
        one = bool(getattr(task, "ai_label_strict", False))
        st = [False] + [one] * max(0, n - 1)
    st = (st + [False] * n)[:n]
    st[0] = False        # 第 1 层永远是入门票，没有"严格"可言
    return st


def flat_words(task) -> List[str]:
    """所有层的词展平（喂模型候选集 / 全库词表用）。"""
    out: List[str] = []
    for lv in levels_of(task):
        for w in lv:
            if w not in out:
                out.append(w)
    return out


def deep_words(task) -> List[str]:
    """第 2 层及以后的词（"细分词"）。"""
    out: List[str] = []
    for lv in levels_of(task)[1:]:
        for w in lv:
            if w not in out:
                out.append(w)
    return out


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def apply_levels(task, levels: List[List[str]],
                 stricts: List[bool] | None = None) -> None:
    """把层写回任务对象（新字段 + 旧三列一起写，保证老读者仍可用）。"""
    lv = [_clean(x) for x in (levels or [])][:MAX_LEVELS]
    while len(lv) > 1 and not lv[-1]:
        lv.pop()
    if not lv:
        lv = [[]]
    st = _as_bools(stricts or [])
    st = (st + [False] * len(lv))[:len(lv)]
    st[0] = False

    setattr(task, "ai_label_levels", lv)
    setattr(task, "ai_label_strict_levels", st)
    # ---- 旧三列（只放前 3 层；老界面/老测试据此判断"填了哪些词"）----
    def at(i):
        return list(lv[i]) if len(lv) > i else []
    setattr(task, "ai_labels", at(0))
    setattr(task, "ai_labels_l2", at(1))
    setattr(task, "ai_labels_l3", at(2))
    # 旧全局开关 = "细分层里有任何一层是严格的"，只给旧读者看，判定走新数组
    setattr(task, "ai_label_strict", any(st[1:]))


# --------------------------------------------------------------------------- #
# 判定
# --------------------------------------------------------------------------- #
def walk_levels(levels: List[List[str]], stricts: List[bool],
                has_hit: Callable[[str], bool]) -> Tuple[str, str, List[str], str]:
    """逐层往下走，返回 ``(verdict, deepest, hits, reason)``。

    verdict：
        ``"keep"``   一路走通（或没填细分词）→ 保留；
        ``"review"`` 只命中一级、细分层一个都没命中 → 送审核（老行为）；
        ``"drop"``   某个严格层没命中 → 整图判不命中。

    ``deepest`` 是命中的**最深一层**里的那个词，用来当 ``{keyword}`` 子目录名；
    ``hits`` 是所有命中的细分词（打标签用，和以前一样全收）。
    """
    levels = [_clean(x) for x in (levels or [])]
    if not levels:
        return "keep", "", [], "no-levels"
    stricts = list(stricts or []) + [False] * len(levels)
    l1 = levels[0]
    if not l1:
        # 一级留空：整套分级不参与（老铁律：没有入门票就不许严格模式乱杀）
        return "keep", "", [], "no-level-1"
    if len(levels) == 1:
        # 只有一级 = 没有细分层，行为跟老逻辑一样（不参与细分判定）
        return "keep", "", [], "single-level"

    deepest = ""
    hits: List[str] = []
    for idx in range(1, len(levels)):
        words = levels[idx]
        if not words:
            continue                     # 空层 = 这层没设条件，跳过
        layer_hits = [w for w in words if has_hit(w)]
        if layer_hits:
            hits.extend(layer_hits)
            deepest = layer_hits[-1]
            continue
        # 这一层有词但没命中
        if stricts[idx]:
            return ("drop", deepest, hits,
                    f"level-{idx + 1} strict miss（第 {idx + 1} 级设了严格，"
                    f"一个词都没命中）")
        break                            # 不严格：停在上一层，保留
    if deepest:
        return "keep", deepest, hits, f"deep hit: {deepest}"
    return ("review", "", hits,
            "level-1 only（只命中一级，细分层一个都没命中）")


def describe(levels: List[List[str]], stricts: List[bool]) -> str:
    """给界面/日志用的一句话概览，例如 ``三级（第3级严格）``。"""
    n = len([x for x in levels if x])
    tail = [f"第{i + 1}级严格" for i, s in enumerate(stricts) if s and i < len(levels)]
    base = f"{n} 级" if n else "未分级"
    return base + ("（" + "、".join(tail) + "）" if tail else "")
