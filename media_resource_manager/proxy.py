"""Proxy management: HTTP/HTTPS/SOCKS5, per-task binding, health checks.

Compliance note (unchanged): proxies here exist ONLY for legitimate network
situations (corporate/school networks, debugging environments). There is no
feature for evading legal restrictions, geo-blocks, or access controls.

Secrets (username/password) are never logged; see `mask()`.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Optional

SUPPORTED_SCHEMES = ("http", "https", "socks5")

PROXY_TABLE = """
CREATE TABLE IF NOT EXISTS proxies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT UNIQUE,
    scheme     TEXT DEFAULT 'http',
    host       TEXT,
    port       INTEGER,
    username   TEXT DEFAULT '',
    password   TEXT DEFAULT '',
    enabled    INTEGER DEFAULT 1,
    priority   INTEGER DEFAULT 0,
    failures   INTEGER DEFAULT 0,
    last_ok    TEXT,
    last_error TEXT,
    latency_ms INTEGER DEFAULT -1
);
"""


@dataclass
class ProxyEntry:
    id: Optional[int] = None
    name: str = ""
    scheme: str = "http"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""
    enabled: bool = True
    priority: int = 0          # lower number = preferred
    failures: int = 0
    last_ok: str = ""
    last_error: str = ""
    latency_ms: int = -1

    def url(self, *, hide_secret: bool = False) -> str:
        auth = ""
        if self.username:
            pwd = "***" if hide_secret else self.password
            auth = f"{self.username}:{pwd}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __str__(self) -> str:          # safe for logs
        return f"{self.name}<{self.mask()}>"

    def mask(self) -> str:
        return self.url(hide_secret=True)

    def healthy(self) -> bool:
        return self.enabled and self.failures < 3

    def validate(self) -> Optional[str]:
        if not self.name:
            return "missing name"
        if self.scheme not in SUPPORTED_SCHEMES:
            return f"unsupported scheme (use {', '.join(SUPPORTED_SCHEMES)})"
        if not self.host or not self.port:
            return "missing host/port"
        return None


class ProxyPool:
    """Stores named proxies in SQLite and picks the best healthy one."""

    def __init__(self, db):
        self.db = db
        db.execute(PROXY_TABLE)

    # ---------- CRUD ----------
    def list(self, only_enabled: bool = True) -> list[ProxyEntry]:
        sql = "SELECT * FROM proxies"
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY priority ASC, failures ASC, id ASC"
        return [self._row(r) for r in self.db.query(sql)]

    @staticmethod
    def _row(r) -> ProxyEntry:
        return ProxyEntry(
            id=r["id"], name=r["name"], scheme=r["scheme"], host=r["host"],
            port=r["port"], username=r["username"], password=r["password"],
            enabled=bool(r["enabled"]), priority=r["priority"],
            failures=r["failures"], last_ok=r["last_ok"] or "",
            last_error=r["last_error"] or "", latency_ms=r["latency_ms"],
        )

    def get(self, pid: int) -> Optional[ProxyEntry]:
        r = self.db.query("SELECT * FROM proxies WHERE id=?", (pid,))
        return self._row(r[0]) if r else None

    def get_by_name(self, name: str) -> Optional[ProxyEntry]:
        r = self.db.query("SELECT * FROM proxies WHERE name=?", (name,))
        return self._row(r[0]) if r else None

    def save(self, e: ProxyEntry) -> int:
        err = e.validate()
        if err:
            raise ValueError(err)
        if e.id is None:
            cur = self.db.execute(
                """INSERT INTO proxies(name, scheme, host, port, username,
                   password, enabled, priority, failures, last_ok, last_error,
                   latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e.name, e.scheme, e.host, e.port, e.username, e.password,
                 int(e.enabled), e.priority, e.failures, e.last_ok,
                 e.last_error, e.latency_ms))
            return int(cur.lastrowid)
        self.db.execute(
            """UPDATE proxies SET name=?, scheme=?, host=?, port=?, username=?,
               password=?, enabled=?, priority=?, failures=?, last_ok=?,
               last_error=?, latency_ms=? WHERE id=?""",
            (e.name, e.scheme, e.host, e.port, e.username, e.password,
             int(e.enabled), e.priority, e.failures, e.last_ok, e.last_error,
             e.latency_ms, e.id))
        return e.id

    def remove(self, pid: int):
        self.db.execute("DELETE FROM proxies WHERE id=?", (pid,))

    # ---------- health ----------
    #
    # ⚠ R48-D3：`check/acheck` 早就有 try/except，但错误文案是**英文原样**
    #   （`ImportError: socksio is not installed` 之类）。作业单要求
    #   「所有用户可见错误必须中文」，代理面板直接把 last_error 显示出来，
    #   所以这里必须**翻译**成中文，不能只记英文。
    #
    # ⚠ 关键区分：**代理健康检查失败绝不能抛到上层**。检查只是"这个代理还
    #   能不能用"的旁路信息，失败只记 last_error 并让检查返回 False。
    #   一旦往上抛，点一下"测试"就会打断正在跑的任务 —— 那才是真事故。
    @staticmethod
    def _zh_error(ex: BaseException) -> str:
        """把 httpx/socksio 的异常翻成中文短句（用户可见文案）。"""
        name = type(ex).__name__
        raw = str(ex)
        # 2026-10-28：**类名也要参与匹配**。httpx 的 ConnectError 消息是
        # `[WinError 10061] 由于目标计算机积极拒绝，无法连接。` —— 里面没有
        # "connect" 这个词，只有类名有，于是原来一直落到兜底的
        # 「代理不可用（ConnectError）」，用户看不出到底是端口没开还是密码错了。
        low = f"{name} {raw}".lower()
        if isinstance(ex, ImportError) or "socksio" in low or "socks" in low:
            return ("SOCKS 代理缺少 socksio 组件（打包时未装 httpx[socks]）；"
                    "请改用 HTTP 代理，或重新安装 httpx[socks]")
        if isinstance(ex, TimeoutError) or "timeout" in low or "timed out" in low:
            return "连接代理超时"
        if "connect" in low or "refused" in low or "unreachable" in low:
            return "无法连接代理（地址不通或端口未开放）"
        if "certificate" in low or "ssl" in low:
            return "代理 SSL 证书校验失败"
        if isinstance(ex, UnicodeDecodeError) or "decode" in low:
            return "代理返回的数据编码无法解析"
        return f"代理不可用（{name}）"

    def _record(self, e: ProxyEntry, ok: bool, latency_ms: int, err: str = ""):
        if ok:
            e.failures = 0
            e.last_ok = time.strftime("%Y-%m-%d %H:%M:%S")
            e.latency_ms = latency_ms
            e.last_error = ""
        else:
            e.failures += 1
            e.last_error = err[:200]
        try:
            self.save(e)
        except Exception:
            pass

    def check(self, e: ProxyEntry, test_url: str = "https://example.com",
              timeout: float = 8.0) -> tuple[bool, int, str]:
        """Sync health probe. Returns (ok, latency_ms, error)。

        ⚠ 任何异常都在这里吞掉并转中文，绝不外抛（R48-D3）。
        """
        import httpx
        t0 = time.monotonic()
        try:
            with httpx.Client(proxy=e.url(), timeout=timeout,
                              follow_redirects=True) as c:
                r = c.get(test_url)
                lat = int((time.monotonic() - t0) * 1000)
                ok = r.status_code < 500
                err = "" if ok else f"代理返回状态 {r.status_code}"
        except BaseException as ex:          # noqa: BLE001 —— 兜底，不外抛
            lat = int((time.monotonic() - t0) * 1000)
            ok, err = False, self._zh_error(ex)
        self._record(e, ok, lat, err)
        return ok, lat, err

    async def acheck(self, e: ProxyEntry, test_url: str = "https://example.com",
                     timeout: float = 8.0) -> tuple[bool, int, str]:
        """异步版健康检查。语义与 `check` 完全一致：吞异常 + 中文 + 不外抛。"""
        import httpx
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(proxy=e.url(), timeout=timeout,
                                         follow_redirects=True) as c:
                r = await c.get(test_url)
                lat = int((time.monotonic() - t0) * 1000)
                ok = r.status_code < 500
                err = "" if ok else f"代理返回状态 {r.status_code}"
        except BaseException as ex:          # noqa: BLE001
            lat = int((time.monotonic() - t0) * 1000)
            ok, err = False, self._zh_error(ex)
        self._record(e, ok, lat, err)
        return ok, lat, err

    def check_all(self, test_url: str = "https://example.com") -> list[tuple[str, bool, int, str]]:
        out = []
        for e in self.list():
            ok, lat, err = self.check(e, test_url)
            out.append((e.name, ok, lat, err))
        return out

    # ---------- selection / failover ----------
    def pick(self) -> Optional[ProxyEntry]:
        healthy = [e for e in self.list() if e.healthy()]
        if not healthy:
            return None
        # prefer: fewest failures, then lowest latency, then priority order
        healthy.sort(key=lambda e: (e.failures, max(e.latency_ms, 0), e.priority, e.id or 0))
        return healthy[0]

    def report_failure(self, e: ProxyEntry):
        self._record(e, False, e.latency_ms, "task failure")

    def rotate(self, current: ProxyEntry) -> Optional[ProxyEntry]:
        """Return a different healthy proxy (failover), or None."""
        self.report_failure(current)
        for cand in self.list():
            if cand.id != current.id and cand.healthy():
                return cand
        return None

    # ---------- helper for tasks ----------
    def resolve(self, proxy_id: Optional[int] = None,
                custom: Optional[str] = None,
                fallback: Optional[str] = None) -> Optional[str]:
        """Resolve the final httpx proxy URL for a task."""
        if custom:
            return custom
        if proxy_id:
            e = self.get(proxy_id)
            if e and e.enabled:
                return e.url()
        # auto: use best healthy proxy from the pool
        e = self.pick()
        if e:
            return e.url()
        return fallback
