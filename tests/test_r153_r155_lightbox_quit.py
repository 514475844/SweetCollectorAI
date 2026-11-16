"""R153 + R155 合并护栏（2026-10-17 下午棒）。

R153（用户原话：「把灯箱里面的『不喜欢』这个功能直接去掉吧，在网格页或者
列表页有这个功能就够了」）：
  1. 审核页灯箱 ← = 上一张翻页，**不再**触发 on_decide('dislike')；
  2. → 仍触发 like；纯浏览灯箱四向翻页不变；
  3. 审核页**网格/列表**的「不喜欢」（键盘 ← / 按钮 / 右键）保持原样；
  4. 快捷键帮助文本不再宣传「灯箱 ← 不喜欢」，且灯箱段键位描述与真实一致。

R155（用户实测反馈「最小化和关闭都是最小化到托盘，这个不对」）：
  5. 最小化（-）= 标准行为缩任务栏：MainWindow 不得再接 changeEvent 劫持；
  6. 关闭（X）仍缩托盘（closeEvent 链路不动，R138 护栏另钉）；
  7. 设置菜单退出项 = 「完全退出」，tooltip 写清与「关闭窗口缩托盘」的区别，
     仍连 request_quit；顶级菜单仍 8 个（verify_r11 同款判据）。
"""
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_MW = os.path.join(_ROOT, "media_resource_manager", "gui", "main_window.py")
_LB = os.path.join(_ROOT, "media_resource_manager", "gui", "lightbox.py")


@pytest.fixture(autouse=True)
def qapp(monkeypatch):
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    return app


def _rows(n):
    return [{"path": f"/no/such/img_{i}.webp", "id": 100 + i,
             "width": 8, "height": 6, "score": 0.5,
             "source_url": "https://example.com/x", "tags": ""}
            for i in range(n)]


def _send(widget, key):
    from PySide6.QtCore import Qt, QEvent
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    ev = QKeyEvent(QEvent.Type.KeyPress, int(key),
                   Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, ev)


LEFT = 0x01000012      # Qt.Key.Key_Left
UP = 0x01000013        # Qt.Key.Key_Up
RIGHT = 0x01000014     # Qt.Key.Key_Right
DOWN = 0x01000015      # Qt.Key.Key_Down


# ---------------------------------------------------------------- R153 灯箱
def test_r153_review_lightbox_left_pages_and_right_likes(qapp):
    """R153-1/2：审核页灯箱 ← 上一张（不判定）；→ 喜欢并下一张。"""
    from media_resource_manager.gui.lightbox import Lightbox
    from media_resource_manager.models import ReviewDecision

    seen = []
    lb = Lightbox(_rows(3), 1, None,
                  on_decide=lambda d, r: seen.append((d, r["id"])))
    try:
        _send(lb, LEFT)
        assert lb.i == 0, f"← 应回到第 0 张，实际 {lb.i}"
        assert seen == [], f"← 在审核页灯箱只翻页，实际 {seen}"
        _send(lb, RIGHT)
        assert seen == [(ReviewDecision.LIKE, 100)], seen
        assert lb.i == 1, lb.i
    finally:
        lb.close()


def test_r153_browse_lightbox_four_way_unchanged(qapp):
    """R153-2 反向：纯浏览灯箱四向翻页行为不变（不判定）。"""
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(_rows(3), 0, None)
    try:
        _send(lb, RIGHT); assert lb.i == 1      # →
        _send(lb, DOWN); assert lb.i == 2      # ↓
        _send(lb, LEFT); assert lb.i == 1      # ←
        _send(lb, UP); assert lb.i == 0      # ↑
    finally:
        lb.close()


def test_r153_review_grid_dislike_entry_unchanged(qapp, tmp_config, tmp_db):
    """R153-3：审核页网格键盘流 ← 仍判「不喜欢」（本单只动灯箱）。"""
    from unittest.mock import MagicMock
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    from media_resource_manager.gui.review_tab import ReviewTab
    from media_resource_manager.models import ReviewDecision

    tab = ReviewTab(tmp_db, tmp_config.isolate_dir, lambda s: None,
                    cfg=tmp_config)
    try:
        it = QListWidgetItem("#101")
        it.setData(Qt.ItemDataRole.UserRole, 101)
        tab.grid.addItem(it)
        tab.grid.setCurrentItem(tab.grid.item(0))
        spy = MagicMock()
        tab.decide = spy
        _send(tab.grid, LEFT)      # ←
        spy.assert_called_once_with(ReviewDecision.DISLIKE, keyboard=True)
    finally:
        tab.deleteLater()


def test_r153_help_text_no_longer_advertises_lightbox_dislike():
    """R153-4：快捷键帮助里灯箱段不再写「← 不喜欢」，键位描述与真实一致。"""
    src = open(_MW, encoding="utf-8").read()
    block = re.search(r"def show_shortcuts\(self\):(.*?)self\._shortcuts_shown",
                      src, re.S)
    assert block, "没找到 show_shortcuts"
    seg = block.group(1)
    # 灯箱段（「灯箱（」到下一个段标题「三种【列表模式】」之间）
    m = re.search(r"灯箱（.*?三种【列表模式】", seg, re.S)
    assert m, "帮助文本里找不到灯箱段"
    lb_seg = m.group(0)
    # 关键判据：灯箱段不得再把「←」宣传成「不喜欢」（键位提示须与真实一致；
    # 段内可以说明「判不喜欢请去列表页」，但不许出现 ← 与不喜欢的键位绑定）
    assert not re.search(r"←\s*不喜欢", lb_seg), (
        f"灯箱段还在宣传「← 不喜欢」：\n{lb_seg}")
    assert "上一张" in lb_seg and "喜欢" in lb_seg, (
        f"灯箱段键位描述不完整：\n{lb_seg}")
    # 审核页网格段仍保留 ← 不喜欢（网格/列表页功能未动）
    grid_seg = re.search(r"审核页（焦点在缩略图列表上）：.*?灯箱（", seg, re.S)
    assert grid_seg and "不喜欢" in grid_seg.group(0), (
        "审核页网格段的「← 不喜欢」被误删 —— 网格/列表页的入口必须保留")


# ---------------------------------------------------------------- R155 退出
def test_r155_change_event_hijack_removed():
    """R155-5：MainWindow 不得再劫持「窗口状态改变→藏托盘」。"""
    src = open(_MW, encoding="utf-8").read()
    assert "def changeEvent" not in src, (
        "main_window.py 又有 changeEvent 了 —— R155 要求最小化走 Qt 标准"
        "行为（缩任务栏），任何「最小化→藏托盘」劫持都必须删除")
    # 用户可见口径同步：托盘提示不再说「已最小化到系统托盘」（那是关闭→托盘）
    assert "已最小化到系统托盘" not in src, (
        "托盘提示还写着「已最小化到系统托盘」—— 现在只有关闭才缩托盘，"
        "文案要改成「已缩到系统托盘」")
    # 注释口径同步：不许留着「点最小化也缩托盘」的错误描述（只查注释行）
    for ln, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not s.startswith("#"):
            continue
        if "最小化" in s and "托盘" in s:
            assert ("不" in s or "删除" in s or "标准行为" in s), (
                f"main_window.py:{ln} 留着「最小化→缩托盘」的错误描述：{s}")


def test_r155_settings_menu_full_quit_entry():
    """R155-7：设置菜单退出项=「完全退出」+ tooltip + 仍连 request_quit。"""
    src = open(_MW, encoding="utf-8").read()
    m = re.search(r'a_quit = QAction\("([^"]+)"', src)
    assert m, "设置菜单里找不到 a_quit"
    assert m.group(1) == "完全退出", (
        f"设置菜单退出项应叫「完全退出」，实际「{m.group(1)}」")
    seg = src[m.start():m.start() + 700]
    assert "setToolTip" in seg, "「完全退出」没有 tooltip（用户分不清它和关闭窗口）"
    assert "缩到托盘" in seg, "tooltip 没写清「关闭窗口只是缩到托盘」"
    assert "request_quit" in seg, "「完全退出」没连 request_quit（行为会变）"
    # 不新增顶级菜单：8 个菜单的锁由 verify_r11 / test_r26 负责，这里只确认
    # 本改动没有新 addMenu 到 menuBar（改名的 a_quit 仍走 m_set.addAction）
    assert "menuBar().addMenu" not in src, "不许新增顶级菜单（verify_r11 钉死 8 个）"
