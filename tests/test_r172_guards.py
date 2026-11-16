"""R172 护栏：R154-F6「SSE 统一推送通道」（执行方笔记本棒）。

订单要点：单 `/api/events`（SSE，带数据版本号），任务进度/图库计数/审核数
变化时推送；前端用事件源替代各页轮询（**SSE 优先、轮询兜底**）；Docker 友好
（反代要关缓冲）；事件只送版本号/摘要。

每条按「行为断言优先、源码判据兜底」配，变异自检脚本
`.workbuddy/_r172_mutate.py` 逐条注入坏实现验证会被抓。

⚠ 读原始 socket 而不是 http.client：SSE 是 chunked 长流，用 urllib/
  http.client 的 read() 会一直等到流结束（= 连不上就挂死测试）。
  这里自己发一个最简 HTTP 请求、直接 recv 原始字节，既能看到 chunk 前缀，
  也能随时 close 掉长连接。
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ---- 隔离必须在 import config 之前（config.user_data_dir 读 MRM_HOME） ----
_TMP = Path(tempfile.mkdtemp(prefix="r172test_"))

WEBUI = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"


@pytest.fixture()
def web_home():
    """每个用例一个干净的 MRM_HOME，退出还原（R67 教训：必须 finally）。"""
    old = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    os.environ["MRM_HOME"] = str(_TMP)
    os.environ["MRM_NO_MIGRATE"] = "1"
    try:
        yield _TMP
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ==================================================================
# 一、纯函数：版本号与摘要
# ==================================================================
def test_version_changes_with_task_count(web_home):
    """建一个任务 → 版本号必须变（否则 SSE 永远不推，等于没做）。"""
    from media_resource_manager import webui
    from media_resource_manager.db import Database

    db = Database(_TMP / "v1.db")
    try:
        v0 = webui.event_version(db)
        db.execute("INSERT INTO tasks(name) VALUES ('a')")
        v1 = webui.event_version(db)
        assert v0 != v1, (v0, v1)
        assert ".t1." in v1 or v1.startswith("t1."), v1
    finally:
        db.close()


def test_version_is_idempotent(web_home):
    """同一状态连算两次必须一样 —— 否则前端会收到「变了」其实没变，白拉接口。"""
    from media_resource_manager import webui
    from media_resource_manager.db import Database

    db = Database(_TMP / "v2.db")
    try:
        db.execute("INSERT INTO tasks(name) VALUES ('a')")
        assert webui.event_version(db) == webui.event_version(db)
    finally:
        db.close()


def test_version_covers_review_and_quarantine(web_home):
    """审核数/隔离数变化也要进版本号（订单点名「图库计数/审核数」）。"""
    from media_resource_manager import webui
    from media_resource_manager.db import Database

    db = Database(_TMP / "v3.db")
    try:
        db.execute("INSERT INTO tasks(name) VALUES ('a')")
        v0 = webui.event_version(db)
        db.execute(
            "INSERT INTO images(task_id, source_url) VALUES (1, 'http://x/1.jpg')")
        v_img = webui.event_version(db)
        assert v_img != v0
        db.execute("UPDATE images SET needs_review=1")
        v_rev = webui.event_version(db)
        assert v_rev != v_img
        db.execute("UPDATE images SET needs_review=0, in_quarantine=1")
        v_q = webui.event_version(db)
        assert v_q != v_rev
    finally:
        db.close()


def test_version_covers_queue_progress(web_home):
    """运行中任务的进度只在内存（queue.reports）——少了它任务跑着界面就不动。"""
    from media_resource_manager import webui
    from media_resource_manager.db import Database

    db = Database(_TMP / "v4.db")
    q = type("Q", (), {"reports": {}})()
    try:
        v0 = webui.event_version(db, q)
        q.reports[1] = {"pages": 1, "downloaded": 2, "failed": 0}
        v1 = webui.event_version(db, q)
        assert v0 != v1, (v0, v1)
        q.reports[1] = {"pages": 9, "downloaded": 2, "failed": 0}
        assert webui.event_version(db, q) != v1
    finally:
        db.close()


def test_version_is_order_independent(web_home):
    """dict 遍历顺序不该影响版本号（排序拼串，否则同状态算出两个版本）。

    变异形态：把 sorted(...) 改成直接遍历 ⇒ 这条红。
    """
    from media_resource_manager import webui

    a = type("Q", (), {"reports": {}})()
    b = type("Q", (), {"reports": {}})()
    a.reports = {1: {"pages": 1, "downloaded": 2, "failed": 0},
                 2: {"pages": 3, "downloaded": 4, "failed": 0}}
    b.reports = {2: {"pages": 3, "downloaded": 4, "failed": 0},
                 1: {"pages": 1, "downloaded": 2, "failed": 0}}
    assert webui.event_version(None, a) == webui.event_version(None, b)


def test_snapshot_never_raises_and_has_no_detail(web_home):
    """摘要绝不抛 + 绝不携带明细（SSE 是常驻连接，泄露面比一次性接口大）。"""
    from media_resource_manager import webui

    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def failed_url_count(self):
            raise RuntimeError("db down")

    snap = webui.event_snapshot(Boom())
    assert snap["tasks"] == 0 and snap["images"] == 0
    v = webui.event_version(Boom(), None)
    assert isinstance(v, str) and v

    # ⚠ 键集合白名单（不是"内容里搜不到敏感词"那种软判据）：
    # 往 out 里塞一个 source_url 键，靠 blob 搜索会漏（异常 db 下塞不进去），
    # 直接盯键集合才能抓住。SSE 是常驻连接，多一个明细键就是多一份泄露。
    #
    # R167 补 `gallery`：左侧导航「图库」角标需要一个**与桌面端同口径**的
    # 计数（有 path 的总张数），旧的四项里没有这个语义（`images` 是净值）。
    # 它是纯 COUNT 数字、无任何明细，加入白名单不削弱本判据的防泄露意图。
    # V0.0.18 补 `notify_id`：通知中心最大行 id（铃铛实时性），同为纯数字、
    # 无标题无正文，判据意图不变。
    allowed = {"tasks", "images", "pending", "quarantine", "gallery",
               "failed_urls", "progress", "notify_id"}
    assert set(snap.keys()) <= allowed, set(snap.keys()) - allowed
    blob = json.dumps(snap, ensure_ascii=False)
    for leak in ("source_url", "path", "name", "sha256"):
        assert leak not in blob, leak

    # 真库场景再验一次：有图也不许把网址/路径带出来
    from media_resource_manager.db import Database
    db = Database(_TMP / "leak.db")
    try:
        db.execute("INSERT INTO tasks(name) VALUES ('a')")
        db.execute("INSERT INTO images(task_id, source_url) VALUES "
                   "(1, 'http://secret.example/1.jpg')")
        real = webui.event_snapshot(db)
        assert set(real.keys()) <= allowed, set(real.keys()) - allowed
        assert "secret.example" not in json.dumps(real, ensure_ascii=False)
    finally:
        db.close()
    assert webui.event_snapshot(None)["tasks"] == 0


def test_version_is_human_readable(web_home):
    """版本号用明文（t/i/p/q/f 段），不用 hash：DevTools 里要能看出哪项变了。"""
    from media_resource_manager import webui
    from media_resource_manager.db import Database

    db = Database(_TMP / "v5.db")
    try:
        v = webui.event_version(db)
    finally:
        db.close()
    # V0.0.18：版本号新增 n 段（通知最大 id），保持明文可排查。
    assert re.match(r"^t\d+\.i\d+\.p\d+\.q\d+\.f\d+\.n\d+\.g\[.*\]$", v), v


# ==================================================================
# 二、真起服务：SSE 行为
# ==================================================================
class _Cli:
    """最简客户端：登录拿 cookie，之后能发原始 socket 请求（读 SSE 长流）。"""

    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie = ""
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def login(self, pw: str = "pw123") -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/login",
            data=json.dumps({"password": pw}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=15) as r:
            assert r.status == 200
        for c in self.jar:
            self.cookie = f"{c.name}={c.value}"
            break

    def raw_get(self, path: str, timeout: float = 10.0):
        """发请求后**不关**，返回 (socket, 已读到的头字节)。"""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        s.sendall(("GET %s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   "Accept: text/event-stream\r\nCookie: %s\r\n\r\n"
                   % (path, self.cookie)).encode("utf-8"))
        return s


def _read_until(sock, needle: bytes, limit: float = 8.0) -> bytes:
    import time
    end = time.time() + limit
    buf = b""
    while time.time() < end and needle not in buf:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return buf


@pytest.fixture()
def sse_server(web_home):
    """真起 WebServer（密码登录）+ 把轮询/心跳间隔压到测试能跑的量级。"""
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    cfg = AppConfig()
    cfg.db_path = str(_TMP / "sse.db")
    cfg.web_password = "pw123"
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_lan_open = False
    cfg.web_ip_whitelist = []

    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    db = Database(_TMP / "sse.db")
    srv = webui.WebServer(cfg, db)
    ok, msg = srv.start()
    assert ok, f"服务器起不来：{msg}"

    old = (webui.SSE_POLL_SEC, webui.SSE_HEARTBEAT_SEC)
    webui.SSE_POLL_SEC = 0.2
    webui.SSE_HEARTBEAT_SEC = 0.5
    try:
        yield webui, cfg, db, _Cli(cfg.web_port)
    finally:
        webui.SSE_POLL_SEC, webui.SSE_HEARTBEAT_SEC = old
        srv.stop()
        db.close()
        cfgmod.save_config = orig_save


def test_events_stream_headers(sse_server):
    """SSE 三件套响应头：content-type / 反代关缓冲 / chunked。

    少了 X-Accel-Buffering 就是「nginx 后面 SSE 一条都收不到」——
    订单点名要 Docker 友好，这条是它的落点。
    """
    webui, cfg, db, cli = sse_server
    cli.login()
    s = cli.raw_get("/api/events")
    try:
        head = _read_until(s, b"\r\n\r\n")
        low = head.lower()
        assert b"200" in head.split(b"\r\n")[0], head[:120]
        assert b"text/event-stream" in low, head
        assert b"x-accel-buffering: no" in low, head
        assert b"no-cache" in low, head
        assert b"chunked" in low, head
    finally:
        s.close()


def test_events_first_frame_carries_version(sse_server):
    """首帧必须带 retry + 版本号（前端拿它做 since 基线）。"""
    webui, cfg, db, cli = sse_server
    cli.login()
    s = cli.raw_get("/api/events")
    try:
        buf = _read_until(s, b"retry:")
        assert b"retry: 3000" in buf, buf[:200]
        buf = _read_until(s, b"data:")
        txt = buf.decode("utf-8", "replace")
        assert '"version"' in txt, txt[:300]
    finally:
        s.close()


def test_events_pushes_on_change(sse_server):
    """数据变了要推第二帧，且是 change 事件（这是整条功能的价值所在）。"""
    webui, cfg, db, cli = sse_server
    cli.login()
    s = cli.raw_get("/api/events")
    try:
        first = _read_until(s, b"data:")
        db.execute("INSERT INTO tasks(name) VALUES ('new')")
        more = _read_until(s, b"event: change", limit=8.0)
        assert b"event: change" in more, more[:300]
        # 版本号确实变了（不是同一帧重发）
        vs = set(re.findall(rb'"version": "([^"]+)"', first + more))
        assert len(vs) >= 2, (first[:200], more[:300])
    finally:
        s.close()


def test_events_since_matched_emits_snapshot(sse_server):
    """带上与当前一致的版本号重连 → 首帧是 snapshot（不是 change），
    免得刚连上就白拉一次列表。"""
    webui, cfg, db, cli = sse_server
    cli.login()
    v = webui.event_version(db)
    s = cli.raw_get("/api/events?since=" + v)
    try:
        buf = _read_until(s, b"data:")
        assert b"event: snapshot" in buf, buf[:300]
    finally:
        s.close()


def test_events_heartbeat_keeps_connection_alive(sse_server):
    """心跳必须发：`_Handler.timeout=30` 会砍掉静默连接，心跳比它密才不掉线。"""
    webui, cfg, db, cli = sse_server
    cli.login()
    s = cli.raw_get("/api/events")
    try:
        _read_until(s, b"data:")
        buf = _read_until(s, b": ping", limit=8.0)
        assert b": ping" in buf, buf[:300]
    finally:
        s.close()


def test_events_slot_limit_and_release(sse_server):
    """并发闸：占满上限后新连接 503 + 中文原因；关掉一条立刻能再连。

    不限流的话 8 个标签页就能把 64 个线程槽吃干（R152-E1 的闸会被绕过）。
    """
    webui, cfg, db, cli = sse_server
    cli.login()
    socks = []
    try:
        for _ in range(webui.SSE_MAX_CLIENTS):
            s = cli.raw_get("/api/events", timeout=5)
            _read_until(s, b"retry:", limit=5.0)
            socks.append(s)
        # 满了：新连接应被拒（503，且是 JSON 中文提示，不是 HTML）
        req = urllib.request.Request(
            f"http://127.0.0.1:{cli.port}/api/events",
            headers={"Cookie": cli.cookie, "Accept": "text/event-stream"})
        try:
            with cli.opener.open(req, timeout=10) as r:
                pytest.fail(f"超上限竟放行：{r.status}")
        except urllib.error.HTTPError as e:
            assert e.code == 503, e.code
            body = e.read().decode("utf-8")
            assert "事件流过多" in body, body[:200]
        # 释放一条 → 服务端应在 1 个探测周期内收摊，槽位还回来
        # （对端 FIN 的检测是 select+peek，不是等下一次写失败 ⇒ 亚秒级）
        socks.pop().close()
        import time as _t
        ok = False
        for _ in range(6):
            _t.sleep(0.3)
            s2 = cli.raw_get("/api/events", timeout=5)
            head = _read_until(s2, b"\r\n\r\n", limit=5.0)
            if b"200" in head[:20]:
                socks.append(s2)
                ok = True
                break
            s2.close()
        assert ok, "关掉一条连接后槽位没还回来（泄漏）"
    finally:
        for s in socks:
            try:
                s.close()
            except Exception:
                pass


def test_events_requires_auth(sse_server):
    """SSE 在鉴权之后：没 cookie 不能拿到事件流（否则等于绕过登录看数据）。"""
    webui, cfg, db, cli = sse_server       # 故意不 login
    req = urllib.request.Request(
        f"http://127.0.0.1:{cli.port}/api/events",
        headers={"Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            pytest.fail(f"未登录竟放行：{r.status}")
    except urllib.error.HTTPError as e:
        assert e.code in (401, 403), e.code


def test_snapshot_matches_api_summary(sse_server):
    """SSE 摘要与 /api/summary **同口径**：两边打架用户就看到「推了变化数没变」。

    这是防「以后改了一边忘了另一边」的钉子（订单纪律：不另造统计口径）。
    """
    webui, cfg, db, cli = sse_server
    cli.login()
    db.execute("INSERT INTO tasks(name) VALUES ('a')")
    db.execute("INSERT INTO images(task_id, source_url) VALUES (1, 'http://x/1.jpg')")
    req = urllib.request.Request(
        f"http://127.0.0.1:{cli.port}/api/summary",
        headers={"Cookie": cli.cookie})
    with cli.opener.open(req, timeout=10) as r:
        api = json.loads(r.read().decode("utf-8"))
    snap = webui.event_snapshot(db)
    for k in ("tasks", "images", "pending", "quarantine"):
        assert snap[k] == api[k], (k, snap[k], api[k])


# ==================================================================
# 三、源码判据（接线在不在，防「写了函数没接路由」）
# ==================================================================
def test_webui_source_wiring():
    src = WEBUI.read_text(encoding="utf-8")
    assert 'path == "/api/events"' in src          # 路由字面量（不可改拼接）
    assert "text/event-stream" in src
    assert "X-Accel-Buffering" in src
    assert "_sse_slot_acquire" in src and "_sse_slot_release" in src
    assert "def event_version(" in src
    assert "def event_snapshot(" in src
    assert "def _events_loop(" in src
    # 必须排在鉴权之后：源码里 _auth_ok 的分支要在路由之前
    assert src.index('if not self._auth_ok()') < src.index('path == "/api/events"')
    # SSE 不能在 try→500 JSON 里（头已发出）
    seg = src[src.index('path == "/api/events"'):src.index('path == "/api/events"') + 200]
    assert "_serve_events" in seg
