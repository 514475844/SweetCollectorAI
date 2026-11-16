"""Scale-safe storage layout (百万级 / 跨平台前置).

问题
----
单站点扁平目录 ``{site}/`` 一旦装进几十万～上百万文件，文件系统枚举与随机
查找就会慢到不可用；而且现有的图库每次刷新都 ``SELECT COUNT(*)`` 全表扫
（见 ``main_window.py`` 的 stats 查询），百万行时 UI 直接卡死。

方案
----
按图片**源 URL** 的稳定哈希做两级分桶（URL 是现有去重键，下载前就已知，
无需等内容 sha）：

    {root}/{site}/{hh}/{hh2}/{safe_filename}

``hh``/``hh2`` 取 ``sha256(url)`` 前 4 个 hex → 65536 个桶，即便 1M 张图平均
每桶也只有 ~15 个文件。确定性好：重下载、DB ``path`` 都一致；一次性迁移脚本可
把既有扁平文件重新归桶而不丢图库。

本模块对纯函数保持 DB 无关；迁移辅助函数接收 duck-typed 的 DB（任何提供
``query`` / ``execute`` 的对象）。后续接 Postgres 时只要 DB 对象满足同一接口即可。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from .downloader.naming import sanitize


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def bucket_rel(site: str, url: str, filename: str) -> Path:
    """返回相对 root 的分桶路径 ``{site}/{hh}/{hh2}/{safe}``。"""
    h = _url_hash(url)
    safe = sanitize(filename) or "untitled"
    return Path(sanitize(site or "site")) / h[:2] / h[2:4] / safe


def bucketed_dest(root: Path, site: str, url: str, filename: str) -> Path:
    return (Path(root) / bucket_rel(site, url, filename)).resolve()


def resolve_existing(root: Path, site: str, url: str, filename: str) -> Path | None:
    """在磁盘上找图：优先分桶布局，回退到旧扁平 ``{site}/{filename}``，
    让未迁移的安装也能正常显示图库。"""
    bucketed = bucketed_dest(root, site, url, filename)
    if bucketed.exists():
        return bucketed
    legacy = (Path(root) / sanitize(site or "site") / sanitize(filename)).resolve()
    if legacy.exists():
        return legacy
    return None


def save_atomic(data: bytes, dest: Path) -> Path:
    """把 ``data`` 原子写入 ``dest``（临时文件 + os.replace）。父目录自动创建。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".part_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def migrate_flat_to_bucketed(db, root: Path, *, limit: int | None = None) -> int:
    """一次性：把既有扁平文件搬进分桶并更新 DB ``path``。

    ``db`` 必须提供 ``query(sql, params=...)``（返回 dict 行）与
    ``execute(sql, params=...)``。返回迁移成功的行数。
    """
    root = Path(root)
    rows = db.query(
        "SELECT id, path, source_url, site, filename FROM images "
        "WHERE path IS NOT NULL"
    )
    moved = 0
    for r in rows:
        old = Path(r["path"])
        if not old.exists():
            continue
        url = r.get("source_url") or ""
        if not url:
            continue  # 无 URL 没法稳定分桶，留作扁平（resolve_existing 可找回）
        new = bucketed_dest(root, r.get("site") or "site", url, old.name)
        if new == old:
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
        except OSError:
            continue
        db.execute("UPDATE images SET path=? WHERE id=?", (str(new), r["id"]))
        moved += 1
        if limit and moved >= limit:
            break
    return moved
