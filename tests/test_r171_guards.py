"""R171 护栏：R154-F9「页面级断点续采」（执行方笔记本棒）。

行为断言优先、源码判据兜底；变异自检 .workbuddy/_r171_mutate.py。

覆盖（与 R154-F9 订单逐条对齐）：
- DB 五方法：seen（pending 不降级）/ result（done/error/blocked upsert
  不堆行）/ resume 种子 / stats（"已抓 x/y"口径）/ clear；
- Discovery 续跑语义：done 跳过（不 fetch）、pending 带**原深度**重试、
  error/blocked 也重试、on_discover 深度口径（子链 depth+1、翻页同深度）、
  续跑种子不重复回调、入口已 done 不重爬、超深 pending 不再爬；
- engine 真跑两轮：第一轮落账（done+pending），第二轮**只抓没抓完的**
  （fetch 计数证明 done 页一次都没再 fetch）、rep.pages_resumed 记账、
  error/blocked 页第二轮重试成功、账本读失败退化成全量重爬；
- webui 双 API 带 pages_done/pages_total；删除任务连带清账本；
- gui _pages_brief 纯函数口径（模块级，R141 教训）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager.crawler.adapter import AdapterRunner
from media_resource_manager.crawler.discovery import Discovery
from media_resource_manager.crawler.rules import AdapterRule
from media_resource_manager.downloader.downloader import DownloadOutcome
from media_resource_manager.models import TaskConfig
from media_resource_manager.repo import save_task


# ==================== 共用小工具 ====================

PAGES = {
    "https://site.com/": '<a class="album" href="/a1">A1</a>',
    "https://site.com/a1": ('<a class="photo" href="/a1/p1">p1</a>'
                            '<a class="photo" href="/a1/p2">p2</a>'),
    "https://site.com/a1/p1": '<img src="/img/1.jpg">',
    "https://site.com/a1/p2": '<img src="/img/2.jpg">',
}

# 翻页版：/a1 有 a.next → /a1?p=2（同深度续页），p2 又有新图 /a1/p3
PAGES_PAGED = dict(PAGES)
PAGES_PAGED["https://site.com/a1"] = (
    '<a class="photo" href="/a1/p1">p1</a>'
    '<a class="next" href="/a1?p=2">n</a>')
PAGES_PAGED["https://site.com/a1?p=2"] = (
    '<a class="photo" href="/a1/p3">p3</a>')

RULE = AdapterRule(
    name="site", base_url="https://site.com",
    allowed_domains=["site.com"],
    list_page_selectors={"albums": {"kind": "css", "expr": "a.album", "attr": "href"}},
    album_link_selectors={"photos": {"kind": "css", "expr": "a.photo", "attr": "href"}},
    image_link_selectors=[{"kind": "css", "expr": "img", "attr": "src"}],
    next_page_selectors={"next": {"kind": "css", "expr": "a.next", "attr": "href"}},
    max_depth=3,
)


def _write_rule(rules_dir: Path):
    rule = {
        "name": "site", "base_url": "https://site.com",
        "allowed_domains": ["site.com"],
        "list_page_selectors": {"albums": {"kind": "css", "expr": "a.album", "attr": "href"}},
        "album_link_selectors": {"photos": {"kind": "css", "expr": "a.photo", "attr": "href"}},
        "image_link_selectors": [{"kind": "css", "expr": "img", "attr": "src"}],
        "next_page_selectors": {"next": {"kind": "css", "expr": "a.next", "attr": "href"}},
        "max_depth": 3,
    }
    (rules_dir / "site.json").write_text(json.dumps(rule), encoding="utf-8")


def _mk_task(tmp_config, tmp_db, **kw) -> int:
    kw.setdefault("adapter_name", "site")
    kw.setdefault("entry_urls", ["https://site.com/"])
    t = TaskConfig(id=None, name="f9",
                   output_dir=str(tmp_config.library_dir), **kw)
    return save_task(tmp_db, t)


def _authorize(tmp_db, site="site.com"):
    tmp_db.execute(
        "INSERT INTO compliance(site, robots_ok, basis) VALUES (?,?,?)",
        (site, 1, "fixture test"))


def _seed_tasks(tmp_db, *tids):
    """R221/C10：FK 开启后 task_pages/failed_urls 的父行必须先存在。

    下面几个用例测的是页面账本/简报本身，与 tasks 表无关，但外键不管这个 ——
    父行不铺，INSERT 直接 IntegrityError（R220 r38「手搓对象缺字段」同族：
    产品没坏，是测试基建要跟着补）。
    """
    for tid in tids:
        tmp_db.execute("INSERT INTO tasks(id,name) VALUES (?,?)",
                       (tid, f"t{tid}"))


class _CountingFetcher:
    """假 fetcher：只服务 fixture 页，并逐 URL 计数 fetch_text 调用。

    Discovery 只调用 fetch_text / close，鸭子类型足够。第二轮到底有没有
    重新抓某个页，靠这个账本说话（不靠猜）。
    error_urls：这些 URL 抛通用异常（→ 页面 error 记账）；
    block_urls：这些 URL 抛 AccessBlocked（→ 页面 blocked 记账）。
    """

    def __init__(self, pages, error_urls=(), block_urls=()):
        from media_resource_manager.crawler.fetcher import (
            RobotDisallowed, AccessBlocked)
        self._pages = dict(pages)
        self._error_urls = set(error_urls)
        self._block_urls = set(block_urls)
        self.calls: list[str] = []
        self._RobotDisallowed = RobotDisallowed
        self._AccessBlocked = AccessBlocked

    async def fetch_text(self, url, delay=None):
        self.calls.append(url)
        if url in self._block_urls:
            raise self._AccessBlocked(f"blocked {url}")
        if url in self._error_urls:
            raise ValueError(f"boom {url}")
        if url not in self._pages:
            raise self._RobotDisallowed(f"no fixture for {url}")
        return self._pages[url]

    async def close(self):
        pass


class _TinyDL:
    """假下载器：每个 URL 落一个不同内容的小文件（ok=True）。

    只为让 engine 的 _process_image 走完整管线（sha256/尺寸/入库），
    本棒断言不依赖下载结果，只依赖页面账本与 fetch 计数。
    """

    def __init__(self, libdir):
        self.libdir = Path(libdir)
        self.calls: list[str] = []

    async def download_one(self, url, *, site="site", album="", date="",
                           keyword="", meta=None, already=None):
        self.calls.append(url)
        p = self.libdir / "tiny" / (
            hashlib.sha1(url.encode()).hexdigest()[:12] + ".jpg")
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
        data = p.read_bytes()
        return DownloadOutcome(
            url=url, path=p, ok=True,
            sha256=hashlib.sha256(data).hexdigest(),
            width=10, height=10, bytes=len(data))


def _engine(tmp_config, tmp_db, pages, monkeypatch, **fetcher_kw):
    """真 Engine（真库真发现）+ 计数 fetcher + 假下载器。"""
    from media_resource_manager.engine import Engine
    f = _CountingFetcher(pages, **fetcher_kw)
    eng = Engine(tmp_config, tmp_db,
                 fetcher_factory=lambda cfg, proxy: f)
    monkeypatch.setattr(eng, "_make_downloader",
                        lambda task, fetcher: _TinyDL(tmp_config.library_dir))
    return eng, f


# ==================== 1. DB 五方法 ====================

def test_db_record_page_seen_pending_and_empty_ignored(tmp_db):
    _seed_tasks(tmp_db, 1)
    tmp_db.record_page_seen(1, "https://s.com/a", 2)
    tmp_db.record_page_seen(1, "   ", 0)
    m = tmp_db.task_pages_resume(1)
    assert m == {"https://s.com/a": ("pending", 2)}, m


def test_db_record_page_seen_never_downgrades_done(tmp_db):
    """抓过的页被"重新发现"时不许降级回 pending（否则下轮又重抓）。

    深度同理不覆盖：以**抓取时**记的深度为准（seen 只给新 URL 补行）。
    """
    _seed_tasks(tmp_db, 1)
    tmp_db.record_page_result(1, "https://s.com/a", "done", images=3, depth=2)
    tmp_db.record_page_seen(1, "https://s.com/a", 1)
    m = tmp_db.task_pages_resume(1)
    assert m["https://s.com/a"] == ("done", 2), m


def test_db_record_page_result_upsert_no_dup_rows(tmp_db):
    _seed_tasks(tmp_db, 1)
    tmp_db.record_page_seen(1, "https://s.com/a", 1)
    tmp_db.record_page_result(1, "https://s.com/a", "done", images=2)
    tmp_db.record_page_result(1, "https://s.com/a", "error", error="boom")
    rows = tmp_db.query("SELECT * FROM task_pages WHERE task_id=1")
    assert len(rows) == 1, f"堆行了：{[dict(r) for r in rows]}"
    r = rows[0]
    assert r["status"] == "error" and r["error"] == "boom"
    assert int(r["images"]) == 0, "error 落账时 images 该被刷新"
    # 行不存在时直接补建（断电/清账后重抓同一页）
    tmp_db.record_page_result(1, "https://s.com/b", "done", images=5, depth=2)
    m = tmp_db.task_pages_resume(1)
    assert m["https://s.com/b"] == ("done", 2), m


def test_db_record_page_result_rejects_unknown_status(tmp_db):
    tmp_db.record_page_result(1, "https://s.com/a", "bogus")
    assert tmp_db.task_pages_resume(1) == {}
    tmp_db.record_page_result(1, "", "done")
    assert tmp_db.task_pages_resume(1) == {}


def test_db_task_page_stats_counts_and_isolation(tmp_db):
    _seed_tasks(tmp_db, 1, 2)
    tmp_db.record_page_result(1, "https://s.com/a", "done")
    tmp_db.record_page_result(1, "https://s.com/b", "done")
    tmp_db.record_page_seen(1, "https://s.com/c", 1)
    tmp_db.record_page_result(1, "https://s.com/d", "error", error="x")
    tmp_db.record_page_result(1, "https://s.com/e", "blocked")
    tmp_db.record_page_result(2, "https://s.com/a", "done")
    st = tmp_db.task_page_stats(1)
    assert st == {"done": 2, "pending": 1, "error": 1, "blocked": 1,
                  "total": 5}, st
    assert tmp_db.task_page_stats(2)["total"] == 1
    assert tmp_db.task_page_stats(99)["total"] == 0


def test_db_clear_task_pages_only_target_task(tmp_db):
    _seed_tasks(tmp_db, 1, 2)
    tmp_db.record_page_result(1, "https://s.com/a", "done")
    tmp_db.record_page_result(2, "https://s.com/b", "done")
    n = tmp_db.clear_task_pages(1)
    assert n == 1
    assert tmp_db.task_pages_resume(1) == {}
    assert len(tmp_db.task_pages_resume(2)) == 1


def test_db_schema_task_pages_unique(tmp_db):
    """UNIQUE(task_id,url)：同页跨运行只一行（账本的脊梁）。"""
    cols = {r["name"] for r in
            tmp_db.query("PRAGMA table_info(task_pages)")}
    for c in ("task_id", "url", "status", "depth", "images", "error"):
        assert c in cols, f"task_pages 缺列 {c}"
    idx = tmp_db.query(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='task_pages'")
    sqls = " ".join(r["name"] for r in idx)
    assert "sqlite_autoindex" in sqls or idx, sqls


# ==================== 2. Discovery 续跑语义 ====================

def test_discovery_resume_skips_done_retries_rest():
    f = _CountingFetcher(PAGES)
    results = []

    disc = Discovery(f, AdapterRunner(RULE), base_domain="site.com",
                     max_depth=3, on_page=results.append,
                     known_pages={
                         "https://site.com/": ("done", 0),
                         "https://site.com/a1": ("pending", 1),
                         "https://site.com/a1/p1": ("error", 2),
                     })
    asyncio.run(disc.run(["https://site.com/"]))
    urls = [r.url for r in results]
    # done 的页：跳过，一次都不 fetch
    assert "https://site.com/" not in urls
    assert f.calls.count("https://site.com/") == 0
    # pending / error：带着原深度重试
    assert "https://site.com/a1" in urls
    assert f.calls.count("https://site.com/a1") == 1
    assert "https://site.com/a1/p1" in urls
    # 断点账：跳过 1 个已抓页
    assert disc.skipped_done == 1
    # 深度保真：pending 的 /a1 仍在深度 1 抓
    by = {r.url: r for r in results}
    assert by["https://site.com/a1"].depth == 1
    assert by["https://site.com/a1/p1"].depth == 2


def test_discovery_entry_done_not_recrawled():
    """入口页上轮已抓过 → 本轮跳过（它的子页靠账本里的 pending 续命）。"""
    f = _CountingFetcher(PAGES)
    results = []
    disc = Discovery(f, AdapterRunner(RULE), base_domain="site.com",
                     max_depth=3, on_page=results.append,
                     known_pages={"https://site.com/": ("done", 0)})
    asyncio.run(disc.run(["https://site.com/"]))
    assert f.calls == [], f"入口已 done 还被重抓了：{f.calls}"
    assert disc.skipped_done == 1


def test_discovery_on_discover_depths_and_seed_silence():
    """on_discover：子链 depth+1、翻页同深度；续跑种子不重复回调。"""
    # 第一幕：全新爬一遍 —— 深度口径
    f1 = _CountingFetcher(PAGES_PAGED)
    seen1: list[tuple[str, int]] = []
    disc = Discovery(f1, AdapterRunner(RULE), base_domain="site.com",
                     max_depth=3,
                     on_discover=lambda link, d: seen1.append((link, d)))
    asyncio.run(disc.run(["https://site.com/"]))
    got = dict(seen1)
    assert got.get("https://site.com/a1") == 1, seen1
    assert got.get("https://site.com/a1?p=2") == 1, seen1   # 翻页同深度
    assert got.get("https://site.com/a1/p1") == 2, seen1
    assert got.get("https://site.com/a1/p3") == 2, seen1
    assert len(seen1) == len(set(seen1))

    # 第二幕：带种子续跑 —— 种子不重复回调，这轮新发现的子页照常回调
    f2 = _CountingFetcher(PAGES_PAGED)
    seen2: list[tuple[str, int]] = []
    disc2 = Discovery(f2, AdapterRunner(RULE), base_domain="site.com",
                      max_depth=3,
                      on_discover=lambda link, d: seen2.append((link, d)),
                      known_pages={
                          "https://site.com/": ("done", 0),
                          "https://site.com/a1": ("pending", 1),
                          "https://site.com/a1?p=2": ("pending", 1),
                      })
    asyncio.run(disc2.run(["https://site.com/"]))
    urls2 = [u for u, _ in seen2]
    for seeded in ("https://site.com/", "https://site.com/a1",
                   "https://site.com/a1?p=2"):
        assert seeded not in urls2, f"种子 {seeded} 被重复记账了"
    assert "https://site.com/a1/p1" in urls2   # /a1 这轮抓到的子页
    assert "https://site.com/a1/p3" in urls2   # /a1?p=2 这轮抓到的子页


def test_discovery_no_known_pages_full_crawl():
    """回归：不传账本 = 老行为（全量重爬），一行不改旧语义。"""
    f = _CountingFetcher(PAGES)
    results = []
    disc = Discovery(f, AdapterRunner(RULE), base_domain="site.com",
                     max_depth=3, on_page=results.append)
    asyncio.run(disc.run(["https://site.com/"]))
    assert disc.skipped_done == 0
    assert len({r.url for r in results}) == 4
    assert f.calls.count("https://site.com/") == 1


def test_discovery_resume_pending_beyond_max_depth_skipped():
    """账本里深度超限的 pending 页不再爬（任务把深度调小后生效）。"""
    f = _CountingFetcher(PAGES)
    results = []
    disc = Discovery(f, AdapterRunner(RULE), base_domain="site.com",
                     max_depth=1, on_page=results.append,
                     known_pages={"https://site.com/": ("done", 0),
                                  "https://site.com/a1/p1": ("pending", 2)})
    asyncio.run(disc.run(["https://site.com/"]))
    assert [r.url for r in results] == [], [r.url for r in results]
    assert disc.skipped_done == 1


def test_discovery_blocked_retried_next_run():
    """blocked 不是终态：下轮带着原深度重试（robots 状态可能变）。"""
    f = _CountingFetcher(PAGES, block_urls={"https://site.com/a1/p2"})
    results = []
    disc = Discovery(f, AdapterRunner(RULE), base_domain="site.com",
                     max_depth=3, on_page=results.append)
    asyncio.run(disc.run(["https://site.com/"]))
    by = {r.url: r for r in results}
    assert by["https://site.com/a1/p2"].error == "blocked"
    # 第二轮：block 解除，重试
    f2 = _CountingFetcher(PAGES)
    results2 = []
    disc2 = Discovery(f2, AdapterRunner(RULE), base_domain="site.com",
                      max_depth=3, on_page=results2.append,
                      known_pages={"https://site.com/a1/p2": ("blocked", 2)})
    asyncio.run(disc2.run(["https://site.com/"]))
    assert "https://site.com/a1/p2" in [r.url for r in results2]
    assert f2.calls.count("https://site.com/a1/p2") == 1


# ==================== 3. engine 真跑两轮 ====================

def test_engine_run_records_task_pages(tmp_config, tmp_db, monkeypatch):
    """第一轮：抓完的页落 done、发现了没抓的落 pending（中途停也不丢）。"""
    _write_rule(tmp_config.rules_dir)
    _authorize(tmp_db)
    tid = _mk_task(tmp_config, tmp_db, max_pages=2)   # 只抓 2 页就停
    eng, f = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    rep = asyncio.run(eng.run(tid))

    assert rep.pages == 2, rep.pages
    assert rep.pages_resumed == 0
    m = tmp_db.task_pages_resume(tid)
    # 抓完的 2 页 = done
    assert m["https://site.com/"] == ("done", 0), m
    assert m["https://site.com/a1"] == ("done", 1), m
    # 发现了但被页数上限挡住的 2 页 = pending（断点就存在这）
    assert m["https://site.com/a1/p1"] == ("pending", 2), m
    assert m["https://site.com/a1/p2"] == ("pending", 2), m
    st = tmp_db.task_page_stats(tid)
    assert st == {"done": 2, "pending": 2, "error": 0, "blocked": 0,
                  "total": 4}, st


def test_engine_rerun_continues_from_breakpoint(tmp_config, tmp_db,
                                                monkeypatch):
    """★第二轮：只抓没抓完的页 —— done 页一次都没再 fetch（账本说话）。"""
    _write_rule(tmp_config.rules_dir)
    _authorize(tmp_db)
    tid = _mk_task(tmp_config, tmp_db, max_pages=2)

    eng1, f1 = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    rep1 = asyncio.run(eng1.run(tid))
    assert rep1.pages == 2

    # 第二轮：同样的页数上限，但只剩 2 个 pending 页要抓
    eng2, f2 = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    rep2 = asyncio.run(eng2.run(tid))

    assert rep2.pages == 2, rep2.pages
    # ★断点续采的账：跳过 2 个已抓页
    assert rep2.pages_resumed == 2, rep2.pages_resumed
    # ★done 页一次都没再 fetch（父页跳过，子页没丢 —— 账本里续命）
    assert f2.calls.count("https://site.com/") == 0
    assert f2.calls.count("https://site.com/a1") == 0
    assert f2.calls.count("https://site.com/a1/p1") == 1
    assert f2.calls.count("https://site.com/a1/p2") == 1
    st = tmp_db.task_page_stats(tid)
    assert st["done"] == 4 and st["pending"] == 0, st


def test_engine_rerun_after_full_crawl_does_nothing(tmp_config, tmp_db,
                                                    monkeypatch):
    """全抓完的任务再跑：零 fetch、零新页、账本 4/4。"""
    _write_rule(tmp_config.rules_dir)
    _authorize(tmp_db)
    tid = _mk_task(tmp_config, tmp_db)

    eng1, _ = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    asyncio.run(eng1.run(tid))
    eng2, f2 = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    rep2 = asyncio.run(eng2.run(tid))

    assert f2.calls == [], f2.calls
    assert rep2.pages == 0 and rep2.pages_resumed == 4, rep2
    st = tmp_db.task_page_stats(tid)
    assert st["done"] == 4 and st["total"] == 4, st


def test_engine_rerun_retries_error_page(tmp_config, tmp_db, monkeypatch):
    """error 页第二轮重试成功：done 记账，断点闭环。"""
    _write_rule(tmp_config.rules_dir)
    _authorize(tmp_db)
    tid = _mk_task(tmp_config, tmp_db, max_pages=4)   # 4 页全抓到

    eng1, _ = _engine(tmp_config, tmp_db, PAGES, monkeypatch,
                      error_urls={"https://site.com/a1/p2"})
    rep1 = asyncio.run(eng1.run(tid))
    assert rep1.pages == 4
    m = tmp_db.task_pages_resume(tid)
    assert m["https://site.com/a1/p2"][0] == "error", m

    eng2, f2 = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    rep2 = asyncio.run(eng2.run(tid))
    assert f2.calls == ["https://site.com/a1/p2"], f2.calls
    assert rep2.pages == 1 and rep2.pages_resumed == 3, rep2
    m2 = tmp_db.task_pages_resume(tid)
    assert m2["https://site.com/a1/p2"][0] == "done", m2


def test_engine_rerun_retries_blocked_page(tmp_config, tmp_db, monkeypatch):
    """blocked 页（robots/403）第二轮解禁后重试成功。"""
    _write_rule(tmp_config.rules_dir)
    _authorize(tmp_db)
    tid = _mk_task(tmp_config, tmp_db, max_pages=4)

    eng1, _ = _engine(tmp_config, tmp_db, PAGES, monkeypatch,
                      block_urls={"https://site.com/a1/p2"})
    asyncio.run(eng1.run(tid))
    m = tmp_db.task_pages_resume(tid)
    assert m["https://site.com/a1/p2"][0] == "blocked", m

    eng2, f2 = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    rep2 = asyncio.run(eng2.run(tid))
    assert f2.calls == ["https://site.com/a1/p2"], f2.calls
    m2 = tmp_db.task_pages_resume(tid)
    assert m2["https://site.com/a1/p2"][0] == "done", m2


def test_engine_resume_book_read_failure_degrades_to_full_crawl(
        tmp_config, tmp_db, monkeypatch):
    """账本读不出来不能拦住抓取：退化成全量重爬（老行为）。"""
    _write_rule(tmp_config.rules_dir)
    _authorize(tmp_db)
    tid = _mk_task(tmp_config, tmp_db)

    def _boom(self, task_id):
        raise RuntimeError("ledger gone")
    monkeypatch.setattr(type(tmp_db), "task_pages_resume", _boom)
    eng, f = _engine(tmp_config, tmp_db, PAGES, monkeypatch)
    rep = asyncio.run(eng.run(tid))
    assert rep.pages == 4 and rep.pages_resumed == 0, rep
    assert f.calls.count("https://site.com/") == 1


# ==================== 4. webui API ====================

def _handler(db, queue=None):
    from media_resource_manager import webui
    h = object.__new__(webui._Handler)
    h.server = types.SimpleNamespace(db=db, queue=queue)
    return h


def test_api_tasks_includes_pages_fields(tmp_config, tmp_db):
    _mk_task(tmp_config, tmp_db)
    tmp_db.record_page_result(1, "https://s.com/a", "done")
    tmp_db.record_page_seen(1, "https://s.com/b", 1)
    h = _handler(tmp_db)
    rows = h._api_tasks()
    assert rows and isinstance(rows, list)
    row = rows[0]
    assert row["pages_done"] == 1, row
    assert row["pages_total"] == 2, row


def test_api_task_detail_includes_pages_fields(tmp_config, tmp_db):
    _mk_task(tmp_config, tmp_db)
    tmp_db.record_page_result(1, "https://s.com/a", "done", images=2)
    tmp_db.record_page_result(1, "https://s.com/b", "error", error="x")
    h = _handler(tmp_db)
    out = h._api_task_detail(1)
    assert out["ok"] is True, out
    assert out["task"]["pages_done"] == 1
    assert out["task"]["pages_total"] == 2


def test_api_task_delete_clears_pages(tmp_config, tmp_db):
    _mk_task(tmp_config, tmp_db)
    tmp_db.record_page_result(1, "https://s.com/a", "done")
    h = _handler(tmp_db)
    assert h._api_task_delete(1)["ok"] is True
    assert tmp_db.task_pages_resume(1) == {}, "删了任务账本还在（孤行）"


def test_webui_delete_and_cleanup_reference_clear():
    """源码判据：两条删除路径都清了子表再删父行（防孤行/串账）。

    R221/C10 口径更新：_api_task_delete 与批量清理都改成「先清
    failed_urls/task_pages 两张子表，再删 tasks 父行」，字面量从
    `"DELETE FROM task_pages"` 变成 _fk_order 循环里的 child 清理 ——
    判据跟着实现走，守的是「两条路径都清子表」这个行为，不是某个字符串。
    """
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(
        encoding="utf-8")
    assert "for _clr in (self.db.clear_failed_urls, " \
           "self.db.clear_task_pages):" in src, "删除任务没先清两张子表"
    assert '_fk_order = ("reviews", "images", "tasks")' in src, \
        "全面清理丢了固定顺序"
    assert '"failed_urls", "task_pages"' in src, \
        "删任务/清理任务前没清两张子表"


# ==================== 5. GUI 助手 ====================

def test_gui_pages_brief_formats(tmp_db):
    _seed_tasks(tmp_db, 1, 2)
    from media_resource_manager.gui.main_window import _pages_brief
    assert _pages_brief(tmp_db, 1) == ""          # 没有账本 → 不占行宽
    tmp_db.record_page_result(1, "https://s.com/a", "done")
    tmp_db.record_page_seen(1, "https://s.com/b", 1)
    assert _pages_brief(tmp_db, 1) == "已抓 1/2 页"
    tmp_db.record_page_result(2, "https://s.com/c", "done")
    assert _pages_brief(tmp_db, 2) == "已抓 1/1 页"


def test_gui_row_and_log_use_pages_brief():
    """双端交付判据：任务行副行接了简报、完成日志带断点账。"""
    mw = (ROOT / "media_resource_manager" / "gui" / "main_window.py"
          ).read_text(encoding="utf-8")
    assert "def _pages_brief(" in mw, "桌面端缺页面简报助手"
    assert "_pages_brief(getattr(win" in mw, "任务行没接简报"
    assert "pages_resumed" in mw, "完成日志没带断点账"

# ==================== 6. 接线判据（源码级） ====================

def test_engine_wires_resume():
    """engine 的兜底发现路径真接了账本：种子进 Discovery、结果落库。"""
    src = (ROOT / "media_resource_manager" / "engine.py").read_text(
        encoding="utf-8")
    assert "task_pages_resume(task_id)" in src, "没读页面账本当种子"
    assert "known_pages=known_pages" in src, "种子没喂给 Discovery"
    assert "record_page_result(" in src, "抓完的页没落账"
    assert "record_page_seen(task_id" in src, "新链接没落 pending"
    assert "pages_resumed" in src, "RunReport 没有断点账字段"


def test_discovery_run_seeds_known_pages():
    src = (ROOT / "media_resource_manager" / "crawler" / "discovery.py"
           ).read_text(encoding="utf-8")
    assert "self.known_pages.items()" in src, "run() 没把账本搬进队列"
    assert "self.skipped_done += 1" in src, "没数跳过多少已抓页"
    assert "self.on_discover(link, d)" in src, "新链接没回调落账"


def test_queue_report_carries_pages_resumed():
    """完成日志/UI 的断点账来自队列报告字典（GUI 只认这个口径）。"""
    src = (ROOT / "media_resource_manager" / "scheduler" / "queue.py"
           ).read_text(encoding="utf-8")
    assert '"pages_resumed"' in src, "队列报告没带 pages_resumed"
