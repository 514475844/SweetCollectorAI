# -*- coding: utf-8 -*-
"""R154-F20 手机扫码登录 护栏。

三层（R63-D* 口径）：
- L1 纯函数：token 注册表 approve/reject/GC（直接操作 webui._QR_TOKENS，
  autouse fixture 每测清空，防模块级状态串测）。
- L2 源码判据：路由字面量 / confirm 复用 make_session + 一次性 / image 用
  segno / 签发限流 / main_window 信号与 hook 接线 / spec 收 segno /
  前端入口。**按函数边界切块**（R84：防同文欺骗——块外另有同款字符串）。
- L3 真服务：起 WebServer（端口 0）走完整 HTTP 流程：
  签发 → 二维码 SVG → status pending → 未授权 confirm 403 → approve →
  approved → confirm 拿 Set-Cookie → cookie 访问 /api/summary → 再 confirm
  401（一次性）。另测拒绝流 / 过期 / 未知 token / 确认页 / 签发限流 429。
"""
import json
import re
import time
import types

import pytest

from media_resource_manager import webui

# 仓库根 = 本测试文件的父目录的父目录。
# ⚠ 2026-11-09 修：原来是写死的旧机器路径 <user-path>
# 整文件 8 条全部 FileNotFoundError 假红）。跟着 __file__ 走，换机换盘符都成立。
from pathlib import Path
ROOT = str(Path(__file__).resolve().parents[1])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _cfg(password="pw123", lan_open=True, bind="127.0.0.1"):
    return types.SimpleNamespace(
        web_password=password, web_lan_open=lan_open, web_ip_whitelist=[],
        web_bind=bind, web_port=0, web_enabled=True)


def _src(rel):
    # R216 仪器第六变体：webui.py/app.js 是 CRLF，read_text 会翻译成 LF；
    # 源码判据必须 newline="" 读真实字节视图。
    with open(ROOT + "\\" + rel, "r", newline="", encoding="utf-8") as f:
        return f.read()


def _block(src, marker):
    """从 marker 到下一个同级 `def` 切块（R84：按函数边界切，防同文欺骗）。"""
    i = src.index(marker)
    m = re.search(r"\n    def ", src[i + 1:])
    j = i + 1 + m.start() if m else len(src)
    return src[i:j]


def _req(port, method, path, body=None, headers=None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = dict(headers or {})
    payload = None
    if body is not None:
        h.setdefault("Content-Type", "application/json")
        payload = json.dumps(body)
    conn.request(method, path, body=payload, headers=h)
    resp = conn.getresponse()
    data = resp.read()
    hdrs = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, hdrs, data


@pytest.fixture(autouse=True)
def _clean_qr_registry():
    """L1/L3 共用模块级 _QR_TOKENS，每测前后清空防串测。"""
    yield
    with webui._QR_LOCK:
        webui._QR_TOKENS.clear()


class _FakeDb:
    """最小 db 替身：query 恒 0 行计数，audit 静默（WebServer 构造与
    /api/summary 需要 db 存在；真库不在本护栏范围）。"""

    def query(self, sql):
        return [{"c": 0}]

    def audit(self, *a, **k):
        pass


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """起一个真 WebServer（端口 0 自动分配），测完 stop。"""
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    cfg = _cfg()
    cfg.db_path = tmp_path / "home" / "mrm.db"
    s = webui.WebServer(cfg, _FakeDb())
    ok, msg = s.start()
    assert ok, f"Web 服务起不来：{msg}"
    port = s._httpd.server_address[1]
    yield s, cfg, port
    s.stop()


def _issue(port):
    st, _, body = _req(port, "POST", "/api/login/qrcode", body={})
    assert st == 200, f"签发失败：HTTP {st} {body[:200]}"
    d = json.loads(body)
    assert d["ok"] and d["token"] and d["url"]
    assert "/qr-login?token=" in d["url"]
    return d["token"], d["url"]


# --------------------------------------------------------------------------
# L1 纯函数：注册表 approve/reject/GC
# --------------------------------------------------------------------------

def _put(tok, status="pending", age=0.0):
    with webui._QR_LOCK:
        webui._QR_TOKENS[tok] = {
            "created_at": time.time() - age, "status": status,
            "ip": "1.1.1.1", "url": "http://x/qr-login?token=" + tok}


def test_approve_pending_token():
    _put("t-approve")
    assert webui.approve_qr_token("t-approve") is True
    assert webui._QR_TOKENS["t-approve"]["status"] == "approved"


def test_approve_used_token_returns_false():
    _put("t-used", status="used")
    assert webui.approve_qr_token("t-used") is False
    assert webui._QR_TOKENS["t-used"]["status"] == "used"


def test_approve_rejected_token_returns_false():
    _put("t-rej", status="rejected")
    assert webui.approve_qr_token("t-rej") is False


def test_reject_pending_token():
    _put("t-rej2")
    assert webui.reject_qr_token("t-rej2") is True
    assert webui._QR_TOKENS["t-rej2"]["status"] == "rejected"


def test_reject_used_token_returns_false():
    _put("t-used2", status="used")
    assert webui.reject_qr_token("t-used2") is False


def test_gc_removes_expired():
    _put("t-expired", age=1000.0)
    _put("t-fresh")
    webui._qr_gc()
    assert "t-expired" not in webui._QR_TOKENS
    assert "t-fresh" in webui._QR_TOKENS


def test_unknown_token_approve_reject_false():
    assert webui.approve_qr_token("nope") is False
    assert webui.reject_qr_token("nope") is False


def test_approve_expired_token_false():
    _put("t-exp2", age=1000.0)
    assert webui.approve_qr_token("t-exp2") is False
    assert "t-exp2" not in webui._QR_TOKENS


# --------------------------------------------------------------------------
# L2 源码判据（按块切）
# --------------------------------------------------------------------------

def test_route_literals_present():
    src = _src("media_resource_manager/webui.py")
    for lit in ('"/api/login/qrcode"', '"/api/login/qrcode/confirm"',
                '"/api/login/qrcode/image"', '"/api/login/qrcode/status"',
                '"/qr-login"'):
        assert lit in src, f"路由字面量缺失：{lit}"


def test_confirm_reuses_make_session_and_is_once():
    src = _src("media_resource_manager/webui.py")
    blk = _block(src, "def _do_qr_confirm")
    assert "make_session(DEFAULT_USER, want)" in blk, (
        "confirm 必须复用 make_session（与密码登录同一 session 机制）")
    assert 'rec["status"] = "used"' in blk, "confirm 必须标记 used（一次性）"
    assert 'rec["status"] != "approved"' in blk, "confirm 必须要求 approved"


def test_image_uses_segno():
    src = _src("media_resource_manager/webui.py")
    blk = _block(src, "def _do_qr_image")
    assert "segno" in blk, "二维码必须由 segno 生成"
    assert "svg_inline" in blk
    assert "_qr_rec" in blk, "image 端点必须校验 token（失效回 410）"


def test_request_is_rate_limited():
    src = _src("media_resource_manager/webui.py")
    blk = _block(src, "def _do_qr_request")
    assert "_QR_ISSUE_MAX" in blk, "签发必须限流（防弹窗轰炸）"
    assert "_qr_issued" in blk
    assert "secrets.token_urlsafe" in blk, "token 必须用 secrets 生成"


def test_main_window_signal_and_hook_wired():
    src = _src("media_resource_manager/gui/main_window.py")
    assert "qr_login_requested = Signal(str, str)" in src, (
        "MainWindow 必须有扫码授权信号")
    assert "srv.qr_hook = self.qr_login_requested.emit" in src, (
        "_ensure_web_server 必须注入 qr_hook")


def test_main_window_slot_decides_and_shows():
    src = _src("media_resource_manager/gui/main_window.py")
    blk = _block(src, "def _show_next_qr_login")
    assert "approve_qr_token" in blk and "reject_qr_token" in blk
    assert "box.show()" in blk, "确认框必须非模态 show（防恶意扫码=主动点）"


def test_spec_bundles_segno():
    src = _src("build/pyinstaller.spec")
    assert "'segno'" in src, "spec hiddenimports 必须收 segno（否则打包漏）"

def test_confirm_page_script_exists():
    js = _src("media_resource_manager/web/static/js/qr-login.js")
    assert "/api/login/qrcode/confirm" in js, "确认页脚本必须自动 confirm"
    assert "/api/login/qrcode/status" in js, "确认页脚本必须轮询状态"
    assert "poll();" in js, "确认页脚本必须真的启动轮询"


# --------------------------------------------------------------------------
# L3 真服务：完整 HTTP 流程
# --------------------------------------------------------------------------

def test_full_scan_login_flow(srv):
    """★ 主判据：签发→SVG→pending→未授权403→approve→approved→confirm
    拿 cookie→cookie 访问数据面→再 confirm 401（一次性）。"""
    _, _, port = srv
    tok, url = _issue(port)
    assert url.startswith("http://")

    # 二维码 SVG
    st, hd, body = _req(port, "GET", f"/api/login/qrcode/image?token={tok}")
    assert st == 200, f"二维码图片 HTTP {st}"
    assert hd.get("content-type", "").startswith("image/svg+xml")
    assert b"<svg" in body, "二维码不是 SVG"

    # 初始 pending
    st, _, body = _req(port, "GET", f"/api/login/qrcode/status?token={tok}")
    d = json.loads(body)
    assert d["ok"] and d["status"] == "pending"
    assert d["expires_in"] > 0 and d["expires_in"] <= 300

    # 未授权 confirm → 403
    st, _, _ = _req(port, "POST", "/api/login/qrcode/confirm",
                    body={"token": tok})
    assert st == 403, f"未授权 confirm 应 403，实得 {st}"

    # 桌面端授权
    assert webui.approve_qr_token(tok) is True
    st, _, body = _req(port, "GET", f"/api/login/qrcode/status?token={tok}")
    assert json.loads(body)["status"] == "approved"

    # confirm → Set-Cookie
    st, hd, body = _req(port, "POST", "/api/login/qrcode/confirm",
                        body={"token": tok})
    assert st == 200, f"confirm HTTP {st} {body[:200]}"
    d = json.loads(body)
    assert d["ok"] and d["user"] == "admin"
    sc = hd.get("set-cookie", "")
    assert "mrm_session=" in sc, "confirm 必须发会话 cookie"
    session = sc.split("mrm_session=")[1].split(";")[0]

    # cookie 访问数据面
    st, _, body = _req(port, "GET", "/api/summary",
                       headers={"Cookie": f"mrm_session={session}"})
    assert st == 200, f"cookie 访问 /api/summary HTTP {st}"
    assert json.loads(body)["ok"] is True

    # 一次性：再 confirm → 401
    st, _, _ = _req(port, "POST", "/api/login/qrcode/confirm",
                    body={"token": tok})
    assert st == 401, f"重复 confirm 应 401（一次性），实得 {st}"


def test_reject_flow(srv):
    """拒绝流：reject 后 confirm 403，status rejected。"""
    _, _, port = srv
    tok, _ = _issue(port)
    assert webui.reject_qr_token(tok) is True
    st, _, _ = _req(port, "POST", "/api/login/qrcode/confirm",
                    body={"token": tok})
    assert st == 403
    st, _, body = _req(port, "GET", f"/api/login/qrcode/status?token={tok}")
    d = json.loads(body)
    assert d["ok"] and d["status"] == "rejected"


def test_image_unknown_token_410(srv):
    _, _, port = srv
    st, _, _ = _req(port, "GET", "/api/login/qrcode/image?token=nope")
    assert st == 410


def test_expired_token_rejected(srv):
    """手工把 token 改老 → status expired → confirm 401。"""
    _, _, port = srv
    tok, _ = _issue(port)
    webui._QR_TOKENS[tok]["created_at"] = time.time() - 1000
    st, _, body = _req(port, "GET", f"/api/login/qrcode/status?token={tok}")
    d = json.loads(body)
    assert not d["ok"] and d["status"] == "expired"
    st, _, _ = _req(port, "POST", "/api/login/qrcode/confirm",
                    body={"token": tok})
    assert st == 401


def test_qr_page_renders(srv):
    """确认页：有效 token → 200 且引用 qr-login.js；缺 token → 400。"""
    _, _, port = srv
    tok, _ = _issue(port)
    st, _, body = _req(port, "GET", f"/qr-login?token={tok}")
    assert st == 200
    assert b"qr-login.js" in body
    st, _, _ = _req(port, "GET", "/qr-login")
    assert st == 400


def test_issue_rate_limit_429(srv):
    """同 IP 连续签发：前 5 次 200，第 6 次 429（弹窗轰炸防线）。"""
    _, _, port = srv
    for _ in range(5):
        st, _, _ = _req(port, "POST", "/api/login/qrcode", body={})
        assert st == 200
    st, _, body = _req(port, "POST", "/api/login/qrcode", body={})
    assert st == 429, f"第 6 次签发应 429，实得 {st}"


def test_password_login_unaffected(srv):
    """无手机场景：密码登录路径零改动（回归保险丝）。"""
    _, cfg, port = srv
    assert cfg.web_password == "pw123"
    st, hd, body = _req(port, "POST", "/api/login", body={"password": "pw123"})
    assert st == 200, f"密码登录 HTTP {st}"
    assert "mrm_session=" in hd.get("set-cookie", "")
