"""R68：Web 登录鉴权回归（**含非 ASCII 密码**）。

## 本文件钉住什么

R68 真跑（`tools/r68_probe_web_pages.py`，110 项）时挖到一个**永久锁死级**缺陷：
`webui._do_login` 里`hmac.compare_digest(pw, want)` 两边都是 `str`，
而 CPython 的 `compare_digest` **对非 ASCII 的 str 直接抛 TypeError**
（只支持 ASCII str 或 bytes）。

后果：
- 密码含中文/emoji ⇒ 用户敲的密码**完全正确**也抛 ⇒ 登录处理器崩 ⇒
  连接被掐断（客户端是 `RemoteDisconnected`，**连 401 都收不到**）⇒
  拿不到 cookie ⇒ **Web 端被永久锁在门外**，只能回桌面端改密码。
  而桌面端那个密码框**只校验长度 ≥4、没有任何 ASCII 限制**
  （`gui/main_window.py` 的 Web 设置对话框）⇒ 中文密码是完全可达的正常输入。
- 反向：密码是 ASCII，用户输入法没切回英文打了中文 ⇒ 同样掐断连接，
  而不是回一句「密码不对」⇒ 用户只会以为软件坏了。

为什么既有护栏全绿：`verify_session` 比的是 hexdigest（纯 ASCII）、
`check_auth` 比的是 base64（纯 ASCII）、`crypt.py` 比的是 **bytes**
—— 三处都安全，**只有 `_do_login` 直接拿用户输入的 str 比**。
测了「登录成功」「密码错被拒」，**没测非 ASCII 密码** ⇒ 又一例护栏盲区。

## 分层

1. **纯函数层**：`_do_login` 用的比对方式必须对任意 unicode 安全；
   `compare_digest(str, str)` 这个写法不许再出现（AST 判，不全文搜）。
2. **行为层**：真起 WebServer + 真 HTTP，用**中文密码**真登录一次，
   断言拿得到 200 + `ok:True`（不是连接被掐断）。
3. **不回归**：ASCII 密码的原有行为不变。
4. **不污染**：本文件不许泄漏 monkeypatch / 环境变量给别的测试
   （R67 教训：我自己的测试曾让全量多出 3 条红）。

⚠ 本文件**只起WebServer，不 import Qt 窗口**；但仍按MEMORY 铁律 1，
   跑全量时不要并行任何 GUI 负载。
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
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
# ⚠ 用**函数级** fixture 且 finally 还原，不写模块级全局 ——
#   R67 教训：monkeypatch 模块属性与设环境变量一样是**进程级全局**。
_TMP = Path(tempfile.mkdtemp(prefix="r68test_"))


@pytest.fixture()
def web_home():
    """给每个用例一个干净的 MRM_HOME，退出时还原。"""
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


# ==================== 1 · 纯函数层 ====================

def test_compare_digest_str_is_unsafe_for_non_ascii():
    """**判据本身**要先被验证：compare_digest(str,str) 遇到非 ASCII 会抛。

    ⚠ 这条是元测试：它锁住「为什么必须 encode」这个前提。
    若哪天 Python 改了行为（变得安全），本条会红 —— 那时侯可以简化实现，
    但**不能默默继续encode 而不看这条**。
    """
    import hmac
    with pytest.raises(TypeError):
        hmac.compare_digest("我的密码", "pw123")
    # 正解确实不抛
    assert hmac.compare_digest("我的密码".encode("utf-8"),
                                "我的密码".encode("utf-8")) is True
    assert hmac.compare_digest("密码".encode("utf-8"),
                                "pw123".encode("utf-8")) is False


def test_do_login_compares_encoded_bytes_only():
    """AST 判：`_do_login` 里那个 compare_digest 的两个实参都必须是 .encode()。

    ⚠ MEMORY 铁律：判「某处不许出现 X」**绝不能全文搜字面量** —— 本仓注释
    大量引用「原来错在哪」，全文搜必然匹配到自己 ⇒ 假红。正解走 AST。
    ⚠ 只盯 `_do_login` 这一个函数：同文件另两处（check_auth 比 base64、
    verify_session 比 hexdigest）**本来就安全**，逼它们 encode 是无意义改动面。
    """
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_do_login"), None)
    assert fn is not None, "找不到 _do_login"
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "compare_digest"]
    assert len(calls) == 1, f"_do_login 里 compare_digest 应恰好 1 处，实为 {len(calls)}"
    for a in calls[0].args:
        assert isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute) \
            and a.func.attr == "encode", \
            f"第 {calls[0].lineno} 行 compare_digest 实参不是 .encode() —— " \
            "非 ASCII 密码会抛 TypeError 把用户锁在门外"


def test_other_auth_paths_are_left_as_they_are():
    """反向护栏：确认我**没有**顺手改那两处本来安全的比对。

    （判据同样走 AST。不变量=「仍用未 encode 的实参」，因为它们比的是
    base64/hexdigest 这种纯 ASCII 派生值，改了反而是无意义改动面。）
    """
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for fname in ("check_auth", "verify_session"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == fname), None)
        assert fn is not None, f"找不到 {fname}"
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "compare_digest"]
        assert calls, f"{fname} 里没有 compare_digest 了，结构变了需人工确认"


# ==================== 2 · 行为层（真起服务 + 真 HTTP） ====================

class _Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def post(self, path: str, body: dict):
        data = json.dumps(body).encode("utf-8")
        r = urllib.request.Request(
            self.base + path, data=data, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(r, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path: str):
        r = urllib.request.Request(self.base + path,
                                   headers={"Accept": "application/json"})
        try:
            with self.opener.open(r, timeout=15) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()


@pytest.fixture()
def auth_server(web_home):
    """起一台 `web_lan_open=False` 的服务器 —— **逼真走密码校验**。

    ⚠⚠ R68 仪器错 7（本轮最重要的一个）：默认 `web_lan_open=True` 时，
      客户端是 127.0.0.1 ⇒ `_client_allowed_by_ip()` 恒True ⇒
      `_auth_ok()` **一路直接放行**，压根不走密码校验。
      ⇒ 在默认配置下，「登录成功」是**恒真**的，证明不了任何鉴权逻辑。
      ⇒ 必须显式关掉免密 + 清空白名单。
    """
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    cfg = AppConfig()
    cfg.db_path = str(web_home / "auth.db")
    cfg.web_password = "pw123"
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_lan_open = False          # ★逼它要密码
    cfg.web_ip_whitelist = []         # ★白名单也清空

    # ⚠ 不许写用户真实 config.json。这个 monkeypatch 是**进程级全局**，
    #   R67 教训：它曾泄漏到别的测试文件，害得全量多出 3 条红
    #   （真凶不是 env，是这个模块属性）。⇒ **必须 finally 还原**。
    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    db = Database(_TMP / "auth.db")
    srv = webui.WebServer(cfg, db, queue=object())
    ok, msg = srv.start()
    assert ok, f"服务器起不来：{msg}"
    try:
        yield cfg, _Client(f"http://127.0.0.1:{cfg.web_port}")
    finally:
        srv.stop()
        db.close()
        cfgmod.save_config = orig_save


def test_chinese_password_can_log_in(auth_server):
    """★核心回归：中文密码 + 正确输入 ⇒ 200 且 ok=True。

    修复前：连接被掐断（RemoteDisconnected），连401 都收不到。
    ⚠ 判据必须能区分「200 且 ok」与「连接被掐断」——
      urllib 在掐断时**抛异常**而不是返回状态码，所以只要能走到断言就说明没掐断。
    """
    cfg, cli = auth_server
    cfg.web_password = "我的密码"
    st, raw = cli.post("/api/login", {"password": "我的密码"})
    assert st == 200, f"中文密码登录失败：st={st} raw={raw[:120]!r}"
    assert json.loads(raw.decode("utf-8")).get("ok") is True


def test_wrong_chinese_password_returns_401_not_disconnect(auth_server):
    """中文密码 + 错误输入 ⇒ 401「密码不对」，而不是掐断连接。"""
    cfg, cli = auth_server
    cfg.web_password = "我的密码"
    st, raw = cli.post("/api/login", {"password": "错的密码"})
    assert st == 401, f"应回 401，实际 st={st} raw={raw[:120]!r}"
    assert "密码不对" in raw.decode("utf-8")


def test_ascii_password_with_non_ascii_input_returns_401(auth_server):
    """反向：密码是 ASCII，用户输入法没切回英文打了中文 ⇒ 也应是 401。"""
    cfg, cli = auth_server
    cfg.web_password = "pw123"
    st, raw = cli.post("/api/login", {"password": "密码"})
    assert st == 401, f"应回 401，实际 st={st} raw={raw[:120]!r}"
    assert "密码不对" in raw.decode("utf-8")


def test_emoji_password_can_log_in(auth_server):
    """emoji 密码（输入法很容易打出来）也能登录。"""
    cfg, cli = auth_server
    cfg.web_password = "pw😀123"
    st, raw = cli.post("/api/login", {"password": "pw😀123"})
    assert st == 200, f"emoji 密码登录失败：st={st} raw={raw[:120]!r}"


def test_ascii_password_still_works(auth_server):
    """不回归：ASCII 密码的原有行为完全不变。"""
    _cfg, cli = auth_server
    st, raw = cli.post("/api/login", {"password": "pw123"})
    assert st == 200, f"ASCII 密码登录失败：st={st}"
    body = json.loads(raw.decode("utf-8"))
    assert body.get("ok") is True and body.get("user") == "admin"
    # 登录后数据面放行
    st2, _ = cli.get("/api/tasks")
    assert st2 == 200, f"登录后取数据应放行，实际 {st2}"


def test_unauthenticated_is_blocked(auth_server):
    """未登录：数据面 401、页面回登录壳（不弹原生Basic Auth 框）。"""
    _cfg, cli = auth_server
    st, raw = cli.get("/api/tasks")
    assert st == 401
    assert json.loads(raw.decode("utf-8")).get("error") == "未登录"
    st2, raw2 = cli.get("/")
    assert st2 == 200, "未登录访问页面应回 200 登录壳，否则手机会弹原生认证框"
    assert b"<" in raw2[:40]


def test_logout_reblocks(auth_server):
    """登出后数据面重新被拦。"""
    _cfg, cli = auth_server
    assert cli.post("/api/login", {"password": "pw123"})[0] == 200
    assert cli.get("/api/tasks")[0] == 200
    cli.post("/api/logout", {})
    assert cli.get("/api/tasks")[0] == 401, "登出后应重新被拦"


# ==================== 3 · 不污染别的测试 ====================

def test_this_file_does_not_leak_monkeypatch(web_home):
    """R67 教训防复发：跑完本文件后 cfgmod.save_config 必须已还原。

    R67 那次全量多出 3 条红，真凶就是 `cfgmod.save_config` 这个monkeypatch
    没还原（当时猜是 MRM_HOME 泄漏，**被自己的实验证伪**了）。
    """
    import media_resource_manager.config as cfgmod
    assert getattr(cfgmod.save_config, "__name__", "") != "<lambda>", \
        "save_config 还是被替换成了 lambda —— 会污染别的测试"
    assert not callable(getattr(cfgmod.save_config, "__wrapped__", None))


def test_probe_tool_covers_the_nonascii_case():
    """护栏不许是空壳：断言 tools/ 里的真跑探针**确实**含非 ASCII 密码用例。

    （R63/R67 教训：静态护栏第一版常是空壳，变异掉它仍全绿。）
    这里判「探针源码里真的有中文密码字面量」——用 AST 扫字符串常量，
    **不全文搜**（工具文件里也有大段注释引用这个 bug）。
    """
    probe = ROOT / "tools" / "r68_probe_web_pages.py"
    src = probe.read_text(encoding="utf-8")
    tree = ast.parse(src)
    strs = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert any("我的密码" in s for s in strs), "探针里没有中文密码用例"
    assert any("😀" in s for s in strs), "探针里没有 emoji 密码用例"