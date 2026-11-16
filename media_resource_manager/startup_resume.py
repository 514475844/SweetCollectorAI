"""V0.0.51 · 启动后自动恢复上次没做完的工作。

用户反馈：「缺少一个功能，启动后自动恢复原有任务。原有任务让用户勾选
是下载、打标、导入还是怎么。」

三条规矩
--------
1. **默认全关**。`resume_on_start=False` 时本模块完全不做事，老用户升级
   后不会被突然自动跑起来的任务吓到。设置页一个总开关 + 三个分类勾选。
2. **只认库里还真正欠着的活**：
   - 下载：`queue.reset_stale_running()` 抓到的「上次退出时还在跑」的任务；
   - 导入/打标：`images.ai_state=0` 的行，按任务分别续跑（导入会重新
     索引并跳过已存在的图，纯打标只跑标签阶段）。
   不保存额外的断点文件，也不会重复下载已入库的图。
3. **失败不连累启动**。任何异常都吞掉并写日志 —— 恢复功能是锦上添花，
   绝不能让用户连软件都打不开。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

LOG = logging.getLogger("mrm.startup_resume")

# 同时最多恢复几个导入来源：自动恢复不该一开机就把机器占满。
MAX_IMPORT_SOURCES = 3


def _on(cfg, key: str) -> bool:
    """总开关没开 ⇒ 子开关一律不生效（避免老配置只带了子开关就误跑）。"""
    return bool(getattr(cfg, "resume_on_start", False)
                and getattr(cfg, key, False))


def _notice(db, title: str, text: str) -> None:
    try:
        from . import notify
        notify.add(db, "startup-resume", title, text[:200], link="/tasks",
                   level="info")
    except Exception:
        pass


def restore_downloads(cfg, stale_ids, queue) -> int:
    """把上次被中断的下载任务重新入队。返回入队条数。"""
    if not _on(cfg, "resume_downloads") or not stale_ids:
        return 0
    items = []
    for tid in stale_ids:
        try:
            items.append((int(tid), None))
        except (TypeError, ValueError):
            continue
    if not items:
        return 0
    try:
        queue.submit_many(items)     # submit_many 自带幂等，不会重复入队
        return len(items)
    except Exception as ex:          # noqa: BLE001
        LOG.warning("启动恢复下载任务失败：%s", ex)
        return 0


def _pending_sources(db, limit: int = MAX_IMPORT_SOURCES) -> list[dict]:
    """列出还有 ai_state=0 的导入来源（按任务最近优先）。"""
    rows = db.query(
        """SELECT t.id AS tid, t.name AS name, t.output_dir AS folder,
                  COUNT(i.id) AS total,
                  SUM(CASE WHEN i.ai_state=0 THEN 1 ELSE 0 END) AS pending
             FROM tasks t JOIN images i ON i.task_id=t.id
            WHERE i.source_url LIKE 'file://%'
            GROUP BY t.id
           HAVING pending > 0
            ORDER BY t.id DESC
            LIMIT ?""", (int(limit),))
    out = []
    for r in rows:
        folder = str(r["folder"] or "")
        try:
            exists = bool(folder) and Path(folder).is_dir()
        except OSError:
            exists = False
        if not exists:
            continue                # 外接盘拔了：恢复也没用，别假装成功
        out.append({"tid": int(r["tid"]), "name": str(r["name"] or ""),
                    "folder": folder, "pending": int(r["pending"] or 0),
                    "total": int(r["total"] or 0)})
    return out


def _run_import_source(cfg, db_path, src: dict) -> dict:
    """重跑一次导入：索引阶段会跳过已登记的图，只把待打标那批补上。"""
    from .db import Database
    from . import import_folder as imp

    if imp.is_running():
        return {"ok": False, "error": "已有导入任务在跑"}
    db = Database(db_path)
    try:
        return imp.import_folder(db, cfg, Path(src["folder"]),
                                 task_id=src["tid"], do_tag=True)
    except Exception as ex:          # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    finally:
        try:
            db.close()
        except Exception:
            pass


def _run_tag_source(cfg, db_path, src: dict) -> dict:
    """只补标签（不重新扫目录）：给「打标」单独勾选时用。"""
    from .db import Database
    from . import import_folder as imp

    if imp.is_running():
        return {"ok": False, "error": "已有导入任务在跑"}
    db = Database(db_path)
    try:
        return imp.tag_pending(db, cfg, src["tid"])
    except Exception as ex:          # noqa: BLE001
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    finally:
        try:
            db.close()
        except Exception:
            pass


def _worker(cfg, db_path: str, mode: str, sources: list[dict]) -> None:
    """后台串行恢复，不占启动时间。"""
    done = 0
    for src in sources:
        fn = _run_import_source if mode == "import" else _run_tag_source
        res = fn(cfg, db_path, src)
        if res and res.get("ok"):
            done += 1
    if done:
        try:
            from .db import Database
            db = Database(db_path)
            try:
                _notice(db, "已自动恢复未完成的工作",
                        f"自动继续了 {done} 个来源（{mode}），可在通知中心查看详情。")
            finally:
                db.close()
        except Exception:
            pass


def restore_pending_work(cfg) -> dict:
    """启动恢复导入 / 打标。返回给调用方看的简要结果（也便于测试）。"""
    want_import = _on(cfg, "resume_imports")
    want_tag = _on(cfg, "resume_tags")
    if not (want_import or want_tag):
        return {"started": False, "sources": 0}
    try:
        from .db import Database
        db = Database(cfg.db_path)
        try:
            sources = _pending_sources(db)
        finally:
            db.close()
    except Exception as ex:          # noqa: BLE001
        LOG.warning("启动恢复：读待办失败：%s", ex)
        return {"started": False, "sources": 0}
    if not sources:
        return {"started": False, "sources": 0}
    # 导入本身包含打标；两样都勾时只跑导入，避免同一个来源跑两遍。
    mode = "import" if want_import else "tag"
    try:
        threading.Thread(target=_worker,
                         args=(cfg, str(cfg.db_path), mode, sources),
                         name="mrm-startup-resume", daemon=True).start()
    except Exception as ex:          # noqa: BLE001
        LOG.warning("启动恢复线程起不来：%s", ex)
        return {"started": False, "sources": len(sources)}
    return {"started": True, "sources": len(sources), "mode": mode}


def restore_all(cfg, queue, stale_ids) -> dict:
    """统一入口：下载交给队列，导入 / 打标后台串行跑。"""
    downloads = restore_downloads(cfg, stale_ids, queue)
    work = restore_pending_work(cfg)
    return {"downloads": downloads, **work}
