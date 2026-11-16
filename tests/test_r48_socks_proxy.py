"""R48 / THOROUGH_AUDIT_ORDER D：SOCKS 代理 socksio 缺失导致任务崩停。

来源：作业单 D（用户第 5 条，严重）—— 代理填 socks5://，一点「开始任务」
整个任务直接崩停，还冒英文。根因：httpx 的 SOCKS 支持是**可选依赖 socksio**
（即 `httpx[socks]`），打包环境里没有，而 `AsyncClient(proxy=...)` 会在
**构造时**抛 ImportError，这条路径上没人 try。

本文件的判据（对应作业单的三条修复）：
  D-1 有 socksio 时能正常建连（不误降级）；
  D-2 没有 socksio 时**异常被捕获并转中文**，且**降级**（任务流程不崩）；
  D-3 `proxy.py` 的 check/acheck 同样 try/except + 中文，且不外抛。

⚠ 测「没有 socksio」不能靠删 `sys.modules`（R37 教训：那样是**假通过**，
   因为 import 缓存 / 包命名空间会留引用）。这里 monkeypatch
   `fetcher.socks_available` 逼它走 except 分支 —— 那才是真实触发点。
"""
import asyncio

import pytest

from media_resource_manager.crawler import fetcher as F
from media_resource_manager.config import AppConfig


SOCKS5 = "socks5://127.0.0.1:1080"
HTTP = "http://127.0.0.1:8080"


# ======================================================= D-0 前置条件本身

def test_socksio_is_installed_in_this_env():
    """环境前提：本机 venv 必须装得上 socksio。

    这条是**打包链的地基**：机器上都没装，打包时更不可能进 `_internal`。
    """
    assert F.socks_available(), (
        "socksio 没装 —— 打包后 SOCKS 代理必崩。"
        "装法：pip install 'httpx[socks]' 或 pip install socksio")


def test_requirements_declares_socksio():
    """护栏：requirements.txt 必须显式列 socksio（作业单 D-1）。

    红→绿：它是注释掉的时候，exe 里必然缺 → SOCKS 一用就崩。
    """
    from pathlib import Path

    req = Path(__file__).resolve().parents[1] / "requirements.txt"
    txt = req.read_text(encoding="utf-8")
    socks_lines = [ln for ln in txt.splitlines()
                   if "socksio" in ln and not ln.strip().startswith("#")]
    assert socks_lines, "requirements.txt 里没有生效的 socksio 依赖行"


def test_spec_hiddenimports_includes_socksio():
    """护栏：PyInstaller hiddenimports 必须含 socksio。

    红→绿：不列它，纯 Python 的 socksio 可能不进 `_internal`。
    """
    import ast
    from pathlib import Path

    spec = Path(__file__).resolve().parents[1] / "build" / "pyinstaller.spec"
    tree = ast.parse(spec.read_text(encoding="utf-8"))
    found = [n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and n.value == "socksio"]
    assert found, "spec 的 hiddenimports 里没有 socksio"


# ======================================================= 识别 SOCKS

@pytest.mark.parametrize("url,expect", [
    ("socks5://127.0.0.1:1080", True),
    ("SOCKS5://127.0.0.1:1080", True),
    ("socks5h://h:1080", True),
    ("socks4://127.0.0.1:1080", True),
    ("http://127.0.0.1:8080", False),
    ("https://127.0.0.1:8443", False),
    ("", False),
    (None, False),
])
def test_is_socks_proxy(url, expect):
    """只有 SOCKS 协议才需要 socksio —— 别把 HTTP 代理也降级掉。"""
    assert F.is_socks_proxy(url) is expect


# ======================================================= D-2 缺 socksio 时降级

def test_missing_socksio_degrades_instead_of_crashing(monkeypatch):
    """D-2 核心：缺 socksio 时**构造 Fetcher 不抛异常**，而是降级 + 中文提示。

    红→绿：改码前 `Fetcher(cfg, proxy="socks5://…")` 之后首次请求直接抛
    ImportError，任务崩停。
    """
    monkeypatch.setattr(F, "socks_available", lambda: False)
    f = F.Fetcher(AppConfig(), proxy=SOCKS5)
    assert f.degraded is True
    assert f.socks_missing is True
    # 用户填的原值要留着（UI/日志要显示），真正下发给 httpx 的是 None
    assert f.proxy == SOCKS5
    assert f.effective_proxy is None
    # ⚠ 提示必须**中文**且包含作业单指定的关键信息
    assert f.notice, "没有中文提示，用户只看到崩停"
    assert "socksio" in f.notice.lower()
    assert "代理" in f.notice
    # 断言是中文，不是英文原样异常
    assert not f.notice.strip().startswith(("Traceback", "ImportError"))


def test_http_proxy_is_not_degraded(monkeypatch):
    """HTTP 代理**不许**被误降级（那会让用户以为走了代理其实直连）。"""
    monkeypatch.setattr(F, "socks_available", lambda: False)
    f = F.Fetcher(AppConfig(), proxy=HTTP)
    assert f.degraded is False
    assert f.effective_proxy == HTTP
    assert f.notice == ""


def test_no_proxy_at_all_is_not_degraded(monkeypatch):
    monkeypatch.setattr(F, "socks_available", lambda: False)
    f = F.Fetcher(AppConfig(), proxy=None)
    assert f.degraded is False
    assert f.effective_proxy is None


def test_client_builds_directly_when_degraded(monkeypatch):
    """D-2：降级后**能真的建出客户端**（不抛），任务流程继续。

    这是「不闪退、不卡死、不吞掉进度」的可测形态。
    """
    monkeypatch.setattr(F, "socks_available", lambda: False)
    f = F.Fetcher(AppConfig(), proxy=SOCKS5)

    async def go():
        c = await f._client_get()
        return c

    c = asyncio.run(go())
    assert c is not None
    assert f.degraded is True
    asyncio.run(f.close())


def test_socks_failure_at_request_time_is_caught(monkeypatch):
    """D-2 后半：**首次请求处**炸 socks 也要被接住（构造通过、握手才发现）。

    红→绿：只修构造不够 —— 真实场景常常是握手阶段才抛。
    这里用假客户端模拟「请求时抛 socks 异常」，断言：
    ① 异常不外抛；② 中文提示被记下；③ 降级后用直连重试。
    """
    monkeypatch.setattr(F, "socks_available", lambda: True)   # 构造期不降级
    f = F.Fetcher(AppConfig(), proxy=SOCKS5)
    assert f.effective_proxy == SOCKS5        # 构造期仍带代理

    boom = [0]

    class _Resp:
        status_code = 200
        text = "ok"
        headers: dict = {}

        def raise_for_status(self):
            return None

    class FakeClient:
        """第一次请求抛 socks ImportError；之后（直连）正常返回。"""

        async def get(self, url, headers=None):
            boom[0] += 1
            if boom[0] <= 2:      # 两个 host 变体都先炸一次
                raise ImportError("socksio is not installed")
            return _Resp()

        async def aclose(self):
            return None

    import httpx

    async def fake_client_get(self, url=None):
        # 2026-10-28（编号 33）：真实签名变成 `_client_get(url=None)` ——
        # 要按域名选"校不校验证书"的客户端。替身必须跟着改，否则
        # fetch_text 传参时 TypeError，看上去像产品坏了（其实是替身过时）。
        if self._client is None:
            self._client = FakeClient()
        return self._client

    monkeypatch.setattr(F.Fetcher, "_client_get", fake_client_get)

    async def go():
        return await f.fetch_text("https://example.com/a.txt")

    # 关键：整个 fetch 不抛异常
    result = asyncio.run(go())
    assert result == "ok"
    assert f.degraded is True
    assert "socksio" in f.notice.lower()
    assert f.effective_proxy is None


def test_real_socks_url_builds_client_when_available():
    """D-1：有 socksio 时 SOCKS 代理**照常**下发给 httpx（不降级）。

    红→绿：把 socks 一律降级也算「通过」，但那是把功能做坏了。
    """
    f = F.Fetcher(AppConfig(), proxy=SOCKS5)
    if F.socks_available():
        assert f.effective_proxy == SOCKS5
        assert f.degraded is False
    # 无论如何都不能抛
    assert f.proxy == SOCKS5


def test_client_build_never_raises_for_socks(monkeypatch):
    """D-2：`AsyncClient` 构造抛异常时也不外抛（就地降级重试）。

    模拟「socksio 在 import 时才失败」的形态：构造 httpx.AsyncClient 抛。
    """
    import httpx

    monkeypatch.setattr(F, "socks_available", lambda: False)
    f = F.Fetcher(AppConfig(), proxy=SOCKS5)
    real = httpx.AsyncClient
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if k.get("proxy") is not None:
            raise ImportError("socksio missing")
        return real(*a, **k)

    monkeypatch.setattr(F.httpx, "AsyncClient", flaky)

    async def go():
        return await f._client_get()

    c = asyncio.run(go())
    assert c is not None
    assert calls["n"] == 1        # 构造期已降级，压根没带代理去试
    asyncio.run(f.close())


def test_non_proxy_client_error_still_raises(monkeypatch):
    """对照：非代理类构造错误**照旧上抛**（别把真 bug 一起吞了）。"""
    import httpx

    monkeypatch.setattr(F, "socks_available", lambda: True)
    f = F.Fetcher(AppConfig(), proxy=HTTP)

    def boom(*a, **k):
        raise ValueError("真 bug，不是代理问题")

    monkeypatch.setattr(F.httpx, "AsyncClient", boom)
    with pytest.raises(ValueError):
        asyncio.run(f._client_get())


# ======================================================= D-3 proxy.py 兜底

def _pool(tmp_path):
    """ProxyPool 收的是项目的 `Database` 封装（有 .query()/.execute()）。"""
    from media_resource_manager.db import Database
    from media_resource_manager.proxy import ProxyPool

    return ProxyPool(Database(tmp_path / "p.db"))


def _entry(url=HTTP):
    """按真实结构造 ProxyEntry（scheme/host/port 分开，不是 url=）。"""
    from urllib.parse import urlsplit

    from media_resource_manager.proxy import ProxyEntry

    u = urlsplit(url)
    return ProxyEntry(name="t", scheme=u.scheme or "http",
                      host=u.hostname or "127.0.0.1", port=u.port or 8080)


def test_proxy_check_translates_error_to_chinese(tmp_path, monkeypatch):
    """D-3：`ProxyPool.check` 失败文案必须是**中文**，且不外抛。"""
    import httpx

    from media_resource_manager.proxy import ProxyPool

    st = _pool(tmp_path)

    def boom(*a, **k):
        raise ImportError("socksio is not installed")

    monkeypatch.setattr(httpx, "Client", boom)
    e = _entry(SOCKS5)
    ok, lat, err = st.check(e, test_url="https://example.com", timeout=1)
    assert ok is False
    assert err, "必须有错误文案"
    assert "socksio" in err.lower()
    assert "SOCKS" in err or "代理" in err


def test_proxy_acheck_translates_error_to_chinese(tmp_path, monkeypatch):
    """D-3 异步版同理。"""
    import httpx

    from media_resource_manager.proxy import ProxyPool

    st = _pool(tmp_path)

    class BadAC:
        def __init__(self, *a, **k):
            raise ImportError("socksio is not installed")

    monkeypatch.setattr(httpx, "AsyncClient", BadAC)
    e = _entry(SOCKS5)
    ok, lat, err = asyncio.run(
        st.acheck(e, test_url="https://example.com", timeout=1))
    assert ok is False
    assert "socksio" in err.lower() or "代理" in err


@pytest.mark.parametrize("exc,key", [
    (TimeoutError("timed out"), "超时"),
    (ConnectionRefusedError("connection refused"), "无法连接"),
    (OSError("certificate verify failed"), "证书"),
])
def test_proxy_check_error_translations(exc, key):
    """D-3：常见网络错误各有中文说法（不是把英文原样透给用户）。"""
    from media_resource_manager.proxy import ProxyPool

    msg = ProxyPool._zh_error(exc)
    assert key in msg, f"{type(exc).__name__} → {msg!r}，缺 {key!r}"


def test_proxy_check_records_error_not_raises(tmp_path):
    """D-3：失败只记 last_error，**不改变代理条目结构**、不抛。"""
    from media_resource_manager.proxy import ProxyPool

    st = _pool(tmp_path)
    e = _entry("http://127.0.0.1:1")          # 必然连不通
    ok, lat, err = st.check(e, test_url="https://example.com", timeout=1)
    assert ok is False
    assert err
    # check 不外抛，且**就地**把失败记在条目对象上（last_error 中文）
    # ⚠ 记忆教训：`_record` 走的是 `save()` = UPDATE，id 为 None 时写不进去，
    #   所以别断言 `st.get(e.id)` 能读回来 —— 那是测试自己的假设错，不是 bug。
    assert e.failures >= 1
    assert e.last_error == err
    assert "代理" in e.last_error or "socksio" in e.last_error.lower()


def test_describe_socks_problem_is_chinese():
    """作业单指定的中文文案必须含关键信息。"""
    msg = F.describe_socks_problem(SOCKS5)
    assert "socksio" in msg.lower()
    assert "降级" in msg
    assert "代理" in msg
    # 不许出现英文报错原文
    assert "Traceback" not in msg