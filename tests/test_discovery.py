import asyncio
from media_resource_manager.crawler.discovery import Discovery
from media_resource_manager.crawler.adapter import AdapterRunner
from media_resource_manager.crawler.rules import AdapterRule
from media_resource_manager.models import PageType

PAGES = {
    "https://site.com/": '<a class="album" href="/a1">A1</a><a class="album" href="/a2">A2</a>',
    "https://site.com/a1": '<a class="photo" href="/a1/p1">p1</a><a class="next" href="/a1?p=2">n</a>',
    "https://site.com/a2": '<a class="photo" href="/a2/p1">p1</a>',
    "https://site.com/a1/p1": '<img src="/img/1.jpg">',
    "https://site.com/a2/p1": '<img src="/img/2.jpg">',
    "https://site.com/a1?p=2": '<a class="photo" href="/a1/p3">p3</a>',
    "https://site.com/a1/p3": '<img src="/img/3.jpg">',
}

RULE = AdapterRule(
    name="site", base_url="https://site.com",
    allowed_domains=["site.com"],
    list_page_selectors={"albums": {"kind": "css", "expr": "a.album", "attr": "href"}},
    album_link_selectors={"photos": {"kind": "css", "expr": "a.photo", "attr": "href"}},
    image_link_selectors=[{"kind": "css", "expr": "img", "attr": "src"}],
    next_page_selectors={"next": {"kind": "css", "expr": "a.next", "attr": "href"}},
    max_depth=3,
)


def test_discovery_bfs_depth_and_cycle_safe(fake_fetcher):
    f = fake_fetcher(PAGES)
    runner = AdapterRunner(RULE)
    results = []

    def on_page(r):
        results.append(r)

    # NOTE: /a1?p=2 is a NEXT-PAGE link (a.next) from /a1, so it is enqueued at
    # the SAME depth as /a1 (depth 1), NOT depth 2. Its child /a1/p3 therefore
    # sits at depth 2 — max_depth=3 is plenty. This is the "翻页不占深度" fix:
    # paging through one album must not burn the depth budget.
    disc = Discovery(f, runner, base_domain="site.com", max_depth=3,
                     on_page=on_page)
    asyncio.run(disc.run(["https://site.com/"]))

    urls = {r.url for r in results}
    assert "https://site.com/" in urls
    assert "https://site.com/a1" in urls
    assert "https://site.com/a2" in urls
    # depth 2 pages reached
    assert "https://site.com/a1/p1" in urls
    assert "https://site.com/a1?p=2" in urls
    # images collected from image pages
    all_imgs = [u for r in results for u in r.image_urls]
    assert "https://site.com/img/1.jpg" in all_imgs
    assert "https://site.com/img/3.jpg" in all_imgs
    # next-page link stays at the SAME depth as its parent (the key fix)
    by_url = {r.url: r for r in results}
    assert by_url["https://site.com/a1?p=2"].depth == by_url["https://site.com/a1"].depth
    # cycle safety: no URL visited twice
    assert len(urls) == len(set(urls))


def test_next_page_does_not_consume_depth(fake_fetcher):
    """A long pagination chain inside one album must not exhaust max_depth.

    Before the fix, every `a.next` hop added +1 to depth, so a 50-page album
    needed max_depth>=50 to be fully crawled. After the fix, all `a.next` links
    stay at the album's depth, and only the photo (discovery) links advance.
    """
    deep = {}
    # home -> one album
    deep["https://d.com/"] = '<a class="album" href="/alb">alb</a>'
    # album page: a photo + a next-page link
    deep["https://d.com/alb"] = '<a class="photo" href="/alb/1">1</a><a class="next" href="/alb?p=2">n</a>'
    # 5 more pagination pages, each only links to its photo + next page
    for p in range(2, 7):
        nxt = f"/alb?p={p+1}" if p < 6 else "/alb?p=6"
        deep[f"https://d.com/alb?p={p}"] = (
            f'<a class="photo" href="/alb/{p}">{p}</a>'
            f'<a class="next" href="{nxt}">n</a>')
    deep["https://d.com/alb/1"] = '<img src="/i/1.jpg">'
    for p in range(2, 7):
        deep[f"https://d.com/alb/{p}"] = f'<img src="/i/{p}.jpg">'

    rule = AdapterRule(
        name="d", base_url="https://d.com", allowed_domains=["d.com"],
        list_page_selectors={"albums": {"kind": "css", "expr": "a.album", "attr": "href"}},
        album_link_selectors={"photos": {"kind": "css", "expr": "a.photo", "attr": "href"}},
        image_link_selectors=[{"kind": "css", "expr": "img", "attr": "src"}],
        next_page_selectors={"next": {"kind": "css", "expr": "a.next", "attr": "href"}},
        max_depth=2,   # intentionally tiny: must still crawl all 6 album pages
    )
    f = fake_fetcher(deep)
    runner = AdapterRunner(rule)
    results = []
    disc = Discovery(f, runner, base_domain="d.com", max_depth=2,
                     on_page=lambda r: results.append(r))
    asyncio.run(disc.run(["https://d.com/"]))

    urls = {r.url for r in results}
    # all 6 album pages (home + p2..p6 via next-page) discovered despite depth=2
    for p in range(2, 7):
        assert f"https://d.com/alb?p={p}" in urls, f"pagination page {p} missed"
    # all 6 photos (images) collected
    all_imgs = [u for r in results for u in r.image_urls]
    for p in range(1, 7):
        assert f"https://d.com/i/{p}.jpg" in all_imgs, f"image {p} missed"
    # every pagination page sits at the SAME depth as the album (depth 1)
    by_url = {r.url: r for r in results}
    for p in range(2, 7):
        assert by_url[f"https://d.com/alb?p={p}"].depth == by_url["https://d.com/alb"].depth
    # cycle safety
    assert len(urls) == len(set(urls))


def test_adapter_extracts_links_and_images():
    runner = AdapterRunner(RULE)
    links, _ = runner.extract_links(PAGES["https://site.com/a1"], "https://site.com/a1", PageType.ALBUM)
    # links are returned ABSOLUTE (normalize_url+urljoin), not relative
    assert "https://site.com/a1/p1" in links
    assert "https://site.com/a1?p=2" in links      # next-page link
    imgs, _ = runner.extract_images(PAGES["https://site.com/a1/p1"], "https://site.com/a1/p1")
    assert "https://site.com/img/1.jpg" in imgs
