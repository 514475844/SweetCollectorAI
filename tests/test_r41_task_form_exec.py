"""r41：锁死 #38「新建/修改任务 点了没反应 / 白框」。

r40 修「面板被对话框借走不还」时，把 `_open_task_form` 的尾部（填值 +
`dlg.exec()`）错缩进进了 `_attach_panel_to_dlg` 的 `return True` 之后，变成永远
执行不到的死代码 —— 真机上对话框压根没弹出来。而 test_r40 把 `QDialog.exec`
打桩成 no-op、只检查「面板挂得对不对」，所以**旧代码照样全绿**。

本文件锁三条不变量（旧代码三条全不满足）：

1. 每次 `_open_task_form` 都必须真的对对话框调 `exec()`，并且复用的还是同一个框；
2. 新建（t=None）时，exec 之前必须已经把「上次保存地址」等新建默认值填进面板；
3. 编辑（t=任务）时，exec 之前必须已经把任务值填进面板（不是上一次打开的残值）。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QWidget  # noqa: E402


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


def _rec_exec(monkeypatch):
    """把 exec() 换成探针：记录被 exec 的对话框，立刻返回（不进事件循环）。"""
    seen = []
    monkeypatch.setattr(
        QDialog, "exec",
        lambda self, *a, **k: (seen.append(self)
                               or QDialog.DialogCode.Rejected))
    return seen


def test_open_task_form_actually_execs(qapp, monkeypatch):
    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        w._ensure_task_form()
        w._open_task_form(None)
        assert len(seen) == 1, (
            f"对话框没被 exec（用户点了等于没点），exec 一共调了 {len(seen)} 次")
        dlg, panel = seen[0], w._form_panel
        assert dlg.layout().indexOf(panel) >= 0, "对话框里挂的不是当前面板 → 白框"
        assert panel.parent() is dlg
        assert len(panel.findChildren(QWidget)) > 20, "表单控件没进对话框"

        w._open_task_form(None)               # 第二次：必须复用同一个框
        assert len(seen) == 2, "第二次打开又新造了个对话框"
        assert seen[1] is dlg, "第二次不该换成新框"
    finally:
        w.close()


def test_new_task_gets_defaults_before_exec(qapp, monkeypatch):
    """新建任务：输出目录必须**留空**（P1-2 改需求，方向与本用例原断言相反）。

    历史：r41 那轮断言「新建要带上次的保存地址」，后来被生产测试报告 P1-2
    判定为反人类—— 表单看起来是新的，目录却被悄悄复制过来，正是用户最早
    吐槽的「新建任务直接复制」的同源残留。现在改成**显式可点、默认不勾**：
    想沿用就自己勾「沿用上次目录」，不许静默填充。
    """
    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        w._ensure_task_form()
        w.cfg.last_output_dir = r"D:\单次默认值"
        w._open_task_form(None)
        assert len(seen) == 1
        assert w.f_out.text() == "", (
            "新建任务不该静默填回上次的输出目录 —— 必须由用户主动勾「沿用」")
        cb = getattr(w, "_cb_reuse_last", None)
        assert cb is not None, "缺少「沿用上次目录」入口 —— 想沿用得有个可点的地方"
        assert not cb.isChecked(), "「沿用上次目录」必须默认不勾"
        # 勾上才填 —— 这才是「显式可点」的含义
        cb.setChecked(True)
        assert w.f_out.text() == r"D:\单次默认值", "勾了「沿用」却没填目录"
        cb.setChecked(False)
        assert w.f_out.text() == "", "取消勾选后目录没清空"
        assert w.f_entry.text().strip() == "", "新建不该留着上次的入口"
    finally:
        w.close()


def test_edit_task_fills_form_before_exec(qapp, monkeypatch):
    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        from media_resource_manager.models import TaskConfig
        from media_resource_manager.repo import get_task, save_task

        w._ensure_task_form()
        tid = save_task(w.db, TaskConfig(
            id=None, name="", adapter_name="",
            entry_urls=["https://example.com/album/1"]))
        w.f_entry.setText("上次的残值网址")
        w._open_task_form(get_task(w.db, tid))
        assert len(seen) == 1, "编辑任务时对话框没弹出来"
        # 需求 5：任务名已无独立输入框（由网址派生），编辑打开时
        # 「网址」必须回填本任务的入口，而不是上一次打开残留的值。
        assert w.f_entry.text().strip() == "https://example.com/album/1", (
            f"编辑打开时 f_entry 是 {w.f_entry.text()!r}，不是本任务 → 用户看到旧内容")
    finally:
        w.close()
