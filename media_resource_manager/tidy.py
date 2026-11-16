"""任务跑完后的轻量清理（r32）。

**只清本软件自己产生的垃圾**，一共三类：

1. 0 字节 / ``.part`` 孤儿文件（下载中断留下的残片， DownLoader 是先写
   ``xxx.part`` 再 rename 的，崩了就剩孤儿）；
2. 图库里指向「文件已消失 / 0 字节」的残留记录（连带 reviews），
   否则图库会一直挂着「缺失 · path」的幽灵项；
3. 隔离区 ``_purged`` 目录（软件自己「彻底删除」时搬进去的，找回来过
   就不用再留了）。

**绝不碰系统回收站（``$Recycle.Bin``）** —— 那是整个电脑的回收站，里面
可能有用户的照片/文档/别的东西删错的东西。清它属于不可逆的越界操作。
"""
from __future__ import annotations

from pathlib import Path


def _clean_orphan_files(dirs, trash) -> int:
    """调 ``maintenance.move_orphan_files``（与「维护→清理 0kb」同一套）。

    ⚠ 原来这里 import 的是 ``tools.repair_gallery`` —— 而 ``tools/`` **不在
    打包清单里**（``build_nodel.py`` 只打 ``media_resource_manager``），
    异常又被 ``except: return 0`` 吞掉，于是**安装版里每个任务跑完的自动
    清理一直是空跑**，而且看不出来。现在走包内模块，打包版才有这个东西。
    """
    try:
        from .maintenance import move_orphan_files
        return int(move_orphan_files(list(dirs), trash).get("moved") or 0)
    except Exception:
        return 0


def _purge_dir(d: Path, keep_days: int = 0) -> int:
    """清掉一个「软件自己当垃圾收进来」的目录（只删文件，保留目录本身）。

    ``keep_days > 0`` 时**尊重回收站保留期**：只清理保留期外的文件，
    保留期内的（用户可能还要捞回去，对应 ``recycle_keep_days``）跳过。
    ``keep_days <= 0`` = 立即全清。按文件的 mtime 判龄（_purged 文件名
    不带时间戳，mtime 是唯一可靠年龄依据）。
    """
    n = 0
    if not d.is_dir():
        return 0
    import time
    cutoff = time.time() - max(0, int(keep_days or 0)) * 86400
    try:
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            if cutoff > 0:
                try:
                    if (f.stat().st_mtime or 0) > cutoff:
                        continue      # 仍在保留期内，不删
                except Exception:
                    pass
            try:
                f.unlink()
            except Exception:
                pass
            n += 1
    except Exception:
        pass
    return n


def _is_local_folder_task(t) -> bool:
    """learn_folder 建的任务：entry_urls 全是本地路径（不是 http）。

    这类任务的 ``output_dir`` 就是**用户自选的文件夹**（相册/文档等），
    0 字节清理扫它 = 越界清用户文件（R152-B4）。
    """
    urls = [str(u) for u in (getattr(t, "entry_urls", None) or [])]
    if not urls:
        return False
    return all(not u.lower().startswith(("http://", "https://"))
               for u in urls)


def clean_scan_dirs(cfg, db=None) -> list[Path]:
    """R152-B4：0 字节/.part 孤儿清理**允许扫**的目录白名单。

    · 软件自己的三个目录（library / isolate / review）—— 始终扫；
    · 下载类任务（entry_urls 是 http）的 output_dir 的**子目录**
      （engine 落盘在 ``output_dir/<站点文件夹>/``，见 engine.py:909）；
    · learn_folder 类任务（entry_urls 是本地路径）**整棵排除** ——
      它的 output_dir 就是用户自选文件夹，扫它就是清用户相册；
    · images.path 的父目录一律不扫 —— 回填/学习入库的图，父目录可能
      就在用户文件夹里。

    原来是无条件把每个任务的 output_dir 原样丢进清理列表，用户相册被
    设成 output_dir 时 0 字节文件会被当垃圾收走 —— 已按本条收窄。
    """
    dirs: list[Path] = []
    for name in ("library_dir", "isolate_dir", "review_dir"):
        d = getattr(cfg, name, None)
        if d:
            dirs.append(Path(d))
    if db is not None:
        try:
            from .repo import list_tasks
            for t in list_tasks(db):
                od = getattr(t, "output_dir", None)
                if not od or _is_local_folder_task(t):
                    continue
                base = Path(od)
                if not base.is_dir():
                    continue
                for sub in base.iterdir():
                    if sub.is_dir():
                        dirs.append(sub)
        except Exception:
            pass
    out, seen = [], set()
    for d in dirs:
        k = str(d)
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def tidy_after_task(cfg, db, log=None) -> dict:
    """任务跑完扫一遍垃圾。返回统计 dict（失败静默，绝不把 UI 拖崩）。"""
    stat = {"files": 0, "rows": 0, "purged": 0}

    # 1) 0 字节 / .part 孤儿文件（R152-B4：只扫白名单目录，见 clean_scan_dirs）
    trash = Path(getattr(cfg, "log_dir", "")) / "orphan_trash"
    dirs = clean_scan_dirs(cfg, db)
    stat["files"] = _clean_orphan_files(dirs, trash)

    # 2) 隔离区 _purged（软件自己彻底删除时搬进去的）
    #    2026-11-05：尊重回收站保留期 recycle_keep_days —— 原来是每次 task
    #    后无条件全清，用户设了「保留 30 天」却一天都留不住（僵尸配置）。
    #    现在保留期内的文件不动，只清过期的；保留期 <=0 才立即全清。
    try:
        p = Path(getattr(cfg, "isolate_dir", "")) / "_purged"
        keep = int(getattr(cfg, "recycle_keep_days", 0) or 0)
        stat["purged"] = _purge_dir(p, keep)
    except Exception:
        pass

    # 3) 图库里指向「文件没了 / 0 字节」的残留记录（连 reviews 一起删）
    #    R152-B3：去掉 in_quarantine=0 过滤 —— 隔离区/待审核的幽灵行
    #    （文件已不在盘上）以前永远清不到，pending() 一直展示已删文件。
    #    存活文件有 gone 判据挡着，不会被误删。
    #    2026-11-05 加「盘失联熔断」：可移动盘 / NAS 挂载（如 F:）整棵不可达
    #    时，Path.exists() 对该树一律 False —— 旧的判据会把整棵目录的行判成
    #    幽灵**全删**（虽然只删 DB 行不删磁盘文件，但图库视图瞬间清空，图得
    #    重新回填才回来）。现在先判该文件的最近存在祖先是否可达：不可达 =
    #    整盘失联，本轮跳过这个文件，等盘回来再清理。
    _reach_cache: dict[str, bool] = {}

    def _reachable(path: Path) -> bool:
        """该路径是否有**已存在的祖先目录**（盘在）。结果按祖先缓存。"""
        try:
            cur = path.parent
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
            if parent == cur:                      # 到根了还没命中 → 盘不在
                out = False
                break
            cur = parent
        for k in chain:
            _reach_cache[k] = out
        return out

    try:
        rows = db.query(
            "SELECT id, path FROM images WHERE path IS NOT NULL AND path<>'' "
            "AND blacklisted=0")
        for r in rows:
            p = Path(r["path"])
            if not _reachable(p):
                continue
            gone = (not p.exists()) or (p.exists() and p.stat().st_size == 0)
            if not gone:
                continue
            db.execute("DELETE FROM reviews WHERE image_id=?", (r["id"],))
            db.execute("DELETE FROM images WHERE id=?", (r["id"],))
            stat["rows"] += 1
    except Exception:
        pass

    if log and (stat["files"] or stat["rows"] or stat["purged"]):
        log(f"任务后清理：孤儿文件 {stat['files']} · 残留记录 "
            f"{stat['rows']} · 隔离回收 {stat['purged']}")
    return stat
