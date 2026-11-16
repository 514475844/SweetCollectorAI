"""Adapter runner: applies user selectors to parsed HTML.

Supports css (bs4), xpath (lxml, optional), regex. Also extracts from
JSON-LD and OpenGraph meta tags automatically. Reports selector failures
(zero matches) so the UI can warn instead of silently failing.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from ..models import PageType
from ..utils.url import normalize_url
from .rules import AdapterRule

# ---------------------------------------------------------------------------
# 通用兜底（generic fallback）
#
# 用户往软件里加一个新网站时，往往没有(也不该强求有)一套精确的选择器。
# 规则里的选择器一条都没命中 → 以前直接 0 张图、只留一句 "matched 0"，
# 用户完全不知道该怎么办。这里补一层启发式兜底：
#   1) <a href="xxx.jpg"> —— 图库站最常见的"缩略图链到原图"结构，优先取（是原图）
#   2) <img src="...">    —— 过滤掉 logo/图标/占位图后取（可能是封面缩略图）
# 只有在选择器颗粒无收时才启用，不会影响已经配好规则的站点。
# ---------------------------------------------------------------------------
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif")
_NOISE_RE = re.compile(
    r"(logo|icon|sprite|avatar|placeholder|spacer|pixel|banner|blank|"
    r"emoji|favicon|badge|/btn|button|arrow|loader|loading|spinner|star|rating)",
    re.I)
# 每页兜底链接上限，防止"图片墙 2000 个入口"把任务跑飞。
FALLBACK_LINK_CAP = 150


def _to_int(v) -> int:
    try:
        return int(str(v).strip().lower().rstrip("px"))
    except Exception:
        return 0


def _is_content_img(img) -> bool:
    """启发式判断这个 <img> 是不是"正经内容图"而不是站点 UI 图标。"""
    src = (img.get("src") or "").strip()
    if not src or src.startswith(("data:", "javascript:")):
        return False
    path = src.split("?", 1)[0].lower()
    if not path.endswith(_IMG_EXT):
        return False
    if _NOISE_RE.search(path):
        return False
    w = _to_int(img.get("width"))
    h = _to_int(img.get("height"))
    # 显式声明了尺寸并且很小（如 logo 81x53）→ 当成 UI 元素丢掉
    if w and h and max(w, h) < 100:
        return False
    if w and w < 80:
        return False
    return True


def _img_full_links(soup) -> list[str]:
    """<a href="...jpg">：多数图库用锚点指向原图，优先用它。

    带"成组"过滤：同一个相册的原图 URL 通常都在同一个目录下（如
    /galleries/26/988/0_716.jpg … 15_128.jpg），而页头页脚的广告图链接是散的。
    取"最大的那一组"，前提是它确实占多数——这样能自动把广告甩掉。
    """
    out: list[str] = []
    for a in soup.select("a[href]"):
        h = (a.get("href") or "").strip()
        if not h:
            continue
        p = h.split("?", 1)[0].lower()
        if p.endswith(_IMG_EXT) and not _NOISE_RE.search(p):
            out.append(h)
    if len(out) < 3:
        return out
    groups: dict[str, list[str]] = {}
    for u in out:
        groups.setdefault(u.rsplit("/", 1)[0], []).append(u)
    best = max(groups.values(), key=len)
    if len(best) >= 3 and len(best) * 2 >= len(out):
        return best
    return out


def _content_img_srcs(soup) -> list[str]:
    out: list[str] = []
    for im in soup.find_all("img"):
        if _is_content_img(im):
            out.append((im.get("src") or "").strip())
    return out


class SelectorFailure(Exception):
    """Raised when an expected selector matched nothing (site may have changed)."""


def _extract_with(soup, sel: dict, html: str):
    """Return (attr, value) for a selector dict.

    selector dict shape: {"kind": "css"|"xpath"|"regex", "expr": "...",
                          "attr": "href"|"src"|... (optional), "group": N}
    """
    kind = sel.get("kind", "css")
    expr = sel["expr"]
    if kind == "css":
        nodes = soup.select(expr)
        attr = sel.get("attr")
        if attr:
            return [n.get(attr, "") for n in nodes if n.get(attr)]
        return [n.get_text(strip=True) for n in nodes]
    if kind == "xpath":
        try:
            from lxml import etree  # type: ignore
        except ImportError as e:
            raise RuntimeError("使用 XPath 选择器需要 lxml 库") from e
        tree = etree.HTML(html)
        attr = sel.get("attr")
        if attr:
            return tree.xpath(f"{expr}/@{attr}")
        return [str(x) for x in tree.xpath(expr)]
    if kind == "regex":
        grp = sel.get("group", 0)
        return [m.group(grp) for m in re.finditer(expr, html)]
    raise ValueError(f"未知的选择器类型： {kind}")


class AdapterRunner:
    def __init__(self, rule: AdapterRule):
        self.rule = rule

    def _soup(self, html: str):
        from bs4 import BeautifulSoup
        try:
            return BeautifulSoup(html, "lxml")
        except Exception:
            # lxml is optional; stdlib parser keeps the pipeline working.
            return BeautifulSoup(html, "html.parser")

    def extract_links(self, html: str, base: str, page_type: PageType) -> tuple[list[str], list[str]]:
        """Return (links, warnings). Links are normalized & same-domain checked.
        Backward-compatible: child (discovery) links + next-page links combined.
        Prefer extract_links_typed() to keep the two buckets separate — next-page
        links are "continuation" and must NOT consume BFS depth."""
        child, nxt, warnings = self.extract_links_typed(html, base, page_type)
        return child + nxt, warnings

    def extract_links_typed(self, html: str, base: str, page_type: PageType):
        """Return (child_links, next_links, warnings).

        - child_links: from list/album/image page-type selectors → "new discovery";
          BFS enqueues these at depth+1.
        - next_links: from next_page_selectors (pagination / continuation) → BFS
          enqueues these at the SAME depth, so paging through one album or list
          does not burn the depth budget. This is what makes "抓取深度" mean
          "layers of new resources" instead of "how deep you paged inside one
          album". Without this split, a 50-page album would eat depth 50 and the
          crawl would silently stop after a handful of albums.
        """
        soup = self._soup(html)
        sel_map = {
            PageType.LIST: self.rule.list_page_selectors,
            PageType.ALBUM: self.rule.album_link_selectors,
            PageType.IMAGE: self.rule.image_page_selectors,
        }.get(page_type, {})
        warnings: list[str] = []
        child: list[str] = []
        for label, sel in sel_map.items():
            vals = _extract_with(soup, sel, html)
            if not vals:
                warnings.append(f"selector '{label}' matched 0 links")
            for v in vals:
                n = normalize_url(v, base)
                if n:
                    child.append(n)
        # next-page links (continuation — do NOT count as deeper discovery)
        next_links: list[str] = []
        for label, sel in self.rule.next_page_selectors.items():
            vals = _extract_with(soup, sel, html)
            if vals:
                for v in vals:
                    n = normalize_url(v, base)
                    if n:
                        next_links.append(n)
            elif page_type in (PageType.LIST, PageType.ALBUM):
                warnings.append(f"next_page selector '{label}' matched 0 links")
        if not child and not next_links:
            fb = self._fallback_links(soup)[:FALLBACK_LINK_CAP]
            for v in fb:
                n = normalize_url(v, base)
                if n:
                    child.append(n)
            if fb:
                warnings.append(
                    f"链接选择器全落空，已用通用兜底（a>img 封面链接 / "
                    f"link[rel=next]）取到 {len(fb)} 条")
        return child, next_links, warnings

    def _fallback_links(self, soup) -> list[str]:
        """选择器一条都没命中时的通用链接兜底。"""
        out: list[str] = []
        # 标准翻页：<link rel="next" href="...">
        for lk in soup.select("link[rel='next']"):
            h = (lk.get("href") or "").strip()
            if h:
                out.append(h)
        # 封面即入口：<a href="/album/123"><img ...></a>
        for a in soup.select("a[href]"):
            h = (a.get("href") or "").strip()
            if not h or h.startswith("#"):
                continue
            if h.lower().startswith(("javascript:", "mailto:", "tel:")):
                continue
            im = a.find("img")
            if im is None or not _is_content_img(im):
                continue
            out.append(h)
        return out

    def _fallback_images(self, soup) -> tuple[list[str], str]:
        """选择器颗粒无收时的通用图片兜底，返回 (urls, 说明)。"""
        linked = _img_full_links(soup)
        if linked:
            return linked, "a[href$='.jpg'] 原图链接"
        srcs = _content_img_srcs(soup)
        if srcs:
            return srcs, "img[src]（已过滤 logo/图标）"
        return [], ""

    def _upgrade_url(self, u: str) -> str:
        """应用规则的缩略图→原图替换（如 t1.主机 → s2.主机）。"""
        for rep in getattr(self.rule, "image_url_replace", None) or []:
            f, t = rep.get("from", ""), rep.get("to", "")
            if f and f in u:
                u = u.replace(f, t)
        return u

    def extract_images(self, html: str, base: str) -> tuple[list[str], list[str]]:
        soup = self._soup(html)
        warnings: list[str] = []
        out: list[str] = []
        for sel in self.rule.image_link_selectors:
            vals = _extract_with(soup, sel, html)
            if not vals:
                warnings.append(f"image selector '{sel.get('expr')}' matched 0")
            for v in vals:
                n = normalize_url(self._upgrade_url(v), base)
                if n:
                    out.append(n)
        # Auto-detect OpenGraph / JSON-LD original images (public, highest res).
        for meta in soup.find_all("meta", attrs={"property": "og:image"}):
            c = meta.get("content")
            if c:
                n = normalize_url(self._upgrade_url(c), base)
                if n:
                    out.append(n)
        for meta in soup.find_all("meta", attrs={"name": "twitter:image"}):
            c = meta.get("content")
            if c:
                n = normalize_url(self._upgrade_url(c), base)
                if n:
                    out.append(n)
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except Exception:
                continue
            imgs = _walk_jsonld(data)
            for i in imgs:
                n = normalize_url(self._upgrade_url(i), base)
                if n:
                    out.append(n)
        # de-dup preserving order
        seen = set()
        uniq = []
        for u in out:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        linked = _img_full_links(soup)
        if linked and len(linked) > len(uniq):
            # 这一页有"成组的缩略图→原图锚点"，那才是相册正文；
            # 选择器/og:image 只捞到零星几张（常常混着广告），换掉。
            old = len(uniq)
            uniq = []
            seen = set()
            for v in linked:
                n = normalize_url(self._upgrade_url(v), base)
                if n and n not in seen:
                    seen.add(n)
                    uniq.append(n)
            if old:
                warnings.append(
                    f"图片选择器只取到 {old} 张，改用页面内成组的原图链接 "
                    f"{len(uniq)} 张")
            else:
                warnings.append(
                    f"所有 image selector 落空，已用通用兜底 "
                    f"a[href$='.jpg'] 原图链接 取到 {len(uniq)} 张"
                    f"（想要更稳可以给该站点单独存一份规则）")
        elif not uniq:
            fb, how = self._fallback_images(soup)
            for v in fb:
                n = normalize_url(self._upgrade_url(v), base)
                if n and n not in seen:
                    seen.add(n)
                    uniq.append(n)
            if uniq:
                warnings.append(
                    f"所有 image selector 落空，已用通用兜底 {how} 取到 "
                    f"{len(uniq)} 张（建议为该站点单独保存一份规则，精度更高）")
            else:
                warnings.append(
                    "所有 image selector 落空，通用兜底也没找到图片：页面可能是 "
                    "JS 动态渲染、需要登录/Cookie，或入口页本身不含图")
        return uniq, warnings

    def detect_page_type(self, html: str, url: str) -> PageType:
        """Pick the page type by which selector set actually MATCHES.

        Scoring beats guessing: a list page whose between-page links match
        `list_page_selectors` must not be treated as an album just because a
        generic rule exists.
        """
        soup = self._soup(html)
        counts: dict[PageType, int] = {}
        for ptype, selmap in (
            (PageType.LIST, self.rule.list_page_selectors),
            (PageType.ALBUM, self.rule.album_link_selectors),
            (PageType.IMAGE, self.rule.image_page_selectors),
        ):
            if not selmap:
                continue
            total = 0
            for sel in selmap.values():
                try:
                    total += len(_extract_with(soup, sel, html))
                except Exception:
                    pass
            counts[ptype] = total

        if counts.get(PageType.LIST, 0) > 0 and \
                counts.get(PageType.LIST, 0) >= counts.get(PageType.ALBUM, 0):
            return PageType.LIST
        if counts.get(PageType.ALBUM, 0) > 0:
            return PageType.ALBUM
        if counts.get(PageType.IMAGE, 0) > 0:
            return PageType.IMAGE
        # no rule matched: fall back to structure
        if soup.find_all("img"):
            return PageType.IMAGE
        return PageType.LIST


def _walk_jsonld(node, acc=None):
    if acc is None:
        acc = []
    if isinstance(node, dict):
        for k, v in node.items():
            kl = k.lower()
            if isinstance(v, str) and (
                kl in ("image", "contenturl")
                or (kl == "url" and v.split("?", 1)[0].lower().endswith(_IMG_EXT))
            ):
                # url 字段只收"长得像图片"的值，避免把页面链接/作者页混进下载队列
                acc.append(v)
            else:
                _walk_jsonld(v, acc)
    elif isinstance(node, list):
        for x in node:
            _walk_jsonld(x, acc)
    return acc
