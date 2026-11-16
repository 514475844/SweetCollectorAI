"""V0.0.18 · 通知中心（欠账 #8）。

SSE `/api/events` 只推「正在看页面的人」，关掉浏览器消息就没了。本模块把
任务完成/失败、Cookie 疑似过期、备份异常等事件**落库累积**，页面重开仍可
回看；SSE 的版本号机制天然把 notifications 表算进去即可实时到达。

纪律：
  · 只在 `notifications` 表读写，聚合口径一处收口；
  · `add()` 必须吞掉一切异常——通知记不上绝不能反伤业务动作；
  · 自动去重：同 kind+title 一小时内只记一条（防止失败刷屏）。
"""
from __future__ import annotations

import datetime as _dt

MAX_KEEP = 500          # 只留最近 500 条，老消息自动滚出
DEDUP_MIN = 60          # 同 kind+title 去重窗口（分钟）

_LEVELS = ("info", "warn", "error")


def add(db, kind: str, title: str, body: str = "",
        link: str = "", level: str = "info") -> None:
    """记一条通知。全程吞异常：通知失败不能影响调用方。"""
    try:
        kind = str(kind or "info")[:40]
        title = str(title or "").strip()[:200]
        if not title:
            return
        body = str(body or "")[:1000]
        link = str(link or "")[:200]
        if level not in _LEVELS:
            level = "info"
        # 去重：窗口内同 kind+title 只更新时间和正文，不堆行
        row = db.query(
            "SELECT id FROM notifications WHERE kind=? AND title=? "
            "AND read=0 AND ts >= datetime('now', ?) ORDER BY id DESC LIMIT 1",
            (kind, title, f"-{DEDUP_MIN} minutes"))
        if row:
            db.execute("UPDATE notifications SET body=?, ts=datetime('now') "
                       "WHERE id=?", (body, int(row[0]["id"])))
        else:
            db.execute(
                "INSERT INTO notifications(kind, level, title, body, link) "
                "VALUES (?,?,?,?,?)", (kind, level, title, body, link))
        # 滚动清理
        db.execute(
            "DELETE FROM notifications WHERE id <= "
            "(SELECT MAX(id)-? FROM notifications)", (MAX_KEEP,))
        db._conn.commit()
    except Exception:
        pass


def _has_column(db, table: str, col: str) -> bool:
    try:
        return col in {r["name"] for r in
                       db.query(f"PRAGMA table_info({table})")}
    except Exception:
        return False


def list_all(db, limit: int = 100, only_unread: bool = False) -> dict:
    try:
        limit = min(300, max(1, int(limit or 100)))
    except (TypeError, ValueError):
        limit = 100
    where = "read=0" if only_unread else "1=1"
    rows = db.query(
        f"SELECT id, ts, kind, level, title, body, link, read "
        f"FROM notifications WHERE {where} ORDER BY id DESC LIMIT ?",
        (limit,))
    items = [{
        "id": int(r["id"]), "ts": r["ts"], "kind": r["kind"],
        "level": r["level"], "title": r["title"], "body": r["body"] or "",
        "link": r["link"] or "", "read": bool(r["read"]),
    } for r in rows]
    try:
        unread = int(db.query(
            "SELECT COUNT(*) AS n FROM notifications WHERE read=0")[0]["n"] or 0)
    except Exception:
        unread = sum(1 for x in items if not x["read"])
    return {"ok": True, "items": items, "unread": unread}


def mark_read(db, ids: list | None = None, all_read: bool = False) -> dict:
    try:
        if all_read:
            db.execute("UPDATE notifications SET read=1 WHERE read=0")
        else:
            for x in (ids or [])[:300]:
                try:
                    db.execute("UPDATE notifications SET read=1 WHERE id=?",
                               (int(x),))
                except (TypeError, ValueError):
                    continue
        db._conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"标记已读失败：{e}"}


def clear(db, older_than_days: int = 0) -> dict:
    """清空通知；``older_than_days``>0 时只清更早的。"""
    try:
        if older_than_days > 0:
            db.execute("DELETE FROM notifications WHERE "
                       "ts < datetime('now', ?)",
                       (f"-{int(older_than_days)} days",))
        else:
            db.execute("DELETE FROM notifications")
        db._conn.commit()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"清空通知失败：{e}"}


def since_id(db) -> int:
    """当前最大 id（SSE/前端增量轮询基线）。"""
    try:
        row = db.query("SELECT COALESCE(MAX(id),0) AS m FROM notifications")
        return int(row[0]["m"] or 0) if row else 0
    except Exception:
        return 0
