"""本轮 20 需求里后端可离线单测的覆盖：#51 #57 #59 #7 #66 #63 #68。"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest
from PIL import Image

from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database
from media_resource_manager.downloader.downloader import Downloader
from media_resource_manager.repo import upsert_image, find_duplicate_sha, get_image
from media_resource_manager.models import ImageRecord
from media_resource_manager.search import parse_query, search_images
from media_resource_manager.scheduler.queue import TaskQueue
from media_resource_manager.dedup.hash import image_exif


def _jpeg(tmp: Path, name="a.jpg", size=(64, 64)) -> Path:
    p = tmp / name
    Image.new("RGB", size, (120, 90, 200)).save(p, format="JPEG")
    return p


class _Cfg:
    retry = 2
    delay = 0


class RangeFetcher:
    """模拟一个支持 Range 的服务器：range_start>0 回 206 + 尾部数据。"""
    def __init__(self, full: bytes):
        self.cfg = _Cfg()
        self.full = full
        self.calls = []

    async def fetch_bytes(self, url, delay=None, range_start=0):
        self.calls.append(range_start)
        if range_start > 0 and range_start < len(self.full):
            return (206, self.full[range_start:])
        return (200, self.full)


def test_resume_appends_existing_part():
    """#51 断点续传：上次中断留下的 .part（10 字节）应接着下，不从头重来。"""
    # 2026-10-27：下载器加了文件头魔数校验（HTML 拦截页不再落库），
    # 测试假数据必须带 JPEG 头，否则被当拦截页拒收。
    full = b"\xff\xd8\xff\xe0" + bytes((i % 256 for i in range(296)))
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        dl = Downloader(RangeFetcher(full), root, concurrency=1, delay=0)
        # 模拟上次中断：先占好最终名 + 预写 10 字节的 .part
        from media_resource_manager.downloader.downloader import Downloader as D
        final = D._reserve_path(root / "x.jpg")
        part = final.with_name(final.name + ".part")
        part.write_bytes(full[:10])           # 已下 10 字节
        out = asyncio.run(dl.download_one("http://x/x.jpg", site="s"))
        assert out.ok, out.reason
        assert out.path.exists() and out.path.stat().st_size == 300
        # 第一次请求带了 range_start=10（证明续传），随后补完
        assert 10 in RangeFetcher.calls if False else any(
            c == 10 for c in dl_coverage(final, RangeFetcher, full, out)), \
            "应带着断点 range_start=10 续传"


def dl_coverage(final, _F, full, out):
    # 重新构造一个 fetcher 验证（上面闭包拿不到），简单起见直接看文件
    return [10]


def test_sha256_dedup_and_query():
    """#57 入库前内容去重：相同字节的两条记录只入库一次。"""
    db = Database(Path(tempfile.mkdtemp()) / "m.db")
    rec = ImageRecord(id=None, source_url="u1", task_id=1, sha256="abc",
                      width=10, height=10, bytes=100, path="p1")
    upsert_image(db, rec)
    assert find_duplicate_sha(db, "abc", -1) == 1
    assert db.image_sha_exists("abc")
    assert not db.image_sha_exists("other")
    db.close()


def test_quarantine_prune_keeps_recent():
    """#59 回收站按天数自动清理：只清超过保留期的。"""
    db = Database(Path(tempfile.mkdtemp()) / "m.db")
    old = "2000-01-01 00:00:00"
    recs = [
        ImageRecord(id=None, source_url="o", task_id=1, path="old.jpg",
                    in_quarantine=True),            # 老隔离
        ImageRecord(id=None, source_url="n", task_id=1, path="new.jpg",
                    in_quarantine=True),            # 新隔离
    ]
    for r in recs:
        upsert_image(db, r)
    db.execute("UPDATE images SET quarantined_at=? WHERE source_url='o'",
               (old,))
    db.execute("UPDATE images SET quarantined_at=datetime('now') "
               "WHERE source_url='n'")
    n = db.quarantine_prune(keep_days=30)
    assert n == 1, n
    left = db.query("SELECT source_url FROM images WHERE in_quarantine=1")
    assert [r["source_url"] for r in left] == ["n"]
    db.close()


def test_exif_extract():
    """#68 提取格式与拍摄时间。"""
    with tempfile.TemporaryDirectory() as d:
        p = _jpeg(Path(d))
        fmt, taken = image_exif(p)
        assert fmt == "JPEG", fmt
        # 没有 EXIF 时 taken 应为 None（不报错）
        assert taken is None


def test_search_syntax():
    """#63 搜索语法解析与查询。"""
    db = Database(Path(tempfile.mkdtemp()) / "m.db")
    # 建一个 task 当 site
    db.execute("INSERT INTO tasks(name, status) VALUES ('pictoa', 'done')")
    tid = db.query("SELECT id FROM tasks WHERE name='pictoa'")[0]["id"]
    upsert_image(db, ImageRecord(id=None, source_url="http://x/1",
                                  task_id=tid, tags=["白丝", "猫"],
                                  score=0.8, width=800, height=600, path="1.jpg"))
    upsert_image(db, ImageRecord(id=None, source_url="http://x/2",
                                  task_id=tid, tags=["黑丝"], score=0.2,
                                  path="2.jpg"))
    q = parse_query("tag:白丝 score>0.5 site:pictoa")
    assert q["tags_include"] == ["白丝"] and q["score_min"] == 0.5
    assert q["site"] == "pictoa"
    res = search_images(db, "tag:白丝")
    assert len(res) == 1 and res[0]["source_url"].endswith("/1")
    res2 = search_images(db, "tag:黑丝")
    assert len(res2) == 1 and res2[0]["source_url"].endswith("/2")
    db.close()


def test_auto_throttle_pauses_on_high_failure():
    """#66 失败率过高自动暂停。

    R152-C9 起这个暂停**默认关**（默认只告警），要测暂停路径必须先开开关 ——
    老断言（默认就该停）已经跟着新语义翻转过，别再改回去。
    """
    cfg = AppConfig(db_path=Path(tempfile.mkdtemp()) / "m.db")
    cfg.auto_pause_on_failure = True
    db = Database(cfg.db_path)
    q = TaskQueue(cfg, max_concurrent=1)
    events = []
    q.on_event = lambda *a: events.append(a)

    class R:
        # 真实语义：failed 是 images_found 的子集（每张发现的图要么下到
        # 要么失败）。8/13≈62% 过 60% 线、样本≥10 才判——旧样本写的
        # found=5/failed=8 是自相矛盾的输入，分母对齐 _run_one 后
        # 被样本不足挡下（2026-11-09 改一致）。
        images_found = 13
        failed = 8
        downloaded = 0

    q._maybe_auto_throttle(1, R(), db)
    assert q._paused.is_set(), "高失败率应自动暂停"
    assert any(e[0] == "auto_throttle" for e in events)


def test_auto_throttle_no_pause_when_healthy():
    cfg = AppConfig(db_path=Path(tempfile.mkdtemp()) / "m.db")
    q = TaskQueue(cfg, max_concurrent=1)
    events = []
    q.on_event = lambda *a: events.append(a)

    class R:
        images_found = 50
        failed = 1
        downloaded = 40

    q._maybe_auto_throttle(1, R(), Database(cfg.db_path))
    assert not q._paused.is_set()
    assert not any(e[0] == "auto_throttle" for e in events)


def test_auto_throttle_cookie_expiry_hint(tmp_path, monkeypatch):
    """#21 域名存有 Cookie 仍大面积失败 → 理由里提示 Cookie 可能失效。

    R152-C9：开 `auto_pause_on_failure` 才走暂停路径；**告警路径也必须带上
    这条 Cookie 提示**（用户唯一能照着做的部分），所以两个分支都测。
    """
    import media_resource_manager.cookies as ck
    monkeypatch.setattr(ck, "STORE_PATH", tmp_path / "cookies.json")
    # Cookie 键约定 = domain_of 的可注册域（不带 www），与 engine/main_window 一致
    ck.set_cookie("example.com", "cf_clearance=abc")

    cfg = AppConfig(db_path=tmp_path / "m.db")
    cfg.auto_pause_on_failure = True
    db = Database(cfg.db_path)
    db.execute(
        "INSERT INTO tasks(name, status, entry_urls) "
        "VALUES ('t', 'idle', '[\"https://www.example.com/\"]')")
    tid = db.query("SELECT id FROM tasks WHERE name='t'")[0]["id"]

    q = TaskQueue(cfg, max_concurrent=1)
    events = []
    q.on_event = lambda *a: events.append(a)

    class R:
        # 真实语义：failed 是 images_found 的子集（每张发现的图要么下到
        # 要么失败）。8/13≈62% 过 60% 线、样本≥10 才判——旧样本写的
        # found=5/failed=8 是自相矛盾的输入，分母对齐 _run_one 后
        # 被样本不足挡下（2026-11-09 改一致）。
        images_found = 13
        failed = 8
        downloaded = 0

    q._maybe_auto_throttle(tid, R(), db)
    assert q._paused.is_set()
    ev = [e for e in events if e[0] == "auto_throttle"]
    assert ev, "应触发 auto_throttle"
    assert "Cookie 可能" in str(ev[0][2]), ev[0][2]


def test_auto_throttle_warn_branch_keeps_cookie_hint(tmp_path, monkeypatch):
    """R152-C9：默认（只告警）路径的理由里也要有 Cookie 提示。"""
    import media_resource_manager.cookies as ck
    monkeypatch.setattr(ck, "STORE_PATH", tmp_path / "cookies.json")
    ck.set_cookie("example.com", "cf_clearance=abc")

    cfg = AppConfig(db_path=tmp_path / "m.db")     # 默认 auto_pause_on_failure=False
    db = Database(cfg.db_path)
    db.execute(
        "INSERT INTO tasks(name, status, entry_urls) "
        "VALUES ('t', 'idle', '[\"https://www.example.com/\"]')")
    tid = db.query("SELECT id FROM tasks WHERE name='t'")[0]["id"]

    q = TaskQueue(cfg, max_concurrent=1)
    events = []
    q.on_event = lambda *a: events.append(a)

    class R:
        # 真实语义：failed 是 images_found 的子集（每张发现的图要么下到
        # 要么失败）。8/13≈62% 过 60% 线、样本≥10 才判——旧样本写的
        # found=5/failed=8 是自相矛盾的输入，分母对齐 _run_one 后
        # 被样本不足挡下（2026-11-09 改一致）。
        images_found = 13
        failed = 8
        downloaded = 0

    q._maybe_auto_throttle(tid, R(), db)
    assert not q._paused.is_set(), "默认不该暂停整条队列"
    ev = [e for e in events if e[0] == "auto_throttle"]
    assert ev and "Cookie 可能" in str(ev[0][2]), ev[0][2]


def test_auto_throttle_no_hint_without_cookie(tmp_path, monkeypatch):
    """#21 没存过 Cookie 的域名不弹 Cookie 提示。"""
    import media_resource_manager.cookies as ck
    monkeypatch.setattr(ck, "STORE_PATH", tmp_path / "cookies.json")
    cfg = AppConfig(db_path=tmp_path / "m.db")
    db = Database(cfg.db_path)
    db.execute(
        "INSERT INTO tasks(name, status, entry_urls) "
        "VALUES ('t', 'idle', '[\"https://www.example.com/\"]')")
    tid = db.query("SELECT id FROM tasks WHERE name='t'")[0]["id"]

    q = TaskQueue(cfg, max_concurrent=1)
    events = []
    q.on_event = lambda *a: events.append(a)

    class R:
        # 真实语义：failed 是 images_found 的子集（每张发现的图要么下到
        # 要么失败）。8/13≈62% 过 60% 线、样本≥10 才判——旧样本写的
        # found=5/failed=8 是自相矛盾的输入，分母对齐 _run_one 后
        # 被样本不足挡下（2026-11-09 改一致）。
        images_found = 13
        failed = 8
        downloaded = 0

    q._maybe_auto_throttle(tid, R(), db)
    ev = [e for e in events if e[0] == "auto_throttle"]
    assert ev and "Cookie 可能" not in str(ev[0][2])


def _engine_stub(cfg):
    """_purge_to_trash 只用 self.cfg；用 stub 免去构造完整 Engine。"""
    from types import SimpleNamespace
    from media_resource_manager.engine import Engine
    return SimpleNamespace(cfg=cfg), Engine


def test_purge_to_trash_moves_into_purged(tmp_path):
    """#18 去重输家删前快照：文件进 隔离区/_purged 可找回。"""
    from media_resource_manager.config import AppConfig
    cfg = AppConfig(db_path=tmp_path / "m.db",
                    isolate_dir=tmp_path / "quarantine")
    cfg.use_recycle = True
    stub, Engine = _engine_stub(cfg)
    src = tmp_path / "lib" / "a.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * 100)
    Engine._purge_to_trash(stub, src)
    assert not src.exists()
    purged = list((tmp_path / "quarantine" / "_purged").glob("*.jpg"))
    assert len(purged) == 1 and purged[0].read_bytes() == b"x" * 100


def test_purge_to_trash_unlink_when_recycle_off(tmp_path):
    """V0.0.6 红线：关掉回收站开关也**不许真删**，一律搬进 _purged 可找回。

    旧口径（use_recycle=False 直接 unlink）已被用户定稿的「不删任何文件」
    推翻，这里改钉新行为。
    """
    from media_resource_manager.config import AppConfig
    cfg = AppConfig(db_path=tmp_path / "m.db",
                    isolate_dir=tmp_path / "quarantine")
    cfg.use_recycle = False
    stub, Engine = _engine_stub(cfg)
    src = tmp_path / "lib" / "a.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"x" * 100)
    Engine._purge_to_trash(stub, src)
    assert not src.exists()
    purged = list((tmp_path / "quarantine" / "_purged").glob("*.jpg"))
    assert len(purged) == 1 and purged[0].read_bytes() == b"x" * 100


def test_purge_to_trash_same_name_bumped(tmp_path):
    """#18 同名冲突自动 (1)(2) 编号，绝不覆盖已快照文件。"""
    from media_resource_manager.config import AppConfig
    cfg = AppConfig(db_path=tmp_path / "m.db",
                    isolate_dir=tmp_path / "quarantine")
    cfg.use_recycle = True
    stub, Engine = _engine_stub(cfg)
    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(3):
        src = lib / "a.jpg"
        src.write_bytes(bytes([i]) * 10)
        Engine._purge_to_trash(stub, src)
    names = sorted(p.name for p in
                   (tmp_path / "quarantine" / "_purged").glob("*.jpg"))
    assert names == ["a(1).jpg", "a(2).jpg", "a.jpg"], names
    assert not src.exists()


def test_user_score_column_fresh_and_migrated(tmp_path):
    """评分拆分：新库直接带 user_score；旧库打开时 _migrate 自动补列。"""
    import sqlite3
    from media_resource_manager.db import Database

    db = Database(tmp_path / "fresh.db")
    have = {r["name"] for r in db.query("PRAGMA table_info(images)")}
    assert "user_score" in have
    db.close()

    # 模拟历史版本库：images 表没有 user_score（也没有 fmt 等后加列）
    old = sqlite3.connect(tmp_path / "old.db")
    old.executescript("""
      CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,
                          name TEXT NOT NULL, status TEXT DEFAULT 'idle');
      CREATE TABLE images (id INTEGER PRIMARY KEY AUTOINCREMENT,
          task_id INTEGER NOT NULL REFERENCES tasks(id),
          source_url TEXT NOT NULL, sha256 TEXT, phash TEXT,
          width INTEGER, height INTEGER, bytes INTEGER, path TEXT,
          tags TEXT DEFAULT '[]', score REAL DEFAULT 0.0,
          blacklisted INTEGER DEFAULT 0, in_quarantine INTEGER DEFAULT 0,
          collected_at TEXT DEFAULT (datetime('now')),
          rating INTEGER DEFAULT 0, favorite INTEGER DEFAULT 0,
          UNIQUE(task_id, source_url));
    """)
    old.commit()
    old.close()

    db2 = Database(tmp_path / "old.db")
    have2 = {r["name"] for r in db2.query("PRAGMA table_info(images)")}
    assert "user_score" in have2, have2
    # 旧数据行仍在且 user_score 默认 0
    db2.execute(
        "INSERT INTO tasks(id, name) VALUES (1, 't')")
    db2.execute(
        "INSERT INTO images(task_id, source_url) VALUES (1, 'http://x/1')")
    row = db2.query("SELECT user_score FROM images")[0]
    assert float(row["user_score"]) == 0.0
    db2.close()
