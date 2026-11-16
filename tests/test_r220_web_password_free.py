# -*- coding: utf-8 -*-
"""R220 护栏：Web 空密码 + 绑本机 = **无条件免密**（不再吃首启动时序误判）。

用户报告：`web_password=""`、`web_bind="127.0.0.1"`，软件启动自动拉起 Web
后浏览器弹密码页；「重新开关一下 web 功能就直接进」。

根因（订单 R220 源码取证）：`_auth_ok()` 原顺序先查 `_client_allowed_by_ip`，
首启动时序里 `client_ip` 可能取到 ""，`_ip_is_private("")=False` ⇒ 空密码也被
判「要密码」⇒ `/api/session` 被送登录壳 HTML（非 JSON）⇒ 前端 r.json() 解析
失败弹密码页。手动「关→开 web」后状态就绪才免密。

修法（按订单）：空密码分支**提前**；绑本机（127.0.0.1/::1/localhost）无条件
放行 —— bind=本机时物理上只有本机能连上，client_ip 无关紧要；非本机绑定仍走
R58 公网护栏（不设密码不对公网敞开）。

护栏三层（R63-D* 口径）：
- L1 行为矩阵：绑本机 × client_ip 取空/公网（物理不可能组合，钉的是代码
  捷径）；0.0.0.0 绑定 × 空 ip/私网/公网；设密码路径完全不变。
- L2 源码判据：`_auth_ok` 函数块内含 web_bind 读取 + 回环白名单元组
  （R84 按函数边界切，防同文欺骗）。
- L3 真服务：空密码 + 绑 127.0.0.1 起真 WebServer（端口 0），HTTP GET
  /api/session 必须 200 JSON ok:true password_required:false（用户可感判据）。

反向可变：删绑本机捷径 → L1 第 1/2 组 + L3 必红；
          改成任何绑定都放行 → 0.0.0.0 + 公网那条必红；
          把空密码分支挪回 _client_allowed_by_ip 之后 → client_ip="" 用例必红。

⚠ 与 test_r157_guards 的关系（R73 纪律，改护栏要有台账）：
  R157 的「公网 + 空密码仍拦」行为测试原用 bind="127.0.0.1"（其 _cfg 默认值）
  —— R220 重排后该组合走「绑本机无条件放行」，物理上绑本机根本看不到公网
  客户端，那条测试的组合不存在于真实 socket，已随本单把 bind 改为 "0.0.0.0"
  （安全语义不变，见该文件内注记）。
"""
import json
import re
import types
from pathlib import Path

import pytest

from media_resource_manager import webui

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "media_resource_manager" / "webui.py"


# ---------------------------------------------------------------- L1 harness

class _H(webui._Handler):
    """只测鉴权分支，不走真 socket（与 R157/R48 同思路）。"""

    _cfg = None
    _db = None
    _headers = None
    _path = "/api/x"

    def __init__(self, cfg, headers=None, client_ip="127.0.0.1"):
        self._cfg = cfg
        self._db = None
        self._headers = headers or {}
        self._path = "/api/x"
        self.client_address = (client_ip, 0)

    def _send(self, code, body, ctype, extra=None):
        return (code, body, ctype, extra or {})

    def _json(self, obj, code=200, extra=None):
        return self._send(code, json.dumps(obj, ensure_ascii=False)
                          .encode("utf-8"), "application/json", extra)

    def _body(self):
        return {}

    @property
    def cfg(self):
        return self._cfg

    @property
    def headers(self):
        return self._headers

    @property
    def path(self):
        return self._path


def _cfg(password="", lan_open=False, bind="127.0.0.1"):
    return types.SimpleNamespace(
        web_password=password, web_lan_open=lan_open, web_ip_whitelist=[],
        web_bind=bind, web_port=0, web_enabled=True)


# ------------------------------------------------- L1 行为矩阵：绑本机无条件放行

@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost"])
@pytest.mark.parametrize("ip", ["", "127.0.0.1", "203.0.113.9"])
def test_loopback_bind_empty_password_always_ok(bind, ip):
    """★ 主判据：绑本机 + 空密码 → 无条件放行。

    client_ip="" 正是首启动时序 bug 的形态；"203.0.113.9" 是真实 socket
    里绑本机时不可能出现的组合 —— 这里钉的是**代码捷径本身**（不看 ip）。
    """
    h = _H(_cfg(password="", bind=bind), client_ip=ip)
    assert h._auth_ok() is True, (
        f"bind={bind} client_ip={ip!r} 空密码竟被拦 —— R220 绑本机免密没生效，"
        "「重新开关 web 才能进」复发")


# ------------------------------------------------- L1 非本机绑定仍守 R58

def test_open_bind_unknown_ip_still_blocked():
    """0.0.0.0 绑定 + client_ip 取空 → 仍拦（首启动时序下不扩大暴露面）。"""
    h = _H(_cfg(password="", bind="0.0.0.0"), client_ip="")
    assert h._auth_ok() is False, (
        "开放绑定 + 未知来源 ip 竟放行 —— 空密码图库对不可知来源敞开")


def test_open_bind_lan_still_ok_public_still_blocked():
    """0.0.0.0 绑定 + 空密码：私网客户端放行、公网客户端仍拦（R58 不放开）。"""
    ok = _H(_cfg(password="", bind="0.0.0.0"), client_ip="192.168.1.100")
    assert ok._auth_ok() is True, "私网客户端被误拦 —— R157 免密语义倒退"
    bad = _H(_cfg(password="", bind="0.0.0.0"), client_ip="203.0.113.9")
    assert bad._client_allowed_by_ip() is False
    assert bad._auth_ok() is False, "公网 + 空密码竟放行 —— R58 护栏被拆"


def test_lan_open_true_open_bind_private_ok():
    """web_lan_open=True（非本机绑定 + 空密码）私网仍免密：私网客户端走
    `_ip_is_private` 放行，与 R157 行为等价（订单「等价于此前行为」条款）。"""
    h = _H(_cfg(password="", lan_open=True, bind="0.0.0.0"),
           client_ip="10.0.0.5")
    assert h._auth_ok() is True


# ------------------------------------------------- L1 设了密码路径不变

def test_password_mode_untouched_by_r220():
    """设了密码 + 绑本机：无凭据仍拦、Basic 对了才放行（R220 不解锁密码模式）。"""
    import base64
    cfg = _cfg(password="secret", bind="127.0.0.1")
    assert _H(cfg, client_ip="127.0.0.1")._auth_ok() is False, (
        "绑本机 + 设密码竟免密 —— R220 越权放开了密码模式")
    tok = base64.b64encode(
        f"{webui.DEFAULT_USER}:secret".encode()).decode()
    h = _H(cfg, headers={"Authorization": f"Basic {tok}"},
           client_ip="127.0.0.1")
    assert h._auth_ok() is True


def test_password_mode_lan_open_still_wins():
    """设密码 + lan_open=True + 局域网 → 仍免密（R52 契约，走
    `_client_allowed_by_ip`，该检查在设密码路径保持第一位）。"""
    h = _H(_cfg(password="secret", lan_open=True), client_ip="192.168.1.100")
    assert h._auth_ok() is True


# ---------------------------------------------------------------- L2 源码判据

def _code_only(src: str) -> str:
    """剥 `#` 注释（防注释骗判据，R157 同款）。"""
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


def _func_block(src: str, marker: str) -> str:
    """按函数边界切块（R84：防块外同文欺骗）。"""
    i = src.index(marker)
    m = re.search(r"\n    def ", src[i + 1:])
    j = i + 1 + m.start() if m else len(src)
    return src[i:j]


def test_source_auth_ok_has_loopback_shortcut():
    """★ 源码判据：`_auth_ok` 块内必须先取 web_bind、再对回环绑定直接放行。

    顺序敏感：bind 检查必须在 `if not want:` 分支**内部**、在
    `_ip_is_private(client_ip)` 之前（R220 的全部意义就在这个顺序）。
    """
    with open(WEBUI, "r", newline="", encoding="utf-8") as f:
        src = f.read()
    blk = _code_only(_func_block(src, "def _auth_ok"))
    assert 'getattr(self.cfg, "web_bind"' in blk, \
        "_auth_ok 里没有读 web_bind —— 绑本机捷径被删（R220 回退）"
    i_bind = blk.index("web_bind")
    i_tuple = blk.find('("127.0.0.1", "::1", "localhost")')
    assert i_tuple > 0, "回环绑定白名单元组消失 —— R220 回退"
    i_ret = blk.index("return self._ip_is_private(self._client_ip())")
    assert i_bind < i_tuple < i_ret, (
        "bind 捷径没在「空密码 → 公网护栏」之前 —— 顺序错，时序 bug 依旧")
    # 空密码分支必须在 `_client_allowed_by_ip` 之前（R220 重排的核心）
    i_want = blk.index("if not want:")
    i_allowed = blk.index("if self._client_allowed_by_ip():")
    assert i_want < i_allowed, (
        "空密码分支又排到 _client_allowed_by_ip 后面了 —— 首启动时序 bug 复发")


def test_source_session_password_required_expression_kept():
    """/api/session 的 password_required 口径不许被顺手改掉：
    want 为空时恒 False（与前端免密直进一致），且保留「对本客户端」语义。"""
    src = _code_only(WEBUI.read_text(encoding="utf-8"))
    assert "not self._client_allowed_by_ip()" in src, \
        "/api/session 的 password_required 表达式被改 —— 前端免密直进判据失配"


# ---------------------------------------------------------------- L3 真服务

class _FakeDb:
    def query(self, sql):
        return [{"c": 0}]

    def audit(self, *a, **k):
        pass


@pytest.fixture
def srv_free(tmp_path, monkeypatch):
    """空密码 + 绑 127.0.0.1 的真 WebServer（端口 0），测完 stop。"""
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    cfg = _cfg(password="", bind="127.0.0.1")
    cfg.db_path = tmp_path / "home" / "mrm.db"
    s = webui.WebServer(cfg, _FakeDb())
    ok, msg = s.start()
    assert ok, f"Web 服务起不来：{msg}"
    port = s._httpd.server_address[1]
    yield s, cfg, port
    s.stop()


def _req(port, method, path):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_real_http_session_free_password_cold_start(srv_free):
    """★ 用户可感判据：冷启动（空密码 + 绑本机）GET /api/session 必须
    200 + JSON ok:true + password_required:false —— 弹密码页 = 本判据红。"""
    _, cfg, port = srv_free
    st, body = _req(port, "GET", "/api/session")
    assert st == 200, f"/api/session 回 HTTP {st} —— 免密被判要密码（弹密码页）"
    d = json.loads(body.decode("utf-8"))
    assert d.get("ok") is True
    assert d.get("password_required") is False, (
        f"password_required={d.get('password_required')} —— 前端会弹密码页")
