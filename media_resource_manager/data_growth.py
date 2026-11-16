"""长期运行的数据增长控制（R247）。

软件要 7×24 挂着跑，量只会一直涨。四张表**只增不减**：

====================  ==============================================
表                     谁在写
====================  ==============================================
``audit_log``          采集时每张图的「模糊 / 黑名单 / 超大送审」各一条
``task_pages``         每抓一页一条（断点续采的账本，天然按任务堆积）
``failed_urls``        失败网址（重试成功会被删，但失败的会留着）
``disk_snapshots``     每天一条（不急，两年才 730 行）
====================  ==============================================

10 万张图大约是 3–8 万条 ``audit_log``；百万级就是几十万到上百万行。
数量本身 SQLite 扛得住，问题是**永远不清**：库文件只会变大，备份
（整库 zip）也跟着变大，最后「备份一次半小时」。

策略：**按时间保留 + 按行数封顶，双保险**。
  · 按时间：清掉「均匀增长」的正常历史；
  · 按行数：封住「某一晚疯狂刷」的尖峰（时间还没到但已经几十万行）。

为什么按 id 封顶而不是按 ts 排序：``id`` 是 AUTOINCREMENT，单调递增
≈ 时间序，``WHERE id > max_id - N`` 一次主键范围扫描就够；按 ts 排序
要动临时 B 树。两者都做，各自负责一件事。

调用时机：服务启动时跑一次，之后每 24 小时一次（见 webui 的维护线程）。
清理失败**绝不抛**：清不掉旧日志是小事，让服务起不来是大事。
"""
from __future__ import annotations

#: 各表的默认保留策略（天 / 行）。
DEFAULTS = {
    "audit_days": 90,
    "audit_rows": 200_000,
    "pages_days": 30,
    "failed_days": 90,
    "snap_days": 730,
}


def _int(v, fallback: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    return n if n > 0 else fallback


def _del(db, sql: str, params=()) -> int:
    try:
        cur = db.execute(sql, params)
        n = cur.rowcount
        return n if isinstance(n, int) and n > 0 else 0
    except Exception:
        return 0


def prune_growth(db, cfg=None, *, force: bool = False) -> dict:
    """按保留策略清理增长表，返回 ``{表名: 删除行数}``。

    ``cfg`` 缺省时用 :data:`DEFAULTS`。读配置一律带类型兜底 ——
    这几个值可能被用户改成 0 或字符串，不能让一次清理把表清空。
    """
    g = dict(DEFAULTS)
    if cfg is not None:
        g["audit_days"] = _int(getattr(cfg, "retain_audit_days", 0), 90)
        g["audit_rows"] = _int(getattr(cfg, "retain_audit_rows", 0), 200_000)
        g["pages_days"] = _int(getattr(cfg, "retain_pages_days", 0), 30)
        g["failed_days"] = _int(getattr(cfg, "retain_failed_days", 0), 90)
        g["snap_days"] = _int(getattr(cfg, "retain_snap_days", 0), 730)

    out: dict = {}

    # 审计日志：先按天删，再按行数封顶（两步各管一件事，见模块说明）
    n = _del(db, "DELETE FROM audit_log "
                 "WHERE ts < datetime('now', ?)", (f"-{g['audit_days']} days",))
    keep = g["audit_rows"]
    n += _del(db, "DELETE FROM audit_log WHERE id <= "
                  "(SELECT COALESCE(MAX(id), 0) FROM audit_log) - ?", (keep,))
    out["audit_log"] = n

    # 页面账本：断点续采只关心「这个任务还没抓的页」，30 天前的 done 页
    # 没有任何用处；但 pending/error 的页**不能按时间删**（那等于把
    # 断点丢了），所以只删 done。
    out["task_pages"] = _del(
        db, "DELETE FROM task_pages WHERE status='done' "
            "AND last_at < datetime('now', ?)", (f"-{g['pages_days']} days",))

    out["failed_urls"] = _del(
        db, "DELETE FROM failed_urls WHERE last_tried < datetime('now', ?)",
        (f"-{g['failed_days']} days",))

    out["disk_snapshots"] = _del(
        db, "DELETE FROM disk_snapshots WHERE day < date('now', ?)",
        (f"-{g['snap_days']} days",))

    if force:
        _del(db, "PRAGMA incremental_vacuum")
    return out
