"""R154-F19 快捷键自定义护栏（2026-10-22 R209 棒）。

订单原文（R154 第二组 F19）：
  「设置页"快捷键"表：列出主要动作当前键位 → 点击改键 → 冲突中文提示；
    至少覆盖审核流（喜欢/不喜欢/跳过/翻页）、灯箱、任务启停；
    持久化 cfg，恢复默认按钮。」

判据写法（沿用 R73/R79/R84 钉死的三条）：
  ① **真调**被护对象：纯逻辑真函数、灯箱/审核页真建控件发合成键事件、
     设置框真建 QDialog 驱动（录键按钮直接喂 QKeyEvent）、落盘真走
     AppConfig round-trip；
  ② **默认键位必须与改前硬编码逐字节一致**（升级不许悄悄改行为）——
     ACTIONS 注册表的默认值单独有断言；
  ③ 变异自检见 `.workbuddy/_r209_mutate.py`（每处变异必须至少红一条）。

⚠ 判据不全文搜字面量：涉及源码的断言一律先剥注释行（本仓注释大量
  quoting，直接搜会误伤/被骗，R73 家族教训）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QDialog, QListWidgetItem, QMessageBox

from media_resource_manager import hotkeys as hk
from media_resource_manager.config import AppConfig
from media_resource_manager.gui.hotkeys_qt import (
    event_matches, qt_sequence, seq_from_event,
)

_ROOT = Path(__file__).resolve().parents[1]
_MW = _ROOT / "media_resource_manager" / "gui" / "main_window.py"
_LB = _ROOT / "media_resource_manager" / "gui" / "lightbox.py"
_RT = _ROOT / "media_resource_manager" / "gui" / "review_tab.py"


# ---------------------------------------------------------------- 工具
def _ev(key, mods=Qt.KeyboardModifier.NoModifier):
    return QKeyEvent(QKeyEvent.Type.KeyPress, int(key), mods)


def _code(path: Path) -> str:
    """剥掉注释行的源码（判据锚点，防注释骗判据 —— R73 家族）。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


@pytest.fixture(autouse=True)
def qapp(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    return app


@pytest.fixture
def rows():
    return [{"path": "a.jpg", "id": 1}, {"path": "b.jpg", "id": 2},
            {"path": "c.jpg", "id": 3}]


# ================================================== 一、序列解析（纯逻辑）
def test_parse_sequence_basic_and_modifiers():
    assert hk.parse_sequence("→") == (frozenset(), "→")
    assert hk.parse_sequence("ctrl+z") == (frozenset({"Ctrl"}), "Z")
    assert hk.parse_sequence("Ctrl+Alt+Shift+F5") == (
        frozenset({"Ctrl", "Alt", "Shift"}), "F5")
    assert hk.parse_sequence("Meta+A") == (frozenset({"Meta"}), "A")


def test_parse_sequence_all_arrow_glyphs_roundtrip():
    """四个箭头字形必须逐字自映射（写错一个符号整套箭头静默失灵——
    开发中真踩到过：别名表把 ↑ 误写成 ↓）。"""
    for a in ("↑", "↓", "←", "→"):
        assert hk.parse_sequence(a) == (frozenset(), a), a
        assert hk.normalize_sequence(a) == a, a


def test_parse_sequence_plus_key_is_not_a_separator():
    """「+」既是分隔符又是合法键位（灯箱放大默认键）—— 必须认。"""
    assert hk.parse_sequence("+") == (frozenset(), "+")
    assert hk.parse_sequence("Ctrl++") == (frozenset({"Ctrl"}), "+")
    assert hk.normalize_sequence("ctrl++") == "Ctrl++"
    for bad in ("++", "Ctrl+++", "Ctrl+"):
        assert hk.parse_sequence(bad) is None, bad


def test_parse_sequence_rejects_garbage():
    for bad in ("", "   ", "Ctrl", "Shift", "A+B", "Ctrl+Ctrl+Z", "F13",
                "F0", "abc", None, 42, "Ctrl+RightCtrl", "\u2192\u2193"):
        assert hk.parse_sequence(bad) is None, repr(bad)


def test_normalize_and_display():
    assert hk.normalize_sequence("shift+f5") == "Shift+F5"
    assert hk.normalize_sequence("CONTROL+right") == "Ctrl+→"
    assert hk.normalize_sequence("nope") is None
    assert hk.display("Space") == "空格"
    assert hk.display("RightCtrl") == "右 Ctrl"
    assert hk.display("Ctrl+Z") == "Ctrl+Z"
    assert hk.display("Ctrl++") == "Ctrl++"


# ================================================== 二、注册表与默认键位
def test_registry_shape_and_contexts():
    ids = [a["id"] for a in hk.ACTIONS]
    assert len(ids) == len(set(ids)), "动作 id 不许重复"
    for a in hk.ACTIONS:
        assert a["context"] in hk.CONTEXTS, a
        assert hk.parse_sequence(a["default"]) is not None, a
    assert hk.CONTEXTS == ("审核页", "审核灯箱", "图库灯箱", "灯箱（通用）", "任务")


def test_defaults_cover_order_required_contexts():
    """订单点名「至少覆盖审核流（喜欢/不喜欢/跳过/翻页）、灯箱、任务启停」。"""
    have = {a["id"] for a in hk.ACTIONS}
    for need in ("review.like", "review.dislike", "review.skip",
                 "review.up", "review.down",
                 "rlight.like", "rlight.prev", "rlight.next",
                 "glight.prev", "glight.next",
                 "light.zoom_in", "light.zoom_out", "light.zoom_reset",
                 "light.fullscreen", "light.rotate", "light.slideshow",
                 "task.start", "task.stop"):
        assert need in have, f"缺动作 {need}"


def test_default_keys_match_pre_f19_hardcoded_behavior():
    """升级铁律：默认键位必须与 F19 之前硬编码的键位逐字节一致。

    这不是快照式断言——这是**产品契约**（改默认值 = 老用户行为突变），
    和 rateplan.DEFAULT_PLAN 一个性质。
    """
    expect = {
        "review.up": "↑", "review.down": "↓", "review.like": "→",
        "review.dislike": "←", "review.reason": "E", "review.skip": "RightCtrl",
        "rlight.like": "→", "rlight.prev": "←", "rlight.next": "↓",
        "glight.prev": "←", "glight.next": "→",
        "light.zoom_in": "+", "light.zoom_out": "-", "light.zoom_reset": "0",
        "light.fullscreen": "F", "light.rotate": "R", "light.slideshow": "Space",
        "task.start": "F5", "task.stop": "F6",
    }
    assert hk.DEFAULT_HOTKEYS == expect


# ================================================== 三、覆盖解析与落盘
def test_resolve_override_invalid_and_unknown():
    assert hk.resolve({}, "review.like") == "→"
    assert hk.resolve({"review.like": "l"}, "review.like") == "L"
    assert hk.resolve({"review.like": "Ctrl"}, "review.like") == "→", \
        "非法覆盖回退默认（配错不许让动作消失）"
    assert hk.resolve({"review.like": 123}, "review.like") == "→"
    assert hk.resolve(None, "review.like") == "→"
    assert hk.resolve({}, "no.such.action") == ""


def test_effective_and_overrides_only():
    eff = hk.effective({})
    assert len(eff) == len(hk.ACTIONS)
    assert hk.overrides_only({}) == {}, "全默认 ⇒ 空覆盖 = 恢复默认"
    assert hk.overrides_only({"review.like": "L", "review.dislike": "←"}) == {
        "review.like": "L"}, "与默认相同的条目不该进覆盖"


def test_sanitize_overrides_drops_garbage():
    out = hk.sanitize_overrides(
        {"review.like": "l", "bogus.action": "X", "review.up": "Ctrl",
         "review.down": 7})
    assert out == {"review.like": "L"}


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    cfg = AppConfig()
    assert cfg.hotkeys == {}
    cfg.hotkeys = hk.sanitize_overrides({"review.like": "L"})
    back = AppConfig.from_dict(cfg.to_dict())
    assert back.hotkeys == {"review.like": "L"}
    assert hk.resolve(back.hotkeys, "review.like") == "L"


# ================================================== 四、冲突检测
def test_no_false_conflicts_on_defaults():
    assert hk.find_conflicts({}) == []
    assert hk.alias_clashes({}) == []
    assert hk.warnings_for({}) == {}


def test_conflict_within_context_only():
    """同上下文同键才算冲突；跨上下文同键是误报（永不同时在场）。"""
    w = hk.warnings_for({"review.like": "↑"})
    assert set(w) == {"review.like", "review.up"}
    assert "挡住" in w["review.like"], "文案必须说清「排前面的生效」"
    # 图库灯箱的 → 与审核页的 → 同键 —— 不同上下文，不许报
    assert hk.warnings_for({"glight.next": "→"}) == {}


def test_alias_clash_scoped_to_lightbox():
    """固定别名只存在于灯箱：审核页绑 ↑↓ 不是别名冲突（网格 ↑↓ 是
    可自定义动作，与灯箱别名不同时在场）——曾因漏这层把默认键全误报。"""
    assert hk.alias_clashes({"review.up": "↑"}) == []
    hits = hk.alias_clashes({"glight.next": "P"})
    assert [h["aids"] for h in hits] == [["glight.next"]]
    assert "固定别名" in hk.warnings_for({"glight.next": "P"})["glight.next"]


def test_alias_owner_not_warned():
    """把某个动作绑到它自己「归属」的别名键上不算冲突。"""
    assert hk.warnings_for({"light.slideshow": "P"}) == {}
    assert hk.warnings_for({"rlight.prev": "↑"}) == {}


# ================================================== 五、Qt 桥接
def test_event_matches_single_key_ignores_modifiers():
    """单键不看修饰键：按 + 天然带 Shift，若因此失灵就是缺陷。"""
    assert event_matches(_ev(Qt.Key.Key_Plus, Qt.KeyboardModifier.ShiftModifier), "+")
    assert event_matches(_ev(Qt.Key.Key_E, Qt.KeyboardModifier.ControlModifier), "E")
    assert event_matches(_ev(Qt.Key.Key_F5), "F5")
    assert not event_matches(_ev(Qt.Key.Key_F6), "F5")


def test_event_matches_combo_requires_exact():
    ctrl = Qt.KeyboardModifier.ControlModifier
    assert event_matches(_ev(Qt.Key.Key_Z, ctrl), "Ctrl+Z")
    assert not event_matches(_ev(Qt.Key.Key_Z), "Ctrl+Z")
    assert not event_matches(
        _ev(Qt.Key.Key_Z, ctrl | Qt.KeyboardModifier.AltModifier), "Ctrl+Z")


def test_event_matches_arrows_and_right_ctrl():
    for key, seq in ((Qt.Key.Key_Up, "↑"), (Qt.Key.Key_Down, "↓"),
                     (Qt.Key.Key_Left, "←"), (Qt.Key.Key_Right, "→")):
        assert event_matches(_ev(key), seq)
    # 合成事件 nativeVirtualKey=0 ⇒ Windows 语义下不算右 Ctrl（与改前代码同口径）
    assert not event_matches(
        _ev(Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier), "RightCtrl")


def test_seq_from_event_capture_rules():
    assert seq_from_event(_ev(Qt.Key.Key_E)) == "E"
    assert seq_from_event(
        _ev(Qt.Key.Key_E, Qt.KeyboardModifier.ShiftModifier)) == "E", \
        "没有 Ctrl/Alt 时丢掉 Shift（Shift+E 就是 E）"
    assert seq_from_event(_ev(Qt.Key.Key_Plus, Qt.KeyboardModifier.ShiftModifier)) == "+"
    assert seq_from_event(
        _ev(Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.ShiftModifier)) == "Ctrl+Shift+Z"
    assert seq_from_event(
        _ev(Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)) is None, \
        "只按修饰键不算一次录键"


def test_qt_sequence_for_task_shortcuts():
    assert qt_sequence("F5").toString() == "F5"
    assert qt_sequence("Ctrl+Z").toString() == "Ctrl+Z"
    assert qt_sequence("→") is not None
    assert qt_sequence("RightCtrl") is None, "QKeySequence 表达不了右 Ctrl"
    assert qt_sequence("垃圾") is None


def test_qt_sequence_unrepresentable_returns_none():
    """qt_sequence 尾部空串守卫（M11）：parse 放行、但 QKeySequence
    表达不了的串必须给 None，不许把 toString 为空的序列交给 QShortcut。

    洞样本 'Plus'/'Ctrl+Plus' 探针实证（_r209b_probe_qseq.py）：parse_sequence
    放行、QKeySequence(...).toString() 为空串 —— 守卫砍掉后这两条会漏过去。
    前两条断言是版本行为锚点：Qt 某天修了这个洞，会先在这里响而不是误报。
    """
    from PySide6.QtGui import QKeySequence
    assert QKeySequence("Plus").toString() == ""
    assert QKeySequence("Ctrl+Plus").toString() == ""
    assert qt_sequence("Plus") is None
    assert qt_sequence("Ctrl+Plus") is None


# ================================================== 六、灯箱真驱动
def test_lightbox_default_gallery_navigation(rows):
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(rows, 0, None)
    assert lb.i == 0
    lb.keyPressEvent(_ev(Qt.Key.Key_Right))
    assert lb.i == 1
    lb.keyPressEvent(_ev(Qt.Key.Key_Left))
    assert lb.i == 0
    lb.keyPressEvent(_ev(Qt.Key.Key_Up))      # 固定别名
    assert lb.i == 2, "↑ 是灯箱固定别名，恒翻上一张（环绕）"
    lb.keyPressEvent(_ev(Qt.Key.Key_Down))
    assert lb.i == 0
    # 缩放/全屏/旋转/幻灯片键不抛且不翻页（只断言不崩 + 光标不动）
    for k in (Qt.Key.Key_Plus, Qt.Key.Key_Minus, Qt.Key.Key_0,
              Qt.Key.Key_F, Qt.Key.Key_R, Qt.Key.Key_Space, Qt.Key.Key_P):
        lb.keyPressEvent(_ev(k))
        assert lb.i == 0, f"键 {k} 不该翻页"


def test_lightbox_default_review_context(rows):
    from media_resource_manager.gui.lightbox import Lightbox
    decided = []
    lb = Lightbox(rows, 0, None,
                  on_decide=lambda d, r: decided.append((d, r.get("id"))))
    lb.keyPressEvent(_ev(Qt.Key.Key_Right))       # → 喜欢 + 自动下一张
    assert decided and decided[0][0].value == "like"
    assert lb.i == 1
    lb.keyPressEvent(_ev(Qt.Key.Key_Left))        # ← 只翻页（R153 起不判不喜欢）
    assert len(decided) == 1 and lb.i == 0
    lb.keyPressEvent(_ev(Qt.Key.Key_Down))
    assert lb.i == 1
    lb.keyPressEvent(_ev(Qt.Key.Key_Up))          # 固定别名
    assert lb.i == 0


def test_lightbox_custom_keys_take_effect(rows):
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(rows, 0, None, hotkeys={"glight.next": "N",
                                          "light.zoom_in": "Z"})
    lb.keyPressEvent(_ev(Qt.Key.Key_N))
    assert lb.i == 1
    lb.keyPressEvent(_ev(Qt.Key.Key_Right))       # 改绑后原键失效
    assert lb.i == 1
    lb.zoom_in()
    before = lb.zoom()
    lb.keyPressEvent(_ev(Qt.Key.Key_Z))           # 新键放大
    assert lb.zoom() > before


def test_lightbox_button_texts_follow_keys(rows):
    """R23 同类教训：文案必须与真实键位一致（印着 → 实际绑了 L 比没有提示更糟）。"""
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(rows, 0, None)
    assert lb.b_prev.text() == "上一张 (←)"
    assert lb.b_next.text() == "下一张 (→)"
    assert lb.b_zoom_in.text() == "放大 (+)"
    assert lb.b_rot.text() == "旋转 (R)"
    assert lb.b_slideshow.text() == "幻灯片 (空格/P)"
    # 图库灯箱提示行（无审核上下文）：四向翻页 + 空格/P 幻灯片
    assert "↑↓ 翻页" in lb._hint.text() and "空格/P" in lb._hint.text()
    lb_r = Lightbox(rows, 0, None, on_decide=lambda d, r: None)
    assert "→ 喜欢" in lb_r._hint.text() and "← 上一张" in lb_r._hint.text()
    lb2 = Lightbox(rows, 0, None, hotkeys={"glight.next": "N",
                                           "light.zoom_in": "Z",
                                           "light.slideshow": "S"})
    assert lb2.b_next.text() == "下一张 (N)"
    assert lb2.b_zoom_in.text() == "放大 (Z)"
    assert lb2.b_slideshow.text() == "幻灯片 (S/P)"
    lb3 = Lightbox(rows, 0, None, on_decide=lambda d, r: None,
                   hotkeys={"rlight.next": "J"})
    assert lb3.b_next.text() == "下一张 (J)"       # 审核灯箱下一张用 ↓ 默认 → J


# ================================================== 七、审核页真驱动
def _tab(tmp_db, tmp_config):
    from media_resource_manager.gui.review_tab import ReviewTab
    return ReviewTab(tmp_db, tmp_config.isolate_dir, lambda s: None,
                     cfg=tmp_config)


def _fill(tab, ids=(101, 102)):
    for rid in ids:
        it = QListWidgetItem(f"#{rid}")
        it.setData(Qt.ItemDataRole.UserRole, rid)
        tab.grid.addItem(it)
    tab.grid.setCurrentRow(0)


def test_review_grid_default_keys(tmp_db, tmp_config, monkeypatch):
    from unittest.mock import MagicMock
    from media_resource_manager.models import ReviewDecision
    tab = _tab(tmp_db, tmp_config)
    try:
        _fill(tab)
        spy = MagicMock()
        tab.decide = spy
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_Right))
        spy.assert_called_once_with(ReviewDecision.LIKE, keyboard=True)
        spy.reset_mock()
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_Left))
        spy.assert_called_once_with(ReviewDecision.DISLIKE, keyboard=True)
        spy.reset_mock()
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_E))
        spy.assert_called_once_with(ReviewDecision.DISLIKE, keyboard=True,
                                    ask_reason=True)
        spy.reset_mock()
        # 右 Ctrl 待定：合成事件 nativeVirtualKey=0 ⇒ Windows 语义下不算
        # 右 Ctrl（与改前代码同口径）；打桩 _right_ctrl_pressed 再验触发。
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_Control,
                                             Qt.KeyboardModifier.ControlModifier))
        assert spy.call_count == 0, "合成 Ctrl 在 Windows 语义下不算右 Ctrl"
        from media_resource_manager.gui import hotkeys_qt
        monkeypatch.setattr(hotkeys_qt, "_right_ctrl_pressed", lambda ev: True)
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_Control,
                                             Qt.KeyboardModifier.ControlModifier))
        spy.assert_called_once_with(ReviewDecision.SKIP)
        # ↑↓ 只移光标不判定
        spy.reset_mock()
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_Down))
        assert spy.call_count == 0 and tab.grid.currentRow() == 1
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_Up))
        assert spy.call_count == 0 and tab.grid.currentRow() == 0
    finally:
        tab.deleteLater()


def test_review_grid_custom_keys(tmp_db, tmp_config):
    from unittest.mock import MagicMock
    from media_resource_manager.models import ReviewDecision
    tmp_config.hotkeys = {"review.like": "L"}
    tab = _tab(tmp_db, tmp_config)
    try:
        _fill(tab)
        spy = MagicMock()
        tab.decide = spy
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_L))
        spy.assert_called_once_with(ReviewDecision.LIKE, keyboard=True)
        spy.reset_mock()
        QApplication.sendEvent(tab.grid, _ev(Qt.Key.Key_Right))   # 原键失效
        assert spy.call_count == 0
    finally:
        tab.deleteLater()


def test_review_grid_hint_follows_keys(tmp_db, tmp_config):
    tab = _tab(tmp_db, tmp_config)
    try:
        tip = tab.grid.toolTip()
        assert "→ 喜欢" in tip and "← 不喜欢" in tip and "E 说原因" in tip
        assert "右 Ctrl 待定" in tip
        tab.cfg.hotkeys = {"review.like": "L", "review.dislike": "K"}
        tab.sync_hotkey_hints()
        tip2 = tab.grid.toolTip()
        assert "L 喜欢" in tip2 and "K 不喜欢" in tip2
        assert "→ 喜欢" not in tip2, "改键后旧键位文案必须撤"
    finally:
        tab.deleteLater()


# ================================================== 八、设置框真驱动
def test_dialog_build_and_default_rows():
    from media_resource_manager.gui.hotkey_dialog import HotkeyDialog
    cfg = AppConfig()
    dlg = HotkeyDialog(None, cfg)
    assert len(dlg._buttons) == len(hk.ACTIONS)
    assert dlg._buttons["review.like"].seq() == "→"
    assert dlg.current_overrides() == {}
    assert dlg.warnings_text() == {}


def test_dialog_capture_key_by_event():
    from media_resource_manager.gui.hotkey_dialog import HotkeyDialog
    dlg = HotkeyDialog(None, AppConfig())
    btn = dlg._buttons["review.dislike"]
    btn._start_listen()
    btn.keyPressEvent(_ev(Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier))
    assert btn.seq() == "Ctrl+K" and not btn._listening
    # 只按修饰键：继续等，不退出录键态
    btn2 = dlg._buttons["review.skip"]
    btn2._start_listen()
    btn2.keyPressEvent(_ev(Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier))
    assert btn2._listening and btn2.seq() == "RightCtrl"
    # Esc 取消当次录入（保留原键位）
    btn2.keyPressEvent(_ev(Qt.Key.Key_Escape))
    assert not btn2._listening and btn2.seq() == "RightCtrl"


def test_dialog_conflict_warning_and_reset():
    from media_resource_manager.gui.hotkey_dialog import HotkeyDialog
    cfg = AppConfig()
    dlg = HotkeyDialog(None, cfg)
    dlg._buttons["review.up"].set_seq("L")
    dlg._buttons["review.like"].set_seq("L")
    w = dlg.warnings_text()
    assert set(w) == {"review.up", "review.like"}
    assert "挡住" in w["review.like"]
    assert dlg.lbl_warn.text() != "", "顶部要有汇总告警"
    dlg.reset_default()
    assert dlg.current_overrides() == {}
    assert dlg.warnings_text() == {}


def test_dialog_apply_and_roundtrip():
    from media_resource_manager.gui.hotkey_dialog import HotkeyDialog
    cfg = AppConfig()
    dlg = HotkeyDialog(None, cfg)
    dlg._buttons["task.start"].set_seq("Ctrl+R")
    dlg._buttons["review.like"].set_seq("→")      # 与默认相同 ⇒ 不进覆盖
    dlg.apply_to(cfg)
    assert cfg.hotkeys == {"task.start": "Ctrl+R"}
    back = AppConfig.from_dict(cfg.to_dict())
    assert hk.resolve(back.hotkeys, "task.start") == "Ctrl+R"
    # 恢复默认后再存 = 空字典（老用户清掉自定义的出口）
    dlg.reset_default()
    dlg.apply_to(cfg)
    assert cfg.hotkeys == {}


def test_hotkeys_default_not_shared():
    """dataclass 默认值不许用可变字面量（两个实例会共享同一个 dict）。"""
    a, b = AppConfig(), AppConfig()
    a.hotkeys["review.like"] = "L"
    assert b.hotkeys == {}, "cfg.hotkeys 默认 dict 被实例间共享了"


def test_rebind_hotkeys_dedupes_and_skips_rightctrl():
    """任务快捷键重建：同键只建排前面的；RightCtrl 建不出窗口快捷键就跳过。"""
    from PySide6.QtCore import QObject
    from media_resource_manager.gui import main_window as mw_mod

    class _Fake(QObject):
        def __init__(self, hotkeys):
            super().__init__()
            self.cfg = AppConfig()
            self.cfg.hotkeys = hotkeys
            self._hotkey_shortcuts = []
            self.log = lambda *a, **k: None
            self.run_calls = 0
            self.stop_calls = 0

        def run_selected(self):
            self.run_calls += 1

        def stop_selected(self):
            self.stop_calls += 1

    f = _Fake({"task.start": "F5", "task.stop": "F5"})     # 同键
    mw_mod.MainWindow._rebind_hotkeys(f)
    seqs = [sc.key().toString() for sc in f._hotkey_shortcuts]
    assert seqs == ["F5"], "同键只建排前面的（Qt 重复序列 ambiguous overload）"
    f._hotkey_shortcuts[0].activated.emit()
    assert f.run_calls == 1 and f.stop_calls == 0

    logs = []
    f2 = _Fake({"task.start": "RightCtrl"})
    f2.log = lambda *a, **k: logs.append(a)
    mw_mod.MainWindow._rebind_hotkeys(f2)
    assert [sc.key().toString() for sc in f2._hotkey_shortcuts] == ["F6"], \
        "RightCtrl 建不出窗口快捷键 ⇒ 跳过（不弹窗、不静默丢）"
    assert logs and "建不出窗口快捷键" in str(logs[0])


# ================================================== 九、接线源码判据
def _func_block(src: str, name: str) -> str:
    """取「def name(self…):」到下一个同缩进 def 的方法块。

    位置类判据按块切（R84 家族）：'dlg.apply_to(self.cfg)' 在 main_window
    出现两处（F24 rateplan 与 F19 hotkey），全文搜会被另一处骗到。
    """
    m = re.search(rf"^    def {re.escape(name)}\(", src, re.M)
    assert m, f"main_window 里没有方法 {name}"
    nxt = re.search(r"^    def ", src[m.end():], re.M)
    return src[m.start(): m.end() + nxt.start()] if nxt else src[m.start():]


def test_main_window_wires_hotkey_dialog():
    src = _code(_MW)
    assert "def open_hotkey_dialog" in src
    assert "def _rebind_hotkeys" in src
    assert 'QAction("快捷键…"' in src, "设置菜单里没有「快捷键…」入口"
    assert "act_hotkeys.triggered.connect(self.open_hotkey_dialog)" in src
    assert "HotkeyDialog(self, self.cfg)" in src
    block = _func_block(src, "open_hotkey_dialog")
    assert "dlg.apply_to(self.cfg)" in block, \
        "open_hotkey_dialog 保存分支没把表单写回 cfg"
    assert "save_config(self.cfg)" in block, \
        "open_hotkey_dialog 没落盘（关主窗口改动静默丢失）"
    assert "self._rebind_hotkeys()" in block
    assert "self._review_tab.sync_hotkey_hints()" in block, \
        "保存后没重拼审核页键位文案（设置写着 L、界面印着 →）"


def test_qshortcut_imported_from_qtgui():
    """QShortcut 在 PySide6 6.11 只位于 QtGui；从 QtWidgets 导会静默失效
    （F1 快捷键曾因此死掉，except 把它吞了）。两处 import 都钉死。"""
    src = _code(_MW)
    gui_imports = re.findall(r"from PySide6\.QtGui import[^\n]*", src)
    assert any("QShortcut" in line for line in gui_imports), \
        "没有一处从 QtGui 导 QShortcut"
    bad = [line for line in re.findall(r"from PySide6\.QtWidgets import[^\n]*", src)
           if "QShortcut" in line]
    assert not bad, f"还有从 QtWidgets 导 QShortcut 的死代码：{bad}"


def test_lightbox_and_review_tab_use_resolver():
    lb = _code(_LB)
    assert "event_matches(ev, self._hk(" in lb
    assert 'def _hk(self, action_id: str) -> str:' in lb
    assert 'hotkeys=None' in lb, "Lightbox 构造没留 hotkeys 入口"
    rt = _code(_RT)
    assert 'event_matches(ev, _hk(hk, "review.like"))' in rt
    assert 'event_matches(ev, _hk(hk, "review.skip"))' in rt
    assert 'hotkeys=getattr(self.cfg, "hotkeys", None)' in rt


def test_task_shortcut_defaults_are_f5_f6():
    """任务启停默认键位（新能力，订单点名覆盖）。"""
    assert hk.DEFAULT_HOTKEYS["task.start"] == "F5"
    assert hk.DEFAULT_HOTKEYS["task.stop"] == "F6"
