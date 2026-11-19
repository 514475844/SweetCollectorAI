"""#63 / A6 ·学习看板的**纯统计层**（不碰 Qt，可离线单测）。

为什么要单独拆一个模块：看板的**算法**（正负向分档、Top-N、按天补零、命中汇总）
和**界面**（QPainter 画条形图、表格）是两件事。混在一起写，
一改动图就得重算一遍数据、测试也只能起 QApplication 才能碰。

本模块只回答一个问题：**「AI 到底从我身上学到了什么？」** 全部是只读聚合查询，
不写库、不碰图片、不起线程 —— 看板可以在UI 主线程同步调（数据量是
preferences 表，量级几十~几千行，毫秒级）。

⚠️ **权重是全局的**（``preferences.task_id`` 恒为 0，见 ``repo.GLOBAL_TASK_ID``），
所以看板**一律不按任务分组** —— 按任务分列会让用户以为"A 任务学的对 B 任务没用"，
而实际上全面清理后新任务照样继承。
"""
from __future__ import annotations

import datetime as _dt

from ..i18n import label_text

# 看板默认展示的 Top-N 条数。保守取值：再多表格就要滚了，看板的意义是"一眼看完"。
TOP_N = 12
# 趋势图默认天数。两周足够看出"最近还在不在教它"，再长就成噪声了。
DAYS = 14

#: decisions 表里出现的判定值 → 中文。**没出现过的也要列出来**（值为 0），
#: 否则"这周一个都没点过"会显示成空白条，而不是一条 0 —— 空白看起来像坏了。
#:
#: R166：新增「收藏 / 彻底删除」两种样本来源。用户要求「全部进行学习」，
#: 图库收藏与隔离区彻底删除都要进看板，来源必须可区分（订单第 4 条）。
#: ⚠ 顺序即看板显示顺序：先「留」后「不留」，用户一眼能看出倾向。
DECISION_ZH = {
    "like": "喜欢",
    "restore": "还原",
    "favorite": "收藏",
    "dislike": "不喜欢",
    "purge": "彻底删除",
    "blacklist": "拉黑",
    "skip": "跳过",
}

#: R166：样本极性（正/强负…）。看板与摘要都靠它把「样本来源」讲清楚，
#: 不必在每个调用点重复判断。skip 不表态（0 权重），故不列。
DECISION_POLARITY = {
    "like": "pos",
    "restore": "pos",
    "favorite": "pos",
    "dislike": "neg",
    "blacklist": "strong_neg",
    "purge": "strong_neg",
}

#: 「强权重」门槛。|weight| 达到这个值才算学得比较确信，用于概览里的一个数字。
STRONG_ABS = 1.0


def _f(v, default: float = 0.0) -> float:
    """把库里可能为 NULL 的列安全转成float。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _today() -> _dt.date:
    return _dt.date.today()


def day_window(days: int = DAYS, today: _dt.date | None = None) -> list[str]:
    """返回最近 ``days`` 天的日期字符串列表（升序，ISO ``YYYY-MM-DD``）。

    **必须由代码补出完整窗口，不能只靠 SQL 的 GROUP BY 返回现有日期**：
    哪天没审核就没有行，图表会直接跳过那一天 ——看起来像"那天软件没在工作"，
    实际只是那天没人点。看板要给0 值行，趋势才连续。
    """
    end = today or _today()
    return [(end - _dt.timedelta(days=days - 1 - i)).isoformat()
            for i in range(days)]


def _row_to_item(r) -> dict:
    w = _f(r["weight"])
    return {
        "label": str(r["label"]),
        # 显示用中文（r40：界面中文化，存库/学习仍用原始 key）
        "text": label_text(str(r["label"])),
        "weight": w,
        "seen": _i(r["seen"]),
        "hit_pos": _i(r["hit_pos"]),
        "hit_neg": _i(r["hit_neg"]),
        # 正向=喜欢方向；负向=排除词方向；0 视为中性（导入偏好等无方向来源）
        "sign": "pos" if w > 0 else ("neg" if w < 0 else "neu"),
    }


def collect(db, days: int = DAYS, top_n: int = TOP_N) -> dict:
    """汇总学习看板要的全部数据。

    返回结构（**空库也必须有完整键**，界面才能无条件渲染）::

        {
          "has_data": bool,          # 有没有任何权重；False 时界面显示引导文案
          "labels_total": int,       # 权重条数
          "pos_count": int,          # 正权重条数（喜欢方向）
          "neg_count": int,          # 负权重条数（排除方向）
          "neu_count": int,          # 零权重条数
          "strong_count": int,       # |weight| >= STRONG_ABS 的条数
          "hit_pos_total": int,      # 主题词命中累计
          "hit_neg_total": int,      # 排除词命中累计
          "seen_total": int,         # 被表态过的总次数
          "top_pos": [ {...}, ... ], # 喜欢方向 Top-N（按权重降序）
          "top_neg": [ {...}, ... ], # 排除方向 Top-N（按权重升序，最负的在前）
          "decisions": {value: {"zh":..,"count":..}},  # reviews 判定分布（含 0 值项）
          "reviews_total": int,
          "samples": {act: {"zh":..,"count":..,"polarity":..}},  # R166 样本账本
          "sample_total": int,       # 样本账本总条数（含未进 reviews 的动作）
          "sources": {source: {act: n}},   # R166 按来源视图分组
          "daily": [{"date": "YYYY-MM-DD", "count": int}, ...],  # 定长 days 行
          "daily_max": int,          # 趋势图柱高基准（至少 1，避免除零）
          "window_days": int,
        }

    ⚠ R166：``decisions`` 与 ``samples`` **是两套账，别混**：
      · ``decisions`` 读 ``reviews`` 表 —— 那是"审核判定"的历史记录，
        受 R164 的隔离区真删除影响（删图会连带删 reviews 行）；
      · ``samples`` 读 ``learn_samples`` 账本（append-only）—— 那才是
        「用户一共给 AI 喂了多少个判断样本」的权威数字，删图也不丢。
      用户要求「所有判断动作都参与学习」，而「图库收藏」「隔离区彻底删除」
      这两类动作要么不写 reviews、要么写完就被真删除清掉 —— 只有账本记得住。
    """
    rows = db.query(
        "SELECT label, weight, seen, hit_pos, hit_neg FROM preferences "
        # 必须钉死 task_id=0：权重是全局的（repo.GLOBAL_TASK_ID）。
        # 不带这个过滤，老库里按任务分的遗留行会混进看板 ——
        # 那些行的语义（某任务自己学的）跟全局权重不是一回事，
        # 混进来会让「喜欢/排除」两栏的统计对不上真实学习结果。
        # 与 repo.get_preferences 的口径保持一致。
        "WHERE task_id=0")
    items = [_row_to_item(r) for r in rows]

    pos = sorted((i for i in items if i["sign"] == "pos"),
                 key=lambda x: (-x["weight"], x["label"]))[:top_n]
    neg = sorted((i for i in items if i["sign"] == "neg"),
                 key=lambda x: (x["weight"], x["label"]))[:top_n]

    # reviews 判定分布：先按 decision 聚合，再把没出现过的判定补成 0
    by_dec = {r["decision"]: _i(r["c"]) for r in db.query(
        "SELECT decision, COUNT(*) c FROM reviews "
        "WHERE decision IS NOT NULL GROUP BY decision")}
    decisions = {}
    for k, zh in DECISION_ZH.items():
        decisions[k] = {"zh": zh, "count": by_dec.get(k, 0)}
    # 库里若出现了未知判定值（老版本/手改），也要显示出来而不是静默丢失
    for k, c in by_dec.items():
        if k not in decisions:
            decisions[k] = {"zh": str(k), "count": c}

    # R166：样本账本分布（**权威口径**）。老库没有这张表 / 还没记过样本时
    # 一律给 0 值项 —— 与 decisions 同规矩：空白会被读成"界面坏了"。
    samples = {}
    for k, zh in DECISION_ZH.items():
        samples[k] = {"zh": zh, "count": 0,
                      "polarity": DECISION_POLARITY.get(k, "")}
    try:
        for r in db.query("SELECT act, COUNT(*) c FROM learn_samples "
                          "GROUP BY act"):
            k = str(r["act"] or "")
            if k not in samples:
                samples[k] = {"zh": DECISION_ZH.get(k, k), "count": 0,
                              "polarity": DECISION_POLARITY.get(k, "")}
            samples[k]["count"] = _i(r["c"])
    except Exception:
        pass          # 老库没有 learn_samples 表 → 全 0，不炸看板

    sources: dict = {}
    try:
        for r in db.query("SELECT source, act, COUNT(*) c FROM learn_samples "
                          "GROUP BY source, act"):
            src = str(r["source"] or "")
            sources.setdefault(src, {})[str(r["act"] or "")] = _i(r["c"])
    except Exception:
        pass

    # 趋势：按天聚合后**用 day_window 补零对齐**
    cnt = {r["d"]: _i(r["c"]) for r in db.query(
        "SELECT substr(created_at,1,10) d, COUNT(*) c FROM reviews "
        "WHERE created_at IS NOT NULL GROUP BY d")}
    # R166：账号本记的样本也并进趋势 —— 用户要看到"今天喂了多少个判断"，
    # 而收藏/彻底删除这些不写 reviews 的动作在旧口径里是**看不见的**。
    try:
        for r in db.query("SELECT substr(ts,1,10) d, COUNT(*) c "
                          "FROM learn_samples WHERE ts IS NOT NULL "
                          "GROUP BY d"):
            d = str(r["d"])
            cnt[d] = cnt.get(d, 0) + _i(r["c"])
    except Exception:
        pass
    window = day_window(days)
    daily = [{"date": d, "count": cnt.get(d, 0)} for d in window]

    return {
        "has_data": bool(items),
        "labels_total": len(items),
        "pos_count": sum(1 for i in items if i["sign"] == "pos"),
        "neg_count": sum(1 for i in items if i["sign"] == "neg"),
        "neu_count": sum(1 for i in items if i["sign"] == "neu"),
        "strong_count": sum(1 for i in items if abs(i["weight"]) >= STRONG_ABS),
        "hit_pos_total": sum(i["hit_pos"] for i in items),
        "hit_neg_total": sum(i["hit_neg"] for i in items),
        "seen_total": sum(i["seen"] for i in items),
        "top_pos": pos,
        "top_neg": neg,
        "decisions": decisions,
        "reviews_total": sum(d["count"] for d in decisions.values()),
        "samples": samples,
        "sample_total": sum(v["count"] for v in samples.values()),
        "sources": sources,
        "daily": daily,
        # 基准至少给 1：全 0 的窗口若拿 0 当除数会 ZeroDivisionError
        "daily_max": max(1, max((d["count"] for d in daily), default=0)),
        "window_days": days,
    }


def summary_line(st: dict) -> str:
    """一行摘要，给看板顶部用（也方便测试断言，不必碰界面）。

    措辞刻意说「学到的偏好」而不是「准确率」—— 本地权重**没有**任何
    准确率可算，硬编一个百分比就是骗用户。
    """
    if not st.get("has_data"):
        return "还没学到任何偏好。在审核页点「喜欢 / 不喜欢」，或对图库里的图标记一次即可。"
    return (f"共 {st['labels_total']} 条权重"
            f"（喜欢 {st['pos_count']} / 排除 {st['neg_count']}）"
            f"· 强权重 {st['strong_count']} 条"
            f"· 累计审核 {st['reviews_total']} 次")


#: R166：来源视图 → 中文（看板「样本来源」一行用）。
SOURCE_ZH = {
    "gallery": "图库",
    "review": "审核页",
    "quarantine": "隔离区",
    "web_gallery": "网页图库",
    "web_quarantine": "网页隔离区",
}


def sample_line(st: dict) -> str:
    """一行「样本来源」摘要（R166 订单第 4 条）。

    用户要求「全部进行学习」，那就得能看出**每种判断各给了多少个样本、
    从哪个视图给的**。只看权重条数看不出"我明明点过很多收藏却没学"这类问题
    —— 那个数字在这行里。

    ⚠ 与 ``summary_line`` 的分工：summary 讲的是**模型状态**（有多少条权重），
    本函数讲的是**用户行为**（喂了多少个样本）。两者都对不上时，
    说明有动作没接进学习链路 —— 这正是本函数存在的意义。
    """
    samples = st.get("samples") or {}
    rows = [(k, v) for k, v in samples.items()
            if _i((v or {}).get("count")) > 0]
    if not rows:
        return "样本来源：还没记录（做一次审核 / 收藏 / 移除即会记上）"
    # 按 DECISION_ZH 的顺序排（那是刻意设计的"先留后不留"阅读顺序），
    # 表里没有的键（未知动作）排到后面，别打乱主序列。
    order = {k: i for i, k in enumerate(DECISION_ZH)}
    rows.sort(key=lambda kv: order.get(kv[0], 999))
    body = " · ".join(
        f"{(v or {}).get('zh') or k} {_i((v or {}).get('count'))}"
        for k, v in rows)
    line = f"样本来源：{body}（共 {_i(st.get('sample_total'))} 个判断样本）"
    srcs = st.get("sources") or {}
    if srcs:
        # 来源视图也列出来 —— 用户要知道"这些样本是我在哪点的"。
        parts = []
        for name, acts in sorted(srcs.items()):
            n = sum(_i(c) for c in (acts or {}).values())
            if n:
                parts.append(f"{SOURCE_ZH.get(name, name or '未知')} {n}")
        if parts:
            line += "；来自：" + " · ".join(parts)
    return line


def preference_origins(db) -> dict:
    """R174：按来源给学习权重分个类（看板/网页同源，口径只有这一处）。

    返回 ``{"user_typed": n, "model_auto": n, "model_auto_neg": n}``。

    为什么要单独一个数 ``model_auto_neg``：它是**唯一**会被「一键清模型
    自动词」干掉的那批，看板上必须先让用户看见有多少条，别点了才知道。
    """
    out = {"user_typed": 0, "model_auto": 0, "model_auto_neg": 0,
           "user_imported": 0}
    try:
        rows = db.query("SELECT weight, source FROM preferences WHERE task_id=0")
    except Exception:
        return out
    for r in rows:
        try:
            src = str(r["source"] or "").strip() or "model_auto"
        except Exception:
            src = "model_auto"
        # V0.0.67：user_imported（从用户原有图库批量学的）必须单列。
        # 原来落到 model_auto ⇒ 看板显示「模型自动 9 条」，用户以为机器自己
        # 猜的，甚至误点「清模型自动词」。虽因只清负权不会真删正权，但标注
        # 错了就是对用户撒谎。
        if src not in ("user_typed", "model_auto", "user_imported"):
            src = "model_auto"
        out[src] += 1
        if src == "model_auto" and _f(r["weight"]) < 0:
            out["model_auto_neg"] += 1
    return out


def origin_line(counts: dict) -> str:
    """一行中文说明（与 sample_line 同风格，两端共用）。"""
    c = dict(counts or {})
    return (f"学习来源：用户手填 {_i(c.get('user_typed'))} 条 · "
            f"图库导入 {_i(c.get('user_imported'))} 条 · "
            f"模型自动 {_i(c.get('model_auto'))} 条"
            f"（其中负权 {_i(c.get('model_auto_neg'))} 条，可批量清除）")


def bar_ratio(item: dict, scale: float) -> float:
    """把一条权重换算成 0~1 的条长比例（画图用）。

    ⚠️ 比例夹在 0~1，且 ``scale<=0`` 时返回 0 —— 调用方可能传进来0
    （比如权重全被改成了 0），除零会直接把paintEvent 里的绘制打断，
    表现为「整个看板空白」。宁可画成 0 长度的条，也不要崩。
    """
    try:
        s = float(scale)
    except (TypeError, ValueError):
        return 0.0
    if s <= 0:
        return 0.0
    return max(0.0, min(1.0, abs(_f(item.get("weight"))) / s))


def top_scale(items: list[dict]) -> float:
    """给定一组权重，返回画条形图的权重量级基准（最大绝对值）。

    空列表返回 1.0（而不是 0）—— 同上，避免除零。
    """
    m = max((abs(_f(i.get("weight"))) for i in items), default=0.0)
    return m if m > 0 else 1.0