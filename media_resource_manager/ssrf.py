# -*- coding: utf-8 -*-
"""SSRF 自保（2026-11-18 批1）。

个人自用软件的定位：这不是合规门禁，是防止「采集到的网页里塞了
恶意链接，软件替你去打云元数据/本机服务」。所以：

* 硬拦：169.254.0.0/16（云厂商元数据）、0.0.0.0、::/128、
  fe80::/10、以及所有 link-local —— 这些对家庭网络采图毫无用处；
* 放行：127.0.0.1、192.168.*、10.*、172.16-31.* —— 用户的 NAS、
  软路由就是采集目标，历史版本还专门给内网免限速（编号 20），
  这里绝不能反向拦死；
* 重定向逐跳校验：httpx 自动跟跳时，第二跳指向元数据段也要拦。

只在采集链生效（fetcher/downloader），WebDAV/SMB/FTP 导入链本就连的
就是内网设备，不经过这里。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

# 明确允许直连的私有/回环段（NAS 采集场景）
_ALLOWED_NETS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)
# 无论解析结果如何都要拦的段
_BLOCKED_NETS = (
    ipaddress.ip_network("169.254.0.0/16"),     # 云元数据 / link-local
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),           # 组播
)


class SsrfBlocked(Exception):
    """目标解析到云元数据/保留段。和 AccessBlocked 不同，这不是墙，
    是地址本身不该被软件访问。"""


def _check_ip(ip: str) -> None:
    a = ipaddress.ip_address(ip)
    for n in _BLOCKED_NETS:
        if a in n:
            raise SsrfBlocked(f"目标地址 {ip} 属于保留段 {n}，已拦截")
    # 其余公网地址与 _ALLOWED_NETS 里的私有地址都放行；
    # 其他特殊段（如 100.64/10 CGNAT）不拦——家庭宽带常见，误伤大于收益。


def check_url(url: str) -> None:
    """解析主机名并校验所有 A/AAAA 结果。解析失败不拦（交给正常
    网络错误路径，别在这里把报错变复杂）。"""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return
    if not host:
        return
    try:
        _check_ip(host)          # 字面 IP（含 IPv6 带括号已被 urlsplit 去掉）
        return
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return
    for _, _, _, _, sa in infos:
        try:
            _check_ip(sa[0])
        except ValueError:
            continue
