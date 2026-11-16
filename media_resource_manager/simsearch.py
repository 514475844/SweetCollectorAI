"""「查找类似图片」——按整图三重色调 + 主题词 + pHash 三路加权推荐（需求 30）。

用户原话：「我选择相似图片以后，就会出现很多小女孩抱着洋娃娃或者抱着一只狗
之类的图片」—— 要的是**语义同类、道具可以不同**，不是 SSIM 那种「几乎一模一样」
的近重复检测。所以这套算法刻意把「长得像」的权重压到最低：

    主题词 Jaccard   0.42   主角/主题一致（最重要）
    整图三重色调     0.33   九宫格主色 + 整图主色的欧氏距离（构图与色调倾向）
    pHash 汉明       0.25   整体结构接近

三路的分值都归一到 0~1，加权求和即最终相似度。

与 demo（<build-dir> —— demo 已被用户验收
（「是我想要的结果」），这里是把同一套公式搬进软件，可以对任意一张库内图片
实时推荐，取代原来的「查找相似(SSIM)」。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Optional

W_WORD = 0.42
W_COLOR = 0.33
W_HASH = 0.25

# 太常见的词不携带信息量（「可爱」「nsfw」几乎每张都有），算 Jaccard 时忽略
GENERIC = {"nsfw", "cute", "可爱", "高清", "写真", "综合", "uncensored",
           "无修正", "无码", "高清画质"}


# BOX 是 Pillow 的重采样核（面积平均）。**必须写 Image.BOX**：
# 写 im.BOX 会 AttributeError（实例上没有这个属性），demo 里能跑是因为
# 它每处都直接写 Image.BOX —— 搬进函数时漏了模块前缀就整条链返回 None。
_BOX = None


def _box():
    global _BOX
    if _BOX is None:
        from PIL import Image
        _BOX = Image.BOX
    return _BOX


def grid3x3(im):
    """三等分竖切 × 3 行 = 9 格主色，外加整图主色。

    返回 ``(cells, avg)``：cells 是 9 个 [r,g,b]，avg 是整图主色。
    用 BOX 重采样到 1x1 取均值，比 resize+量化快得多，也不用引入新依赖。
    """
    w, h = im.size
    gw = max(1, w // 3)
    gh = max(1, h // 3)
    cells = []
    for ry in range(3):
        for rx in range(3):
            box = im.crop((rx * gw, ry * gh,
                           min(w, (rx + 1) * gw), min(h, (ry + 1) * gh)))
            px = box.resize((1, 1), _box()).getpixel((0, 0))
            cells.append([px[0], px[1], px[2]])
    allr = im.resize((1, 1), _box()).getpixel((0, 0))
    return cells, [allr[0], allr[1], allr[2]]


def hamming64(a, b) -> int:
    """两个 16 位十六进制 phash 的汉明距离（0~64）。任一为空按最不像算。"""
    try:
        x = int(a, 16) ^ int(b, 16)
    except Exception:
        return 64
    return bin(x).count("1")


def split_tags(tags) -> list[str]:
    """把 images.tags 拆成小写词表。

    库里的 tags **是 JSON 数组字符串**（实测落库值形如
    ``["white stockings", "pantyhose", "cute"]``），不是逗号分隔的裸串。
    早先只按逗号/空白切分，整串会被当成一个词 → Jaccard 恒等于「相同才 1」，
    翻译也整条失效（界面直接显示方括号和引号）。这里必须先剥 JSON。
    """
    import json
    import re
    if not tags:
        return []
    raw = tags
    if isinstance(raw, str):
        t = raw.strip()
        if t.startswith("["):
            try:
                raw = json.loads(t)
            except Exception:
                raw = None
        if raw is None:
            raw = re.split(r"[,\s;，、]+", t)
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    out = []
    for x in raw:
        t = str(x).strip().lower()
        if t:
            out.append(t)
    return out


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """主题词 Jaccard，通用词（可爱/高清这类）不计入。"""
    sa = {t for t in a if t not in GENERIC}
    sb = {t for t in b if t not in GENERIC}
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def color_score(a, b) -> float:
    """三重色调相似度 0~1：九宫格 9 组 + 整图 1 组，欧氏距离归一。

    归一化用 441.67（= sqrt(3 * 255²)，RGB 空间最大欧氏距离）。
    """
    if not a or not b:
        return 0.0
    ac, aa = a
    bc, ba = b
    if not ac or not bc or len(ac) != len(bc):
        return 0.0
    dist = 0.0
    for p, q in zip(ac, bc):
        dist += math.sqrt((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                          + (p[2] - q[2]) ** 2)
    dist += math.sqrt((aa[0] - ba[0]) ** 2 + (aa[1] - ba[1]) ** 2
                      + (aa[2] - ba[2]) ** 2)
    return max(0.0, 1.0 - dist / (math.sqrt(3 * 255 ** 2) * len(ac) + 441.67))


def hash_score(a: str, b: str) -> float:
    """pHash 相似度 0~1（汉明 0 → 1.0，汉明 64 → 0.0）。"""
    return max(0.0, 1.0 - hamming64(a, b) / 64.0)


def total_score(word: float, color: float, hsh: float) -> float:
    return W_WORD * word + W_COLOR * color + W_HASH * hsh


def features_for_image(path) -> Optional[tuple]:
    """单张图的视觉特征 (cells, avg)；读不出来返回 None。"""
    try:
        from PIL import Image
        p = Path(path)
        if not p.exists() or p.stat().st_size < 2048:
            return None
        with Image.open(p) as im:
            return grid3x3(im.convert("RGB"))
    except Exception:
        return None


def rank_similar(seed, candidates, *, limit: int = 120) -> list[dict]:
    """给种子图在候选集里排「类似图片」。

    ``seed`` / ``candidates`` 形如
    ``{"id":…, "path":…, "tags":…, "phash":…, "color": (cells, avg)}``，
    其中 ``color`` 可以是 None（没预计算的话现算）。

    返回按 ``score`` 降序的 dict 列表，每项多带 ``w_word/w_color/w_hash``
    三个分量，方便界面解释「为什么像」。
    """
    if not seed or not candidates:
        return []
    sc = seed.get("color") or features_for_image(seed.get("path"))
    if sc is None:
        return []
    s_tags = split_tags(seed.get("tags"))
    out = []
    for c in candidates:
        if not c or c.get("id") == seed.get("id"):
            continue
        cc = c.get("color") or features_for_image(c.get("path"))
        if cc is None:
            continue
        w = jaccard(s_tags, split_tags(c.get("tags")))
        col = color_score(sc, cc)
        hs = hash_score(seed.get("phash") or "", c.get("phash") or "")
        out.append({
            **c,
            "score": total_score(w, col, hs),
            "w_word": w, "w_color": col, "w_hash": hs,
        })
    out.sort(key=lambda d: d["score"], reverse=True)
    return out[:limit]
