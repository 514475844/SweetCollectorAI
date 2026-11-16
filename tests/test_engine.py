import asyncio
import json
from pathlib import Path

from media_resource_manager.engine import Engine
from media_resource_manager.repo import get_task, save_task
from media_resource_manager.models import TaskConfig, TaskStatus

PAGES = {
    "https://site.com/": '<a class="album" href="/a1">A1</a>',
    "https://site.com/a1": ('<a class="photo" href="/a1/p1">p1</a>'
                            '<a class="photo" href="/a1/p2">p2</a>'),
    "https://site.com/a1/p1": '<img src="/img/1.jpg">',
    # second image under a DIFFERENT url; FakeFetcher serves identical bytes
    # for both, so content-based (SHA256) duplicate detection must trigger.
    "https://site.com/a1/p2": '<img src="/img/2.jpg">',
}


def _write_rule(rules_dir: Path):
    rule = {
        "name": "site", "base_url": "https://site.com",
        "allowed_domains": ["site.com"],
        "list_page_selectors": {"albums": {"kind": "css", "expr": "a.album", "attr": "href"}},
        "album_link_selectors": {"photos": {"kind": "css", "expr": "a.photo", "attr": "href"}},
        "image_link_selectors": [{"kind": "css", "expr": "img", "attr": "src"}],
        "max_depth": 3,
    }
    (rules_dir / "site.json").write_text(json.dumps(rule), encoding="utf-8")


def test_engine_full_run_downloads_and_dedups(tmp_config, tmp_db, fake_fetcher):
    _write_rule(tmp_config.rules_dir)
    t = TaskConfig(id=None, name="t", adapter_name="site",
                   entry_urls=["https://site.com/"], output_dir=str(tmp_config.library_dir),
                   status=TaskStatus.IDLE)
    tid = save_task(tmp_db, t)
    # pre-clear compliance (user already confirmed authorization)
    tmp_db.execute("INSERT INTO compliance(site, robots_ok, basis) VALUES (?,?,?)",
                   ("site.com", 1, "fixture test"))

    def factory(cfg, proxy):
        return fake_fetcher(PAGES)

    eng = Engine(tmp_config, tmp_db, fetcher_factory=factory)
    rep = asyncio.run(eng.run(tid))

    assert rep.pages >= 3
    assert rep.images_found >= 1
    assert rep.downloaded >= 1
    # identical fixture image bytes -> duplicates detected
    assert rep.dedup >= 1
    # output files exist
    files = list(tmp_config.library_dir.rglob("*.jpg*"))
    assert files


def test_engine_runs_without_compliance(tmp_config, tmp_db, fake_fetcher):
    """2026-10-26 门禁整层删除后的守卫：无授权记录也必须照跑。"""
    _write_rule(tmp_config.rules_dir)
    t = TaskConfig(id=None, name="t", adapter_name="site",
                   entry_urls=["https://site.com/"], output_dir=str(tmp_config.library_dir))
    tid = save_task(tmp_db, t)

    def factory(cfg, proxy):
        return fake_fetcher(PAGES)

    eng = Engine(tmp_config, tmp_db, fetcher_factory=factory)
    rep = asyncio.run(eng.run(tid))  # no basis, no prior compliance — 照跑
    assert not any("compliance" in e for e in rep.errors), rep.errors
