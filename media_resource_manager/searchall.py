"""V0.0.18 · 全局搜索（**纯逻辑**：只读 LIKE，不写任何数据）。

一个关键词同时搜：任务（名称/入口网址）、图库（标签/路径）、审计日志
（动作/对象/详情）。桌面「全局搜索」对话框与网页 `/api/search/all` 共用
本模块 —— 口径只有一处，两端结果必须一致。

安全纪律：
  · 全参数化 SQL；
  · LIKE 通配符（% _ \\）先转义再匹配，用户输入 ``%`` 不会变成全表扫描；
  · 关键词截 60 字符，各组限量返回（任务/图 20、日志 10），只读无副作用。
"""
from __future__ import annotations

import os

_ESC = " ESCAPE '\\'"


def search_all(db, kw: str) -> dict:
    """返回 ``{"ok", "q", "tasks": [...], "images": [...], "logs": [...]}``。

    任何一组查询失败退化成空列表，不影响其它组。
    """
    kw = str(kw or "").strip()[:60]
    if not kw:
        return {"ok": False, "error": "请输入搜索关键词",
                "tasks": [], "images": [], "logs": []}
    core = kw.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    like = f"%{core}%"

    tasks: list[dict] = []
    try:
        rows = db.query(
            "SELECT id, name, status FROM tasks "
            f"WHERE name LIKE ?{_ESC} OR entry_urls LIKE ?{_ESC} "
            "ORDER BY id DESC LIMIT 20", (like, like))
        tasks = [{"id": int(r["id"]), "name": str(r["name"] or ""),
                  "status": str(r["status"] or "")} for r in rows]
    except Exception:
        pass

    imgs: list[dict] = []
    try:
        rows = db.query(
            "SELECT id, path, tags, score, task_id FROM images "
            "WHERE in_quarantine=0 AND blacklisted=0 AND path IS NOT NULL "
            f"AND (tags LIKE ?{_ESC} OR path LIKE ?{_ESC}) "
            "ORDER BY score DESC, id DESC LIMIT 20", (like, like))
        for r in rows:
            p = str(r["path"] or "")
            imgs.append({"id": int(r["id"]),
                         "name": os.path.basename(p),
                         "score": round(float(r["score"] or 0), 4),
                         "task_id": int(r["task_id"] or 0),
                         "thumb_url": f"/thumb/{int(r['id'])}"})
    except Exception:
        pass

    logs: list[dict] = []
    try:
        rows = db.query(
            "SELECT id, ts, actor, action, target, detail FROM audit_log "
            f"WHERE action LIKE ?{_ESC} OR target LIKE ?{_ESC} "
            f"OR COALESCE(detail,'') LIKE ?{_ESC} "
            "ORDER BY id DESC LIMIT 10", (like, like, like))
        logs = [{"id": int(r["id"]), "ts": r["ts"] or "",
                 "actor": str(r["actor"] or ""),
                 "action": str(r["action"] or ""),
                 "target": str(r["target"] or ""),
                 "detail": str(r["detail"] or "")[:120]} for r in rows]
    except Exception:
        pass

    return {"ok": True, "q": kw, "tasks": tasks, "images": imgs, "logs": logs}


def image_row_for_lightbox(db, image_id: int) -> dict | None:
    """按 id 取一条可直接喂给桌面灯箱的行（path/宽高/来源/标签/评分）。

    对话框侧禁止内嵌 SQL，取数口径统一放本模块；查不到返回 None。
    """
    try:
        rows = db.query(
            "SELECT id, path, width, height, source_url, tags, score "
            "FROM images WHERE id=?", (int(image_id),))
    except Exception:
        return None
    return dict(rows[0]) if rows else None
