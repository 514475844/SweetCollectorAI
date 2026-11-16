"""本机网卡 / 源地址枚举（编号 32：绑定网卡 / 源 IP 的配套）。

为什么需要它：设置里让用户"选从哪张网卡出去"，就得把可选地址列出来给人选。
手打 IP 是最容易打错的一类配置（错一位 → 所有请求都超时，而错误信息
完全看不出是"源地址写错了"）。

实现走 **ctypes 调 Windows 的 GetAdaptersAddresses**（不引第三方库）：
* 只解析结构体前 4 个字段（Length/IfIndex、Next、AdapterName、
  FirstUnicastAddress），后续字段一律不碰 —— 少碰就少踩"结构体对齐"
  的坑（这个结构体后面还有十几个指针，硬啃容易崩）。
* 非 Windows / 调用失败 → 回退 `gethostbyname_ex` + UDP 探测默认出口。
  回退拿到的地址不保证全，但至少有一个能用的（绝不返回空列表给 UI）。
"""
from __future__ import annotations

import ctypes
import socket
import sys
from typing import List


def _default_route_ip() -> str:
    """不真正发包地探测"默认走哪张网卡"的地址（UDP connect 只查路由表）。"""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("223.5.5.5", 53))
        return str(s.getsockname()[0] or "")
    except Exception:
        return ""
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


def _hostname_ips() -> List[str]:
    out: List[str] = []
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip and ip not in out:
                out.append(ip)
    except Exception:
        pass
    d = _default_route_ip()
    if d and d not in out:
        out.insert(0, d)
    return out


def _windows_ips() -> List[str]:
    """GetAdaptersAddresses，返回本机所有 IPv4 单播地址。"""
    if sys.platform != "win32":
        return []
    try:
        AF_UNSPEC, AF_INET = 0, 2
        GAA_FLAG_SKIP_ANYCAST = 0x02
        GAA_FLAG_SKIP_MULTICAST = 0x04
        GAA_FLAG_SKIP_DNS_SERVER = 0x08
        flags = (GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
                 | GAA_FLAG_SKIP_DNS_SERVER)

        iphlpapi = ctypes.WinDLL("iphlpapi.dll")
        get_aa = iphlpapi.GetAdaptersAddresses
        get_aa.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        get_aa.restype = ctypes.c_ulong

        size = ctypes.c_ulong(16 * 1024)
        for _ in range(4):                       # ERROR_BUFFER_OVERFLOW 时重试
            buf = ctypes.create_string_buffer(size.value)
            rc = get_aa(AF_UNSPEC, flags, None, ctypes.byref(buf),
                        ctypes.byref(size))
            if rc == 0:
                break
            if rc != 111:                        # 111 = ERROR_BUFFER_OVERFLOW
                return []
        else:
            return []

        out: List[str] = []
        node = ctypes.addressof(buf)
        guard = 0
        while node and guard < 512:
            guard += 1
            # 偏移 8 = Next，偏移 24 = FirstUnicastAddress（64 位指针对齐）
            nxt = ctypes.c_void_p.from_address(node + 8).value
            ua = ctypes.c_void_p.from_address(node + 24).value
            seen = 0
            while ua and seen < 64:
                seen += 1
                ua_next = ctypes.c_void_p.from_address(ua + 8).value
                # SOCKET_ADDRESS 在偏移 16：lpSockaddr + iSockaddrLength
                sa = ctypes.c_void_p.from_address(ua + 16).value
                if sa:
                    fam = ctypes.c_ushort.from_address(sa).value
                    if fam == AF_INET:
                        raw = ctypes.string_at(sa + 4, 4)
                        ip = ".".join(str(b) for b in raw)
                        if ip and ip not in out:
                            out.append(ip)
                ua = ua_next
            node = nxt
        return out
    except Exception:
        return []


def list_local_ips() -> List[str]:
    """本机可用作源地址的 IPv4 列表（带一点排序：默认出口排最前）。

    ⚠ 169.254.x.x（APIPA 自动私有地址）**一律剔掉**：那是"网卡没拿到 DHCP"
    的产物，绑上去等于把请求发进黑洞（连不通也不报错，只超时）。实测本机
    就有 5 个这种地址，全列给用户选只会让人选错。
    """
    out: List[str] = []
    for ip in _windows_ips():
        if ip and ip not in out and not ip.startswith("169.254."):
            out.append(ip)
    for ip in _hostname_ips():
        if ip and ip not in out and not ip.startswith("169.254."):
            out.append(ip)
    d = _default_route_ip()
    if d and d in out:
        out.remove(d)
        out.insert(0, d)
    return out


def validate_bind_ip(ip: str) -> tuple[bool, str]:
    """校验源地址：空串=自动（合法）；否则必须是本机现有地址之一。

    为什么严格要求"必须是本机现有地址"：填错了 httpx 会在**每次请求**时
    抛 OSError（源地址不可用），而错误信息是一串英文 errno，用户只会觉得
    "软件坏了"。这里提前拦住并列出可选地址。
    """
    s = (ip or "").strip()
    if not s:
        return True, ""
    locals_ = list_local_ips()
    if s in locals_:
        return True, ""
    if locals_:
        return False, f"本机没有地址 {s}；可选：{'、'.join(locals_)}"
    return True, ""      # 拿不到本机列表时放行（不因枚举失败拦住用户）
