"""从文件夹批量导入图库：两阶段、可停可续、带进度与剩余时间（R248）。

为什么拆两阶段
--------------
- **索引阶段**只读 stat + 文件头（拿宽高），10 万张约一两分钟。跑完用户
  当场就能翻、能搜、能审 —— 不必等打标。
- **打标阶段**要跑 CLIP。10 万张在台式机上几小时、在小主机上十几小时，
  它必须能中途停、明天接着打。

可停可续靠什么
--------------
`images.ai_state` 列：**0=待打标 / 1=已打标**。进程重启后
`SELECT ... WHERE ai_state=0` 就是「还没打的那批」，不需要另存断点文件，
也不会因为换了机器、换了路径而对不上。

限核
----
打标**不另起线程池**（串行 + ONNX 内部多线程），线程数取
`cfg.ai_threads`，为 0 时退成「一半核数」—— 这样前台点网页、看图库仍然跟手。
实测 L14 在 8 线程比 20 线程还快 41%（并行过度有同步开销），所以限核不是纯损失。

坏文件
------
V0.0.5 用户定稿：0 字节、解不开的图**一律不收、不登记**（复制模式下也不
产生空壳副本）。计数并把前若干个路径放进结果的 `broken` 列表，让用户知道
漏了谁，但绝不把坏图塞进图库。
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from pathlib import Path

LOG = logging.getLogger("mrm.import_folder")

# 2026-10-30：复用 learn_folder 的内置通用词表（桌面端早就用它兜底）
from .learn_folder import DEFAULT_GENERIC_LABELS
from typing import Callable, Optional

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
              ".tif", ".tiff", ".avif", ".heic"}

#: 索引阶段每多少行提交一次（每行一个事务 = 每行一次 fsync，10 万行会慢到分钟级）
INDEX_BATCH = 500
#: 打标阶段每多少行提交一次（拿锁时间要短，否则网页点图库会转圈）
TAG_BATCH = 50
#: 坏文件路径最多回报几条（够用户知道是哪些，不必把几千条塞进界面）
BROKEN_MAX = 50

#: 同一时刻只允许一个导入在跑（两个线程同时写库 + 抢 CPU 只会互相拖慢）
_RUNNING = threading.Lock()


def is_running() -> bool:
    """当前是否有导入在跑（前端用来禁用按钮）。"""
    got = _RUNNING.acquire(blocking=False)
    if got:
        _RUNNING.release()
        return False
    return True


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def _size_of(path: Path) -> tuple[int, int]:
    """读宽高（只读文件头，不解码）。解不开返回 (0, 0)。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def iter_images(folder: Path, recursive: bool = True) -> list[Path]:
    """列出目录下所有图片（默认递归，跳过隐藏目录）。

    用 `os.walk` 而不是 `rglob`：后者在 10 万文件上要构造大量 Path 对象，
    实测慢 2–3 倍。隐藏目录（`.git` / `.thumbnails` / 群晖的 `@eaDir`）
    直接跳过 —— 那里面通常是一堆同名缩略图，导进来只会污染图库。

    V0.0.35：``recursive=False`` 只收所选文件夹这一层的图片，子文件夹
    一张都不进。用户有时会先挑一层试导，不想把下面几十个子目录一起拖进来。
    桌面端「从文件夹学习」不传这个参数，行为不变（仍然全递归）。
    """
    out: list[Path] = []
    if not recursive:
        try:
            for p in folder.iterdir():
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                    out.append(p)
        except OSError:
            pass
        out.sort(key=lambda p: str(p))
        return out
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and not d.startswith("@")]
        for f in files:
            if os.path.splitext(f)[1].lower() in IMAGE_EXTS:
                out.append(Path(root) / f)
    out.sort(key=lambda p: str(p))
    return out


def _fmt_eta(sec: float) -> str:
    try:
        s = int(max(0, sec))
    except (TypeError, ValueError):
        return ""
    # 不到 5 秒就别报了：算出来是 0/1/2 秒的抖动，"约剩 0 秒"看着像出错
    if s < 5:
        return ""
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分钟"
    return f"{s // 3600} 小时 {(s % 3600) // 60} 分"


class _Meter:
    """滑动平均算「每张多少秒」，用来给剩余时间（不用从头平均，避免前期抖动）。"""

    def __init__(self, window: int = 60):
        self.window = window
        self.times: list[float] = []
        self.last = time.time()

    def tick(self) -> None:
        now = time.time()
        self.times.append(now - self.last)
        self.last = now
        if len(self.times) > self.window:
            del self.times[:len(self.times) - self.window]

    def eta(self, left: int) -> float:
        if not self.times or left <= 0:
            return 0.0
        return (sum(self.times) / len(self.times)) * left


def resolve_task(db, cfg, folder: Path, task_id: Optional[int]):
    """找到/建出这个目录对应的任务（与「从文件夹学习」同一套记忆规则）。

    按 `output_dir` 稳定记忆：同一个文件夹重复导入不会每来一次就多一个任务。
    """
    from .models import TaskConfig, TaskStatus
    from .repo import get_task, save_task

    task = get_task(db, task_id) if task_id else None
    if task is None:
        row = db.query("SELECT id FROM tasks WHERE output_dir=? ORDER BY id LIMIT 1",
                       (str(folder),))
        if row:
            task = get_task(db, int(row[0]["id"]))
    if task is not None:
        return task
    # 2026-10-30：与 learn_folder.py 对齐 —— 用户没配全局词表时用内置通用词表。
    # 原来这里 `labels = cfg.ai_labels` 直接可能为空，建出的任务 ai_labels 也为空，
    # 于是 _tag_stage 判「没有配置主题词」直接跳过打标（网页端学一个文件夹
    # 217 张图、0 个标签，就是这个原因）。桌面端的 learn_folder 有兜底、
    # 网页端的 import_folder 没有 —— 同一个仓库两个入口两套行为。
    # V0.0.3：全软件共用一张词表（含曾用词历史 + 中英双语候选），
    # 别再各入口各写一套，否则同图不同标签、学到的权重对不上号。
    from .labels_effective import effective_labels
    labels = effective_labels(cfg=cfg)
    t = TaskConfig(
        id=None, name=folder.name or "本地图库",
        adapter_name="", entry_urls=[str(folder)],
        output_dir=str(folder), max_depth=0, concurrency=1, delay=0.0,
        status=TaskStatus.IDLE,
        ai_enabled=bool(labels),
        ai_labels=labels,
        ai_threshold=float(getattr(cfg, "ai_threshold", 0.35) or 0.35),
    )
    return get_task(db, save_task(db, t))


def import_folder(db, cfg, folder, *,
                  task_id: Optional[int] = None,
                  force: bool = False,
                  do_tag: bool = True,
                  copy_to_library: bool = False,
                  recursive: bool = True,
                  tag_threads: int = 0,
                  stop: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[..., None]] = None) -> dict:
    """把 ``folder`` 里的图片导入图库。返回统计 dict。

    ``progress(phase, done, total, name, eta_text, pct)``
        phase：``index`` / ``tag``；pct 是**整体**百分比（索引 0–20）。
    ``stop()`` 返回 True 时优雅停下：已入库的保留、已打标的保留，
        下次调用会从剩下的一批继续。
    ``copy_to_library``（V0.0.5 用户定稿的开关，默认关）：
        开 → 每张图**复制一份**进软件自己的图库目录（library/imported/<目录名>/），
        账本登记的是这份副本。之后软件里所有删除 / 搬动只碰副本，
        原文件夹彻底隔离（连"只登记不删"的承诺都不必再依赖保护闸）。
        关 → 老行为：只登记原文件地址，保护闸兜底不删外部文件。
        复制失败（盘满 / 没权限）单张记错跳过，不中断整批。
    ``recursive``（V0.0.35 用户要求，默认开）：关 → 只导入所选文件夹这一层，
        不进任何子文件夹；开 → 老行为，连同所有下级目录一起导。
    """
    folder = Path(folder)
    if not folder.exists() or not folder.is_dir():
        return {"ok": False, "error": f"文件夹不存在：{folder}"}
    # V0.0.6：把这个来源目录记成「用户原有图库」，以后任何删除入口
    # 都绕开它 —— 不只是靠「文件在不在软件目录里」这一道判断。
    try:
        from .safe_delete import mark_external_root
        mark_external_root(cfg, folder)
    except Exception:
        pass
    if not _RUNNING.acquire(blocking=False):
        return {"ok": False, "error": "已有一个导入任务在跑，等它结束再开始"}
    try:
        return _run(db, cfg, folder, task_id=task_id, force=force,
                    do_tag=do_tag, copy_to_library=copy_to_library,
                    recursive=recursive, tag_threads=tag_threads,
                    stop=stop, progress=progress)
    finally:
        _RUNNING.release()


def tag_pending(db, cfg, task_id: int, progress=None,
                stop: Optional[Callable[[], bool]] = None) -> dict:
    """V0.0.51：只补这个任务里 ai_state=0 的标签（启动恢复用）。

    与 ``import_folder`` 的区别：不重新扫目录、不登记新图，只把上次没打完
    的那批跑完。和导入共用同一把 ``_RUNNING`` 锁 —— 自动恢复绝不能跟用户
    手动发起的导入抢同一批行。
    """
    if not _RUNNING.acquire(blocking=False):
        return {"ok": False, "error": "已有一个导入/打标任务在跑"}
    try:
        from .repo import get_task
        task = get_task(db, int(task_id))
        if task is None:
            return {"ok": False, "error": "任务不存在"}
        stats = {"ok": True, "task_id": int(task.id), "tag_total": 0,
                 "tagged": 0, "pending": 0, "stopped": False, "ai_note": ""}

        def _stop() -> bool:
            try:
                return bool(stop and stop())
            except Exception:
                return False

        def _prog(phase, done, total, name, meter):
            if not progress:
                return
            try:
                pct = (20 + 80 * (done / total)) if total else 100
                progress(phase, done, total, name, "", int(pct))
            except Exception:
                pass

        _tag_stage(db, cfg, int(task.id), task, stats, _stop, _prog, 0)
        left = db.query(
            "SELECT COUNT(*) c FROM images WHERE task_id=? AND ai_state=0",
            (int(task.id),))
        stats["pending"] = int(left[0]["c"] or 0) if left else 0
        return stats
    finally:
        _RUNNING.release()


def wake_untagged_for_retag(db, tid: int, src_key: str, path: str) -> int:
    """V0.0.7：把「只入库先不打标签」留下的空标签行放回待打标队列。

    以前这类行被写成 ``ai_state=1``（已打标），于是永远进不了补标队列、
    标签永远空、也就永远学不到偏好。取消勾选重导时调一次即可唤醒。
    只动本任务里「确实没标签」的行，不碰文件、不新建行。
    """
    try:
        cur = db.execute(
            "UPDATE images SET ai_state=0 WHERE task_id=? "
            "AND (source_url=? OR path=?) "
            "AND (tags IS NULL OR tags='' OR tags='[]')",
            (int(tid), src_key, path))
        return int(getattr(cur, "rowcount", 0) or 0)
    except Exception:
        return 0


def _existing_copy(db, src_key: str, cfg=None):
    """这个源文件是不是已经复制过一份、且副本还在？有就别再复制。

    V0.0.6：force=True 的语义是「重新登记 / 重新打标」，不该每次都
    再造一份 x(1).jpg。只认软件自己目录里的副本 —— 若上次没开复制、
    账本里存的是外部原图地址，这次开了开关仍要真复制一份。
    """
    try:
        rows = db.query("SELECT path FROM images WHERE source_url=? LIMIT 1",
                        (src_key,))
        if rows and rows[0]["path"] and Path(rows[0]["path"]).exists():
            p = rows[0]["path"]
            if cfg is not None:
                from .safe_delete import is_software_file
                if not is_software_file(p, cfg):
                    return None
            return p
    except Exception:
        pass
    return None


def _copy_dest(lib_dir: Path, folder_name: str, src: Path) -> Path:
    """复制模式下副本落点：library/imported/<来源目录名>/，重名自动编号。"""
    d = lib_dir / "imported" / (folder_name or "library")
    d.mkdir(parents=True, exist_ok=True)
    dest = d / src.name
    k = 1
    while dest.exists():
        dest = d / f"{src.stem}({k}){src.suffix}"
        k += 1
    return dest


def _run(db, cfg, folder: Path, *, task_id, force, do_tag, tag_threads,
         stop, progress, copy_to_library: bool = False,
         recursive: bool = True) -> dict:
    task = resolve_task(db, cfg, folder, task_id)
    if task is None:
        return {"ok": False, "error": "任务创建失败"}
    tid = int(task.id)

    def _stop() -> bool:
        try:
            return bool(stop and stop())
        except Exception:
            return False

    def _prog(phase, done, total, name, meter):
        if not progress:
            return
        try:
            if phase == "index":
                pct = 20 * (done / total) if total else 20
                eta = meter.eta(total - done) if meter else 0.0
            else:
                pct = 20 + 80 * (done / total) if total else 100
                eta = meter.eta(total - done) if meter else 0.0
            progress(phase, done, total, name, _fmt_eta(eta), int(pct))
        except Exception:
            pass

    stats = {"ok": True, "task_id": tid, "total": 0, "indexed": 0,
             "skipped_dup": 0, "errors": 0, "broken": [],
             "tag_total": 0, "tagged": 0, "pending": 0,
             "stopped": False, "ai_note": ""}

    # ───────── 阶段一：索引 ─────────
    files = iter_images(folder, recursive=recursive)
    total = len(files)
    stats["total"] = total
    # 已有路径一次性读进内存：`WHERE path=?` 没有索引，逐条查就是 10 万次
    # 全表扫描。10 万个路径字符串约几 MB，换掉十分钟的扫描很划算。
    existing_paths = set()
    for _r in db.query("SELECT path, source_url FROM images"):
        if _r["path"]:
            existing_paths.add(_r["path"])
        if _r["source_url"]:
            existing_paths.add(_r["source_url"])
    meter = _Meter()

    def _write_batch(rows: list) -> None:
        """一批写完再提交。每行一个事务 = 每行一次 fsync，10 万行要几分钟。"""
        with db.transaction():
            for row in rows:
                db.execute(
                    """INSERT INTO images
                       (task_id, source_url, sha256, width, height, bytes, path,
                        tags, score, blacklisted, in_quarantine, ai_state)
                       VALUES (?,?,?,?,?,?,?,'[]',0,0,0,?)
                       ON CONFLICT(task_id, source_url) DO UPDATE SET
                         sha256=excluded.sha256, width=excluded.width,
                         height=excluded.height, bytes=excluded.bytes,
                         path=excluded.path
                       -- 2026-11-18 修「打标不能断点」：原来这里带着
                       -- ai_state=excluded.ai_state，force 重导会把整个任务
                       -- 已打标的行统统重置回待打标，进度全丢、只能从头再打。
                       -- 断点续打的权威状态就是 ai_state 本身：新行按参数
                       -- 置 0/1，老行保留原值。真要重打走 wake_untagged_for_retag。
                    """,
                    row)

    buf: list = []
    # 同一批里的重复图（sha 相同、路径不同）查库查不到 —— 那批还没提交。
    # 用一个内存集合兜住，否则一次导入里同一张图会出现两行。
    seen_sha: set = set()
    for i, p in enumerate(files):
        if _stop():
            stats["stopped"] = True
            break
        sp = str(p)
        _prog("index", i, total, p.name, meter)
        meter.tick()
        try:
            # V0.0.6：source_url 固定用「源文件地址」当唯一身份。以前复制
            # 模式下把 source_url 也写成副本路径，于是账本里根本没有原路径，
            # 下次导入（尤其 force=True 跳过 sha 去重）会再造一份 x(1).jpg、
            # 再插一行 —— 同一张图导入两次变成两行两副本。
            src_key = f"file://{p.resolve()}"
            if not force and (sp in existing_paths or src_key in existing_paths):
                stats["skipped_dup"] += 1
                # V0.0.7：以前网页勾「只入库、先不打标签」会把 ai_state 写成
                # 1（已打标），于是这些图永远进不了补标队列、标签永远空、
                # 也就永远学不到偏好 —— 导入这条链对它们彻底断掉。这次
                # 取消勾选、带打标重导时把空标签行放回待打标队列，阶段二
                # 的 _tag_stage 按 task_id 就能捞到并补上（不新建行、不动文件）。
                if do_tag:
                    wake_untagged_for_retag(db, tid, src_key, sp)
                continue
            st = p.stat()
            sha = _sha256(p)
            # force 的语义是「重新登记 / 重新打标」，不该变成「跳过查重、
            # 每次都插新行」。同一批内、以及库里已有同内容时都只记一条。
            if sha in seen_sha:
                stats["skipped_dup"] += 1
                continue
            if not force:
                dup = db.query("SELECT id FROM images WHERE sha256=? LIMIT 1", (sha,))
                if dup:
                    stats["skipped_dup"] += 1
                    continue
            w, h = _size_of(p)
            if w == 0 or h == 0 or st.st_size == 0:
                # V0.0.5 用户定稿：0 字节 / 读不出尺寸的坏图，
                # **无论什么情况都不收，导入时也不登记**。
                stats["errors"] += 1
                if len(stats["broken"]) < BROKEN_MAX:
                    stats["broken"].append(sp)
                continue
            reg_path = sp
            if copy_to_library:
                # 复制模式：副本落进软件图库，账本登记副本地址；
                # 原文件（如 F 盘）从此与删除链路彻底无关。
                # 源目录本身就在软件图库里时别「自己复制自己」——
                # 那会每导一次多一份副本，还把源和副本混在一棵目录里。
                try:
                    lib = Path(cfg.library_dir).resolve()
                    prev = _existing_copy(db, src_key, cfg)
                    if prev:
                        # 已经复制过：force 重导只更新账本，不造第二份副本
                        reg_path = str(prev)
                    elif p.resolve().is_relative_to(lib):
                        reg_path = sp
                    else:
                        import shutil as _sh
                        dest = _copy_dest(Path(cfg.library_dir), folder.name, p)
                        try:
                            _sh.copy2(str(p), str(dest))
                            reg_path = str(dest)
                            stats["copied"] = stats.get("copied", 0) + 1
                        except Exception:
                            # 复制半截留下的空壳是软件自己刚生成的文件，
                            # 清掉不算碰用户原图；源图一个字节不动。
                            try:
                                Path(dest).unlink(missing_ok=True)
                            except Exception:
                                pass
                            raise
                except Exception as e:
                    stats["errors"] += 1
                    stats["last_error"] = f"{p.name}: {e}"
                    continue
            buf.append((tid, src_key, sha, w, h, int(st.st_size), reg_path,
                        0 if do_tag else 1))
            existing_paths.add(sp)
            existing_paths.add(src_key)
            existing_paths.add(reg_path)
            seen_sha.add(sha)
            if len(buf) >= INDEX_BATCH:
                _write_batch(buf)
                stats["indexed"] += len(buf)
                buf = []
        except Exception as e:
            stats["errors"] += 1
            stats["last_error"] = f"{p.name}: {type(e).__name__}: {e}"
            LOG.warning("导入单张图失败：%s（%s）", p, e)
    if buf:
        _write_batch(buf)
        stats["indexed"] += len(buf)
    _prog("index", total, total,
          "已停止" if stats["stopped"] else "索引完成", None)

    # ───────── 阶段二：打标 ─────────
    if not do_tag:
        # 只索引时明确说一句：否则用户看到「几十万张、一个标签都没有」
        # 会以为是打标失败了。
        stats["ai_note"] = "本次只做索引、未打标（标签为空是正常现象）"
    if do_tag:
        if _tag_stage(db, cfg, tid, task, stats, _stop, _prog, tag_threads):
            stats["stopped"] = True

    left = 0
    try:
        left = int(db.query(
            "SELECT COUNT(*) c FROM images WHERE task_id=? AND ai_state=0",
            (tid,))[0]["c"] or 0)
    except Exception:
        pass
    stats["pending"] = left

    # ───────── 阶段三：导入即学习（V0.0.3 的核心修正）─────────
    # 用户把自己已有的图库交给软件，这件事本身就是「这些是我喜欢的」。
    # 老设计把它当成待审素材，非要用户后来一张张点喜欢才学，于是导入
    # 十万张、偏好表仍是空的。现在打完标签就自动把口味学出来。
    # 幂等：重复导入只补差额，不会把权重越堆越高（见 learn_imported）。
    try:
        from .learn_imported import learn_from_imported
        learned = learn_from_imported(db, tid, source="import_folder")
        stats["learned"] = learned
        if learned.get("written"):
            stats["ai_note"] = (
                (stats["ai_note"] + "；" if stats["ai_note"] else "")
                + f"已从这批图自动学到 {learned['written']} 个偏好词")
    except Exception as e:
        stats["learned"] = {"ok": False, "reason": str(e)}
    return stats


def _tag_stage(db, cfg, tid: int, task, stats: dict, _stop, _prog,
               tag_threads: int) -> bool:
    """给 ai_state=0 的行打标。每批取 TAG_BATCH 张，打完再取下一批。

    返回 ``True`` 表示**被停止**中断（调用方要据此把结果标成「已停止」，
    否则界面会把「停了一半」显示成「导入完成」）。
    """
    # 2026-10-30：三级兜底 —— 任务词表 → 全局词表 → 内置通用词表。
    # 少了最后一级时，历史上建的老任务（ai_labels 存的是空数组）会永远
    # 判「没有配置主题词」而不打标，用户在图库上看到的就是标签全空。
    # V0.0.3：与全软件共用一张词表（任务词 → 全局词 → 曾用词历史 → 通用词，
    # 并统一补中英候选），不再各入口各写一套。
    from .labels_effective import effective_labels
    labels = effective_labels(task=task, cfg=cfg)
    thr = float(getattr(task, "ai_threshold", 0.0) or 0.35)
    # V0.0.3：本地图库是用户已经留下的图 = 现成的正样本，不是待筛的素材。
    # 老逻辑拿任务排除词把导入的图丢进隔离区，等于「机器先学会了我喜欢啥，
    # 再来筛我的私人图库」，方向反了。导入阶段一律不隔离、不删。
    blacklist = set()

    clf = None
    note = ""
    if labels:
        try:
            from .classify.clip_model import LocalClassifier
            threads = int(tag_threads or getattr(cfg, "ai_threads", 0) or 0)
            if threads <= 0:
                # 0 原本是「用满所有核」；批量导入时改成「一半核」，
                # 让前台点网页、看图库仍然跟手（这就是「限核」的落点）。
                threads = max(1, (os.cpu_count() or 4) // 2)
            # V0.0.22：打标改成多线程并行后，单次推理自己的线程数要按
            # 并发数除一下 —— 否则「8 个 worker × 8 线程」= 64 线程抢
            # 10 个物理核，实测比串行还慢。核数 = 并发 × 每次线程数。
            _workers_hint = int(getattr(cfg, "ai_threads", 0) or 0) or \
                max(1, (os.cpu_count() or 4) // 2)
            _workers_hint = max(1, min(8, _workers_hint))
            threads = max(1, min(threads, (os.cpu_count() or 4) // _workers_hint))
            clf = LocalClassifier(
                labels=labels,
                model_dir=(getattr(cfg, "ai_model_path", "") or None),
                threads=threads, gpu=bool(getattr(cfg, "ai_gpu", True)))
            if getattr(clf, "backend", "") != "clip":
                note = (f"本地 AI 处于降级模式（后端={getattr(clf, 'backend', '?')}）；"
                        "图片已入库但未打标签")
                clf = None
            elif getattr(clf, "model_note", ""):
                note = str(clf.model_note)
        except Exception as e:
            clf = None
            note = f"本地 AI 不可用：{e}"
    else:
        note = "没有配置主题词，本次只入库不打标签"
    stats["ai_note"] = note

    try:
        pending = int(db.query(
            "SELECT COUNT(*) c FROM images WHERE task_id=? AND ai_state=0",
            (tid,))[0]["c"] or 0)
    except Exception:
        pending = 0
    stats["tag_total"] = pending
    stopped = False
    if not pending:
        return False

    if clf is None:
        # 没模型：把待打标标记清掉（否则每次进来都显示「还有 N 张待打标」），
        # 但**不动 tags**（用户的图不该被写上假标签）。
        db.execute("UPDATE images SET ai_state=1 WHERE task_id=? AND ai_state=0",
                   (tid,))
        return False

    import json as _json
    from concurrent.futures import ThreadPoolExecutor

    # 2026-11-13（V0.0.22）：打标提速 + 不卡界面。
    # 老写法把「推理」整段放进 db.transaction() 里 —— 事务期间软件共用
    # 那条 SQLite 连接被写锁占着，网页任何查询都得排队（实测导入跑到
    # 3 千张时，/api/learning、/api/find-terms 卡到 45 秒超时），同时
    # 界面线程的长查询也反过来把打标线程拖成 100 张/分钟（真机实测）。
    # 现口径：**推理在事务外并行做，写回只占一次短事务**。
    workers = int(getattr(cfg, "ai_threads", 0) or 0)
    if workers <= 0:
        workers = max(1, (os.cpu_count() or 4) // 2)
    workers = max(1, min(8, workers))

    done = 0
    meter = _Meter()

    # V0.0.46：打标顺手把图片向量也存下来（classify_with_vec 一次推理同时
    # 出标签和向量，零额外成本）。这样以后新导入的图天然带语义特征，
    # 只有历史存量需要单独「补算」。
    from .semantic import save_image_vec
    _model_id = getattr(clf, "model_id", "") or ""

    def _tag_one(item):
        rid, p = item
        # 停止仍按「每张」判：并行只是提速，不该让「停止」从点下就多打 50 张。
        if _stop():
            return rid, None
        try:
            cwv = getattr(clf, "classify_with_vec", None)
            if cwv is not None:
                return rid, cwv(p)
            # 老式/测试桩分类器只有 classify：照常打标，向量缺省
            return rid, (clf.classify(p), None)
        except Exception:                          # noqa: BLE001
            return rid, ([], None)

    while True:
        if _stop():
            stopped = True
            break
        rows = db.query(
            "SELECT id, path FROM images WHERE task_id=? AND ai_state=0 "
            "ORDER BY id LIMIT ?", (tid, TAG_BATCH))
        if not rows:
            break
        items = [(int(r["id"]), str(r["path"] or "")) for r in rows]
        # ① 事务外并行推理（CLIP 内部另有并发闸门，不会把核打满）
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
            results = list(ex.map(_tag_one, items))
        if any(res is None for _, res in results):
            stopped = True                     # 中途被停：这批判过的照写回
        # ② 一次性短事务写回（毫秒级，界面查询不会被卡住）
        wrote = 0
        with db.transaction():
            for rid, res in results:
                if res is None:
                    continue
                pairs, vec = res
                tags = [lbl for lbl, s in pairs if s >= thr]
                best = max((s for _, s in pairs), default=0.0)
                hit_bl = any(lbl in blacklist and s >= thr for lbl, s in pairs)
                try:
                    db.execute(
                        "UPDATE images SET tags=?, score=?, blacklisted=?, "
                        "in_quarantine=?, ai_state=1 WHERE id=?",
                        (_json.dumps(tags, ensure_ascii=False), float(best),
                         1 if hit_bl else 0, 1 if hit_bl else 0, int(rid)))
                except Exception:                  # noqa: BLE001
                    continue
                # V0.0.46：向量与标签同批写回（同一次短事务，不多占锁）
                if vec is not None:
                    save_image_vec(db, int(rid), vec, _model_id)
                wrote += 1
        done += sum(1 for _, res in results if res is not None)
        for _ in results:
            meter.tick()
        _prog("tag", done, pending, "", meter)
        stats["tagged"] += wrote
        if wrote == 0:
            break                       # 整批都失败：避免死循环
        if stopped:
            break
    _prog("tag", done, pending, "已停止" if stopped else "打标完成", None)
    return stopped
