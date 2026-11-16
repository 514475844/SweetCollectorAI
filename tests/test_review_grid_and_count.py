"""审核网格 / 图库网格「同一套样式 + 禁误拖拽」+ 待审核总数量的防回归测试。

三个真问题（#122）：
  ① 待审核区只印「本页」条数（默认每页 50），隔离区里实际压了 300 张时
     用户完全看不到总数，反馈「缺少总数量显示」；
  ② 图库网格 spacing=12、审核网格 spacing=8、缩略图默认 128/160，还一个走
     gridSize 一个交给 Qt 自适应 —— 同一个软件里两种缩略图网格不像一家人；
  ③ 多选时按在缩略图上会直接拖走图片、拖得满屏幕乱转：QListWidget 在
     **IconMode + ExtendedSelection** 下 Qt 会自动把 dragEnabled 打开、
     dragDropMode 打成 DragDrop（探针实测），而这两个网格根本不需要拖放排序。

锁死三件事：总数量走 COUNT 而不是「本页条数」；两处网格的几何 / 拖拽参数来自
同一个 helper；两个网格的间距与拖拽开关必须一致。
"""

import os
import sys
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _boot_app(monkeypatch, tmp_path):
    """offscreen 环境起一个 QApplication（不弹窗、不碰真实 data 目录）。"""
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _db(tmp_config):
    from media_resource_manager.db import Database
    return Database(tmp_config.db_path)


def _dir0(tmp_config) -> Path:
    return Path(tmp_config.db_path).parent


def _mk_pending(db, base: Path, name, task_id=1):
    from media_resource_manager.models import ImageRecord
    from media_resource_manager.repo import upsert_image
    p = base / name
    p.write_bytes(b"x" * 32)
    return upsert_image(db, ImageRecord(
        id=None, source_url=f"https://x/{name}", task_id=task_id,
        path=str(p), in_quarantine=True, needs_review=True))


# ------------------------------------------------------------ 待审核总数（纯 DB）

def test_count_pending_is_total_not_page(tmp_config):
    """总数必须绕过 limit：pending(limit=2) 只有 2 条，count_pending 得是 7。"""
    from media_resource_manager.review.queue import ReviewQueue
    db = _db(tmp_config)
    for n in range(7):
        _mk_pending(db, _dir0(tmp_config), f"p{n}.jpg")
    q = ReviewQueue(db, tmp_config.isolate_dir)
    assert q.count_pending() == 7, "总数量不应被 limit 影响"
    assert len(q.pending(limit=2)) == 2


def test_count_pending_filters_by_task(tmp_config):
    db = _db(tmp_config)
    _mk_pending(db, _dir0(tmp_config), "a.jpg", task_id=1)
    _mk_pending(db, _dir0(tmp_config), "b.jpg", task_id=2)
    from media_resource_manager.review.queue import ReviewQueue
    q = ReviewQueue(db, tmp_config.isolate_dir)
    assert q.count_pending(task_id=1) == 1
    assert q.count_pending(task_id=2) == 1
    assert q.count_pending() == 2


def test_count_pending_zero_when_nothing_pending(tmp_config):
    """needs_review=0 的（真隔离区）不算待审核。"""
    db = _db(tmp_config)
    from media_resource_manager.models import ImageRecord
    from media_resource_manager.repo import upsert_image
    p = _dir0(tmp_config) / "ok.jpg"
    p.write_bytes(b"x" * 32)
    upsert_image(db, ImageRecord(id=None, source_url="https://x/ok.jpg",
                                 task_id=1, path=str(p),
                                 in_quarantine=True, needs_review=False))
    from media_resource_manager.review.queue import ReviewQueue
    assert ReviewQueue(db, tmp_config.isolate_dir).count_pending() == 0


# --------------------------------------------------------------- 标题行文案

def test_info_text_always_prints_total():
    """标题行必须出现「总数量」；本页没 load 满时还要提示还有多少张。"""
    from media_resource_manager.gui.review_tab import ReviewTab
    # _info_text 是纯格式化（不碰 self），直接给 None 省掉建窗口
    text = ReviewTab._info_text(None, shown=50, total=320)
    assert "<b>320</b>" in text, f"总数量要显眼地标出来：{text}"
    assert "本页 50 张" in text
    assert "还有 270 张未显示" in text


def test_info_text_no_unshown_hint_when_page_full():
    from media_resource_manager.gui.review_tab import ReviewTab
    text = ReviewTab._info_text(None, shown=50, total=50, missing=3)
    assert "<b>50</b>" in text
    assert "未显示" not in text
    assert "3 条磁盘缺失" in text


# ---------------------------------------------------------- 网格样式 / 拖拽

def test_apply_grid_layout_sets_one_uniform_grid(monkeypatch, tmp_path):
    """helper 是唯一几何来源：间距 8、单元格 = 图 + 28、拖拽全关。"""
    _boot_app(monkeypatch, tmp_path)
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QAbstractItemView, QListWidget
    from media_resource_manager.gui.table_view import (
        GRID_CAPTION, GRID_SPACING, apply_grid_layout)

    lv = QListWidget()
    apply_grid_layout(lv, 160)
    assert lv.spacing() == GRID_SPACING == 8
    assert lv.iconSize() == QSize(160, 160)
    assert lv.gridSize() == QSize(160, 160 + GRID_CAPTION)
    assert lv.dragEnabled() is False, "多选时不能拖走缩略图"
    assert lv.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop
    assert lv.acceptDrops() is False


def test_review_tab_grid_never_drags(monkeypatch, tmp_path, tmp_config):
    """审核网格（IconMode + 多选举例）下 Qt 会自动开 dragEnabled，必须压掉。"""
    _boot_app(monkeypatch, tmp_path)
    from media_resource_manager.gui.review_tab import ReviewTab
    from media_resource_manager.gui.table_view import GRID_CAPTION, GRID_SPACING
    tab = ReviewTab(_db(tmp_config), tmp_config.isolate_dir,
                    lambda s: None, tmp_config)
    grid = tab.grid
    assert grid.dragEnabled() is False, "审核网格误拖拽回归"
    assert grid.spacing() == GRID_SPACING
    assert grid.gridSize().height() == grid.iconSize().height() + GRID_CAPTION


def _bootstrap_window(monkeypatch, tmp_path):
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted  # 弹窗不进事件循环

    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app_: True
    from media_resource_manager.gui import theme
    theme.apply_theme(app, False)          # 必须先上主题，再构造主窗口
    return mw.MainWindow()


def test_gallery_grid_matches_review_grid(monkeypatch, tmp_path, tmp_config):
    """图库网格与审核网格：间距一致、单元格行高公式一致、都不许拖。"""
    _boot_app(monkeypatch, tmp_path)
    win = _bootstrap_window(monkeypatch, tmp_path)
    win._apply_grid()

    gg, rg = win.g_grid, win._review_tab.grid
    assert gg.spacing() == rg.spacing(), "两处网格间距必须一模一样"
    assert gg.spacing() == 8
    assert gg.gridSize().height() == gg.iconSize().height() + 28
    assert gg.dragEnabled() is False and rg.dragEnabled() is False
    assert gg.acceptDrops() is False and rg.acceptDrops() is False
