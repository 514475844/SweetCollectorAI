# -*- coding: utf-8 -*-
"""定时自动关机（2026-11-18 批1，用户需求）。

两种触发：
  · 「任务跑完再关」：等所有下载任务彻底空闲后关机（挂机采集最怕跑一半没人管）；
  · 「定点关机」：到点就关（不管任务，适合"到点必须走开"）。

安全设计（自用软件，绝不半夜把活干废）：
  1. 默认关闭；开启必须显式勾选。
  2. 关机前留**倒计时**（默认 60 秒），期间可随时取消 —— 用户看到通知能反应。
  3. 「任务跑完」模式对"空闲"判定保守：队列里还有排队项就不算空闲；
     且要求**连续空闲**满一段宽限（默认 3 分钟）才动手，防止两个任务之间
     的短暂空档误判成"跑完了"。
  4. 真关机走 Windows 官方 `shutdown` 命令；Linux/mac 用 systemd `shutdown`。
     只发一次，发成功就把计划清空，绝不反复触发。
  5. 定时器是**纯 Python 线程**（daemon），不注册系统计划任务，软件一退就没了 ——
     不留"关掉软件还在偷偷倒数"的悬空计划。
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time

# 计划状态（进程内单例；daemon 线程轮询）
_LOCK = threading.Lock()
_PLAN: dict = {
    "mode": "",              # "" | "on_idle" | "at_time"
    "cancel_event": None,    # threading.Event：置位 = 取消
    "countdown": 60,         # 关机前倒计时秒
    "idle_grace": 180,       # on_idle：连续空闲需满足这么多秒才动手
    "target_ts": 0.0,        # at_time：目标时刻（epoch 秒）
    "daily": False,          # 每日定点（触发后自动顺延明天）
    "at_hhmm": "",           # 每日定点 "HH:MM"
    "note": "",
}
_thread: threading.Thread | None = None
_STOP = threading.Event()


def status() -> dict:
    with _LOCK:
        return {
            "mode": _PLAN.get("mode", ""),
            "countdown": int(_PLAN.get("countdown", 60)),
            "target_ts": float(_PLAN.get("target_ts", 0.0)),
            "daily": bool(_PLAN.get("daily", False)),
            "at_hhmm": str(_PLAN.get("at_hhmm", "")),
            "note": _PLAN.get("note", ""),
        }


def cancel() -> dict:
    """取消当前关机计划。"""
    with _LOCK:
        ev = _PLAN.get("cancel_event")
        _PLAN["mode"] = ""
        _PLAN["target_ts"] = 0.0
    if ev:
        ev.set()
    _notify("已取消自动关机", "auto-shutdown cancelled", "info")
    return {"ok": True, "cancelled": True}


def _next_ts_at(hhmm: str) -> float:
    """把 "HH:MM" 换算成**下一次**发生的 epoch 秒（今天过了就明天）。"""
    try:
        hh, mm = (int(x) for x in hhmm.strip().split(":")[:2])
    except (ValueError, AttributeError):
        return 0.0
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return 0.0
    now = time.localtime()
    due = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0,
                       0, 0, -1))
    if due <= time.time():
        due += 86400
    return due


def schedule(mode: str, *, countdown: int = 60, idle_grace: int = 180,
             target_ts: float = 0.0, at_hhmm: str = "", daily: bool = False,
             queue=None) -> dict:
    """安排关机。

    mode：
      · "off"      —— 取消（等价 cancel）
      · "on_idle"  —— 等队列彻底空闲（且连续空闲满 idle_grace）后关机
      · "at_time"  —— 到 target_ts（epoch 秒）关机
    countdown：进入倒计时后给用户的反悔窗口（秒）。
    """
    if mode == "off":
        return cancel()
    if mode not in ("on_idle", "at_time"):
        return {"ok": False, "error": "未知的关机模式"}
    daily_on = bool(daily) and mode == "at_time"
    if mode == "at_time" and not daily_on and target_ts <= time.time():
        return {"ok": False, "error": "定点时间已过，请重选时间"}
    countdown = max(15, int(countdown or 60))
    idle_grace = max(30, int(idle_grace or 180))
    with _LOCK:
        _PLAN.update({
            "mode": mode,
            "cancel_event": threading.Event(),
            "countdown": countdown,
            "idle_grace": idle_grace,
            "target_ts": float(target_ts or 0.0),
            "daily": daily_on,
            "at_hhmm": str(at_hhmm or "") if daily_on else "",
            "note": "",
        })
        if daily_on:
            t = _next_ts_at(at_hhmm)
            if t <= 0:
                _PLAN["mode"] = ""
                return {"ok": False, "error": "定点时间格式不对（应为 HH:MM）"}
            _PLAN["target_ts"] = t
    _ensure_thread(queue)
    if mode == "on_idle":
        msg = f"已安排：任务全部跑完后自动关机（倒计时 {countdown} 秒，可取消）"
    elif daily_on:
        msg = f"已安排：每天 {at_hhmm} 自动关机（倒计时 {countdown} 秒）"
    else:
        msg = f"已安排：到点自动关机（{time.strftime('%m-%d %H:%M', time.localtime(target_ts))}，倒计时 {countdown} 秒）"
    _notify(msg, msg, "info")
    return {"ok": True, **status()}


def _ensure_thread(queue):
    global _thread
    if _thread and _thread.is_alive():
        # 把最新队列句柄交给已在跑的监视线程
        with _LOCK:
            _MON["queue"] = queue
        return
    _MON["queue"] = queue
    _STOP.clear()
    _thread = threading.Thread(target=_watch, daemon=True, name="wb-auto-shutdown")
    _thread.start()


_MON: dict = {"queue": None}


def _idle_since(queue):
    """队列空闲返回 now（起点由调用方维护）；忙或读不到状态返回 None。

    空闲 = running 空 且 queued 空。取不到队列时按"忙"处理（保守，
    绝不能因为读不到状态就误关机）。
    """
    try:
        snap = queue.snapshot()
        running = snap.get("running") or {}
        queued = queue.queued() if hasattr(queue, "queued") else []
    except Exception:
        return None
    if running or queued:
        return None
    return time.time()


def _watch():
    idle_from = None
    countdown_start = None
    fired_once = False
    while not _STOP.wait(2):
        with _LOCK:
            mode = _PLAN.get("mode", "")
            ev = _PLAN.get("cancel_event")
            cd = int(_PLAN.get("countdown", 60))
            grace = int(_PLAN.get("idle_grace", 180))
            target = float(_PLAN.get("target_ts", 0.0))
            daily = bool(_PLAN.get("daily"))
            hhmm = str(_PLAN.get("at_hhmm", ""))
        if ev is not None and ev.is_set():
            idle_from = countdown_start = None
            fired_once = False
            continue
        if not mode:
            idle_from = countdown_start = fired_once = None
            continue
        if fired_once and not daily:
            continue
        queue = _MON.get("queue")
        ready_now = False
        if mode == "at_time":
            ready_now = target and time.time() >= target
        elif mode == "on_idle":
            now_idle = _idle_since(queue) if queue is not None else None
            if now_idle is None:
                idle_from = None                      # 还在忙：重置空闲起点
            else:
                idle_from = idle_from or now_idle
                if time.time() - idle_from >= grace:
                    ready_now = True
        if ready_now:
            countdown_start = countdown_start or time.time()
            remain = cd - (time.time() - countdown_start)
            if remain <= 0:
                _do_shutdown()
                if daily:
                    # 每日模式：关机命令已发（若用户在倒计时后开机回来，
                    # 计划仍生效）。顺延到明天同一时刻，继续等。
                    with _LOCK:
                        nt = _next_ts_at(hhmm)
                        if nt:
                            _PLAN["target_ts"] = nt
                    countdown_start = None
                    idle_from = None
                    continue
                with _LOCK:
                    _PLAN["mode"] = ""
                break
        else:
            countdown_start = None                    # 还没到关机条件，清倒计时


def _do_shutdown():
    _notify("正在关机", "系统在数秒后关机", "warn")
    try:
        if sys.platform.startswith("win"):
            # /s 关机 /t 5 五秒后（倒计时已过，这是最后一道）/c 说明
            subprocess.run(["shutdown", "/s", "/t", "5",
                            "/c", "Sweet 采集：任务完成，按计划自动关机"],
                           check=False, timeout=15)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            'tell app "System Events" to shut down'],
                           check=False, timeout=15)
        else:
            subprocess.run(["shutdown", "-h", "+0",
                            "Sweet 采集：任务完成，自动关机"],
                           check=False, timeout=15)
    except Exception as e:
        _notify("自动关机失败", f"{type(e).__name__}: {e}", "error")


def _notify(title, body, level):
    """落一条通知中心的记录（拿得到库就写，拿不到就算了，绝不因此崩）。"""
    try:
        from . import notify as _n
        db = _MON.get("db")
        if db is not None:
            _n.add(db, "auto-shutdown", title, str(body)[:200],
                   link="/settings?tab=ui", level=level)
    except Exception:
        pass


def set_db(db):
    _MON["db"] = db


def apply_cfg(cfg, queue=None) -> dict:
    """设置保存后的统一执行：把内存计划对齐到配置里的每日定点。

    配置里只有每日定点会落盘（见 webui._api_powerdown 的持久化口径），
    所以这里只管两态：配置说"每日 HH:MM"就安排/改时刻，说"off"就取消。
    on_idle 与一次性定点是会话内存计划，不受本函数影响——用户改别的
    设置项时也不该被误清掉，故仅当改动涉及 auto_shutdown_* 键才调用。
    """
    mode = str(getattr(cfg, "auto_shutdown_mode", "") or "")
    hhmm = str(getattr(cfg, "auto_shutdown_at", "") or "")
    daily = str(getattr(cfg, "auto_shutdown_daily", "0")) == "1"
    if mode == "at_time" and daily and hhmm:
        return schedule("at_time",
                        countdown=int(getattr(cfg, "auto_shutdown_countdown", 60) or 60),
                        at_hhmm=hhmm, daily=True, queue=queue)
    if not mode or mode == "off":
        st = status()
        if st.get("mode") == "at_time" and bool(st.get("daily")):
            return cancel()      # 每日计划被关掉：清内存计划
        return {"ok": True, **st}
    return {"ok": True, **status()}


def resume_from_cfg(cfg, queue=None) -> dict:
    """开机时按配置恢复关机计划 —— **只恢复每日定点**。

    on_idle / 一次性定点都不复活：机器都重启过了，说明用户开了机，
    这时候把"跑完就关"复活，等于空闲三分钟立刻把刚开机的电脑又关掉，
    是事故不是功能。每日计划是用户明确"每天如此"的承诺，才续。
    """
    mode = str(getattr(cfg, "auto_shutdown_mode", "") or "")
    if mode == "at_time" and str(getattr(cfg, "auto_shutdown_daily", "0")) == "1" \
            and str(getattr(cfg, "auto_shutdown_at", "") or ""):
        return schedule("at_time",
                        countdown=int(getattr(cfg, "auto_shutdown_countdown", 60)),
                        at_hhmm=str(getattr(cfg, "auto_shutdown_at", "")),
                        daily=True, queue=queue)
    return {"ok": True, "resumed": False}
