"""#22 下载限速 / 允许时段：解析与门控逻辑（默认关闭 = 行为不变）。"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from media_resource_manager.engine import Engine
from media_resource_manager.models import TaskConfig


def _engine() -> Engine:
    # 只测纯逻辑，不需要真 db/fetcher
    return Engine(cfg=None, db=None)


def _task(hours: str = "", rate: int = 0) -> TaskConfig:
    return TaskConfig(id=None, name="t", adapter_name="",
                      allowed_hours=hours, rate_limit=rate)


def test_empty_hours_means_always_allowed():
    e = _engine()
    assert e._in_allowed_hours(_task()) is True


def test_start_equals_end_means_all_day():
    e = _engine()
    assert e._in_allowed_hours(_task(hours="0-0")) is True
    assert e._in_allowed_hours(_task(hours="8-8")) is True


def test_malformed_hours_never_blocks():
    """填错格式就当不限，绝不能把任务卡死。"""
    e = _engine()
    for bad in ("abc", "25-30", "8", "", "  "):
        assert e._in_allowed_hours(_task(hours=bad)) is True


def test_hours_window_bounds(monkeypatch):
    import media_resource_manager.engine as eng

    class _FakeDT(datetime):
        @classmethod
        def now(cls, *a, **k):
            return _fixed

    e = _engine()
    # 白天 12 点
    _fixed = datetime.now().replace(hour=12, minute=0)
    monkeypatch.setattr(eng, "datetime", _FakeDT)
    # 22-6 跨午夜：12 点不在范围内
    assert e._in_allowed_hours(_task(hours="22-6")) is False
    # 8-18：12 点在范围内
    assert e._in_allowed_hours(_task(hours="8-18")) is True


def test_hours_window_wraps_midnight(monkeypatch):
    import media_resource_manager.engine as eng

    _fixed = datetime.now().replace(hour=23, minute=0)

    class _FakeDT(datetime):
        @classmethod
        def now(cls, *a, **k):
            return _fixed

    monkeypatch.setattr(eng, "datetime", _FakeDT)
    e = _engine()
    assert e._in_allowed_hours(_task(hours="22-6")) is True
    assert e._in_allowed_hours(_task(hours="8-18")) is False


def test_rate_limit_zero_is_noop():
    """rate_limit=0 时不引入任何等待（默认行为不变）。"""
    e = _engine()

    async def go():
        import time
        t0 = time.monotonic()
        for _ in range(20):
            await e._rate_wait(_task(rate=0))
        return time.monotonic() - t0

    assert asyncio.run(go()) < 0.5


def test_rate_limit_spaces_downloads():
    """限速 60 张/分 => 每次间隔 1 秒，20 次至少 ~19 秒。

    这里用 600 张/分（间隔 0.1s）做快速验证，避免测试太慢。
    """
    e = _engine()

    async def go():
        import time
        t0 = time.monotonic()
        for _ in range(5):
            await e._rate_wait(_task(rate=600))   # 0.1s 间隔
        return time.monotonic() - t0

    el = asyncio.run(go())
    assert el >= 0.35, f"限速没生效，耗时 {el:.3f}s"
