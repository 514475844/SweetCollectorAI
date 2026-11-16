"""R171：中文词的「拼音序」排序（零依赖、跨平台确定）。

为什么不用 ``locale.strcoll`` / ``strxfrm``：排序结果依赖**进程级 locale**，
而本项目是「桌面 GUI + Web 服务同进程」，locale 由启动环境决定 —— 同一份
词表在两台机器上可能排出不同顺序，护栏也就钉不住。用户看到的词表顺序忽东
忽西，比"排序规则不完美"更糟。

为什么用 GB2312 字节序：GB2312 的一级汉字区（0xB0A1–0xD7F9，3755 个常用字）
**就是按拼音升序编码的**，所以「按拼音排序」= 「按 GB2312 字节排序」，
一行代码搞定且完全确定。全角/半角、ASCII、数字都在 GB2312 里有对应码位。

已知取舍（写下来免得以后被当 bug 改回去）：
  · 二级汉字区（0xD8A1 起，3008 个）GB2312 按**部首**排，不是拼音 ——
    生僻字会排到常用字后面且内部按部首序。用户词表里几乎不会出现，可接受。
  · 完全无法编码的字符（GB2312 之外的生僻字、emoji、日文假名等）统一归到
    **最后一档**，档内按 Unicode 码点排。这样"排不进去的词"都聚在末尾，
    不会插在拼音序中间造成看起来像乱序。
"""
from __future__ import annotations

#: 编码失败时用的排序分档：0 = 能按拼音排，1 = 归到末尾档。
_TIER_PINYIN = 0
_TIER_TAIL = 1


def pinyin_key(word: str) -> tuple:
    """单个词的排序键。返回 ``(档位, 字节 / 码点)``，可直接给 ``sorted``。

    同音字（GB2312 码位相邻的）保持**稳定原序** —— 由调用方 ``sorted``
    的稳定性保证，本函数不做二次打散。
    """
    w = str(word if word is not None else "").strip()
    try:
        return (_TIER_PINYIN, w.encode("gb2312"))
    except UnicodeEncodeError:
        return (_TIER_TAIL, w)


def sort_words(words) -> list[str]:
    """按拼音升序返回新列表（不改原列表）。``None`` / 空串会被剔除。"""
    seen: list[str] = []
    for w in words or []:
        s = str(w if w is not None else "").strip()
        if s and s not in seen:
            seen.append(s)
    return sorted(seen, key=pinyin_key)


def sort_uniq(words) -> list[str]:
    """按拼音升序 + 去重（``sort_words`` 已顺带去重，本名只为表意清晰）。"""
    return sort_words(words)
