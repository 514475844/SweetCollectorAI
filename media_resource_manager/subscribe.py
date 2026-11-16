"""站点订阅 / 新图监控（编号 21、22）。

qBittorrent 的 RSS 自动下载，在采集软件里的等价物就是"盯着一个列表页，
出现新条目就自动建任务"。这里刻意**不叫 RSS**：

* 我们订阅的是**普通网页**（图集列表页、作者页、标签页），用户手上没有
  RSS 源，也不想为了看一眼新图去折腾 feed；
* 判定"新"靠**历史快照**（sub_items 表），比解析 feed 的 GUID 更通用 ——
  同一个页面结构变了也照样能认出新增的链接。

三条设计原则（都是踩过一次才写下来的）：
1. **不做"看起来聪明"的启发式**：没有任何链接模式时按通用规则取同域链接，
   但提供「预览」接口让人**先看再开**。乱建任务比漏抓更烦人。
2. **快照只增不删**：链接从页面上消失不代表"我要删它"，删了会导致下次
   它再出现时又被当新条目，重复建任务。
3. **出错不装作正常**：抓不到页面就把 last_error 写进库里给界面显示，
   绝不"0 个新条目"地糊过去（用户会以为站点真的没更新）。
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from .config import AppConfig
from .db import Database
from .logutil import get_logger

LOG = get_logger()

#: 单次抓取最多认多少个链接（防止把整站导航当条目）。
MAX_LINKS = 500
#: 一条订阅最少间隔（分钟）。低于这个值等于定时砸站，不给。
MIN_INTERVAL_MIN = 5

#: 没给自定义规则时，这些路径明显不是"一条内容"，直接跳过。
_SKIP_HINTS = (
    "/login", "/logout", "/register", "/signup", "/about", "/contact",
    "/privacy", "/terms", "/tos", "/faq", "/help", "/dmca", "/rss", "/feed",
    "/search", "/tag/", "/tags/", "/category/", "/categories/", "/page/",
    "/user/login", "/account", "/donate", "/premium", "/api/",
    "/javascript:", "mailto:", "tel:",
)
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")


# ---------------------------------------------------------------- 数据访问
def ensure_tables(db: Database) -> None:
    """幂等建表（与 db.py 的 _migrate 同风格：独立函数，老库直接补）。"""
    db.execute("""
CREATE TABLE IF NOT EXISTS subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,
    pattern     TEXT DEFAULT '',
    interval_min INTEGER DEFAULT 60,
    max_items   INTEGER DEFAULT 30,
    enabled     INTEGER DEFAULT 1,
    auto_run    INTEGER DEFAULT 1,
    adapter_name TEXT DEFAULT '',
    output_dir  TEXT DEFAULT '',
    last_check  TEXT,
    last_found  INTEGER DEFAULT 0,
    last_new    INTEGER DEFAULT 0,
    last_error  TEXT DEFAULT '',
    total_new   INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now'))
)""")
    db.execute("""
CREATE TABLE IF NOT EXISTS sub_items (
    sub_id     INTEGER NOT NULL,
    url        TEXT NOT NULL,
    first_seen TEXT DEFAULT (datetime('now')),
    task_id    INTEGER,
    PRIMARY KEY (sub_id, url)
)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_sub_items_sub "
               "ON sub_items(sub_id)")


def _row_to_sub(r) -> dict:
    keys = r.keys() if hasattr(r, "keys") else []
    get = (lambda k, d=None: (r[k] if k in keys else d)) if keys else \
        (lambda k, d=None: d)
    return {
        "id": int(get("id", 0) or 0),
        "name": get("name", "") or "",
        "url": get("url", "") or "",
        "pattern": get("pattern", "") or "",
        "interval_min": int(get("interval_min", 60) or 60),
        "max_items": int(get("max_items", 30) or 30),
        "enabled": bool(get("enabled", 1)),
        "auto_run": bool(get("auto_run", 1)),
        "adapter_name": get("adapter_name", "") or "",
        "output_dir": get("output_dir", "") or "",
        "last_check": get("last_check", "") or "",
        "last_found": int(get("last_found", 0) or 0),
        "last_new": int(get("last_new", 0) or 0),
        "last_error": get("last_error", "") or "",
        "total_new": int(get("total_new", 0) or 0),
    }


def list_subs(db: Database) -> list[dict]:
    ensure_tables(db)
    return [_row_to_sub(r) for r in
            db.query("SELECT * FROM subscriptions ORDER BY id")]


def get_sub(db: Database, sub_id: int) -> Optional[dict]:
    ensure_tables(db)
    rows = db.query("SELECT * FROM subscriptions WHERE id=?", (int(sub_id),))
    return _row_to_sub(rows[0]) if rows else None


def save_sub(db: Database, data: dict) -> tuple[bool, str, int]:
    """新建或更新。返回 (成功?, 中文说明, id)。"""
    ensure_tables(db)
    url = str(data.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return False, "订阅地址必须以 http:// 或 https:// 开头", 0
    name = str(data.get("name") or "").strip() or (urlsplit(url).netloc or "订阅")
    pattern = str(data.get("pattern") or "").strip()
    if pattern:
        try:
            re.compile(pattern)
        except re.error as ex:
            return False, f"链接规则不是合法的正则：{ex}", 0
    try:
        interval = int(data.get("interval_min") or 60)
    except (TypeError, ValueError):
        interval = 60
    interval = max(MIN_INTERVAL_MIN, min(interval, 7 * 24 * 60))
    try:
        max_items = int(data.get("max_items") or 30)
    except (TypeError, ValueError):
        max_items = 30
    max_items = max(1, min(max_items, 500))
    vals = (name[:80], url, pattern, interval, max_items,
            1 if data.get("enabled", True) else 0,
            1 if data.get("auto_run", True) else 0,
            str(data.get("adapter_name") or "")[:60],
            str(data.get("output_dir") or ""))
    sid = int(data.get("id") or 0)
    if sid:
        db.execute(
            "UPDATE subscriptions SET name=?, url=?, pattern=?, interval_min=?, "
            "max_items=?, enabled=?, auto_run=?, adapter_name=?, output_dir=? "
            "WHERE id=?", vals + (sid,))
        return True, "已保存", sid
    cur = db.execute(
        "INSERT INTO subscriptions(name, url, pattern, interval_min, max_items, "
        "enabled, auto_run, adapter_name, output_dir) "
        "VALUES (?,?,?,?,?,?,?,?,?)", vals)
    return True, "已新建", int(cur.lastrowid or 0)


def delete_sub(db: Database, sub_id: int) -> int:
    """删订阅源 + 它的历史快照（快照跟着源走，留着只会慢慢变垃圾）。"""
    ensure_tables(db)
    db.execute("DELETE FROM sub_items WHERE sub_id=?", (int(sub_id),))
    cur = db.execute("DELETE FROM subscriptions WHERE id=?", (int(sub_id),))
    return int(cur.rowcount or 0)


def seen_urls(db: Database, sub_id: int, limit: int = 20000) -> set[str]:
    ensure_tables(db)
    rows = db.query("SELECT url FROM sub_items WHERE sub_id=? LIMIT ?",
                    (int(sub_id), int(limit)))
    return {r["url"] for r in rows}


def snapshot_count(db: Database, sub_id: int) -> int:
    ensure_tables(db)
    rows = db.query("SELECT COUNT(*) AS n FROM sub_items WHERE sub_id=?",
                    (int(sub_id),))
    return int(rows[0]["n"] or 0) if rows else 0


# ---------------------------------------------------------------- 抓取与解析
def _clean(url: str, base: str) -> str:
    """绝对化 + 去掉 fragment（同一页面的 #anchor 不算两个条目）。"""
    try:
        u = urljoin(base, str(url).strip())
        sp = urlsplit(u)
        if sp.scheme not in ("http", "https"):
            return ""
        return urlunsplit((sp.scheme, sp.netloc, sp.path or "/", sp.query, ""))
    except Exception:
        return ""


_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


def extract_links(html: str, base_url: str,
                  pattern: str = "") -> list[str]:
    """从页面 HTML 里取出候选条目链接（同域、去重、保序）。

    pattern 非空 → 只保留正则命中的链接（用户自己定规则，最准）；
    pattern 为空 → 用通用规则：同域 + 不是明显导航页（见 `_SKIP_HINTS`）。
    """
    text = str(html or "")
    raw = _HREF_RE.findall(text)
    # 懒加载/JSON 里的图片直链也认（有些站列表页只给 data-src）
    raw += re.findall(r"""["']((?:https?:)?//[^"'\s]+?\.(?:jpg|jpeg|png|webp|gif))["']""",
                      text, re.I)
    pat = None
    if pattern:
        try:
            pat = re.compile(pattern, re.I)
        except re.error:
            pat = None
    out: list[str] = []
    seen: set[str] = set()
    for href in raw:
        u = _clean(href, base_url)
        if not u or u in seen:
            continue
        low = u.lower()
        try:
            from .utils.url import same_domain
            if not same_domain(u, base_url):
                continue
        except Exception:
            pass
        if not pat:
            if low.endswith(("/", ".css", ".js", ".ico", ".svg")):
                # 目录页/静态资源；但 .jpg 之类的直链保留（见下）
                if not low.endswith(_IMG_EXT):
                    continue
            if any(h in low for h in _SKIP_HINTS):
                continue
            # 与订阅页本身完全相同的地址不是条目
            if low == _clean(base_url, base_url).lower():
                continue
        else:
            if not pat.search(u):
                continue
        seen.add(u)
        out.append(u)
        if len(out) >= MAX_LINKS:
            break
    return out


def _fetch_html(cfg: AppConfig, url: str) -> str:
    """用**同一套**网络设置抓页面（代理/UA/超时/证书/内网限制全一致）。"""
    from .crawler.fetcher import Fetcher
    from .scheduler.queue import make_loop

    async def go() -> str:
        f = Fetcher(cfg)
        try:
            return await f.fetch_text(url, delay=0)
        finally:
            await f.close()

    loop = make_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        try:
            loop.close()
        except Exception:
            pass


def preview(cfg: AppConfig, url: str, pattern: str = "",
            limit: int = 40) -> dict:
    """「先看看它能认出什么」——不写库、不建任务。"""
    try:
        html = _fetch_html(cfg, url)
    except Exception as ex:                             # noqa: BLE001
        return {"ok": False, "error": f"抓取订阅页失败：{_zh_error(ex)}"}
    links = extract_links(html, url, pattern)
    return {"ok": True, "count": len(links),
            "links": links[:max(1, int(limit or 40))],
            "truncated": len(links) > max(1, int(limit or 40))}


def _zh_error(ex: BaseException) -> str:
    raw = f"{type(ex).__name__}: {ex}"
    # httpx 把底层原因包在 __cause__/__context__ 里，只看外层字符串会把
    # "证书不认"错报成"域名解析失败"（探针里真踩到过：自签站 + allow_private
    # 都通过，却报解析失败，让人去查 DNS —— 查一天也查不出来）。
    chain = []
    cur = ex
    for _ in range(4):
        if cur is None:
            break
        chain.append(f"{type(cur).__name__}: {cur}")
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    low = (raw + " " + " ".join(chain)).lower()
    if "certificate" in low or "certificate_verify_failed" in low or \
            "ssl" in low:
        return ("HTTPS 证书校验失败（自签名或已过期证书）。"
                "如为自有站点，请在「设置 → 网络」中将其加入"
                "「跳过证书校验的域名」")
    if "connection refused" in low or "all connection attempts failed" in low:
        return ("连接被拒绝：端口未开放、服务未启动或被防火墙拦截"
                "（代理配置错误也可能导致此问题）")
    if "timed out" in low or "timeout" in low:
        return "请求超时（站点慢或被墙）；可调大超时或配代理"
    if "name or service not known" in low or "getaddrinfo" in low or \
            "nodename" in low or "name resolution" in low:
        return "域名解析失败（检查网址或请尝试开「域名解析也走代理」）"
    if "403" in low:
        return "站点拒绝访问（403，可能需要 Cookie）"
    if "404" in low:
        return "订阅页 404（网址写错了或页面已下线）"
    return raw[:160]


# ---------------------------------------------------------------- 检查一轮
def check_sub(db: Database, cfg: AppConfig, sub: dict,
              queue=None, dry_run: bool = False) -> dict:
    """查一个订阅源：抓页面 → 与快照比对 → 有新条目就建任务。

    dry_run=True 只比对不建任务、不写快照（界面上的「立即检查」用一眼结果，
    不想因为点一下就被建一堆任务；真正定时监控才落库）。
    """
    sid = int(sub.get("id") or 0)
    url = str(sub.get("url") or "")
    out = {"id": sid, "name": sub.get("name") or url, "ok": False,
           "found": 0, "new": 0, "task_id": 0, "error": "", "items": []}
    try:
        html = _fetch_html(cfg, url)
    except Exception as ex:                             # noqa: BLE001
        out["error"] = _zh_error(ex)
        if sid and not dry_run:
            db.execute("UPDATE subscriptions SET last_check=datetime('now'), "
                       "last_error=?, last_new=0 WHERE id=?", (out["error"][:300], sid))
        return out
    links = extract_links(html, url, str(sub.get("pattern") or ""))
    out["found"] = len(links)
    seen = seen_urls(db, sid) if sid else set()
    fresh = [u for u in links if u not in seen]
    try:
        cap = int(sub.get("max_items") or 30)
    except (TypeError, ValueError):
        cap = 30
    fresh = fresh[:max(1, min(cap, 500))]
    out["new"] = len(fresh)
    out["items"] = fresh[:50]
    out["ok"] = True
    if dry_run:
        return out
    if sid:
        # 快照记得**所有见到的**链接（不只是新建任务的），否则老条目下次又变"新"
        try:
            db.executemany(
                "INSERT OR IGNORE INTO sub_items(sub_id, url) VALUES (?,?)",
                [(sid, u) for u in links])
        except Exception as ex:                          # noqa: BLE001
            LOG.warning("写订阅快照失败: %s", ex)
        if not fresh:
            db.execute("UPDATE subscriptions SET last_check=datetime('now'), "
                       "last_found=?, last_new=0, last_error='' WHERE id=?",
                       (len(links), sid))
            return out
        # 建任务
        tid = 0
        try:
            tid = _make_task(db, cfg, sub, fresh)
        except Exception as ex:                          # noqa: BLE001
            out["error"] = f"建任务失败：{type(ex).__name__}: {ex}"
            db.execute("UPDATE subscriptions SET last_check=datetime('now'), "
                       "last_found=?, last_new=?, last_error=? WHERE id=?",
                       (len(links), len(fresh), out["error"][:300], sid))
            return out
        out["task_id"] = tid
        try:
            db.executemany(
                "UPDATE sub_items SET task_id=? WHERE sub_id=? AND url=?",
                [(tid, sid, u) for u in fresh])
        except Exception:
            pass
        db.execute(
            "UPDATE subscriptions SET last_check=datetime('now'), last_found=?, "
            "last_new=?, last_error='', total_new=COALESCE(total_new,0)+? "
            "WHERE id=?", (len(links), len(fresh), len(fresh), sid))
        if queue is not None and bool(sub.get("auto_run", True)):
            try:
                queue.submit(int(tid))
            except Exception as ex:                      # noqa: BLE001
                LOG.warning("订阅任务入队失败（任务已建，可手动开始）: %s", ex)
    return out


def _make_task(db: Database, cfg: AppConfig, sub: dict, urls: Iterable[str]) -> int:
    """把新条目建成一个任务（一个源一次检查 = 一个任务，方便看进度）。"""
    from .models import TaskConfig
    from .repo import save_task

    urls = [u for u in urls if u]
    stamp = datetime.now().strftime("%m-%d %H:%M")
    name = f"{str(sub.get('name') or '订阅')[:40]} 新图 {stamp}"
    t = TaskConfig(
        id=None, name=name[:80],
        adapter_name=str(sub.get("adapter_name") or ""),
        entry_urls=urls[:500],
        max_depth=int(getattr(cfg, "max_depth", 3) or 3),
        concurrency=int(getattr(cfg, "concurrency", 4) or 4),
        delay=float(getattr(cfg, "delay", 0.5) or 0.5),
        output_dir=str(sub.get("output_dir") or "") or None,
    )
    return int(save_task(db, t))


# ---------------------------------------------------------------- 后台线程
class SubscriptionWatcher:
    """定时检查订阅源的守护线程（GUI / Web / headless 三处共用）。

    `on_task(tid)` 由宿主给：Web 与 headless 用它把任务丢进队列，桌面端可以
    顺便刷新界面。没有回调也能跑（只建任务不自动开始）。
    """

    def __init__(self, cfg: AppConfig, db_path, on_task: Optional[Callable] = None,
                 tick: float = 30.0):
        self.cfg = cfg
        self.db_path = Path(db_path) if db_path else None
        self.on_task = on_task
        self.tick = max(10.0, float(tick or 30))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_run: dict[int, float] = {}
        self._lock = threading.Lock()
        self.checks = 0
        self.created = 0
        self.last_error = ""

    # ---- 生命周期 ----
    def start(self) -> bool:
        if not bool(getattr(self.cfg, "sub_enabled", True)):
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="mrm-subwatch")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _loop(self) -> None:
        # 启动后先等 20 秒：别和启动流程抢 IO，也别让"刚开机就砸站"
        if self._stop.wait(20.0):
            return
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as ex:                      # noqa: BLE001
                self.last_error = f"{type(ex).__name__}: {ex}"
                LOG.warning("订阅检查异常（不影响其它任务）: %s", ex)
            if self._stop.wait(self.tick):
                return

    def tick_once(self, queue=None, force: bool = False) -> list[dict]:
        """检查到点的订阅源。返回本轮结果（供测试/日志）。"""
        if not bool(getattr(self.cfg, "sub_enabled", True)):
            return []
        db = Database(self.db_path) if self.db_path else None
        if db is None:
            return []
        results = []
        try:
            now = time.time()
            for sub in list_subs(db):
                if not sub.get("enabled"):
                    continue
                sid = int(sub["id"])
                every = max(MIN_INTERVAL_MIN, int(sub.get("interval_min") or 60)) * 60
                last = self._last_run.get(sid, 0.0)
                if not force and last and (now - last) < every:
                    continue
                with self._lock:
                    if self._stop.is_set():
                        break
                    r = check_sub(db, self.cfg, sub, queue=queue)
                self._last_run[sid] = now
                self.checks += 1
                if r.get("task_id"):
                    self.created += 1
                    if self.on_task:
                        try:
                            self.on_task(int(r["task_id"]))
                        except Exception:
                            pass
                if not r.get("ok") and r.get("error"):
                    self.last_error = r["error"]
                results.append(r)
        finally:
            try:
                db.close()
            except Exception:
                pass
        return results


#: 全局单例（桌面端会同时存在 GUI、WebServer、headless 三处调用点，
#: 但"定时看订阅"这件事**只能有一个线程**在做，否则同一条订阅会被查两遍、
#: 建两个任务）。
_WATCHER: Optional[SubscriptionWatcher] = None
_WATCHER_LOCK = threading.Lock()


def start_global_watcher(cfg: AppConfig, db_path, queue=None) -> Optional[SubscriptionWatcher]:
    """拿到（必要时启动）全局订阅线程。不可用/关掉时返回现有实例或 None。"""
    global _WATCHER
    with _WATCHER_LOCK:
        w = _WATCHER
        if w is not None and w.running and w.db_path == Path(str(db_path)):
            return w
        if not bool(getattr(cfg, "sub_enabled", True)):
            return w
        on_task = None
        if queue is not None:
            def on_task(tid, _q=queue):                  # noqa: ANN001
                try:
                    _q.submit(int(tid))
                except Exception:
                    pass
        w = SubscriptionWatcher(cfg, db_path, on_task=on_task)
        if w.start():
            _WATCHER = w
            LOG.info("订阅监控已启动（%s）", db_path)
            return w
        return None


def stop_global_watcher() -> None:
    global _WATCHER
    with _WATCHER_LOCK:
        w, _WATCHER = _WATCHER, None
    if w is not None:
        w.stop()


def due_subs(db: Database, cfg: AppConfig, last_run: dict[int, float],
             now: Optional[float] = None) -> list[dict]:
    """哪些源"到点了"（纯函数，方便测时间逻辑）。"""
    now = time.time() if now is None else now
    out = []
    for sub in list_subs(db):
        if not sub.get("enabled"):
            continue
        every = max(MIN_INTERVAL_MIN, int(sub.get("interval_min") or 60)) * 60
        last = last_run.get(int(sub["id"]), 0.0)
        if not last or (now - last) >= every:
            out.append(sub)
    return out
