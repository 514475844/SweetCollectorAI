"""窗口几何「记忆 / 最大化」的防回归测试。

窗口已改回 **Windows 原生标题栏**（不再 FramelessWindowHint 无边框）。
这条演进的背景是真回归：无边框窗口调 showMaximized() 在 Windows 上不走标准
最大化路径，常常只停在 normal 矩形；一旦状态里记着 maximized=true 但正常
几何只有 920 宽（= 旧的 minimumSize 下限；用户「先把窗口拖窄 → 再最大化 →
关闭」就存这种脏记录），重新启动会先 setGeometry(920) 再 showMaximized()，
窗口就卡在 920 宽 —— 用户看到「初始化窗口比原来小一大截」。

本文件锁死两件事：
  ① 脏的 maximized 几何（满屏矩形 / 窄窗）必须被识别，不再原样还原；
  ② 窗口必须是原生标题栏、最大化走 Qt 原生接口，不许再退回无边框自绘
     （那个固有坑：无边框窗口的 showMaximized() 常常不真正满屏）；
  ③ 正常几何要原样还原，不能被修正逻辑误伤。

注：offscreen 主屏固定 800x800，比 minimumSize(1024x640) 还小，靠真实像素
区分不了「窄窗」与「默认尺寸」，所以判定逻辑单独抽成
MainWindow._is_dirty_max_geometry 来测（喂一个虚拟大屏矩形即可）。
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _big_screen():
    from PySide6.QtCore import QRect
    return QRect(0, 0, 1920, 1080)


def _bootstrap(tmp_path, monkeypatch):
    """offscreen 环境 + 打桩 + 构造 MainWindow（沿用 tools/selfcheck.py 冒烟套路）。

    用临时 MRM_HOME，几何记忆写在临时屋里，绝不碰真实 data 目录。
    """
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)

    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted  # 弹窗不进事件循环

    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app: True
    from media_resource_manager.gui import theme
    theme.apply_theme(app, False)          # 必须先上主题，再构造主窗口
    win = mw.MainWindow()
    return app, win


def _tmp_placeholder():
    """纯逻辑用例占位（MainWindow 需要临时 HOME，这里给一个空目录）。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="mrm_geo_")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------- 脏几何判定（纯逻辑）

def test_dirty_geometry_detects_narrow_window(monkeypatch):
    """核心回归：最大化前把窗口拖到 920 宽（旧 minimumSize 下限）→ 判为脏记录。"""
    app, win = _bootstrap(_tmp_placeholder(), monkeypatch)

    assert win._is_dirty_max_geometry(920, 760, _big_screen()) is True, \
        "920 宽窄窗没被判成脏记录 → 启动时会被原样还原（窗口变窄）"
    # 正常尺寸不该被误判
    assert win._is_dirty_max_geometry(1400, 820, _big_screen()) is False
    assert win._is_dirty_max_geometry(1280, 760, _big_screen()) is False


def test_dirty_geometry_detects_fullscreen_rect(monkeypatch):
    """旧版误存的「满屏矩形」也要判脏（否则还原后仍满屏、点还原回不去）。"""
    app, win = _bootstrap(_tmp_placeholder(), monkeypatch)

    assert win._is_dirty_max_geometry(1900, 1060, _big_screen()) is True


def test_dirty_geometry_skips_when_screen_too_small(monkeypatch):
    """屏幕比最小尺寸还小时不能误伤正常几何（小屏 / offscreen 场景）。"""
    app, win = _bootstrap(_tmp_placeholder(), monkeypatch)

    from PySide6.QtCore import QRect
    small = QRect(0, 0, 1000, 1000)       # 比最小宽度（1024）还窄
    assert small.width() <= win.minimumWidth(), "前提失效：屏不比最小宽度窄，用例无意义"
    # 且 900x900 也不该是满屏矩形（< 1000*0.95），排除 full 分支干扰
    assert win._is_dirty_max_geometry(900, 900, small) is False, \
        "小屏上正常几何被误判成脏记录了"


# --------------------------------------------------------- 原生标题栏（防回退）

def test_window_is_native_not_frameless(tmp_path, monkeypatch):
    """防回退：必须保持 Windows 原生标题栏，不许再退回无边框。

    无边框窗口的 showMaximized() 在 Windows 上常常不真正满屏，正是
    「窗口比原来窄一截」那个回归的根源。
    """
    from PySide6.QtCore import Qt
    app, win = _bootstrap(tmp_path, monkeypatch)

    flags = win.windowFlags()
    assert not (flags & Qt.WindowType.FramelessWindowHint), \
        "窗口又退回无边框了：最大化会失效、缩放也得自己搓"
    assert flags & Qt.WindowType.Window, "窗口类型丢了"


def test_toggle_maximize_uses_native_api(tmp_path, monkeypatch):
    """最大化/还原走 Qt 原生接口（用 spy 断言，与 offscreen 屏幕尺寸无关）。"""
    app, win = _bootstrap(tmp_path, monkeypatch)
    called = []
    monkeypatch.setattr(win, "isMaximized", lambda: bool(called and called[-1]))
    monkeypatch.setattr(win, "showMaximized", lambda: called.append(True))
    monkeypatch.setattr(win, "showNormal", lambda: called.append(False))

    win.toggle_maximize()
    assert called == [True], f"最大化没走原生 showMaximized：{called}"
    win.toggle_maximize()
    assert called == [True, False], f"还原没走原生 showNormal：{called}"


def test_restore_does_not_use_borderless_accounting(tmp_path, monkeypatch):
    """还原 maximized 状态不再需要「无边框模拟最大化」那套记账。"""
    app, win = _bootstrap(tmp_path, monkeypatch)
    win._ui_state_file().write_text(
        json.dumps({"maximized": True, "geometry": [638, 388, 920, 760]}),
        encoding="utf-8")

    win._restore_ui_state()   # 不得抛错

    assert not hasattr(win, "_borderless_maximized"), \
        "又冒出「无边框模拟最大化」的记账字段了"


def test_minimum_width_not_low(tmp_path, monkeypatch):
    """最小宽度兜底：不能再放到 920 那种窄窗，否则又会产生上面的脏记录。"""
    app, win = _bootstrap(tmp_path, monkeypatch)

    assert win.minimumWidth() >= 1024, \
        f"最小宽度 {win.minimumWidth()} 太低，窗口很容易被拖成窄窗"
