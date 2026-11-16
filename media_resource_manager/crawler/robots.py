"""Minimal, dependency-free robots.txt parser.

Supports grouped User-agent, Allow/Disallow, and '*' globs converted to
regex. Used to decide whether a path may be fetched *before* any request.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_ROBOTS_CACHE: dict[str, "RobotsPolicy"] = {}


def _glob_to_regex(glob: str) -> re.Pattern:
    esc = re.escape(glob)
    esc = esc.replace(r"\*", ".*")
    if glob.endswith("$"):
        esc = esc[:-2] + "$"
    return re.compile("^" + esc)


class RobotsPolicy:
    def __init__(self, text: str, agent: str = "*"):
        self.agent = agent
        self.groups: dict[str, list[tuple[bool, re.Pattern]]] = {}
        self.rules: list[tuple[bool, re.Pattern]] = []
        self.crawl_delay: float | None = None
        self._parse(text or "")

    def _parse(self, text: str):
        cur: list[tuple[bool, re.Pattern]] = []
        declared = False  # 是否已声明过 user-agent 组（空组也算已声明）
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "user-agent":
                if val:
                    cur = self.groups.setdefault(val.lower(), [])
                    declared = True
            elif key in ("disallow", "allow"):
                if not declared:
                    cur = self.groups.setdefault("*", [])
                allow = key == "allow"
                # F4：空 Allow 不是「只放行空路径」的 ^$ 规则，而是无操作
                # （RFC 9309：Allow 空值等价于没有这条规则）。旧代码编译成
                # ^$ 会在「末条匹配」语义下把真实路径误判成禁访。
                if val == "":
                    continue
                try:
                    cur.append((allow, _glob_to_regex(val)))
                except re.error:
                    continue
            elif key == "crawl-delay" and val:
                try:
                    self.crawl_delay = float(val)
                except ValueError:
                    pass
        rules = list(self.groups.get(self.agent.lower(), []))
        if self.agent.lower() != "*":
            rules += self.groups.get("*", [])
        self.rules = rules

    def allowed(self, path: str) -> bool:
        """F4：最长匹配优先（RFC 9309「最具体规则胜出」），不再末条匹配。

        旧实现是「最后一条匹配的规则说了算」，``Disallow: /a`` 之后写
        ``Allow: /a/b`` 时路径 /a/b/c 会被 Allow 放行——看似对；但规则
        顺序反过来（Allow 在前）同样路径就被误禁。按匹配长度取最具体的
        规则，同长时 Allow 优先（与主流实现一致）。
        """
        best_len = -1
        allowed = True
        for is_allow, pat in self.rules:
            m = pat.search(path)
            if not m:
                continue
            n = len(m.group(0))
            if n > best_len or (n == best_len and is_allow):
                best_len = n
                allowed = is_allow
        return allowed


def build_policy(text: str, agent: str = "*") -> RobotsPolicy:
    return RobotsPolicy(text, agent)


def cached_policy(host: str) -> RobotsPolicy | None:
    return _ROBOTS_CACHE.get(host)


def set_cached_policy(host: str, policy: RobotsPolicy) -> None:
    _ROBOTS_CACHE[host] = policy
