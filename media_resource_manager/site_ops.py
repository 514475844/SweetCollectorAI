"""V0.0.16 · 站点维护动作（纯逻辑层，桌面与网页共用同一份口径）。

把网页端此前缺的 7 个动作收口到一个不依赖 Qt / 不依赖 HTTP 的模块：
  · 数据存放位置（只读）
  · 回填本地图片到图库（复用 engine.backfill_local_files）
  · 备份立即同步到目录（对齐桌面 _backup_sync_internal）
  · 清空 / 导出审计日志
  · 清空隔离区（物理删除）
  · 全面清理（勾选清单）

红线纪律：
1. **破坏性动作不在本模块里自己决定执行**。``clear_audit_logs`` /
   ``prune_quarantine`` / ``full_cleanup`` 只负责「怎么做」，调用方（webui）
   必须先确认请求里带 ``confirm=True`` 且用户已看过清单，再调本层。
2. 备份同步沿用桌面口径：数据库走在线快照（不直接 copy 主库，WAL 会漏），
   config.json 与偏好模型一律加密成 .mrmx，绝不把密钥明文同步到网盘。
3. 全面清理直接调 ``cleanup.perform_full_cleanup``，不另写一套删表顺序
   （外键顺序错了会「返回成功但什么都没删」）。
"""
from __future__ import annotations

import csv
import io
import json
import shutil
import tempfile
from pathlib import Path

#: 日志导出行数上限。审计表可能上万行，一次性全量 CSV 会把网页请求拖死。
LOG_EXPORT_LIMIT = 20000

#: 允许出现在全面清理清单里的项（防前端传任意 key 拼进 SQL / 路径）。
CLEAN_KEYS = frozenset({
    "library", "review", "isolate", "tasks", "audit", "thumbs",
    "backups", "logs", "prefs", "rules", "models",
})


def _p(v) -> str:
    return str(Path(v).resolve()) if v else ""


def data_location(cfg) -> dict:
    """只返回路径，一个文件都不动。"""
    from .config import user_data_dir

    root = Path(user_data_dir())
    return {
        "ok": True,
        "root": str(root),
        "database": _p(getattr(cfg, "db_path", "")),
        "rules": _p(getattr(cfg, "rules_dir", "")),
        "logs": _p(getattr(cfg, "log_dir", "")),
        "library": _p(getattr(cfg, "library_dir", "")),
        "quarantine": _p(getattr(cfg, "isolate_dir", "")),
        "thumbs": str((root / "thumbs").resolve()),
        "note": "以上为运行软件的这台机器上的路径，并非当前浏览器所在电脑。",
    }


def backfill(db, cfg, task_id: int = 0, re_filter_ai: bool = False) -> dict:
    """把磁盘上已有、但没进库的图片补写进图库。"""
    from .engine import backfill_local_files

    tid = int(task_id or 0) or None
    n = backfill_local_files(db, tid, re_filter_ai=bool(re_filter_ai))
    return {"ok": True, "added": int(n or 0),
            "note": f"已把磁盘上 {int(n or 0)} 张图片补写进图库；已在库中的自动跳过。"}


def sync_backup(db, cfg, dest: str = "") -> dict:
    """复制到外部同步目录。**config / 偏好一律加密**，与桌面端逐项对齐。"""
    from .crypt import encrypt_bytes

    target = str(dest or getattr(cfg, "backup_sync_dir", "") or "").strip()
    if not target:
        return {"ok": False,
                "error": "尚未设置备份同步目录，请先在设置中填写后再执行同步。"}
    d = Path(target).expanduser().resolve()
    if d.parent == d:
        return {"ok": False, "error": "不允许把文件系统根目录作为同步目录。"}
    passphrase = str(getattr(cfg, "export_passphrase", "") or "")
    try:
        d.mkdir(parents=True, exist_ok=True)
        dbfile = Path(getattr(cfg, "db_path", ""))
        if dbfile.exists():
            snap_dir = Path(tempfile.mkdtemp(prefix="mrm_sync_"))
            try:
                snap = snap_dir / "mrm.db"
                db.backup(snap)
                shutil.copy2(snap, d / "mrm.db")
            finally:
                shutil.rmtree(snap_dir, ignore_errors=True)
        rules = Path(getattr(cfg, "rules_dir", ""))
        if rules.exists():
            shutil.copytree(rules, d / "rules", dirs_exist_ok=True)
        from .config import user_data_dir
        thumbs = Path(user_data_dir()) / "thumbs"
        if thumbs.exists():
            shutil.copytree(thumbs, d / "thumbs", dirs_exist_ok=True)
        cfgf = dbfile.parent / "config.json"
        if cfgf.exists():
            (d / "config.json.mrmx").write_bytes(
                encrypt_bytes(cfgf.read_bytes(), passphrase))
        prefs = db.query("SELECT * FROM preferences")
        (d / "preferences.json.mrmx").write_bytes(encrypt_bytes(
            json.dumps([dict(r) for r in prefs], ensure_ascii=False).encode("utf-8"),
            passphrase))
    except Exception as e:
        return {"ok": False, "error": f"备份同步失败：{e}"}
    return {"ok": True, "dest": str(d),
            "note": f"已同步到「{d}」。配置与学习记录均已加密。"}


def clear_audit_logs(db) -> dict:
    """清空操作日志表。只影响日志列表，不碰任务、图片与学习记录。"""
    row = db.query("SELECT COUNT(*) c FROM audit_log")
    n = int(row[0]["c"] if row else 0)
    db.execute("DELETE FROM audit_log")
    return {"ok": True, "deleted": n,
            "note": f"已清空 {n} 条操作日志；任务、图片与学习记录均未改动。"}


def audit_log_csv(db) -> bytes:
    """审计日志 CSV（UTF-8 BOM，Excel 直接可开）。调用方负责加密后再送出。"""
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["编号", "时间", "来源", "动作", "对象", "详情"])
    for r in db.query(
            "SELECT id, ts, actor, action, target, detail FROM audit_log "
            "ORDER BY id DESC LIMIT ?", (LOG_EXPORT_LIMIT,)):
        w.writerow([r["id"], r["ts"] or "", r["actor"] or "",
                    r["action"] or "", r["target"] or "", r["detail"] or ""])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def prune_quarantine(db, keep_days: int = 0) -> dict:
    """物理删除隔离区里未收藏 / 未拉黑 / 未待审的图（桌面同一条 SQL）。

    V0.0.56：``keep_days`` > 0 时只清「隔离超过这么多天」的图（按保留期自动
    清理）；=0 或极大值表示全清（原行为不变）。天数上限钳到 10 年防误填。
    """
    try:
        days = int(keep_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        days = 10 ** 6                    # 全清（原行为）
    days = min(days, 3650)                # 上限 10 年，防手滑填天文数字
    n = int(db.quarantine_prune(days) or 0)
    _scope = f"隔离超过 {days} 天、" if keep_days and int(keep_days or 0) > 0 else ""
    return {"ok": True, "deleted": n, "keep_days": days,
            "note": (f"已删除 {_scope}未收藏未拉黑未待审的 {n} 张隔离图，操作不可撤销。"
                     "已收藏、已拉黑与待审核的图片均未改动。") if n else
                    "没有符合条件的隔离图，未删除任何文件。"}


def cleanup_items() -> list[dict]:
    """全面清理的可勾选项，直接借用 cleanup.CLEAN_ITEMS，不另写一份清单。"""
    from .cleanup import CLEAN_ITEMS, DEFAULT_PLAN

    return [{"key": k, "label": label, "risky": risk == "risky",
             "desc": desc, "checked": k in DEFAULT_PLAN}
            for k, label, risk, desc in CLEAN_ITEMS]


def full_cleanup(db, cfg, plan) -> dict:
    """按勾选清单重置到新装状态。**真删硬盘**，调用方必须已二次确认。"""
    from .cleanup import perform_full_cleanup

    keys = {str(k) for k in (plan or []) if str(k) in CLEAN_KEYS}
    if not keys:
        return {"ok": False, "error": "请至少勾选一项要清理的内容。"}
    try:
        perform_full_cleanup(db, cfg, None, drop_prefs=("prefs" in keys),
                             plan=keys)
    except Exception as e:
        return {"ok": False, "error": f"全面清理失败：{e}"}
    return {"ok": True, "plan": sorted(keys),
            "note": f"已按勾选的 {len(keys)} 项完成清理；未勾选项原样保留。"}


# ---------------------------------------------------------------- V0.0.17 ----
# 网页端补齐的两个纯逻辑动作：在文件夹中显示 / 日志统计报告。
# 与上面 7 个动作同一条纪律：本层只决定「怎么做」，不碰 HTTP、不执行打开，
# 也不落盘；调用方（webui）负责远程访问拦截与二进制下发。

def _is_within(path: Path, root: Path) -> bool:
    """path 是否落在 root 之内（两者都已 resolve，防路径穿越）。"""
    try:
        return path.is_relative_to(root)
    except (AttributeError, ValueError, OSError):
        return False


def _registered_roots(cfg) -> list[Path]:
    """已登记的图片根目录：图库 / 待审核 / 隔离区 + 软件数据根目录。

    数据根目录兜住 thumbs 等由本软件自己建的目录；三个 cfg 目录允许用户把
    图库放在别处（例如另一块盘）。目录清单与 diskspace.DIR_SPECS 同源，
    不在此另写一份。
    """
    from .config import user_data_dir

    roots = [Path(user_data_dir()).resolve()]
    for key in ("library_dir", "review_dir", "isolate_dir"):
        v = getattr(cfg, key, "") or ""
        if not v:
            continue
        try:
            roots.append(Path(v).expanduser().resolve())
        except (OSError, ValueError):
            continue
    seen, out = set(), []
    for r in roots:
        s = str(r)
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def reveal_image_path(db, cfg, image_id) -> dict:
    """解析「在文件夹中显示」的目标路径（V0.0.17 网页端与桌面端共用口径）。

    安全红线：
    1. 只按图片编号查**数据库已登记的 path**，前端不得传任意路径；
    2. 该路径必须落在已登记目录内，越权路径一律拒绝，避免借「打开文件夹」
       读到软件数据目录之外的任意磁盘位置；
    3. 文件必须仍在磁盘上；不存在时明确返回中文错误，不静默。
    真正的「打开」由调用方执行：远程访问还要在 webui 一侧另拦一层。
    """
    try:
        iid = int(image_id or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "图片编号不对。"}
    if iid <= 0:
        return {"ok": False, "error": "图片编号不对。"}
    try:
        rows = db.query("SELECT path FROM images WHERE id=?", (iid,))
    except Exception:
        return {"ok": False, "error": "读取图片登记记录失败，请稍后重试。"}
    if not rows or not rows[0]["path"]:
        return {"ok": False, "error": "未找到该图片的登记记录。"}
    try:
        p = Path(str(rows[0]["path"])).expanduser().resolve()
    except (OSError, ValueError):
        return {"ok": False, "error": "图片路径不可用。"}
    if not any(_is_within(p, r) for r in _registered_roots(cfg)):
        return {"ok": False, "error": "该图片路径不在已登记目录内，已拒绝打开。"}
    if not p.is_file():
        return {"ok": False, "error": "图片文件已不在磁盘，未找到可打开的文件夹。"}
    return {"ok": True, "path": str(p), "dir": str(p.parent)}


def audit_log_stats_csv(db) -> bytes:
    """日志统计报告 CSV（UTF-8 BOM）。调用方负责加密成 .mrmx 后送出。

    与 ``audit_log_csv``（逐条明细）分开：这份只含「按动作 / 按来源」的
    统计汇总，便于留档。红线在 webui 的导出端点——此处绝不出明文。
    """
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["日志统计报告"])
    w.writerow(["项目", "数值"])
    try:
        row = db.query("SELECT COUNT(*) c FROM audit_log")
        total = int(row[0]["c"] if row else 0)
        w.writerow(["日志总条数", total])
        w.writerow([])
        w.writerow(["动作", "次数"])
        for r in db.query(
                "SELECT action, COUNT(*) c FROM audit_log "
                "GROUP BY action ORDER BY c DESC, action"):
            w.writerow([r["action"] or "（空）", int(r["c"] or 0)])
        w.writerow([])
        w.writerow(["来源", "次数"])
        for r in db.query(
                "SELECT actor, COUNT(*) c FROM audit_log "
                "GROUP BY actor ORDER BY c DESC, actor"):
            w.writerow([r["actor"] or "（空）", int(r["c"] or 0)])
    except Exception:
        w.writerow(["日志总条数", 0])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")
