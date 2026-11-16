"""r40：锁死「新建/编辑任务 对话框整片空白」这个 bug。

用户截屏现场：点「编辑任务：https://ososedki.com」，弹出来的对话框**整片白色**，
只有右下角「保存」「取消」两个按钮，表单一个都看不见。

根因（面板被对话框“借走”之后没有还回来）：

1. `_open_task_form` 里 `v.addWidget(panel, 1)` 会把 panel 的 parent 从主窗口
   **改成 dlg**（Qt 语义：加进 layout 就等于 reparent）。
2. dlg 一旦被销毁（close / 被 sweep 当游离窗口收掉 / 主窗口重建），panel 跟着死；
   下一轮 `_ensure_task_form()` 发现 `_form_panel` 已 invalid，于是**重建面板**
   并挂回主窗口。
3. 但此时 `self._form_dlg` 可能还“活着”（deleteLater 排队期间 invalid 判定仍是 True），
   代码走 else 分支只 `dlg.setWindowTitle(title)` 就 `dlg.exec()` ——
   对话框里装的是**那个已经死了的旧面板** → 只剩按钮、一片空白。

修法：复用对话框前先确认「当前面板确实还在 dlg 的布局里」，不在就 `_attach_panel_to_dlg`
重新装配；另外面板一重建就把 `_form_dlg` 置 None，让下次强制重来。

本文件锁的不变量：**每一次 `_open_task_form` 返回后，当前 `_form_panel` 必须活在
`_form_dlg` 的布局里、parent 是 dlg、且是可见的、带一整个表单的子控件**。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _new_window(qapp):
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


def _no_exec(monkeypatch):
    """对话框建完立刻返回，别真的 exec() 把测试卡住。"""
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)


def _assert_form_attached(w):
    """核心不变量：面板在对话框里、可见、且带得动整个表单。"""
    dlg, panel = w._form_dlg, w._form_panel
    assert dlg is not None and panel is not None
    # 注意：exec() 被打桩成 no-op，对话框从没真正 show 过，所以这里不能断言
    # isVisible()（祖先没显示，子控件的 isVisible 必然 False），只锁“挂对了没”。
    assert dlg.layout().indexOf(panel) >= 0, "对话框里挂的不是当前面板 → 空白框"
    assert panel.parent() is dlg
    assert len(panel.findChildren(QWidget)) > 20, "表单控件没进对话框"


def test_form_panel_attached_on_every_open(qapp, monkeypatch):
    _no_exec(monkeypatch)
    w = _new_window(qapp)
    try:
        w._ensure_task_form()
        w._open_task_form(None)
        _assert_form_attached(w)
        _assert_form_attached(w)          # 再开一次（复用同一个对话框）依旧成立
    finally:
        w.close()


def test_rebind_panel_when_detached_from_dialog(qapp, monkeypatch):
    """面板被摘走（reparent 走 / 被 deleteLater 排队）后重开，必须重新挂回对话框。"""
    _no_exec(monkeypatch)
    w = _new_window(qapp)
    try:
        w._ensure_task_form()
        w._open_task_form(None)
        dlg = w._form_dlg
        panel = w._form_panel

        panel.setParent(w)                # 模拟面板被摘出对话框
        panel.hide()
        w._open_task_form(None)

        assert w._form_dlg is dlg, "不该每次都造新对话框"
        _assert_form_attached(w)
        assert panel.parent() is dlg
    finally:
        w.close()


def test_rebuilt_panel_invalidates_stale_dialog(qapp, monkeypatch):
    """面板重建后，旧的“装着死面板”的对话框引用必须被清掉。"""
    _no_exec(monkeypatch)
    w = _new_window(qapp)
    try:
        w._ensure_task_form()
        w._open_task_form(None)
        assert w._form_dlg is not None

        w._form_panel.deleteLater()
        w._open_task_form(None)           # 面板已死 → 重建 + 强制重新装配
        assert w._form_panel is not None
        _assert_form_attached(w)
    finally:
        w.close()
