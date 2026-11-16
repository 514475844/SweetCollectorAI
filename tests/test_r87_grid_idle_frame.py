"""R87 护栏：#50「每张图之间有可见间距 + 边框」的**常驻**细边框。

背景：#50 让缩略图不再裁剪（竖图竖长条、横图横长条）之后，图在格子里
大小不一、有的贴边有的居中，一屏看过去分不清「这张到哪结束」。间距
（GRID_SPACING）只解决一半，还需要一条边把每张图框住 —— 这条边必须
**未选中也在**（选中框是 r85 的另一件事，答的是"选没选中"）。

⭐ 判据为什么用「A/B 对照 + 扫描线梯度」而不是"看一眼截图"：
  - 唯一变量 = 我们的委托（对照组换成原生 QStyledItemDelegate）；
  - 有边框 ⇒ 扫描线在边框那一行的亮度出现**局部突变**；没有就是一条平线。
  - 写死"必须是灰色 #a0a0a0"是快照式断言，换套皮肤就假红（本项目在
    r63/r68/r85 已经为快照断言付过三次学费）。

⚠ 另一条必须钉死的**关系**（不是数值）：常驻边框**不许画进最外 2px**。
  r85 的选中对比度判据量的是最外 2px 环带主色；常驻边框若落在那一圈，
  未选中/选中两态的环带主色就成了同一个色，对比度退化成 1.0 ⇒ 老护栏
  假红。所以这里断言「外圈主色与对照组一致」+「IDLE_INSET > 环带宽」。
"""
from __future__ import annotations

import ast
import io
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import (QApplication, QListWidget,  # noqa: E402
                               QListWidgetItem, QStyledItemDelegate)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TV = os.path.join(ROOT, "media_resource_manager", "gui", "table_view.py")


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _arr(pm):
    from PIL import Image
    from PySide6.QtCore import QBuffer, QIODevice
    import numpy as np
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    ok = pm.save(buf, "PNG")
    data = bytes(buf.data())
    buf.close()
    assert ok and data
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype("int64")


def _grid(qapp, delegate=True, n=2, icon=64):
    """造一个与产品同参的网格；``delegate=False`` = 对照组（原生委托）。

    ⚠ 格子**交错填纯白/纯黑**：边框色取自调色板的 Mid，而 Mid 在全量跑里
    会被前面的用例换掉（换皮肤/换调色板）⇒ 只放一种底色的判据是**皮肤相关**
    的，单跑绿、全量红（本项目踩过三次的同型坑）。放白+黑两种，
    Mid 不可能同时等于白和黑 ⇒ 至少一种看得到边框，判据与皮肤无关。
    """
    from media_resource_manager.gui.table_view import apply_grid_layout
    v = QListWidget()
    apply_grid_layout(v, icon)
    if not delegate:
        v.setItemDelegate(QStyledItemDelegate(v))
    for i in range(n):
        it = QListWidgetItem()
        pm = QPixmap(icon, icon)
        pm.fill(QColor(255, 255, 255) if i % 2 == 0 else QColor(0, 0, 0))
        it.setIcon(QIcon(pm))
        it.setText(f"格子{i}")
        v.addItem(it)
    v.resize(520, 420)
    v.show()
    for _ in range(8):
        qapp.processEvents()
    return v


def _scan(a, rect):
    """格子竖直中线上的亮度序列（从格子顶到图片区底部）。"""
    import numpy as np
    x, y, w, h = rect
    from media_resource_manager.gui.table_view import GRID_CAPTION
    h = max(4, h - GRID_CAPTION)
    col = a[y:y + h, min(a.shape[1] - 1, x + w // 2)]
    return (col[:, 0] * 0.299 + col[:, 1] * 0.587 + col[:, 2] * 0.114)


def _max_grad(a, rect):
    s = _scan(a, rect)
    import numpy as np
    return float(np.abs(np.diff(s)).max()), int(np.abs(np.diff(s)).argmax())


def _rect_of(view, row):
    r = view.visualRect(view.model().index(row, 0))
    return (r.x(), r.y(), r.width(), r.height())


# ----------------------------------------------------------- 结构层

def test_delegate_has_an_idle_frame_painter():
    from media_resource_manager.gui import table_view as tv
    assert hasattr(tv.GridSelectionDelegate, "_paint_idle_frame")


def test_paint_draws_the_idle_frame_when_not_selected():
    """未选中分支必须**真的去画**，不是直接 return。

    用 AST 找真调用（R63-D3 教训：注释里引用字面量会骗过全文搜索）。
    """
    src = open(TV, encoding="utf-8").read()
    tree = ast.parse(src)
    paint = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GridSelectionDelegate":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "paint":
                    paint = sub
    assert paint is not None
    calls = [n for n in ast.walk(paint)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_paint_idle_frame"]
    assert calls, "paint() 未选中时没画常驻边框（#50 只做了一半）"


def test_idle_frame_is_inset_farther_than_the_contrast_ring():
    """内缩必须 > r85 判据用的最外 2px 环带，否则老护栏会假红。"""
    from media_resource_manager.gui.table_view import GridSelectionDelegate as D
    assert D.IDLE_INSET >= 3, D.IDLE_INSET
    assert D.IDLE_PX >= 1


# ----------------------------------------------------------- 行为层（A/B）

def test_unselected_cells_show_a_frame_the_control_does_not(qapp):
    """⭐ 唯一变量 = 委托：有它才有那条边（同一控件上换委托，A/B 只差这一个变量）。

    ⚠ 为什么是「同一控件换委托」而不是「开两个网格互相比」：两个网格的
    滚动位置/焦点态/调色板可能不同，差值是**两个变量**叠出来的。同一控件
    换委托（r85 用的也是这招）能把变量压到一个。
    """
    import numpy as np
    from media_resource_manager.gui.table_view import (
        GridSelectionDelegate, GRID_CAPTION)

    v = _grid(qapp, delegate=True, n=2)
    a_with = _arr(v.viewport().grab())
    v.setItemDelegate(QStyledItemDelegate(v))
    for _ in range(6):
        qapp.processEvents()
    a_ctrl = _arr(v.viewport().grab())
    v.setItemDelegate(GridSelectionDelegate(v))

    best = 0.0
    best_row = None
    for row in range(v.count()):
        x, y, w, h = _rect_of(v, row)
        h_img = max(4, h - GRID_CAPTION)
        d = np.abs(a_with[y:y + h_img, x:x + w]
                   - a_ctrl[y:y + h_img, x:x + w]).max()
        if d > best:
            best, best_row = float(d), row
    assert best_row is not None
    assert best >= 25, (
        f"常驻边框看不见：与对照组最大差只有 {best:.0f}（阈值 25）")


def test_idle_frame_does_not_invade_the_outer_ring(qapp):
    """最外 2px 环带的主色必须与对照组一致 ⇒ r85 的选中判据不受影响。"""
    import numpy as np

    def _ring_dom(v):
        a = _arr(v.viewport().grab())
        x, y, w, h = _rect_of(v, 0)
        ring = np.concatenate([
            a[y:y + 2, x:x + w].reshape(-1, 3),
            a[y + h - 2:y + h, x:x + w].reshape(-1, 3)])
        q = (ring // 8 * 8).astype("int64")
        keys = q[:, 0] * 1000000 + q[:, 1] * 1000 + q[:, 2]
        vals, cnts = np.unique(keys, return_counts=True)
        top = int(vals[cnts.argmax()])
        return [top // 1000000, (top // 1000) % 1000, top % 1000]

    with_d = _ring_dom(_grid(qapp, delegate=True))
    ctrl = _ring_dom(_grid(qapp, delegate=False))
    assert max(abs(a - b) for a, b in zip(with_d, ctrl)) <= 8, (
        f"常驻边框画进了最外 2px（{with_d} vs 对照 {ctrl}）"
        " ⇒ r85 选中对比度判据会退化")
