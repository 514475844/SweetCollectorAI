# -*- coding: utf-8 -*-
"""V0.0.55 批1 护栏：定时自动关机。

安全红线（比功能本身更重要）：
  · on_idle / 一次性定点**不落盘** —— 重启后复活"跑完就关"等于把
    用户刚开机的电脑再安排关机，是事故；只有每日定点才持久化。
  · 倒计时最少 15 秒，随时可取消。
  · 空闲判定读不到队列时按"忙"处理，绝不因读不到就关。
"""
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_powerdown_next_ts_at_rolls_to_tomorrow():
    from media_resource_manager.powerdown import _next_ts_at
    t = _next_ts_at("03:30")
    assert t > time.time() - 86400 * 2 and t <= time.time() + 86400
    assert _next_ts_at("25:00") == 0.0
    assert _next_ts_at("abc") == 0.0


def test_powerdown_rejects_past_and_bad_mode():
    from media_resource_manager import powerdown as pd
    r = pd.schedule("at_time", target_ts=time.time() - 10)
    assert r.get("ok") is False and "已过" in r.get("error", "")
    assert pd.schedule("nonsense").get("ok") is False
    assert pd.cancel().get("ok") is True


def test_powerdown_idle_busy_never_ready():
    """队列忙 / 读不到 → 绝不能进入倒计时。"""
    from media_resource_manager import powerdown as pd

    class Busy:
        def snapshot(self): return {"running": {1: "x"}}
        def queued(self): return [(5, 1)]

    class Boom:
        def snapshot(self): raise RuntimeError("读不到")
        def queued(self): raise RuntimeError("读不到")

    assert pd._idle_since(Busy()) is None
    assert pd._idle_since(Boom()) is None

    class Idle:
        def snapshot(self): return {"running": {}}
        def queued(self): return []

    assert isinstance(pd._idle_since(Idle()), float)
    pd.cancel()


def test_onetime_plans_are_never_persisted():
    """webui 持久化逻辑：只有每日定点落盘，其他一律存 off。"""
    ui = _read("media_resource_manager/webui.py")
    seg = ui.split("def _api_powerdown", 1)[1]
    seg = seg.split("\n    def ", 1)[0] if "\n    def " in seg else seg[:4000]
    assert 'persist = (mode == "at_time" and daily and at_hhmm)' in seg
    assert 'auto_shutdown_mode = "at_time" if persist else "off"' in seg


def test_resume_only_daily():
    from media_resource_manager import powerdown as pd
    from media_resource_manager.config import AppConfig
    cfg = AppConfig()
    cfg.auto_shutdown_mode = "on_idle"
    assert pd.resume_from_cfg(cfg).get("resumed") is False
    cfg2 = AppConfig()
    cfg2.auto_shutdown_mode = "at_time"
    cfg2.auto_shutdown_daily = 0
    assert pd.resume_from_cfg(cfg2).get("resumed") is False
    cfg3 = AppConfig()
    cfg3.auto_shutdown_mode = "at_time"
    cfg3.auto_shutdown_daily = 1
    cfg3.auto_shutdown_at = "03:30"
    r = pd.resume_from_cfg(cfg3)
    assert r.get("ok") is True
    assert r.get("mode") == "at_time"
    pd.cancel()


def test_countdown_floor():
    s = (PKG / "powerdown.py").read_text(encoding="utf-8")
    assert "max(15, int(countdown or 60))" in s
    assert "max(30, int(idle_grace or 180))" in s


def test_settings_save_reapplies_shutdown_plan():
    """改 auto_shutdown_* 设置后，必须让内存计划跟着配置走（apply_cfg）。"""
    ui = _read("media_resource_manager/webui.py")
    seg = ui.split("def _api_settings_save", 1)[1]
    seg = seg.split("\n    def ", 1)[0] if "\n    def " in seg else seg[:8000]
    assert 'any(str(k).startswith("auto_shutdown_") for k in changed)' in seg
    assert "powerdown.apply_cfg(" in seg


def test_get_powerdown_returns_nested_plan():
    """GET /api/powerdown 把计划包在 plan 下，前端 pdRefresh 读 r.plan。"""
    ui = _read("media_resource_manager/webui.py")
    assert '"ok": True, "plan": powerdown.status()' in ui


def test_status_exposes_daily_and_hhmm():
    """status() 要带 daily / at_hhmm，前端才能回填每日定点下拉。"""
    s = (PKG / "powerdown.py").read_text(encoding="utf-8")
    seg = s.split("def status()", 1)[1].split("def ", 1)[0]
    assert '"daily": bool(' in seg and '"at_hhmm": str(' in seg


def test_settings_vue_has_shutdown_card():
    v = (PKG / "webapp/src/views/Settings.vue").read_text(encoding="utf-8")
    assert "定时自动关机" in v
    for k in ("pdApply", "pdCancel", "pdRefresh", "api.powerdown",
              "api.post('/api/powerdown'"):
        assert k in v, f"设置页缺关机接线 {k}"
    # on_idle/daily 都走即时动作，不进草稿
    assert "pdSetMode" in v


def test_api_js_has_inbox_and_powerdown():
    a = (PKG / "webapp/src/api.js").read_text(encoding="utf-8")
    assert "inboxLink:" in a and "/api/inbox/link" in a
    assert "powerdown:" in a and "/api/powerdown" in a


def test_onboarding_final_step_creates_first_task():
    """引导最后一步：能真建任务并起跑（V0.0.75 起走 taskSave+start，
    取代旧收件口 inboxLink —— 因为新版要把白/黑名单一起带进任务）。"""
    o = (PKG / "webapp/src/components/Onboarding.vue").read_text(encoding="utf-8")
    assert "api.taskSave(" in o
    assert "api.taskCtl('start'" in o
    assert "保存并运行任务" in o
    assert "useRouter" in o and "router.push('/tasks')" in o

