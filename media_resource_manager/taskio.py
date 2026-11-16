"""Import/export: tasks (JSON), reports, image metadata, rule versioning.

All files are written locally. Task exports include the config but NOT
machine-specific ids; proxy_id is dropped on export (proxies are per-machine).
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path

from .db import Database
from .models import TaskConfig, TaskStatus
from .repo import get_task, list_tasks, save_task


# --------------------------------------------- R154-F16：克隆 / 模板 ----
def _copy_config(t: TaskConfig) -> dict:
    """任务配置的纯数据副本（dict），随时可改后塞回 TaskConfig。

    只保留 TaskConfig 真有的字段 —— asdict 出来的是快照，将来模型加字段
    也不会漏；但**绝不带 id**（带 id 会把原任务 UPDATE 掉，那是事故）。
    """
    d = asdict(t)
    fields = TaskConfig.__dataclass_fields__
    return {k: v for k, v in d.items() if k in fields and k != "id"}


def clone_task(db: Database, task_id: int, *, name: str | None = None,
               as_template: bool = False) -> int | None:
    """复制任务配置建一个新任务（R154-F16「克隆」）。返回新任务 id。

    - **不复制历史记录**：images/reviews/task_pages/failed_urls 全按
      task_id 归属，新任务天然是白纸一张（订单红线：克隆不要历史）。
    - 名字默认加「（副本）」；``as_template=True`` 时按模板命名走
      （见 :func:`save_as_template`）。
    - 运行态一律清零：status=idle、last_error/last_run_at 清空 ——
      克隆一个「昨天报错的 stopped 任务」不该把错误文案也带过来。
    - ``parent_id`` 记来源 id（血缘，仅存档用，调度/引擎不读它）。
    - 模板（``is_template=1``）也能克隆：那就是「用模板新建任务」。
    """
    t = get_task(db, int(task_id))
    if t is None:
        return None
    d = _copy_config(t)
    d["status"] = TaskStatus.IDLE.value
    d["last_error"] = ""
    d["last_run_at"] = ""
    d["parent_id"] = t.id
    if as_template:
        d["is_template"] = True
        if name:
            d["name"] = name
    else:
        d["is_template"] = False
        d["name"] = name or ((t.name or "(未命名)") + "（副本）")
    new = TaskConfig(id=None, **d)
    return save_task(db, new)


def save_template_config(db: Database, cfg: TaskConfig) -> int:
    """把一个 TaskConfig 直接存成模板（R154-F16，桌面「表单直存」用）。

    会**原地改写**传入对象的 id/is_template/status/parent_id/last_* 再落库
    （调用方传进来的都是刚 _collect 出来的新对象，改写无副作用）。
    """
    cfg.id = None
    cfg.is_template = True
    cfg.status = TaskStatus.IDLE
    cfg.parent_id = None
    cfg.last_error = ""
    cfg.last_run_at = ""
    return save_task(db, cfg)


def save_as_template(db: Database, task_id: int,
                     name: str | None = None) -> int | None:
    """把任务存为模板（R154-F16）。返回模板 id；源任务不动。

    模板 = ``is_template=1`` 的任务行：复用 2019 年起就有的 DB 列
    （``repo.list_tasks(templates=True)`` 一直是现成入口），不另造
    JSON 文件格式 —— 模板跟着库走，备份/迁移自动带上，也不会在
    任务列表 / Web / 导出里出现（``list_tasks`` 默认 is_template=0）。
    """
    t = get_task(db, int(task_id))
    if t is None:
        return None
    d = _copy_config(t)
    d["name"] = (name or "").strip() or (t.name or "(未命名)")
    return save_template_config(db, TaskConfig(id=None, **d))


def list_templates(db: Database) -> list[TaskConfig]:
    """全部模板（is_template=1），按 id 升序。"""
    return list_tasks(db, templates=True)


def template_payload(t: TaskConfig) -> dict:
    """模板给前端的载荷：新建任务表单要回填的字段全集（R154-F16）。

    **不带 cookie**：Web 表单没有 Cookie 字段（桌面端才有「取站点
    Cookie」那套），给了也无处回填 —— 少暴露一个敏感字段是一个。
    模板行本身仍带 cookie（桌面端套用时要），这里只是列表接口的口径。
    """
    return {
        "id": t.id, "name": t.name,
        "adapter_name": t.adapter_name,
        "entry_urls": list(t.entry_urls or []),
        "keywords": list(t.keywords or []),
        "concurrency": t.concurrency, "max_depth": t.max_depth,
        "delay": t.delay, "priority": t.priority,
        "ai_labels": list(t.ai_labels or []),
        "ai_labels_l2": list(t.ai_labels_l2 or []),
        "ai_labels_l3": list(t.ai_labels_l3 or []),
        "ai_blacklist": list(t.ai_blacklist or []),
        "ai_threshold": t.ai_threshold,
        "ai_enabled": bool(t.ai_enabled),
        "use_proxy": bool(t.use_proxy),
        "auto_run": bool(t.auto_run),
        "output_dir": t.output_dir or "",
    }


def delete_template(db: Database, template_id: int) -> bool:
    """删除模板。**只删 is_template=1 的行** —— 真任务传进来是 no-op
    （返回 False），防「用模板接口误删真任务」。"""
    try:
        # R221/C10：先清两张引用 tasks 的子表再删父行（外键已开启）。
        # 模板正常不会有采集数据，但任何残留子行都会让删除被 FK 拦下。
        for _clr in (db.clear_failed_urls, db.clear_task_pages):
            try:
                _clr(int(template_id))
            except Exception:
                pass
        cur = db.execute("DELETE FROM tasks WHERE id=? AND is_template=1",
                         (int(template_id),))
    except Exception:
        return False
    return bool(getattr(cur, "rowcount", 0))
from .stats import library_stats


# ----------------------------------------------------------------- tasks ---
def task_progress(db: Database, task_id: int) -> dict:
    """C11：单个任务的进度统计（已下载 / 已丢弃 / 待审核 / 记录总数）。

    - **已下载**：文件在磁盘、没被隔离、没被拉黑（待审核的也算已到手，单列出来）
    - **已丢弃**：AI 筛掉 + 隔离区 + 黑名单 + 路径为空的记录
    """
    try:
        rows = db.query(
            "SELECT path, in_quarantine, blacklisted, needs_review "
            "FROM images WHERE task_id=?", (int(task_id),))
    except Exception:
        return {"downloaded": 0, "discarded": 0,
                "pending_review": 0, "total_records": 0}
    downloaded = discarded = pending = 0
    for r in rows:
        if r["blacklisted"] or r["in_quarantine"] or not r["path"]:
            discarded += 1
        else:
            downloaded += 1
            if r["needs_review"]:
                pending += 1
    return {"downloaded": downloaded, "discarded": discarded,
            "pending_review": pending, "total_records": len(rows)}


def build_export_data(db: Database, ids: list | None = None,
                      with_progress: bool = True) -> list:
    """构造任务导出数据（JSON 数组），不落盘。

    C11：**默认带上进度统计**（``with_progress=True``）。换机器/重装后导入，
    光有配置不够用 —— 用户最想知道的是"这个任务跑到哪了"。每条多一个
    ``progress`` 段：``{downloaded, discarded, pending_review, total_records}``。

    ``progress`` 只是**给人看的**：导入时会被丢掉（软件里没有"从第 N 张续抓"
    的机制，假装能还原比不给更糟）。机器相关的 id（id/parent_id/proxy_id）
    依旧不带过去。

    ``ids`` 给定时只导这些任务（GUI 上让用户勾选），None = 全部非模板任务。
    Web 端 / 桌面端共用这一份口径（R154-F14）。
    """
    tasks = list_tasks(db)
    if ids is not None:
        want = {int(i) for i in ids}
        tasks = [t for t in tasks if t.id is not None and int(t.id) in want]
    data = []
    for t in tasks:
        d = asdict(t)
        d.pop("id", None)
        d.pop("parent_id", None)
        d["status"] = TaskStatus.IDLE.value          # never carry run state
        d.pop("proxy_id", None)                      # proxies are per-machine
        d["proxy"] = t.proxy if t.use_proxy else None
        if with_progress and t.id is not None:
            d["progress"] = task_progress(db, t.id)
        data.append(d)
    return data


def export_tasks(db: Database, out: Path, *, ids: list | None = None,
                 with_progress: bool = True) -> int:
    """导出任务到 JSON，返回导出条数（数据由 :func:`build_export_data` 构造）。"""
    data = build_export_data(db, ids=ids, with_progress=with_progress)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return len(data)


def summarize_progress(tasks_data: list) -> str:
    """把导出内容里的 ``progress`` 汇成几行给人看（导出后弹窗用）。"""
    lines = []
    for d in tasks_data:
        p = d.get("progress") or {}
        name = d.get("name") or "(未命名)"
        if p:
            lines.append(
                f"{name}｜已下载 {p.get('downloaded', 0)}"
                f"｜已丢弃 {p.get('discarded', 0)}"
                f"｜待审核 {p.get('pending_review', 0)}")
        else:
            lines.append(f"{name}｜（无进度数据）")
    return "\n".join(lines)


def _skip_duplicate_urls(db: Database) -> set:
    """已存在的入口网址集合（导入时用来跳过重复）。"""
    have: set = set()
    try:
        for r in db.query("SELECT entry_urls FROM tasks"):
            try:
                for uu in (json.loads(r["entry_urls"] or "[]") or []):
                    have.add(uu)
            except Exception:
                continue
    except Exception:
        pass
    return have


def import_tasks_data(db: Database, raw: list,
                      *, skip_duplicates: bool = True,
                      valid_adapters: set | None = None) -> dict:
    """从任务数据列表导入任务，返回明细。

    返回 ``{"created", "skipped_dup", "missing_adapter"}``：

    - ``created``：新建任务数；
    - ``skipped_dup``：因入口网址重复而跳过的任务名列表；
    - ``missing_adapter``：适配器在本地不存在、未导入的任务名列表（R154-F14：
      缺适配器的任务建出来也是坏的，宁可跳过并提示，也不留一个跑不起来的任务）。

    C11：``progress`` 段**不还原**（见 :func:`export_tasks` 的说明）。
    ``skip_duplicates=True``（默认）时，入口网址已存在的任务会跳过，
    避免同一个导出文件导两遍就多出一堆重复任务。

    ``valid_adapters`` 给定时（Web 导入场景），``adapter_name`` 不在该集合内的
    任务会被归入 ``missing_adapter`` 并跳过；传 ``None``（桌面导入路径）则不做
    适配器校验，向后兼容旧行为。
    """
    if not isinstance(raw, list):
        raise ValueError("期望是任务 JSON 数组")
    have = _skip_duplicate_urls(db) if skip_duplicates else set()
    created = 0
    skipped_dup: list = []
    missing_adapter: list = []
    for d in raw:
        if not isinstance(d, dict):
            continue
        adapter_name = d.get("adapter_name") or ""
        if valid_adapters is not None and adapter_name \
                and adapter_name not in valid_adapters:
            missing_adapter.append(d.get("name") or adapter_name)
            continue
        urls = [str(u) for u in (d.get("entry_urls") or []) if u]
        if skip_duplicates and urls and all(u in have for u in urls):
            skipped_dup.append(d.get("name") or "(未命名)")
            continue                       # 网址全都见过 → 当重复跳过
        kwargs = {k: v for k, v in d.items()
                  if k in TaskConfig.__dataclass_fields__ and k != "progress"}
        t = TaskConfig(id=None, name=kwargs.get("name") or "imported",
                       adapter_name=adapter_name,
                       **{k: v for k, v in kwargs.items()
                          if k not in ("name", "adapter_name", "status")})
        t.status = TaskStatus.IDLE
        t.entry_urls = urls or list(getattr(t, "entry_urls", []) or [])
        save_task(db, t)
        have.update(urls)
        created += 1
    return {"created": created, "skipped_dup": skipped_dup,
            "missing_adapter": missing_adapter}


def import_tasks_file(db: Database, path: Path,
                      *, skip_duplicates: bool = True) -> int:
    """从导出文件导入任务，返回新建条数（桌面端入口，不做适配器校验）。

    校验逻辑见 :func:`import_tasks_data`。
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    res = import_tasks_data(db, raw, skip_duplicates=skip_duplicates,
                            valid_adapters=None)
    return res["created"]


# --------------------------------------------------------------- reports ---
def export_report(db: Database, library_dir: Path, out: Path) -> Path:
    s = library_stats(db, Path(library_dir))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out


def export_metadata(db: Database, out: Path, fmt: str = "json") -> Path:
    rows = db.query(
        """SELECT id, task_id, source_url, sha256, phash, width, height,
                  bytes, path, tags, score, blacklisted, in_quarantine,
                  collected_at, rating, favorite
           FROM images ORDER BY id""")
    cols = rows[0].keys() if rows else []
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
    else:
        out.write_text(json.dumps([dict(r) for r in rows],
                                  ensure_ascii=False, indent=2),
                       encoding="utf-8")
    return out


# ----------------------------------------------------------- rule versions --
def next_rule_version(db: Database, name: str) -> int:
    row = db.query("SELECT MAX(version) v FROM rules WHERE name=?", (name,))
    cur = int(row[0]["v"] or 0) if row else 0
    return cur + 1


def save_rule_version(db: Database, name: str, content: str, note: str = "") -> int:
    cur = db.execute(
        "INSERT INTO rules(name, version, content, note) VALUES (?,?,?,?)",
        (name, next_rule_version(db, name), content, note))
    return int(cur.lastrowid)


def rollback_rule(db: Database, rules_dir: Path, name: str,
                  version: int) -> Path:
    """Restore an older rule version as the live file (.bak of current kept)."""
    rows = db.query("SELECT content FROM rules WHERE name=? AND version=?",
                    (name, version))
    if not rows:
        raise ValueError(f"没有版本 {version}，规则 '{name}'")
    content = rows[0]["content"]
    target = None
    for ext in (".yaml", ".yml", ".json"):
        p = Path(rules_dir) / (name + ext)
        if p.exists():
            target = p
            break
    if target is None:
        target = Path(rules_dir) / (name + ".yaml")
    backup = target.with_suffix(target.suffix + ".bak")
    if target.exists():
        backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    target.write_text(content, encoding="utf-8")
    db.audit("rules", "rollback", name, f"restored v{version} -> {target.name}")
    return target
