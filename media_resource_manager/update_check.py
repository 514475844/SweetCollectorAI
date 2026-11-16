"""检查程序更新（编号 7）。

诚实说明：这是**自用软件**，没有官方更新服务器。所以本模块做两件真能做的事：

1. 报出**当前版本 + 构建时间 + 可执行文件位置**（用户至少能确认自己在跑哪一版）；
2. 如果你配了「更新清单地址」（config.update_manifest_url 或环境变量
   `MRM_UPDATE_URL`），就去拉一个 JSON 比对版本号：

       {"version": "1.0.3", "url": "https://…/Sweet采集AI版.zip",
        "notes": "修复 xxx", "published": "2026-10-27"}

   没配地址时**不会假装"已是最新"**，而是明确说"未配置更新地址"。

设计要点：
* 网络失败一律翻中文，绝不抛异常（查更新失败不该弹崩溃框）；
* 走 `proxy_tasks_only` 的相反侧：这是软件自身请求，按配置决定是否用代理；
* 版本比较用数字元组，`"1.0.10" > "1.0.9"` 必须成立（字符串比较会错）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional


def current_version() -> str:
    try:
        from . import __version__
        return str(__version__ or "0.0.0")
    except Exception:
        return "0.0.0"


def _exe_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(__file__).resolve().parent.parent


def build_info() -> dict:
    """当前版本 / 构建时间 / 路径（界面上直接展示这三项就够判断在用哪一版）。"""
    p = _exe_path()
    try:
        ts = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except Exception:
        ts = "未知"
    return {
        "version": current_version(),
        "built": ts,
        "path": str(p),
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _vtuple(v: str) -> tuple:
    out = []
    for part in str(v or "").strip().lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out) or (0,)


def is_newer(remote: str, local: str) -> bool:
    """远端版本是否比本地新（按数字元组比较，不用字符串）。"""
    a, b = _vtuple(remote), _vtuple(local)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def manifest_url(cfg=None) -> str:
    u = ""
    try:
        u = str(getattr(cfg, "update_manifest_url", "") or "").strip()
    except Exception:
        u = ""
    return u or os.environ.get("MRM_UPDATE_URL", "").strip()


def _zh_error(ex: BaseException) -> str:
    raw = f"{type(ex).__name__}: {ex}"
    low = raw.lower()
    if "tunnel" in low or "proxy" in low or "502" in low:
        return "代理连不通（检查代理是否在运行）"
    if "timed out" in low or "timeout" in low:
        return "连接超时（网络或代理有问题）"
    if "name or service" in low or "getaddrinfo" in low:
        return "域名解析失败（地址写错了？）"
    if "refused" in low:
        return "对方拒绝连接（地址/端口不对）"
    if "404" in raw:
        return "更新地址不存在（404），检查清单地址"
    return raw[:100]


def check(cfg=None, timeout: float = 12.0) -> dict:
    """检查更新。返回结构固定：

    {"ok": bool, "has_update": bool, "local": str, "remote": str,
     "url": str, "notes": str, "built": str, "text": str}

    `ok=False` 表示"没能完成检查"（未配置地址 / 网络失败），此时 `text` 说明原因。
    """
    info = build_info()
    base = {
        "ok": False, "has_update": False,
        "local": info["version"], "remote": "", "url": "", "notes": "",
        "built": info["built"], "path": info["path"], "text": "",
    }
    url = manifest_url(cfg)
    if not url:
        base["text"] = (f"当前版本 {info['version']}（构建于 {info['built']}）。"
                        "未配置更新地址，无法自动检查新版本 —— "
                        "在设置里填「更新清单地址」后即可自动比对。")
        return base

    proxy = None
    try:
        # 「仅任务走代理」开着时，软件自身请求直连（更新检查属于软件自身）
        if not bool(getattr(cfg, "proxy_tasks_only", False)):
            proxy = (getattr(cfg, "proxy", "") or
                     os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    except Exception:
        proxy = None

    try:
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))   # 显式直连
        op = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, headers={"User-Agent": "SweetMRM-UpdateCheck"})
        with op.open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as ex:                    # noqa: BLE001
        base["text"] = f"检查更新失败：{_zh_error(ex)}"
        return base

    if not isinstance(data, dict):
        base["text"] = "检查更新失败：更新清单格式不对（应是 JSON 对象）"
        return base

    remote = str(data.get("version") or "").strip()
    base.update({
        "ok": True,
        "remote": remote,
        "url": str(data.get("url") or ""),
        "notes": str(data.get("notes") or ""),
        "has_update": bool(remote) and is_newer(remote, info["version"]),
    })
    if not remote:
        base["text"] = "更新清单里没有 version 字段，无法比对"
    elif base["has_update"]:
        base["text"] = (f"发现新版本 {remote}（当前 {info['version']}）。"
                        + (f"更新说明：{base['notes']} " if base["notes"] else ""))
    else:
        base["text"] = f"已是最新版本（{info['version']}，构建于 {info['built']}）"
    return base
