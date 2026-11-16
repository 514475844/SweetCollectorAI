"""「最大抓取页数」改成每任务独立（r29）的防回归测试。

改之前：只有一个**全局** max_pages（菜单里却叫「每任务最大抓取页数…」），
改一次影响所有任务，而且被同名的规则字段盖住 —— 任务自己设了没用。
改之后：任务表单里有「最大页数」，优先级 任务级 > 规则级 > 全局默认，0=不限。

这里锁四件事：
  1. 优先级解析（纯函数）；
  2. 老任务读出来是 0（=不限），不会凭空被截；
  3. 各任务的页数互不干扰，落库往返不丢；
  4. 任务表单里真有这个控件，编辑任务时回填的是任务自己的值。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _task(mid=None, **kw):
    """造一个字段齐全的 TaskConfig（用 dataclass 默认值），只覆盖要测的字段。"""
    import dataclasses
    from media_resource_manager.models import TaskConfig, TaskStatus
    d = {}
    for f in dataclasses.fields(TaskConfig):
        if f.default is not dataclasses.MISSING:
            d[f.name] = f.default
        elif f.default_factory is not dataclasses.MISSING:
            d[f.name] = f.default_factory()
        else:
            d[f.name] = None
    d.update(id=mid, name=f"task-{mid}", adapter_name="",
             entry_urls=["https://x/"], status=TaskStatus.IDLE)
    d.update(kw)
    return TaskConfig(**d)


def _boot(monkeypatch, tmp_path):
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtCore import QTimer          # QTimer 在 QtCore，不在 QtWidgets
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    # 每个用例都会新建一个 MainWindow，旧窗口的 _tick 定时器在后面用例里
    # 还会触发并去摸已经销毁的控件（libshiboken: already deleted）。先止血。
    for old in app.topLevelWidgets():
        try:
            for tmr in old.findChildren(QTimer):
                tmr.stop()
            old.close()
        except Exception:
            pass
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app_: True
    from media_resource_manager.gui import theme
    theme.apply_theme(app, False)
    win = mw.MainWindow()
    win.show()
    app.processEvents()
    # 任务表单是**按需**建的（每次开对话框才建一次），填表前先把它建出来，
    # 否则 f_* 控件根本不存在。
    # 表单容器要一直握着引用：面板顶层控件被回收时，挂在上面的 f_* 控件会
    # 一起被 Qt 删掉，self.f_name 之类就成悬空包装了。
    win._keep_panel = win._build_task_form_panel()
    return app, win


# ----------------------------------------------------------- 1) 优先级 ------
def test_priority_task_over_rule_over_cfg():
    from media_resource_manager.engine import resolve_max_pages

    class R:
        max_pages = 50

    class C:
        max_pages = 200

    assert resolve_max_pages(_task(1, max_pages=8), R(), C()) == 8   # 任务最优先
    assert resolve_max_pages(None, R(), C()) == 50                   # 任务没设 → 规则
    assert resolve_max_pages(None, None, C()) == 200                 # 都没设 → 全局
    assert resolve_max_pages(_task(1, max_pages=0), R(), C()) == 50  # 0=不限 → 继续往里找


def test_zero_means_unlimited():
    from media_resource_manager.engine import resolve_max_pages
    assert resolve_max_pages(_task(1), None, None) == 0
    assert resolve_max_pages() == 0


def test_garbage_field_does_not_crash():
    from media_resource_manager.engine import resolve_max_pages

    class Bad:
        max_pages = "oops"

    assert resolve_max_pages(Bad()) == 0


# ----------------------------------------------------- 2) 老任务兼容 -------
def test_old_task_without_column_is_unlimited(tmp_path):
    """没有 max_pages 这一列的老库 → 读出来 0（不限），不会被悄悄截一刀。"""
    import sqlite3
    from media_resource_manager.db import Database
    path = tmp_path / "old.db"
    raw = sqlite3.connect(str(path))
    raw.execute(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "name TEXT NOT NULL, adapter_name TEXT, entry_urls TEXT DEFAULT '[]', "
        "keywords TEXT DEFAULT '[]', blacklist TEXT DEFAULT '[]', proxy TEXT, "
        "use_proxy INTEGER DEFAULT 0, concurrency INTEGER DEFAULT 4, "
        "max_depth INTEGER DEFAULT 3, delay REAL DEFAULT 1.0, output_dir TEXT, "
        "auto_run INTEGER DEFAULT 0, status TEXT DEFAULT 'idle', "
        "is_template INTEGER DEFAULT 0, parent_id INTEGER, "
        "created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')))")
    raw.execute("INSERT INTO tasks (name) VALUES ('legacy')")
    raw.commit()
    raw.close()
    db = Database(str(path))
    from media_resource_manager.repo import get_task
    t = get_task(db, 1)
    assert t is not None
    assert t.max_pages == 0      # 老任务 = 不设上限


# ------------------------------------------------- 3) 任务互不干扰 ----------
def test_each_task_keeps_its_own_page_cap(tmp_path):
    from media_resource_manager.db import Database
    from media_resource_manager.repo import get_task, save_task
    db = Database(str(tmp_path / "t.db"))
    ids = [save_task(db, _task(None, max_pages=n)) for n in (0, 7, 500, 3000)]
    assert [get_task(db, i).max_pages for i in ids] == [0, 7, 500, 3000]


def test_updating_one_task_does_not_touch_others(tmp_path):
    from media_resource_manager.db import Database
    from media_resource_manager.repo import get_task, save_task
    db = Database(str(tmp_path / "t2.db"))
    a = save_task(db, _task(None, max_pages=0))
    b = save_task(db, _task(None, max_pages=100))
    t = get_task(db, a)
    t.max_pages = 888                      # 只改 a
    save_task(db, t)
    assert get_task(db, a).max_pages == 888
    assert get_task(db, b).max_pages == 100  # b 没被连带


# ----------------------------------------------------- 4) GUI 表单 ---------
def test_form_has_the_spinbox(monkeypatch, tmp_path):
    app, win = _boot(monkeypatch, tmp_path)
    assert hasattr(win, "f_pages")
    assert win.f_pages.maximum() >= 100
    assert win.f_pages.minimum() == 0


def test_fill_form_reads_task_own_value(monkeypatch, tmp_path):
    app, win = _boot(monkeypatch, tmp_path)
    win._fill_form(_task(1, max_pages=321))
    assert win.f_pages.value() == 321

    win._fill_form(_task(2, max_pages=0))
    assert win.f_pages.value() == 0


def test_new_task_inherits_global_default(monkeypatch, tmp_path):
    app, win = _boot(monkeypatch, tmp_path)
    win.cfg.max_pages = 0
    win._fill_form(_task(None, max_pages=0))
    assert win.f_pages.value() == 0

    win.cfg.max_pages = 300
    win._fill_form(_task(None, max_pages=0))
    assert win.f_pages.value() == 300


def test_collect_writes_task_max_pages(monkeypatch, tmp_path):
    app, win = _boot(monkeypatch, tmp_path)
    t = _task(5)
    win._fill_form(t)
    win.f_pages.setValue(777)
    assert win._collect(t).max_pages == 777


def test_global_default_is_no_cap_by_default():
    from media_resource_manager.config import AppConfig
    from dataclasses import fields
    f = next((x for x in fields(AppConfig) if x.name == "max_pages"), None)
    assert f is not None and f.default == 0
