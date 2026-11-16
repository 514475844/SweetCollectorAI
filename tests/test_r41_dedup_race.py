"""r41：并发去重竞态回归（engine「查库→入库」缺临界区）。

这条锁的是一个**真实功能缺陷**，不是测试写法问题：

* 现象：`tests/test_engine.py::test_engine_full_run_downloads_and_dedups`
  稳定红 `assert 0 >= 1`（RunReport.dedup 恒为 0）。
* 只读探针证据（跑两张内容相同的图）：
    download_one 1.jpg -> sha256='f5a5fd42d16a' phash=None
    download_one 2.jpg -> sha256='f5a5fd42d16a' phash=None
    find_duplicate_sha(sha=f5a5fd42d16a, excl=-1) -> None     ← 第一次
    find_duplicate_sha(sha=f5a5fd42d16a, excl=-1) -> None     ← 第二次，库里已有！
    库里却躺着 id=1 / id=2 两条 sha256 完全相同的记录
* 根因：worker 是并发的（n_workers 个 worker 抢同一个 queue），而
  「查库看重复」与「把这张 upsert 入库」之间隔着 `await`（AI 推理）。
  两个 worker 在对方入库前都查到「库里没有」→ 双双入库，dedup 计数还是 0。
  这等于 README 承诺的「去重(SHA256+pHash)」在并发下失效。

修法（engine.py）：查库那段放进 `self._dedup_lock` 临界区，并用
`_seen_sha` 记下「本批已见」；同批撞车（同 sha256）直接判重复并移出，
不再交给 `_keep_largest`（撞车时库里那条可能正被另一个 worker 看着）。

所以本文件比 test_engine 那条更严：**不只看 dedup 计数，还要库里没有重复**。
"""
import asyncio
import json


PAGES = {
    "https://site.com/": '<a class="album" href="/a1">A1</a>',
    "https://site.com/a1": ('<a class="photo" href="/a1/p1">p1</a>'
                            '<a class="photo" href="/a1/p2">p2</a>'),
    "https://site.com/a1/p1": '<img src="/img/1.jpg">',
    # 两个不同 URL、但 FakeFetcher 给的是同一串字节（图片不在 fixture 里 →
    # 落 `b"\x00" * 64`），所以 sha256 必然相同：这是刻意的「同内容不同 URL」
    "https://site.com/a1/p2": '<img src="/img/2.jpg">',
}


def _rule(rules_dir):
    rule = {
        "name": "site", "base_url": "https://site.com",
        "allowed_domains": ["site.com"],
        "list_page_selectors": {"albums": {"kind": "css", "expr": "a.album",
                                          "attr": "href"}},
        "album_link_selectors": {"photos": {"kind": "css", "expr": "a.photo",
                                          "attr": "href"}},
        "image_link_selectors": [{"kind": "css", "expr": "img", "attr": "src"}],
        "max_depth": 3,
    }
    (rules_dir / "site.json").write_text(json.dumps(rule), encoding="utf-8")


def _run_once(tmp_config, tmp_db, fake_fetcher, concurrency):
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig, TaskStatus
    from media_resource_manager.repo import save_task

    _rule(tmp_config.rules_dir)
    t = TaskConfig(id=None, name="t", adapter_name="site",
                   entry_urls=["https://site.com/"],
                   output_dir=str(tmp_config.library_dir),
                   status=TaskStatus.IDLE, concurrency=concurrency)
    tid = save_task(tmp_db, t)
    tmp_db.execute("INSERT INTO compliance(site, robots_ok, basis) VALUES (?,?,?)",
                   ("site.com", 1, "fixture test"))
    eng = Engine(tmp_config, tmp_db,
                 fetcher_factory=lambda c, p: fake_fetcher(PAGES))
    return asyncio.run(eng.run(tid))


def test_concurrent_same_content_is_deduped(tmp_config, tmp_db, fake_fetcher):
    """并发（同内容两张）必须只留一条入库，且 dedup 计数 ≥1。"""
    rep = _run_once(tmp_config, tmp_db, fake_fetcher, concurrency=4)

    rows = tmp_db.query("SELECT id, sha256 FROM images ORDER BY id")
    shas = [r["sha256"] for r in rows]

    assert rep.dedup >= 1, f"并发下去重没触发：dedup={rep.dedup}"
    assert len(shas) == len(set(shas)), (
        f"库里出现重复 SHA（去重在并发下失效）：{shas}")
    assert len(shas) == 1, (
        f"两张同内容图片应只留一条，实际入库 {len(shas)} 条：{shas}")


def test_serial_run_still_dedups(tmp_config, tmp_db, fake_fetcher):
    """串行（concurrency=1）也必须去重 —— 别为了修并发把串行路径弄坏。"""
    rep = _run_once(tmp_config, tmp_db, fake_fetcher, concurrency=1)
    shas = [r["sha256"] for r in tmp_db.query("SELECT sha256 FROM images")]
    assert rep.dedup >= 1, f"串行下去重没触发：dedup={rep.dedup}"
    assert len(shas) == 1, f"串行下应只留一条，实际 {len(shas)} 条"


def test_distinct_content_both_kept(tmp_config, tmp_db, fake_fetcher):
    """反向守卫：内容**不同**的两张不能被误判成重复（别把去重修成瞎删）。"""
    from conftest import FakeFetcher
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig, TaskStatus
    from media_resource_manager.repo import save_task

    class _Bytes(FakeFetcher):
        """给两张图不同字节，sha256 必然不同（其余行为沿用 FakeFetcher）。"""

        async def fetch_bytes(self, url, delay=None, range_start=0):
            # 2026-10-27：带 JPEG 头（魔数校验），内容仍不同
            body = (b"\xff\xd8\xff\xe0" + b"\x01" * 60 if url.endswith("1.jpg")
                    else b"\xff\xd8\xff\xe0" + b"\x02" * 60)
            return (200, body)

    _rule(tmp_config.rules_dir)
    t = TaskConfig(id=None, name="t2", adapter_name="site",
                   entry_urls=["https://site.com/"],
                   output_dir=str(tmp_config.library_dir),
                   status=TaskStatus.IDLE, concurrency=4)
    tid = save_task(tmp_db, t)
    tmp_db.execute("INSERT INTO compliance(site, robots_ok, basis) VALUES (?,?,?)",
                   ("site.com", 1, "fixture test"))
    eng = Engine(tmp_config, tmp_db, fetcher_factory=lambda c, p: _Bytes(PAGES))
    rep = asyncio.run(eng.run(tid))

    shas = [r["sha256"] for r in tmp_db.query("SELECT sha256 FROM images")]
    assert rep.dedup == 0, f"不同内容被误判成重复：dedup={rep.dedup}"
    assert len(shas) == 2, f"不同内容应都入库，实际 {len(shas)} 条"
