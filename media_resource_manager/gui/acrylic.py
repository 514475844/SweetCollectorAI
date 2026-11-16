# -*- coding: utf-8 -*-
"""Windows 系统级透明背板（毛玻璃）模块 —— V0.0.19 现代化视觉子任务。

纪律（最高优先级）：
  1. 本模块所有对外函数在任何失败路径下静默降级，返回 ok=False 与简体中文
     原因，绝不抛出异常、绝不影响软件启动与窗口正常显示；
  2. 纯标准库实现（ctypes/os/sys），不触碰任何 Qt 对象，函数签名只收 int
     类型的窗口句柄；
  3. 每次 DwmSetWindowAttribute 调用都检查 HRESULT，非 0 时记录简体中文原因。

调用方义务（模块外配合项，接线时务必满足）：
  - 窗口必须以带 alpha 的背景色创建（Qt 侧对顶层窗口设置半透明背景色，推荐
     搭配 theme 的半透明 QSS；仅靠本模块的 DWM 属性无法让 Qt 已绘制为
     不透明的背景变透明）；
  - 主窗口/对话框在 show() 之后取 int(windowHandle()) 再调用 apply_glass；
  - 关闭毛玻璃（配置 glass_enabled=False 或运行时降级）时调用 remove_glass。

平台策略：
  - Win11（build >= 22000）：DWMWA_SYSTEMBACKDROP_TYPE(38)=3（Acrylic），
     失败退化 2（Mica）；Mica 是系统自带动效，无需额外前置属性。
     DWMWA_MICA_EFFECT(1029) 在正式发布 SDK 中已废弃，调用它返回错误属预期，
     本模块不调用该值。明暗属性 DWMWA_USE_IMMERSIVE_DARK_MODE(20) 与背板
     无关，本模块绝不触碰；同样绝不触碰窗口捕获排除类属性。
  - Win10 1903+（build >= 18362）：系统无真模糊（blur）能力，采用近似方案：
     圆角属性(33) 设为不圆角 + DwmExtendFrameIntoClientArea 全无边距扩展帧，
     透明度由调用方的半透明背景色呈现；
  - 更低版本：不支持，is_supported 返回 ok=False。
"""
from __future__ import annotations

import ctypes
import os
import sys

# ---- DWM 属性常量（数值即 DWMWINDOWATTRIBUTE 枚举原值） ----
DWMWA_USE_IMMERSIVE_DARK_MODE = 20  # 明暗属性，本模块绝不设置
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_SYSTEMBACKDROP_TYPE = 38

DWMWCP_DONOTROUND = 1
DWMWCP_DEFAULT = 0

DWM_BACKDROP_DISABLED = 1
DWM_BACKDROP_MICA = 2
DWM_BACKDROP_ACRYLIC = 3

WIN11_MIN_BUILD = 22000
WIN10_MIN_BUILD = 18362  # 1903

VALID_LEVELS = ("auto", "acrylic", "mica", "transparent")

# 对外暴露的档位字符串
LEVEL_ACRYLIC = "acrylic"
LEVEL_MICA = "mica"
LEVEL_TRANSPARENT = "transparent"


def _os_build() -> int:
    """返回 Windows 内部版本号；非 Windows 或取值失败返回 0。"""
    if sys.platform != "win32":
        return 0
    try:
        return int(sys.getwindowsversion().build)
    except Exception:
        return 0


def _dwm_set_attr(hwnd: int, attr: int, value: int) -> tuple[int, str]:
    """调用 DwmSetWindowAttribute 设置一个 int 属性，返回 (HRESULT, 中文原因)。"""
    try:
        dwmapi = ctypes.windll.dwmapi
    except Exception as exc:
        return -1, "无法加载系统 dwmapi 组件：%s" % exc
    try:
        c_val = ctypes.c_int(value)
        hr = dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_int(attr),
            ctypes.byref(c_val),
            ctypes.c_int(ctypes.sizeof(c_val)),
        )
        hr = int(hr) & 0xFFFFFFFF
        if hr == 0:
            return 0, ""
        return hr, "系统属性设置未生效（HRESULT 0x%08X，属性号 %d）" % (hr, attr)
    except Exception as exc:
        return -1, "系统属性调用异常：%s" % exc


def _dwm_extend_frame(hwnd: int, enable: bool) -> tuple[int, str]:
    """Win10 近似：全无边距扩展帧（玻璃边界）。

    该 API 返回 void，调用本身不会失败；效果依赖调用方背景 alpha。
    """
    try:
        dwmapi = ctypes.windll.dwmapi

        class _MARGINS(ctypes.Structure):
            _fields_ = [
                ("lLeftWidth", ctypes.c_int),
                ("lRightWidth", ctypes.c_int),
                ("lTopWidth", ctypes.c_int),
                ("lBottomWidth", ctypes.c_int),
            ]

        m = _MARGINS(-1, -1, -1, -1) if enable else _MARGINS(0, 0, 0, 0)
        dwmapi.DwmExtendFrameIntoClientArea(
            ctypes.c_void_p(hwnd), ctypes.byref(m)
        )
        return 0, ""
    except Exception as exc:
        return -1, "扩展帧调用异常：%s" % exc


def _valid_hwnd(hwnd) -> bool:
    """句柄是否为真实存在的窗口。"""
    try:
        h = int(hwnd)
    except (TypeError, ValueError):
        return False
    if h <= 0:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(h)))
    except Exception:
        return False


def is_supported() -> dict:
    """探测本机毛玻璃能力。

    返回 {'ok': bool, 'os_build': int, 'level': 'acrylic'|'mica'|
    'transparent'|'', 'reason': 简体中文说明}。
    level 为本机可应用的最高档位；不支持时 level 为空串。
    """
    result = {"ok": False, "os_build": 0, "level": "", "reason": ""}
    try:
        build = _os_build()
        result["os_build"] = build
        if sys.platform != "win32":
            result["reason"] = "当前操作系统不是 Windows，透明背板能力不可用"
            return result
        if build == 0:
            result["reason"] = "无法读取 Windows 内部版本号，按不支持处理"
            return result
        if build >= WIN11_MIN_BUILD:
            # Win11：Acrylic 与 Mica 均可用，报告最高档 acrylic
            result["ok"] = True
            result["level"] = LEVEL_ACRYLIC
            result["reason"] = (
                "Windows 11（内部版本号 %d），支持 Acrylic 背板，"
                "失败时自动退化为 Mica" % build
            )
            return result
        if build >= WIN10_MIN_BUILD:
            result["ok"] = True
            result["level"] = LEVEL_TRANSPARENT
            result["reason"] = (
                "Windows 10（内部版本号 %d），无系统级模糊能力，"
                "采用无边距扩展帧加半透明背景色近似" % build
            )
            return result
        result["reason"] = (
            "Windows 内部版本号 %d 低于 1903，不满足透明背板最低要求" % build
        )
        return result
    except Exception as exc:
        result["reason"] = "能力探测过程异常，按不支持处理：%s" % exc
        return result


def apply_glass(hwnd: int, level: str = "auto") -> dict:
    """对给定窗口句柄启用毛玻璃。hwnd 传 int(windowHandle())。

    返回 {'ok': bool, 'level': 'acrylic'|'mica'|'transparent'|'',
    'error': 简体中文}。任何失败静默降级，绝不抛出异常。
    level 取值：'auto'（默认，按平台取最高可用档）/ 'acrylic' / 'mica' /
    'transparent'。
    """
    result = {"ok": False, "level": "", "error": ""}
    try:
        if not _valid_hwnd(hwnd):
            result["error"] = "窗口句柄无效或已销毁，毛玻璃未启用"
            return result
        if level not in VALID_LEVELS:
            level = "auto"
        hwnd = int(hwnd)
        build = _os_build()

        if build >= WIN11_MIN_BUILD:
            if level in ("auto", "acrylic"):
                hr, err = _dwm_set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                                        DWM_BACKDROP_ACRYLIC)
                if hr == 0:
                    result["ok"] = True
                    result["level"] = LEVEL_ACRYLIC
                    return result
                # Acrylic 失败，退化 Mica
                hr2, err2 = _dwm_set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                                          DWM_BACKDROP_MICA)
                if hr2 == 0:
                    result["ok"] = True
                    result["level"] = LEVEL_MICA
                    result["error"] = "Acrylic 背板未启用（%s），已退化为 Mica" % err
                    return result
                result["error"] = (
                    "Acrylic 与 Mica 背板均未启用（%s；%s）" % (err, err2)
                )
                return result
            if level == "mica":
                hr, err = _dwm_set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                                        DWM_BACKDROP_MICA)
                if hr == 0:
                    result["ok"] = True
                    result["level"] = LEVEL_MICA
                    return result
                result["error"] = err
                return result
            # level == 'transparent'：Win11 上也允许走 Win10 近似路径
            hr, err = _apply_win10_fallback(hwnd)
            if hr == 0:
                result["ok"] = True
                result["level"] = LEVEL_TRANSPARENT
                return result
            result["error"] = err
            return result

        if build >= WIN10_MIN_BUILD:
            hr, err = _apply_win10_fallback(hwnd)
            if hr == 0:
                result["ok"] = True
                result["level"] = LEVEL_TRANSPARENT
                return result
            result["error"] = err
            return result

        result["error"] = "当前 Windows 版本不满足毛玻璃最低要求，保持不透明显示"
        return result
    except Exception as exc:
        result["error"] = "毛玻璃启用过程发生异常，已静默降级：%s" % exc
        return result


def _apply_win10_fallback(hwnd: int) -> tuple[int, str]:
    """Win10 近似：去圆角 + 全无边距扩展帧。"""
    hr, err = _dwm_set_attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                            DWMWCP_DONOTROUND)
    if hr != 0:
        return hr, err
    return _dwm_extend_frame(hwnd, enable=True)


def remove_glass(hwnd: int) -> dict:
    """关闭指定窗口的毛玻璃，恢复常规不透明背板。

    返回 {'ok': bool, 'error': 简体中文}。任何失败静默降级，绝不抛出异常。
    """
    result = {"ok": False, "error": ""}
    try:
        if not _valid_hwnd(hwnd):
            result["error"] = "窗口句柄无效或已销毁，无需关闭毛玻璃"
            return result
        hwnd = int(hwnd)
        build = _os_build()

        if build >= WIN11_MIN_BUILD:
            hr, err = _dwm_set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE,
                                    DWM_BACKDROP_DISABLED)
            if hr != 0:
                result["error"] = err
                return result
            result["ok"] = True
            return result
        if build >= WIN10_MIN_BUILD:
            hr, err = _dwm_extend_frame(hwnd, enable=False)
            if hr != 0:
                result["error"] = err
                return result
            # 恢复系统默认圆角（0 = Default）
            _dwm_set_attr(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_DEFAULT)
            result["ok"] = True
            return result
        result["ok"] = True  # 低版本从未启用，视为成功空操作
        return result
    except Exception as exc:
        result["error"] = "毛玻璃关闭过程发生异常，已静默忽略：%s" % exc
        return result
