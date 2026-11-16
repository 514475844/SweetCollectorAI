"""图库批量重命名 —— 桌面与网页共用的唯一实现（V0.0.12）。

两条红线（用户定死，写在这里而不是各端界面层）：
  * **用户原有图库（如 F 盘）的文件一律不改名**——红线是「不删 / 不移 /
    不改名」，命中 `safe_delete.is_software_file` 之外的路径直接跳过并计数
    回报（跳过数不是错误，必须回给界面显示，否则用户以为全改成功了）。
  * 磁盘改名与数据库 `images.path` 必须**同一步**改：先 `os.replace`
    成功才 UPDATE，反序会在改名失败时留下指不到文件的死记录。

桌面菜单和网页接口都只组参数 + 展示结果，逻辑不许各写一份
（两端口径分叉是 R143/R158 系列反复踩过的坑）。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path


def bump_name(dst: Path) -> Path:
    """目标名已被占用则顺延 name(1).ext（与 Downloader._unique_dest 同规则）。"""
    if not dst.exists():
        return dst
    stem, dot, ext = dst.name.rpartition(".")
    if not dot:
        stem, ext = dst.name, ""
    for i in range(1, 1000):
        cand = (dst.with_name(f"{stem}({i}).{ext}") if ext
                else dst.with_name(f"{stem}({i})"))
        if not cand.exists():
            return cand
    return dst


def _sanitise(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name)


def rename_by_prefix(db, cfg, ids, prefix: str) -> dict:
    """给选中文件名统一加前缀。返回 {"renamed", "skipped_external", "missing"}。"""
    from .safe_delete import is_software_file
    prefix = (prefix or "").strip()
    if not prefix:
        return {"renamed": 0, "skipped_external": 0, "missing": 0,
                "error": "前缀为空"}
    rows = _rows_for(db, ids)
    n = ext = miss = 0
    for r in rows:
        src = Path(r["path"] or "")
        if not src.exists():
            miss += 1
            continue
        if not is_software_file(src, cfg):
            ext += 1
            continue
        tgt = bump_name(src.with_name(_sanitise(prefix + src.name)))
        try:
            os.replace(src, tgt)
            db.execute("UPDATE images SET path=? WHERE id=?",
                       (str(tgt), int(r["id"])))
            n += 1
        except Exception:
            miss += 1
    return {"renamed": n, "skipped_external": ext, "missing": miss}


def rename_by_template(db, cfg, ids, template: str) -> dict:
    """按模板批量重命名。占位符：
    {n}=三位序号(按选择顺序) {orig}=原文件名(去扩展名)
    {date}=今天 YYYYMMDD {site}=来源页域名 {task}=所属任务名。

    序号只给「会真改名」的图编号——原有图库跳过的不占号（桌面口径，
    曾出现跳过图占号导致序号断档）。
    """
    from .safe_delete import is_software_file
    tpl = (template or "").strip()
    if not tpl:
        return {"renamed": 0, "skipped_external": 0, "missing": 0,
                "error": "模板为空"}
    if "{" not in tpl and "}" not in tpl:
        return {"renamed": 0, "skipped_external": 0, "missing": 0,
                "error": "模板里没有占位符，全部文件会重名。"
                         "请至少用一个占位符，如 {date}_{site}_{n}"}
    rows = _rows_for(db, ids, "id, path, source_url, task_id")
    task_names = {t["id"]: t["name"] for t in
                  db.query("SELECT id, name FROM tasks")}
    try:
        from .utils.url import domain_of
    except Exception:
        domain_of = lambda s: ""     # noqa: E731  非致命：site 兜底 "site"
    n = ext = miss = idx = 0
    for r in rows:
        src = Path(r["path"] or "")
        if not src.exists():
            miss += 1
            continue
        if not is_software_file(src, cfg):
            ext += 1
            continue
        idx += 1
        site = domain_of(r["source_url"] or "") or "site"
        task_name = task_names.get(r["task_id"], "task")
        try:
            name = tpl.format(
                n=f"{idx:03d}", orig=src.stem,
                date=datetime.now().strftime("%Y%m%d"),
                site=re.sub(r"[^0-9A-Za-z.\-]", "_", site),
                task=re.sub(r'[<>:"/\\|?*\s]', "_", str(task_name)) or "task")
        except (KeyError, IndexError, ValueError) as e:
            return {"renamed": n, "skipped_external": ext, "missing": miss,
                    "error": f"模板占位符无效：{e}"
                             "（支持 {n} {orig} {date} {site} {task}）"}
        name = _sanitise(name)
        if not name.strip("._ "):
            continue
        tgt = bump_name(src.with_name(name + src.suffix))
        try:
            os.replace(src, tgt)
            db.execute("UPDATE images SET path=? WHERE id=?",
                       (str(tgt), int(r["id"])))
            n += 1
        except Exception:
            miss += 1
    return {"renamed": n, "skipped_external": ext, "missing": miss}


def _rows_for(db, ids, cols: str = "id, path"):
    """按 id 顺序取行（选择顺序 = 序号顺序，两端必须同 SQL 口径）。"""
    ids = [int(i) for i in (ids or [])][:500]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = db.query(f"SELECT {cols} FROM images WHERE id IN ({ph})", ids)
    order = {v: k for k, v in enumerate(ids)}
    return sorted(rows, key=lambda r: order.get(int(r["id"]), 10 ** 9))
