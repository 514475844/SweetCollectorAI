"""全面清理核心逻辑（与 GUI 解耦，便于单元测试）。

「全面清理」原来是**一句笼统二次确认 + 一个「学习进度要不要一起清」的附加问**，
用户既看不出会清掉什么、也没法只清自己想清的那部分（比如只想清日志、想留图库）。

#37 摊成**复选项清单**：勾完点确认。清什么 = 勾了什么，默认全勾业务项，
特权项（站点适配器 / AI 模型 / 学习记录 / 设置 / Cookie）默认不勾。

⚠ 「清图库」＝**真删硬盘落库图**，不走回收站开关（cfg.use_recycle 只管常规删除，
见 D9：设置一个全局开关，清理统一遵循，不在清理窗口逐项问）。
想撤销就别勾「已落库图」——这是唯一需要想清楚的一项。

永远保留：站点适配器(data/rules + exe 内置)、CLIP 模型(data/models)、
学习记录(preferences，默认保留)、设置(config.json)、登录 Cookie(cookies.json)。
"""
from __future__ import annotations

from pathlib import Path
import shutil

from .config import AppConfig, user_data_dir

# 清业务表，但保留这两张「特权」表（除非 plan 里显式勾了 prefs / rules）
CLEAR_TABLES = ("tasks", "images", "reviews", "compliance", "audit_log")
KEEP_TABLES = ("preferences", "rules")

# ---------- #37：清理项清单（GUI 直接拿它铺复选项） ----------
# (key, 显示名, 危险等级, 说明)  危险等级：normal=小事 / risky=会真删硬盘文件
CLEAN_ITEMS = (
    ("library", "已落库图（清图库）", "risky", "images 表 + 图库目录全部文件，真删硬盘"),
    ("review", "待审核图", "risky", "reviews 表 + 待审核目录全部文件，真删硬盘"),
    ("isolate", "隔离区 / 回收站", "risky", "隔离区目录全部文件"),
    ("tasks", "任务", "normal", "tasks 表（任务名 / 抓取页数 / 输出目录）"),
    ("audit", "审核与操作记录", "normal", "reviews / compliance / audit_log 表"),
    ("thumbs", "缩略图缓存", "normal", "缩略图目录，下次会重建"),
    ("backups", "备份", "normal", "备份目录"),
    ("logs", "日志", "normal", "日志目录，下次会重建"),
    ("prefs", "学习记录（偏好模型）", "risky", "preferences 表，删了 AI 要从头学"),
    ("rules", "站点适配器规则", "risky", "rules 表 + 规则目录（ exe 内置适配器不受影响）"),
    ("models", "AI 模型（CLIP）", "risky", "模型目录，删了要重新下载"),
)

# 表 → 清理项 key（一项可能对应多张表）
_ITEM_TABLES = {
    "library": ("images",),
    "review": ("reviews",),
    "tasks": ("tasks",),
    "audit": ("compliance", "audit_log"),
    "prefs": ("preferences",),
    "rules": ("rules",),
    # thumbs/isolate/backups/logs/models 只对应物理目录
}
# 清理项 key → 该删哪些目录（相对 cfg / root）
_ITEM_DIRS = {
    "library": ("cfg:library_dir",),
    "review": ("cfg:review_dir",),
    "isolate": ("cfg:isolate_dir",),
    "rules": ("cfg:rules_dir",),
    "thumbs": ("root:thumbs",),
    "backups": ("root:backups",),
    "logs": ("root:logs",),
    "models": ("root:models",),
}

# 默认勾选项 = 业务数据（学习记录默认保留，别一勾手滑把几百次点出来的偏好清了）
DEFAULT_PLAN = frozenset({
    "library", "review", "isolate", "tasks", "audit",
    "thumbs", "backups", "logs",
})


def move_dir_contents(src: str | Path, dst: str | Path) -> dict:
    """编号 14：把任务目录里的文件搬到新目录（改输出路径时用）。

    只搬**文件**，保留相对子目录结构（站点/主题词分层不能丢）。
    策略：`shutil.move`，目标已存在同名文件则跳过（绝不覆盖用户数据）——
    「改个目录就把旧图覆盖没了」是不可接受的失败模式。
    返回 {"moved": n, "skipped": n, "failed": n}
    """
    out = {"moved": 0, "skipped": 0, "failed": 0}
    try:
        s, d = Path(src), Path(dst)
    except Exception:
        return out
    if not s.exists() or not s.is_dir():
        return out
    try:
        if s.resolve() == d.resolve():
            return out
    except Exception:
        pass
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        return out
    for f in s.rglob("*"):
        if not f.is_file():
            continue
        try:
            rel = f.relative_to(s)
            target = d / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                out["skipped"] += 1
                continue
            shutil.move(str(f), str(target))
            out["moved"] += 1
        except Exception:
            out["failed"] += 1
    # 搬完后清掉源目录里的空子目录（源根保留：可能是用户指定的目录本身）
    try:
        for sub in sorted([x for x in s.rglob("*") if x.is_dir()],
                          key=lambda x: len(x.parts), reverse=True):
            try:
                if not any(sub.iterdir()):
                    sub.rmdir()
            except Exception:
                pass
    except Exception:
        pass
    return out


def clean_task_parts(cfg: AppConfig, output_dir: str | Path | None,
                     root: Path | None = None) -> dict:
    """编号 11：清掉某个任务目录下的 .part 残留与空目录。

    为什么要单独做：下载器是「先写 xxx.part 再 rename」，用户点停止/删任务时
    rename 还没发生 → 磁盘上留一堆半个文件。这些文件**不在库里**，
    平时看不见，但会一直吃盘（实测一个中断的任务能留 200MB 残渣），
    而且下次同名下载会误判「已存在」。

    安全边界：只删 `.part` / `.crdownload` 结尾的文件，**绝不碰任何图片**；
    空目录只删「删完后仍为空」的，且最多往下 3 层（防止误删用户自己的空目录树）。
    返回 {"files": 删了几个, "dirs": 删了几个空目录, "bytes": 释放字节数}
    """
    out = {"files": 0, "dirs": 0, "bytes": 0}
    try:
        base = Path(output_dir or getattr(cfg, "library_dir", "") or "")
    except Exception:
        return out
    if not str(base) or not base.exists() or not base.is_dir():
        return out
    # 防呆：绝不能是盘根或用户主目录
    try:
        if base.parent == base or len(base.parts) <= 1:
            return out
    except Exception:
        return out

    suffixes = (".part", ".crdownload", ".download")
    for f in base.rglob("*"):
        try:
            if not f.is_file() or not f.name.lower().endswith(suffixes):
                continue
            sz = f.stat().st_size
            f.unlink()
            out["files"] += 1
            out["bytes"] += int(sz or 0)
        except Exception:
            continue
    # 由深到浅删空目录（最多 3 层，避免把输出根的父级结构掀了）
    try:
        depth_base = len(base.parts)
        for d in sorted([x for x in base.rglob("*") if x.is_dir()],
                        key=lambda x: len(x.parts), reverse=True):
            if len(d.parts) - depth_base > 3:
                continue
            try:
                if not any(d.iterdir()):
                    d.rmdir()
                    out["dirs"] += 1
            except Exception:
                continue
    except Exception:
        pass
    return out


def _resolve(root: Path, cfg: AppConfig, spec: str) -> Path | None:
    if spec.startswith("cfg:"):
        v = getattr(cfg, spec[4:], None)
        return Path(v) if v else None
    return root / spec.split(":", 1)[1]


def perform_full_cleanup(db, cfg: AppConfig, root: Path | None = None,
                         drop_prefs: bool = False,
                         plan: set[str] | None = None) -> None:
    """执行清理。GUI 在调用前应先停任务 + 让用户勾完清单。

    db         : media_resource_manager.db.Database
    cfg        : AppConfig（提供 library_dir / review_dir / isolate_dir / rules_dir）
    root       : 数据根目录；默认 user_data_dir()
    drop_prefs : 兼容旧调用方（== plan 里勾了 "prefs"）
    plan       : 要清的项集合（见 CLEAN_ITEMS）。None = DEFAULT_PLAN。

    返回 None；清完不负责刷新界面（GUI 自己 refresh）。
    """
    root = Path(root) if root is not None else user_data_dir()
    p = set(DEFAULT_PLAN if plan is None else plan)
    if drop_prefs:
        p.add("prefs")

    # 1) 学习记录：先合并到全局（task_id=0），避免按旧任务 ID 残留孤儿权重。
    #    取消学习记录这一项时，清表里不含 preferences，这里的行为不变（只归并不删）。
    try:
        if "prefs" in p:
            db.execute("DELETE FROM preferences")
        else:
            db.execute("UPDATE preferences SET task_id=0 WHERE task_id<>0")
    except Exception:
        pass

    # 2) 清空业务表（按勾选项）；规则表 rules 是特权表，只有显式勾了 "rules" 才动
    # R221/C10：FK 开启后子表必须先于父表 —— reviews 引用 images，
    # failed_urls/task_pages 引用 tasks。p 是 set（迭代序不定）⇒ 固定让
    # "review" 排在 "library" 前；"tasks" 删前先清两张子表。
    # （原来先删 images 会撞 FK，异常被 except 吞掉 ⇒「ok 但没清」。）
    _ordered = (["review"] if "review" in p else []) + \
               [k for k in p if k != "review"]
    for key in _ordered:
        if key == "prefs":
            continue          # 由第 1 步统一处理（删掉 or 归并到全局）
        if key == "tasks":
            for _child in ("failed_urls", "task_pages"):
                try:
                    db.execute(f"DELETE FROM {_child}")
                except Exception:
                    pass
        if key == "library":
            # R221/C10：reviews 是 images 的子表 —— 勾「library」时级联清掉
            # reviews 再删 images，否则 FK 拦下 + except 吞掉 ⇒「ok 但没清」。
            # R67 修的是键名/SQL 映射 bug；学习样本自 R166 起在 learn_samples，
            # 不依赖 reviews 表，这里连带清不破坏「不碰学习记录」的约定。
            try:
                db.execute("DELETE FROM reviews")
            except Exception:
                pass
        for tbl in _ITEM_TABLES.get(key, ()):
            if tbl in KEEP_TABLES and key != "rules":
                continue
            try:
                db.execute(f"DELETE FROM {tbl}")
            except Exception:
                pass

    # 2.5) R152-B3：勾了「待审核图」时，images 里的待审核行/幽灵行也要清。
    #      原来是只删 reviews 表 —— review_dir 里的文件删了，images 行还在，
    #      pending() 继续展示「文件已消失」的项。两种行都清：
    #        · 物理上就在 review_dir 里的待审核行（文件随后被整目录删掉）；
    #        · 文件已经不在盘上的幽灵行（needs_review=1 的隔离区误杀抽检项）。
    if "review" in p:
        try:
            rdir = Path(getattr(cfg, "review_dir", "") or "")
            # 2026-11-05 盘失联熔断（同 tidy.py）：not pp.exists() 在整盘不可达
            # 时恒为 True，会把待审核行整批删掉。物理在 review_dir 里的行仍删
            # （那目录马上被 rmtree），只有"文件不在盘"这一支要先判盘可达。
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

            rows = db.query(
                "SELECT id, path FROM images WHERE needs_review=1") or []
            for r in rows:
                pp = Path(r["path"] or "")
                under = bool(str(rdir)) and str(pp).startswith(str(rdir))
                if under or (not pp.exists() and _reachable(r["path"] or "")):
                    db.execute("DELETE FROM reviews WHERE image_id=?", (r["id"],))
                    db.execute("DELETE FROM images WHERE id=?", (r["id"],))
        except Exception:
            pass

    # 3) 真删物理文件夹（按勾选项；重建空目录保证下次启动不报错）
    # 2026-11-05 修目录错位：engine 下载落盘在 **task.output_dir/<站点文件夹>/**
    # （engine.py:1274 `task.output_dir or cfg.library_dir`），不落在
    # cfg.library_dir。原来勾「清图库」只 rmtree library_dir ⇒ 库行删光、
    # 各任务目录里的图还在盘上占空间，图库与磁盘永久错位。这里勾了 library
    # 时把**下载类任务**的 output_dir 整棵一起删（本地文件夹任务整棵排除，
    # 见 tidy._is_local_folder_task —— 那是用户自选相册，绝不碰）。
    for key in p:
        for spec in _ITEM_DIRS.get(key, ()):
            d = _resolve(root, cfg, spec)
            if d and Path(d).exists():
                shutil.rmtree(str(d), ignore_errors=True)
    if "library" in p:
        try:
            from .repo import list_tasks
            from .tidy import _is_local_folder_task
            _lib = Path(getattr(cfg, "library_dir", "") or "")
            for t in list_tasks(db) if db is not None else []:
                od = getattr(t, "output_dir", None)
                if not od or _is_local_folder_task(t):
                    continue
                base = Path(od)
                # 已在 library_dir / isolate_dir / review_dir 里的不重复删，
                # 上面按 _ITEM_DIRS 已处理；本地相册目录上面已被排除。
                if base == _lib:
                    continue
                if base.is_dir():
                    shutil.rmtree(str(base), ignore_errors=True)
        except Exception:
            pass
    for key in ("library", "review", "isolate", "thumbs", "backups", "logs"):
        if key not in p:
            continue
        for spec in _ITEM_DIRS.get(key, ()):
            d = _resolve(root, cfg, spec)
            if d:
                Path(d).mkdir(parents=True, exist_ok=True)
