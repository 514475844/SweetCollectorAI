"""定时运行调度器：解析 + 到点判定逻辑（不依赖 GUI 实例化）。

直接把 MainWindow._parse_schedule 绑到一个空对象上调用，避免拉起整个 Qt 窗口。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from media_resource_manager.gui.main_window import MainWindow


def _parser():
    # 把实例方法绑到一个空对象，免得实例化整个 MainWindow（要 Qt）
    return MainWindow._parse_schedule.__get__(object.__new__(object))


def test_empty_means_no_schedule():
    kind, when, err = _parser()("")
    assert kind is None and when is None and err == "empty"


def test_daily_picks_most_recent_slot():
    """daily 解析出「今天」的 08:30（**可能是未来时刻**）。

    ⚠ R159 改判（不是放水）：这条原名「most recent slot」的断言写于
      R152-A4 **之前** —— 那时 daily 返回「最近一个 ≤now 的时刻」。
      R152-A4 特意改掉那个语义（旧实现会让下午新建的 08:30 任务一启动就
      命中昨天、到点当天再跑一次 ⇒ 双跑），改为恒返回今天那一刻，
      到没到点交给 `_check_schedule` 判。新契约由
      tests/test_r152_guards.py::test_a4_parse_schedule_daily_is_today_not_clamped
      守着（同一份断言、且不含依赖墙上时钟的那条）。

      旧的 `assert when <= now` 是**凌晨定时炸弹**：00:00–08:30 之间跑，
      今天的 08:30 还没到 ⇒ 必假红（本棒 03:40 实测命中）。
    """
    p = _parser()
    kind, when, err = p("08:30")
    assert err is None and kind == "daily"
    now = datetime.now()
    assert when.date() == now.date(), "daily 必须是今天那一刻（不能夹成昨天）"
    assert when.hour == 8 and when.minute == 30 and when.second == 0
    assert when.microsecond == 0


def test_daily_rejects_bad_hour():
    kind, when, err = _parser()("25:30")
    assert err is not None and kind is None


def test_once_parses_exact_datetime():
    p = _parser()
    kind, when, err = p("2026-10-01 08:30")
    assert err is None and kind == "once"
    assert when == datetime(2026, 10, 1, 8, 30, 0)


def test_unknown_format_rejected():
    kind, when, err = _parser()("every monday")
    assert err is not None and kind is None


def test_due_logic_daily_catchup(monkeypatch):
    """daily 08:30：今天没跑过 -> 到点触发；已跑过 -> 不触发。"""
    import media_resource_manager.gui.main_window as mw
    p = _parser()
    # 模拟"现在 = 今天 09:00"，任务 daily 08:30
    fixed_now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, *a, **k):
            return fixed_now

    monkeypatch.setattr(mw, "datetime", _FakeDT)

    kind, when, _ = p("08:30")
    assert kind == "daily" and when <= fixed_now
    # 没跑过（last=None）-> due（last 为空）
    assert mw.MainWindow._parse_dt(None) is None
    # 已跑过（last = 今天 08:31，晚于 when）-> not due
    last = fixed_now.replace(hour=8, minute=31)
    assert not (last < when)
