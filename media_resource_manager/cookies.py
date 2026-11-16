"""Per-domain cookie store.

Cloudflare (and other login-gated sites) require a valid session cookie
(e.g. ``cf_clearance``) that a plain ``httpx`` Fetcher cannot obtain on its
own. The user solves the challenge once in a *real* browser, we capture the
cookie, and reuse it for every task on that domain. This is the light,
reliable alternative to bundling a headless browser engine: the existing
``Fetcher`` already forwards a ``Cookie`` header (#52), so we only need to
feed it the right value.

Store file: ``<软件目录>/data/cookies.json``（随软件目录一起走，见 config.user_data_dir）
    { "ososedki.com": "cf_clearance=xxxx; csrftoken=yyyy", ... }
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

# 与数据库/规则同一个数据目录：默认「软件目录/data」，
# 程序目录不可写时才回退到 %APPDATA%（见 config.user_data_dir）。
from .config import user_data_dir  # noqa: E402

STORE_PATH = user_data_dir() / "cookies.json"


def _load() -> dict[str, str]:
    try:
        if STORE_PATH.exists():
            data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _save(d: dict[str, str]) -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORE_PATH.write_text(
            json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        # Persistence is best-effort; never crash the crawl for a cookie write.
        pass


def get_cookie(domain: str) -> Optional[str]:
    """Return the stored Cookie header for ``domain`` (or None)."""
    if not domain:
        return None
    return _load().get(domain) or None


def set_cookie(domain: str, header: str) -> None:
    """Persist a Cookie header string for ``domain``."""
    if not domain or not header:
        return
    d = _load()
    d[domain] = header.strip()
    _save(d)


def del_cookie(domain: str) -> None:
    d = _load()
    if d.pop(domain, None) is not None:
        _save(d)


def all_cookies() -> dict[str, str]:
    return _load()


def _parse(header: str) -> dict[str, str]:
    """Parse a ``a=b; c=d`` Cookie header into an ordered name->value map."""
    out: dict[str, str] = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out[part] = ""
    return out


def merge_cookies(task_cookie: str, domain_cookie: Optional[str]) -> str:
    """Combine a per-task cookie with the per-domain stored cookie.

    The domain cookie (e.g. ``cf_clearance``) is the important one; on a name
    clash it wins. The result is a single ``; ``-joined header.
    """
    merged = _parse(task_cookie)
    if domain_cookie:
        merged.update(_parse(domain_cookie))  # domain overrides same-named
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def cookie_for_task(task_cookie: str,
                    domain: str,
                    getter: Callable[[str], Optional[str]] = get_cookie) -> str:
    """Build the Cookie header the Fetcher should send for a task.

    ``domain`` is the registrable domain of the task's site; ``getter`` is
    injectable for tests.
    """
    if not task_cookie and not domain:
        return ""
    domain_cookie = getter(domain) if domain else None
    if not task_cookie and not domain_cookie:
        return ""
    return merge_cookies(task_cookie, domain_cookie)


# ───────────────────────── 配对码（R251） ─────────────────────────
# 浏览器插件要「直接把 Cookie 推给软件」，就得跨过软件的登录鉴权：
# 插件拿不到会话 cookie，也不该让用户把网页密码填进插件里。
# 所以用**配对码**：设置页显示 6 位，插件里填一次，之后每次带上它。
#
# ⚠ 这一条不能省。`/api/cookie/ingest` 写进去的是**登录态**：任何网页都能
#   向 127.0.0.1 发 POST（CORS 只挡读响应、不挡发请求，DNS rebinding 更彻底），
#   没有配对码就等于谁都能往这台机器塞一个域名的 Cookie（= 接管该站账号）。
def new_pair_code() -> str:
    """生成 6 位数字配对码（纯数字：用户要在插件里手抄一遍）。"""
    import secrets
    return "%06d" % secrets.randbelow(1000000)


def pair_code(cfg, save: Optional[Callable[[], None]] = None) -> str:
    """取当前配对码；没有就生成一个并落盘（``save`` 传进来才会持久化）。"""
    cur = str(getattr(cfg, "cookie_pair_code", "") or "")
    if len(cur) >= 4:
        return cur
    cur = new_pair_code()
    try:
        cfg.cookie_pair_code = cur
        if save is not None:
            save()
    except Exception:
        pass
    return cur


def check_pair_code(cfg, got) -> bool:
    """恒定时间比较，避免按响应时间逐位试出来。"""
    import hmac
    want = str(getattr(cfg, "cookie_pair_code", "") or "")
    if len(want) < 4:
        return False
    return hmac.compare_digest(want, str(got or "").strip())


# ───────────────────── 从「Cookie 导出 JSON」导入 ─────────────────────
# 兼容 Cookie-Editor / EditThisCookie 的导出格式：
#   [{"name":"cf_clearance","value":"…","domain":".example.com", …}, …]
# 也兼容 {"cookies":[...]} 与 {"域名": "k=v; k2=v2"} 两种整体形态。
def normalize_domain(domain: str) -> str:
    """去掉前导点、端口与协议，统一成小写主机名。"""
    d = str(domain or "").strip().lower()
    if "://" in d:
        try:
            from urllib.parse import urlsplit
            d = urlsplit(d).hostname or ""
        except Exception:
            d = ""
    d = d.split("/")[0].split(":")[0]
    return d.lstrip(".")


def rows_from_payload(payload) -> list:
    """把各种「导出的 Cookie」形态归一成 ``[{"domain","name","value"}]``。

    认不出的项**跳过**而不是报错：用户从别处复制来的东西经常夹着空值、
    注释、甚至整段说明文字，为其中一条不合法就整批失败最难用。
    """
    out = []
    items = payload
    if isinstance(payload, dict):
        if isinstance(payload.get("cookies"), list):
            items = payload["cookies"]
        else:
            # {域名: "k=v; k2=v2"} 形态
            for dom, header in payload.items():
                if isinstance(header, str):
                    for k, v in _parse(header).items():
                        out.append({"domain": normalize_domain(dom),
                                    "name": k, "value": v})
            return [r for r in out if r["domain"] and r["name"]]
    if not isinstance(items, list):
        return []
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        dom = normalize_domain(it.get("domain") or it.get("host") or "")
        if not dom:
            continue
        out.append({"domain": dom, "name": name,
                    "value": str(it.get("value") or "")})
    return out


def import_payload(payload, merge: bool = True) -> dict:
    """把归一后的 Cookie 写进按域名的存储，返回每个域名的结果。

    ``merge=True`` 时与已有 Cookie **按名字合并**（新值覆盖同名）——
    这是实用的默认：用户往往只补一条 ``cf_clearance``，不该把整站 Cookie
    冲掉。``merge=False`` 则整域替换。
    """
    rows = rows_from_payload(payload)
    by_dom: dict[str, list] = {}
    for r in rows:
        by_dom.setdefault(r["domain"], []).append(r)
    done = {}
    for dom, items in by_dom.items():
        old = _parse(get_cookie(dom) or "") if merge else {}
        for it in items:
            old[it["name"]] = it["value"]
        header = "; ".join(f"{k}={v}" for k, v in old.items())
        if header:
            set_cookie(dom, header)
            done[dom] = {"count": len(items), "total": len(old),
                         "names": [i["name"] for i in items]}
    return {"ok": True, "domains": done, "imported": len(rows)}


def summary() -> list:
    """按域名列出已存的 Cookie（**只给名字，不给值**）。"""
    out = []
    for dom, header in sorted(all_cookies().items()):
        names = list(_parse(header).keys())
        out.append({"domain": dom, "count": len(names), "names": names[:12],
                    "more": max(0, len(names) - 12),
                    "chars": len(header)})
    return out
