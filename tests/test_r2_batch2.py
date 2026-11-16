"""第 2 批护栏（编号 21/22/23/32/33/34/35）。

写法惯例（与本仓其它护栏一致）：**行为断言优先**，源码判据只用来钉死
「这件事不许被悄悄改回去」。每条都是踩过一次才写下来的，注释里说明原因，
免得后来的人觉得"这行测试多余"把它删了。
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(tmp_path):
    from media_resource_manager.db import Database
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


@pytest.fixture()
def cfg(tmp_path):
    from media_resource_manager.config import AppConfig
    c = AppConfig()
    c.db_path = tmp_path / "t.db"
    c.library_dir = tmp_path / "lib"
    c.rules_dir = tmp_path / "rules"
    c.isolate_dir = tmp_path / "iso"
    c.review_dir = tmp_path / "rev"
    c.log_dir = tmp_path / "log"
    return c


# ---------------------------------------------------------------- 配置默认值
def test_new_config_fields_have_safe_defaults():
    """新键的默认值必须"不改变既有行为"（升级即失能是最不能接受的）。"""
    from media_resource_manager.config import AppConfig
    c = AppConfig()
    assert c.web_https is False            # 默认仍只有 HTTP
    assert c.web_https_port == 8443
    assert c.web_https_redirect is False
    assert c.bind_ip == ""                 # 系统选网卡
    assert c.ssl_verify is True            # 默认照常校验证书
    assert c.ssl_insecure_hosts == ""
    assert c.allow_private_hosts is True   # 老用户的本地 NAS 任务不能被拦
    assert c.sub_enabled is True
    assert c.sub_interval_min == 60
    assert c.slow_yield_enabled is False   # 抢跑行为必须用户自己开


def test_new_config_fields_roundtrip(tmp_path):
    """存盘→读回必须一致（漏字段 = 重启后设置悄悄回默认）。"""
    from media_resource_manager.config import load_config, save_config
    import os
    os.environ["MRM_HOME"] = str(tmp_path)
    try:
        c = load_config()
        c.web_https = True
        c.web_https_port = 9443
        c.bind_ip = "127.0.0.1"
        c.ssl_insecure_hosts = "nas.local, 10.0.0.5"
        c.slow_yield_enabled = True
        c.slow_yield_min = 7
        save_config(c)
        c2 = load_config()
        assert c2.web_https is True
        assert c2.web_https_port == 9443
        assert c2.bind_ip == "127.0.0.1"
        assert c2.ssl_insecure_hosts == "nas.local, 10.0.0.5"
        assert c2.slow_yield_min == 7
    finally:
        os.environ.pop("MRM_HOME", None)


def test_settings_whitelist_matches_real_fields():
    """白名单里的每个键都必须在 AppConfig 上真实存在。

    ⚠ 这就是「改了没反应」的根因：白名单写了、dataclass 里没有 ⇒
    setattr 进去没人读、接口还回 ok。校验必须用 dataclasses.fields。
    """
    from media_resource_manager.config import AppConfig
    from media_resource_manager.webui import _Handler
    real = {f.name for f in dataclasses.fields(AppConfig)}
    missing = sorted(k for k in _Handler._SETTINGS_WRITABLE if k not in real)
    assert not missing, f"白名单里有 cfg 上不存在的键：{missing}"


def test_batch2_settings_are_writable():
    from media_resource_manager.webui import _Handler
    for k in ("web_https", "web_https_port", "web_https_redirect",
              "bind_ip", "ssl_verify", "ssl_insecure_hosts",
              "allow_private_hosts", "sub_enabled", "sub_interval_min",
              "slow_yield_enabled", "slow_yield_min"):
        assert k in _Handler._SETTINGS_WRITABLE, f"{k} 不能在网页上改"


# ---------------------------------------------------------------- 21/22 订阅
def test_extract_links_absolute_and_skip_nav():
    from media_resource_manager.subscribe import extract_links
    html = """
    <a href="/post/1">1</a>
    <a href="post/2#comments">2</a>
    <a href="/about">about</a>
    <a href="/tag/cat">tag</a>
    <a href="https://other.example.com/x">外站</a>
    <a href="/post/1">重复</a>
    """
    links = extract_links(html, "https://site.test/list/1")
    # ⚠ 相对路径按页面 URL 解析：「post/2」相对 /list/1 → /list/post/2。
    #   这里要的是"和浏览器一致"，不是"看起来像我以为的"（第一版断言就写错了）。
    assert links == ["https://site.test/post/1",
                     "https://site.test/list/post/2"], links


def test_extract_links_pattern_wins():
    from media_resource_manager.subscribe import extract_links
    html = '<a href="/p/11">a</a><a href="/p/22">b</a><a href="/tag/x">c</a>'
    links = extract_links(html, "https://s.test/", r"/p/\d+")
    assert links == ["https://s.test/p/11", "https://s.test/p/22"]


def test_sub_save_validates_url_and_pattern(db):
    from media_resource_manager import subscribe as sub
    ok, msg, _ = sub.save_sub(db, {"url": "ftp://x"})
    assert not ok and "http" in msg
    ok, msg, _ = sub.save_sub(db, {"url": "https://a.test/x", "pattern": "([bad"})
    assert not ok and "正则" in msg
    ok, _, sid = sub.save_sub(db, {"url": "https://a.test/x", "interval_min": 1})
    assert ok and sid > 0
    # 间隔钳到下限（防止用户填 1 分钟把站点砸了）
    assert sub.get_sub(db, sid)["interval_min"] == sub.MIN_INTERVAL_MIN


def test_sub_check_creates_task_once_and_remembers(db, cfg, monkeypatch):
    """核心闭环：第一次有新条目→建任务；第二次同页面→0 条新。"""
    from media_resource_manager import subscribe as sub
    page = '<a href="/post/1">1</a><a href="/post/2">2</a><a href="/post/3">3</a>'
    monkeypatch.setattr(sub, "_fetch_html", lambda c, u: page)
    ok, _, sid = sub.save_sub(db, {"url": "https://a.test/list", "max_items": 2})
    assert ok
    s = sub.get_sub(db, sid)
    r1 = sub.check_sub(db, cfg, s)
    assert r1["ok"] and r1["new"] == 2 and r1["task_id"] > 0
    r2 = sub.check_sub(db, cfg, s)
    assert r2["ok"] and r2["new"] == 0 and r2["task_id"] == 0
    # 快照记的是**整页**（3 条），不是只有建任务的那 2 条
    assert sub.snapshot_count(db, sid) == 3


def test_sub_check_max_items_caps_task_urls(db, cfg, monkeypatch):
    from media_resource_manager import subscribe as sub
    from media_resource_manager.repo import get_task
    page = "".join(f'<a href="/p/{i}">x</a>' for i in range(10))
    monkeypatch.setattr(sub, "_fetch_html", lambda c, u: page)
    sub.save_sub(db, {"url": "https://a.test/l", "max_items": 3})
    s = sub.list_subs(db)[0]
    r = sub.check_sub(db, cfg, s)
    t = get_task(db, r["task_id"])
    assert len(t.entry_urls) == 3, t.entry_urls


def test_sub_fetch_error_is_recorded_not_swallowed(db, cfg, monkeypatch):
    """抓不到页面必须落 last_error，不能"0 条新"地糊过去。"""
    from media_resource_manager import subscribe as sub

    def _boom(c, u):
        raise OSError("connection refused")

    monkeypatch.setattr(sub, "_fetch_html", _boom)
    sub.save_sub(db, {"url": "https://a.test/l"})
    s = sub.list_subs(db)[0]
    r = sub.check_sub(db, cfg, s)
    assert not r["ok"] and r["error"]
    assert sub.get_sub(db, s["id"])["last_error"]


def test_sub_delete_removes_snapshot(db, cfg, monkeypatch):
    from media_resource_manager import subscribe as sub
    monkeypatch.setattr(sub, "_fetch_html",
                        lambda c, u: '<a href="/p/1">x</a>')
    sub.save_sub(db, {"url": "https://a.test/l"})
    s = sub.list_subs(db)[0]
    sub.check_sub(db, cfg, s)
    assert sub.snapshot_count(db, s["id"]) == 1
    sub.delete_sub(db, s["id"])
    assert sub.list_subs(db) == []
    assert sub.snapshot_count(db, s["id"]) == 0


def test_due_subs_respects_interval(db, cfg):
    from media_resource_manager import subscribe as sub
    sub.save_sub(db, {"url": "https://a.test/l", "interval_min": 30})
    s = sub.list_subs(db)[0]
    now = 1_000_000.0
    assert sub.due_subs(db, cfg, {}, now)        # 从没查过 → 到点
    assert not sub.due_subs(db, cfg, {s["id"]: now - 60}, now)   # 1 分钟前查过
    assert sub.due_subs(db, cfg, {s["id"]: now - 31 * 60}, now)  # 31 分钟前


# ---------------------------------------------------------------- 23 证书
def test_self_signed_cert_generated_and_reused(tmp_path):
    from media_resource_manager import tls
    ok, msg = tls.ensure_self_signed(tmp_path)
    assert ok, msg
    assert (tmp_path / tls.CERT_NAME).is_file()
    assert (tmp_path / tls.KEY_NAME).is_file()
    ok2, msg2 = tls.ensure_self_signed(tmp_path)
    assert ok2 and "沿用" in msg2, msg2          # 第二次必须复用，不重签
    info = tls.cert_info(tmp_path)
    assert info["exists"] and info["days_left"] > 3000
    ctx, msg3 = tls.make_ssl_context(tmp_path)
    assert ctx is not None, msg3


def test_ssl_context_never_raises_on_bad_dir(tmp_path):
    """证书目录不可写 / 组件缺失时必须返回中文原因，**绝不抛异常**
    （它是在 WebServer.start 里调的，抛出去就等于 Web 起不来）。"""
    from media_resource_manager import tls
    bad = tmp_path / "x"
    bad.write_text("i am a file, not a dir", encoding="utf-8")
    ctx, msg = tls.make_ssl_context(bad)
    assert ctx is None and msg


# ---------------------------------------------------------------- 32/33/34 网络
def test_validate_bind_ip_rejects_foreign_address():
    from media_resource_manager import nets
    ips = nets.list_local_ips()
    if not ips:
        pytest.skip("本机枚举不到网卡地址（容器环境）")
    ok, why = nets.validate_bind_ip("10.99.99.99")
    assert not ok and "可选" in why
    ok2, _ = nets.validate_bind_ip(ips[0])
    assert ok2
    ok3, _ = nets.validate_bind_ip("")
    assert ok3                                   # 空 = 自动，永远合法


def test_list_local_ips_filters_apipa():
    from media_resource_manager import nets
    for ip in nets.list_local_ips():
        assert not ip.startswith("169.254."), ip


def test_fetcher_honours_insecure_hosts_and_private_flag(cfg):
    from media_resource_manager.crawler.fetcher import Fetcher, AccessBlocked
    cfg.ssl_verify = True
    cfg.ssl_insecure_hosts = "nas.local, 10.0.0.5"
    f = Fetcher(cfg)
    assert f._want_verify("https://www.nas.local/x") is False     # 子域也认
    assert f._want_verify("https://10.0.0.5/x") is False
    assert f._want_verify("https://other.test/x") is True
    # 总开关关掉 = 全不校验
    cfg.ssl_verify = False
    f2 = Fetcher(cfg)
    assert f2._want_verify("https://other.test/x") is False
    cfg.ssl_verify = True
    # 编号 34：默认放行内网
    assert Fetcher(cfg)._check_ip_filter("http://127.0.0.1:9/x") is None
    cfg.allow_private_hosts = False
    f3 = Fetcher(cfg)
    with pytest.raises(AccessBlocked) as ei:
        f3._check_ip_filter("http://127.0.0.1:9/x")
    assert "内网" in str(ei.value)


def test_gallery_backend_verify_flag_reaches_config(tmp_path):
    """编号 33 的快路径半边：gallery-dl 的开关叫 extractor.verify，
    传错位置 = 自签站永远 0 图（而界面看着一切正常）。"""
    from media_resource_manager.backends.gallery_dl import GalleryDLBackend
    be = GalleryDLBackend(dest_root=tmp_path, verify=False)
    assert be._make_config()["extractor"]["verify"] is False
    be2 = GalleryDLBackend(dest_root=tmp_path)
    assert be2._make_config()["extractor"]["verify"] is True


def test_https_redirect_helper_only_when_enabled(cfg):
    """强制跳转的三个必要条件（少一个都会把 HTTP 用户挡在门外）。"""
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    assert "def _redirect_to_https" in src
    assert 'self.headers.get("Host")' in src
    # 必须同时看 web_https 与 web_https_redirect，且 HTTPS 请求不跳自己
    body = src.split("def _redirect_to_https", 1)[1].split("def _send_security_headers", 1)[0]
    assert "web_https" in body and "web_https_redirect" in body
    assert "_is_https()" in body


# ---------------------------------------------------------------- 35 慢任务让位
def test_slow_yield_default_off_and_state_isolated(cfg):
    from media_resource_manager.scheduler.queue import TaskQueue
    q = TaskQueue(cfg, max_concurrent=1)
    assert cfg.slow_yield_enabled is False
    assert q._prog == {} and q._yielded == set()
    assert q.snapshot()["yields"] == []


def test_slow_yield_marks_not_stopped(cfg):
    """让位 ≠ 用户停止：状态必须是 IDLE（回队列），不是 STOPPED。

    ⚠ 这条钉的是 `_run_one` 里 Cancel 分支的分流：搞错的话用户会看到
    「我什么都没点，任务自己变红/变灰了」。
    """
    src = (ROOT / "media_resource_manager" / "scheduler" / "queue.py").read_text(
        encoding="utf-8")
    # ⚠ 必须先在 _run_one 里找：文件里还有 _worker 的 Cancel 分支，
    #   直接 split 第一处会切到那儿去（第一次写就切错了）。
    run_one = src.split("async def _run_one", 1)[1]
    seg = run_one.split("except asyncio.CancelledError:", 1)[1]
    seg = seg.split("except Exception as e:", 1)[0]
    assert "_yielded" in seg
    assert "TaskStatus.IDLE" in seg
    assert seg.index("_yielded") < seg.index("暂停") if "暂停" in seg else True


def test_slow_yield_requires_queue_pressure(cfg):
    """没人排队时绝不允许让位（否则就是"好好的任务被莫名停掉"）。"""
    src = (ROOT / "media_resource_manager" / "scheduler" / "queue.py").read_text(
        encoding="utf-8")
    seg = src.split("async def _slow_yield_watchdog", 1)[1]
    seg = seg.split("def _yield_one", 1)[0]
    assert "waiting" in seg and "<= 0" in seg
    assert "slow_yield_enabled" in seg


# ---------------------------------------------------------------- 前后端接线
def test_subscription_endpoints_routed():
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    for path in ("/api/subs", "/api/sub/save", "/api/sub/delete",
                 "/api/sub/check", "/api/sub/preview", "/api/sub/items",
                 "/api/net/ips"):
        assert f'if path == "{path}"' in src, f"{path} 没接路由"


def test_vue_has_subscription_page_and_nav():
    web = ROOT / "media_resource_manager" / "webapp" / "src"
    assert (web / "views" / "Subs.vue").is_file()
    main = (web / "main.js").read_text(encoding="utf-8")
    assert "Subs" in main and "/subs" in main
    app = (web / "App.vue").read_text(encoding="utf-8")
    assert "/subs" in app
    settings = (web / "views" / "Settings.vue").read_text(encoding="utf-8")
    for k in ("web_https", "bind_ip", "slow_yield_enabled", "sub_interval_min"):
        assert f"k: '{k}'" in settings, f"设置页缺 {k}"


def test_spec_lists_cryptography_x509():
    """打包漏了 cryptography.x509 → 开了 HTTPS 也起不来（源码跑得好好的）。"""
    spec = (ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8")
    assert "'cryptography.x509'" in spec
    assert "asymmetric.ec" in spec
