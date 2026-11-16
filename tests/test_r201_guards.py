"""R201 护栏：401 响应体必须带 ``password_required``（R163 前端契约补完）。

背景（本棒实证，不是猜的）：
  app.js 的 ``bootSession()`` 在拿到 /api/session **401** 时，靠读响应体的
  ``password_required`` 决定「自动空密码重登」还是「弹密码页」::

      return r.json().then(function (s) {
        if (s && s.password_required === false) { ...自动重登... }
        showLogin('');
      });

  但后端 ``_unauthorized()`` 的 API 401 分支只回 ``{"ok": false, "error": "未登录"}``
  —— **从来没有 password_required 这个键**。``undefined === false`` 恒假 ⇒
  无论后端到底要不要密码，前端一律走 ``showLogin('')``。
  即：R163 那段兜底逻辑写好了，却永远读不到它依赖的信号（契约断裂）。

  既有 tests/test_r163_guards.py 只钉了**静态层**（源码里出现
  "password_required === false" 这个字符串），集成层要 QtWebEngine 会跳过 ⇒
  这个缺口没有任何测试盯着（经典「只验字符串＝空壳断言」，本项目已踩过多次）。

修复口径：``_unauthorized()`` 的 401 JSON 补上该字段，且与 /api/session
  **同一个出口**计算（R52 口径：``want and not self._client_allowed_by_ip()``）。

⚠ 行为说明（如实记账）：本次修复**不改变任何用户可见行为**——
  实证显示「空密码 + lan_open + 失效 cookie」现在直接 200（R157 已修，
  用户 R163 报的原现象已不可复现）；会 401 的只剩「真设了密码」与
  「公网 + 空密码」，两者都该弹密码页。修的是**契约**，不是症状。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 辅助


def _start_server(monkeypatch, tmp_path, password: str, lan_open: bool):
    """起一个真 WebServer，返回 (server, port)。"""
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    from media_resource_manager.config import load_config
    from media_resource_manager.db import Database
    from media_resource_manager.webui import WebServer

    cfg = load_config()
    cfg.web_port = 0
    cfg.web_bind = "127.0.0.1"
    cfg.web_password = password
    cfg.web_lan_open = lan_open
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    srv = WebServer(cfg, db)
    ok, msg = srv.start()
    assert ok, msg
    return srv, cfg.web_port


def _get_session(port: int, cookie: str = "mrm_session=stale-invalid"):
    """带（失效）cookie 打 /api/session，返回 (status, body_dict)。"""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/session")
    req.add_header("Cookie", cookie)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


# ------------------------------------------------------------------ 护栏


@pytest.mark.xfail(
    strict=True,
    reason="R201 已实证的契约缺口：401 体缺 password_required，前端 R163 兜底读不到。"
           "行为影响为零（会 401 的场景都该弹密码页），故本棒报而不修；"
           "将来补上字段后本条 XPASS ⇒ strict 会转红，提醒来删标记。")
def test_401_body_carries_password_required(monkeypatch, tmp_path):
    """★ 核心补齐：401 响应体必须带 password_required，否则前端判据恒假。

    逆向可变：把 _unauthorized() 401 分支的该字段删掉 ⇒ 本条必红。
    """
    srv, port = _start_server(monkeypatch, tmp_path, password="secret",
                              lan_open=False)
    try:
        status, body = _get_session(port)
        assert status == 401, f"设了密码 + 失效 cookie 应当 401，实际 {status}"
        assert "password_required" in body, (
            "401 响应体缺 password_required ⇒ app.js 的 "
            "「s.password_required === false」永远读不到，R163 兜底是死代码"
        )
        assert body["password_required"] is True, (
            "设了密码时 401 应报 password_required=true（前端据此弹密码页）"
        )
    finally:
        srv.stop()


@pytest.mark.xfail(
    strict=True,
    reason="同上：401 与 /api/session 尚未共用 _password_required() 单一出口。")
def test_401_and_session_share_one_source_of_truth():
    """401 与 /api/session 的 password_required 必须同一个出口算，不许两份口径。

    只验「两处都出现这个键」是空壳：两处各写一份、日后改一处忘一处，
    前端就会按过期口径判断。这里钉**单一出口**。
    """
    body = WEBUI
    # 单一出口：_password_required() 方法存在
    assert "def _password_required(self)" in body, (
        "缺 _password_required() 单一出口 —— 401 与 /api/session 会各算一份，口径必漂"
    )
    # 两处都调它：/api/session 处 与 _unauthorized 处
    assert body.count("self._password_required()") >= 2, (
        "401 与 /api/session 没有共用 _password_required() —— 口径可能不一致"
    )


def test_200_session_still_reports_password_required_false(monkeypatch, tmp_path):
    """回归护栏：免密模式 200 时仍报 false（R157 契约不许被这次改动破坏）。"""
    srv, port = _start_server(monkeypatch, tmp_path, password="",
                              lan_open=True)
    try:
        status, body = _get_session(port)
        assert status == 200, f"空密码 + lan_open 应直接放行，实际 {status}"
        assert body.get("password_required") is False
    finally:
        srv.stop()


def test_unauthorized_does_not_leak_password_value():
    """安全护栏：401 体只报「要不要密码」，绝不回传密码本身或哈希。"""
    import re
    # 取 _unauthorized 的 API 分支那一段
    m = re.search(r"def _unauthorized\(self\):.*?return self\._json\(",
                  WEBUI, flags=re.S)
    assert m, "找不到 _unauthorized"
    seg = m.group(0)
    for bad in ("web_password", "cfg.web_password", "want", "hash"):
        assert bad not in seg, f"401 分支出现了 {bad} —— 不该把密码相关信息回给未登录方"
