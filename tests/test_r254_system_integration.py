"""R254：桌面系统集成搬到网页端（版本 / 开机自启 / 检查更新）。

先澄清一个事实：**这三项在桌面端早就有了**（`act_autostart` /
`act_hotkeys` / `act_check_update`）。缺的是网页端入口 —— 装在 NAS 或小主机
上以后人不在机器前，只能靠网页开。

托盘与全局热键**只能**桌面端做（浏览器碰不到窗口系统），所以这里不假装支持。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


def test_desktop_already_has_all_three():
    """守住事实：别把已有的东西当缺失再"实现"一遍。"""
    src = (PKG / "gui/main_window.py").read_text(encoding="utf-8")
    assert "def toggle_autostart" in src
    assert "def open_hotkey_dialog" in src
    assert "def check_update" in src


def test_routes_registered():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    for k in ('"/api/system/integration"', '"/api/system/update"',
              '"/api/system/autostart"'):
        assert k in src, f"缺路由 {k}"


def test_integration_reports_version_and_autostart():
    from media_resource_manager import webui

    class O:
        cfg = type("C", (), {"proxy": "", "proxy_tasks_only": False})()
        db = None

    r = webui._Handler._api_sys_integration(O())
    assert r["ok"] is True
    assert "version" in r and "autostart_supported" in r
    # 平台字段要如实上报（前端据此判断要不要显示开关）
    assert r["platform"]


def test_autostart_refuses_on_unsupported_platform(monkeypatch):
    """非 Windows 必须**明确拒绝**，不能返回 ok=True 让用户以为设上了。"""
    from media_resource_manager import autostart as A, webui

    class O:
        cfg = None
        db = None

    monkeypatch.setattr(A, "supported", lambda: False)
    r = webui._Handler._api_sys_autostart(O(), {"enabled": True})
    assert r["ok"] is False
    assert "Windows" in r["error"]


def test_autostart_calls_install_or_remove(monkeypatch):
    from media_resource_manager import autostart as A, webui

    calls = []

    class O:
        cfg = None

        class db:                     # noqa: N801
            @staticmethod
            def audit(*a, **k):
                pass

    monkeypatch.setattr(A, "supported", lambda: True)
    monkeypatch.setattr(A, "install", lambda *a, **k: (calls.append("install")
                                                       or (True, "ok")))
    monkeypatch.setattr(A, "remove", lambda *a, **k: (calls.append("remove")
                                                      or (True, "ok")))
    monkeypatch.setattr(A, "status", lambda: (True, ""))
    r = webui._Handler._api_sys_autostart(O(), {"enabled": True})
    assert r["ok"] and calls == ["install"]
    r = webui._Handler._api_sys_autostart(O(), {"enabled": False})
    assert r["ok"] and calls == ["install", "remove"]


def test_update_endpoint_never_raises(monkeypatch):
    """检查更新会联网：网络炸了也只回中文说明，不许把异常抛给请求线程。"""
    from media_resource_manager import update_check, webui

    def boom(*a, **k):
        raise OSError("no network")

    monkeypatch.setattr(update_check, "check", boom)

    class O:
        cfg = None

    r = webui._Handler._api_sys_update(O())
    assert r["ok"] is False and "no network" in r["error"]


def test_frontend_wires_system_block():
    st = (PKG / "webapp/src/views/Settings.vue").read_text(encoding="utf-8")
    for k in ("程序版本", "检查更新", "开机自启", "toggleAutostart", "loadSys"):
        assert k in st, f"设置页缺「{k}」"
    api = (PKG / "webapp/src/api.js").read_text(encoding="utf-8")
    for k in ("sysIntegration:", "sysUpdate:", "sysAutostart:"):
        assert k in api


def test_built_bundle_contains_system_block():
    d = PKG / "web/static/app/assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    assert "开机自启" in js and "/api/system/update" in js, "前端没重新构建"
