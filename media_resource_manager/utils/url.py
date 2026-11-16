"""URL helpers: normalization, same-domain check, allow/deny regex."""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qs, urlencode

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "spm", "from",
    "cmpid", "_t", "t",
}


def normalize_url(url: str, base: str | None = None) -> str:
    """Absolute-ify and strip tracking params, fragments, default ports."""
    if base:
        url = urljoin(base, url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        # Only http(s) is supported; reject others defensively.
        return ""
    q = parse_qs(parts.query, keep_blank_values=False)
    q = {k: v for k, v in q.items() if k.lower() not in _TRACKING_PARAMS}
    query = urlencode(q, doseq=True)
    # 注意：保留 www. 前缀！有些站点 www 与 apex 走不同的 CDN/代理线路，
    # 剥掉 www 会导致经代理抓取时整站 502（pictoa 实测踩坑）。
    # 域名归一化判断由 same_domain/domain_of 单独处理。
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    # collapse duplicate slashes (path-only, never touch auth)
    path = re.sub(r"/{2,}", "/", path)
    return urlunsplit((parts.scheme, netloc.lower(), path, query, ""))


def _host(a: str) -> str:
    try:
        h = urlsplit(a).hostname or ""
    except Exception:
        return ""
    if h.startswith("www."):
        h = h[4:]
    return h.lower()


def same_domain(a: str, b: str) -> bool:
    da, db = _host(a), _host(b)
    if not da or not db:
        return False
    return da == db or da.endswith("." + db) or db.endswith("." + da)


def allowed_by_regex(url: str, allow: list[str] | None, deny: list[str] | None) -> bool:
    """R152-C8：用户自填正则可能写错（`[`、`w>=abc` 之类）。

    原来 re.search 直接抛 re.error 冒到 discovery/worker，整条采集链路
    带病运行。现在：**坏正则按"不存在"跳过**（deny 坏→不拦，allow 坏→
    不算命中；allow 全坏→等价于没配 allow，放行）。宁松勿紧：正则写错
    不该让任务静默停摆，提示由任务设置侧校验负责（后续轮次）。
    """
    def _hit(patterns) -> bool:
        for pat in patterns or []:
            try:
                if re.search(pat, url):
                    return True
            except re.error:
                continue
        return False

    if deny and _hit(deny):
        return False
    if allow:
        # allow 全坏 → 等价于没配 allow（放行）：用户正则写错不该让
        # 整条采集静默停摆；有合法模式时只按合法模式判。
        valid = [p for p in allow if _compilable(p)]
        if not valid:
            return True
        return _hit(valid)
    return True


def _compilable(pat) -> bool:
    try:
        re.compile(pat)
        return True
    except re.error:
        return False


def domain_of(url: str) -> str:
    """Host name without scheme / www / port, e.g. 'example.com'."""
    return _host(url)
