"""R249 · 磁盘级维护四件套（网页端与桌面端共用）。

四件事，原来**只有桌面端有**，而且其中之一在打包版里根本跑不起来：

1. **文件完整性检查** —— 列「文件缺失 / 0 字节 / 过小 / 无法解码」，
   只报告、不自动删。清不清由用户勾选后自己点。
2. **清理 0KB 与 .part 孤儿文件** —— 先移到 trash，再删；可回溯。
3. **批量转 JPG** —— avif/heic 之类 Qt 天生读不了，统一落成 jpg。
4. **修复图库** —— 恢复「被误隔离的图」、清掉「文件早已不在的死链」。

⚠ 为什么搬进包里
   原来第 4 件在 ``tools/repair_gallery.py``，桌面端写的是
   ``from tools.repair_gallery import main`` —— 而 ``tools/`` **不在打包清单里**
   （``build_nodel.py`` 只打 ``media_resource_manager``）。也就是说安装版点了
   「修复图库」必然 ModuleNotFoundError，只是没人点过。这里把它变成包内模块，
   桌面端与网页端共用同一份逻辑，护栏会钉住「不许再 import tools」。

设计取舍
--------
* **转 JPG 必须同步改数据库路径**。原 ``convert_file_to_jpg`` 只给人用（采集
  落盘时调用，那时还没有库记录），它不知道 DB 里有 path。库里存着 ``a.webp``
  而磁盘上已经是 ``a.jpg`` ⇒ 图直接从图库消失。所以这里转换成功后立刻
  ``UPDATE images SET path=?``，并顺手更新字节数。
* **目标文件已存在就不转**。``convert_file_to_jpg`` 会先删同名 .jpg 再改后缀
  （采集路径保证唯一，所以那是安全的）。但在图库里 ``a.webp`` 和 ``a.jpg``
  可能是两张不同的图 —— 直接转会把 a.jpg 覆盖掉。这里先判冲突、跳过并计数。
* **只报告不改数据**。除了「修复图库」这个用户明确点名的动作，检查类一律
  只读；清理类先落 trash。用户被删怕过，这里的默认值是「什么都不删」。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

#: 报告里最多带多少条明细（数量另给，不受这个上限影响）。
#: 100 万张库里坏文件可能有几万个 —— 全塞进 JSON 会让手机端直接卡死。
MAX_ITEMS = 3000

#: 与 convert_img 保持一致的「转 jpg 有意义」的判定
_JPG_EXT = {".jpg", ".jpeg"}
_IMG_EXT = {".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif",
            ".heic", ".heif", ".jfif"}
_SKIP_EXT = _JPG_EXT | {".mp4", ".webm", ".mkv", ".mov", ".avi", ".txt",
                        ".json", ".part", ".tmp"}


#: 「修复图库」把任务阈值压到这个值 —— 必须等于产品默认（见项目记忆）：
#: 本机 CLIP 对正确匹配的峰值约 0.65，阈值高于模型上限就会「全隔离」。
SAFE_THRESHOLD = 0.35


def _mk_progress(progress):
    """包一层「每 0.2 秒或每 200 次才真正回调一次」。

    10 万张逐张回调会把前端打爆（每秒几万次），而进度条只需要看起来连续。
    """
    if progress is None:
        return lambda *a, **k: None
    state = {"t": 0.0, "n": 0}

    def _cb(pct, msg=""):
        state["n"] += 1
        now = time.monotonic()
        if state["n"] % 200 and now - state["t"] < 0.2:
            return
        state["t"] = now
        progress(pct, msg)
    return _cb


# ───────────────────────────── ① 完整性检查 ─────────────────────────────
def scan(db, cfg, deep: bool = False, progress=None,
         stop=None) -> dict:
    """扫描库内所有图片文件，返回异常清单（**只读**）。

    ``deep=True`` 会逐张用 Pillow 解码验证 —— 10 万张要几分钟，
    但能抓出「文件大小正常、内容截断」的那一类（下载中断最常见）。
    """
    prog = _mk_progress(progress)
    _stop = stop or (lambda: False)
    susp = int(getattr(cfg, "suspicious_bytes", 1024) or 0)
    rows = db.query(
        "SELECT id, path FROM images "
        "WHERE path IS NOT NULL AND path<>'' ORDER BY id") or []
    total = len(rows)
    items, kinds, checked = [], {}, 0
    t0 = time.monotonic()
    for i, r in enumerate(rows):
        if _stop():
            return {"ok": True, "stopped": True, "total": total,
                    "checked": checked, "items": items, "kinds": kinds,
                    "elapsed": round(time.monotonic() - t0, 1)}
        if i % 500 == 0:
            prog(min(99, int(i * 100 / max(total, 1))),
                 f"已检查 {i} / {total}")
        p = Path(r["path"])
        why = ""
        size = 0
        try:
            if not p.exists():
                why = "文件缺失"
            else:
                size = p.stat().st_size
                if size == 0:
                    why = "0 字节"
                elif susp and size < susp:
                    why = f"过小（<{susp}B）"
                elif deep:
                    try:
                        from PIL import Image
                        with Image.open(p) as im:
                            im.verify()
                    except Exception:
                        why = "无法解码"
        except OSError:
            why = "无法读取"
        checked += 1
        if not why:
            continue
        kinds[why] = kinds.get(why, 0) + 1
        if len(items) < MAX_ITEMS:
            items.append({"id": int(r["id"]), "name": p.name, "why": why,
                          "size": size, "path": str(p)})
    prog(100, "扫描完成")
    return {"ok": True, "stopped": False, "total": total, "checked": checked,
            "items": items, "kinds": kinds,
            "elapsed": round(time.monotonic() - t0, 1),
            "truncated": sum(kinds.values()) > len(items)}


# ───────────────────────── ② 清理异常数据库记录 ─────────────────────────
def clean_records(db, ids) -> dict:
    """删除指定的图库记录（**只删库记录，不动磁盘文件**）。

    子表先删：``reviews`` 引用 ``images``，开了外键时先删父表会被拦下，
    而异常被吞掉就变成「界面说清完了、实际一条没删」。
    """
    n = 0
    for raw in ids or []:
        try:
            iid = int(raw)
        except (TypeError, ValueError):
            continue
        try:
            db.execute("DELETE FROM reviews WHERE image_id=?", (iid,))
            db.execute("DELETE FROM images WHERE id=?", (iid,))
            n += 1
        except Exception:
            continue
    try:
        db.audit("maintenance", "clean-records", str(n))
    except Exception:
        pass
    return {"ok": True, "deleted": n}


# ──────────────────────── ③ 清理 0KB / .part 孤儿 ────────────────────────
def orphan_dirs(db, cfg) -> list:
    """要扫哪些目录 —— **必须复用** ``tidy.clean_scan_dirs`` 的白名单。

    这里原本我自己写了一份「图库 + 隔离区 + 各任务输出 + 库里出现过的父目录」，
    那恰好是 R152-B4 修掉的越界问题：从文件夹学习建的任务，它的
    ``output_dir`` 就是**用户自己选的相册目录**，把那里当垃圾场扫，会顺手
    把用户相册里的 0 字节文件（甚至是 `.part`）搬走。父目录同理 ——
    回填/学习入库的图，父目录可能就在用户文件夹里。

    白名单的规则（见 ``tidy.clean_scan_dirs`` 的注释）：软件自己的三个目录
    始终扫；下载类任务只扫 ``output_dir`` **下一级子目录**；本地文件夹任务
    整棵排除；``images.path`` 的父目录一律不扫。
    """
    try:
        from .tidy import clean_scan_dirs
        return list(clean_scan_dirs(cfg, db))
    except Exception:
        # 白名单取不到时宁可不清理，也不能退回「扫一切」
        return []


def move_orphan_files(dirs, trash, progress=None, stop=None) -> dict:
    """把给定目录里的 0 字节图与 ``.part`` 残片移到 trash（**不是直接删**）。

    直接删的风险在于「0 字节」也可能是用户正在断点续传的文件 —— 移到
    trash 是可逆的，用户看完发现不对还能拿回来。

    目录由调用方决定：网页/桌面都走 ``orphan_dirs()``（带白名单），
    ``tidy`` 每个任务跑完也走同一套白名单。**不要在这里自作聪明加目录。**
    """
    prog = _mk_progress(progress)
    _stop = stop or (lambda: False)
    trash = Path(trash)
    try:
        trash.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "error": f"无法创建暂存目录：{e}"}
    moved, scanned, seen = 0, 0, set()
    for d in dirs:
        if _stop():
            break
        for root, _dirs, files in os.walk(d):
            if _stop():
                break
            for fn in files:
                low = fn.lower()
                if not (low.endswith((".jpg", ".jpeg", ".png", ".webp",
                                      ".gif", ".bmp", ".avif", ".heic",
                                      ".part", ".tmp"))):
                    continue
                p = Path(root) / fn
                key = str(p).lower()
                if key in seen:
                    continue
                seen.add(key)
                scanned += 1
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                if sz and not low.endswith((".part", ".tmp")):
                    continue
                try:
                    # 用递增序号而不是 hash(p)：同名不同目录会撞车覆盖
                    dest = trash / f"{moved:06d}_{fn}"
                    shutil.move(str(p), str(dest))
                    moved += 1
                except Exception:
                    continue
        prog(min(99, 50), f"已扫 {scanned} 个文件，清理 {moved} 个")
    prog(100, "清理完成")
    return {"ok": True, "moved": moved, "scanned": scanned,
            "dirs": len(dirs), "trash": str(trash)}


def clean_orphans(db, cfg, trash_dir=None, progress=None, stop=None) -> dict:
    """按白名单扫一遍、把孤儿文件移进 trash（网页端「清理 0KB」用的入口）。"""
    dirs = orphan_dirs(db, cfg)
    trash = Path(trash_dir) if trash_dir else (
        Path(getattr(cfg, "log_dir", ".") or ".") / "orphan_trash"
        / time.strftime("run-%Y%m%d_%H%M%S"))
    return move_orphan_files(dirs, trash, progress=progress, stop=stop)


# ─────────────────────────── ④ 批量转 JPG ───────────────────────────
def convertible_rows(db, limit: int = 0, cfg=None) -> list:
    """挑出「非 jpg 且文件还在」的库记录。

    V0.0.6：只挑**软件自己目录里**的文件。用户原有图库（例如 F 盘）只是
    在账本里登记了个地址，转格式会把原文件改名、再删掉原文件 —— 那是
    明令禁止的「不删 / 不移 / 不改名」。这类行一律跳过，统计里也不算。
    """
    rows = db.query(
        "SELECT id, path, bytes FROM images "
        "WHERE path IS NOT NULL AND path<>'' ORDER BY id") or []
    out = []
    for r in rows:
        p = Path(r["path"])
        if p.suffix.lower() not in _IMG_EXT:
            continue
        if cfg is not None:
            try:
                from .safe_delete import is_software_file
                if not is_software_file(p, cfg):
                    continue
            except Exception:
                continue          # 判不清就别动，安全优先
        out.append({"id": int(r["id"]), "path": str(p)})
        if limit and len(out) >= limit:
            break
    return out


def convert_to_jpg(db, cfg, dry_run: bool = False, limit: int = 0,
                   quality: int = 88, progress=None, stop=None) -> dict:
    """把库里的非 jpg 图转成 jpg，并**同步更新数据库路径**。

    ``dry_run=True`` 只统计「有多少张能转」，一张都不动 —— 10 万张转换会
    重写十几 G 数据，动手前必须能先看一眼规模。
    """
    from .convert_img import convert_file_to_jpg

    prog = _mk_progress(progress)
    _stop = stop or (lambda: False)
    rows = convertible_rows(db, limit, cfg)
    total = len(rows)
    converted, skipped, missing, failed, to_convert = 0, 0, 0, 0, 0
    repointed = 0
    conflicts = []
    for i, r in enumerate(rows):
        if _stop():
            break
        if i % 100 == 0:
            prog(min(99, int(i * 100 / max(total, 1))),
                 f"已处理 {i} / {total}")
        src = Path(r["path"])
        if not src.exists():
            # 同一个文件可能被两条记录引用（例如导入任务 + 隔离区各一条）。
            # 前一条已经把它转成 .jpg 了，后一条就「文件缺失」—— 那不是坏数据，
            # 只是路径没跟上。认「同名 .jpg」并改指过去，比把它当死链删掉合理。
            alt = src.with_suffix(".jpg")
            if alt != src and alt.exists():
                try:
                    db.execute("UPDATE images SET path=? WHERE id=?",
                               (str(alt), r["id"]))
                    repointed += 1
                    continue
                except Exception:
                    pass
            missing += 1
            continue
        try:
            if src.stat().st_size <= 0:
                missing += 1
                continue
        except OSError:
            missing += 1
            continue
        dst = src.with_suffix(".jpg")
        if dst != src and dst.exists():
            # a.webp 与 a.jpg 同时存在 ⇒ 转过去会覆盖另一张真图，跳过
            skipped += 1
            if len(conflicts) < 50:
                conflicts.append(src.name)
            continue
        if dry_run:
            to_convert += 1
            continue
        try:
            newp, conv = convert_file_to_jpg(src, quality)
        except Exception:
            failed += 1
            continue
        if not conv:
            failed += 1
            continue
        try:
            nbytes = newp.stat().st_size
        except OSError:
            nbytes = 0
        try:
            db.execute("UPDATE images SET path=?, bytes=? WHERE id=?",
                       (str(newp), nbytes, r["id"]))
        except Exception:
            # 库没更新成功就把文件改回去，不能让「文件是 jpg、库里写 webp」
            try:
                newp.rename(src)
            except Exception:
                pass
            failed += 1
            continue
        converted += 1
    prog(100, "转换完成")
    res = {"ok": True, "scanned": total, "converted": converted,
           "skipped": skipped, "missing": missing, "failed": failed,
           "repointed": repointed, "dry_run": bool(dry_run)}
    if dry_run:
        res["to_convert"] = to_convert
    if conflicts:
        res["conflicts"] = conflicts
    if not dry_run:
        try:
            db.audit("maintenance", "convert-jpg", str(converted))
        except Exception:
            pass
    return res


# ─────────────────────────── ⑤ 修复图库 ───────────────────────────
def repair(db, cfg, fix_threshold: bool = False,
           restore_quarantine: bool = True, drop_ghosts: bool = True,
           progress=None, stop=None) -> dict:
    """恢复误隔离的图 + 清掉指向已消失文件的死链。

    ``fix_threshold``：把所有启用 AI 的任务阈值降到产品默认安全值。
    这件事**会改动用户配置**，所以默认关闭 —— 桌面端当年是强制改的，
    结果用户不知道自己阈值被改过。要改就显式勾。
    """
    from .repo import list_tasks, save_task

    prog = _mk_progress(progress)
    _stop = stop or (lambda: False)
    out = {"threshold_changed": 0, "restored": 0, "kept": 0,
           "ghosts": 0, "visible_before": 0, "visible_after": 0}

    def _visible():
        try:
            return db.query(
                "SELECT COUNT(*) c FROM images WHERE path IS NOT NULL "
                "AND path<>'' AND blacklisted=0 AND in_quarantine=0")[0]["c"]
        except Exception:
            return 0

    out["visible_before"] = _visible()
    prog(5, "读取任务列表")

    if fix_threshold:
        safe = SAFE_THRESHOLD
        for t in list_tasks(db):
            if _stop():
                break
            try:
                if getattr(t, "ai_enabled", False) and t.ai_threshold > safe:
                    t.ai_threshold = safe
                    save_task(db, t)
                    out["threshold_changed"] += 1
            except Exception:
                continue

    # 重新打分：只对「隔离里、文件还在」的图做。这是唯一需要加载模型的步骤。
    if restore_quarantine:
        clf = None
        labels = []
        try:
            from .classify.labels_zh import expand_labels
            rows = db.query(
                "SELECT ai_labels FROM tasks WHERE ai_enabled=1 "
                "AND ai_labels IS NOT NULL AND ai_labels NOT IN ('', '[]') "
                "LIMIT 1")
            if rows:
                import json as _json
                labels = expand_labels(list(_json.loads(rows[0]["ai_labels"])))
        except Exception:
            labels = []
        if labels:
            prog(12, "加载本地模型…")
            try:
                from .classify.clip_model import LocalClassifier
                clf = LocalClassifier(
                    labels=labels,
                    model_dir=getattr(cfg, "ai_model_path", "") or None,
                    threads=max(1, (os.cpu_count() or 4) // 2))
            except Exception:
                clf = None
        safe = SAFE_THRESHOLD
        rows = db.query(
            "SELECT id, path FROM images WHERE in_quarantine=1 "
            "AND path IS NOT NULL AND path<>''") or []
        total = len(rows)
        for i, r in enumerate(rows):
            if _stop():
                break
            if i % 50 == 0:
                prog(15 + int(i * 75 / max(total, 1)),
                     f"重新打分 {i} / {total}")
            p = Path(r["path"])
            try:
                if not p.exists() or p.stat().st_size == 0:
                    continue        # 交给下面的死链清理
                score = 0.0
                if clf is not None:
                    pairs = clf.classify(p)
                    if pairs:
                        score = float(max(s for _lbl, s in pairs))
                if score >= safe:
                    db.execute(
                        "UPDATE images SET in_quarantine=0, needs_review=0, "
                        "score=? WHERE id=?", (score, r["id"]))
                    out["restored"] += 1
                else:
                    db.execute("UPDATE images SET score=? WHERE id=?",
                               (score, r["id"]))
                    out["kept"] += 1
            except Exception:
                continue

    # 死链：隔离里 + 可见里，path 有值但文件不在
    if drop_ghosts:
        prog(92, "清理死链记录")
        # 2026-11-05 盘失联熔断：可移动盘 / NAS 挂载整棵不可达时
        # Path.exists() 对该树一律 False，旧判据会把整棵的行判成死链**全删**。
        # 现在先判该文件是否有已存在的祖先目录（按祖先缓存，整树只 stat 一次）；
        # 不可达 = 盘不在，本轮跳过，等盘回来再清理。
        _reach_cache: dict[str, bool] = {}

        def _reachable(path_str: str) -> bool:
            try:
                cur = Path(path_str).parent
            except Exception:
                return True
            chain: list[str] = []
            out = True
            while True:
                key = str(cur)
                if key in _reach_cache:
                    out = _reach_cache[key]
                    break
                chain.append(key)
                if cur.exists():
                    out = True
                    break
                parent = cur.parent
                if parent == cur:
                    out = False
                    break
                cur = parent
            for k in chain:
                _reach_cache[k] = out
            return out

        for r in db.query(
                "SELECT id, path FROM images WHERE path IS NOT NULL "
                "AND path<>''") or []:
            if _stop():
                break
            try:
                if Path(r["path"]).exists():
                    continue
                if not _reachable(r["path"]):
                    continue                      # 整盘失联，本轮不清理
                db.execute("DELETE FROM reviews WHERE image_id=?", (int(r["id"]),))
                db.execute("DELETE FROM images WHERE id=?", (int(r["id"]),))
                out["ghosts"] += 1
            except Exception:
                continue
        try:
            for r in db.query(
                    "SELECT id FROM images WHERE path IS NULL OR path=''"):
                db.execute("DELETE FROM reviews WHERE image_id=?", (int(r["id"]),))
                db.execute("DELETE FROM images WHERE id=?", (int(r["id"]),))
                out["ghosts"] += 1
        except Exception:
            pass

    out["visible_after"] = _visible()
    prog(100, "修复完成")
    try:
        db.audit("maintenance", "repair-gallery",
                 f"restored={out['restored']} ghosts={out['ghosts']}")
    except Exception:
        pass
    return {"ok": True, **out}
