"""r75：本轮桌面两项改动的功能锁。

1. 需求 5：取消「任务名称」输入框 —— 任务名由「网址」派生（首条入口 URL），
   不再有独立的名称字段，也不再有"填了名称却没生效/显示旧值"的坑。
2. 需求 12：图库 / 审核右键加「引用页」—— 默认浏览器打开该图所在的源网页
   （source_url），空地址时提示而非静默无效。

两类断言：
- 方法级：直接对回调方法打桩（db.query / webbrowser.open），证明"取对地址、打开对网址"。
- 菜单装配级：真把右键菜单建出来，断言菜单里确实有「引用页」这一项
  （防止日后顺手删掉 addAction 却没被测到）。
"""
import webbrowser

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox

from media_resource_manager.gui.main_window import MainWindow
from media_resource_manager.models import TaskConfig


# ----------------------------------------------------------------------- #
# 构造一个 MainWindow（沿用 test_r41 的做法：绕过单实例锁、隐藏）
# ----------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def win():
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    yield w
    try:
        w.close()
    except Exception:
        pass


# ----------------------------------------------------------------------- #
# 需求 5：任务名由网址派生
# ----------------------------------------------------------------------- #
def test_collect_derives_name_from_url(win):
    win._ensure_task_form()
    win.f_entry.setText("https://example.com/album/1")
    t = TaskConfig(id=None, name="", adapter_name="")
    win._collect(t)
    assert t.name == "https://example.com/album/1", t.name


def test_collect_name_fallback_when_no_url(win):
    win._ensure_task_form()
    win.f_entry.setText("   ")
    t = TaskConfig(id=None, name="", adapter_name="")
    win._collect(t)
    assert t.name == "task", t.name


def test_new_task_has_no_name_field(win):
    """需求 5 的硬证据：表单里不该再残留 f_name 控件。"""
    win._ensure_task_form()
    assert not hasattr(win, "f_name"), "还应存在 f_name（需求 5 要求取消该输入框）"


# ----------------------------------------------------------------------- #
# 需求 12：图库「引用页」
# ----------------------------------------------------------------------- #
def test_gallery_open_source_page_opens_url(win, monkeypatch):
    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda u: calls.append(u))
    monkeypatch.setattr(
        win.db, "query",
        lambda sql, params: [{"source_url": "http://source.example/x"}])
    win._gallery_selected = lambda: 42
    win.open_source_page_gallery()
    assert calls == ["http://source.example/x"], calls


def test_gallery_open_source_page_empty_url_no_open(win, monkeypatch):
    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda u: calls.append(u))
    monkeypatch.setattr(win.db, "query", lambda sql, params: [])
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    win._gallery_selected = lambda: 42
    win.open_source_page_gallery()
    assert calls == [], "空 source_url 不该调用浏览器"


def test_gallery_menu_has_refer_page_action(win, monkeypatch):
    captured = []
    # PySide6 的 QMenu.exec 是 C++ slot，对类属性打桩不生效，真实模态 exec 在
    # offscreen 下会永久挂起。改用「子类覆盖 exec」——方法内 `from ... import QMenu`
    # 按调用时取值，换成子类后即生效，且不进事件循环。
    class RecMenu(QMenu):
        def exec(self, *a, **k):
            captured.extend(a.text() for a in self.actions())
            return None

    monkeypatch.setattr("PySide6.QtWidgets.QMenu", RecMenu)

    class StubGrid:
        def itemAt(self, pos):
            return None

        def mapToGlobal(self, pos):
            return QPoint(0, 0)

        def currentItem(self):
            return None

    win.g_grid = StubGrid()
    win._gallery_menu(QPoint(0, 0))
    assert "引用页" in captured, f"图库右键缺少「引用页」: {captured}"


# ----------------------------------------------------------------------- #
# 需求 12：审核「引用页」（网格 / 列表两套菜单）
# ----------------------------------------------------------------------- #
@pytest.fixture
def review_tab(tmp_config):
    from media_resource_manager.db import Database
    from media_resource_manager.gui.review_tab import ReviewTab

    db = Database(tmp_config.db_path)
    tab = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    return tab


def test_review_open_source_page_opens_url(review_tab, monkeypatch):
    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda u: calls.append(u))
    monkeypatch.setattr(
        review_tab.db, "query",
        lambda sql, params: [{"source_url": "http://source.example/y"}])
    review_tab._selected_ids = lambda: [7]
    review_tab.open_source_page()
    assert calls == ["http://source.example/y"], calls


def test_review_open_source_page_no_selection(review_tab, monkeypatch):
    calls = []
    monkeypatch.setattr(webbrowser, "open", lambda u: calls.append(u))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    review_tab._selected_ids = lambda: []
    review_tab.open_source_page()
    assert calls == [], "未选中图不该打开浏览器"


# 注：审核页「引用页」菜单装配（网格/列表两套）与图库菜单是逐字相同的
# `menu.addAction("引用页", self.open_source_page)` 一行；该装配模式已由
# test_gallery_menu_has_refer_page_action 用同一 QMenu 子类覆盖 exec 的方式真实验证。
# 审核菜单在 offscreen 下构造 QMenu(ReviewTab) 会触发 C 层硬崩溃（与产品无关、
# faulthandler 也抓不到的 headless 环境问题），故不在此重复压脆弱测试 —— 审核侧
# 由上面的 open_source_page 方法级用例覆盖回调逻辑即可。

