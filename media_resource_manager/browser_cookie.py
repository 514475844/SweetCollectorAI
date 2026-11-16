"""Harvest a site cookie by driving the USER's real browser.

Design: light, no bundled Chromium.
  * Prefer Playwright ``channel="chrome"`` — reuses the system Chrome install,
    so nothing extra is downloaded.
  * If Playwright or a usable browser is unavailable, raise
    ``CookieHarvestUnavailable``; the GUI then falls back to a manual paste
    dialog (open the site in the default browser + let the user paste
    ``document.cookie``). Either way the result is stored per-domain.

The browser is opened HEADFUL (visible) so the user can solve the Cloudflare
challenge / log in. We poll the cookie jar until ``cf_clearance`` shows up (or
a timeout elapses), then return the full Cookie header.
"""
from __future__ import annotations

import asyncio
from typing import Optional


class CookieHarvestUnavailable(Exception):
    """Raised when automatic browser cookie harvest cannot run."""


def _build_header(cookies: list[dict]) -> str:
    parts = []
    for c in cookies:
        name = c.get("name")
        if not name:
            continue
        parts.append(f"{name}={c.get('value', '')}")
    return "; ".join(parts)


def _proxy_arg(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    return {"server": proxy}


async def harvest_cookie_async(url: str,
                               proxy: Optional[str] = None,
                               timeout: int = 180) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:  # playwright not installed -> GUI falls back
        raise CookieHarvestUnavailable(
            "未安装 playwright（自动获取 Cookie 需要它）。可手动复制，或先 "
            "pip install playwright 并用系统 Chrome 自动获取。") from e

    px = _proxy_arg(proxy)
    try:
        async with async_playwright() as p:
            # 1) try system Chrome (no download); 2) fall back to any browser
            try:
                browser = await p.chromium.launch(
                    channel="chrome", headless=False, proxy=px)
            except Exception:
                browser = await p.chromium.launch(headless=False, proxy=px)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(
                    url, wait_until="domcontentloaded",
                    timeout=min(timeout, 60) * 1000)
            except Exception:
                # site may already be partly loaded; keep polling cookies
                pass

            waited = 0
            while waited < timeout:
                try:
                    cookies = await context.cookies()
                except Exception:
                    cookies = []
                # cf_clearance is the Cloudflare "passed the challenge" cookie.
                if any(c.get("name") == "cf_clearance" for c in cookies):
                    break
                await asyncio.sleep(2)
                waited += 2

            cookies = await context.cookies()
            await browser.close()
            if not cookies:
                raise CookieHarvestUnavailable(
                    "浏览器未读到任何 Cookie（页面可能没加载出来）。")
            return _build_header(cookies)
    except CookieHarvestUnavailable:
        raise
    except Exception as e:  # launch failures, missing browser, etc.
        raise CookieHarvestUnavailable(f"浏览器启动/读取失败：{e}") from e


def harvest_cookie(url: str,
                   proxy: Optional[str] = None,
                   timeout: int = 180) -> str:
    """Synchronous wrapper for ``harvest_cookie_async`` (run in a worker)."""
    return asyncio.run(harvest_cookie_async(url, proxy, timeout))
