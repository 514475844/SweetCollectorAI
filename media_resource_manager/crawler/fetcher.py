"""HTTP fetcher built on httpx.

个人自用工具（2026-10-26 起无门禁）：
  * Single, stable User-Agent (no rotation to evade blocks).
  * On 401/403 treat as "access-controlled -> stop, ask human" (no bypass).
  * On 429 honour Retry-After with capped exponential backoff.
  * Per-host delay (min of rule delay, robots Crawl-Delay).
  * Per-task proxy (legal network only); no geo/access evasion features.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

from ..config import AppConfig
from ..utils.url import domain_of, normalize_url
from .robots import (
    RobotsPolicy, build_policy, cached_policy, set_cached_policy, RobotsPolicy,
)


class RobotDisallowed(Exception):
    pass


class AccessBlocked(Exception):
    """401/403/captcha-ish — caller must pause and ask a human, never bypass."""


# ---------- R48-D1：SOCKS 代理支持 ----------
#
# 真机事故（作业单 D，用户第 5 条）：代理填 `socks5://…`，一点「开始任务」
# 整个任务直接崩停，还冒一串英文。根因 = httpx 的 SOCKS 支持是**可选依赖**
# `socksio`（等价于装 `httpx[socks]`），打包环境里根本没有它，
# `AsyncClient(proxy="socks5://…")` 会在**构造时**抛 ImportError，
# 而这条路径上没人 try → 异常一路冒到调度线程，任务死掉。
#
# 三件事必须都做，少一件都不闭环：
#   ① 构造时探测 socksio，缺就**降级成"不用代理"**而不是崩（下面的 `_socks_ok`）；
#   ② 任何 socks 相关异常都翻成**中文**给用户看；
#   ③ 降级后任务照跑（进度不丢、不闪退），只是没走代理。
_SOCKS_SCHEMES = ("socks5://", "socks5h://", "socks4://", "socks4a://")

#: 降级时给用户看的中文提示（作业单指定文案，测试按关键字匹配）
SOCKS_HINT_ZH = "SOCKS 代理需要 socksio 组件，已尝试降级为不使用代理，请检查代理设置"


def is_socks_proxy(url: Optional[str]) -> bool:
    """代理 URL 是不是 SOCKS（决定要不要探测 socksio）。"""
    return bool(url) and str(url).strip().lower().startswith(_SOCKS_SCHEMES)


def socks_available() -> bool:
    """当前运行环境有没有 socksio。独立函数便于 monkeypatch（测试用）。"""
    try:
        import socksio  # noqa: F401
        return True
    except Exception:
        return False


def describe_socks_problem(url: Optional[str]) -> str:
    """SOCKS 代理不可用时的**中文**说明（用户可见）。"""
    kind = "socks5" if not url else str(url).split("://", 1)[0].lower()
    return (f"{SOCKS_HINT_ZH}（当前代理类型 {kind}，"
            f"缺少的组件 socksio 未随程序安装）")


class Fetcher:
    def __init__(self, cfg: AppConfig, proxy: Optional[str] = None,
                 verify_ssl: bool = True, cookie: Optional[str] = None):
        self.cfg = cfg
        self.proxy = proxy
        self.verify_ssl = verify_ssl
        # #52 站点 Cookie / 登录态：原样作为 Cookie 请求头下发（留空=不发送）
        self.cookie = (cookie or "").strip()
        self._last: dict[str, float] = {}
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        # ---- R48-D1：SOCKS 降级 ----
        # `self.proxy` 保持用户填的**原值**（要显示在 UI / 日志里），
        # 真正传给 httpx 的是 `self.effective_proxy`：SOCKS 且 socksio 缺失
        # 时降级成 None（直连），任务照跑。
        self.notice: str = ""          # 中文提示，UI 可直接显示
        self.degraded: bool = False    # 是否真的降级过
        # 2026-10-27（编号 17）：IP/网段过滤。空名单时是个零成本空操作。
        from ..ipfilter import IpFilter
        try:
            self.ip_filter = IpFilter(str(getattr(cfg, "ip_filter_text", "") or "")
                                      if bool(getattr(cfg, "ip_filter_enabled", False))
                                      else "")
        except Exception:
            self.ip_filter = IpFilter("")
        # 2026-10-27（编号 18/20）：页面型请求也要受带宽上限约束。
        # 令牌桶按**本 fetcher 实例**一个（一个任务一个 fetcher），
        # 速率用 callable 以便跨时段边界自动生效。
        from ..rateplan import BandwidthLimiter, effective_kbps
        self._limiter = BandwidthLimiter(lambda: effective_kbps(cfg))
        self._skip_lan = bool(getattr(cfg, "speed_skip_lan", True))
        self.blocked = 0               # 被 IP 过滤挡掉的请求数（日志/统计用）
        proxy = self._apply_proxy_dns(proxy)
        if is_socks_proxy(proxy) and not socks_available():
            self.effective_proxy = None
            self.degraded = True
            self.notice = describe_socks_problem(proxy)
        else:
            self.effective_proxy = proxy
        # ---- 2026-10-28（编号 32/33/34）----
        # 32：源地址绑定（多网卡机器选从哪张网卡出去）。
        #     走代理时源地址由"连代理那条连接"决定 —— httpx 的 local_address
        #     在这种情况下不生效，这是协议决定的，不是我们的 bug；设置页里
        #     写了这条说明，别让人以为"选了没用"。
        self.bind_ip = str(getattr(cfg, "bind_ip", "") or "").strip()
        # 33：证书校验。两档：
        #     · `ssl_verify=False` = 全局不校验（危险，但用户明确要求时有路可走）
        #     · `ssl_insecure_hosts` = 只给这些域放行（自签/内网站的正确做法）
        #     实现上要靠**两个客户端**（httpx 的 verify 是客户端级参数），
        #     按 host 选客户端，见 _client_for()。
        self.verify_ssl = bool(verify_ssl) and bool(
            getattr(cfg, "ssl_verify", True))
        self._insecure_hosts = [
            s.strip().lower().lstrip("*").lstrip(".")
            for s in str(getattr(cfg, "ssl_insecure_hosts", "") or "")
            .replace("\n", ",").replace(" ", ",").split(",")
            if s.strip()
        ]
        # 34：内网地址限制（默认放行 = 不改变既有行为，收紧要靠用户开）
        self._allow_private = bool(getattr(cfg, "allow_private_hosts", True))
        self._client_insecure: Optional[httpx.AsyncClient] = None

    def _is_insecure_host(self, url: str) -> bool:
        """该 URL 的域名是否在"跳过证书校验"名单里（含子域）。"""
        if not self._insecure_hosts:
            return False
        try:
            host = (urlsplit(url).hostname or "").lower()
        except Exception:
            return False
        if not host:
            return False
        for rule in self._insecure_hosts:
            if host == rule or host.endswith("." + rule):
                return True
        return False

    def _want_verify(self, url: str) -> bool:
        return bool(self.verify_ssl) and not self._is_insecure_host(url)

    @property
    def socks_missing(self) -> bool:
        """SOCKS 代理 + socksio 缺失（=本次真的降级了）。"""
        return self.degraded

    def _apply_proxy_dns(self, proxy: Optional[str]) -> Optional[str]:
        """编号 15：``socks5://`` → ``socks5h://``（域名解析也走代理）。

        为什么要这一步：socks5 是**本地解析** DNS —— 国内 DNS 被污染时，
        代理通了但域名解到错误 IP，表现是"用代理反而打不开"。
        socks5h 把域名交给代理解析，这是代理软件里同一个开关的等价物。
        ``cfg.proxy_dns=False`` 时保持原样（用户明确要本地解析）。
        """
        if not proxy:
            return proxy
        try:
            if not bool(getattr(self.cfg, "proxy_dns", True)):
                return proxy
        except Exception:
            pass
        low = str(proxy).lower()
        if low.startswith("socks5://"):
            return "socks5h://" + str(proxy)[len("socks5://"):]
        return proxy

    def _check_ip_filter(self, url: str) -> None:
        """编号 17：命中过滤规则就抛 AccessBlocked（带中文原因）。

        2026-11-18（批1）：先过 ssrf.check_url —— 云元数据/链路本地等保留段
        无条件硬拦（采集链唯一收口点，重定向由 httpx 事件钩子另行兜底）；
        家庭私有网段**放行**，抓 NAS 是正常用法。

        编号 34：`allow_private_hosts=False` 时，内网/本机地址同样直接跳过。
        默认 **True（放行）** —— 默认拦掉会让"抓本地 NAS"的用户升级即失能。
        """
        try:
            from ..ssrf import check_url, SsrfBlocked
            check_url(url)
        except SsrfBlocked as e:
            self.blocked += 1
            raise AccessBlocked(f"已拦截保留地址：{e}")
        except Exception:
            pass
        if not self._allow_private:
            try:
                from ..ipfilter import is_local_host
                host = urlsplit(url).hostname or ""
                if host and is_local_host(host):
                    self.blocked += 1
                    raise AccessBlocked(
                        f"已按设置禁止访问内网/本机地址：{host}"
                        "（想抓内网图库请在「设置 → 网络」里打开「允许抓内网地址」）")
            except AccessBlocked:
                raise
            except Exception:
                pass
        try:
            reason = self.ip_filter.blocked_reason(url)
        except Exception:
            return
        if reason:
            self.blocked += 1
            raise AccessBlocked(reason)

    async def _client_get(self, url: Optional[str] = None) -> httpx.AsyncClient:
        """建 httpx 客户端。**构造失败绝不外抛**（R48-D1）。

        原来这里直接 `httpx.AsyncClient(proxy=self.proxy, …)`，SOCKS 缺
        socksio 时抛 ImportError → 任务崩停。现在：
        ① 已经预降级过（effective_proxy is None）→ 直连；
        ② 仍然失败 → 就地再降级一次并记中文提示，绝不把异常抛给调度线程。

        2026-10-28（编号 33）：传了 url 且该域在"跳过证书校验"名单里 → 用
        **不校验证书**的那个客户端。名单为空时行为与以前逐字节一致（只有
        一个客户端），不会因为加了功能就多开连接池。
        """
        want_verify = self._want_verify(url) if url else bool(self.verify_ssl)
        if url and not want_verify and not bool(self.verify_ssl):
            # 全局关校验：直接走 _client 那条（verify=False 已经建好了）
            want_verify = False
        if not want_verify and url and bool(self.verify_ssl):
            # 只为个别域名放行：单独的客户端（verify=False）
            if self._client_insecure is None:
                self._client_insecure = self._build_client(False)
            return self._client_insecure
        if self._client is None:
            self._client = self._build_client(bool(self.verify_ssl))
        return self._client

    def _build_client(self, verify: bool) -> httpx.AsyncClient:
        """按 (代理, 源地址, 校验) 建一个客户端；socks 失败就地降级。"""

        def _mk(proxy: Optional[str]) -> httpx.AsyncClient:
            kw = dict(verify=verify)

            def _ssrf_hook(request):
                # 批1：重定向的每一跳都会重新走 request 钩子 ——
                # 首查在 _check_ip_filter，这里兜住「跳板转向元数据段」。
                from ..ssrf import check_url, SsrfBlocked
                try:
                    check_url(str(request.url))
                except SsrfBlocked as e:
                    raise AccessBlocked(f"跳转目标属保留地址：{e}")

            try:
                local = self.bind_ip or None
                if local:                       # 编号 32：绑定源地址
                    kw["local_address"] = local
                transport = httpx.AsyncHTTPTransport(proxy=proxy, **kw)
                return httpx.AsyncClient(
                    headers={"User-Agent": self.cfg.user_agent},
                    follow_redirects=True, timeout=self.cfg.timeout,
                    transport=transport, event_hooks={"request": [_ssrf_hook]})
            except TypeError:
                # 极老的 httpx 不认识 local_address —— 丢掉这个参数再试一次
                # （宁可源地址不生效，也不能让任务起不来）
                transport = httpx.AsyncHTTPTransport(proxy=proxy, verify=verify)
                return httpx.AsyncClient(
                    headers={"User-Agent": self.cfg.user_agent},
                    follow_redirects=True, timeout=self.cfg.timeout,
                    transport=transport, event_hooks={"request": [_ssrf_hook]})

        try:
            client = _mk(self.effective_proxy)
            if self.bind_ip and self.effective_proxy:
                # 说清楚：代理链路上源地址由"连代理的那条连接"决定
                self.notice = self.notice or (
                    f"已设置源地址 {self.bind_ip}；走代理时该设置对代理链路不生效")
            return client
        except BaseException as ex:  # noqa: BLE001
            if not is_socks_proxy(self.proxy):
                raise
            # 只有代理相关构造失败才降级；其余异常照旧上抛（别掩盖真 bug）
            self._note_socks_failure(ex)
            self.effective_proxy = None
            return _mk(None)


    def _note_socks_failure(self, ex: BaseException) -> None:
        """把 socks 相关异常翻成中文提示（记进 `self.notice`）。

        原始英文异常（`ImportError: socksio is not installed` 之类）对用户
        毫无意义，作业单明确要求中文。
        """
        raw = f"{type(ex).__name__}: {ex}"
        low = raw.lower()
        if "socksio" in low or "socks" in low or isinstance(ex, ImportError):
            self.notice = describe_socks_problem(self.proxy)
        elif "ssl" in low or "certificate" in low:
            self.notice = (f"代理连接失败（证书校验未通过）：{raw[:120]}；"
                           "已尝试不使用代理继续")
        else:
            self.notice = f"代理连接失败：{raw[:120]}；已尝试不使用代理继续"
        self.degraded = True

    async def close(self):
        for attr in ("_client", "_client_insecure"):
            c = getattr(self, attr, None)
            setattr(self, attr, None)
            if c:
                try:
                    await c.aclose()
                except Exception:
                    pass

    async def _ensure_robots(self, url: str) -> RobotsPolicy:
        host = urlsplit(url).netloc
        p = cached_policy(host)
        if p is not None:
            return p
        robots_url = f"{urlsplit(url).scheme}://{host}/robots.txt"
        try:
            c = await self._client_get(url)
            r = await c.get(robots_url)
            text = r.text if r.status_code == 200 else ""
        except Exception:
            text = ""
        p = build_policy(text, self.cfg.user_agent.split("/")[0])
        set_cached_policy(host, p)
        return p

    async def _throttle(self, host: str, delay: float):
        # 编号 20：内网/本机地址不限速（本地 NAS、127.0.0.1 之类，
        # 限它们既没意义也会让"本地源"看起来坏了）。
        if self._skip_lan:
            try:
                from ..ipfilter import is_local_host
                if is_local_host(host):
                    return
            except Exception:
                pass
        async with self._lock:
            now = time.monotonic()
            wait = delay - (now - self._last.get(host, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[host] = time.monotonic()

    @staticmethod
    def _host_variants(url: str) -> list[str]:
        """同一站点的 www / 无 www 两个变体。

        有些代理节点对 apex 域名解析不稳（502），加 www 或去掉 www 再试一次
        能大幅降低偶发失败；内容完全相同，不算绕过任何访问控制。
        """
        sp = urlsplit(url)
        host = sp.netloc
        if host.startswith("www."):
            alt = host[4:]
        else:
            alt = "www." + host
        alt_url = f"{sp.scheme}://{alt}{sp.path or '/'}"
        if sp.query:
            alt_url += "?" + sp.query
        return [url, alt_url]

    async def _get_stable(self, c: httpx.AsyncClient, url: str,
                          headers: Optional[dict] = None) -> httpx.Response:
        """GET，带 www/apex 变体兜底（仅对网络错误和 502/503/504）。

        ⚠ R48-D2：**首次请求处**也可能炸 socks（构造通过但握手时才发现
        依赖缺失）。这里第一次遇到 socks 类异常时：记中文提示 → 关掉这个
        客户端 → 用无代理重建 → 把当前 URL 再请求一次。
        任务**继续跑**（进度不丢、不闪退），代价只是没走代理。
        """
        variants = self._host_variants(url)
        last_exc: Optional[Exception] = None
        first_exc: Optional[Exception] = None
        for i, u in enumerate(variants):
            try:
                r = await c.get(u, headers=headers or {})
            except BaseException as e:      # noqa: BLE001
                low = f"{type(e).__name__}: {e}".lower()
                if i == 0:
                    # 2026-10-28：**主域名的错才是真错**。
                    # 实测：自签 HTTPS 站 → 主域名抛「证书校验失败」，
                    # 紧接着 www 变体抛「getaddrinfo failed」，循环结束时
                    # 把后者抛出去 → 用户看到"域名解析失败"，跑去查 DNS，
                    # 查一天也查不出是证书问题。所以主域名那次的异常单独留住。
                    first_exc = e
                if "socks" in low or isinstance(e, ImportError):
                    # 就地降级：丢掉带代理的客户端，用直连重试一次
                    self._note_socks_failure(e)
                    await self._reset_client_direct()
                    try:
                        c = await self._client_get(u)
                        r = await c.get(u, headers=headers or {})
                    except BaseException as e2:  # noqa: BLE001
                        last_exc = e2
                        continue
                elif isinstance(e, httpx.HTTPError):
                    last_exc = e
                    continue
                else:
                    raise
            if r.status_code in (401, 403):
                raise AccessBlocked(f"{r.status_code} for {url}")
            if r.status_code in (502, 503, 504) and i == 0 and len(variants) > 1:
                continue          # 换 host 变体再试一次
            return r
        if first_exc is not None:
            raise first_exc
        if last_exc is not None:
            raise last_exc
        return r

    async def _reset_client_direct(self) -> None:
        """丢掉当前客户端，改成**无代理**重建（降级路径专用）。

        2026-10-28（编号 33）起有第二个"不校验证书"的客户端，这里一起丢，
        否则降级后旧的带代理客户端还活着。
        """
        olds = [self._client, self._client_insecure]
        self._client = None
        self._client_insecure = None
        for old in olds:
            if old is not None:
                try:
                    await old.aclose()
                except Exception:
                    pass
        self.effective_proxy = None

    async def fetch_text(self, url: str, delay: Optional[float] = None) -> str:
        delay = delay if delay is not None else self.cfg.delay
        # 2026-10-26：robots 阻断已移除（个人自用软件，用户要求取消所有门禁）。
        # 仍读 robots 的 Crawl-Delay 做礼貌限速——那不是门禁，是网速礼仪。
        self._check_ip_filter(url)                 # 编号 17
        policy = await self._ensure_robots(url)
        effective = max(delay, policy.crawl_delay or 0)
        await self._throttle(urlsplit(url).netloc, effective)
        c = await self._client_get(url)
        hdrs = {"Cookie": self.cookie} if self.cookie else {}
        for attempt in range(self.cfg.retry + 1):
            try:
                r = await self._get_stable(c, url, hdrs)
            except (AccessBlocked, RobotDisallowed):
                raise
            except httpx.HTTPError as e:
                if attempt >= self.cfg.retry:
                    raise
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))
                await asyncio.sleep(min(wait, 120))
                continue
            if r.status_code >= 400:
                if attempt >= self.cfg.retry:
                    raise httpx.HTTPStatusError(
                        f"{r.status_code} {url}", request=r.request, response=r)
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            # 编号 18：按实际收到的字节扣令牌（页面 HTML 也占带宽，
            # 站点把整页塞成几百 KB 时同样会把带宽吃满）。
            try:
                await self._limiter.take(len(r.content or b""))
            except Exception:
                pass
            return r.text
        raise RuntimeError("无法访问该地址")

    async def fetch_bytes(self, url: str, delay: Optional[float] = None,
                           range_start: int = 0) -> tuple[int, bytes]:
        """返回 (status_code, bytes)。

        range_start>0 时带 Range 请求（断点续传用）。若服务器不支持 Range，
        status_code 仍是 200 但返回的是整文件——调用方据此决定是 append 还是重来。
        """
        # 2026-10-26：robots 阻断已移除（同 fetch_text，用户要求取消所有门禁）。
        self._check_ip_filter(url)                 # 编号 17
        policy = await self._ensure_robots(url)
        delay = delay if delay is not None else self.cfg.delay
        effective = max(delay, policy.crawl_delay or 0)
        await self._throttle(urlsplit(url).netloc, effective)
        c = await self._client_get(url)
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if range_start > 0:
            headers["Range"] = f"bytes={range_start}-"
        # 媒体下载：先在 fetcher 内做变体兜底 + 一次重试，成功率显著更高
        r = None
        for attempt in range(2):
            try:
                r = await self._get_stable(c, url, headers)
            except (AccessBlocked, RobotDisallowed):
                raise
            except httpx.HTTPError:
                if attempt >= 1:
                    raise
                await asyncio.sleep(1)
                continue
            break
        if r.status_code in (401, 403):
            raise AccessBlocked(f"{r.status_code} for {url}")
        # 部分内容(206)或整文件(200)都接受；上层按 status 判断断点续传语义
        r.raise_for_status()
        return r.status_code, r.content
