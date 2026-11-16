"""系统电源管理（编号 6）：采集/下载期间阻止系统休眠。

为什么需要：夜里跑几小时的长任务，Windows 默认会在空闲后睡眠 → 网络断开、
任务卡在半路，用户第二天看到"跑了一夜 0 张"。这不是 bug 而是系统策略，
程序只能显式申请「我这会儿需要醒着」。

实现：`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`
  * `ES_CONTINUOUS` = 持续生效直到显式清除（而不是一次性）；
  * 只申请「系统保持唤醒」，**不申请屏幕常亮**（`ES_DISPLAY_REQUIRED`）——
    显示器该黑屏就黑屏，用户不需要看着它。
  * 非 Windows / 调用失败一律当"不支持"，**绝不抛异常**（电源管理是加分项，
    不能因为它让任务起不来）。

用法：
    with prevent_sleep(cfg):
        ... 跑任务 ...
或
    awake = SleepBlocker(); awake.start(); ...; awake.stop()
支持嵌套计数（多个任务同时跑时，最后一个结束时才真正放行）。
"""
from __future__ import annotations

import sys
import threading
from typing import Optional

#: ES_CONTINUOUS(0x80000000) | ES_SYSTEM_REQUIRED(0x00000001)
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_lock = threading.Lock()
_depth = 0
_ok: Optional[bool] = None      # None = 还没试过；True/False = 试过且结果
_last_error = ""


def _set_state(flags: int) -> bool:
    global _last_error
    if not sys.platform.startswith("win"):
        _last_error = "非 Windows 系统，跳过"
        return False
    try:
        import ctypes
        # restype 必须按 Win32 声明（默认 int 在 64 位上会被截断，返回值无意义）
        fn = ctypes.windll.kernel32.SetThreadExecutionState
        fn.argtypes = [ctypes.c_uint]
        fn.restype = ctypes.c_uint
        r = fn(flags)
        if not r:
            _last_error = "系统拒绝了电源请求（SetThreadExecutionState 返回 0）"
            return False
        return True
    except Exception as ex:                    # noqa: BLE001
        _last_error = f"{type(ex).__name__}: {ex}"
        return False


class SleepBlocker:
    """可重入的「别睡」申请。线程安全，可多任务共享一个实例。"""

    def __init__(self) -> None:
        self._held = False

    def start(self) -> bool:
        """申请保持唤醒。返回是否真的生效（False = 系统不支持，调用方可忽略）。"""
        global _depth, _ok
        with _lock:
            if self._held:
                return True
            if _depth == 0:
                _ok = _set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
            _depth += 1
            self._held = True
            return bool(_ok)

    def stop(self) -> None:
        """撤销申请（计数归零时才真的放行）。"""
        global _depth
        with _lock:
            if not self._held:
                return
            self._held = False
            _depth = max(0, _depth - 1)
            if _depth == 0:
                # 传 0 = 清除本线程此前的所有执行状态请求
                _set_state(_ES_CONTINUOUS)

    @property
    def active(self) -> bool:
        return self._held

    @classmethod
    def status_text(cls) -> str:
        """给界面/日志用的一句话（中文）。"""
        global _ok, _last_error, _depth
        if _ok is True:
            return (f"已阻止系统休眠（{_depth} 个任务持有）" if _depth
                    else "未阻止休眠（当前无任务）")
        if _ok is False:
            return f"无法阻止休眠：{_last_error or '系统不支持'}"
        return "未尝试（还没跑过任务）"


def prevent_sleep(cfg=None) -> SleepBlocker:
    """按配置返回一个 blocker：`cfg.prevent_sleep` 关掉时返回空壳（start 恒 False）。"""
    b = SleepBlocker()
    enabled = True
    try:
        enabled = bool(getattr(cfg, "prevent_sleep", True)) if cfg is not None else True
    except Exception:
        enabled = True
    if not enabled:
        _disable(b)
    return b


def _disable(b: SleepBlocker) -> None:
    b.start = lambda: False        # type: ignore[assignment]
    b.stop = lambda: None          # type: ignore[assignment]
