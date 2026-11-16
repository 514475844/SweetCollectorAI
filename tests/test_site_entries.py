"""站点入口映射 / 形状提示 的守卫测试（2026-10-26）。

真机事故背景：用户把任务填成裸首页（pornpics.com/），gallery-dl 不认这个
形状，引擎静默退回自研爬虫 → 站点回 JS 空壳 → 0 图。修复后：
裸首页/裸列表要能映射到该站的合法入口；已明确形状的 URL 一律不动。

2026-10-26 用户裁决补充：pornpics 的 `/recent/` 只是「最新」不是「全站」，
所以那道捷径被删了 —— 全站靠规则文件里的分类入口 + offset JSON 分页。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from media_resource_manager.crawler.site_entries import (  # noqa: E402
    HOMEPAGE_MAP, map_entry, supported_shapes)


def test_pornpics_homepage_is_not_mapped_to_recent():
    """裸首页不再被偷懒映射成 /recent/（那只是最新流）。"""
    mapped = map_entry("https://www.pornpics.com/")
    assert mapped is None or "recent" not in mapped, mapped


def test_pornpics_specific_shape_untouched():
    for u in ("https://www.pornpics.com/tags/lingerie/",
              "https://www.pornpics.com/galleries/12345/slug/",
              "https://www.pornpics.com/recent/"):
        assert map_entry(u) is None, f"{u} 不该被映射"


def test_danbooru_bare_posts_maps_to_explore():
    mapped = map_entry("https://danbooru.donmai.us/posts")
    assert mapped and "explore/posts/popular" in mapped, mapped
    # 带 tags 的 URL 是明确形状，不许动
    assert map_entry("https://danbooru.donmai.us/posts?tags=bikini") is None


def test_konachan_yandere_bare_post_maps_with_month():
    for host in ("konachan.com", "yande.re"):
        mapped = map_entry(f"https://{host}/post")
        assert mapped and "popular_by_month" in mapped, (host, mapped)
        assert "{year}" not in mapped and "{month}" not in mapped, mapped


def test_unknown_site_not_mapped():
    assert map_entry("https://example.org/") is None
    assert map_entry("") is None


def test_supported_shapes_lists_examples():
    shapes = supported_shapes("https://www.pornpics.com/")
    assert shapes, "pornpics 至少应列出几种形状"
    assert any(("recent" in s) or ("popular" in s) or ("tags" in s)
               or ("galleries" in s) for s in shapes), shapes
    d_shapes = supported_shapes("https://danbooru.donmai.us/posts")
    assert d_shapes and any("posts" in s for s in d_shapes), d_shapes


def test_homepage_map_entries_are_absolute():
    for host, tpl in HOMEPAGE_MAP.items():
        assert tpl.startswith("https://"), (host, tpl)


def test_pornpics_rule_covers_whole_site():
    """规则文件必须是「分类入口 + offset 分页」，而不是映射到某个列表页。"""
    import yaml
    rule = yaml.safe_load((ROOT / "data" / "rules" / "pornpics.yaml")
                          .read_text(encoding="utf-8"))
    entries = rule.get("entry_urls") or []
    assert len(entries) >= 100, f"分类入口太少：{len(entries)}"
    assert all("/galleries/" not in e for e in entries), "不该把相册当入口"
    paging = rule.get("paging") or {}
    assert paging.get("mode") == "offset_json", paging
    assert paging.get("link_key") == "g_url", paging
