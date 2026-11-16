"""IP 过滤（编号 17）：抓取时跳过被屏蔽的主机 / 网段。

用途（用户视角）：
* 某个镜像站/广告域一直返回垃圾图，我不想再碰它；
* 站点把图片挂在一个已知的恶意 CDN 上；
* 内网地址被误抓（配合编号 34 的内网限制）。

规则格式（一行一条，`#` 开头是注释）：
    1.2.3.4            单个地址
    1.2.3.0/24         网段（支持 IPv4 CIDR）
    2001:db8::/32      IPv6 网段
    example.com        域名（含所有子域）

设计取舍：
* **域名规则匹配 host 字符串**，不解析（快且稳定）；
* **IP/CIDR 规则需要解析**，结果按 host 缓存 10 分钟（否则每次请求都查 DNS，
  等于给每张图多付一次解析开销）；
* 解析失败 → **放行**并记原因。过滤是"少抓点垃圾"，不能因为某个 DNS 抖了
  就把正常站点全拦死（与限速同一条设计原则：优化项绝不阻断主流程）。
"""
from __future__ import annotations

import ipaddress
import socket
import time
from typing import Optional
from urllib.parse import urlsplit

#: DNS 结果缓存秒数（站点 IP 变动不频繁，10 分钟足够）
DNS_TTL = 600.0


def _iter_lines(text: str):
    for raw in str(text or "").splitlines():
        s = raw.split("#", 1)[0].strip()
        if s:
            yield s


class IpFilter:
    def __init__(self, text: str = "") -> None:
        self.nets: list = []        # ipaddress 网络对象
        self.hosts: list[str] = []  # 域名规则（小写）
        self.invalid: list[str] = []
        self._dns: dict[str, tuple[float, list]] = {}
        for line in _iter_lines(text):
            self._add(line)

    def _add(self, item: str) -> None:
        low = item.lower()
        # 先当网段/单 IP 解析；失败再当域名
        try:
            self.nets.append(ipaddress.ip_network(item, strict=False))
            return
        except ValueError:
            pass
        if any(ch.isalpha() for ch in low) or "." in low:
            self.hosts.append(low.lstrip("*").lstrip("."))
        else:
            self.invalid.append(item)

    @property
    def empty(self) -> bool:
        return not self.nets and not self.hosts

    def _host_blocked(self, host: str) -> bool:
        h = (host or "").lower().split(":")[0]
        for rule in self.hosts:
            if h == rule or h.endswith("." + rule):
                return True
        return False

    def _resolve(self, host: str) -> list:
        now = time.monotonic()
        hit = self._dns.get(host)
        if hit and now - hit[0] < DNS_TTL:
            return hit[1]
        try:
            infos = socket.getaddrinfo(host, None)
            ips = []
            for fam, _t, _p, _c, sockaddr in infos:
                ip = sockaddr[0]
                if ip not in ips:
                    ips.append(ip)
        except Exception:
            ips = []
        self._dns[host] = (now, ips)
        return ips

    def blocked_reason(self, url: str) -> Optional[str]:
        """被屏蔽则返回中文原因，放行返回 None。"""
        if self.empty:
            return None
        try:
            host = urlsplit(url).netloc.split("@")[-1]
            if host.startswith("["):                    # IPv6 字面量
                host = host[1:].split("]")[0]
            else:
                host = host.split(":")[0]
        except Exception:
            return None
        if not host:
            return None
        if self._host_blocked(host):
            return f"域名被 IP 过滤规则屏蔽：{host}"

        # 字面量 IP 直接判；域名才需要解析
        try:
            ip = ipaddress.ip_address(host)
            addrs = [str(ip)]
        except ValueError:
            if not self.nets:
                return None                              # 只有域名规则时不必解析
            addrs = self._resolve(host)
        for a in addrs:
            try:
                ip = ipaddress.ip_address(a)
            except ValueError:
                continue
            for net in self.nets:
                try:
                    if ip in net:
                        return f"地址被 IP 过滤规则屏蔽：{host} → {a}（命中 {net}）"
                except TypeError:
                    continue                             # v4/v6 混比，跳过
        return None


#: 内网 / 本机地址判断（编号 20 与 34 共用同一口径，别各写一份）
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_local_host(host: str) -> bool:
    """host 是否属于内网/本机（限速豁免、内网限制都用它）。"""
    h = (host or "").lower().split(":")[0].strip("[]")
    if not h:
        return False
    if h in _LOOPBACK_NAMES or h.endswith(".local") or h.endswith(".lan"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)
