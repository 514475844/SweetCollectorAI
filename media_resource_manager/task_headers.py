"""R173：任务级请求头（User-Agent / 站点 Cookie）的清洗与归一化。

单独成模块的原因：这两个值要跨三处使用、口径必须**完全一致**——
桌面 GUI 保存、Web 表单保存、engine 起跑前兜底。写在任一处再由别处
import，比在三处各写一遍可靠（历史上「三处各写一遍」型 bug 在本项目
已发生多次）。

设计约束（对应订单 R173 §2 的「清洗与安全」）：
  · 去换行 / 回车 —— 这两个字符进了请求头等于**请求头注入**，必须剁掉；
  · 限长 8KB，超出截断（订单点名）；
  · 绝不记录全文（含 cf_clearance 等会话凭据）——日志只记长度与域名，
    由调用方用 `describe_*` 生成可安全落日志的一句话。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# 订单点名：>8KB 截断
COOKIE_MAX = 8 * 1024
UA_MAX = 512          # UA 远短于 Cookie；给足余量但不让畸形值进包

# 换行 / 回车 / 其他控制字符：请求头里一个都不能出现。
# 映射成**空格**（而不是直接删除）：删除会让 "Bad\r\nX-Injected: 1" 粘成
# "BadX-Injected: 1"，虽然已不构成新头，但语义被悄悄改掉了；空格更诚实。
_CTRL = {c: " " for c in range(0x20) if c not in (0x09,)}
_CTRL[0x7F] = " "


def clean_user_agent(raw: object) -> str:
    """归一化任务级 UA。空/空白 → ""（= 用全局，不产生行为变化）。"""
    if raw is None:
        return ""
    s = str(raw)
    s = s.translate(_CTRL)          # 先剁控制字符（含 \\r\\n）
    s = " ".join(s.split())         # 折叠连续空白（UA 里多空格无意义）
    return s[:UA_MAX].strip()


def clean_cookie(raw: object) -> str:
    """归一化任务级 Cookie。

    接受两种输入（订单 §2 明确要求）：
      1) 一行 Cookie 头：``k=v; k2=v2``（容忍空格/换行粘贴，归一化成标准形）；
      2) 一个 Netscape ``cookies.txt`` 文件路径 —— 按文件读取并拼成请求头。

    返回值保证：单行、分号+空格分隔、无控制字符、<= COOKIE_MAX。
    空 → ""（= 不额外发送 Cookie）。
    """
    if raw is None:
        return ""
    s = str(raw)
    # 换行先保留（下面要判「这是不是一段 cookies.txt 正文」），
    # 但先把 \\r 统一掉，避免 CRLF 文件在 splitlines 外的地方漏出来。
    s = s.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not s:
        return ""

    # 分支 1：用户直接填了一个 cookies.txt 文件路径
    looks_like_file = ("\n" not in s and len(s) < 4096
                       and not s.lstrip().startswith("#"))
    if looks_like_file:
        try:
            p = Path(s).expanduser()
            if p.is_file():
                s = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass          # 读不到就当它不是路径，继续按正文处理

    # 分支 2：Netscape cookies.txt 正文（制表符分隔 7 列，第 6/7 列是名/值）
    if "\t" in s and s.lstrip().startswith(("#", "Netscape")):
        return _from_netscape(s)
    if "\t" in s and s.count("\t") >= 6:
        return _from_netscape(s)

    # 分支 3：普通 Cookie 头。换行当作分隔符（用户从别处粘贴常带换行）
    s = s.replace("\n", "; ")
    s = s.translate(_CTRL)
    parts = [p.strip() for p in s.split(";")]
    keep = []
    for p in parts:
        if not p or "=" not in p:
            continue
        k, _, v = p.partition("=")
        k, v = k.strip(), v.strip()
        if not k:
            continue
        keep.append(f"{k}={v}")
    out = "; ".join(keep)
    return out[:COOKIE_MAX].strip()


def _from_netscape(text: str) -> str:
    """Netscape cookies.txt → 单个 Cookie 请求头。

    格式（7 列，制表符分隔）：
      domain  flag  path  secure  expires  name  value
    注释行以 '#' 开头；域排除行（以 '#' 开头但含 HttpOnly_ 的变体）同样跳过。
    """
    pairs = []
    for line in text.splitlines():
        line = line.rstrip("\n")
        if not line or line.startswith("#"):
            continue
        # HttpOnly_ 前缀的域要剥掉前缀后照常取
        cols = line.split("\t")
        if len(cols) < 7:
            continue
        name, val = cols[5].strip(), cols[6].strip()
        if not name:
            continue
        pairs.append(f"{name}={val}")
    return "; ".join(pairs)[:COOKIE_MAX].strip()


def describe_user_agent(ua: str) -> str:
    """可安全落日志的 UA 说明（不泄露全文）。"""
    return f"(任务级 UA，{len(ua)} 字符)" if ua else "(全局 UA)"


def describe_cookie(ck: str, domain: str = "") -> str:
    """可安全落日志的 Cookie 说明。

    **绝不输出 Cookie 全文**（可能含 cf_clearance 等会话凭据）：
    只给「域名 + 长度 + 几个 cookie 名」。订单 §2 点名此口径。
    """
    if not ck:
        return "(不发 Cookie)"
    names: list[str] = []
    for part in ck.split(";"):
        k = part.split("=", 1)[0].strip()
        if k and k not in names:
            names.append(k)
    shown = ", ".join(names[:5]) + ("…" if len(names) > 5 else "")
    where = f"{domain} " if domain else ""
    return f"({where}Cookie {len(ck)} 字符，含 {shown})"


def resolve_task_headers(user_agent: object,
                         cookie: object) -> tuple[str, str]:
    """一次把两个值清洗好（调用方最常用的一句）。"""
    return clean_user_agent(user_agent), clean_cookie(cookie)


def cookie_file_hint() -> Optional[str]:
    """给 UI 的提示串（桌面/Web 共用，避免两处文案漂移）。"""
    return ("留空=不发送。支持两种填法：① 一行 Cookie（k=v; k2=v2）；"
            "② 一个 cookies.txt 文件路径（Netscape 格式，自动读取）")
