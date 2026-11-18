"""V0.0.14 · 合格线自动校准（**纯逻辑层**，不碰 Qt、不碰文件 IO、不写配置）。

用户初衷：采集回来的图最终要交给 AI 学习、再生成更多图。素材集的纯度决定
生成质量，所以「AI 分合格线」不能一直靠人拍脑袋填一个数，应当从**用户自己
已经做过的判断**里反推出来。

口径纪律（改本文件前必读）
1. 正样本 = 用户主动收藏、且未被判退的图；负样本 = 已拉黑或已进隔离区
   （**待审核的不算负样本**，它只是还没定，见 ``needs_review=0`` 条件）。
   两端（桌面 / 网页）都必须调 ``suggest()``，不许各写一份 SQL。
2. **只用 images.score**（模型给每张图打的 0~1 分）。不用 user_score，
   因为绝大多数图没有人工星级，拿它算出来的线没有统计意义。
3. 样本不足或分数根本没有区分力时，一律 ``ok=False`` 并给出中文原因，
   **绝不返回一个看似合理的数字**。给一个瞎猜的阈值比不给更糟：用户会照着
   它筛掉一批好图，还以为软件算过。
4. 只负责「建议」，不负责「落盘」。写 ``cfg.ai_threshold`` / 任务阈值由
   调用方完成 —— 纯函数才好单测，也避免网页端和桌面端各自偷偷改配置。

算法：先看两组分布有没有清晰间隙（正样本 10% 分位 ≥ 负样本 90% 分位时取
中点）；没有间隙就扫描候选阈值，取「留住好图比例 − 放进坏图比例」最大的
那个（Youden J）。这一步只做筛选，不训练模型，离线、无网络、毫秒级。
"""
from __future__ import annotations

import math

#: 每类至少要有的样本数。少于这个数，分位数会被单张图带偏，宁可不建议。
MIN_POS = 8
MIN_NEG = 8

#: 建议值的钳制范围。0 / 1 两个极端都会把一侧全砍光，永远不返回。
FLOOR = 0.05
CEIL = 0.95

#: 候选阈值扫描上限。图库上万张时全量候选是 O(n²)，等距采样足够。
MAX_CANDIDATES = 240


def _quantile(sorted_values: list[float], p: float) -> float:
    """线性插值分位数（空列表返回 0.0，单点返回该点）。"""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    idx = (len(sorted_values) - 1) * p
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return float(sorted_values[lo])
    frac = idx - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def collect_scores(db) -> tuple[list[float], list[float]]:
    """取出正 / 负两组 AI 分，各自升序返回。

    ``needs_review=1`` 的图是「等待判定」而不是「判定为差」，把它算进负样本
    会让隔离区里待审的图污染统计 —— 这是最容易写错的一条口径。
    """
    pos_rows = db.query(
        "SELECT score FROM images WHERE favorite=1 AND blacklisted=0 "
        "AND in_quarantine=0 AND needs_review=0 AND score IS NOT NULL")
    neg_rows = db.query(
        "SELECT score FROM images WHERE (blacklisted=1 OR "
        "(in_quarantine=1 AND needs_review=0)) AND score IS NOT NULL")
    pos = sorted(float(r[0]) for r in pos_rows)
    neg = sorted(float(r[0]) for r in neg_rows)
    return pos, neg


def _rates(pos: list[float], neg: list[float], t: float) -> tuple[float, float]:
    """阈值 t 下的「留住好图比例」与「放进坏图比例」。"""
    keep = sum(1 for x in pos if x >= t) / len(pos) if pos else 0.0
    leak = sum(1 for x in neg if x >= t) / len(neg) if neg else 0.0
    return keep, leak


def _confusion(pos: list[float], neg: list[float], t: float) -> dict:
    """阈值 t 下的混淆矩阵四格。

    "合格"定义为分 ≥ t。正样本(收藏)判合格=TP，判不合格=FN(漏杀好图)；
    负样本(判退)判合格=FP(放进坏图)，判不合格=TN。
    """
    tp = sum(1 for x in pos if x >= t)
    fn = len(pos) - tp
    fp = sum(1 for x in neg if x >= t)
    tn = len(neg) - fp
    return {"tp": tp, "fn": fn, "fp": fp, "tn": tn}


def _mistakes(db, t: float, limit: int = 5) -> dict:
    """在建议阈值下会被误判的样例（各取几张，供人工核对阈值是否合理）。

    - false_negative：本该留住的收藏图却落在线下（高分门槛误伤）；
    - false_positive：本该拦掉的判退图却过了线（漏放）。
    只回 id/分数/标签，前端只读展示，绝不写库。老库缺列时安静降级。
    """
    out = {"false_negative": [], "false_positive": []}
    try:
        base = ("SELECT id, score, tags FROM images WHERE score IS NOT NULL "
                "AND path IS NOT NULL")
        for key, extra, order in (
            ("false_negative",
             "AND favorite=1 AND blacklisted=0 AND in_quarantine=0 "
             "AND needs_review=0 AND score<?", "score ASC"),
            ("false_positive",
             "AND (blacklisted=1 OR (in_quarantine=1 AND needs_review=0)) "
             "AND score>=?", "score DESC"),
        ):
            for r in db.query(f"{base} {extra} ORDER BY {order} LIMIT ?",
                              (float(t), int(limit))):
                out[key].append({
                    "id": int(r["id"]),
                    "score": round(float(r["score"] or 0), 3),
                    "tags": str(r["tags"] or "")[:120],
                })
    except Exception:
        pass
    return out


def _best_threshold(pos: list[float], neg: list[float], current: float) -> tuple[float, float]:
    """扫描候选阈值，返回 (最佳阈值, 最佳区分度)。区分度 = 留住比例 − 放进比例。"""
    cand = sorted({round(float(x), 4) for x in (pos + neg)
                   if FLOOR <= float(x) <= CEIL})
    if FLOOR <= float(current) <= CEIL:
        cand.append(round(float(current), 4))
    cand = sorted(set(cand))
    if len(cand) > MAX_CANDIDATES:
        step = len(cand) / MAX_CANDIDATES
        cand = [cand[min(len(cand) - 1, int(i * step))]
                for i in range(MAX_CANDIDATES)]
    best_t, best_j = float(current), -2.0
    for t in cand:
        keep, leak = _rates(pos, neg, t)
        j = keep - leak
        if j > best_j:
            best_t, best_j = t, j
    return best_t, best_j


def _gallery_counts(db, threshold: float) -> tuple[int, int]:
    """按建议阈值复算当前图库：会留下多少张、会落在线下多少张。"""
    row = db.query(
        "SELECT COUNT(*) AS n, "
        "SUM(CASE WHEN score IS NOT NULL AND score>=? THEN 1 ELSE 0 END) AS keep "
        "FROM images WHERE in_quarantine=0 AND blacklisted=0 "
        "AND path IS NOT NULL", (float(threshold),))
    if not row:
        return 0, 0
    total = int(row[0][0] or 0)
    keep = int(row[0][1] or 0)
    return keep, max(0, total - keep)


def suggest(db, current: float = 0.35) -> dict:
    """给出合格线建议。**不写任何配置**。

    返回 ``ok=True`` 时带 ``suggested``；``ok=False`` 时一定带 ``note``
    说明原因与还差多少样本，调用方直接把 ``note`` 显示给用户即可。
    """
    try:
        cur = float(current)
    except (TypeError, ValueError):
        cur = 0.35
    pos, neg = collect_scores(db)
    base = {"ok": False, "pos": len(pos), "neg": len(neg),
            "current": round(cur, 4), "suggested": None}

    if len(pos) < MIN_POS or len(neg) < MIN_NEG:
        base["need_pos"] = max(0, MIN_POS - len(pos))
        base["need_neg"] = max(0, MIN_NEG - len(neg))
        base["note"] = (
            f"样本不足：已收藏 {len(pos)} 张、已判退 {len(neg)} 张，"
            f"两类各需至少 {MIN_POS} 张才能给出建议。"
            "请先在审核页继续判定图片，软件会从判定结果中学习合格线。")
        return base

    p10 = _quantile(pos, 0.10)
    n90 = _quantile(neg, 0.90)
    if p10 >= n90:
        best = (p10 + n90) / 2.0
        quality = "分界清晰"
        j = 1.0 - _rates(pos, neg, best)[1]
    else:
        best, j = _best_threshold(pos, neg, cur)
        quality = "分布重叠"

    if j <= 0:
        base["note"] = (
            "当前打分暂时无法区分「收藏」与「判退」两类图片，"
            "调整合格线反而会误伤素材。建议先继续判定图片，"
            "或检查 AI 模型是否已正确加载。")
        base["quality"] = "暂无区分度"
        return base

    best = min(CEIL, max(FLOOR, float(best)))
    keep, drop = _gallery_counts(db, best)
    keep_rate, leak_rate = _rates(pos, neg, best)
    return {
        "ok": True,
        "suggested": round(best, 3),
        "current": round(cur, 4),
        "quality": quality,
        "pos": len(pos), "neg": len(neg),
        "pos_p10": round(p10, 3), "neg_p90": round(n90, 3),
        "keep_rate": round(keep_rate, 3),
        "leak_rate": round(leak_rate, 3),
        # V0.0.56：混淆矩阵四格 + 误判样例（任务书"显示混淆矩阵与误判样例"）。
        # 均按建议阈值算，前端据此画 2×2 表、列出被误伤/漏放的图。
        "confusion": _confusion(pos, neg, best),
        "mistakes": _mistakes(db, best),
        "keep": keep, "drop": drop,
        "note": (
            f"依据 {len(pos)} 张收藏图与 {len(neg)} 张判退图，建议合格线 "
            f"{round(best, 3)}（当前 {round(cur, 3)}，{quality}）。"
            f"按此线可留住约 {round(keep_rate * 100)}% 的收藏图，"
            f"判退图误入约 {round(leak_rate * 100)}%；"
            f"当前图库预计保留 {keep} 张、落在线下 {drop} 张。"),
    }
