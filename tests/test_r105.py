"""R105：Web 只认密码（不要用户名）。

背景：R86 / 用户明确「Web 登录只密码」。此前「启用 Web 功能」对话框还留着
「用户名」输入行，浏览器端登录页/鉴权也仍读 cfg.web_user。

本单修复：
  · 桌面「启用 Web 功能」对话框去掉用户名输入行与成功弹窗里的「账号」行；
  · 浏览器登录页只提交/校验密码（前端本来就只发 password，这里再钉一遍）；
  · 鉴权只比对密码，内部不再区分用户（统一用常量 DEFAULT_USER，不读
    cfg.web_user）；
  · 老配置若带 web_user 字段，加载不报错、不迁移。

决策（见回报）：**保留 web_user 字段（废弃/无操作）**，所有鉴权改成只认密码、
UI 去掉它。理由见文件底部注释。
"""
import base64
import json
import types

import pytest

pytest.importorskip("PySide6")

from media_resource_manager import webui
from media_resource_manager.gui import main_window as mw_mod
from media_resource_manager.config import AppConfig

from pathlib import Path


# ============================================================ 复刻 _FakeHandler
# （与 test_r48_web_login 同款：只测鉴权/登录分支，不走真 socket）

class _FakeHandler(webui._Handler):
    _cfg = None
    _db = None
    _headers = None
    _path = "/api/x"
    client_address = ("127.0.0.1", 0)

    def __init__(self, cfg, headers=None, body=None, client_ip="127.0.0.1"):
        self._cfg = cfg
        self._db = None
        self._headers = headers or {}
        self._path = "/api/x"
        self._body_obj = body or {}
        self._sent = None
        self.client_address = (client_ip, 0)

    def _send(self, code, body, ctype, extra=None):
        self._sent = (code, body, ctype, extra or {})
        return self._sent

    def _json(self, obj, code=200, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(code, raw, "application/json; charset=utf-8", extra)

    def _body(self):
        return dict(self._body_obj)

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

    def json(self):
        return json.loads(self._sent[1].decode("utf-8"))


@pytest.fixture
def cfgs():
    """三种配置（显式带 web_lan_open，不依赖默认值——R152-E5 后默认 False）。"""
    a = AppConfig()
    a.web_password = ""
    a.web_lan_open = True
    b = AppConfig()
    b.web_password = "secret"
    b.web_lan_open = True
    c = AppConfig()
    c.web_password = "secret"
    c.web_lan_open = False
    yield a, b, c


# ============================================================ (a) 对话框无用户名行

def test_main_window_web_dialog_has_no_username_row():
    """启用 Web 功能对话框不得再出现用户名输入行 / 账号展示。"""
    src = Path(mw_mod.__file__).read_text(encoding="utf-8")
    assert 'f.addRow("用户名"' not in src, "对话框还留着用户名输入行"
    assert "self.cfg.web_user =" not in src, "对话框还在写 cfg.web_user"
    assert "账号：" not in src, "成功弹窗还显示账号行"

# ============================================================ (b) 鉴权只认密码

def test_login_with_correct_password_succeeds(cfgs):
    """设了密码时，正确密码 → 登录成功，返回的 user 是常量 DEFAULT_USER。"""
    _, withpw, _ = cfgs
    h = _FakeHandler(withpw, body={"password": "secret"}, client_ip="127.0.0.1")
    h._do_login()
    code, _, _, _ = h._sent
    assert code == 200, f"正确密码被拒（HTTP {code}）"
    assert h.json()["ok"] is True
    assert h.json()["user"] == webui.DEFAULT_USER


def test_login_with_wrong_password_rejected(cfgs):
    """设了密码时，错误密码 → 401。"""
    _, withpw, _ = cfgs
    h = _FakeHandler(withpw, body={"password": "wrong"}, client_ip="127.0.0.1")
    h._do_login()
    code, _, _, _ = h._sent
    assert code == 401, "错误密码没被拦"


def test_auth_ignores_cfg_web_user(cfgs):
    """R105 核心：鉴权只用 DEFAULT_USER，不读 cfg.web_user（内部不再区分用户）。

    用「公网 + 关免密」那一档（c），让密码路径真正生效；否则回环/免密会
    直接放行，测不出用户名是否被忽略。
    """
    _, _, strict = cfgs
    strict.web_user = "root"          # 残留/老用户名，应被忽略
    # 用 DEFAULT_USER 签发的 Basic Auth 必须放行
    tok = base64.b64encode(
        f"{webui.DEFAULT_USER}:secret".encode()).decode()
    h = _FakeHandler(strict,
                     headers={"Authorization": f"Basic {tok}"},
                     client_ip="203.0.113.5")
    assert h._auth_ok() is True
    # 用残存用户名 "root" 签发则被拒 —— 证明鉴权不认 cfg.web_user
    tok2 = base64.b64encode(b"root:secret").decode()
    h2 = _FakeHandler(strict,
                      headers={"Authorization": f"Basic {tok2}"},
                      client_ip="203.0.113.5")
    assert h2._auth_ok() is False


# ============================================================ (c) 老配置兼容

def test_old_config_with_web_user_loads():
    """老配置带 web_user 字段：from_dict 不报错、不崩溃。

    （R105 决策：保留 web_user 字段为废弃/无操作位，老配置照常加载，
     鉴权不再读它。）
    """
    d = {"web_user": "legacyadmin", "web_password": "x", "web_port": 9090}
    cfg = AppConfig.from_dict(d)
    assert cfg.web_password == "x"
    assert cfg.web_port == 9090
    # 字段保留（废弃但兼容），不因老配置而异常
    assert cfg.web_user == "legacyadmin"
