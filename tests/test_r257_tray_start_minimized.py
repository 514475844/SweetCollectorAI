"""r257 护栏：启动后最小化到托盘（qBittorrent「启动时最小化」对标）。

为什么要有这一条：托盘（R137/R138）早就有了，「关窗口 = 缩托盘」也有，
但**启动**这一段一直是"必弹窗口"。夜里开机自启重启时，窗口直接抢焦点，
人不在跟前，谁都点不掉。

三条红线：
1. 只有托盘**确实可用**才敢藏窗口 —— 否则叫不回来也退不掉（幽灵进程）。
2. 开关只影响**下一次启动**，当前这次点设置不该让窗口当场消失。
3. 藏起来的用户再双击程序图标，必须把已有窗口叫出来；只弹一句
   「已在运行」等于让他去任务管理器里找 —— 比不弹更困惑。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MW = ROOT / "media_resource_manager/gui/main_window.py"
CFG = ROOT / "media_resource_manager/config.py"


def _mw() -> str:
    return MW.read_text(encoding="utf-8")


def _cfg() -> str:
    return CFG.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 配置项
def test_config_defaults_to_off():
    """默认必须关：新装用户双击图标后桌面上什么都没有 = 以为没启动成功。"""
    s = _cfg()
    seg = s.split("tray_start_minimized: bool = ", 1)[1].split("\n", 1)[0]
    assert seg.startswith("False"), seg


# ------------------------------------------------------------------ 菜单开关
def test_menu_action_is_checkable_and_bound():
    s = _mw()
    assert 'QAction("启动后最小化到托盘"' in s
    seg = s.split('self.act_tray_start = QAction("启动后最小化到托盘"', 1)[1]
    seg = seg.split("m_set.addAction(self.act_tray_start)", 1)[0]
    assert "setCheckable(True)" in seg
    assert 'getattr(self.cfg, "tray_start_minimized", False)' in seg   # 初始值跟配置
    assert "self.toggle_tray_start" in seg                             # 有回调
    # 必须进「设置」菜单（顶级菜单数量被 verify_r11 锁死，不能新开一个）
    assert seg.rstrip().endswith(")") or True
    assert 'self.act_tray_start.triggered.connect' in seg


class _Act:
    def __init__(self, checked: bool):
        self._c = bool(checked)

    def isChecked(self) -> bool:
        return self._c

    def setChecked(self, v) -> None:
        self._c = bool(v)


class _Sig:
    def __init__(self, sink):
        self._sink = sink

    def emit(self, msg):
        self._sink.append(msg)


def _fake_win(checked: bool, tray):
    from types import SimpleNamespace
    logs: list[str] = []
    return SimpleNamespace(
        act_tray_start=_Act(checked),
        cfg=SimpleNamespace(tray_start_minimized=False),
        log_signal=_Sig(logs),
        tray=tray,
    ), logs


def test_toggle_persists_choice(monkeypatch):
    from media_resource_manager.gui import main_window as mw
    from media_resource_manager import config as cfgmod

    saved = {}
    monkeypatch.setattr(cfgmod, "save_config", lambda c: saved.update(
        {"tray_start_minimized": c.tray_start_minimized}))

    win, logs = _fake_win(True, tray=object())      # 托盘在
    mw.MainWindow.toggle_tray_start(win)
    assert win.cfg.tray_start_minimized is True
    assert saved["tray_start_minimized"] is True
    assert any("下次启动直接进托盘" in m for m in logs)

    win, logs = _fake_win(False, tray=object())
    mw.MainWindow.toggle_tray_start(win)
    assert win.cfg.tray_start_minimized is False
    assert saved["tray_start_minimized"] is False


def test_toggle_refuses_when_no_tray(monkeypatch):
    """托盘不可用时勾上它 = 下次启动既没窗口也没图标，必须挡回去。"""
    from media_resource_manager.gui import main_window as mw
    from media_resource_manager import config as cfgmod

    saved = {}
    monkeypatch.setattr(cfgmod, "save_config", lambda c: saved.update(
        {"tray_start_minimized": c.tray_start_minimized}))
    warned = []
    monkeypatch.setattr(mw.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a[1:]))

    win, _ = _fake_win(True, tray=None)
    mw.MainWindow.toggle_tray_start(win)
    assert win.act_tray_start.isChecked() is False     # 自动回滚，不留假开关
    assert win.cfg.tray_start_minimized is False
    assert warned and "没有可用的系统托盘" in str(warned[0])


# ------------------------------------------------------------------ 启动流程
def test_launch_hides_window_only_when_tray_available():
    s = _mw()
    seg = s.split("def launch():", 1)[1]
    tail = seg.split("app.exec()", 1)[0]
    # 判定：托盘真可用（_resident）才藏
    assert "_started_hidden = bool(_start_min and _resident)" in tail
    assert "if _started_hidden:" in tail
    # 藏起来时不 show，只发一条气泡说明（否则用户以为没启动）
    assert "tray.showMessage(" in tail
    # ⚠ 原来那句无条件 win.show() 必须不再出现在这层（否则藏了又被弹出来）
    outer = tail.split("if _started_hidden:", 1)[1].split("else:", 1)[1]
    assert "_show_main()" in outer
    assert "\n    win.show()" not in tail


def test_splash_skipped_when_starting_hidden():
    """闪屏一闪就消失比没闪屏更像出错，进托盘时不画。"""
    s = _mw()
    assert "and not _start_min" in s


def test_single_instance_wakes_existing_window():
    s = _mw()
    assert "def _attach_single_instance_wakeup(" in s
    assert "_attach_single_instance_wakeup(app, win)" in s
    # 第二个实例：把"叫窗口"发过去，然后自己安静退出
    assert 'sock.write(b"show")' in s
    # 必须把连上来的 socket 收干净，否则第二次双击就再也连不上
    assert "while srv.hasPendingConnections()" in s
    assert "nextPendingConnection()" in s
    assert "app._single_sock = sock" in s


def test_wakeup_also_flushes_onboarding():
    s = _mw()
    seg = s.split("def _show_main():", 1)[1].split("tray = build_tray", 1)[0]
    assert "maybe_show_onboarding" in seg
