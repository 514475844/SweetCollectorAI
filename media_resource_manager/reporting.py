"""R154-F25 ·采集周报 / 月报的**纯统计层**（不碰 Qt、不碰文件 IO）。

订单原文：学习看板或独立页：按周/月统计（采集张数、通过率、不喜欢 top 词、
任务排行）；可导出 Markdown/CSV；数据源复用 stats/learning_stats，不另写统计口径。

为什么单独拆一层：与 ``classify/learning_stats.py`` 同一个理由 —— 算法和界面
分开，才能**不起 QApplication 就单测**，也才能让桌面端与网页端拿到
**同一个数字**（两端各写一份 SQL，用户就会看到「桌面说 12、手机说 9」）。

⚠ 三条口径纪律（改本文件前必读）
1. **不发明统计口径**。「通过率」里哪个判定算正向、哪个算负向，一律借用
   ``learning_stats.DECISION_POLARITY``（R166 定下来的那张表，两端共用）。
   在这里重写一份，早晚和看板对不上 —— 那时用户看到的是「同一件事两个数字」。
2. **「不喜欢 top 词」是「当前学到的」，不是「本期新增的」**。
   ``preferences`` 表**没有时间戳**（learning_stats 模块说明写得很清楚：
   权重是全局的，既不分任务也不分时间），硬按周切片只能伪造数据。
   所以这里照搬 ``learning_stats.collect()['top_neg']``，并在报告里**写明**
   这一条，不许让读者以为是本期新增。
3. **没有分母的通过率给 ``None``，不给 ``0.0``**。「本期一张都没判」和
   「判了 100 张全不喜欢」是两件事，都显示 0% 等于骗人。界面层负责把
   ``None`` 翻成「本期还没有判定」。
"""
from __future__ import annotations

import datetime as _dt

from .classify.learning_stats import DECISION_POLARITY, DECISION_ZH, collect as _ls_collect

#: 周期类型。刻意只给两种：再多就是噪声，周/月已经够看趋势了。
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"

PERIOD_ZH = {PERIOD_WEEK: "周报", PERIOD_MONTH: "月报"}

#: 任务排行 / 不喜欢 top 词的默认条数。看板的规矩是「一眼看完」，报告同理。
TOP_N = 8

#: 通过率的显示精度（百分比保留一位小数足够，两位是假精度）。
_RATE_NDIGITS = 1


def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _today() -> _dt.date:
    return _dt.date.today()


def period_range(kind: str = PERIOD_WEEK, today: _dt.date | None = None,
                 offset: int = 0) -> dict:
    """算出一期的时间窗口（**闭区间**，两端都含）。

    返回 ``{"kind","start","end","label","offset"}``，日期一律 ISO
    ``YYYY-MM-DD``（与库里 ``datetime('now')`` 的前 10 位同型，可直接
    ``substr(col,1,10) BETWEEN start AND end``）。

    - ``week``：**周一 ~ 周日**（ISO 周，与日历应用一致，不是「往前数 7 天」）；
    - ``month``：1 号 ~ 月末；
    - ``offset``：往前几期，**0 = 当期**。

    ⚠ ``offset < 0``（未来期）一律**钳回 0**：未来没有数据，允许翻过去只会
    看到一张空表，用户会以为是软件坏了。
    """
    k = str(kind or PERIOD_WEEK).strip().lower()
    if k not in PERIOD_ZH:
        k = PERIOD_WEEK
    try:
        off = int(offset)
    except (TypeError, ValueError):
        off = 0
    if off < 0:
        off = 0

    base = today or _today()
    if k == PERIOD_WEEK:
        start = base - _dt.timedelta(days=base.weekday())
        start = start - _dt.timedelta(weeks=off)
        end = start + _dt.timedelta(days=6)
        label = (f"{start.isoformat()} ~ {end.isoformat()}"
                 f"（{start.isocalendar()[1]} 周）")
    else:
        # 按月回退：用「第几年第几个月」的整数运算，别用 timedelta(days=30)
        # —— 30 天不是一个月，跨 2 月会直接漂到下下个月。
        y, m = base.year, base.month - off
        while m <= 0:
            m += 12
            y -= 1
        start = _dt.date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        end = _dt.date(ny, nm, 1) - _dt.timedelta(days=1)
        label = f"{start.isoformat()} ~ {end.isoformat()}（{y} 年 {m} 月）"

    return {"kind": k, "start": start.isoformat(), "end": end.isoformat(),
            "label": label, "offset": off}


def _daily_window(start: str, end: str) -> list[str]:
    """把 [start, end] 展开成逐日列表（升序）。

    **必须由代码补出完整窗口**（与 learning_stats.day_window 同规矩）：
    哪天没采图就没有行，图表会跳过那天 —— 看起来像「那天软件没工作」，
    实际只是那天没采到。报告要给 0 值行，趋势才连续。
    """
    try:
        d0 = _dt.date.fromisoformat(start)
        d1 = _dt.date.fromisoformat(end)
    except (TypeError, ValueError):
        return []
    if d1 < d0:
        return []
    out = []
    cur = d0
    while cur <= d1:
        out.append(cur.isoformat())
        cur += _dt.timedelta(days=1)
    return out


def _q(db, sql: str, params=()):
    """跑一条只读查询。``db`` 不是真库（测试桩）时退化成空列表，不炸报告。"""
    try:
        return list(db.query(sql, params))
    except Exception:
        return []


def collect_period(db, start: str, end: str, top_n: int = TOP_N) -> dict:
    """汇总某一期的全部指标。

    返回结构（**空库也必须有完整键**，界面才能无条件渲染）::

        {
          "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
          "collected": int,      # 本期入库图片数（images.collected_at）
          "favorites": int,      # 其中收藏数
          "quarantined": int,    # 其中进了隔离区的
          "decisions": {value: {"zh":.., "count":..}},   # 判定分布（含 0 值项）
          "reviewed": int,       # 本期有判定的总数
          "pos_count": int, "neg_count": int,
          "pass_rate": float | None,   # 通过率（无判定= None）
          "daily": [{"date":.., "count": int}, ...],     # 逐日采集（补零）
          "daily_max": int,
          "top_dislike": [ {...}, ... ],  # 不喜欢方向 Top-N（**全局口径**，见模块说明 2）
          "task_rank": [{"task_id":..,"name":..,"images":..,
                         "liked":..,"disliked":..,"pass_rate": float|None}, ...],
          "has_data": bool,
        }
    """
    if not start or not end:
        return _empty_period(start, end, top_n)
    if start > end:
        start, end = end, start

    p = (start, end)

    collected = _i(_scalar(_q(db, "SELECT COUNT(*) c FROM images "
                              "WHERE substr(collected_at,1,10) BETWEEN ? AND ?", p)))
    favorites = _i(_scalar(_q(db, "SELECT COUNT(*) c FROM images "
                              "WHERE favorite=1 AND "
                              "substr(collected_at,1,10) BETWEEN ? AND ?", p)))
    quarantined = _i(_scalar(_q(db, "SELECT COUNT(*) c FROM images "
                                "WHERE in_quarantine=1 AND "
                                "substr(collected_at,1,10) BETWEEN ? AND ?", p)))

    # 判定分布：先聚合，再把没出现过的判定补成 0（同 learning_stats 的规矩）
    by_dec = {str(r["decision"]): _i(r["c"]) for r in _q(
        db, "SELECT decision, COUNT(*) c FROM reviews "
            "WHERE decision IS NOT NULL AND "
            "substr(created_at,1,10) BETWEEN ? AND ? GROUP BY decision", p)}
    decisions = {}
    for k, zh in DECISION_ZH.items():
        decisions[k] = {"zh": zh, "count": by_dec.get(k, 0)}
    for k, c in by_dec.items():
        if k not in decisions:
            decisions[k] = {"zh": str(k), "count": c}

    pos_count = sum(c for d, c in by_dec.items()
                    if DECISION_POLARITY.get(d) == "pos")
    neg_count = sum(c for d, c in by_dec.items()
                    if DECISION_POLARITY.get(d) in ("neg", "strong_neg"))
    reviewed = pos_count + neg_count
    pass_rate = (pos_count / reviewed) if reviewed else None

    # 逐日采集趋势（补零对齐）
    cnt = {str(r["d"]): _i(r["c"]) for r in _q(
        db, "SELECT substr(collected_at,1,10) d, COUNT(*) c FROM images "
            "WHERE substr(collected_at,1,10) BETWEEN ? AND ? GROUP BY d", p)}
    daily = [{"date": d, "count": cnt.get(d, 0)} for d in _daily_window(start, end)]

    # 任务排行：本期采集量降序；喜欢/不喜欢按 reviews.task_id 归到任务上
    names = {_i(r["id"]): str(r["name"]) for r in _q(db, "SELECT id, name FROM tasks")}
    imgs = {_i(r["t"]): _i(r["c"]) for r in _q(
        db, "SELECT task_id t, COUNT(*) c FROM images "
            "WHERE substr(collected_at,1,10) BETWEEN ? AND ? GROUP BY task_id", p)}
    likes: dict = {}
    dislikes: dict = {}
    for r in _q(db, "SELECT task_id t, decision, COUNT(*) c FROM reviews "
                    "WHERE decision IS NOT NULL AND task_id IS NOT NULL AND "
                    "substr(created_at,1,10) BETWEEN ? AND ? "
                    "GROUP BY task_id, decision", p):
        t = _i(r["t"])
        c = _i(r["c"])
        pol = DECISION_POLARITY.get(str(r["decision"]))
        if pol == "pos":
            likes[t] = likes.get(t, 0) + c
        elif pol in ("neg", "strong_neg"):
            dislikes[t] = dislikes.get(t, 0) + c
    rank = []
    for tid, n in imgs.items():
        lk, dk = likes.get(tid, 0), dislikes.get(tid, 0)
        tot = lk + dk
        rank.append({
            "task_id": tid,
            "name": names.get(tid, f"任务 {tid}"),
            "images": n,
            "liked": lk,
            "disliked": dk,
            "pass_rate": (lk / tot) if tot else None,
        })
    # 采集量降序，同量按任务号升序（排序必须全序，否则每次打开顺序都在跳
    # —— 这是看图表的第一体验杀手）
    rank.sort(key=lambda x: (-x["images"], x["task_id"]))
    rank = rank[:max(1, int(top_n or TOP_N))]

    # 不喜欢 top 词：**全局口径**（preferences 无时间戳，见模块说明 2）
    top_dislike = []
    try:
        top_dislike = list(_ls_collect(db).get("top_neg") or [])[:max(1, int(top_n or TOP_N))]
    except Exception:
        top_dislike = []

    return {
        "start": start,
        "end": end,
        "collected": collected,
        "favorites": favorites,
        "quarantined": quarantined,
        "decisions": decisions,
        "reviewed": reviewed,
        "pos_count": pos_count,
        "neg_count": neg_count,
        "pass_rate": pass_rate,
        "daily": daily,
        "daily_max": max(1, max((d["count"] for d in daily), default=0)),
        "top_dislike": top_dislike,
        "task_rank": rank,
        "has_data": bool(collected or reviewed),
    }


def _scalar(rows):
    if not rows:
        return 0
    try:
        return rows[0]["c"]
    except Exception:
        return 0


def _empty_period(start: str, end: str, top_n: int = TOP_N) -> dict:
    """空档期的完整骨架（日期非法 / 区间为空时也给界面一个能画的东西）。"""
    return {
        "start": start or "", "end": end or "",
        "collected": 0, "favorites": 0, "quarantined": 0,
        "decisions": {k: {"zh": zh, "count": 0} for k, zh in DECISION_ZH.items()},
        "reviewed": 0, "pos_count": 0, "neg_count": 0, "pass_rate": None,
        "daily": [], "daily_max": 1, "top_dislike": [], "task_rank": [],
        "has_data": False,
    }


# ---------------- 展示层：一行中文（两端共用，不许各写一份） ----------------

def pass_rate_text(rate) -> str:
    """通过率 → 中文。**没有分母就明说没有**，不给 0.0%（模块说明 3）。"""
    if rate is None:
        return "本期还没有判定"
    r = _f(rate)
    if r < 0:
        r = 0.0
    if r > 1:
        r = 1.0
    return f"{r * 100:.{_RATE_NDIGITS}f}%"


def headline(rep: dict) -> str:
    """报告顶部一行摘要（桌面窗口标题下方 / 网页标题下方同一句）。"""
    r = rep or {}
    rate = pass_rate_text(r.get("pass_rate"))
    return (f"{r.get('start', '')} ~ {r.get('end', '')}："
            f"采集 {_i(r.get('collected'))} 张 · "
            f"已判定 {_i(r.get('reviewed'))} 张 · 通过率 {rate}")


# ---------------- 导出 ----------------

def to_markdown(rep: dict, kind: str = PERIOD_WEEK) -> str:
    """完整报告 → Markdown 字符串。

    ⚠ 报告里**必须写明**「不喜欢 top 词是全局口径」—— 它是唯一一个
    不随时间切片的指标，不写就是误导（模块说明 2）。
    """
    r = rep or {}
    zh = PERIOD_ZH.get(str(kind or PERIOD_WEEK).strip().lower(), "周报")
    L: list[str] = []
    L.append(f"# 采集{zh}（{r.get('start', '')} ~ {r.get('end', '')}）")
    L.append("")
    L.append(f"- 采集张数：**{_i(r.get('collected'))}**")
    L.append(f"- 收藏：{_i(r.get('favorites'))} · 进隔离区：{_i(r.get('quarantined'))}")
    L.append(f"- 已判定：{_i(r.get('reviewed'))}"
             f"（正向 {_i(r.get('pos_count'))} / 负向 {_i(r.get('neg_count'))}）")
    L.append(f"- 通过率：**{pass_rate_text(r.get('pass_rate'))}**")
    L.append("")

    decs = r.get("decisions") or {}
    used = [(k, v) for k, v in decs.items() if _i((v or {}).get("count")) > 0]
    if used:
        L.append("## 判定分布")
        L.append("")
        L.append("| 判定 | 次数 |")
        L.append("| --- | --- |")
        for k, v in used:
            L.append(f"| {(v or {}).get('zh') or k} | {_i((v or {}).get('count'))} |")
        L.append("")

    daily = r.get("daily") or []
    if daily:
        L.append("## 每日采集")
        L.append("")
        L.append("| 日期 | 张数 |")
        L.append("| --- | --- |")
        for d in daily:
            L.append(f"| {d.get('date', '')} | {_i(d.get('count'))} |")
        L.append("")

    tops = r.get("top_dislike") or []
    L.append("## 不喜欢 Top 词")
    L.append("")
    if tops:
        L.append("| 词 | 权重 |")
        L.append("| --- | --- |")
        for t in tops:
            L.append(f"| {t.get('text') or t.get('label') or ''} | "
                     f"{_f(t.get('weight')):.2f} |")
        L.append("")
        L.append("> 学习权重是**全局**的（不分任务也不分时间），"
                 "上表是「目前学到的排除词」，不是本期新增。")
    else:
        L.append("还没有学到排除方向的偏好。")
    L.append("")

    rank = r.get("task_rank") or []
    L.append("## 任务排行")
    L.append("")
    if rank:
        L.append("| 任务 | 采集 | 喜欢 | 不喜欢 | 通过率 |")
        L.append("| --- | --- | --- | --- | --- |")
        for t in rank:
            L.append(f"| {t.get('name', '')} | {_i(t.get('images'))} | "
                     f"{_i(t.get('liked'))} | {_i(t.get('disliked'))} | "
                     f"{pass_rate_text(t.get('pass_rate'))} |")
    else:
        L.append("本期没有采集记录。")
    L.append("")
    return "\n".join(L)


def to_csv(rep: dict) -> str:
    """任务排行 → CSV 字符串（**UTF-8 BOM 开头**，Excel 双击不乱码）。

    ⚠ 为什么 CSV 只导任务排行、不导整份报告：CSV 是**一张表**的格式，
    把「概览 + 趋势 + 词表 + 排行」四块不同列数的内容塞进一个文件，
    得到的只能是人看不懂、Excel 也识别不了的东西。完整报告走 Markdown
    （``to_markdown``），表格数据走 CSV —— 各用各的长处。
    """
    r = rep or {}
    out = ["\ufeff任务,采集张数,喜欢,不喜欢,通过率"]
    for t in (r.get("task_rank") or []):
        out.append(_csv_row([
            str(t.get("name", "")), _i(t.get("images")), _i(t.get("liked")),
            _i(t.get("disliked")), pass_rate_text(t.get("pass_rate")),
        ]))
    return "\r\n".join(out) + "\r\n"


def _csv_row(cells: list) -> str:
    """一格一格的 CSV 转义：**含逗号/引号/换行就加引号，内部引号翻倍**。

    ⚠ 任务名是用户自己起的，逗号和引号都很常见；不做转义会让 Excel
    把一行切成两列，而这类错位**看过去像数据错了**。
    """
    out = []
    for c in cells:
        s = str(c)
        if any(ch in s for ch in (',', '"', '\n', '\r')):
            s = '"' + s.replace('"', '""') + '"'
        out.append(s)
    return ",".join(out)
