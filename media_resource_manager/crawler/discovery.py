"""Multi-level recursive discovery (BFS/DFS).

Walks same-domain links up to max_depth, normalizes URLs, applies
allow/deny regex, skips already-visited (cycle-safe), and extracts image
URLs + child links via the task's adapter. Emits a CrawlResult per page.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Callable, Optional

from ..models import CrawlResult, PageType
from ..utils.url import normalize_url, same_domain, allowed_by_regex, domain_of
from .adapter import AdapterRunner, SelectorFailure
from .fetcher import Fetcher, RobotDisallowed, AccessBlocked


class Discovery:
    def __init__(self, fetcher: Fetcher, runner: AdapterRunner,
                 base_domain: str, max_depth: int = 3,
                 allow: Optional[list[str]] = None,
                 deny: Optional[list[str]] = None,
                 strategy: str = "bfs",
                 on_page: Optional[Callable[[CrawlResult], None]] = None,
                 on_warning: Optional[Callable[[str], None]] = None,
                 max_pages: int = 0,
                 # F9（R154）：页面级断点续采 —— 上次的页面账本
                 # {url: (status, depth)}（db.task_pages_resume 的返回值）。
                 # done 的页进 visited = 本轮跳过不重爬；pending/error/blocked
                 # 连同**原深度**重新入队。None/空 dict = 老行为（全量重爬）。
                 known_pages: Optional[dict[str, tuple[str, int]]] = None,
                 # F9：新链接首次入队的瞬间回调（engine 据此落 pending 行）。
                 # 只在"这轮第一次见到它"时触发，续跑种子不触发。
                 on_discover: Optional[Callable[[str, int], None]] = None):
        self.f = fetcher
        self.runner = runner
        self.base_domain = base_domain
        self.max_depth = max_depth
        self.allow = allow
        self.deny = deny
        self.strategy = strategy
        self.on_page = on_page
        self.on_warning = on_warning
        self.visited: set[str] = set()
        self.max_pages = int(max_pages or 0)   # 0=不限
        self.pages = 0
        self._stop = False
        self.known_pages = known_pages or {}
        self.on_discover = on_discover
        # F9：本轮因"上轮已抓过"而跳过的页数（断点续采的账，进 RunReport）。
        self.skipped_done = 0

    def stop(self):
        self._stop = True

    def _capped(self) -> bool:
        """到达页数上限：停爬并给用户一条能看懂的说明。"""
        if not self.max_pages or self.pages < self.max_pages:
            return False
        if self.on_warning:
            self.on_warning(
                f"已达单次抓取页数上限 {self.max_pages} 页，自动停止"
                f"（想要更多：编辑任务 → 最大抓取页数；留 0 = 不限）")
        return True

    async def run(self, entry_urls: list[str]):
        queue: deque[tuple[str, int]] = deque()
        # F9：先把上次的进度搬进来。done 的页只进 visited（=本轮跳过，
        # 不重新 fetch、不计入本次 pages）；pending/error/blocked 连同
        # **原深度**重新入队 —— 断点续采的"续"就在这：上次爬到一半的
        # 页面这次接着爬，且翻页链不会因为父页跳过而断（子页自己在账本里）。
        for url, (status, depth) in self.known_pages.items():
            self.visited.add(url)
            if status == "done":
                self.skipped_done += 1
            else:
                queue.append((url, int(depth or 0)))
        for u in entry_urls:
            n = normalize_url(u)
            if n and n not in self.visited:
                self.visited.add(n)
                queue.append((n, 0))
        while queue and not self._stop:
            if self._capped():
                break
            url, depth = (queue.popleft() if self.strategy == "bfs"
                          else queue.pop())
            if depth > self.max_depth:
                continue
            self.pages += 1
            res = await self._crawl_page(url, depth)
            if self.on_page:
                self.on_page(res)
            if res.error == "blocked":
                continue
            # enqueue child links (new discovery → depth+1) and next-page links
            # (continuation → SAME depth, so paging one album/list doesn't burn
            # the depth budget). The total-page cap (max_pages) is the real
            # budget; depth only bounds "layers of new resources".
            for link, d in (
                [(l, depth + 1) for l in res.child_links] +
                [(l, depth) for l in res.next_links]
            ):
                if link in self.visited:
                    continue
                if not same_domain(link, f"https://{self.base_domain}"):
                    continue
                if not allowed_by_regex(link, self.allow, self.deny):
                    continue
                self.visited.add(link)
                queue.append((link, d))
                # F9：这轮第一次见到的新链接 → 落 pending 账本
                # （"知道了但还没抓"）。续跑种子不回调（它上轮就落过了）。
                if self.on_discover:
                    self.on_discover(link, d)
        return

    async def _crawl_page(self, url: str, depth: int) -> CrawlResult:
        res = CrawlResult(url=url, depth=depth)
        try:
            html = await self.f.fetch_text(url)
        except RobotDisallowed as e:
            res.error = "blocked"
            if self.on_warning:
                self.on_warning(f"robots blocked: {url}")
            return res
        except AccessBlocked as e:
            res.error = "blocked"
            if self.on_warning:
                self.on_warning(f"access blocked (ask human): {url}")
            return res
        except Exception as e:  # network/parse error -> skip, log
            res.error = str(e)[:200]
            if self.on_warning:
                self.on_warning(f"fetch failed: {url}: {res.error}")
            return res
        # selector extraction (its own try: so a bad selector doesn't kill the page)
        try:
            ptype = self.runner.detect_page_type(html, url)
            child_links, next_links, lw = self.runner.extract_links_typed(
                html, url, ptype)
            imgs, iw = self.runner.extract_images(html, url)
            res.page_type = ptype
            # child_links = discovery links (depth+1); next_links = pagination
            # (same depth). Keep them separate so next-page links never get
            # folded into the depth+1 bucket (which would re-burn depth).
            res.child_links = child_links
            res.next_links = next_links
            res.image_urls = imgs
            for w in lw + iw:
                if self.on_warning:
                    self.on_warning(f"selector warning @ {url}: {w}")
            # 2026-10-26：通用数据接口分页（offset_json）。
            # 有些站的列表页 HTML 里**一根翻页链接都没有**（无限滚动 / JS），
            # 后续页只能从站点自己的 JSON 接口拿（pornpics 分类页就是
            # `?offset=20&limit=20` 返回 20 条 {g_url} 的列表）。这类站以前
            # 永远只能抓到第一页 ——「最新」不等于「全站」的根因。
            # 规则在 YAML 里声明 paging；这里按声明把后续页的链接追加到
            # same-depth 的 next_links（续页，不烧深度）。
            try:
                _paging = getattr(self.runner.rule, "paging", None) or {}
                if _paging.get("mode") == "offset_json":
                    more = await self._offset_json_links(url, _paging)
                    if more:
                        res.next_links = list(dict.fromkeys(
                            list(res.next_links or []) + more))
                        if self.on_warning:
                            self.on_warning(
                                f"接口分页 @ {url}: 追加 {len(more)} 个后续链接")
            except Exception as e:
                if self.on_warning:
                    self.on_warning(f"接口分页失败 @ {url}: {type(e).__name__}: {e}")
        except SelectorFailure as e:
            res.error = f"selector: {e}"
        return res

    async def _offset_json_links(self, page_url: str, cfg: dict) -> list[str]:
        """按 YAML 的 paging 声明，从站点 JSON 接口翻出后续页的链接。

        约定（全部可配）：
          url         模板，占位 {base}/{offset}/{limit}（默认 <base>?offset=..&limit=..）
          offset_start 第一页之后从多少开始（默认 = limit）
          limit       每页条数（默认 20）
          step        步长（默认 = limit）
          link_key    每条记录里装链接的字段名（默认 g_url）
          max_pages   最多翻多少页（默认 200，防失控）
        停止条件：接口报错 / 返回空 / 条数不足 limit / 全是重复链接。
        """
        import json as _json
        from urllib.parse import urlsplit, urlunsplit

        sp = urlsplit(page_url)
        base = urlunsplit((sp.scheme, sp.netloc, sp.path, "", ""))
        limit = int(cfg.get("limit") or 20)
        step = int(cfg.get("step") or limit)
        off = int(cfg.get("offset_start") or limit)
        key = str(cfg.get("link_key") or "g_url")
        max_pages = int(cfg.get("max_pages") or 200)
        tpl = str(cfg.get("url") or "{base}?offset={offset}&limit={limit}")

        out: list[str] = []
        seen: set[str] = set()
        for _ in range(max_pages):
            if self._stop or (self.max_pages and self.pages >= self.max_pages):
                break
            u = tpl.format(base=base, offset=off, limit=limit)
            try:
                text = await self.f.fetch_text(u)
            except Exception:
                break
            try:
                data = _json.loads(text)
            except Exception:
                break
            items = data if isinstance(data, list) else (
                data.get("data") or data.get("galleries") or [])
            if not items:
                break
            added = 0
            for it in items:
                v = it.get(key) if isinstance(it, dict) else None
                if not v or not isinstance(v, str):
                    continue
                if v.startswith("/"):
                    v = f"{sp.scheme}://{sp.netloc}{v}"
                if v in seen:
                    continue
                seen.add(v)
                out.append(v)
                added += 1
            # 页内条数不足 = 到最后一页；全是旧链接 = 接口在兜圈
            if len(items) < limit or added == 0:
                break
            off += step
        return out
