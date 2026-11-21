"""V0.0.78 修复护栏：Web 安全闸 / 导入竞态 / 导出 offset / ids 空集 / Retry-After。

五条都是审查确认过的真实 bug，每条一组回归测试：
  1. _api_settings_save 安全闸命中时必须完整回滚三个安全键（含「只改
     web_password/web_lan_open、web_bind 沿用旧值」的情形）；
  2. 三处导入端点把「检查 + 占位」合并进同一把 _IMPORT_LOCK 临界区，
     并发/双击时只放一个进去；
  3. /api/export/file 的 offset 做下界钳制（负 offset → 0）；
  4. export_training_kit 里 ids 给了空列表/全非法时必须导出 0 张，
     只有 ids=None 才保留按分数捞全量的口径；
  5. Retry-After 支持 HTTP-date，不再用裸 float() 打断任务线程。
"""
import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_resource_manager import webui


class H(webui._Handler):
    """只测业务方法、不走真 socket 的最小 handler（沿用 test_r48 的写法）。"""

    def __init__(self, cfg, db, headers=None):
        self._cfg = cfg
        self._db = db
        self.server = SimpleNamespace(queue=None)
        self._headers = headers or {}
        self._path = "/api/x"
        self._sent = None

    @property
    def cfg(self):
        return self._cfg

    @property
    def db(self):
        return self._db

    @property
    def headers(self):
        return self._headers

    @property
    def path(self):
        return self._path

    def _send(self, code, body, ctype, extra=None):
        self._sent = (code, body, ctype, extra or {})
        return self._sent

    def _json(self, obj, code=200, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(code, raw, "application/json; charset=utf-8", extra)


@pytest.fixture
def handler(tmp_db, monkeypatch):
    """真 DB（临时文件）+ 免落盘 cfg + handler。"""
    from media_resource_manager.config import AppConfig
    import media_resource_manager.config as cfgmod

    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)
    cfg = AppConfig()
    cfg.db_path = str(getattr(tmp_db, "db_path", "") or "")
    return H(cfg, tmp_db)


# ====================================================================
# ① _api_settings_save 安全闸：回滚必须覆盖三个安全键
# ====================================================================

def test_settings_gate_rolls_back_when_web_bind_not_in_body(handler):
    """★ 核心回归：body 不带 web_bind，沿用旧值 0.0.0.0，只改密码/开关。

    修复前：9977 只在 "web_bind" in changed 才回滚 → 空密码+非回环绑定
    留在内存里立即生效（内网任意客户端免密接管）。修复后三个键全部
    从快照还原。
    """
    cfg = handler.cfg
    cfg.web_bind = "0.0.0.0"
    cfg.web_password = "old-good-password"
    cfg.web_lan_open = True

    r = handler._api_settings_save({"web_password": "", "web_lan_open": True})

    assert r["ok"] is False, r
    assert "密码" in r["error"], r
    assert cfg.web_bind == "0.0.0.0", "web_bind 本就没改，不该被乱动"
    assert cfg.web_password == "old-good-password", "空密码没被回滚！"
    assert cfg.web_lan_open is True, "web_lan_open 没被回滚！"


def test_settings_gate_rolls_back_all_three_keys(handler):
    """三个安全键一次性全改（非回环 + 空密码 + 允许局域网）→ 全部还原。"""
    cfg = handler.cfg
    cfg.web_bind = "127.0.0.1"
    cfg.web_password = "old-good-password"
    cfg.web_lan_open = False

    r = handler._api_settings_save({
        "web_bind": "0.0.0.0", "web_password": "", "web_lan_open": True,
    })

    assert r["ok"] is False, r
    assert cfg.web_bind == "127.0.0.1"
    assert cfg.web_password == "old-good-password"
    assert cfg.web_lan_open is False


def test_settings_gate_allows_nonloopback_with_password(handler):
    """正向对照：非回环 + 非空密码必须放行，不能把正常配置也回滚了。"""
    cfg = handler.cfg
    cfg.web_bind = "127.0.0.1"
    cfg.web_password = "old-good-password"
    cfg.web_lan_open = False

    r = handler._api_settings_save({
        "web_bind": "0.0.0.0", "web_password": "new-strong-pw",
        "web_lan_open": True,
    })

    assert r["ok"] is True, r
    assert cfg.web_bind == "0.0.0.0"
    assert cfg.web_password == "new-strong-pw"
    assert cfg.web_lan_open is True


# ====================================================================
# ② 导入启动 check-then-act：三处端点并发只放一个
# ====================================================================

@pytest.mark.parametrize("method,body_of", [
    ("_api_import_folder",
     lambda d: {"folder": str(d), "tag": False}),
    ("_api_import_webdav",
     lambda _d: {"url": "https://dav.example.com", "tag": False}),
    ("_api_import_remote",
     lambda _d: {"protocol": "ftp", "host": "example.com", "tag": False}),
])
def test_import_start_is_atomic(tmp_path, monkeypatch, method, body_of):
    """双击/并发 N 次：即使 is_running() 恒为 False（后台线程未起），
    也只有第一个请求拿到占位；其余必须被锁内的 _IMPORT_STATE 检查挡住。
    """
    from media_resource_manager import import_folder as imp

    # 模拟「后台线程还没起来」—— 这是竞态能发生的前提
    monkeypatch.setattr(imp, "is_running", lambda: False)

    started = []

    class _FakeThread:
        def __init__(self, target=None, **kw):
            self._target = target

        def start(self):
            started.append(self._target)

    real_threading = threading
    monkeypatch.setattr(
        webui, "threading",
        SimpleNamespace(Thread=_FakeThread, Event=real_threading.Event,
                        Lock=real_threading.Lock))

    with webui._IMPORT_LOCK:
        webui._IMPORT_STATE["job"] = ""
        webui._IMPORT_STATE["cancel"] = None
    try:
        from media_resource_manager.config import AppConfig
        h = H(AppConfig(), tmp_db_stub(tmp_path))

        src = tmp_path / "pics"
        src.mkdir(exist_ok=True)
        body = body_of(src)

        n = 8
        barrier = threading.Barrier(n)
        results = []

        def _worker():
            barrier.wait()
            results.append(getattr(h, method)(body))

        threads = [threading.Thread(target=_worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert len(results) == n, "有并发请求没返回"
        ok = [r for r in results if r.get("ok")]
        rejected = [r for r in results if not r.get("ok")]
        assert len(ok) == 1, f"应只放一个导入进去，实得 {len(ok)}：{results}"
        assert len(rejected) == n - 1
        assert all("已有一个导入任务在跑" in r.get("error", "")
                   for r in rejected), rejected
        # 只有一个后台线程被真正拉起
        assert len(started) == 1, f"起了 {len(started)} 条导入线程"
        with webui._IMPORT_LOCK:
            assert webui._IMPORT_STATE["job"] == ok[0]["job"], \
                "占位 job 被后来者覆盖了"
    finally:
        with webui._IMPORT_LOCK:
            webui._IMPORT_STATE["job"] = ""
            webui._IMPORT_STATE["cancel"] = None


def tmp_db_stub(tmp_path):
    """导入端点只在 finally/审计里用 db；给个真库最省事。"""
    from media_resource_manager.db import Database
    return Database(tmp_path / "import_race.db")


# ====================================================================
# ③ /api/export/file offset 下界钳制
# ====================================================================

@pytest.mark.parametrize("raw,expected", [
    ("-1000000", 0),
    ("-3", 0),
    ("4", 4),
    ("not-a-number", 0),
])
def test_export_file_report_offset_clamped(handler, monkeypatch, raw, expected):
    from media_resource_manager import export_kit as ek

    seen = {}

    def _fake_report_bytes(db, period, offset):
        seen["offset"] = offset
        return b"report"

    monkeypatch.setattr(ek, "report_bytes", _fake_report_bytes)
    monkeypatch.setattr(ek, "encrypted", lambda data, pw: b"blob")

    u = SimpleNamespace(query=f"kind=report&offset={raw}&period=month")
    h = handler
    h._api_export_file(u)

    assert seen.get("offset") == expected, \
        f"offset={raw} 被钳成 {seen.get('offset')}，应为 {expected}"


# ====================================================================
# ④ export_training_kit：ids 空集必须导出 0 张
# ====================================================================

@pytest.fixture
def export_env(tmp_path, tmp_db, monkeypatch):
    from media_resource_manager import export_kit as ek
    from media_resource_manager.config import AppConfig

    src = tmp_path / "src"
    src.mkdir()
    for i in (1, 2):
        (src / f"img{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 32)

    rows = [{
        "id": i, "task_id": 0, "path": str(src / f"img{i}.jpg"),
        "tags": "", "score": 0.9, "source_url": "", "collected_at": "",
    } for i in (1, 2)]
    norm = {
        "min_score": 0.35, "limit": 10, "task_id": 0, "tag": "",
        "only_favorite": False, "extra_terms": [], "group_by": "flat",
        "caption_template": "",
    }
    monkeypatch.setattr(ek, "_filter_rows", lambda db, opt: (rows, norm))
    # 本组只验 ids 过滤口径；pytest 临时目录在 AppData 下会被 _check_dest
    # 当系统目录拒掉，与被测逻辑无关，桩掉即可。
    monkeypatch.setattr(ek, "_check_dest", lambda dest, dirs: "")
    cfg = AppConfig()
    return ek, tmp_db, cfg, tmp_path / "out"


def test_export_ids_empty_list_exports_nothing(export_env):
    ek, db, cfg, dest = export_env
    res = ek.export_training_kit(db, cfg, dest, {"ids": []})
    assert res["ok"] is False, "ids=[]（一个都没选中）却导出了图片"
    assert "未找到符合条件" in res["error"]


def test_export_ids_all_invalid_exports_nothing(export_env):
    ek, db, cfg, dest = export_env
    res = ek.export_training_kit(db, cfg, dest, {"ids": ["bad", "oops"]})
    assert res["ok"] is False, "ids 全非法却导出了图片"


def test_export_ids_none_keeps_full_filter(export_env):
    """ids 没给（None）时必须保留原来的按分数全量口径。"""
    ek, db, cfg, dest = export_env
    res = ek.export_training_kit(db, cfg, dest, {"ids": None})
    assert res["ok"] is True, res
    assert res["copied"] == 2


def test_export_ids_subset_still_works(export_env):
    ek, db, cfg, dest = export_env
    res = ek.export_training_kit(db, cfg, dest, {"ids": [2]})
    assert res["ok"] is True, res
    assert res["copied"] == 1


# ====================================================================
# ⑤ Retry-After HTTP-date
# ====================================================================

def test_parse_retry_after_delay_seconds():
    from media_resource_manager.crawler.fetcher import parse_retry_after
    assert parse_retry_after("3", 2) == 3.0
    assert parse_retry_after("-5", 0) == 0.0


def test_parse_retry_after_http_date_future():
    from media_resource_manager.crawler.fetcher import parse_retry_after
    future = datetime.now(timezone.utc) + timedelta(seconds=10)
    header = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
    got = parse_retry_after(header, 2)
    assert 5 <= got <= 11, f"HTTP-date 没转成相对秒：{got}"


def test_parse_retry_after_http_date_past():
    from media_resource_manager.crawler.fetcher import parse_retry_after
    got = parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 2)
    assert got == 0.0


def test_parse_retry_after_garbage_falls_back():
    from media_resource_manager.crawler.fetcher import parse_retry_after
    assert parse_retry_after("not-a-date", 3) == 8.0   # 2**3
    assert parse_retry_after(None, 9) == 30.0         # 封顶 30


def test_fetch_text_survives_http_date_429():
    """端到端：429 + HTTP-date 不得抛 ValueError 打断任务线程。"""
    from media_resource_manager.crawler.fetcher import Fetcher
    from media_resource_manager.crawler.robots import RobotsPolicy

    class _Cfg:
        delay = 0
        retry = 1
        ip_filter_enabled = False
        ip_filter_text = ""
        speed_skip_lan = True
        ssl_verify = True

    f = Fetcher(_Cfg())

    async def _noop(*a, **k):
        return None

    async def _robots(url):
        return RobotsPolicy("")

    async def _client(url):
        return object()

    f._ensure_robots = _robots
    f._throttle = _noop
    f._client_get = _client

    calls = []

    async def _get_stable(c, url, hdrs):
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(
                status_code=429,
                headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"},
                content=b"", text="")
        return SimpleNamespace(status_code=200, headers={},
                               content=b"ok", text="ok")

    f._get_stable = _get_stable

    text = asyncio.run(f.fetch_text("https://example.com/p"))
    assert text == "ok"
    assert len(calls) == 2, "429 后没有重试"
