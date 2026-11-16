"""Adapter rule model + loader/validation.

Rules are user-supplied JSON/YAML. This module only loads & validates them.
It does NOT contain any built-in site rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..config import load_rule_file
from ..utils.url import domain_of

# Selector kinds we support.
SELECTOR_KINDS = ("css", "xpath", "regex")

# R152-C1：规则名白名单，与 Web 端导入闸（webui.py _ADAPTER_NAME_OK）同源。
# ⚠ 为什么必须有：**规则名会被拼进文件路径**（`<rules_dir>/<name>.yaml`，
# engine._resolve_adapter）和**下载目录名**（engine._run_via_gallery_dl 的
# `dest = output_dir / rule.name`）。一条 `../../Desktop/evil` 就能把规则读到
# 用户目录外、把图下到图库外。宁可让用户改个名字，也不能写错一次就丢数据。
ADAPTER_NAME_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def sanitize_adapter_name(name: str, fallback: str = "adapter") -> str:
    """把规则名收敛到白名单字符集；不合法整体回退 fallback（通常是文件名 stem）。

    用于 load_adapter 出口：文件内容是用户自己粘的，name 字段可能带中文或
    路径字符。这里不做"替换字符"式的清洗——那会把 `../../x` 洗成 `..x`
    这种仍可能惹事的名字——直接整体回退到安全的文件名，语义可预期。
    """
    n = (name or "").strip()
    if ADAPTER_NAME_OK.match(n):
        return n
    fb = (fallback or "").strip()
    return fb if ADAPTER_NAME_OK.match(fb) else "adapter"


@dataclass
class AdapterRule:
    name: str
    base_url: str
    allowed_domains: list[str] = field(default_factory=list)
    entry_urls: list[str] = field(default_factory=list)
    list_page_selectors: dict = field(default_factory=dict)
    album_link_selectors: dict = field(default_factory=dict)
    image_page_selectors: dict = field(default_factory=dict)
    image_link_selectors: list[dict] = field(default_factory=list)
    original_image_attrs: list[str] = field(default_factory=lambda: ["src", "data-src", "data-original"])
    # 把缩略图 URL 换成原图：如 [{"from": "t1.site.com", "to": "s2.site.com"}]
    image_url_replace: list[dict] = field(default_factory=list)
    next_page_selectors: dict = field(default_factory=dict)
    # 通用数据接口分页（2026-10-26）：很多站的列表/分类页支持
    # `?offset=N&limit=M` 返回 JSON（pornpics 就是），HTML 里没有任何
    # 翻页链接，选择器抓不到后续页。这里用 YAML 声明接口形状：
    #   paging: {mode: offset_json, url: '{base}?offset={offset}&limit={limit}',
    #            offset_start: 20, limit: 20, link_key: g_url, max_pages: 500}
    paging: dict = field(default_factory=dict)
    # 站点级 Cookie / UA（R252）。很多站在「有登录态」时才给图，而每个任务
    # 单独填一次 Cookie 极其难用 —— 写进适配器就只填一次，这个站以后所有
    # 任务自动带上。空值 = 不改变既有行为（沿用任务/全局）。
    # ⚠ 与「按域名的站点 Cookie」是两套：那边是插件/导入推过来的联动态，
    #   这边是规则自带的固定值。合并优先级：任务 > 适配器 > 域名存档 > 全局。
    cookie: str = ""
    user_agent: str = ""
    max_depth: int = 3
    max_pages: int = 0      # 翻页/ BFS 页数上限（0=不限）；规则可单独收紧
    delay: float = 1.0
    concurrency: int = 4
    proxy: Optional[str] = None
    note: str = ""

    def domains(self) -> set[str]:
        ds = {domain_of(self.base_url)} | set(self.allowed_domains)
        return {d for d in ds if d}


def _clean_cookie(v) -> str:
    """只保留一行、去掉控制字符（防请求头注入）。"""
    return re.sub(r"[\r\n\x00]", "", str(v or "")).strip()


def _clean_ua(v) -> str:
    return re.sub(r"[\r\n\x00]", " ", str(v or "")).strip()


def healthy(rule: AdapterRule) -> bool:
    """能否直接用于抓取（三项必备字段齐全）。

    与 ``gui/adapter_checkup`` 和网页端适配器表的判据同一份口径：
    缺 base_url/entry_urls、缺 allowed_domains、缺图片选择器 —— 任一缺失
    拿来跑只会产出 0 张图，然后用户以为"这个站抓不了"。
    """
    if not rule:
        return False
    has_entry = bool(rule.base_url or rule.entry_urls)
    return bool(has_entry and rule.allowed_domains and rule.image_link_selectors)


def match_adapter(rules_dir, urls, healthy_only: bool = True):
    """在规则目录里找「域名能对上这些网址」的适配器（R252）。

    为什么要它：用户的自然输入就是「粘一个网址」，不该逼他先去适配器页
    挑一条规则。这里按域名匹配，让「只粘网址」真的能用。

    ⚠ 三条边界（都来自踩过的坑）：
      · **只做域名匹配，不做内容嗅探** —— 猜错的代价是 0 图，比"抓错站"轻；
      · ``healthy_only`` 默认 True：缺图片选择器的半成品规则拿来跑只会 0 图，
        那比"没匹配上"更让人困惑；
      · 多个规则同时命中时取**域名最长**的那个（``a.b.com`` 优先于 ``b.com``）。
    """
    d = Path(rules_dir)
    if not d.is_dir():
        return None
    hosts = set()
    for u in urls or []:
        try:
            h = domain_of(str(u))
        except Exception:
            h = ""
        if h:
            hosts.add(h.lower())
    if not hosts:
        return None
    best, best_len = None, -1
    for f in sorted([*d.glob("*.yaml"), *d.glob("*.yml"), *d.glob("*.json")]):
        try:
            rule = load_adapter(f)
        except Exception:
            continue          # 单个坏规则不能拖累自动匹配
        if healthy_only and not healthy(rule):
            continue
        for dom in rule.domains():
            dl = str(dom).lower().lstrip(".")
            for h in hosts:
                # 允许 rule 写主域（b.com）匹配子域（a.b.com），不允许反向
                if h == dl or h.endswith("." + dl):
                    if len(dl) > best_len:
                        best, best_len = rule, len(dl)
    return best


def load_adapter(path: Path) -> AdapterRule:
    raw = load_rule_file(path)
    if not isinstance(raw, dict):
        raise ValueError("适配器规则文件必须是映射/对象（YAML 顶层需为键值对）")
    # R152-C1：出口统一 sanitize。raw 的 name 是用户粘进文件里的，可能带
    # 路径穿越字符；回退到文件名 stem（web 导入闸保证 rules_dir 里的文件名
    # 本身就是白名单安全的；手工放进去的坏文件名也会被二次收敛）。
    safe_name = sanitize_adapter_name(
        raw.get("name") or "", sanitize_adapter_name(path.stem))
    rule = AdapterRule(
        name=safe_name,
        base_url=raw.get("base_url", ""),
        allowed_domains=raw.get("allowed_domains") or [],
        entry_urls=raw.get("entry_urls") or [],
        list_page_selectors=raw.get("list_page_selectors") or {},
        album_link_selectors=raw.get("album_link_selectors") or {},
        image_page_selectors=raw.get("image_page_selectors") or {},
        image_link_selectors=raw.get("image_link_selectors") or [],
        original_image_attrs=raw.get("original_image_attrs")
        or ["src", "data-src", "data-original"],
        image_url_replace=raw.get("image_url_replace") or [],
        next_page_selectors=raw.get("next_page_selectors") or {},
        paging=raw.get("paging") or {},
        max_depth=raw.get("max_depth", 3),
        max_pages=int(raw.get("max_pages", 0) or 0),
        delay=raw.get("delay", 1.0),
        concurrency=raw.get("concurrency", 4),
        proxy=raw.get("proxy"),
        # 规则里的 Cookie/UA 同样要清洗：这是用户粘进 YAML 的文本，
        # 带换行就是请求头注入（与任务级同一个清洗函数，别自己再写一份）。
        cookie=_clean_cookie(raw.get("cookie")),
        user_agent=_clean_ua(raw.get("user_agent")),
        note=raw.get("note", ""),
    )
    if not rule.base_url and not rule.entry_urls:
        raise ValueError("适配器必须定义 base_url 或 entry_urls（入口网址）")
    if not rule.allowed_domains and rule.base_url:
        rule.allowed_domains = [domain_of(rule.base_url)]
    return rule
