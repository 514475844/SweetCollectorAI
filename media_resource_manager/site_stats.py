"""V0.0.18 · 站点采集统计（**纯逻辑**：只读聚合，不写任何数据）。

回答四个问题（用户欠账 #4）：
  · 每个站（=每个任务）采到了多少图、成功率如何；
  · 平均速率（图/小时，按该任务首末采集时间跨度折算）；
  · 失败原因 Top（failed_urls.error 聚合）；
  · 每个任务占了多少磁盘（images.bytes 聚合，含「文件已不在」的差值提示）。

Cookie 过期提醒（欠账 #5）也在本模块：失败原因文本命中鉴权类特征词
（401/403/cookie/login/expired/unauthorized/需要登录 等）时，把该任务的
失败标记为「疑似登录失效」，前端据此提示去「站点 Cookie 管理」更新。

桌面与网页共用这一份口径，禁止在两端各写一套 SQL。
"""
from __future__ import annotations

import datetime as _dt
import re

# 鉴权失效特征（大小写不敏感）。命中即建议检查 Cookie。
_AUTH_PAT = re.compile(
    r"\b(401|403|407|unauthorized|forbidden|cookie|login|log\s?in|"
    r"expired|session|auth)\b|需要登录|登录失效|登录过期|未授权|无权限|鉴权",
    re.IGNORECASE)


def _host_from_url(url: str) -> str:
    m = re.match(r"https?://([^/]+)", str(url or ""))
    return m.group(1) if m else ""


def task_stats(db, days: int = 0) -> dict:
    """按任务聚合的采集统计。``days``>0 时只统计最近 N 天的图片。

    返回 ``{"ok": True, "items": [...], "generated_at": "..."}``。
    每个 item：任务名 / 站点域名 / 图数 / 收藏数 / 失败URL数 /
    成功率（图数÷(图数+失败URL数)）/ 平均速率 / 磁盘占用 /
    疑似登录失效（bool） / 失败原因 Top3。
    """
    try:
        days = max(0, int(days or 0))
    except (TypeError, ValueError):
        days = 0
    win = ""
    args: tuple = ()
    if days:
        win = " AND collected_at >= datetime('now', ?)"
        args = (f"-{days} days",)

    tasks = db.query("SELECT id, name, entry_urls FROM tasks "
                     "WHERE COALESCE(is_template,0)=0 ORDER BY id")
    items = []
    for t in tasks:
        tid = int(t["id"])
        sql = ("SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS sz, "
               "COALESCE(SUM(favorite),0) AS fav, "
               "MIN(collected_at) AS t0, MAX(collected_at) AS t1 "
               f"FROM images WHERE task_id=?{win}")
        qargs: tuple = (tid,) + ((f"-{days} days",) if days else ())
        row = db.query(sql, qargs)
        r = row[0] if row else None
        n = int(r["n"] or 0) if r else 0
        size = int(r["sz"] or 0) if r else 0
        fav = int(r["fav"] or 0) if r else 0

        frow = db.query("SELECT COUNT(*) AS fn FROM failed_urls "
                        "WHERE task_id=?", (tid,))
        fn = int(frow[0]["fn"] or 0) if frow else 0

        rate = None
        try:
            t0, t1 = (r["t0"] if r else None), (r["t1"] if r else None)
            if n >= 2 and t0 and t1 and t0 != t1:
                d0 = _dt.datetime.fromisoformat(str(t0).replace("Z", ""))
                d1 = _dt.datetime.fromisoformat(str(t1).replace("Z", ""))
                hours = abs((d1 - d0).total_seconds()) / 3600.0
                if hours > 0.01:
                    rate = round(n / hours, 2)
        except (ValueError, TypeError, KeyError):
            rate = None

        # 失败原因 Top3 + 鉴权归因
        reasons = []
        suspect_auth = False
        try:
            fr = db.query("SELECT error, SUM(COALESCE(tries,1)) AS c "
                          "FROM failed_urls WHERE task_id=? "
                          "GROUP BY error ORDER BY c DESC LIMIT 3", (tid,))
            for x in fr:
                e = str(x["error"] or "未知原因")[:120]
                reasons.append({"error": e, "count": int(x["c"] or 0)})
                if _AUTH_PAT.search(e):
                    suspect_auth = True
        except Exception:
            pass

        hosts = set()
        try:
            import json as _json
            urls = _json.loads(t["entry_urls"] or "[]")
            for u in urls:
                h = _host_from_url(u)
                if h:
                    hosts.add(h.lower().lstrip("www."))
        except (ValueError, TypeError):
            pass

        denom = n + fn
        items.append({
            "task_id": tid,
            "task": str(t["name"] or ""),
            "hosts": sorted(hosts)[:3],
            "images": n,
            "favorites": fav,
            "failed_urls": fn,
            "success_rate": round(n / denom, 4) if denom else None,
            "images_per_hour": rate,
            "disk_bytes": size,
            "auth_suspect": suspect_auth and fn > 0,
            "top_errors": reasons,
        })
    items.sort(key=lambda x: (-x["images"], -x["failed_urls"]))
    return {"ok": True,
            "days": days,
            "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "items": items}


def cookie_expiry_hints(db) -> list[dict]:
    """给「Cookie 过期提醒」用：返回疑似登录失效的任务与涉及域名。

    判定只看两件事：该任务失败 URL 的报错文本命中鉴权特征，并且该域名
    在 Cookie 表里存过 —— 有 Cookie 才谈得上过期，没存过的直接建议去添加。
    """
    hints = []
    try:
        st = task_stats(db)
    except Exception:
        return hints
    from . import cookies as ck
    for it in st.get("items", []):
        if not it.get("auth_suspect"):
            continue
        have = []
        missing = []
        for h in it.get("hosts", []):
            try:
                have_ok = bool(ck.get_cookie(h))
            except Exception:
                have_ok = False
            (have if have_ok else missing).append(h)
        if have or missing:
            hints.append({
                "task_id": it["task_id"],
                "task": it["task"],
                "hosts_with_cookie": have,
                "hosts_without_cookie": missing,
                "sample_errors": [e["error"] for e in it.get("top_errors", [])][:2],
            })
    return hints
