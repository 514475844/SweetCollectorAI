"""R157 护栏：Web 空密码 = 免密模式（不生成、不保存、不锁死）。

用户报告原话：
  开启 Web 功能、没设密码 → 关软件 → 重开 → 再进网页就要密码了，
  但关软件前根本没设过密码，不知道是多少。

根因（源码取证）：
  1. `WebServer.start()` 在 `web_password` 为空且 `web_lan_open=False` 时
     `secrets.token_hex(16)` 生成随机密码 → `save_config` 持久化，
     用户从不知情 ⇒ 永久锁在门外（用户没设过，也不知道去哪看）；
  2. `_auth_ok()` 写 `if not want: return False`（空密码反而拦），
     与 `/api/session` 回给前端的 `password_required=False`、
     以及前端 app.js「web_password 为空即免密」**自相矛盾**。

本护栏钉两层：
  · 行为层：空密码启动后 cfg 里**不许**多出密码；空密码 + 本机/局域网放行；
    空密码 + 公网仍拦（R58/R141 安全侧不许放松，本单一并钉住）；
  · 源码层：`start()` 里不许再有生成密码的写入（去注释判，防注释骗过）。

反向可变：把自动生成密码那段加回来 → 第 1/2 条必红；
          把 `_auth_ok` 的空密码分支改回 `return False` → 第 3 条必红。
"""
from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from media_resource_manager import webui

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "media_resource_manager" / "webui.py"


def _code_only(src: str) -> str:
    """剥掉 `#` 注释行 —— R157 特意在注释里留了历史说明（token_hex），
    光搜字符串会让判据假绿（R63-D3 同型坑）。"""
    return "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())


class _H(webui._Handler):
    """只测鉴权分支，不走真 socket（与 R48/R141 同思路）。"""

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
        import json as _j
        return self._send(code, _j.dumps(obj, ensure_ascii=False)
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


def _cfg(password="", lan_open=True, bind="127.0.0.1"):
    return types.SimpleNamespace(
        web_password=password, web_lan_open=lan_open, web_ip_whitelist=[],
        web_bind=bind, web_port=0, web_enabled=True)


# ------------------------------------------------- 行为层：启动不生成密码

def test_start_with_empty_password_generates_nothing(tmp_path, monkeypatch):
    """★ 主判据：空密码启动后 cfg.web_password 仍是空（不许偷偷生成+持久化）。"""
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    cfg = _cfg(password="", lan_open=False)     # lan_open=False 是旧代码生成密码的那一档
    cfg.db_path = tmp_path / "home" / "mrm.db"
    srv = webui.WebServer(cfg, None)
    # 不真起服务：只要能跑到「不生成密码」这一步即可（起服务要前端资产）
    ok, msg = srv.start()
    try:
        assert not cfg.web_password, (
            f"空密码启动竟写入了一个密码（{cfg.web_password!r}）"
            " —— 这正是用户被锁死的直接成因（R157 复发）")
        if ok:
            assert "免密" in msg, f"启动提示应点明免密模式，实得 {msg!r}"
    finally:
        try:
            srv.stop()
        except Exception:
            pass


def test_start_message_never_claims_auto_generated_password(tmp_path, monkeypatch):
    """提示文案不许再出现「已自动生成 Web 密码」这类话术。"""
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    cfg = _cfg(password="", lan_open=True)
    cfg.db_path = tmp_path / "home" / "mrm.db"
    srv = webui.WebServer(cfg, None)
    ok, msg = srv.start()
    try:
        assert "自动生成" not in msg, f"还在声称自动生成密码：{msg!r}"
    finally:
        try:
            srv.stop()
        except Exception:
            pass


# ------------------------------------------------- 行为层：空密码免密直进

@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "192.168.1.100", "10.0.0.5"])
def test_empty_password_passes_from_local_and_lan(ip):
    """★ 空密码 + 本机/局域网 → 放行（不再「空密码反而拦」）。"""
    h = _H(_cfg(password="", lan_open=False), client_ip=ip)
    assert h._auth_ok() is True, f"{ip} 空密码被拦 —— R157 免密没生效"


def test_empty_password_still_blocked_from_public():
    """★ 反向（安全侧不放松）：空密码 + 公网 → 仍必须拦。

    R58/R141 钉死的边界，R157 只做「空密码=免密」的语义统一，
    **不**把图库对全网敞开。
    ⚠ R220 联动（台账）：原用例用 _cfg 默认 bind="127.0.0.1" —— R220 把
    「空密码 + 绑本机」定义为无条件放行（绑本机物理上看不到公网客户端，
    该组合不存在于真实 socket），故公网判据改用开放绑定 "0.0.0.0"。
    R58 语义原样保留，行为矩阵全集见 test_r220_web_password_free.py。
    """
    h = _H(_cfg(password="", lan_open=False, bind="0.0.0.0"),
           client_ip="203.0.113.9")
    assert h._client_allowed_by_ip() is False
    assert h._auth_ok() is False, "公网 + 空密码竟放行 —— 图库对全网敞开"


def test_lan_open_still_wins_when_on():
    """web_lan_open=True 时局域网仍恒免密（R52 契约不变）。"""
    h = _H(_cfg(password="", lan_open=True), client_ip="192.168.1.100")
    assert h._auth_ok() is True


def test_password_still_required_when_set():
    """存量兼容：设了密码的人行为不变 —— 无凭据不放行，对了才放行。"""
    import base64
    cfg = _cfg(password="secret", lan_open=False)
    assert _H(cfg, client_ip="127.0.0.1")._auth_ok() is False
    tok = base64.b64encode(
        f"{webui.DEFAULT_USER}:secret".encode()).decode()
    assert _H(cfg, headers={"Authorization": f"Basic {tok}"},
              client_ip="127.0.0.1")._auth_ok() is True


# ------------------------------------------------- 源码层

def test_source_has_no_password_generation_call():
    """源码判据（去注释）：不许再有自动生成密码的调用。"""
    code = _code_only(WEBUI.read_text(encoding="utf-8"))
    assert not re.search(r"secrets\.token_hex\s*\(", code), \
        "源码里仍有自动生成密码的调用 —— R157 后应为免密模式"


def test_session_api_reports_no_password_required_for_empty():
    """空密码时 /api/session 报 password_required=False（前端才不弹密码页）。"""
    src = WEBUI.read_text(encoding="utf-8")
    assert "password_required" in src
    assert "not self._client_allowed_by_ip()" in src, \
        "/api/session 仍按「有没有密码」判断，没考虑本客户端是否免密"
