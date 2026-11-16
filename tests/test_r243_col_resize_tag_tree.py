"""r243 护栏：列宽可拖 + 标签多级分类。

两条都是「纯前端、没有后端接口」的功能，最容易在下次改版里被整段删掉
而没有任何测试变红（界面还在，只是拖不动了 / 树变平了）。所以这里把
**行为契约**钉死在源码上：

列宽：
  · 窄屏必须交还给 CSS（行内 style 会盖掉媒体查询，手机上直接错位）；
  · 宽度必须落盘（刷新回默认值 = 用户白拖）；
  · 表头手柄不能占列宽（否则越拖越飘）。

标签树：
  · 「/」分层、父节点计数含子树、点父节点筛整棵子树；
  · 折叠状态用 Set 里存**全路径**，不能存短名（不同分支同名会一起收起）。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager/webapp/src"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ------------------------------------------------------------------ 列宽
def test_col_resize_module_contract():
    js = _read(SRC / "colResize.js")
    assert "export function useColResize" in js
    assert "localStorage" in js and "mrm.cols." in js
    # 落盘要带版本号：列数变了就整份丢弃，不能把旧数组硬套到新列上
    assert "v: 1" in js
    # 窄屏必须让位给 CSS
    assert "NARROW_PX" in js and "matchMedia" in js
    assert "if (narrow.value) return {}" in js
    # 最后一列用 minmax(,1fr)：窗口变宽不留一大片空白
    assert "minmax(" in js
    # 拖拽结束才落盘（不能 pointermove 每次都写 localStorage）
    assert "save()" in js and "pointerup" in js


def test_tasks_page_wires_resizable_columns():
    s = _read(SRC / "views/Tasks.vue")
    assert "useColResize('tasks'" in s
    assert ':style="gridStyle"' in s
    # 六列各一个手柄（首列复选框、名称/状态/进度三个排序头、入口、操作）
    assert s.count('class="rz"') == 6
    assert s.count("startResize(") == 6
    # 手柄不占列宽 → 绝对定位
    css = s[s.index(".rz {"):]
    assert "position: absolute" in css
    assert "col-resize" in css
    # 手机上隐藏手柄
    assert ".rz { display: none; }" in s


def test_col_resize_persists_per_page():
    js = _read(SRC / "colResize.js")
    # 每个页面一套键，任务页与以后别的表格互不覆盖
    assert "STORE_PREFIX + key" in js
    # 越界钳制：拖动不得把列拖成 0 宽（拖没了就再也抓不回来）
    assert "clamp(" in js and "min" in js and "max" in js


# ------------------------------------------------------------- 标签多级分类
def test_tag_tree_builds_from_slash_names():
    s = _read(SRC / "components/ImageGrid.vue")
    assert "tagTree" in s and "tagRows" in s
    assert "split('/')" in s
    # 父节点计数 = 子树累加（不是只算自己的张数）
    assert "child.n +=" in s
    # 折叠状态存全路径
    assert "tagCollapsed" in s and "k.path" in s
    # 点父节点筛整棵子树：后端 tags LIKE %词% 天然命中子标签（search.py:143）
    assert "tags LIKE" in (ROOT / "media_resource_manager/search.py").read_text(
        encoding="utf-8")
    # 2026-10-29（第 4 条）：不再单独设「清空筛选」—— 每个筛选下拉自带
    # 「全部任务 / 全部标签」首项，两个入口做同一件事会被问"这不是重复的吗"。
    assert "清空筛选" not in s


def test_tag_dropdown_keeps_counts_and_indent():
    """标签树进下拉后，层级/计数靠「全角空格缩进 + 括号计数」保留。

    2026-10-29（第 1 条）：用户要求把「按标签 / 全部标签」也搬到工具栏
    （与「全部任务」并排），左侧面板整块撤掉。树结构没丢 —— 计算仍在
    script 里，渲染层改成 option 文本缩进（半角空格会被 HTML 折叠，
    所以必须用 U+3000）。
    """
    s = _read(SRC / "components/ImageGrid.vue")
    assert "tagOptionLabel" in s
    assert "\\u3000" in s                # 全角空格缩进
    assert "tg.n" in s                    # 每项带聚合计数
    assert "tagRows" in s                 # 仍是树摊平后的行
    assert "class=\"tagsel\"" in s
