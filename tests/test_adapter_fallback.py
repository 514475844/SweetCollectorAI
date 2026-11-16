"""通用兜底：新站点没有精确选择器时，也要能抓到图（#现场问题 pornpics）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_resource_manager.crawler.adapter import AdapterRunner  # noqa: E402
from media_resource_manager.crawler.rules import AdapterRule  # noqa: E402
from media_resource_manager.models import PageType  # noqa: E402

# 只含 <a href="*.jpg"> 原图链接的相册页（pornpics.center 的真实结构）
ALBUM_HTML = """
<html><body>
  <img class="logo" src="/img/logo.png" width="81" height="53">
  <a href="https://cdn.example.com/galleries/26/988/0_716.jpg"><img src="https://cdn.example.com/galleries/26/988/0_t.jpg" width="225" height="300"></a>
  <a href="https://cdn.example.com/galleries/26/988/1_129.jpg"><img src="https://cdn.example.com/galleries/26/988/1_t.jpg" width="225" height="300"></a>
  <a href="/a/555/"><img src="https://cdn.example.com/415/494_cover.jpg" width="225" height="300"></a>
</body></html>
"""

# 只有 img[src] 的封面墙（首页）
COVER_HTML = """
<html><body>
  <a href="/"><img class="logo" src="/img/logo.png" width="81" height="53"></a>
  <a href="/a/1/"><img src="https://cdn.example.com/1/a.jpg" width="225" height="300"></a>
  <a href="/a/2/"><img src="https://cdn.example.com/2/b.jpg" width="225" height="300"></a>
  <img src="/img/star.gif">
</body></html>
"""

# 规则里的选择器能命中的"正常站点"
LAZY_HTML = """
<html><body>
  <img data-src="https://cdn.example.com/big/1.jpg">
  <img data-src="https://cdn.example.com/big/2.jpg">
</body></html>
"""

NO_IMG_HTML = "<html><body><div>需要登录</div></body></html>"


def _rule(image_selectors=None, list_selectors=None):
    return AdapterRule(
        name="t",
        base_url="https://www.example.com",
        image_link_selectors=image_selectors or [
            {"kind": "css", "expr": "img[data-src]", "attr": "data-src"},
            {"kind": "css", "expr": "img[src*='thumb']", "attr": "src"},
        ],
        list_page_selectors=list_selectors or {
            "albums": {"kind": "css",
                       "expr": "a[href*='gallery'], a[href*='album']",
                       "attr": "href"},
        },
    )


def test_fallback_prefers_full_image_links():
    r = AdapterRunner(_rule())
    got, warn = r.extract_images(ALBUM_HTML, "https://www.example.com/")
    assert "galleries/26/988/0_716.jpg" in " ".join(got), got
    assert "galleries/26/988/1_129.jpg" in " ".join(got), got
    # 兜底走了"原图链接"，就不该再把 _t.jpg 缩略图也收进来
    assert not any("_t.jpg" in u for u in got), got
    # logo 是 81x53，必须被过滤掉
    assert not any("logo" in u for u in got), got
    assert any("通用兜底" in w for w in warn), warn


def test_fallback_filters_logo_and_keeps_covers():
    r = AdapterRunner(_rule())
    got, warn = r.extract_images(COVER_HTML, "https://www.example.com/")
    assert any(u.endswith("/1/a.jpg") for u in got), got
    assert any(u.endswith("/2/b.jpg") for u in got), got
    assert not any("logo" in u for u in got), got
    assert not any(u.endswith(".gif") for u in got), got


def test_no_fallback_when_selectors_work():
    r = AdapterRunner(_rule())
    got, warn = r.extract_images(LAZY_HTML, "https://www.example.com/")
    assert len(got) == 2, got
    assert not any("通用兜底" in w for w in warn), warn


def test_fallback_gives_diagnosis_when_nothing():
    r = AdapterRunner(_rule())
    got, warn = r.extract_images(NO_IMG_HTML, "https://www.example.com/")
    assert got == []
    assert any("JS 动态渲染" in w or "需要登录" in w for w in warn), warn


def test_link_fallback_finds_thumbnail_entries():
    r = AdapterRunner(_rule())
    links, warn = r.extract_links(COVER_HTML, "https://www.example.com/",
                                  PageType.LIST)
    assert any(u.endswith("/a/1/") for u in links), links
    assert any(u.endswith("/a/2/") for u in links), links
    # 包着 logo 的首页链接不该被当成入口
    assert not any(u.rstrip("/").endswith("example.com") for u in links), links
    assert any("通用兜底" in w for w in warn), warn


def test_link_fallback_uses_rel_next():
    html = COVER_HTML.replace("<body>", '<body><link rel="next" href="/2/">')
    r = AdapterRunner(_rule())
    links, _ = r.extract_links(html, "https://www.example.com/", PageType.LIST)
    assert any(u.endswith("/2/") for u in links), links


def test_upgrade_to_grouped_originals_not_og_ads():
    """相册页：og:image 只给 1 张、还混着外站广告 → 应换成成组的原图锚点。"""
    html = """
    <html><head>
      <meta property="og:image" content="https://cdn.example.com/g/1/0_1.jpg">
      <meta name="twitter:image" content="https://ads.example.net/x/1.jpg">
      <meta name="twitter:image" content="https://ads.example.net/x/2.jpg">
    </head><body>
      <a href="https://cdn.example.com/g/1/0_1.jpg"><img src="/t0.jpg" width="225" height="300"></a>
      <a href="https://cdn.example.com/g/1/1_2.jpg"><img src="/t1.jpg" width="225" height="300"></a>
      <a href="https://cdn.example.com/g/1/2_3.jpg"><img src="/t2.jpg" width="225" height="300"></a>
      <a href="https://cdn.example.com/g/1/3_4.jpg"><img src="/t3.jpg" width="225" height="300"></a>
      <a href="https://ads.example.net/banner.jpg"><img src="/b.jpg" width="300" height="100"></a>
    </body></html>
    """
    r = AdapterRunner(_rule())
    got, warn = r.extract_images(html, "https://www.example.com/")
    assert len(got) == 4, got
    assert all("cdn.example.com/g/1/" in u for u in got), got
    assert not any("ads.example.net" in u for u in got), got


def test_discovery_stops_at_max_pages():
    import asyncio
    from media_resource_manager.crawler.discovery import Discovery

    kids = "".join(
        f'<a href="/a/{i}/"><img src="/{i}.jpg" width="225" height="300"></a>'
        for i in range(20))
    parent = f"<html><body>{kids}</body></html>"
    child = '<html><body><img src="/x.jpg" width="225" height="300"></body></html>'

    class _F:
        def __init__(self):
            self.n = 0

        async def fetch_text(self, url):
            self.n += 1
            return parent if url.rstrip("/").endswith("example.com") else child

        async def close(self):
            pass

    async def go(cap):
        warns: list[str] = []
        f = _F()
        d = Discovery(f, AdapterRunner(_rule()), base_domain="example.com",
                      max_depth=2, on_warning=warns.append, max_pages=cap)
        await d.run(["https://example.com/"])
        return d.pages, warns

    pages, warns = asyncio.run(go(3))
    assert pages == 3, pages
    assert any("页数上限" in w for w in warns), warns

    pages2, _ = asyncio.run(go(0))
    assert pages2 > 3, pages2


def test_no_link_fallback_when_selectors_work():
    html = '<html><body><a href="/gallery/9"><img src="/t.jpg" width="225" height="300"></a></body></html>'
    r = AdapterRunner(_rule())
    links, warn = r.extract_links(html, "https://www.example.com/",
                                  PageType.LIST)
    assert any(u.endswith("/gallery/9") for u in links), links
    assert not any("通用兜底" in w for w in warn), warn


def test_jsonld_url_field_excludes_page_links():
    """JSON-LD 的 url 字段是页面链接（专辑/作者页）时不得混进图片队列（4khd 真实场景）。"""
    html = """
    <html><body>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"BlogPosting",
       "url":"https://www.4khd.com/content/01/x.html",
       "mainEntityOfPage":{"@type":"WebPage",
                           "@id":"https://www.4khd.com/content/01/x.html"},
       "image":"https://secure.gravatar.com/avatar/e4074ef2e06ac95f7f709786be3866e4?s=96",
       "author":{"@type":"Person","url":"https://www.4khd.com/content/author/admin"}}
      </script>
    </body></html>
    """
    r = AdapterRunner(_rule())
    got, _ = r.extract_images(html, "https://www.4khd.com/")
    joined = " ".join(got)
    assert "content/01/x.html" not in joined, got
    assert "author/admin" not in joined, got
    # image 字段仍按 schema 收取
    assert any("gravatar.com" in u for u in got), got


def test_jsonld_url_field_keeps_image_suffix():
    """url 字段若是图片后缀仍要收（部分站点只有 url 一个出口）。"""
    html = """
    <html><body>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"ImageObject",
       "url":"https://cdn.example.com/photos/cover.jpg",
       "contentUrl":"https://cdn.example.com/photos/full.png"}
      </script>
    </body></html>
    """
    r = AdapterRunner(_rule())
    got, _ = r.extract_images(html, "https://www.example.com/")
    assert "https://cdn.example.com/photos/cover.jpg" in got, got
    assert "https://cdn.example.com/photos/full.png" in got, got
