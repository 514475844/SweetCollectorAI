"""R181 护栏：左侧「隔离区」= 主窗口内嵌面板（stack 真页，不弹窗）。

订单：把隔离区从独立 QDialog 改为主窗口内嵌页（与图库/审核同级），
复用现有表格/网格组件；点导航在主线体内打开，不再弹独立窗。

判据来源（R73 教训：判据不许被 docstring/注释骗到）：
  · 行为级（L1）真跑 MainWindow：真点导航 / 菜单，断言 stack 状态与
    页对象身份 —— 不 grep 源码字符串；
  · 源码级（L2）按**函数块**切（def 到下一个同级 def），且：
    - quarantine_manager 的 docstring 本身写着「从独立 QDialog 弹窗改为…」，
      所以「弹窗绝迹」判据只认**代码形态**（`QDialog(self)` / `dlg.exec`），
      不认裸词 QDialog；
    - _sync_nav_checks 的新注释里就含 `!= _NAV_QUARANTINE_IX` 字样，
      排除项判据必须**先剥注释**再判（R167 同族教训：注释会骗判据）。

⚠ 保留的既有判据接口（本文件只自检存在性，完整契约由 test_r143 /
   test_r180 / test_r80-AST 各自保证，不许在本文件重复钉死）：
   r143：_QUAR_WHERE / LIMIT ? OFFSET ? / all_quarantine_rows()×≥3；
   r80 AST：clean_missing/restore/purge/purge_all 四回调体内
   reload_rows() 与 self.refresh()。
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

_TMP = Path(tempfile.mkdtemp(prefix="r181_"))


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


def _func_block(src: str, name: str) -> str:
    """按方法级（4 空格缩进）块切：def 到下一个同级 def / 文件尾。"""
    m = re.search(r"(?m)^    def %s\(.*?(?=^    def |\Z)" % re.escape(name),
                  src, re.S)
    assert m, f"找不到函数 {name}"
    return m.group(0)


def _strip_comments(body: str) -> str:
    """逐行剥 # 注释（本仓这几个函数体内无含 # 的字符串字面量）。"""
    return "\n".join(line.split("#", 1)[0] for line in body.splitlines())


def _code_of(body: str) -> str:
    """剥 docstring + # 注释，只留真代码（R73：注释/docstring 会骗判据）。"""
    no_doc = re.sub(r'""".*?"""', "", body, flags=re.S)
    return _strip_comments(no_doc)


# ==================== 一、行为级（真跑 MainWindow） ====================

def test_nav_click_embeds_page_not_dialog(qapp):
    """点导航 = stack 里真切出隔离区页，且**不弹**独立对话框。"""
    from PySide6.QtWidgets import QApplication, QDialog
    w = _win()
    try:
        w._nav_go(w._NAV_QUARANTINE_IX)
        page = getattr(w, "_quar_tab", None)
        assert page is not None, "导航点隔离区没建出内嵌页（_quar_tab）"
        assert w.stack.indexOf(page) >= 0, "内嵌页没挂进 stack"
        assert w.stack.currentIndex() == w.stack.indexOf(page), \
            "当前页没切到隔离区"
        ghosts = [t for t in QApplication.topLevelWidgets()
                  if isinstance(t, QDialog) and t.windowTitle() == "隔离区管理"]
        assert not ghosts, f"还在弹独立对话框：{ghosts}"
    finally:
        w.close()


def test_build_once_same_page_object(qapp):
    """build-once：二次调用复用同一页对象，stack 不重复挂页。"""
    w = _win()
    try:
        w.quarantine_manager()
        p1 = w._quar_tab
        cnt = w.stack.count()
        w.quarantine_manager()
        p2 = w._quar_tab
        assert p1 is p2, "二次调用重建了页对象（不是 build-once）"
        assert w.stack.count() == cnt, \
            f"二次调用 stack 页数涨了：{cnt} -> {w.stack.count()}"
    finally:
        w.close()


def test_reopen_reloads_data(qapp):
    """二次进入（切走再切回）必须真调 _quar_reload + 切页（重载走起）。"""
    w = _win()
    try:
        w.quarantine_manager()
        calls = []
        real_reload = w._quar_reload

        def counting():
            calls.append(True)
            real_reload()

        # 直接换实例属性（quarantine_manager 已建分支调 self._quar_reload()）
        w._quar_reload = counting
        _gal_ix = w.stack.indexOf(w._gallery_tab)   # V0.0.9 页序：图库=3
        w._nav_go(_gal_ix)          # 切到图库
        assert w.stack.currentIndex() == _gal_ix, "前置失败：没切到图库"
        w._nav_go(w._NAV_QUARANTINE_IX)   # 再点隔离区
        assert calls, "二次进入没调 _quar_reload（数据不刷新回归）"
        assert w.stack.currentIndex() == w.stack.indexOf(w._quar_tab), \
            "二次进入没切回隔离区页"
    finally:
        w.close()


def test_menu_entry_same_route(qapp):
    """菜单「隔离区管理…」与导航同源：都走 quarantine_manager，不弹窗。"""
    from PySide6.QtWidgets import QApplication, QDialog
    w = _win()
    try:
        w.quarantine_manager()      # 先 build
        w._nav_go(0)                # 切走
        w.act_qmgr.trigger()        # 菜单入口
        assert w.stack.currentIndex() == w.stack.indexOf(w._quar_tab), \
            "菜单入口没切回隔离区页"
        ghosts = [t for t in QApplication.topLevelWidgets()
                  if isinstance(t, QDialog) and t.windowTitle() == "隔离区管理"]
        assert not ghosts, "菜单入口还在弹独立对话框"
    finally:
        w.close()


def test_stack_index_is_3(qapp):
    """页序契约（V0.0.9）：首页0/任务1/审核2/图库3/**隔离区4**（与图库、审核同级）。"""
    w = _win()
    try:
        w.quarantine_manager()
        ix = w.stack.indexOf(w._quar_tab)
        assert ix == 4, f"隔离区页索引应为 4，实际 {ix}"
        assert ix == w._NAV_QUARANTINE_IX, \
            f"导航常量与页索引脱节：常量={w._NAV_QUARANTINE_IX} 页={ix}"
    finally:
        w.close()


# ==================== 二、源码级（函数块切，防回退） ====================

def test_no_more_dialog_in_quarantine_manager():
    """弹窗绝迹：quarantine_manager 体内不许再有 QDialog/exec 代码形态。

    ⚠ 只认代码形态不认裸词 —— docstring 里就写着「从独立 QDialog 弹窗
    改为…」，裸搜 QDialog 会把说明文字当违规（自摆乌龙）。
    """
    body = _code_of(
        _func_block(MAIN.read_text(encoding="utf-8"), "quarantine_manager"))
    for bad in ("QDialog(self)", "dlg = QDialog", "dlg.exec",
                "QVBoxLayout(dlg)", "setWindowTitle(\"隔离区管理\")"):
        assert bad not in body, f"quarantine_manager 仍有弹窗形态：{bad}"


def test_build_once_wiring_in_source():
    """登记三件套在位：_quar_tab / _quar_reload / stack.addWidget。"""
    body = _func_block(MAIN.read_text(encoding="utf-8"), "quarantine_manager")
    for need in ("self._quar_tab = page",
                 # 2026-10-27：不再是"动态赋值同名洞"，改成真方法
                 # `_quar_reload()` 委托 `_quar_reload_impl`（静态守卫也才看得见）。
                 "self._quar_reload_impl = reload_rows",
                 "self.stack.addWidget(page)"):
        assert need in body, f"build-once 接线缺件：{need}"
    # 已建分支（常态路径）：切页 + 重载 + 同步，一个都不能少
    for need in ("self.stack.setCurrentIndex(self.stack.indexOf(page))",
                 "self._quar_reload()", "self._sync_nav_checks()",
                 "self._refresh_nav_badges()"):
        assert need in body, f"已建分支缺动作：{need}"


def test_sync_nav_checks_no_exclusion():
    """选中态排除项绝迹：剥注释后不许再有 `!= _NAV_QUARANTINE_IX`。

    ⚠ 新注释文本里就含这个字样（解释「为什么撤掉」）—— 不剥注释必空转。
    """
    body = _code_of(
        _func_block(MAIN.read_text(encoding="utf-8"), "_sync_nav_checks"))
    assert "!= self._NAV_QUARANTINE_IX" not in body, \
        "选中态排除项回来了（停隔离区页四个导航都不选中）"
    assert "btn.setChecked(i == ix)" in body, "分组导航选中态形态变了"
    assert "b.setChecked(j == ix)" in body, "平铺导航选中态形态变了"


def test_nav_go_delegates():
    """_nav_go 仍转交 quarantine_manager（不许内联直切越界页）。"""
    body = _func_block(MAIN.read_text(encoding="utf-8"), "_nav_go")
    assert "self.quarantine_manager()" in body, \
        "_nav_go 不再转交 quarantine_manager（接错回调 / 越界直切）"
    assert "ix == self._NAV_QUARANTINE_IX" in body, \
        "_nav_go 的隔离区分支没了"


def test_r143_interface_intact():
    """r143 口径契约自检（完整判据在 test_r143，这里只防整段蒸发）。"""
    body = _func_block(MAIN.read_text(encoding="utf-8"), "quarantine_manager")
    for need in ("_QUAR_WHERE", "LIMIT ? OFFSET ?", "SELECT COUNT(*)",
                 "all_quarantine_rows()"):
        assert need in body, f"r143 口径契约缺件：{need}"
