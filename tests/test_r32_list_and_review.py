"""r32 回归：入库时间列 / 表头三态排序 / 审核多选退出 / 自动补满 / 灯箱尺寸 / 任务后清理。

用户 2026-10-03 反馈的六条，逐条锁死：

1. 审核多选模式点完「喜欢」没退出多选 → ``_leave_multi_mode()`` 两条分支共用；
2. 审核页「自动补满」开关（判定完自动从队列续图填满本页，可关）；
3. 所有列表模式加「入库时间」列（库里是 UTC，显示要 +8h）；
4. 所有列表模式表头可点排序：一次正序、两次倒序、三次回默认；
5. 审核灯箱窗口大小能自由调（以前 sizeHint 被图片撑大）；
6. 任务跑完清垃圾 —— **只清软件自己产生的，绝不碰系统回收站 $Recycle.Bin**。
"""
import base64

import pytest

pytest.importorskip("PySide6")

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from media_resource_manager.db import Database
from media_resource_manager.models import ImageRecord, ReviewDecision
from media_resource_manager.gui import table_view as tv
from media_resource_manager.repo import upsert_image


def _app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def qapp():
    """建表格/灯箱都要 QApplication，否则 Qt 直接 fatal（连报错都不打）。"""
    return _app()


# 1×1 透明 PNG（不依赖 PIL，直接给 Qt 一张真图）
_PNG1x1 = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    b"z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


# --------------------------------------------------------------------- #
# 3) 入库时间：UTC → 东八区 + 相对时间 + 排序键
# --------------------------------------------------------------------- #
def test_collected_time_is_shown_in_local_time_not_utc():
    """库存的是 UTC（SQLite datetime('now')），直接显示会早 8 小时、半夜变"昨天"。

    ⚠ 2026-11-09 修：原来写死 "2026-10-03 02:00:00" —— 超过一个月后
    format_collected 走「只给日期」分支，时间部分整个消失，测试必红，
    和被测代码毫无关系（同文件的相对时间那条早已改过这个坑，这条漏了）。
    改成「当前 UTC 往前推 2 天、整点」，任何时候跑都成立。
    """
    from datetime import datetime, timedelta
    base = datetime.utcnow() - timedelta(days=2)
    base = base.replace(minute=0, second=0, microsecond=0)
    local = base + timedelta(hours=8)          # 被测代码的换算口径
    out = tv.format_collected(base.strftime("%Y-%m-%d %H:%M:%S"))
    assert local.strftime("%H:%M") in out, (
        f"UTC {base:%H:%M} 应显示成东八区 {local:%H:%M}，实际：{out}")
    assert local.strftime("%Y-%m-%d") in out, out


def test_collected_time_relative_part_for_recent_items():
    """相对时间必须是「从现在算」，不能写死日期。

    原来这里写死 "2026-10-03 02:00:00" 断言出现「刚刚/分钟前/小时前」——
    到了 10-04 那天这条记录真的变成「1 天前」，测试必红，而且和被测代码
    一点关系都没有（典型的过期测试）。改成「当前时间往前推 5 分钟」，
    任何时候跑都成立。
    """
    from datetime import datetime, timedelta
    now = datetime.now() - timedelta(minutes=5)
    out = tv.format_collected(now.strftime("%Y-%m-%d %H:%M:%S"))
    assert any(k in out for k in ("刚刚", "分钟前", "小时前")), out


def test_collected_time_only_shows_date_when_old():
    """超过一个月就只给日期，别再挂一串「x 天前」（那时候没意义）。"""
    assert tv.format_collected("2024-01-02 03:00:00").strip() == "2024-01-02"


def test_collected_time_empty_gives_dash():
    assert tv.format_collected(None) == "-"
    assert tv.format_collected("") == "-"


def test_collected_sort_key_is_epoch_seconds():
    old = tv.collected_sort_key("2024-01-01 00:00:00")
    new = tv.collected_sort_key("2026-10-03 00:00:00")
    assert new > old
    assert 1704067200 <= old <= 1704067201      # 2024-01-01T00:00:00Z 的 epoch


def test_collected_sort_key_of_garbage_is_zero():
    assert tv.collected_sort_key("不是时间") == 0.0
    assert tv.collected_sort_key(None) == 0.0


# --------------------------------------------------------------------- #
# 3) 三张表共用同一套表头
# --------------------------------------------------------------------- #
def test_every_list_view_has_the_collected_time_column():
    """图库列表 / 审核列表 / 隔离区三处共用 make_table+fill_table，改一处三处生效。"""
    assert tv.HEADERS[-1] == "入库时间"
    assert len(tv.HEADERS) == 8

    from datetime import datetime, timedelta
    _base = datetime.utcnow() - timedelta(days=2)
    _base = _base.replace(minute=0, second=0, microsecond=0)
    _local = _base + timedelta(hours=8)
    rows = [{
        "id": 1, "path": "/tmp/a.jpg", "task_id": None, "score": 0.5,
        "bytes": 1024, "width": 100, "height": 100,
        "source_url": "https://x.com/a",
        "collected_at": _base.strftime("%Y-%m-%d %H:%M:%S"),
    }]
    tbl = tv.make_table()
    shown, _ = tv.fill_table(tbl, rows, None)
    assert shown == 1
    assert tbl.columnCount() == 8
    hdr = [tbl.horizontalHeaderItem(c).text() for c in range(tbl.columnCount())]
    assert hdr[-1] == "入库时间"
    # 只锁「UTC → 东八区 +8h」这一段（时间部分换算后一致），
    # 相对时间（刚刚 / x 分钟前 / x 小时前）跟着跑测试的时间走，不能写死。
    # ⚠ 2026-11-09：原来写死 2026-10-03，超一个月后走「只给日期」分支必红。
    assert tbl.item(0, 7).text().startswith(_local.strftime("%Y-%m-%d %H:%M"))
    assert "前" in tbl.item(0, 7).text()


def test_three_list_views_all_use_the_shared_table():
    """防止以后有人给某个页面另起一张表、漏掉新列。"""
    import media_resource_manager.gui.main_window as mw
    import media_resource_manager.gui.review_tab as rt
    for mod in (mw, rt):
        src = Path(mod.__file__).read_text(encoding="utf-8", errors="ignore")
        assert "fill_table(" in src, f"{mod.__name__} 没走共用 fill_table"


# --------------------------------------------------------------------- #
# 4) 表头排序：点一次正序、两次倒序、三次回默认
# --------------------------------------------------------------------- #
def _sample_rows(tmp_path):
    a = tmp_path / "a.jpg"
    a.write_bytes(b"x" * 8)
    b = tmp_path / "b.jpg"
    b.write_bytes(b"x" * 8)
    return [
        {"id": 1, "path": str(a), "task_id": None, "score": 0.9,
         "bytes": 5 * 1024 * 1024, "width": 10, "height": 10,
         "source_url": "https://x.com/1", "collected_at": "2026-10-01 00:00:00"},
        {"id": 2, "path": str(b), "task_id": None, "score": 0.1,
         "bytes": 512 * 1024, "width": 10, "height": 10,
         "source_url": "https://y.com/2", "collected_at": "2026-10-03 00:00:00"},
    ]


def _ids(tbl):
    return [int(tbl.item(r, 0).data(Qt.ItemDataRole.UserRole))
            for r in range(tbl.rowCount())]


def _click(tbl, col):
    tbl.horizontalHeader().sectionClicked.emit(col)


def test_header_click_sorts_by_true_value_not_display_text(tmp_path):
    """「512 KB」和「1.2 MB」按文本排会把 512KB 排到 1.2MB 后面 —— 必须按字节数。"""
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)
    before = _ids(tbl)

    _click(tbl, 3)                                   # 大小列：正序
    assert _ids(tbl) == [2, 1], _ids(tbl)
    assert tbl.item(0, 3).text() == "512 KB"

    _click(tbl, 3)                                   # 两次：倒序
    assert _ids(tbl) == [1, 2], _ids(tbl)

    _click(tbl, 3)                                   # 三次：回默认
    assert _ids(tbl) == before


def test_sort_arrows_show_and_disappear(tmp_path):
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)
    assert tbl.horizontalHeaderItem(3).text() == "大小"

    _click(tbl, 3)
    assert "▲" in tbl.horizontalHeaderItem(3).text()
    assert "▼" not in tbl.horizontalHeaderItem(3).text()

    _click(tbl, 3)
    assert "▼" in tbl.horizontalHeaderItem(3).text()

    _click(tbl, 3)
    assert "▲" not in tbl.horizontalHeaderItem(3).text()
    assert "▼" not in tbl.horizontalHeaderItem(3).text()
    assert tbl.horizontalHeaderItem(3).text() == "大小"


def test_sort_by_collected_time_and_score(tmp_path):
    tbl = tv.make_table()
    tv.fill_table(tbl, _sample_rows(tmp_path), None)

    _click(tbl, 7)                                   # 入库时间：正序 = 老的在前
    assert _ids(tbl) == [1, 2]
    _click(tbl, 7)
    assert _ids(tbl) == [2, 1]

    _click(tbl, 4)                                   # AI分
    assert _ids(tbl) == [2, 1]
    _click(tbl, 4)
    assert _ids(tbl) == [1, 2]


def test_sorted_rows_keeps_given_order_when_none():
    rows = [{"id": 2}, {"id": 1}]
    assert [r["id"] for r in tv.sorted_rows(rows, None)] == [2, 1]


# --------------------------------------------------------------------- #
# 1) 审核：判完「喜欢」也要退出多选模式
# --------------------------------------------------------------------- #
@pytest.fixture
def tab(tmp_config):
    _app()
    db = Database(tmp_config.db_path)
    from media_resource_manager.gui.review_tab import ReviewTab
    t = ReviewTab(db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    t.reload()
    return t


@pytest.fixture
def pending_ids(tmp_config, tab):
    ids = []
    for i in range(6):
        p = tmp_config.library_dir / f"p{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
        ids.append(upsert_image(tab.db, ImageRecord(
            id=None, source_url=f"https://x.com/p{i}.jpg", task_id=1,
            path=str(p), tags=["cat"], in_quarantine=True, needs_review=True)))
    tab.reload()
    return ids


def test_multi_mode_exits_after_like(tab, pending_ids):
    """以前只有「不喜欢」会退出多选，「喜欢 / 跳过」走不到 → 点完喜欢还卡在多选。"""
    tab.toggle_multi_mode(True)
    assert tab._multi_mode is True
    tab.grid.selectAll()
    tab.decide(ReviewDecision.LIKE)
    assert tab._multi_mode is False, "点完「喜欢」必须退出多选模式"


def test_multi_mode_exits_after_dislike(tab, pending_ids):
    tab.toggle_multi_mode(True)
    tab._batch_decide_ids(pending_ids[:1], ReviewDecision.DISLIKE)

    tab.toggle_multi_mode(True)               # 重新进多选，验证「不喜欢」也退
    assert tab._multi_mode is True
    tab.grid.selectAll()
    tab.decide(ReviewDecision.DISLIKE)
    assert tab._multi_mode is False


# --------------------------------------------------------------------- #
# 2) 审核「自动补满」开关
# --------------------------------------------------------------------- #
def test_autofill_switch_writes_config(tab):
    tab.cb_autofill.setChecked(False)
    assert tab.cfg.review_auto_fill is False
    tab.cb_autofill.setChecked(True)
    assert tab.cfg.review_auto_fill is True


def test_autofill_on_refills_the_page(tab, pending_ids):
    tab._page_size = 3
    tab.reload(fill=True)
    before = set(tab._page_ids)
    assert len(before) == 3

    tab.cb_autofill.setChecked(True)          # 开关打开 → 判定后补满
    tab._batch_decide_ids([min(before)], ReviewDecision.LIKE)
    assert len(tab._page_ids) == 3, "判掉一张应从队列续一张，本页还是满的"


def test_autofill_off_keeps_the_original_batch(tab, pending_ids):
    """关掉自动补满：本页只减不补，方便「先挑喜欢的、剩下的全选点不喜欢」。"""
    tab._page_size = 3
    tab.reload(fill=True)
    before = set(tab._page_ids)
    assert len(before) == 3

    tab.cb_autofill.setChecked(False)
    tab._batch_decide_ids([min(before)], ReviewDecision.LIKE)
    after = set(tab._page_ids)
    assert min(before) not in after
    assert after == before - {min(before)}, "关掉自动补满后本页只减不补"


def test_reload_fill_switches_between_refill_and_keep(tmp_path, tab, pending_ids):
    """fill=True 才从队列续图；fill=False 只留本页原有这批。"""
    tab._page_size = 3
    tab.reload(fill=False)
    kept = set(tab._page_ids)
    assert len(kept) == 3

    tab.queue.apply_batch([min(kept)], ReviewDecision.LIKE)
    tab.reload(fill=False)
    assert set(tab._page_ids) == kept - {min(kept)}, "关掉补满后不该把队列后面的新图塞进来"

    tab.reload(fill=True)
    assert len(tab._page_ids) == 3, "fill=True 才会从队列续图把本页填满"


# --------------------------------------------------------------------- #
# 5) 灯箱：窗口尺寸能随意调（sizeHint 不能被图片撑大）
# --------------------------------------------------------------------- #
def test_lightbox_preview_is_the_self_redrawing_label(tmp_config, monkeypatch):
    from media_resource_manager.gui.lightbox import Lightbox
    from media_resource_manager.gui.table_view import PreviewLabel

    img = tmp_config.library_dir / "lb.png"
    img.write_bytes(_PNG1x1)

    calls = []
    monkeypatch.setattr(PreviewLabel, "setSource",
                        lambda self, pm: calls.append(pm), raising=False)

    lb = Lightbox([{"path": str(img), "width": 1, "height": 1,
                    "source_url": "https://x.com/1", "tags": [], "score": 0.5}], 0)
    assert isinstance(lb.img, PreviewLabel), "灯箱预览必须是 PreviewLabel"
    assert calls, "render 必须走 setSource（缩放交给 PreviewLabel 按窗口当前尺寸）"


def test_lightbox_size_hint_does_not_follow_the_image(tmp_config):
    """灯箱能缩到多大由窗口决定，不能被图片 sizeHint 撑出下限。"""
    from media_resource_manager.gui.lightbox import Lightbox
    from media_resource_manager.gui.table_view import PreviewLabel

    lb = Lightbox([], 0)
    assert isinstance(lb.img, PreviewLabel)
    assert lb.img.sizeHint().width() == 240


# --------------------------------------------------------------------- #
# 6) 任务后清理：只清软件自己的垃圾，绝不碰系统回收站
# --------------------------------------------------------------------- #
def test_tidy_never_touches_the_system_recycle_bin():
    """静态防线：docstring 之外不许出现系统回收站路径（整机所有文件，不可逆）。"""
    p = Path(__file__).resolve().parents[1] / "media_resource_manager" / "tidy.py"
    src = p.read_text(encoding="utf-8", errors="ignore")
    code = src.split('"""', 2)[-1]          # 砍掉模块 docstring，剩下的都是代码
    assert "Recycle" not in code and "$Recycle.Bin" not in code


def test_tidy_cleans_software_own_junk_only(tmp_config, tmp_path):
    from media_resource_manager.tidy import tidy_after_task
    db = Database(tmp_config.db_path)

    # 1) 孤儿文件：图库里的 0 字节残片 + .part（下载中断留下的）
    junk = tmp_config.library_dir / "junk.jpg"
    junk.write_bytes(b"")
    part = tmp_config.library_dir / "mid.part"
    part.write_bytes(b"part")
    keep = tmp_config.library_dir / "real.jpg"
    keep.write_bytes(b"\xff\xd8\xff\xe0" + b"9" * 64)

    # 2) 隔离区 _purged（软件自己「彻底删除」搬进去的）
    #    2026-11-05 起 tidy 尊重回收站保留期 recycle_keep_days（默认 30 天，
    #    期内文件是留给用户「捞回去」的，不删才是正确行为）。本测试要验证
    #    的是「过期清」，所以把保留期显式设 0 = 立即全清。
    tmp_config.recycle_keep_days = 0
    purged = Path(tmp_config.isolate_dir) / "_purged"
    purged.mkdir(parents=True, exist_ok=True)
    (purged / "gone.jpg").write_bytes(b"x")

    # 3) 图库里指向已消失文件的残留记录
    ghost = tmp_path / "ghost.jpg"
    upsert_image(db, ImageRecord(
        id=None, source_url="https://x.com/ghost.jpg", task_id=1,
        path=str(ghost), tags=[], in_quarantine=False, needs_review=False))

    stat = tidy_after_task(tmp_config, db, log=lambda s: None)

    assert not junk.exists(), "0 字节孤儿应被清掉"
    assert not part.exists(), ".part 残片应被清掉"
    assert keep.exists(), "正常图片绝不能被顺手清掉"
    assert stat["files"] >= 2, stat
    assert stat["purged"] >= 1, stat
    assert stat["rows"] >= 1, stat
    assert not (purged / "gone.jpg").exists()

    rows = db.query("SELECT id FROM images WHERE source_url=?",
                    ("https://x.com/ghost.jpg",))
    assert not rows, "指向已消失文件的残留记录应被清掉"


def test_tidy_keeps_your_own_pictures(tmp_config, tmp_path):
    """真图一张都不许动（用户自己攒的图库是最贵的资产）。"""
    from media_resource_manager.tidy import tidy_after_task
    db = Database(tmp_config.db_path)
    good = tmp_path / "mine.jpg"
    good.write_bytes(b"\xff\xd8\xff\xe0" + b"7" * 128)
    upsert_image(db, ImageRecord(
        id=None, source_url="https://x.com/mine.jpg", task_id=1,
        path=str(good), tags=[], in_quarantine=False, needs_review=False))
    stat = tidy_after_task(tmp_config, db)
    assert stat["rows"] == 0, stat
    assert good.exists()


def test_tidy_never_raises_on_a_broken_database(tmp_config):
    """清理是「顺手」做的：任何异常都得自己吞掉，不能把 UI 拖崩。"""
    from media_resource_manager.tidy import tidy_after_task

    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db gone")

        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    assert tidy_after_task(tmp_config, Boom()) == {"files": 0, "rows": 0, "purged": 0}
