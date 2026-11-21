"""V0.0.14 · 交付层（**纯逻辑**：只读数据库 + 只写用户指定的导出目录）。

用户初衷（本轮明确）：采集回来的图最终要交给 AI 学习、再生成更多图。
所以软件的最后一环不是「存着」，而是**交出一份干净、可直接用于训练的
素材集**：图按合格线挑出来、复制到独立目录、每张配一份同名标注，另附清单。

这个文件同时收口三件以前只在桌面端有的能力，网页端直接复用同一份实现：
  · ``gallery_index_bytes`` —— 图库索引（图片 → 标签映射的 JSON）；
  · ``report_bytes``       —— 采集周报 / 月报（与桌面端同一个 reporting 模块）；
  · ``encrypted``          —— 上述两类**含图库信息**的导出一律加密成 .mrmx。

红线纪律（改本文件前必读）
1. **原图绝不删、绝不移、绝不改名**。``export_training_kit`` 只用
   ``shutil.copy2`` 复制；目标目录里遇到同名文件一律加序号，**绝不覆盖**。
   F 盘原图库全程只读。
2. **零字节图不导出**（用户明确「无论什么情况都不要」）。
3. 素材集目录里**不写明文图库索引**（图片 → 标签映射属于要加密的信息）。
   每张图同名的 ``.txt`` 是训练格式本身必需的标注，属于图片信息，按用户
   定的「图片和图片信息不加密」处理；``manifest.json`` 只记文件名与目标词，
   不写原图绝对路径。
4. 目标目录挡掉系统目录与文件系统根目录；导出上限 5000 张，避免一次操作
   把磁盘塞满。
5. 标注里只放**目标词 + 标签 + 用户补充词**；「排除词」绝不写进标注，
   否则等于把不想要的东西教给模型。
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import os
import re
import shutil
from pathlib import Path

#: 单次导出上限。够一次训练用，也不会让网页请求挂死。
MAX_EXPORT = 5000
DEFAULT_LIMIT = 500

#: 不允许作为导出目标的系统目录片段（大小写不敏感）。
_BLOCKED_PARTS = {
    "windows", "system32", "program files", "program files (x86)",
    "appdata", "system", "library", ".config", "etc", "usr", "proc",
}

#: 目录名里的非法字符（Windows / POSIX 取交集，保守处理）。
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ------------------------------------------------------------------ 工具 ----
def _as_terms(raw) -> list[str]:
    """把 ``keywords`` / ``tags`` 字段解析成词表。

    库里历史数据既可能是 JSON 数组（``'["a","b"]'``），也可能是早期手写
    的逗号 / 中点分隔串。两种都要认，且**去空、去重、保序**。
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        s = str(raw).strip()
        if not s:
            return []
        items = []
        parsed_ok = False
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, list):
                    items = [str(x) for x in parsed]
                    # ⚠ 空数组也是「成功解析」：不能再 fall through 到按字符串
                    # 切分，否则 '[]' 会被当成一个名叫 "[]" 的标签写进标注。
                    parsed_ok = True
            except (ValueError, TypeError):
                items = []
        if not items and not parsed_ok:
            items = re.split(r"[,，、·\s]+", s)
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        w = str(x).strip()
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _safe_dir_name(name: str, fallback: str = "未命名") -> str:
    s = _BAD_NAME.sub("_", str(name or "").strip()).strip(". ")
    return (s or fallback)[:60]


def _check_dest(dest: Path, source_dirs: set[Path]) -> str:
    """返回空串表示可用，否则返回中文拒绝原因。"""
    try:
        p = Path(dest).expanduser().resolve()
    except (OSError, ValueError):
        return "导出目录无法解析，请重新选择"
    if p.parent == p:
        return "不允许把文件系统根目录作为导出目录"
    low = {str(x).lower() for x in p.parts}
    if low & _BLOCKED_PARTS:
        return "不允许把系统目录作为导出目录，请选择图库之外的普通文件夹"
    for d in source_dirs:
        if p == d:
            return "导出目录不能与原图所在目录相同，请选择另一个文件夹"
    return ""


def _unique_path(folder: Path, name: str, used: set[str]) -> Path:
    """绝不覆盖已有文件：同名时追加 _2 / _3 …（同时避开本轮已用过的名字）。"""
    stem, suffix = os.path.splitext(name)
    candidate = name
    n = 2
    while (folder / candidate).exists() or candidate.lower() in used:
        candidate = f"{stem}_{n}{suffix}"
        n += 1
    used.add(candidate.lower())
    return folder / candidate


# ------------------------------------------------- 加密导出（桌面已有口径） ----
def gallery_index_bytes(db) -> bytes:
    """图库索引 JSON（图片 → 标签 / 分数 / 来源）。调用方负责加密后再落盘。"""
    rows = db.query(
        "SELECT id, task_id, path, tags, score, favorite, source_url, "
        "width, height, bytes, collected_at FROM images "
        "WHERE path IS NOT NULL ORDER BY id")
    items = []
    for r in rows:
        items.append({
            "id": int(r["id"]),
            "task_id": int(r["task_id"] or 0),
            "path": r["path"] or "",
            "tags": _as_terms(r["tags"]),
            "score": float(r["score"] or 0),
            "favorite": bool(r["favorite"]),
            "source_url": r["source_url"] or "",
            "width": r["width"], "height": r["height"], "bytes": r["bytes"],
            "collected_at": r["collected_at"] or "",
        })
    payload = {
        "kind": "gallery-index",
        "exported_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "items": items,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def report_bytes(db, kind: str = "week", offset: int = 0) -> bytes:
    """采集周报 / 月报 Markdown，与桌面端 ``reporting`` 同一份统计口径。"""
    from .reporting import PERIOD_WEEK, collect_period, headline, period_range, to_markdown

    p = period_range(kind or PERIOD_WEEK, None, offset)
    rep = collect_period(db, p["start"], p["end"])
    rep["kind"] = p["kind"]
    rep["label"] = p["label"]
    text = "# " + headline(rep) + "\n\n" + to_markdown(rep, p["kind"])
    return text.encode("utf-8")


def encrypted(data: bytes, passphrase: str = "") -> bytes:
    """.mrmx 加密包；与桌面端 ``_write_encrypted`` 用同一个 ``crypt`` 模块。"""
    from .crypt import encrypt_bytes

    return encrypt_bytes(bytes(data), str(passphrase or ""))


# ------------------------------------------------------------ 训练素材集 ----
# V0.0.18：标注模板。默认模板保持 V0.0.14 的口径逐字不变（老行为零变化），
# 想给学生成模型加触发词/换分隔才改。占位符 {terms} 会被目标词列表替换。
CAPTION_TEMPLATE_KEY = "export_caption_template"
DEFAULT_CAPTION_TEMPLATE = "{terms}"
_TEMPLATE_SEP = re.compile(r"[,，、\s]+")


def caption_terms(task_terms: list[str], img_tags: list[str],
                  extra: list[str], fallback: str) -> list[str]:
    """合并目标词：任务词 + 图片标签 + 附加词，去空去重保序，20 个封顶。"""
    terms: list[str] = []
    seen: set[str] = set()
    for w in list(task_terms) + list(img_tags) + list(extra):
        if w and w not in seen:
            seen.add(w)
            terms.append(w)
    if not terms:
        terms = [fallback]
    return terms[:20]


def render_caption(terms: list[str], template: str = "") -> str:
    """按模板渲染标注文本。

    模板支持两种写法：
      · ``{terms}``           —— 整串替换（默认，逗号分隔）；
      · ``前缀 {terms} 后缀`` —— 前后缀原样保留，实现「触发词 + 内容词」。
    模板里的 ``{terms}`` 缺失时按「模板整体作为前缀」处理，兼容用户
    只填了触发词忘了写占位符的手误。
    """
    tpl = str(template or "").strip() or DEFAULT_CAPTION_TEMPLATE
    body = ", ".join(terms)
    if "{terms}" in tpl:
        return tpl.replace("{terms}", body)
    return f"{tpl} {body}".strip()


def _filter_rows(db, opt: dict) -> tuple[list, dict]:
    """按筛选参数取候选图片行。预览与真导出**共用这一份口径**，
    保证「预览看到什么、导出就是什么」。返回 (rows, 归一化参数)。"""
    try:
        min_score = float(opt.get("min_score", 0.35) or 0)
    except (TypeError, ValueError):
        min_score = 0.35
    min_score = min(1.0, max(0.0, min_score))
    try:
        limit = int(opt.get("limit") or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = min(MAX_EXPORT, max(1, limit))
    try:
        task_id = int(opt.get("task_id") or 0)
    except (TypeError, ValueError):
        task_id = 0
    tag = str(opt.get("tag") or "").strip()[:60]
    only_fav = bool(opt.get("only_favorite"))

    where = ("in_quarantine=0 AND blacklisted=0 AND path IS NOT NULL "
             "AND score IS NOT NULL AND score>=?")
    args: list = [min_score]
    if only_fav:
        where += " AND favorite=1"
    if task_id > 0:
        where += " AND task_id=?"
        args.append(task_id)
    if tag:
        where += " AND tags LIKE ?"
        args.append("%" + tag + "%")
    args.append(limit)
    # V0.0.56：补 collected_at —— 清单里要能追溯"什么时候采的"（任务书交付要求）。
    # site 从 source_url 现算，不额外存列（老库无迁移成本）。
    rows = db.query(
        "SELECT id, task_id, path, tags, score, source_url, collected_at FROM images "
        f"WHERE {where} ORDER BY score DESC, id DESC LIMIT ?", tuple(args))
    norm = {"min_score": min_score, "limit": limit, "task_id": task_id,
            "tag": tag, "only_favorite": only_fav,
            "extra_terms": _as_terms(opt.get("extra_terms") or "")[:10],
            "group_by": "flat" if str(opt.get("group_by") or "") == "flat" else "task",
            "caption_template": str(opt.get("caption_template") or "").strip()}
    return rows, norm


def preview_training_kit(db, cfg, options: dict | None = None) -> dict:
    """导出前预览：返回候选图片清单（缩略图 id、路径名、分数、目标词、
    磁盘状态），**不落任何盘**。前端勾选后把选中的 id 列表回传
    ``export_training_kit(ids=...)`` 即所见即所得。"""
    opt = dict(options or {})
    if opt.get("min_score") in (None, ""):
        opt["min_score"] = getattr(cfg, "ai_threshold", 0.35)
    rows, norm = _filter_rows(db, opt)
    task_terms: dict[int, list[str]] = {}
    task_names: dict[int, str] = {}
    try:
        for t in db.query("SELECT id, name, keywords FROM tasks"):
            task_terms[int(t["id"])] = _as_terms(t["keywords"])
            task_names[int(t["id"])] = str(t["name"] or "")
    except Exception:
        pass
    items = []
    for r in rows:
        p = Path(str(r["path"]))
        exists = p.is_file()
        size = 0
        if exists:
            try:
                size = p.stat().st_size
            except OSError:
                exists = False
        tid = int(r["task_id"] or 0)
        terms = caption_terms(task_terms.get(tid, []), _as_terms(r["tags"]),
                              norm["extra_terms"],
                              _safe_dir_name(task_names.get(tid) or "图片素材"))
        items.append({
            "id": int(r["id"]),
            "name": p.name,
            "score": round(float(r["score"] or 0), 4),
            "task_id": tid,
            "task": task_names.get(tid, ""),
            "source_url": r["source_url"] or "",
            "terms": terms,
            "caption": render_caption(terms, norm["caption_template"]),
            "on_disk": exists,
            "zero_bytes": exists and size == 0,
        })
    keep = [x for x in items if x["on_disk"] and not x["zero_bytes"]]
    return {
        "ok": True,
        "total": len(items),
        "exportable": len(keep),
        "skipped_missing": sum(1 for x in items if not x["on_disk"]),
        "skipped_empty": sum(1 for x in items if x["zero_bytes"]),
        "min_score": round(norm["min_score"], 3),
        "items": items,
    }


def record_export(db, dest: str, res: dict, opt: dict) -> None:
    """导出成功后落一行历史（V0.0.18）。失败静默——历史丢了不能反伤导出。"""
    try:
        db.execute(
            "INSERT INTO export_history(dest, copied, captions, skipped, "
            "min_score, filters, note) VALUES (?,?,?,?,?,?,?)",
            (str(dest), int(res.get("copied") or 0),
             int(res.get("captions") or 0),
             int(res.get("skipped_missing") or 0) + int(res.get("skipped_empty") or 0),
             float(res.get("min_score") or 0),
             json.dumps({k: v for k, v in (opt or {}).items()
                         if k in ("only_favorite", "task_id", "tag", "limit",
                                  "group_by", "extra_terms", "caption_template",
                                  "ids")},
                        ensure_ascii=False)[:2000],
             str(res.get("note") or "")[:300]))
    except Exception:
        pass


def export_history(db, limit: int = 50) -> dict:
    try:
        limit = min(200, max(1, int(limit or 50)))
    except (TypeError, ValueError):
        limit = 50
    rows = db.query(
        "SELECT id, ts, dest, copied, captions, skipped, min_score, filters, "
        "note FROM export_history ORDER BY id DESC LIMIT ?", (limit,))
    items = []
    for r in rows:
        try:
            flt = json.loads(r["filters"] or "{}")
        except (ValueError, TypeError):
            flt = {}
        items.append({"id": int(r["id"]), "ts": r["ts"], "dest": r["dest"],
                      "copied": int(r["copied"] or 0),
                      "captions": int(r["captions"] or 0),
                      "skipped": int(r["skipped"] or 0),
                      "min_score": float(r["min_score"] or 0),
                      "filters": flt, "note": r["note"] or ""})
    return {"ok": True, "items": items}


def export_training_kit(db, cfg, dest, options: dict | None = None) -> dict:
    """把合格图复制成一份可直接交给 AI 学习的素材集。

    ``options`` 可含：
      ``min_score``（默认取 ``cfg.ai_threshold``）、``only_favorite``、
      ``task_id``、``tag``、``limit``、``group_by``（``task`` / ``flat``）、
      ``extra_terms``（附加目标词，逗号或空格分隔）；
      V0.0.18 新增 ``ids``（预览勾选后回传的图片 id 白名单，给定时只导
      选中的，不再按分数捞）、``caption_template``（标注模板，见
      ``render_caption``）。

    全程只读原图、只写 ``dest``。返回中文结果摘要，失败时 ``ok=False``。
    """
    opt = dict(options or {})
    rows, norm = _filter_rows(db, opt)
    min_score = norm["min_score"]
    extra = norm["extra_terms"]
    group_by = norm["group_by"]
    tpl = norm["caption_template"]
    ids = opt.get("ids")
    if isinstance(ids, (list, tuple)):
        wanted = set()
        for x in ids[:MAX_EXPORT]:
            try:
                wanted.add(int(x))
            except (TypeError, ValueError):
                continue
        # 2026-11-21：「没给 ids」(None) 才按分数捞全量；只要明确给了
        # list/tuple，哪怕空列表/全非法，也表示「一个都没选中」，必须
        # rows=[]。原来 `if wanted:` 在空集合时跳过滤镜，反而导出全部。
        rows = [r for r in rows if int(r["id"]) in wanted]

    # 先算出所有源图所在目录，用于拒绝「导出到原目录」。
    source_dirs: set[Path] = set()
    for r in rows:
        try:
            source_dirs.add(Path(str(r["path"])).resolve().parent)
        except (OSError, ValueError):
            continue
    dest_path = Path(dest).expanduser()
    reason = _check_dest(dest_path, source_dirs)
    if reason:
        return {"ok": False, "error": reason}
    dest_path = dest_path.resolve()
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"无法创建导出目录：{e}"}
    if not os.access(str(dest_path), os.W_OK):
        return {"ok": False, "error": "导出目录不可写，请选择其他文件夹"}

    # 目标词来自任务词表（手填的 + 学习到的），排除词一律不进标注。
    task_terms: dict[int, list[str]] = {}
    task_names: dict[int, str] = {}
    try:
        for t in db.query("SELECT id, name, keywords FROM tasks"):
            task_terms[int(t["id"])] = _as_terms(t["keywords"])
            task_names[int(t["id"])] = str(t["name"] or "")
    except Exception:
        pass

    copied = 0
    captions = 0
    skipped_missing = 0
    skipped_empty = 0
    manifest: list[dict] = []
    group_dirs: dict[int, Path] = {}
    used: dict[Path, set[str]] = {}

    for r in rows:
        src = Path(str(r["path"]))
        if not src.is_file():
            skipped_missing += 1
            continue
        try:
            if src.stat().st_size == 0:
                skipped_empty += 1
                continue
        except OSError:
            skipped_missing += 1
            continue

        tid = int(r["task_id"] or 0)
        if group_by == "task":
            if tid not in group_dirs:
                folder = dest_path / _safe_dir_name(
                    task_names.get(tid) or f"任务_{tid}", f"任务_{tid}")
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                except OSError:
                    folder = dest_path
                group_dirs[tid] = folder
                used[folder] = set()
            out_dir = group_dirs[tid]
        else:
            out_dir = dest_path
            used.setdefault(out_dir, set())

        dst = _unique_path(out_dir, src.name, used[out_dir])
        try:
            shutil.copy2(str(src), str(dst))
        except OSError:
            skipped_missing += 1
            continue
        copied += 1

        terms = caption_terms(task_terms.get(tid, []), _as_terms(r["tags"]),
                              extra, _safe_dir_name(
                                  task_names.get(tid) or "图片素材"))
        cap = _unique_path(out_dir, dst.stem + ".txt", used[out_dir])
        try:
            cap.write_text(render_caption(terms, tpl), encoding="utf-8")
            captions += 1
        except OSError:
            cap = None
        # V0.0.56：清单补来源可追溯字段（任务书交付要求）——来源 URL、站点、
        # 采集时间。缺失列时按 sqlite3.Row 取值兜底，绝不让导出因一列缺失中断。
        def _cell(key, default=""):
            try:
                return r[key] if key in r.keys() else default
            except Exception:
                return default

        _src_url = str(_cell("source_url") or "")
        try:
            from urllib.parse import urlparse
            _site = urlparse(_src_url).netloc or ""
        except Exception:
            _site = ""
        manifest.append({
            "image": dst.name,
            "caption": cap.name if cap else "",
            "target_terms": terms,
            "score": round(float(r["score"] or 0), 4),
            "task_id": tid,
            "task": task_names.get(tid, ""),
            "source_url": _src_url,
            "site": _site,
            "collected_at": str(_cell("collected_at") or ""),
        })

    # 清单只记文件名与目标词，不写原图绝对路径（图库索引另有加密导出）。
    index_path = _unique_path(dest_path, "素材清单.csv", used.setdefault(dest_path, set()))
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["文件名", "标注文件", "目标词", "AI 评分", "任务编号", "任务名称",
                "来源网址", "站点", "采集时间"])
    for m in manifest:
        w.writerow([m["image"], m["caption"], ", ".join(m["target_terms"]),
                    m["score"], m["task_id"], m["task"],
                    m.get("source_url", ""), m.get("site", ""),
                    m.get("collected_at", "")])
    try:
        index_path.write_text("\ufeff" + buf.getvalue(), encoding="utf-8")
    except OSError:
        index_path = None

    manifest_path = _unique_path(dest_path, "素材清单.json", used.setdefault(dest_path, set()))
    try:
        manifest_path.write_text(json.dumps({
            "kind": "training-kit",
            "exported_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "min_score": min_score,
            "count": copied,
            "items": manifest,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

    if not copied:
        return {"ok": False,
                "error": "未找到符合条件的图片。请降低合格线或放宽筛选条件后重试。"}
    res = {
        "ok": True,
        "dest": str(dest_path),
        "copied": copied,
        "captions": captions,
        "skipped_missing": skipped_missing,
        "skipped_empty": skipped_empty,
        "min_score": round(min_score, 3),
        "index_file": index_path.name if index_path else "",
        "note": (f"已复制 {copied} 张图与 {captions} 份标注到「{dest_path}」，"
                 f"并按目标词生成素材清单。原图未作任何改动。"
                 + (f" 另有 {skipped_missing} 张源文件未找到、"
                    f"{skipped_empty} 张零字节图已跳过。"
                    if (skipped_missing or skipped_empty) else "")),
    }
    # V0.0.18：成功批次落一行导出历史（两端共用）。
    record_export(db, str(dest_path), res,
                  {**norm, "min_score": min_score})
    return res
