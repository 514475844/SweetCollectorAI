"""从任意文件夹批量学习 / 索引本地图库（本地 CLIP，0 积分，纯离线）。

这是给「用户自己的图库不在本机、以后换机喂给软件学习」准备的入口：
GUI「从文件夹学习…」选一个文件夹 → 这里把里面所有图片打标 + 入库，
作为一个独立任务出现在图库，用户随后可浏览 / 喜欢 / 不喜欢，偏好模型随之学习。

设计要点：
  - 本地 CLIP 推理，不上云、不计费；有显卡自动用 DirectML/CUDA，无卡回退 CPU。
  - 幂等：按 sha256 去重，同一张图重复学习自动跳过（force=True 可强制重打标）。
  - 不打乱用户文件夹：黑名单命中的图只标记 in_quarantine，不移动文件。
  - 进度回调 progress(done, total, name) 供 UI 显示。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Callable, Optional

LOG = logging.getLogger("mrm.learn_folder")

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
              ".tif", ".tiff", ".avif", ".heic"}

# 未配置主题词时的兜底标签集（保证图库至少有可搜的标签）。
DEFAULT_GENERIC_LABELS = [
    "person", "landscape", "animal", "food", "car", "building",
    "anime", "illustration", "photograph", "text",
    "white stockings", "black stockings", "lingerie", "swimsuit",
    "portrait", "group",
]


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def learn_folder(db, cfg, folder: str, task_id: int | None = None,
                force: bool = False,
                copy_to_library: bool = False,
                do_tag: bool = True,
                progress: Optional[Callable[[int, int, str], None]] = None
                ) -> dict:
    """扫描 folder 下所有图片，本地 CLIP 打标后入库。返回统计 dict。

    ``copy_to_library``（V0.0.5，与 import_folder 同一开关语义）：
        开 → 每张图复制一份进 图库/imported/<目录名>/，账本登记副本；
        之后删 / 移只碰副本，原图库（如 F 盘）彻底隔离。默认关＝只登记原地址。

    ``do_tag``（V0.0.7，与网页端「只入库、先不打标签」同一个勾）：
        关 → 只登记，不加载模型、不认图；以后取消勾选重导同一文件夹会
        给这些空标签的图补打标并学习偏好，不会重复入库、不会重复复制。
    """
    from .repo import save_task, get_task, upsert_image
    from .models import ImageRecord, TaskConfig, TaskStatus

    folder = Path(folder)
    if not folder.exists() or not folder.is_dir():
        return {"error": f"文件夹不存在: {folder}"}
    # V0.0.6：把这个来源目录记成「用户原有图库」，删除链路一律绕开。
    try:
        from .safe_delete import mark_external_root
        mark_external_root(cfg, folder)
    except Exception:
        pass

    # ---- 解析 / 创建任务（按 output_dir 稳定记忆，避免重复建）----
    task = get_task(db, task_id) if task_id else None
    if task is None:
        row = db.query("SELECT id FROM tasks WHERE output_dir=?", (str(folder),))
        if row:
            task = get_task(db, int(row[0]["id"]))
    if task is None:
        # V0.0.3：与全软件共用一张词表（含曾用词历史 + 中英双语候选）。
        from .labels_effective import effective_labels
        labels = effective_labels(cfg=cfg)
        t = TaskConfig(
            id=None, name=folder.name or "本地图库",
            adapter_name="", entry_urls=[str(folder)],
            output_dir=str(folder), max_depth=0, concurrency=1, delay=0.0,
            status=TaskStatus.IDLE,
            ai_enabled=bool(labels),
            ai_labels=labels,
            # #60：缺省阈值跟随新的全局默认 0.35
            ai_threshold=float(getattr(cfg, "ai_threshold", 0.35) or 0.35),
        )
        tid = save_task(db, t)
        task = get_task(db, tid)
    tid = task.id

    from .labels_effective import effective_labels
    labels = effective_labels(task=task, cfg=cfg)
    thr = float(task.ai_threshold or 0.35)
    # V0.0.3：本地图库 = 用户留下的正样本，不做隔离/筛选（道理见 import_folder）
    blacklist = set()
    clf = None
    ai_note = ""
    # V0.0.7：用户勾「只入库先不打标签」就是想先快点翻图，别为了一个
    # 用不上的模型去加载几百 MB 权重。
    if not do_tag:
        ai_note = "本次只入库、未做 AI 识别（标签为空是正常现象）"
    else:
        try:
            from .classify.clip_model import LocalClassifier
            # R152-D2：原来这里 `LocalClassifier(labels=labels)` 既不传 model_dir
            # 也不查 backend——cfg.ai_model_path 配了也白配，后端降级成 stub 也
            # 没人知道。stub 的 classify 读边车标签全打 1.0 ⇒ 每张图带一堆无关
            # 标签入库，用户以为"学好了"，其实是假标签（与 D1 同根，不同入口）。
            _model_dir = (getattr(cfg, "ai_model_path", "") or None)
            _threads = int(getattr(cfg, "ai_threads", 0) or 0)
            _gpu = bool(getattr(cfg, "ai_gpu", True))
            if labels:
                clf = LocalClassifier(labels=labels, model_dir=_model_dir,
                                      threads=_threads, gpu=_gpu)
            else:
                clf = LocalClassifier(model_dir=_model_dir,
                                      threads=_threads, gpu=_gpu)
            # 与 engine._clf/_excl_clf 同待遇：非 clip 后端不打标，明说原因。
            if getattr(clf, "backend", "") != "clip":
                ai_note = (f"本地 AI 处于降级模式（后端="
                           f"{getattr(clf, 'backend', '?')}）；图片只入库、未做 AI 识别")
                clf = None
        except Exception as e:
            clf = None
            ai_note = f"本地 AI 不可用：{e}"

    # V0.0.7：与网页端共用同一个找图函数。以前桌面用 rglob 把 .thumbnails /
    # @eaDir 里的缩略图也导进来打标，学到的口味跟网页端对不上，还平白
    # 多出一堆缩略图词去驱动站内找图。
    from .import_folder import iter_images
    files = iter_images(folder)
    total = len(files)
    # 模型回退也要带给用户看（配置的 L14 目录缺权重 → 实际跑 B32 这类）
    if clf is not None and getattr(clf, "model_note", ""):
        ai_note = (ai_note + "；" if ai_note else "") + clf.model_note
    stats = {"total": total, "added": 0, "skipped_dup": 0,
             "blacklisted": 0, "errors": 0, "task_id": tid,
             "copied": 0, "ai_note": ai_note}

    import json as _json
    for i, p in enumerate(files):
        if progress:
            progress(i, total, p.name)
        try:
            sha = _sha256(p)
            if not force:
                dup = db.query(
                    "SELECT id, tags, ai_state FROM images "
                    "WHERE sha256=? LIMIT 1", (sha,))
                if dup:
                    # V0.0.7：上次勾「只入库先不打标签」留下的空标签行，
                    # 这次取消勾选重导就补上标签并学偏好。不新建行、不碰
                    # 文件、不再复制副本，原图更不会被改一个字节。
                    try:
                        raw = str(dup[0]["tags"] or "").strip()
                    except Exception:
                        raw = ""
                    # ai_state=2 是「用户勾了只入库、等以后补标」的专属标记
                    # （见下面写入处）。正常打过标、只是模型没认出词的图是
                    # 默认的 1，不该每次重导都被拉去重新跑一遍模型。
                    try:
                        state = int(dup[0]["ai_state"] or 1)
                    except Exception:
                        state = 1
                    if (do_tag and clf is not None and labels and state == 2
                            and (not raw or raw == "[]")
                            and p.stat().st_size > 0):
                        try:
                            prs = clf.classify(p)
                            tg = [lbl for lbl, s in prs if s >= thr]
                            if tg:
                                db.execute(
                                    "UPDATE images SET tags=?, score=?, "
                                    "ai_state=1 WHERE id=?",
                                    (_json.dumps(tg, ensure_ascii=False),
                                     float(max((s for _, s in prs), default=0.0)),
                                     int(dup[0]["id"])))
                                stats["added"] += 1
                        except Exception as e:
                            stats["errors"] += 1
                            stats["last_error"] = f"{p.name}: 补做 AI 识别失败: {e}"
                        continue
                    stats["skipped_dup"] += 1
                    continue
            try:
                from PIL import Image
                with Image.open(p) as im:
                    w, h = im.size
            except Exception:
                w = h = 0
            # V0.0.5 用户定稿：0 字节 / 读不出尺寸的坏图一律不收、不登记。
            if w == 0 or h == 0 or p.stat().st_size == 0:
                stats["errors"] += 1
                continue
            # V0.0.7：打分失败**不能连累入库**。原来这一句在外层 try 里，
            # 模型偶发异常（显存/内存紧张、并发全量跑时更常见）会让这张图
            # 直接记一条 errors、既不入库也不复制 —— 用户看到「导入 0 张」。
            # 认不出内容不等于这张图不该进库：标签留空照样入库，以后还能补打。
            try:
                pairs = clf.classify(p) if (do_tag and clf and labels) else []
            except Exception as e:
                pairs = []
                LOG.warning("本地 AI 打分失败，标签留空但照常入库 %s: %s", p, e)
            # ⚠ 2026-10-29 修：这里原来写死 `s >= 0.5`，而同一段的黑名单判定
            # 用的是 `task.ai_threshold`（默认 0.35）—— 同一个库里的图，
            # 「采集进来的」按任务阈值打标签、「从文件夹导入的」按 0.5 打，
            # 用户把阈值调成 0.6 也只会影响一半。现在统一跟随任务阈值，
            # 与 engine._apply_ai 的 any_match 判据同一条线。
            tags = [lbl for lbl, s in pairs if s >= thr]
            best = max((s for _, s in pairs), default=0.0)
            # V0.0.3：导入的是用户自己的图，黑名单不参与处置（blacklist 恒空）
            hit_bl = False
            # V0.0.6：账本身份固定用「源文件地址」，复制模式下也别改成副本
            # 地址 —— 否则重导一次就多一行、多一份 x(1).jpg。
            src_key = f"file://{p.resolve()}"
            reg = str(p)
            if copy_to_library:
                # 复制模式：与 import_folder 同一套落点规则（共用 _copy_dest）。
                # 源目录本身就在软件图库里时不复制，避免自己复制自己。
                from .import_folder import _copy_dest, _existing_copy
                import shutil as _sh
                try:
                    lib = Path(cfg.library_dir).resolve()
                    prev = _existing_copy(db, src_key, cfg)
                    if prev:
                        # 已经复制过：force 重导不造第二份副本
                        reg = str(prev)
                    elif p.resolve().is_relative_to(lib):
                        reg = str(p)
                    else:
                        dest = _copy_dest(Path(cfg.library_dir), folder.name, p)
                        try:
                            _sh.copy2(str(p), str(dest))
                            reg = str(dest)
                            stats["copied"] += 1
                        except Exception:
                            try:
                                Path(dest).unlink(missing_ok=True)
                            except Exception:
                                pass
                            raise
                except Exception as e:
                    stats["errors"] += 1
                    stats["last_error"] = f"{p.name}: {e}"
                    continue
            rec = ImageRecord(
                id=None, source_url=src_key, task_id=tid, sha256=sha,
                width=w, height=h, bytes=p.stat().st_size, path=reg,
                tags=tags, score=float(best),
                blacklisted=hit_bl, in_quarantine=hit_bl,
            )
            upsert_image(db, rec)
            if not do_tag:
                # V0.0.7：记住「这张是用户主动跳过打标的」，以后取消勾选
                # 重导时精准补标。不写这个标记的话，正常导入里那些模型
                # 没认出词的图会被每次重导反复拿去跑模型，还把旧的
                # 「重复导入跳过」语义弄坏。ai_state 只有 0/1 两个既有值，
                # 2 是新增的第三种状态，补标成功就回到 1。
                try:
                    db.execute(
                        "UPDATE images SET ai_state=2 "
                        "WHERE task_id=? AND source_url=?",
                        (tid, src_key))
                except Exception:
                    pass
            if hit_bl:
                stats["blacklisted"] += 1
            else:
                stats["added"] += 1
        except Exception as e:
            stats["errors"] += 1
            stats["last_error"] = f"{p.name}: {type(e).__name__}: {e}"
    if progress:
        progress(total, total, "完成")

    # V0.0.3 导入即学习：这批图本身就在告诉软件「我喜欢这些」。
    # 打完标签立刻学，不再等用户一张张点喜欢（幂等，重复导入只补差额）。
    try:
        from .learn_imported import learn_from_imported
        learned = learn_from_imported(db, tid, source="learn_folder")
        stats["learned"] = learned
        # V0.0.4：学习结果由界面按 stats["learned"] 单独显示（「已从这批图
        # 自动学到 N 个偏好词」），ai_note 只放模型状态（降级 / 不可用）。
        # 以前两边都写一遍，界面会出现「已学到 1 个词（已学到 1 个词）」，
        # 还把「模型正常」误报成有提示。
    except Exception as e:
        stats["learned"] = {"ok": False, "reason": str(e)}
    return stats
