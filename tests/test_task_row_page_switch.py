"""任务行「切页后行宽卡死」的防回归测试（#123）。

真回归现象：在审核 / 图库页把窗口拉宽或缩小，再切回「下载管理」页时，任务行
仍然是**旧宽度**，行尾的 运行/停止/编辑 就掉到屏幕中间、不靠右。

根因（探针实测）：
  * 任务列表在审核/图库页是隐藏的，而隐藏控件不参与布局，
    `task_list.viewport().width()` 返回的是上一次的旧值；
  * 旧的 `_sync_task_rows_width()` 在这个旧值上 `setMaximumWidth + resize`
    把行永久钳死（实测：视口已 1376、行还停在 900）；
  * 切回任务页也不会再同步（主窗口尺寸没变 → 不触发 resizeEvent）。

锁死三件事：隐藏状态下不许同步（否则就是钳死）；切回任务页必须补一次同步；
同步前必须解锁 min/max，否则任何残留约束都会把行拉回旧宽度。
"""

import dataclasses
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _boot(monkeypatch, tmp_path):
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted  # 弹窗不进事件循环

    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app_: True
    from media_resource_manager.gui import theme
    theme.apply_theme(app, False)
    win = mw.MainWindow()
    # ⚠⚠ R61（§2 引入 table/cards 两套）后**必须显式钉一个卡片皮肤**。
    #
    # 本文件整份都在验「卡片行 TaskRow 的宽度」，而 `_sync_task_rows_width()`
    # 对隐藏控件**故意直接跳过**（这正是 #123 的修法）。默认预设是 classic，
    # 而 §2 把 classic 分到了 `task_view="table"` ⇒ 卡片在默认状态下是隐藏的
    # ⇒ 同步永远跳过 ⇒ 本文件 4 条用例全红。
    #
    # ⚠ 这个红**不是产品回归**，是本文件的隐含前提（"卡片默认可见"）被 §2
    #   改掉了。所以改测试、不改产品 —— 而且必须写清为什么，否则半年后
    #   有人看到「测试红了就把它删掉/改绿」，就把 #123 的防回归覆盖弄丢了。
    win.cfg.ui_preset = "violet_look"        # cards 皮肤：卡片可见
    win._apply_layout_visibility()
    win.show()
    app.processEvents()
    # V0.0.9：开机默认页改成学习看板（首页0），本文件验任务页卡片行，
    # 必须先切到任务页（索引 1）再断言卡片可见。
    win.stack.setCurrentIndex(1)
    app.processEvents()
    assert win.task_list.isVisible(), \
        "本文件验的是卡片行，卡片必须可见（预设没钉对？）"
    return app, win


def _add_row(win, tid=1):
    """往任务列表塞一个 true 的 TaskRow（刷新流程太重，这里直接造）。"""
    import media_resource_manager.gui.main_window as mw
    from media_resource_manager.models import TaskConfig, TaskStatus
    from PySide6.QtWidgets import QListWidgetItem
    kw = {f.name: None for f in dataclasses.fields(TaskConfig)}
    kw.update(id=tid, name=f"row-{tid}", adapter_name="",
              entry_urls=["https://x/"], status=TaskStatus.IDLE)
    it = QListWidgetItem()
    it.setData(mw.Qt.ItemDataRole.UserRole, tid)
    it.setSizeHint(mw.QSize(0, 78))
    win.task_list.addItem(it)
    win.task_list.setItemWidget(it, mw.TaskRow(win, TaskConfig(**kw)))
    win._sync_task_rows_width()


def _row_widths(win):
    """只量「真任务行」的宽度（跳过「还没有任务」的提示行）。"""
    from PySide6.QtCore import Qt
    ws = []
    for i in range(win.task_list.count()):
        it = win.task_list.item(i)
        if it.data(Qt.ItemDataRole.UserRole) is None:
            continue          # 空库时的提示行，不是任务行
        w = win.task_list.itemWidget(it)
        if w is not None:
            ws.append(w.width())
    return ws


# offscreen 下「视口宽」与「行宽」会差一个滚动条（约 14px），断言留这个余量。
MAX_SLACK = 40


def _same_as_viewport(win):
    vw = _viewport(win)
    return all(abs(w - vw) <= MAX_SLACK for w in _row_widths(win)) and _row_widths(win)


def _viewport(win):
    return win.task_list.viewport().width()


def test_sync_skipped_while_task_page_hidden(monkeypatch, tmp_path):
    """页面藏着时同步必须直接跳过：拿旧视口宽钳行 = 把行永久卡死。"""
    app, win = _boot(monkeypatch, tmp_path)
    _add_row(win)
    assert _same_as_viewport(win), "初始行宽应等于视口宽"

    win.stack.setCurrentIndex(2)          # V0.0.9 页序：切到审核页，任务列表隐藏
    app.processEvents()
    before = _row_widths(win)
    win._sync_task_rows_width()           # 隐藏期间被 resizeEvent 叫来同步
    assert _row_widths(win) == before, "隐藏状态下不许改行宽（改=拿旧视口钳死）"


def test_returning_to_task_page_resyncs_row_width(monkeypatch, tmp_path):
    """真回归：审核页拉宽 → 切回任务页，行宽必须跟上新视口，按钮才靠右。"""
    app, win = _boot(monkeypatch, tmp_path)
    _add_row(win)
    old_vw = _viewport(win)

    win.stack.setCurrentIndex(2)          # 隐藏（审核页）
    app.processEvents()
    before = _row_widths(win)
    win.resize(old_vw + 400, 800)         # 用"旧的隐藏视口宽"去钳（旧代码的病根）
    app.processEvents()
    win._sync_task_rows_width()
    assert _row_widths(win) == before, "隐藏期间同步必须保持旧宽（不许钳死）"

    win.stack.setCurrentIndex(1)          # 切回任务页（V0.0.9 页序：首页0/任务1）
    app.processEvents()
    win._on_page_changed(1)               # 切页钩子（singleShot 0）
    app.processEvents()
    win._sync_task_rows_width()
    assert _viewport(win) > old_vw, "前提：切回后视口确实变宽了"
    assert _same_as_viewport(win), (
        "切回任务页后行宽必须跟上当前视口，否则行尾按钮跑到屏幕中间"
        f"（viewport={_viewport(win)}, rows={_row_widths(win)}）")


def test_sync_unlocks_previous_size_constraints(monkeypatch, tmp_path):
    """万一上一次钳过 min/max，同步必须先把约束解锁再 resize。"""
    app, win = _boot(monkeypatch, tmp_path)
    _add_row(win)
    row = win.task_list.itemWidget(win.task_list.item(0))
    row.setMaximumWidth(120)              # 人为造一个旧的、过窄的钳制
    row.setMinimumWidth(80)
    win._sync_task_rows_width()
    assert abs(row.width() - _viewport(win)) <= MAX_SLACK, (
        "旧约束必须被解锁，行要回到视口宽" f"（row={row.width()}, vp={_viewport(win)}）")
    assert row.maximumWidth() > row.width(), "同步后不能留下过窄的 max"


def test_page_changed_hook_only_fires_on_task_page(monkeypatch, tmp_path):
    """切到审核/图库页不该触发行宽同步（那时根本看不见任务列表）。"""
    app, win = _boot(monkeypatch, tmp_path)
    _add_row(win)
    win._on_page_changed(1)
    win._on_page_changed(2)
    app.processEvents()
    assert _same_as_viewport(win)


def test_hook_fires_via_production_signal(monkeypatch, tmp_path):
    """V0.0.9 页序护栏：真实切页信号（currentChanged）走到任务页（索引 1）
    时必须调度补同步 —— 钩子条件若还钉着字面量 0（首页），这条必红。"""
    app, win = _boot(monkeypatch, tmp_path)
    _add_row(win)
    calls = []
    win._sync_task_rows_width = lambda *a, **k: calls.append(1)
    # 先切去审核页（2），再切回任务页（1）：currentChanged → _on_page_changed
    win.stack.setCurrentIndex(2)
    app.processEvents()
    calls.clear()
    win.stack.setCurrentIndex(1)
    app.processEvents()                # singleShot(0) 在这一拍落地
    assert calls, ("切回任务页没补同步：_on_page_changed 还在钉字面量索引"
                   "（V0.0.9 页序：任务=1，首页=0）")
