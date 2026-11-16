"""r33 回归：不喜欢弹窗带二三级词 / 自动补满以复选框为准 / 表头可拖+可隐藏列 / 不喜欢不搬图库。

用户 2026-10-03 反馈的五条：

1. 不喜欢里没有二级三级词汇 —— ``_task_ai_labels`` 只返回一级；
2. 自动补满位置不对，且"界面没勾但效果是补满" —— 判定后去读 cfg（会与框错位）；
3. 表头拖动有 BUG —— Qt 默认禁止拖表头重排，且列宽策略排序后被重设；
4. 表头不能取消显示项目 —— 没有隐藏列的入口；
5. 审核里点「不喜欢」污染图库 —— ``DISLIKE`` 直接 ``_isolate()``，把图库里的
   文件 ``shutil.move`` 进隔离区，图凭空消失。
"""
import pytest

pytest.importorskip("PySide6")

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from media_resource_manager.db import Database
from media_resource_manager.models import ImageRecord, ReviewDecision, TaskConfig
from media_resource_manager.gui import table_view as tv
from media_resource_manager.repo import save_task, upsert_image


@pytest.fixture(autouse=True)
def qapp():
    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------------- #
# 1) 不喜欢弹窗：一/二/三级主题词都要在
# --------------------------------------------------------------------- #
def test_dislike_words_cover_three_levels(tmp_config):
    """以前只给一级，用户想说"其实是二级没对上"都选不了。"""
    db = Database(tmp_config.db_path)
    tid = save_task(db, TaskConfig(
        id=None, adapter_name="generic",
        name="三条词", ai_labels=["一级A"],
        ai_labels_l2=["二级B"], ai_labels_l3=["三级C"]))

    from media_resource_manager.gui.review_tab import ReviewTab
    tab = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    words = tab._task_ai_labels(tid)
    assert set(words) == {"一级A", "二级B", "三级C"}, words


def test_dislike_words_dedup_preserved_order(tmp_config):
    db = Database(tmp_config.db_path)
    tid = save_task(db, TaskConfig(
        id=None, adapter_name="generic",
        name="去重", ai_labels=["X"], ai_labels_l2=["X", "Y"],
        ai_labels_l3=["Y", "Z"]))
    from media_resource_manager.gui.review_tab import ReviewTab
    tab = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    assert tab._task_ai_labels(tid) == ["X", "Y", "Z"]


def test_no_task_gives_no_words(tmp_config):
    db = Database(tmp_config.db_path)
    from media_resource_manager.gui.review_tab import ReviewTab
    tab = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    assert tab._task_ai_labels(999) == []


# --------------------------------------------------------------------- #
# 2) 自动补满：以复选框为准（界面说什么就是什么）+ 位置在动作区末尾
# --------------------------------------------------------------------- #
@pytest.fixture
def tab(tmp_config):
    db = Database(tmp_config.db_path)
    from media_resource_manager.gui.review_tab import ReviewTab
    t = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    t.reload()
    return t


def test_autofill_checkbox_is_the_single_source_of_truth(tab):
    """cfg 被别的路径改了也不能生效 —— 只看框。"""
    tab.cb_autofill.setChecked(False)
    assert tab._auto_fill_on() is False
    tab.cfg.review_auto_fill = True          # 模拟 cfg 被别处偷偷改回 True
    assert tab._auto_fill_on() is False, "框是关的就该关，不能去问 cfg"


def test_autofill_falls_back_to_config_before_the_box_exists(tmp_config):
    db = Database(tmp_config.db_path)
    from media_resource_manager.gui.review_tab import ReviewTab
    t = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    t.cfg.review_auto_fill = False
    t.cb_autofill = None                     # 框还没建起来时的兜底
    assert t._auto_fill_on() is False


def _toolbar_row(tab):
    """顶部工具栏 = ReviewTab 的 QVBoxLayout 第一项（QHBoxLayout）。

    注意：QCheckBox 是 ``addLayout`` 进父布局的，``cb.parent()`` 拿回来的是
    ReviewTab 本身（QWidget），不是那一行 —— 必须走 ``tab.layout().itemAt(0).layout()``。
    """
    v = tab.layout()
    assert isinstance(v.itemAt(0).layout().metaObject().className(), str)
    top = v.itemAt(0).layout()
    return [top.itemAt(i).widget() for i in range(top.count())]


def test_autofill_switch_sits_in_the_action_area(tab):
    """位置：动作区（应用学到的排除词）之后、整行最后一个，不再插在预览滑块中间。"""
    widgets = [w for w in _toolbar_row(tab) if w is not None]
    names = [type(w).__name__ for w in widgets]
    assert "QCheckBox" in names, names
    assert names[-1] == "QCheckBox", f"自动补满应排在整行最后一个：{names}"
    assert names[-2] == "QFrame", f"它前面应是动作区分隔线：{names}"


def test_autofill_switch_is_not_buried_with_the_preview_slider(tab):
    """旧 bug：它夹在「预览大小」滑块中间，视觉上像是一个显示选项而不是动作。"""
    row = _toolbar_row(tab)
    names = [type(w).__name__ if w is not None else "" for w in row]
    cb_at = next(i for i, n in enumerate(names) if n == "QCheckBox")
    # 工具栏前半段是「任务 / 预览大小」，滑块只会出现在这些位置，不该压着复选框
    assert cb_at >= len(names) - 3, f"复选框不该挤在工具栏前段：{names}"


def test_toggling_the_box_does_not_desync_the_ui(tab):
    tab.cb_autofill.setChecked(False)
    assert tab.cb_autofill.isChecked() is False
    assert tab.cfg.review_auto_fill is False


# --------------------------------------------------------------------- #
# 3) + 4) 表头：可拖排列序（列宽不被排序吃掉）+ 右键隐藏/显示列
# --------------------------------------------------------------------- #
def _sample_rows(tmp_path):
    a = tmp_path / "a.jpg"; a.write_bytes(b"x" * 8)
    b = tmp_path / "b.jpg"; b.write_bytes(b"x" * 8)
    return [
        {"id": 1, "path": str(a), "task_id": None, "score": 0.9,
         "bytes": 5 * 1024 * 1024, "width": 10, "height": 10,
         "source_url": "https://x.com/1", "collected_at": "2026-10-01 00:00:00"},
        {"id": 2, "path": str(b), "task_id": None, "score": 0.1,
         "bytes": 512 * 1024, "width": 10, "height": 10,
         "source_url": "https://y.com/2", "collected_at": "2026-10-03 00:00:00"},
    ]


def test_header_sections_are_movable(tmp_path):
    """Qt 默认 setSectionsMovable(False) —— 拖表头纹丝不动，这是用户说的"拖动有BUG"。"""
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)
    assert tbl.horizontalHeader().sectionsMovable() is True


def test_sorting_keeps_the_column_widths(tmp_path):
    """拖好的列宽不能被排序重置（否则每点一次表头布局就跳一次）。"""
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)
    hdr = tbl.horizontalHeader()
    hdr.resizeSection(7, 320)
    hdr.resizeSection(3, 90)
    before = [hdr.sectionSize(c) for c in range(tbl.columnCount())]
    hdr.sectionClicked.emit(3)
    after = [hdr.sectionSize(c) for c in range(tbl.columnCount())]
    assert after == before, f"排序后列宽变了：{before} -> {after}"


def test_header_columns_can_be_hidden_and_restored(tmp_path):
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)
    hdr = tbl.horizontalHeader()
    assert not hdr.isSectionHidden(7)

    tbl._hidden.add(7)
    tv._apply_hidden(tbl)
    assert hdr.isSectionHidden(7), "「入库时间」列应能被藏起来"

    tbl._hidden.clear()
    tv._apply_hidden(tbl)
    assert not hdr.isSectionHidden(7), "「显示所有列」应该把列放回来"


def test_hidden_state_survives_a_refill(tmp_path):
    """一刷新就全冒出来 = 藏了白藏。"""
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)
    hdr = tbl.horizontalHeader()
    tbl._hidden.add(7)
    tv._apply_hidden(tbl)
    tv.fill_table(tbl, _sample_rows(tmp_path), None)     # 重灌（判定后自动刷新）
    assert hdr.isSectionHidden(7)


def test_never_hides_every_last_column(tmp_path):
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)
    hdr = tbl.horizontalHeader()
    tbl._hidden = set(range(tbl.columnCount()))          # 全勾上了
    tv._apply_hidden(tbl)
    assert any(not hdr.isSectionHidden(c)
               for c in range(tbl.columnCount())), "至少得留一列"


# --------------------------------------------------------------------- #
# 5) 不喜欢不能污染图库
# --------------------------------------------------------------------- #
@pytest.fixture
def task(tmp_config):
    db = Database(tmp_config.db_path)
    return db, save_task(db, TaskConfig(
        id=None, adapter_name="generic", name="测站",
        output_dir=str(tmp_config.library_dir)))


def _pending(db) -> int:
    return len(db.query(
        "SELECT id FROM images WHERE in_quarantine=1 AND needs_review=1") or [])


def _seed(tmp_config, tid, rel_name: str, payload: bytes = b"1" * 64) -> tuple:
    """在待审核目录造一条待审核记录，返回 (db, iid, Path(文件))。"""
    db = Database(tmp_config.db_path)
    p = Path(tmp_config.review_dir)
    p.mkdir(parents=True, exist_ok=True)
    f = p / rel_name
    f.write_bytes(b"\xff\xd8\xff\xe0" + payload)
    iid = upsert_image(db, ImageRecord(
        id=None, source_url=f"https://x.com/{rel_name}", task_id=tid,
        path=str(f), tags=[], in_quarantine=True, needs_review=True))
    return db, iid, f


def test_dislike_with_recycle_on_moves_the_file_to_the_trash(tmp_config, task):
    """「删除时先进回收站」开着 → 文件进隔离区(回收站)、记录保留、可还原。"""
    db, tid = task
    tmp_config.use_recycle = True
    db, iid, f = _seed(tmp_config, tid, "on.jpg")
    assert _pending(db) == 1

    from media_resource_manager.review.queue import ReviewQueue
    ReviewQueue(db, tmp_config.isolate_dir, tmp_config).apply(
        iid, ReviewDecision.DISLIKE)

    assert not f.exists(), "审核目录里的原文件应被搬进回收站"
    assert not list(Path(tmp_config.review_dir).glob("on.jpg"))
    left = list(Path(tmp_config.isolate_dir).glob("on*.jpg"))
    assert len(left) == 1, f"回收站里应有一份：{left}"
    assert int(db.query("SELECT needs_review FROM images WHERE id=?",
                        (iid,))[0]["needs_review"]) == 0, "应退出审核队列"
    assert int(db.query("SELECT in_quarantine FROM images WHERE id=?",
                        (iid,))[0]["in_quarantine"]) == 1, "应留在隔离区(回收站)"


def test_dislike_with_recycle_off_deletes_everything(tmp_config, task):
    """「删除时先进回收站」关着 → 文件直删 + DB 行一起删（不留在回收站）。"""
    db, tid = task
    tmp_config.use_recycle = False
    db, iid, f = _seed(tmp_config, tid, "off.jpg")

    from media_resource_manager.review.queue import ReviewQueue
    ReviewQueue(db, tmp_config.isolate_dir, tmp_config).apply(
        iid, ReviewDecision.DISLIKE)

    assert not f.exists(), "文件应被从磁盘删掉"
    assert not db.query("SELECT id FROM images WHERE id=?", (iid,)), \
        "DB 行应一并删除"
    assert not list(Path(tmp_config.isolate_dir).glob("off.jpg")), \
        "不该在回收站里留一份（开关是关的）"
    assert _pending(db) == 0


def test_dislike_never_leaves_a_copy_in_the_gallery(tmp_config, task):
    """用户报的「污染图库」：以前 DISLIKE 会把图搬回图库 → 图库里凭空多一张。"""
    db, tid = task
    tmp_config.use_recycle = True
    lib = Path(tmp_config.library_dir)
    lib.mkdir(parents=True, exist_ok=True)
    db2, iid, f = _seed(tmp_config, tid, "pollute.jpg")
    # 图库目录里本来就有一张同名/同内容的图，判完后不该冒出副本
    before = {x.name for x in lib.glob("*.jpg")}

    from media_resource_manager.review.queue import ReviewQueue
    ReviewQueue(db2, tmp_config.isolate_dir, tmp_config).apply(
        iid, ReviewDecision.DISLIKE)

    assert {x.name for x in lib.glob("*.jpg")} == before, \
        "「不喜欢」不该往图库里放任何东西"
    assert list(lib.glob("pollute(1).jpg")) == []


def test_like_still_lands_in_the_gallery(tmp_config, task):
    """别把「喜欢」也改坏了：它本来就该进图库。"""
    db, tid = task
    rd = Path(tmp_config.review_dir)
    rd.mkdir(parents=True, exist_ok=True)
    f = rd / "like.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0" + b"4" * 64)
    iid = upsert_image(db, ImageRecord(
        id=None, source_url="https://x.com/like.jpg", task_id=tid,
        path=str(f), tags=[], in_quarantine=True, needs_review=True))
    from media_resource_manager.review.queue import ReviewQueue
    ReviewQueue(db, tmp_config.isolate_dir, tmp_config).apply(
        iid, ReviewDecision.LIKE)
    assert not f.exists(), "「喜欢」应把文件从审核目录搬进图库"
    assert iid in [r["id"] for r in db.query(
        "SELECT id FROM images WHERE in_quarantine=0 AND blacklisted=0")]
