"""R134 护栏：图库网格「不能点击 / 渲染错位」修复。

根因（台式机侧只读锁定）：图库网格 `g_grid` 原用手动搭建
（`setViewMode/setIconSize/setResizeMode/setMovement`），**没调 apply_grid_layout**，
即既没有 `setGridSize` 也没有 `setSpacing`，也沒禁拖拽。审核网格走
`apply_grid_layout` 一切正常；图库因为 crop=False 的变高缩略图在 IconMode 下
缺少统一的 gridSize，布局不按均匀网格排布 —— 视觉上格子错位/重叠（渲染乱），
点击落点（按均匀网格预期）与 `itemAt` 映射错位（点不中）。

修复：把 `g_grid` 也交给 `apply_grid_layout(self.g_grid, 128)`（复用审核已验证代码）。

⚠ 关于"复现点不中"的判定（照实，不写假红）：
    静态 offscreen 下，对「每个 item 自己的 visualRect 中心」调 itemAt，
    旧配置反而 100% 命中（中心点永远落在自己格子里）。真正暴露的是——
    旧配置**不按均匀网格排布**（格子不等大、gridSize 空、spacing=0），
    于是用户按"均匀网格预期"点的位置去点，落点就和 itemAt 映射错位 ⇒ 点不中。
    所以复现用「均匀网格预期点」做 itemAt，旧配置大量 miss、修复后 0 miss，
    这才是对得上现象的物理判据（与 R133 同型坑：判据要对得上现象，别想当然）。
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QIcon, QPixmap  # noqa: E402
from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QListWidget, QListWidgetItem,
)

ROOT = Path(__file__).resolve().parents[1]
MW = ROOT / "media_resource_manager" / "gui" / "main_window.py"
TV = ROOT / "media_resource_manager" / "gui" / "table_view.py"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _varying_thumb(i: int) -> QPixmap:
    """不同宽高比的缩略图，模拟 crop=False 的变高原图。

    square / 竖图(128x320) / 横图(128x90) 三档轮换。
    """
    mod = i % 3
    if mod == 0:
        w, h = 128, 128
    elif mod == 1:
        w, h = 128, 320
    else:
        w, h = 128, 90
    pm = QPixmap(w, h)
    pm.fill(QColor((i * 37) % 255, (i * 91) % 255, (i * 53) % 255))
    return pm


def _fill(view: QListWidget, n: int) -> None:
    for i in range(n):
        it = QListWidgetItem()
        it.setIcon(QIcon(_varying_thumb(i)))
        it.setText("图%d" % i)
        view.addItem(it)


def _mk_old(qapp, n: int = 18) -> QListWidget:
    """精确复刻修前的手动搭建（缺 apply_grid_layout 那套几何）：

    setViewMode(IconMode) / setIconSize(128,128) / setResizeMode(Adjust) /
    setMovement(Static) —— 不设 gridSize、不设 spacing。
    """
    v = QListWidget()
    v.setViewMode(QListWidget.ViewMode.IconMode)
    v.setIconSize(QSize(128, 128))
    v.setResizeMode(QListWidget.ResizeMode.Adjust)
    v.setMovement(QListWidget.Movement.Static)
    _fill(v, n)
    v.resize(700, 900)
    v.show()
    for _ in range(12):
        qapp.processEvents()
    return v


def _mk_fixed(qapp, n: int = 18) -> QListWidget:
    """修后：走 apply_grid_layout（与审核页同一函数）。"""
    from media_resource_manager.gui.table_view import apply_grid_layout
    v = QListWidget()
    apply_grid_layout(v, 128)
    _fill(v, n)
    v.resize(700, 900)
    v.show()
    for _ in range(12):
        qapp.processEvents()
    return v


def _uniform_grid_click_miss(v: QListWidget, n: int) -> int:
    """模拟「用户按均匀网格预期位置点击」：第 i 个 item 应在
    (col*maxw, row*maxh) 的中心。统计 itemAt 命中错位 / 落空的数量。

    旧配置缺 gridSize ⇒ 实际布局 ragged，预期点大量 miss（点不中）；
    修复后布局是严格均匀网格 ⇒ 0 miss。
    """
    rects = [v.visualRect(v.model().index(i, 0)) for i in range(n)]
    maxw = max(r.width() for r in rects)
    maxh = max(r.height() for r in rects)
    cols = max(1, v.viewport().width() // maxw)
    miss = 0
    for i in range(n):
        col = i % cols
        row = i // cols
        exp = (col * maxw + maxw // 2, row * maxh + maxh // 2)
        hit = v.itemAt(*exp)
        if hit is None or hit is not v.item(i):
            miss += 1
    return miss


# ----------------------------------------------------------- 修复后判据

def test_fixed_every_cell_center_hits_its_item(qapp):
    """验收#2：修后每个 cell 的 visualRect 中心经 itemAt 都命中该 cell 对应 item。"""
    v = _mk_fixed(qapp, 18)
    miss = 0
    for i in range(v.count()):
        c = v.visualRect(v.model().index(i, 0)).center()
        if v.itemAt(c) is not v.item(i):
            miss += 1
    assert miss == 0, f"修后仍有 {miss} 格中心点不中对应 item"


def test_fixed_sets_uniform_grid_size_and_spacing(qapp):
    """apply_grid_layout 补上旧配置缺的几何：非空 gridSize + spacing>0。"""
    from media_resource_manager.gui.table_view import (
        GRID_CAPTION, GRID_SPACING)
    v = _mk_fixed(qapp, 6)
    gs = v.gridSize()
    assert not gs.isEmpty(), "修后 gridSize 竟然仍为空（几何没补上）"
    assert (gs.width(), gs.height()) == (128, 128 + GRID_CAPTION), \
        f"gridSize 不是 (128,{128 + GRID_CAPTION})，而是 {gs.width(), gs.height()}"
    assert v.spacing() == GRID_SPACING, \
        f"spacing 不是 {GRID_SPACING}，而是 {v.spacing()}"


# ----------------------------------------------------------- 复现（修前）

def test_old_style_uniform_grid_click_misses(qapp):
    """复现「点不中」：旧配置缺 gridSize，按均匀网格预期点击大量错位。

    ⚠ 不用「item 自身中心」做判据（那反而会 100% 命中、假绿），
    用「均匀网格预期点」才对得上用户真实点击落点偏的现象。
    """
    v = _mk_old(qapp, 18)
    n = v.count()
    miss = _uniform_grid_click_miss(v, n)
    assert miss >= n // 2, (
        f"修前旧配置按均匀网格预期点击只漏 {miss}/{n} —— 与'缺 gridSize 导致"
        f"点击落点错位'矛盾，需复核复现")


def test_old_style_lacks_grid_geometry(qapp):
    """根因结构性复现：旧配置 gridSize 空 + spacing=0（正是缺的那套几何）。"""
    v = _mk_old(qapp, 6)
    assert v.gridSize().isEmpty(), "修前 gridSize 竟然非空（与根因矛盾）"
    assert v.spacing() == 0, "修前 spacing 竟然非 0（与根因矛盾）"


def test_fixed_uniform_grid_click_no_miss(qapp):
    """对称保护：同一均匀网格预期点击，修后必须 0 漏。"""
    v = _mk_fixed(qapp, 18)
    miss = _uniform_grid_click_miss(v, v.count())
    assert miss == 0, f"修后均匀网格点击仍有 {miss} 漏（修复没生效）"


# ----------------------------------------------------------- 源码层（钉住产品改动）

def _build_gallery_tab_src():
    tree = ast.parse(MW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_gallery_tab":
            return ast.get_source_segment(MW.read_text(encoding="utf-8"), node) or ""
    return ""


def test_source_gallery_grid_uses_apply_grid_layout():
    """钉住产品改动：_build_gallery_tab 里 g_grid 改用 apply_grid_layout，
    且不再手动设那套会缺 gridSize 的几何。"""
    src = _build_gallery_tab_src()
    assert src, "_build_gallery_tab 没找到"
    assert "apply_grid_layout(self.g_grid, 128)" in src, \
        "_build_gallery_tab 没把 g_grid 交给 apply_grid_layout（修复没落地）"
    # 修前那四行手动几何已删除（尤其 setMovement Static，只在 g_grid 出现过）
    assert "self.g_grid.setMovement(QListWidget.Movement.Static)" not in src, \
        "g_grid 还在手动设 Movement —— 旧几何没清掉"
    assert "attach_grid_selection(self.g_grid)" not in src, \
        "g_grid 还在单独挂选中委托（apply_grid_layout 内部已挂，勿重复）"


def test_apply_grid_layout_unchanged_for_review(qapp):
    """回归保护：apply_grid_layout 是审核/图库共用函数，修图库不能改坏审核。

    审核网格走同一函数 ⇒ 仍挂 GridSelectionDelegate、仍设均匀 gridSize。
    """
    from media_resource_manager.gui.table_view import (
        GridSelectionDelegate, apply_grid_layout)
    v = QListWidget()
    apply_grid_layout(v, 160)
    assert isinstance(v.itemDelegate(), GridSelectionDelegate), \
        "apply_grid_layout 没挂选中委托（审核网格会退回'看不清'）"
    assert not v.gridSize().isEmpty(), "apply_grid_layout 没设 gridSize"
