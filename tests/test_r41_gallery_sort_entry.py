"""r41 / D7：图库「分辨率」下拉改成筛选 + 排序共存的排序入口。

背景：顶部那排排序按钮（最新/站点/AI分/分辨率）整组撤掉了，排序能力改由
**表头点击（D8）** 和 **这个下拉的下半段** 两条路共同承担；上半段仍是原来的
分辨率筛选（按长边 max(宽,高) 分档）。一个下拉只能有一套 currentData，所以
userData 用两段标记区分：

- ``("f", 档位)``             —— 筛选项，全部分辨率 / 小图 / 中图 / 大图 / 超大图
- ``("s", "key:方向")``        —— 排序项，例：``("s", "score:desc")`` = AI 分降序

三条不变量（改动时别踩回去）：

1. 下拉里必须**同时**有筛选项和「排序：」项（分隔线隔开，不加分辨率列）；
2. 选排序项 → 排序键/方向跟着变，但**筛选档位被当成 all**（否则一选排序就
   被分辨率条件卡掉、图直接少一半）；
3. 选回筛选项 → 排序状态**不许被重置**（两个动作各管各的）。
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


def _items(box):
    return [(box.itemText(i), box.itemData(i)) for i in range(box.count())]


def test_res_box_carries_both_filter_and_sort():
    """上半段筛选、下半段排序，顺序不能反、档位不能丢。"""
    from pathlib import Path

    w = _win()
    try:
        box = w.g_res
        items = _items(box)
        filters = [d[1] for t, d in items if d and d[0] == "f"]
        sorts = [d[1] for t, d in items if d and d[0] == "s"]
        assert filters == ["all", "s", "m", "l", "xl"], f"筛选档位变了: {filters}"
        assert sorts, "下半段排序项没了（顶部按钮撤了，这就是唯一入口了）"
        assert all(t.startswith("排序：") for t, d in items
                   if d and d[0] == "s"), "排序项文字必须带『排序：』前缀"
        # 排序键覆盖 最新/AI分/站点/分辨率，且两个方向都要给得出来
        keys = {s.split(":")[0] for s in sorts}
        assert keys == {"new", "score", "site", "res"}, f"排序键不全: {keys}"
        dirs = {s.split(":")[1] for s in sorts}
        assert dirs == {"asc", "desc"}, f"方向不全: {dirs}"
        # 表格不能因此多出「分辨率」这一列
        src = (Path(__file__).resolve().parents[1] / "media_resource_manager"
               / "gui" / "table_view.py").read_text(encoding="utf-8")
        assert '"分辨率"' not in src, "不该加分辨率列"
    finally:
        w.close()


def test_sort_entry_sets_sort_state_and_drops_res_filter(qapp):
    w = _win()
    try:
        w.g_res.setCurrentIndex(0)                 # 全部分辨率
        w._gal_sort_mode, w._gal_sort_desc = "new", True
        idx = next(i for i in range(w.g_res.count())
                   if w.g_res.itemText(i).endswith("AI分 ↓"))
        w.g_res.setCurrentIndex(idx)
        assert w._gal_sort_mode == "score", "选排序项没改排序键"
        assert w._gal_sort_desc is True, "选 ↓ 项必须是降序"
    finally:
        w.close()


def test_filter_segment_keeps_sort_state(qapp):
    """选回筛选项时，排序状态不许被顺手重置。"""
    w = _win()
    try:
        idx = next(i for i in range(w.g_res.count())
                   if w.g_res.itemText(i).endswith("AI分 ↓"))
        w.g_res.setCurrentIndex(idx)
        assert w._gal_sort_mode == "score"
        w.g_res.setCurrentIndex(2)                 # 回到 中图 筛选
        assert w._gal_sort_mode == "score", "选筛选不该动排序"
        assert w._gal_sort_desc is True
    finally:
        w.close()


def test_sort_item_is_not_read_as_res_filter():
    """选排序项时，分辨率筛选条件必须降级成 all。

    这条用源码切块锁死：load_gallery 里那行解析是「排序项不当筛选用」的唯一
    保障，重构成别的写法（比如直接传 currentData）会在这里被抓出来。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "media_resource_manager"
           / "gui" / "main_window.py").read_text(encoding="utf-8")
    assert '_rd[0] == "f"' in src, "load_gallery 没把非筛选项当成 all，选排序会误筛"
    assert "self._on_gallery_res_box" in src, "下拉没接排序入口"
