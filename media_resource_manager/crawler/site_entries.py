"""站点入口映射与形状提示（2026-10-26，修「裸首页被判不会抓」的体验 bug）。

背景（真机事故）：
  用户把任务网址填成裸首页（``https://www.pornpics.com/``）——这是表达
  「我要整个站」最自然的方式。但 gallery-dl 的提取器只认**特定 URL 形状**
  （``/recent/``、``/tags/词/``、``/galleries/ID/`` …），裸首页匹配不上；
  引擎于是静默退回自研爬虫，而部分站对无 Cookie 的普通请求只回 JS 空壳，
  结果 0 图、报错文案还说「站点可能拒绝了抓取」——完全是甩锅。

本模块做三件事：
  1. ``map_entry()``：把「裸首页」或「gallery-dl 不认的裸列表页」翻译成该站
     的全站入口（pornpics→/recent/、danbooru→/explore/posts/popular …）；
  2. ``supported_shapes()``：列出该站在 gallery-dl 里**真正支持的 URL 形状**
     （直接读提取器的 example，别让用户猜）；
  3. 两者都只在 gallery-dl 已安装时生效；纯函数、离线、不联网。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlsplit, urlunsplit


# ── 映射表 ──────────────────────────────────────────────────────────
# key = 小写域名（含 www 与不含都要能命中，见 _host_key）
# value = 全站入口 URL 模板；可用 {year}/{month} 占位（运行时填当期）
#
# 收录标准（宽松但有据）：
#   · 该站 gallery-dl 有提取器；
#   · 用户最可能填的「裸首页/裸列表」形状它不认；
#   · 映射目标是**全站维度**（最新/热门/当月），不是单个标签或相册。
HOMEPAGE_MAP: dict[str, str] = {
    "danbooru.donmai.us": "https://danbooru.donmai.us/explore/posts/popular",
    "konachan.com": ("https://konachan.com/post/popular_by_month"
                     "?year={year}&month={month}"),
    "yande.re": ("https://yande.re/post/popular_by_month"
                 "?year={year}&month={month}"),
}

# 裸列表页（形状不对但意图明确）→ 全站入口。
# key = (域名, 规范化 path)；value 同上。
SHAPE_MAP: dict[tuple[str, str], str] = {
    ("danbooru.donmai.us", "/posts"): HOMEPAGE_MAP["danbooru.donmai.us"],
    ("konachan.com", "/post"): HOMEPAGE_MAP["konachan.com"],
    ("yande.re", "/post"): HOMEPAGE_MAP["yande.re"],
}

# ── UA 例外表 ────────────────────────────────────────────────────────
# 这些站点**不要**给 gallery-dl 覆盖 UA：站方明确允许 gallery-dl 的默认 UA，
# 反而把「浏览器 UA + 非浏览器行为」当可疑请求 → Cloudflare 403。
# （2026-10-26 真机实测 danbooru：默认 UA 正常出图，Chrome UA 直接 403。）
NO_UA_OVERRIDE: set[str] = {"danbooru.donmai.us"}


def ua_override_allowed(url: str) -> bool:
    """该 URL 是否允许覆盖 gallery-dl 的 UA（False = 用 gallery-dl 默认）。"""
    host = _host_key(url)
    return host not in NO_UA_OVERRIDE


def _host_key(url: str) -> str:
    try:
        host = (urlsplit(url).netloc or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _norm_path(url: str) -> str:
    try:
        p = urlsplit(url).path or "/"
    except Exception:
        return "/"
    if not p.startswith("/"):
        p = "/" + p
    if len(p) > 1 and p.endswith("/"):
        p = p[:-1]          # 去尾斜杠便于比较
    return p or "/"


def _fill(tpl: str) -> str:
    import datetime
    now = datetime.datetime.now()
    try:
        return tpl.format(year=now.year, month=now.month)
    except Exception:
        return tpl


def map_entry(url: str) -> Optional[str]:
    """裸首页/裸列表 → 全站入口 URL；不需要映射时返回 None。

    判定很保守：只映射**明确等于首页或裸列表**的 URL —— 用户已经填了
    ``/tags/xxx/``、``/galleries/123/`` 这类具体形状时一律不动。
    """
    if not url:
        return None
    host = _host_key(url)
    if not host:
        return None
    try:
        _sp = urlsplit(url)
    except Exception:
        return None
    # ⚠ 带查询参数的一律不动：/posts?tags=bikini、/?q=xxx 本身就是 gallery-dl
    # 认的合法形状，按 path 粗判会把它们误映射（本模块首个版本的 bug）。
    if _sp.query:
        return None
    path = _norm_path(url)
    # 1) 裸列表页精确匹配
    tpl = SHAPE_MAP.get((host, path))
    if tpl:
        mapped = _fill(tpl)
        return None if mapped == url else mapped
    # 2) 裸首页（path 为 "/" 或空）
    if path == "/":
        tpl = HOMEPAGE_MAP.get(host)
        if tpl:
            mapped = _fill(tpl)
            return None if mapped == url else mapped
    return None


def supported_shapes(url: str, limit: int = 6) -> list[str]:
    """该站在 gallery-dl 里支持的 URL 形状（用它自带的 example，离线）。"""
    if not url:
        return []
    try:
        from gallery_dl import extractor
    except Exception:
        return []
    host = _host_key(url)
    if not host:
        return []
    # 域名主段（danbooru / konachan / pornpics …）：booru 系提取器 root 常为空，
    # 只能靠 pattern 文本里是否出现该主段来认领。
    key = host.split(".")[0]
    out: list[str] = []
    seen: set[str] = set()
    for cls in extractor.extractors():
        try:
            pat = getattr(getattr(cls, "pattern", None), "pattern", "") or ""
            root = getattr(cls, "root", "") or ""
            ex = getattr(cls, "example", "") or ""
        except Exception:
            continue
        if not ex:
            continue
        if not (host in root.lower() or key in pat.lower()):
            continue
        # example 里的占位词（QUERY/TAG/ID/TITLE…）保留——它们是形状说明的一部分
        if ex in seen:
            continue
        seen.add(ex)
        out.append(ex)
        if len(out) >= limit:
            break
    return out
