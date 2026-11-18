"""R141 护栏：Web 密码回归「选填」 —— 留空能启用，本机/局域网免密直进。

钉的是**行为**（真 handler 替身走 `_auth_ok` / `_do_login`）+ 关键源码约束，
不钉数值：
  · 免密直进：空密码 + 本机(127.0.0.1) / 局域网(192.168.x) → 直接放行；
  · **安全侧不放松**（反向）：空密码 + 公网客户端 → 仍必须拦（R58 护栏）；
  · 设了密码时密码仍然有效（存量用户行为不变）；
  · 桌面端启用对话框**不再强制**「至少 4 位」，且 0.0.0.0+空密码有确认。

反向可变：把 `toggle_web` 的 4 位校验加回来 → 第 1/2 条源码判据必须红。
"""

from __future__ import annotations

import ast
import pathlib
import re
import types

import pytest

from media_resource_manager import webui

MAINWIN = pathlib.Path(__file__).resolve().parents[1] / \
    "media_resource_manager" / "gui" / "main_window.py"


# ------------------------------------------------------------ handler 替身


class _H(webui._Handler):
    """只测鉴权/登录分支，不走真 socket（与 R48 那份同思路）。

    ⚠ `cfg` / `headers` / `path` 在基类里是 property，子类不能直接赋值 ⇒
    一律走 `_xxx` 私有槽再由 property 读出来。
    ⚠ `client_address` 必须能注入：R52 的免密判定第一步就读它，
    少了这个属性 ⇒ 免密分支永远走不到 ⇒ 判据集体假红。
    """

    _cfg = None
    _db = None
    _headers = None
    _path = "/api/x"

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
        import json as _j
        raw = _j.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(code, raw, "application/json; charset=utf-8", extra)

    def _body(self):
        return dict(self._body_obj)

    @property
    def cfg(self):
        return self._cfg

    @property
    def headers(self):
        return self._headers

    @property
    def path(self):
        return self._path


def _cfg(password="", lan_open=True, whitelist=None, bind="127.0.0.1"):
    return types.SimpleNamespace(
        web_password=password, web_lan_open=lan_open,
        web_ip_whitelist=whitelist or [], web_bind=bind, web_port=8080)


def _src() -> str:
    return MAINWIN.read_text(encoding="utf-8")


def _toggle_web_src() -> str:
    """取出 toggle_web 的函数体源码（到下一个 def 为止）。"""
    s = _src()
    i = s.index("def toggle_web(")
    j = s.index("\n    def ", i + 10)
    return s[i:j]


# ------------------------------------------------------------ 行为：免密直进


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "192.168.1.9", "10.0.0.5"])
def test_empty_password_goes_straight_in_from_local_and_lan(ip):
    """★ R141 第 2 条：空密码 + 本机/局域网 → 不弹密码框，直接进。"""
    h = _H(_cfg(password=""), client_ip=ip)
    assert h._auth_ok() is True, f"{ip} 空密码竟被拦 —— 免密回归没生效"


def test_empty_password_session_never_reports_password_required():
    """空密码时 /api/session 的 password_required 必须是 False（前端不弹框）。"""
    h = _H(_cfg(password=""), client_ip="127.0.0.1")
    # 直接走 _do_login 的空密码路径：返回 200 + ok + password_required=False
    h._do_login()
    assert h._sent[0] == 200, "空密码登录被拒 —— R48/F 的免密语义被破坏"
    body = h.json() if hasattr(h, "json") else None
    import json as _j
    body = _j.loads(h._sent[1].decode("utf-8"))
    assert body["ok"] is True
    assert body["password_required"] is False, \
        "空密码却告诉前端「需要密码」⇒ 浏览器会弹密码框"


def test_empty_password_does_not_leak_to_public_clients():
    """★ 反向（安全侧不放松）：空密码 + **公网**客户端仍必须拦。

    R58 钉死的护栏，R141 只放宽「本机/局域网」，不开放全网。
    ⚠ R220 联动（台账）：R220 后「空密码 + 绑本机」无条件免密 —— 绑本机
    物理上看不到公网客户端，故本判据改用**开放绑定 0.0.0.0** 表达公网
    暴露面（行为矩阵全集见 test_r220_web_password_free.py）。
    """
    h = _H(_cfg(password="", bind="0.0.0.0"), client_ip="203.0.113.9")
    assert h._client_allowed_by_ip() is False
    assert h._auth_ok() is False, "公网 + 空密码竟放行 —— 图库对全网敞开"


def test_password_still_protects_when_set():
    """存量兼容：设了密码的人行为不变 —— 没凭据不让进，密码对了才行。"""
    cfg = _cfg(password="secret", lan_open=False)
    assert _H(cfg, client_ip="127.0.0.1")._auth_ok() is False
    import base64
    # ⚠ 用户名必须取 `webui.DEFAULT_USER`（R105 后 Web 只认密码，用户名是
    # 常量），硬编码 "admin" 会在常量不是它时假红。
    tok = base64.b64encode(
        f"{webui.DEFAULT_USER}:secret".encode()).decode()
    assert _H(cfg, headers={"Authorization": f"Basic {tok}"},
              client_ip="127.0.0.1")._auth_ok() is True


def test_password_can_be_cleared_back_to_passwordless():
    """存量兼容第 2 半：密码清空后立刻回到免密（不是残留旧密码）。"""
    cfg = _cfg(password="secret", lan_open=True)
    cfg.web_password = ""          # 用户把框清空 ⇒ 写入空串
    assert _H(cfg, client_ip="127.0.0.1")._auth_ok() is True


def test_scope_of_r141_is_local_not_public():
    """⚠ 如实钉住本单的**边界**：放宽的是本机/局域网，公网+空密码仍拦。

    这是 R58 钉死的安全护栏（test_r48_web_login 有 3 条盯着），R141 订单
    同时要求「无新增回归」⇒ 本棒**刻意不动**它。若用户后续裁决「公网也
    要免密」，必须连那 3 条护栏一起改，不能只改产品码。
    ⚠ R220 联动（台账）：R220 正是那次裁决的**部分**落地（绑本机免密），
    公网边界仍在 —— 本判据随之改用开放绑定 0.0.0.0 表达（见
    test_r220_web_password_free.py 的行为矩阵）。
    """
    h = _H(_cfg(password="", lan_open=False, bind="0.0.0.0"),
           client_ip="203.0.113.9")
    assert h._auth_ok() is False


# ------------------------------------------------------------ 源码：桌面端对话框


def test_no_min_length_password_gate_in_toggle_web():
    """★ R141 第 1 条根因：启用对话框不再有「至少 4 位」的强制校验。"""
    # ⚠ 剥掉注释再判：说明文字里写了「原来要求至少 4 位」，不剥会假红。
    seg = re.sub(r"#[^\n]*", "", _toggle_web_src())
    assert "至少 4 位" not in seg, \
        "启用 Web 又要求密码至少 4 位 —— 用户没法留空启用（R141 原话）"
    assert "len(pw.text().strip()) < 4" not in seg
    assert "密码选填" in seg or "留空=免密" in seg, \
        "提示文案应写明密码是选填的"


def test_password_placeholder_says_optional():
    pw_placeholder = re.search(r'pw\.setPlaceholderText\("([^"]+)"\)', _src())
    assert pw_placeholder, "找不到密码框占位符"
    assert "必填" not in pw_placeholder.group(1), "占位符仍写「必填」"
    assert "留空" in pw_placeholder.group(1), "占位符应说明留空 = 免密"


def test_zero_zero_confirmation_exists_and_does_not_block():
    """★ R141 第 3 条 → 已被 2026-11-05 P0 安全修复**改强**。

    R141 当时是「0.0.0.0 + 空密码 → 弹一次确认，确认后仍允许启用」。
    P0 修复把它升级成**直接拒绝启用**：非回环绑定 + 空密码会让同网段
    任意设备免密接管全部数据接口（图库/原图/fs 全盘浏览/整库备份），
    点一下"是"就把整台机器暴露出去 —— 确认弹窗挡不住误操作。

    本条随之改判据：不再要求「确认但仍放行」，改为锁「拒绝 + 提示改回
    本机或设密码」这条更强的现状。判据顺序反了：拒绝分支必须排在
    web_enabled=True **之前**（先拒绝、绝不写启用）。
    """
    seg = _toggle_web_src()
    assert '"127.0.0.1", "localhost", "::1"' in seg, \
        "缺少「监听地址非本机」的判定（0.0.0.0 安全闸没了）"
    # 现在是拒绝（warning + return），不再是确认（question）。
    assert "QMessageBox.warning" in seg, "0.0.0.0+空密码应拒绝启用并告警"
    assert "必须设置密码" in seg or "已被禁止" in seg, \
        "拒绝文案要写明这种组合被禁止（P0 安全语义）"
    # 顺序：拒绝分支必须排在启用之前。
    i_refuse = seg.index("QMessageBox.warning")
    i_enable = seg.index("self.cfg.web_enabled = True")
    assert i_refuse < i_enable, \
        "拒绝分支排在启用之后 —— 等于先开再拒，局域网已经被暴露"


def test_clear_password_hint_shown_when_password_already_set():
    """已设密码时要有「清空即可转回免密」的说明，用户才知道怎么回去。"""
    seg = _toggle_web_src()
    assert "清空" in seg and "免密" in seg, \
        "已设密码的用户看不到「如何转回免密」的提示"


def test_toggle_web_still_compiles_and_has_no_bare_return_on_empty_pw():
    """AST 级：toggle_web 里不存在「密码为空就 return」的早退分支。"""
    tree = ast.parse(_src())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "toggle_web")
    src_seg = ast.get_source_segment(_src(), fn) or ""
    assert "没密码谁都能看你的图库" not in src_seg, \
        "旧的强制密码警告文案还在"
