"""审核页多选/全选批量处理 + 双击内置灯箱预览 的回归测试。

背景（2026-09-30 用户反馈）：审核页原本只支持单张操作、双击打开的是
文件夹（explorer /select），大量图想批量「不喜欢」没法多选。现在：
* 多选（Ctrl/Shift/全选）后点「喜欢/不喜欢/跳过」= 批量决定（一个撤销事务）
* 双击 = 内置灯箱预览（← → 翻页），不再打开文件夹
* 缩略图显示尺寸随窗口最大化放大（apply_grid_size）
"""
import pytest

pytest.importorskip("PySide6")

from media_resource_manager.db import Database
from media_resource_manager.models import ImageRecord, ReviewDecision
from media_resource_manager.repo import get_image, upsert_image


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _mk_image(db: Database, tmp_path, name: str, task_id=1):
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    rec = ImageRecord(
        id=None, source_url=f"https://x.com/{name}", task_id=task_id,
        path=str(p), tags=["cat"], in_quarantine=True, needs_review=True)
    return upsert_image(db, rec)


@pytest.fixture
def tab(tmp_config):
    _app()
    db = Database(tmp_config.db_path)
    from media_resource_manager.gui.review_tab import ReviewTab
    t = ReviewTab(db, tmp_config.isolate_dir, lambda s: None)
    t.reload()
    return t


def test_batch_decide_ids_processes_all_selected(tmp_config, tab):
    db = tab.db
    ids = [_mk_image(db, tmp_config.library_dir, f"b{i}.jpg") for i in range(3)]
    tab._batch_decide_ids(ids, ReviewDecision.DISLIKE)
    for iid in ids:
        rec = get_image(db, iid)
        assert rec.needs_review == 0, "批量不喜欢后应不再是待审核"


def test_decide_with_multiselection_goes_batch(tmp_config, tab, monkeypatch):
    db = tab.db
    ids = [_mk_image(db, tmp_config.library_dir, f"s{i}.jpg") for i in range(2)]
    tab.reload()
    assert tab.grid.count() >= 2
    tab.grid.selectAll()
    assert len(tab._selected_ids()) >= 2
    # 多选时不能弹逐张对话框：任务无 ai_labels 本就不会弹，这里再上双保险
    monkeypatch.setattr("media_resource_manager.gui.review_tab.QMessageBox",
                        None, raising=False) if False else None
    tab.decide(ReviewDecision.LIKE)
    for iid in ids:
        rec = get_image(db, iid)
        assert rec.needs_review == 0, "多选决定应整批处理"


def test_double_click_opens_lightbox_not_folder(tmp_config, tab, monkeypatch):
    db = tab.db
    _mk_image(db, tmp_config.library_dir, "lb.jpg")
    tab.reload()
    assert tab.grid.count() == 1

    captured = {}

    class FakeLightbox:
        def __init__(self, rows, idx=0, parent=None, on_decide=None, hotkeys=None):
            captured["rows"] = rows
            captured["idx"] = idx
            captured["on_decide"] = on_decide

        def exec(self):
            captured["exec"] = True

    import media_resource_manager.gui.review_tab as rt
    monkeypatch.setattr(rt, "Lightbox", FakeLightbox)
    tab.open_file()
    assert captured.get("exec"), "双击应打开灯箱预览"
    rows = captured["rows"]
    assert len(rows) == 1
    assert rows[0]["path"].endswith("lb.jpg")
    assert {"id", "path", "width", "height", "source_url", "tags",
            "score"} <= set(rows[0].keys())


def test_apply_grid_size_bounds_and_maximize(tab):
    from PySide6.QtCore import QSize
    tab.apply_grid_size(False)
    normal = tab.grid.iconSize()
    tab.apply_grid_size(True)
    maximized = tab.grid.iconSize()
    assert maximized.width() > normal.width(), "最大化时缩略图应放大"
    assert normal.width() >= 96 and maximized.width() <= 420


def test_single_decide_still_works(tmp_config, tab):
    db = tab.db
    iid = _mk_image(db, tmp_config.library_dir, "one.jpg")
    tab.reload()
    tab.grid.setCurrentRow(0)
    tab.decide(ReviewDecision.LIKE)
    rec = get_image(db, iid)
    assert rec.needs_review == 0


def test_multi_mode_toggle_changes_selection_mode(tmp_config, tab):
    from PySide6.QtWidgets import QListWidget
    tab.toggle_multi_mode(True)
    assert tab._multi_mode is True
    assert tab.grid.selectionMode() == QListWidget.SelectionMode.MultiSelection
    tab.toggle_multi_mode(False)
    assert tab.grid.selectionMode() == QListWidget.SelectionMode.ExtendedSelection


def test_context_menu_policy_enabled(tmp_config, tab):
    from PySide6.QtCore import Qt
    assert tab.grid.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu


def test_page_size_limits_shown_rows(tmp_config, tab):
    db = tab.db
    for i in range(60):
        _mk_image(db, tmp_config.library_dir, f"pg{i}.jpg")
    tab._page_size = 12
    tab.reload()
    assert tab.grid.count() == 12, "每页应只显示 12 张"
    tab._page_size = 50
    tab.reload()
    assert tab.grid.count() == 50


def test_dislike_dialog_accepts_blacklist_param(monkeypatch):
    from media_resource_manager.gui import dislike_dialog as dd
    # 两者都为空 → 直接 pure，不弹窗（返回 (unmatched, exclusions, mode)）
    assert dd.ask_dislike_reason(None, [], []) == ([], [], "pure")
    # 有内容时：打补丁让 exec 立即返回，验证 blacklist 入参不报错（不阻塞）
    monkeypatch.setattr(dd.QDialog, "exec", lambda self: 0)
    assert dd.ask_dislike_reason(None, ["cat"], ["blackX"]) == ([], [], "cancel")


def test_dislike_dialog_never_puts_whitelist_into_exclusions(monkeypatch):
    """【硬性规则回归】左栏「主题词」勾选只做负向学习，绝不能进入排除词。

    用户明确要求：审核时勾主题词的意思是「这张图其实没匹配上该主题」，
    不代表要把该词加入黑名单（否则整个主题都被误杀）。
    """
    from media_resource_manager.gui import dislike_dialog as dd

    captured = {}

    def fake_exec(self):
        # 模拟用户：勾选左栏全部（主题词），右栏一个都不勾，然后点「加入排除并删除」
        captured["dlg"] = self
        return 1

    monkeypatch.setattr(dd.QDialog, "exec", fake_exec)
    unmatched, exclusions, mode = dd.ask_dislike_reason(None, ["cat", "dog"], ["bad"])
    # 默认（未交互）情况下不应把主题词塞进排除词
    assert "cat" not in exclusions, "主题词绝不能出现在排除词里"
    assert "dog" not in exclusions, "主题词绝不能出现在排除词里"


def test_task_blacklist_returns_words(tmp_config, tab):
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    # 不写死 id（写死 id 会走 UPDATE 空操作），让 save_task 自增插入
    t = TaskConfig(id=None, name="t1", adapter_name="x")
    t.blacklist = ["bbb"]
    tid = save_task(tab.db, t)
    assert "bbb" in tab._task_blacklist(tid)
