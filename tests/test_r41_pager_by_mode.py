"""r41 / #39：分页条按视图模式显隐。

图库的分页条（← 上一页 / 第 N 页 / 下一页 →）是网格、列表两模式共用的公共
布局块，以前它只是个局部变量、可见性没人管 —— 切到**列表模式**它照样杵在最
下面冒出翻页按钮，而列表模式一屏就是整张表格的取数结果，翻页纯属干扰。

锁两条不变量：

1. 网格模式（默认）分页条**不能被显式隐藏** —— 大库分页就靠它；
2. 切到列表模式分页条**必须隐藏**，切回网格再**恢复**。

两个坑（写测试时都踩过，留注释免得后人再踩）：
* 主窗口 ``hide()`` 之后子控件的 ``isVisible()`` 必然是 False（Qt 的可见性要
  一路算到顶层窗口），不能用它验；实现调的是 ``setVisible()``，所以查
  ``isHidden()`` —— 它只看本控件有没有被显式 hide，不受祖先影响。
* ``toggle_gallery_mode()`` 开头读 ``self.g_mode.isChecked()``，而 ``g_mode``
  是 checkable 按钮、靠 ``clicked`` 信号驱动；直接调方法按钮没被点过，
  ``g_list_mode`` 不会翻。这里模拟用户真点一次按钮。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def qapp():
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


def _click_mode(w):
    """模拟用户点一次「网格/列表」模式按钮（g_mode 是 checkable，click() 自己翻）。"""
    w.g_mode.click()


def test_pager_not_hidden_in_grid_mode(qapp, monkeypatch):
    w = _win()
    try:
        monkeypatch.setattr(w, "load_gallery", lambda *_a, **_k: None)
        assert w.g_list_mode is False, "初始应该是网格模式"
        assert w.g_pager.isHidden() is False, "网格模式下分页条被隐藏了"
        _click_mode(w)                          # → 列表
        _click_mode(w)                          # → 回网格
        assert w.g_list_mode is False
        assert w.g_pager.isHidden() is False, "切回网格分页条没恢复"
    finally:
        w.close()


def test_pager_hidden_in_list_mode(qapp, monkeypatch):
    w = _win()
    try:
        monkeypatch.setattr(w, "load_gallery", lambda *_a, **_k: None)
        _click_mode(w)                          # → 列表模式
        assert w.g_list_mode is True
        assert w.g_pager.isHidden() is True, "列表模式还冒出翻页按钮了"
        _click_mode(w)                          # → 回网格
        assert w.g_list_mode is False
        assert w.g_pager.isHidden() is False, "切回网格分页条没恢复"
    finally:
        w.close()
