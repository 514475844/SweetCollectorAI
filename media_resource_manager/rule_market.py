"""适配器市场（规则订阅源）。

用户在「适配器 → 适配器市场」里添加订阅源（一个 index.json 的网址），
即可浏览、搜索、一键安装别人写好的适配器规则。官方市场就是本项目的
GitHub 仓库 Pages：https://514475844.github.io/SweetAdapterHub/index.json

设计约束
--------
* **只搬文本，不跑代码**：订阅源与规则文件都是纯数据（JSON/YAML），
  本模块绝不 import/exec 任何下载内容；
* **防路径穿越**：清单里的 ``file`` 必须是相对路径，拒绝 ``..``、绝对
  路径与带协议的 URL；最终 URL 由清单的 ``base_url`` join 得出，且仍
  限制在 base 同源之下；
* **入库前同一道校验**：下载到的规则走与手动保存完全一致的
  ``load_rule_file`` + ``load_adapter`` 校验，非法规则一个字节都不写；
* 单文件封顶 512 KB、清单封顶 2 MB，防被恶意源撑爆内存；
* 网络失败一律翻中文返回，不抛异常。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

INDEX_TIMEOUT = 15          # 拉清单超时（秒）
RULE_TIMEOUT = 20           # 拉单条规则超时（秒）
MAX_INDEX_BYTES = 2 * 1024 * 1024
MAX_RULE_BYTES = 512 * 1024

DEFAULT_SOURCE_URL = "https://514475844.github.io/SweetAdapterHub/index.json"


class MarketError(Exception):
    """面向用户的中文错误。"""


def _opener(cfg=None) -> urllib.request.OpenerDirector:
    """软件自身请求：按配置决定是否走代理（与 update_check 同一口径）。"""
    proxy = None
    try:
        if cfg is not None and not bool(getattr(cfg, "proxy_tasks_only", False)):
            proxy = (getattr(cfg, "proxy", "") or
                     urllib.request.getproxies().get("https") or
                     urllib.request.getproxies().get("http"))
    except Exception:
        proxy = None
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, cfg=None, timeout: int = INDEX_TIMEOUT,
         max_bytes: int = MAX_INDEX_BYTES) -> bytes:
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise MarketError("订阅源地址必须以 http:// 或 https:// 开头")
    req = urllib.request.Request(u, headers={
        "User-Agent": "SweetCollectorAI-Market",
        "Accept": "application/json, text/yaml, text/plain, */*",
    })
    try:
        with _opener(cfg).open(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise MarketError(f"订阅源返回 {e.code}，请检查地址是否正确或稍后再试")
    except urllib.error.URLError as e:
        raise MarketError(f"连不上订阅源：{getattr(e, 'reason', e)}")
    except Exception as e:
        raise MarketError(f"读取订阅源失败：{type(e).__name__}: {e}")
    if len(data) > max_bytes:
        raise MarketError("文件超过大小上限（恶意或被劫持的源会被拒绝）")
    return data


def _safe_rel_file(file_url: str, base_url: str) -> str:
    """校验规则文件字段：只准是 base 之下的相对路径。"""
    f = str(file_url or "").strip().replace("\\", "/")
    if not f or "://" in f or f.startswith("/") or ".." in f.split("/"):
        raise MarketError(f"规则文件路径不合法：{file_url!r}")
    return f


def fetch_index(source_url: str, cfg=None) -> dict:
    """拉一个订阅源清单，返回 {name, updated, base_url, rules:[...]}。

    清单格式见市场仓库 README；字段缺失容忍：base_url 缺省用源地址的目录。
    """
    raw = _get(source_url, cfg=cfg)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise MarketError("订阅源内容不是合法 JSON（确认填的是 index.json 地址）")
    if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
        raise MarketError("订阅源格式不对：应包含 rules 列表")
    base = str(data.get("base_url") or "").strip()
    if not base:
        base = source_url.rsplit("/", 1)[0] + "/"
    if not base.endswith("/"):
        base += "/"
    rules = []
    for it in data["rules"]:
        if not isinstance(it, dict):
            continue
        try:
            rel = _safe_rel_file(it.get("file"), base)
        except MarketError:
            continue                     # 坏条目跳过，不连累整份清单
        rules.append({
            "name": str(it.get("name") or "")[:64],
            "title": str(it.get("title") or it.get("name") or "")[:80],
            "site": str(it.get("site") or "")[:200],
            "domains": [str(d)[:120] for d in (it.get("domains") or [])][:20],
            "author": str(it.get("author") or "")[:80],
            "added": str(it.get("added") or "")[:20],
            "note": str(it.get("note") or "")[:300],
            "url": urllib.parse.urljoin(base, rel),
        })
    return {
        "ok": True,
        "source": source_url,
        "name": str(data.get("name") or "")[:80],
        "updated": str(data.get("updated") or "")[:20],
        "count": len(rules),
        "rules": rules,
    }


def fetch_rule_preview(rule_url: str, cfg=None) -> str:
    """下载单条规则原文（供安装前预览/校验），不落盘。"""
    raw = _get(rule_url, cfg=cfg, timeout=RULE_TIMEOUT, max_bytes=MAX_RULE_BYTES)
    return raw.decode("utf-8", "replace")


# ───────────────── 订阅源列表持久化（存 AppConfig，与全局设置同文件） ─────────────────

def load_sources(cfg) -> list[str]:
    """已订阅的源地址列表；没订阅过时给出官方市场。"""
    try:
        urls = [str(x).strip() for x in (getattr(cfg, "rule_market_sources", None) or [])
                if str(x).strip()]
        return urls or [DEFAULT_SOURCE_URL]
    except Exception:
        return [DEFAULT_SOURCE_URL]


def save_sources(cfg, urls: list[str]) -> None:
    from .config import save_config
    cfg.rule_market_sources = [str(u).strip() for u in urls if str(u).strip()]
    save_config(cfg)
