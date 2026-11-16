"""r39b：锁死「日志面板控件把 self.log() 方法遮蔽掉」这条回归。

真机现场（r39 前台真窗口按钮全扫描）：

    [64/64] act -> '键盘审核时弹选词框'
    Traceback (most recent call last):
      File "media_resource_manager/gui/main_window.py", line 1498, in toggle_key_ask
        try:
      File "...", line 1506, in toggle_key_ask
        from ..config import save_config
    TypeError: 'PySide6.QtWidgets.QTextEdit' object is not callable

根因很土：日志面板控件起名就叫 ``self.log``（一个 QTextEdit），而代码里到处
``self.log("…")`` 当方法用。Python 里**实例属性优先级高于类方法**，于是那几处
调用在运行时变成「拿 QTextEdit 实例当函数调」。同一个坑在「删除常用词」上
又犯一次（扫描日志里两条一模一样的 TypeError）。

修法：控件改名 ``self.log_view``，还它一个真正的 ``MainWindow.log(msg)`` 方法。
"""
import pathlib

import pytest

pytest.importorskip("PySide6")

MW_SRC = pathlib.Path("media_resource_manager/gui/main_window.py")
MW_REL = "media_resource_manager/gui/main_window.py"


@pytest.fixture(autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def home(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", str(d))
    monkeypatch.setattr("media_resource_manager.config.user_data_dir",
                        lambda: d)
    return d


@pytest.fixture()
def win(qapp, home, monkeypatch):
    import media_resource_manager.gui.main_window as mw
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme

    mw._ensure_single_instance = lambda a: True
    app = QApplication.instance()
    for old in app.topLevelWidgets():
        try:
            for tmr in old.findChildren(QTimer):
                tmr.stop()
            old.close()
        except Exception:
            pass
    theme.apply_theme(app, False)
    w = mw.MainWindow()
    w.show()
    app.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass


def test_log_widget_is_named_log_view():
    """控件必须叫 log_view，不能占用 self.log 这个名字。"""
    src = MW_SRC.read_text(encoding="utf-8")
    assert "self.log = QTextEdit()" not in src
    assert "self.log_view = QTextEdit()" in src
    # 面板的 append / document / toPlainText 也都要跟着改。
    # r40：`log_append` 里不再直接写 self.log_view（控件随合并后的日志页
    # 被释放时会撞 QTextEdit already deleted，真机第 3 轮刷了 433 条），
    # 改成先取局部 lv 并做 shiboken6.isValid 判断。所以这里断言 append
    # 仍然作用在 log_view 上，但写法是 `lv.append(`。
    assert "self.log_view = QTextEdit()" in src
    assert "self.log_view.toPlainText()" in src
    assert "lv.append(text)" in src, "log_append 应该通过局部变量写日志"
    assert "shiboken6.isValid(lv)" in src, "log_append 必须判控件是否已销毁"
    # 并且全文件只能有一处 log_view 创建（旧的 _build_log_tab 会再建一个顶掉它）
    assert src.count("self.log_view = QTextEdit()") == 1
    assert "def _build_log_tab(" not in src


def test_log_is_a_real_method():
    src = MW_SRC.read_text(encoding="utf-8")
    assert "def log(self, msg: str):" in src


def test_toggle_key_ask_does_not_raise(win):
    """真点击一次「键盘审核时弹选词框」：不许再冒 TypeError。"""
    win.toggle_key_ask(True)
    assert win.cfg.review_key_ask is True
    assert isinstance(win.log_view, type(win.log_view))


def test_log_method_writes_and_widget_exists(win, qapp):
    """log() 能正常写日志，控件也在。"""
    import time
    from PySide6.QtWidgets import QTextEdit

    assert isinstance(win.log_view, QTextEdit)
    before = win.log_view.document().blockCount()
    win.log("r39 回归：这行必须能写进去")
    for _ in range(30):
        qapp.processEvents()
        if win.log_view.document().blockCount() > before:
            break
        time.sleep(0.02)
    txt = win.log_view.toPlainText()
    assert "r39 回归" in txt
