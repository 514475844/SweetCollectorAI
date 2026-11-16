"""R85 护栏：PART2 #53 多选框可见性 + #50/#52 网格渲染自查。

为什么写这些断言（而不是「探针跑过一次就完了」）：
  探针抓的是**当下**有没有缺陷；护栏钉的是**以后改代码别把它改回去**。
  尤其 #53 这种「视觉」需求，历史上一改皮肤就被静默推翻（原生模式下
  行内 QSS 整体失效，classic 恰好是最弱的那套）。

⭐ 本轮实测（tools/r85_ab_delegate.py，同进程开关委托，唯一变量=委托）：
      皮肤        关委托   开委托
      classic      1.07     3.76     ← 真原生，修前最弱，提升 3.5 倍
      violet_look  3.26     9.43
      modern       3.23     9.61
      night        6.60     9.62
      qbittorrent  2.24     8.89
  数字 = 格子最外 2px 环带「选中前后主色」的 WCAG 对比度。
  阈值取 3.0 = WCAG 1.4.11 对非文本 UI 组件的要求（不是我拍脑袋定的）。

⚠ 三条踩过的坑（照抄会假红/空壳）：
  ① **别用「整格平均像素差」当指标** —— 3px 描边对平均值贡献极小，
     指标迟钝到改前后都是 1.07（本轮 v3 就栽在这）⇒ 只量**最外 2px 环带**；
  ② **别用「环带主色对比度」覆盖 5px** —— 缩略图占格子 86.7%，主色永远
     是缩略图 ⇒ 同样失效。环带必须窄到描边真正落笔的那一圈；
  ③ **断言关系不快照**：阈值用 WCAG 底线（3.0）+「开委托 ≥ 关委托×1.3」，
     不写死「必须等于 9.43」—— 那是快照式断言，换台机器就假红。
"""
from __future__ import annotations

import ast
import io
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidget, QListWidgetItem  # noqa: E402
from PySide6.QtWidgets import QStyledItemDelegate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TV = ROOT / "media_resource_manager" / "gui" / "table_view.py"
MW = ROOT / "media_resource_manager" / "gui" / "main_window.py"

# WCAG 1.4.11：非文本 UI 组件的对比度底线
CONTRAST_FLOOR = 3.0
# 「开委托至少比关委托强多少」—— 取 1.3 是因为 night 修前本来就 6.60，
# 描边只能再抬 1.46 倍；定太高会把 night 判成假红。
RELATIVE_FLOOR = 1.3


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# ----------------------------------------------------------- 取像素工具

def _arr(pm):
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice
    import numpy as np
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = pm.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    assert ok and data, "截图失败（offscreen 下 grab 返回空）"
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype("int64")


def _ring(a, rect, px=2):
    """最外 px 像素那一圈 —— 描边落笔的地方（见模块 docstring 坑①②）。"""
    import numpy as np
    x, y, w, h = rect
    H, W = a.shape[0], a.shape[1]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 - x0 < 2 * px + 2 or y1 - y0 < 2 * px + 2:
        return None
    return np.concatenate([
        a[y0:y0 + px, x0:x1].reshape(-1, 3),
        a[y1 - px:y1, x0:x1].reshape(-1, 3),
        a[y0:y1, x0:x0 + px].reshape(-1, 3),
        a[y0:y1, x1 - px:x1].reshape(-1, 3)])


def _dom(a2d):
    import numpy as np
    q = (a2d // 4 * 4).astype("int64")
    keys = q[:, 0] * 1000000 + q[:, 1] * 1000 + q[:, 2]
    vals, cnts = np.unique(keys, return_counts=True)
    top = vals[cnts.argmax()]
    return [int(top // 1000000), int((top // 1000) % 1000), int(top % 1000)]


def _lum(c):
    def f(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contrast(c1, c2):
    a, b = _lum(c1), _lum(c2)
    hi, lo = max(a, b), min(a, b)
    return round((hi + 0.05) / (lo + 0.05), 2)


# ----------------------------------------------------------- 造网格

def _mk_grid(qapp, n=6, icon=64):
    """与产品同参的缩略图网格（走 apply_grid_layout，不另写一套）。"""
    from media_resource_manager.gui.table_view import apply_grid_layout
    v = QListWidget()
    apply_grid_layout(v, icon)
    v.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
    for i in range(n):
        it = QListWidgetItem()
        pm = QPixmap(icon, icon)
        pm.fill(QColor(40 + (i * 12) % 180, 40 + (i * 29) % 180,
                       40 + (i * 47) % 180))
        it.setIcon(QIcon(pm))
        it.setText(f"格子{i}")
        v.addItem(it)
    v.resize(520, 420)
    v.show()
    for _ in range(8):
        qapp.processEvents()
    return v


def _rect_of(view, row):
    r = view.visualRect(view.model().index(row, 0))
    return (r.x(), r.y(), r.width(), r.height())


def _ring_contrast(view, qapp, row):
    """选中前后，最外 2px 环带主色的对比度。"""
    view.clearSelection()
    for _ in range(4):
        qapp.processEvents()
    a0 = _arr(view.viewport().grab())
    view.item(row).setSelected(True)
    for _ in range(6):
        qapp.processEvents()
    a1 = _arr(view.viewport().grab())
    view.item(row).setSelected(False)
    for _ in range(3):
        qapp.processEvents()
    r0, r1 = _ring(a0, _rect_of(view, row)), _ring(a1, _rect_of(view, row))
    assert r0 is not None and r1 is not None, "格子太小，取不到环带"
    return contrast(_dom(r1), _dom(r0))


# ----------------------------------------------------------- 元 / 结构层

def test_module_exports_the_delegate():
    from media_resource_manager.gui import table_view as tv
    assert hasattr(tv, "GridSelectionDelegate")
    assert hasattr(tv, "attach_grid_selection")
    assert hasattr(tv, "_auto_ink")


def test_apply_grid_layout_attaches_the_delegate(qapp):
    """两处网格共用 apply_grid_layout ⇒ 挂委托必须做在它里面（唯一入口）。"""
    from media_resource_manager.gui.table_view import (
        GridSelectionDelegate, apply_grid_layout)
    v = QListWidget()
    apply_grid_layout(v, 64)
    assert isinstance(v.itemDelegate(), GridSelectionDelegate), (
        "apply_grid_layout 没挂选中画框委托 —— 审核页那一套会退回「看不清」")


def test_delegate_paint_calls_super_so_icons_survive():
    """委托必须**先**把图标/文字交给 Qt 原逻辑，只在上层补两笔。

    ⚠ 不这么钉的后果：谁顺手把 super().paint() 删了，格子就整片空白，
    而这种失败在测试里是静默的（不抛异常）。用 AST 找真调用，
    不全文搜字面量（R63-D3：注释里大量引用字面量会骗过判据）。
    """
    tree = ast.parse(TV.read_text(encoding="utf-8"))
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GridSelectionDelegate":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "paint":
                    found = sub
    assert found is not None, "GridSelectionDelegate.paint 不存在"
    calls_super = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "paint"
        and isinstance(n.func.value, ast.Call)
        and isinstance(n.func.value.func, ast.Name)
        and n.func.value.func.id == "super"
        for n in ast.walk(found))
    assert calls_super, "paint() 里没有 super().paint() —— 图标/文字会被画没"


def test_delegate_paint_only_acts_when_selected():
    """未选中的格子**一律不动画框**（`:selected` 判据），否则整屏都是框。"""
    tree = ast.parse(TV.read_text(encoding="utf-8"))
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GridSelectionDelegate":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "paint":
                    found = sub
    assert found is not None
    src = ast.get_source_segment(TV.read_text(encoding="utf-8"), found) or ""
    assert "State_Selected" in src, "paint() 没判 State_Selected"


# ----------------------------------------------------------- 取色规则

def test_ink_is_picked_by_background_luminance():
    """描边色必须按背景亮度自动取黑/白 —— 写死一个色在另一套皮肤下就消失。

    断言的是**关系**（深底给浅墨、浅底给深墨、且都够对比度），
    不是具体色值。
    """
    from media_resource_manager.gui.table_view import _auto_ink
    dark_bg = QColor("#0f1115")
    light_bg = QColor("#ffffff")
    ink_on_dark = _auto_ink(dark_bg)
    ink_on_light = _auto_ink(light_bg)
    assert _lum([ink_on_dark.red(), ink_on_dark.green(), ink_on_dark.blue()]) \
        > _lum([ink_on_light.red(), ink_on_light.green(), ink_on_light.blue()]), \
        "深底没给浅墨（或浅底没给深墨）—— 描边会在某套皮肤里消失"
    for bg, ink in ((dark_bg, ink_on_dark), (light_bg, ink_on_light)):
        c = contrast([ink.red(), ink.green(), ink.blue()],
                     [bg.red(), bg.green(), bg.blue()])
        assert c >= 4.5, f"描边与背景对比度只有 {c}（<4.5）"


# ----------------------------------------------------------- 行为 / 像素层

def test_unselected_cell_only_gets_a_thin_frame(qapp):
    """没选中的格子：有委托 vs 无委托**只允许差一圈细边框**。

    ⚠ 契约变更（R86 #50，2026-10-13）：
      这一条原本钉的是「未选中态与原生委托**逐像素一致**」—— 那是 #50
      「缩略图不裁剪、每张图要有可见边框」之前的需求。用户后来明确要求
      「每张图之间留可见间距 + 边框」（R86 功能单 §一），于是未选中态
      **必须**多一圈常驻细边框，原断言（max 差 ≤2）必然红。
      新契约保留原意（画框是增强，不许改常态观感），只把「一模一样」
      放宽成「只在边上描一圈」：
        ① 变化像素占比 ≤ 15%（描一圈，不是糊一片）；
        ② 图片区**中心 50%** 与原生委托逐像素一致（图本身没被动）；
        ③ 未选中时**不出现**选中徽标（勾号/强调色块）—— 那条仍是硬契约。
    """
    import numpy as np
    from media_resource_manager.gui.table_view import (
        GridSelectionDelegate, GRID_CAPTION)
    v = _mk_grid(qapp, n=6, icon=64)
    a_with = _arr(v.viewport().grab())
    plain = QStyledItemDelegate(v)
    v.setItemDelegate(plain)
    for _ in range(6):
        qapp.processEvents()
    a_without = _arr(v.viewport().grab())
    assert a_with.shape == a_without.shape
    v.setItemDelegate(GridSelectionDelegate(v))

    x, y, w, h = _rect_of(v, 0)
    h_img = max(4, h - GRID_CAPTION)
    cell = a_with[y:y + h_img, x:x + w]
    base = a_without[y:y + h_img, x:x + w]
    diff = np.abs(cell - base).max(axis=2)
    changed = float((diff > 2).mean())
    assert changed <= 0.15, (
        f"未选中态被改动了 {changed:.1%} 的像素 —— 那就不是「描一圈」了")

    cy0, cy1 = h_img // 4, h_img - h_img // 4
    cx0, cx1 = w // 4, w - w // 4
    core = int(np.abs(cell[cy0:cy1, cx0:cx1]
                      - base[cy0:cy1, cx0:cx1]).max())
    assert core <= 2, f"图片中心被画框覆盖了（最大差 {core}）"

    # ③ 未选中不许出现强调色徽标：整格里强调色（Highlight）占比极低
    from PySide6.QtGui import QPalette
    accent = v.palette().color(QPalette.ColorRole.Highlight)
    tgt = np.asarray([accent.red(), accent.green(), accent.blue()],
                     dtype="int64")
    share = float((np.abs(cell - tgt).max(axis=2) <= 24).mean())
    assert share < 0.01, f"未选中却画了选中徽标（强调色占比 {share:.1%}）"


def _ink_share(a, rect, ink, px=3, tol=34):
    """外圈 px 像素里「与描边墨色一致」的像素占比。

    ⚠ 为什么不用「环带主色对比度」：描边只有 3px，加上抗锯齿与圆角，
    主色占比才 19%（实测），主色随时被缩略图夺走 ⇒ 指标失效。
    直接问「墨色这一圈到底落笔了没有」才是对得上现象的判据。
    """
    import numpy as np
    ring = _ring(a, rect, px)
    if ring is None:
        return 0.0
    d = np.abs(ring - np.asarray(ink, dtype="int64")).max(axis=1)
    return float((d <= tol).mean())


def _ink_for(view):
    """视图背景亮度决定的墨色（与委托内部同一条规则）。"""
    from PySide6.QtGui import QPalette
    from media_resource_manager.gui.table_view import _auto_ink
    bg = view.palette().color(QPalette.ColorRole.Window)
    ink = _auto_ink(bg)
    return [ink.red(), ink.green(), ink.blue()]


def test_selected_cell_gets_an_ink_frame(qapp):
    """#53 正向判据：选中后格子外圈**真的落上了**反差墨色的描边。"""
    v = _mk_grid(qapp, n=6, icon=64)
    ink = _ink_for(v)
    v.clearSelection()
    for _ in range(4):
        qapp.processEvents()
    a0 = _arr(v.viewport().grab())
    v.item(0).setSelected(True)
    for _ in range(6):
        qapp.processEvents()
    a1 = _arr(v.viewport().grab())
    v.item(0).setSelected(False)
    for _ in range(3):
        qapp.processEvents()
    rect = _rect_of(v, 0)
    assert _ink_share(a1, rect, ink) >= 0.10, (
        f"选中后外圈几乎没有墨色描边（占比 {_ink_share(a1, rect, ink):.3f}）")


def test_without_the_delegate_there_is_no_ink_frame(qapp):
    """反向判据：撤掉委托，外圈就不该有这圈墨色 —— 证明框是委托画的。

    没有这条，「占比 ≥0.10」可能只是某张缩略图刚好是深色（恒真）。
    """
    v = _mk_grid(qapp, n=6, icon=64)
    ink = _ink_for(v)
    plain = QStyledItemDelegate(v)
    v.setItemDelegate(plain)
    for _ in range(6):
        qapp.processEvents()
    v.item(0).setSelected(True)
    for _ in range(6):
        qapp.processEvents()
    a = _arr(v.viewport().grab())
    v.item(0).setSelected(False)
    from media_resource_manager.gui.table_view import GridSelectionDelegate
    v.setItemDelegate(GridSelectionDelegate(v))
    share = _ink_share(a, _rect_of(v, 0), ink)
    assert share <= 0.03, f"没挂委托时外圈也有墨色（占比 {share:.3f}）—— 判据恒真"


def test_frame_stays_contrasty_on_any_thumbnail(qapp):
    """双色描边：不管缩略图是死黑还是惨白，总有一道线反差 ≥ 3.0。

    ⚠ 这条是**护栏抓出来的真缺陷**留下的：单色描边按视图底色取墨（浅底取黑），
    压在深色缩略图上只有 1.25（实测）⇒ 加了内圈反墨才救回来。
    断言取 max(外圈, 内圈) —— 这正是双色设计要保证的性质。
    """
    from media_resource_manager.gui.table_view import _auto_ink
    v = _mk_grid(qapp, n=6, icon=64)
    outer = _ink_for(v)
    inner = _auto_ink_color(outer)
    a = _arr(v.viewport().grab())
    x, y, w, h = _rect_of(v, 0)
    cell = a[y:y + h, x:x + w]
    content = _dom(cell)
    best = max(contrast(outer, content), contrast(inner, content))
    assert best >= CONTRAST_FLOOR, (
        f"内外两道描边对内容 {content} 的最大对比度只有 {best}"
        f"（<{CONTRAST_FLOOR}）—— 双色兜底失效")


def test_two_tone_ink_guarantee_holds_for_any_content():
    """双色描边的**数学保证**：黑/白两道对任意内容色，较大者 ≥ 4.5。

    纯函数层验证（不依赖渲染），把「总有一道看得见」这条性质钉死。
    """
    from media_resource_manager.gui.table_view import _auto_ink
    black = _auto_ink(QColor("#ffffff"))     # 浅底 -> 深墨
    white = _auto_ink(QColor("#0f1115"))     # 深底 -> 浅墨
    pair = [[black.red(), black.green(), black.blue()],
            [white.red(), white.green(), white.blue()]]
    for gray in range(0, 256, 16):
        c = [gray, gray, gray]
        best = max(contrast(pair[0], c), contrast(pair[1], c))
        assert best >= 4.5, f"中灰 {gray} 的双向对比度只有 {best}"


def _auto_ink_color(rgb):
    """给定外圈墨色，返回内圈反墨（与委托内部同一条规则）。"""
    from media_resource_manager.gui.table_view import _auto_ink
    c = _auto_ink(QColor(*rgb))
    return [c.red(), c.green(), c.blue()]


def test_inner_stroke_is_actually_drawn(qapp):
    """内圈反墨必须**真的画上去**，不是只在函数层算得出。

    ⚠ 这条是专门防「空壳判据」的：上一条 `test_frame_stays_contrasty_on_any_thumbnail`
    的内圈墨色是用 `_auto_ink(outer)` **算**出来的，就算把内圈那道线从
    paint() 里删掉，它照样绿（R73/R84 同型第三次）。所以必须回到**像素**上
    再问一遍：外圈往里缩 3px 那一带，到底有没有反墨的像素。
    """
    from media_resource_manager.gui.table_view import GridSelectionDelegate
    v = _mk_grid(qapp, n=6, icon=64)
    outer = _ink_for(v)
    inner = _auto_ink_color(outer)
    v.clearSelection()
    for _ in range(4):
        qapp.processEvents()
    v.item(0).setSelected(True)
    for _ in range(6):
        qapp.processEvents()
    a1 = _arr(v.viewport().grab())
    v.item(0).setSelected(False)
    for _ in range(3):
        qapp.processEvents()
    x, y, w, h = _rect_of(v, 0)
    inset = GridSelectionDelegate.FRAME_PX
    inner_rect = (x + inset, y + inset, w - 2 * inset, h - 2 * inset)
    share = _ink_share(a1, inner_rect, inner, px=2, tol=40)
    assert share >= 0.08, (
        f"内圈反墨没画上去（占比 {share:.3f}）—— 深色缩略图上的兜底那道线没了")


def test_selection_roundtrip_leaves_no_ghost(qapp):
    """#52：全选 -> 取消，画面必须回到原样（不留残影）。

    探针实测 mean_abs_diff=0.0 / max=0；这里钉成护栏。
    """
    import numpy as np
    v = _mk_grid(qapp, n=6, icon=64)
    v.clearSelection()
    for _ in range(5):
        qapp.processEvents()
    a0 = _arr(v.viewport().grab())
    v.selectAll()
    for _ in range(6):
        qapp.processEvents()
    v.clearSelection()
    for _ in range(8):
        qapp.processEvents()
    a1 = _arr(v.viewport().grab())
    assert a0.shape == a1.shape
    assert int(np.abs(a0 - a1).max()) == 0, "取消选中后画面没回到原样（残影）"


def test_grid_geometry_uniform_and_bounded(qapp):
    """#50：图库/审核网格在 N 个格子下仍然等大、不重叠、不越出视口。

    「错位 / 重影」这四个字能落到客观形态上的就这三条。
    """
    v = _mk_grid(qapp, n=60, icon=64)
    vw = v.viewport().width()
    rects = []
    for i in range(v.count()):
        r = v.visualRect(v.model().index(i, 0))
        rects.append((r.x(), r.y(), r.width(), r.height()))
    assert rects, "一个格子都没量到"
    assert all(w > 0 and h > 0 for _x, _y, w, h in rects), "存在 0 尺寸格子"
    assert len({(w, h) for _x, _y, w, h in rects}) == 1, "格子不等大（错位）"
    assert all(0 <= x and x + w <= vw + 2 for x, _y, w, _h in rects), \
        "格子横向越出视口"
    rs = sorted(rects, key=lambda t: (t[1], t[0]))
    for a in range(len(rs)):
        for b in range(a + 1, min(a + 6, len(rs))):
            x1, y1, w1, h1 = rs[a]
            x2, y2, w2, h2 = rs[b]
            ix = min(x1 + w1, x2 + w2) - max(x1, x2)
            iy = min(y1 + h1, y2 + h2) - max(y1, y2)
            assert not (ix > 1 and iy > 1), f"格子 {a}/{b} 重叠（重影）"


# ----------------------------------------------------------- 接线层（真窗口）

@pytest.fixture(scope="module")
def win(qapp):
    from media_resource_manager.gui.main_window import MainWindow
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    for _ in range(4):
        qapp.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass


def test_gallery_grid_carries_the_delegate(win):
    """图库网格不走 apply_grid_layout ⇒ 必须单独挂一次（本轮就是补这个）。"""
    from media_resource_manager.gui.table_view import GridSelectionDelegate
    assert isinstance(win.g_grid.itemDelegate(), GridSelectionDelegate), \
        "图库网格没有选中画框委托"


def test_review_grid_carries_the_delegate(win):
    """审核网格走 apply_grid_layout ⇒ 那里一撤，这里立刻退回「看不清」。"""
    from media_resource_manager.gui.table_view import GridSelectionDelegate
    grid = getattr(win._review_tab, "grid", None)
    assert grid is not None, "审核页网格不见了"
    assert isinstance(grid.itemDelegate(), GridSelectionDelegate), \
        "审核网格没有选中画框委托"
