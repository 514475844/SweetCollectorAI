"""V0.0.9 护栏：学习看板升级成主界面（打开软件第一眼 = 学到什么、正按什么找）。

用户初衷（2026-11-08 定稿）：导入图库学习只是起点，学到的东西必须是**门面**——
以前看板缩在菜单里当弹窗，首页仍是任务列表；现在两端都把看板提到第一屏。

钉住的行为契约：
  桌面
  1. stack 页序：首页0(看板)/任务1/审核2/图库3/（隔离区4 懒建）；
     启动默认页就是看板；导航「首页」是第一项（平铺与分组两套都算）。
  2. 看板内嵌形态：底部「关闭」按钮条隐藏、补了页头；弹窗形态（embed=False）
     保持原样不受影响（open_learning_dashboard 仍是老对话框行为）。
  3. 菜单「学习看板」入口不再弹模态：切到首页并返回看板部件本身。
  4. 切回首页会 reload（判退完回来看板数字/词表立刻是新的）。
  5. 首页导航项不挂数量角标（没有"多少个"的口径，挂 0 是误导）。
  6. 「跑完任务跳图库」不许再钉字面量索引（页序变了会跳错页）。
  网页
  7. 默认路由 '/' → '/learning'；导航第一项 = 首页（/learning）。
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"
BOARD = ROOT / "media_resource_manager" / "gui" / "learning_board.py"
MAINJS = ROOT / "media_resource_manager" / "webapp" / "src" / "main.js"
APPVUE = ROOT / "media_resource_manager" / "webapp" / "src" / "App.vue"

_MAIN_TXT = MAIN.read_text(encoding="utf-8")
_BOARD_TXT = BOARD.read_text(encoding="utf-8")

_TMP = Path(tempfile.mkdtemp(prefix="v009_"))


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("MRM_HOME", str(_TMP))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    yield


@pytest.fixture()
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _win():
    from media_resource_manager.gui.main_window import MainWindow
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


# ==================== 一、桌面页序与默认页 ====================

def test_board_is_page_zero_and_default(qapp):
    """首页=看板（stack 索引 0），且启动默认就停在它上面。"""
    from media_resource_manager.gui.learning_board import LearningDashboard
    w = _win()
    try:
        assert isinstance(w.stack.widget(0), LearningDashboard), \
            "stack 索引 0 不是学习看板——首页没升级成功"
        assert w.stack.currentIndex() == 0, \
            f"启动默认页不是首页：currentIndex={w.stack.currentIndex()}"
    finally:
        w.close()


def test_page_order_contract(qapp):
    """页序契约：任务1/审核2/图库3；隔离区常量 4；导航索引映射齐。"""
    w = _win()
    try:
        assert w.stack.indexOf(w._review_tab) == 2
        assert w.stack.indexOf(w._gallery_tab) == 3
        assert w._NAV_QUARANTINE_IX == 4, \
            "隔离区常量没跟着页序后移（导航点了会跳错页）"
        assert set(w._NAV_KEY_BY_IX) == {0, 1, 2, 3, 4}, w._NAV_KEY_BY_IX
    finally:
        w.close()


def test_flat_nav_home_first(qapp):
    """平铺导航第一项「首页」，后续任务/审核/图库/隔离区顺序不动。"""
    w = _win()
    try:
        labels = [b.text() for b in w._nav_btns]
        assert labels[:5] == ["首页", "任务", "审核", "图库", "隔离区"], labels
        # 首项选中态与默认页一致（开机停在首页，首页按钮必须亮）
        assert w._nav_btns[0].isChecked()
    finally:
        w.close()


def test_grouped_nav_home_first(qapp):
    """分组导航同样「首页」置顶，索引 0..4 连续。"""
    w = _win()
    try:
        idxs = [ix for _b, _bd, ix in w._nav_qb]
        labels = [b.text() for b, _bd, _ix in w._nav_qb]
        assert idxs == [0, 1, 2, 3, 4], idxs
        assert labels[0] == "首页", labels
        assert labels.index("隔离区") == w._NAV_QUARANTINE_IX
    finally:
        w.close()


# ==================== 二、看板内嵌形态 ====================

def test_embed_board_hides_close_bar(qapp):
    """内嵌：按钮条隐藏 + 有页头；弹窗形态（默认）按钮条仍在。"""
    from media_resource_manager.gui.learning_board import LearningDashboard
    from media_resource_manager.db import Database
    d = Database(_TMP / "embed.db")
    try:
        emb = LearningDashboard(d, embed=True)
        assert emb._bb.isVisible() is False, "内嵌首页还挂着「关闭」按钮条"
        hdr = emb.layout().itemAt(0).widget()
        assert hdr is not None and "学习看板" in hdr.text(), \
            "内嵌首页缺页头（弹窗标题栏内嵌后不可见，必须补）"
        dlg = LearningDashboard(d)          # 老弹窗形态（默认参数）
        assert dlg._bb.isVisibleTo(dlg), \
            "弹窗形态的关闭按钮条被顺手改没了"
    finally:
        d.close()


def test_menu_entry_navigates_not_modal(qapp):
    """菜单「学习看板」：不再弹模态，切首页并返回看板部件本身。"""
    from PySide6.QtWidgets import QDialog, QApplication
    w = _win()
    try:
        tops_before = {id(t) for t in QApplication.topLevelWidgets()}
        ret = w.show_learning_dashboard()
        assert ret is w._board_tab, "菜单入口没返回首页看板本体"
        assert w.stack.currentIndex() == 0, "菜单入口没切到首页"
        new_dialogs = [t for t in QApplication.topLevelWidgets()
                       if id(t) not in tops_before
                       and isinstance(t, QDialog) and t.isVisible()]
        assert not new_dialogs, "菜单入口还在弹独立对话框（应改为切页）"
    finally:
        w.close()


def test_nav_back_home_reloads_board(qapp):
    """切回首页必须重刷看板：判退完回来看，词表/数字要立刻是新的。"""
    w = _win()
    try:
        calls = []
        real = w._board_tab.reload
        w._board_tab.reload = lambda *a, **k: (calls.append(1), real(*a, **k))
        w._nav_go(0)
        assert calls, "_nav_go(0) 没触发看板 reload"
    finally:
        w.close()


def test_home_badge_not_shown(qapp, monkeypatch):
    """首页导航项不挂数字角标（分组侧栏）。"""
    w = _win()
    monkeypatch.setattr(w, "_nav_badge_counts",
                        lambda: {"tasks": 1, "review": 2,
                                 "gallery": 3, "quarantine": 4})
    try:
        w._refresh_nav_badges()
        for _btn, badge, ix in w._nav_qb:
            if ix == 0:
                assert badge.isVisible() is False, \
                    "首页角标不该显示（没有数量口径，挂 0 会被当成'0 个什么'）"
            else:
                assert badge.text(), f"导航 {ix} 角标没写数字"
    finally:
        w.close()


# ==================== 三、源码形态防回退 ====================

def test_no_literal_navgo_gallery():
    """「跑完任务跳图库」不许再钉字面量：页序一变就跳错页。"""
    body = re.search(r"def _on_queue_event.*?\n(?=    def )",
                     _MAIN_TXT, re.S)
    assert body, "找不到 _on_queue_event，检查函数是否改名并同步本护栏"
    assert "_nav_go(2)" not in body.group(0), \
        "又写回字面量 _nav_go(2)：应改用 stack.indexOf(self._gallery_tab)"
    assert "indexOf(self._gallery_tab)" in body.group(0)


def test_board_embed_param_exists():
    """learning_board 必须有 embed 参数与关闭条隐藏逻辑（产品契约不是巧合）。"""
    assert "embed" in _BOARD_TXT
    assert "_bb.setVisible(False)" in _BOARD_TXT


# ==================== 四、网页端两端对齐 ====================

def test_web_default_route_is_board():
    src = MAINJS.read_text(encoding="utf-8")
    m = re.search(r"\{\s*path:\s*'/'.*?\}", src, re.S)
    assert m and "redirect: '/learning'" in m.group(0), \
        "网页默认路由没指向学习看板（两端都该第一眼是它）"


def test_web_nav_home_first():
    src = APPVUE.read_text(encoding="utf-8")
    m = re.search(r"const navs = \[(.*?)\]", src, re.S)
    assert m, "找不到 navs 导航表"
    first = re.search(r"\{[^}]*\}", m.group(1), re.S).group(0)
    assert "'/learning'" in first, f"网页导航第一项不是看板：{first}"
    assert "首页" in first, f"看板导航项没改叫「首页」：{first}"
