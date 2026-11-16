"""检查程序更新。

默认读**本项目 GitHub 仓库的最新 Release** 比对版本，无需用户配置：

  1. 报出当前版本 + 构建时间 + 可执行文件位置；
  2. 拉取 https://api.github.com/repos/<owner>/<repo>/releases/latest
     的 tag_name 作为远端版本，html_url 作为下载页，body 作为更新说明。

也保留自定义清单地址（config.update_manifest_url 或环境变量 MRM_UPDATE_URL），
填了就以它为准，格式：

  {"version": "1.0.3", "url": "https://…", "notes": "修复 xxx", "published": "2026-10-27"}

设计要点：
* 网络失败一律翻中文，绝不抛异常（查更新失败不该弹崩溃框），且**不假装"已是最新"**；
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


# 本项目开源仓库 —— 默认更新源。用户没配自定义清单地址时读这里的最新 Release。
GITHUB_OWNER = "514475844"
GITHUB_REPO = "SweetCollectorAI"
DEFAULT_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)
RELEASES_PAGE = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"


def manifest_url(cfg=None) -> str:
    """自定义清单地址（用户填了优先用），没填返回空串表示走默认 GitHub Release。"""
    u = ""
    try:
        u = str(getattr(cfg, "update_manifest_url", "") or "").strip()
    except Exception:
        u = ""
    return u or os.environ.get("MRM_UPDATE_URL", "").strip()


def _is_github_release(url: str) -> bool:
    return "api.github.com/repos/" in url and "/releases/latest" in url


def parse_manifest(data: dict, is_release: bool) -> dict:
    """把清单 JSON 归一成 {version,url,notes,published}。

    GitHub Release 的字段和自定义清单不同：
      tag_name → version, html_url → url, body → notes, published_at → published
    版本号去掉开头的 v（v0.0.52 → 0.0.52）。
    """
    if not isinstance(data, dict):
        return {"version": "", "url": "", "notes": "", "published": ""}
    if is_release:
        ver = str(data.get("tag_name") or "").strip().lstrip("vV")
        return {
            "version": ver,
            "url": str(data.get("html_url") or RELEASES_PAGE).strip(),
            "notes": str(data.get("body") or "").strip(),
            "published": str(data.get("published_at") or "")[:10],
        }
    return {
        "version": str(data.get("version") or "").strip(),
        "url": str(data.get("url") or "").strip(),
        "notes": str(data.get("notes") or "").strip(),
        "published": str(data.get("published") or "").strip(),
    }


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
    custom = manifest_url(cfg)
    url = custom or DEFAULT_RELEASE_API       # 默认读 GitHub 最新 Release
    is_release = _is_github_release(url) and not custom

    proxy = None
    try:
        # 「仅任务走代理」开着时，软件自身请求直连（更新检查属于软件自身）
        if not bool(getattr(cfg, "proxy_tasks_only", False)):
            proxy = (getattr(cfg, "proxy", "") or
                     os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY"))
    except Exception:
        proxy = None

    def _fetch(target: str):
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))   # 显式直连
        op = urllib.request.build_opener(*handlers)
        # GitHub 要求带 User-Agent，否则返回 403；自定义清单沿用旧 UA。
        ua = "SweetMRM-UpdateCheck" if not _is_github_release(target) else \
             "SweetMRM-UpdateCheck (GitHub Release check)"
        req = urllib.request.Request(target, headers={
            "User-Agent": ua, "Accept": "application/vnd.github+json"})
        with op.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        data = _fetch(url)
    except Exception as ex:                    # noqa: BLE001
        # GitHub 直连失败时，再试一次直连（用户本机代理可能不通）
        if proxy and is_release:
            try:
                saved, proxy = proxy, None
                data = _fetch(url)
                proxy = saved
            except Exception:                  # noqa: BLE001
                proxy = saved
                base["url"] = RELEASES_PAGE
                base["text"] = (f"当前版本 {info['version']}（构建于 {info['built']}）。"
                                "自动检查更新连不上 GitHub（代理或网络不通），"
                                "可到项目 Releases 页手动查看。")
                return base
        else:
            base["url"] = RELEASES_PAGE if is_release else ""
            base["text"] = (f"当前版本 {info['version']}（构建于 {info['built']}）。"
                            f"检查更新失败：{_zh_error(ex)}。"
                            + ("可到项目 Releases 页手动查看。" if is_release else ""))
            return base

    if not isinstance(data, dict):
        base["text"] = "检查更新失败：更新清单格式不对（应是 JSON 对象）"
        return base

    norm = parse_manifest(data, is_release)
    remote = norm["version"]
    base.update({
        "ok": True,
        "remote": remote,
        "url": norm["url"] or (RELEASES_PAGE if is_release else ""),
        "notes": norm["notes"],
        "published": norm["published"],
        "has_update": bool(remote) and is_newer(remote, info["version"]),
    })
    if not remote:
        base["text"] = "更新源里没有版本字段，无法比对"
    elif base["has_update"]:
        base["text"] = (f"发现新版本 {remote}（当前 {info['version']}）。"
                        + (f" 更新说明：{norm['notes'][:120]} " if norm["notes"] else ""))
    else:
        base["text"] = f"已是最新版本（{info['version']}，构建于 {info['built']}）"
    return base
