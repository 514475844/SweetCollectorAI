"""R135 护栏：R134（网格统一 gridSize）与 R117-P2-7（缩略图等比不裁剪）的**交叉不变量**。

## 为什么要有这个文件

R117-P2-7 的诉求是「网格缩略图 = 原图整体等比缩小、完整可见、绝不裁方块、
绝不拉伸变形」。R121 落地时把两处缩略图都改成 `crop=False`（等比），
护栏 `tests/test_r117_remaining.py::test_thumb_sync/async_path_keeps_aspect`
钉的是**解码层**——`square_thumb_pixmap(crop=False)` 返回的 pixmap 尺寸对不对。

R134 为了修「图库网格点不中」，把 `g_grid` 交给
`apply_grid_layout(g_grid, 128)`，那里会 `setGridSize(128, 128+GRID_CAPTION)`
+ `setIconSize(128,128)`——**这是 Qt 的强制几何**。也就是说：

    解码层等比  →  [Qt IconMode + 固定 gridSize 强制渲染]  →  用户看到的格子

R134 的护栏钉的是**点击命中**，`_uniform_grid_click_miss`；R117-P2-7 的护栏钉的是
**解码 pixmap**。**中间那一段——Qt 拿到非方形 pixmap + 固定 gridSize 之后，
画到屏幕上到底还是不是等比——此前没有任何一条测试钉过。**

本机实测（.workbuddy/_r135_probe3.py，offscreen 真实渲染 + 像素扫描）：
竖图 400x1200→渲染 bbox AR 0.34（原图 0.33，误差 0.8%）、横图 4.00→4.00、
超宽 8.00→8.00 ⇒ 结论是**没有**被打回去。
但这条结论当时只活在一个临时探针里，下棒就丢。所以固化成护栏：
以后任何人改 `apply_grid_layout` 的 gridSize / iconSize、或改回 crop=True，
本文件会当场红。

## 判据设计上的三个坑（照实记，别重踩）

1. **别用"内容 bbox"量图标**：cell 底部有文字标签（GRID_CAPTION=28），
   拿"cell 内非白像素包围盒"会把竖图量成 122 宽 × 138 高（AR 0.88），
   凭空造出"变形"的假象（R135 探针 v1 踩过）。正解：**图标铺纯红 + item 不设文字**，
   这样 cell 内红像素 bbox 就是纯图标几何，无歧义。
2. **别写自定义 QStyledItemDelegate 去"精确绘制"**：R135 探针 v2 这么干，
   `QStyleOptionViewItem` + `icon.paint` 那一套直接 **Segmentation fault（EXIT=139）**。
   保持走 `apply_grid_layout` 的原生 delegate（`GridSelectionDelegate`）即可，
   它本来就能正常渲染。
3. **AR 容差别写死绝对值**：Qt 在 IconMode 下对非方形图标按 KeepAspectRatio
   绘制，1px 级取整误差正常（实测竖图 0.8%）。判据用**相对误差 <12%**
   （远超取整误差、又远小于"被拉成正方形"这种 100%+ 级变形），钉行为不钉数值。

## 与既有护栏的分工

- `test_r117_remaining.py`：解码层（pixmap 尺寸）
- `test_r134_gallery_grid_click.py`：点击命中层（itemAt）
- **本文件**：渲染层（屏幕上真正画出来的宽高比）—— 补上这两层之间的洞
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QListWidget, QListWidgetItem,
)

ROOT = Path(__file__).resolve().parents[1]

# 明显竖图 / 横图 / 正方 / 超宽 —— 覆盖极端宽高比，最大化"被裁/被拉"的可观测性
SPECS = [
    ("portrait", 400, 1200),      # AR 0.33
    ("landscape", 1600, 400),     # AR 4.00
    ("square", 600, 600),         # AR 1.00
    ("ultrawide", 2400, 300),     # AR 8.00
]

# 相对误差阈值：Qt 取整误差实测 ≤1%；"被拉成正方形"是 100%+ 级。
AR_TOLERANCE = 0.12


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _red_source(tmp_path: Path, name: str, w: int, h: int) -> Path:
    """铺**纯红**的图：任何裁切/拉伸都会直接反映在像素 bbox 上。"""
    from PIL import Image
    p = tmp_path / f"{name}.png"
    Image.new("RGB", (w, h), (255, 0, 0)).save(p)
    return p


def _red_bbox(img, rect):
    """在 rect 内扫描红像素的包围盒；找不到返回 None。"""
    minx = miny = maxx = maxy = None
    for y in range(max(0, rect.y()), min(img.height(), rect.y() + rect.height())):
        for x in range(max(0, rect.x()), min(img.width(), rect.x() + rect.width())):
            c = img.pixelColor(x, y)
            if c.red() > 170 and c.green() < 95 and c.blue() < 95:
                if minx is None:
                    minx, miny, maxx, maxy = x, y, x, y
                else:
                    minx = min(minx, x); miny = min(miny, y)
                    maxx = max(maxx, x); maxy = max(maxy, y)
    return None if minx is None else (minx, miny, maxx, maxy)


def _mk_grid(qapp, tmp_path: Path, *, crop: bool = False):
    """按图库现行配置搭网格（apply_grid_layout(128)），item **不设文字**。

    不设文字是为了让 cell 内红像素 bbox = 纯图标（见模块 docstring 坑①）。
    """
    from media_resource_manager.gui.table_view import (
        apply_grid_layout, square_thumb_pixmap,
    )
    v = QListWidget()
    apply_grid_layout(v, 128)
    v.setStyleSheet("QListWidget{background:#ffffff;}")
    v.resize(940, 720)
    v.show()
    for _ in range(10):
        qapp.processEvents()

    items = []
    for name, w, h in SPECS:
        src = _red_source(tmp_path, name, w, h)
        pm = square_thumb_pixmap(src, size=128, crop=crop)
        it = QListWidgetItem("")
        it.setIcon(QPixmap(pm) if not isinstance(QPixmap, type(pm)) else pm)
        v.addItem(it)
        items.append((name, it, w, h))
    for _ in range(10):
        qapp.processEvents()
    return v, items


# -------------------------------------------------- 交叉不变量（核心）

def test_rendered_icons_keep_aspect_ratio_under_fixed_gridsize(qapp, tmp_path):
    """★ R134 的固定 gridSize **没有**把 R117-P2-7 的等比显示打回去。

    真实渲染后逐格扫描红像素 bbox，比对"渲染 AR" vs "原图 AR"。
    钉行为（等比），不钉数值。
    """
    v, items = _mk_grid(qapp, tmp_path)
    try:
        img = v.viewport().grab().toImage()
        bad = []
        for name, it, w, h in items:
            ar_src = w / h
            rect = v.visualItemRect(it)
            bb = _red_bbox(img, rect)
            assert bb is not None, f"{name}: cell 内没扫到图标像素，判据失效"
            bw, bh = bb[2] - bb[0] + 1, bb[3] - bb[1] + 1
            ar_rendered = bw / bh
            err = abs(ar_rendered - ar_src) / ar_src
            if err > AR_TOLERANCE:
                bad.append(f"{name}: 原图AR={ar_src:.3f} 渲染AR={ar_rendered:.3f} "
                           f"误差{err:.1%} bbox={bw}x{bh}")
        assert not bad, (
            "网格缩略图被裁剪/拉伸变形（违反 R117-P2-7）：\n  " + "\n  ".join(bad))
    finally:
        v.close()


def test_grid_geometry_stays_uniform_after_r134(qapp, tmp_path):
    """钉住 R134 的根因修复没被回退：固定 gridSize + 非零 spacing 仍在。

    没有它们，crop=False 的变高缩略图会让 IconMode 行高按该行最高图浮动
    ⇒ 格子错位 ⇒ R134 的"点不中"复发。
    """
    v, _ = _mk_grid(qapp, tmp_path)
    try:
        gs = v.gridSize()
        assert not gs.isEmpty(), "gridSize 变空了 ⇒ R134 修复被回退（点不中会复发）"
        assert v.spacing() > 0, "spacing 归零 ⇒ R134 修复被回退"
        assert v.dragDropMode() == v.dragDropMode().NoDragDrop, "拖放没关掉"
    finally:
        v.close()


def test_click_still_hits_after_geometry_and_aspect_checks(qapp, tmp_path):
    """两层护栏的交点：既等比、又能点中。

    R134 钉点击、R117-P2-7 钉等比；这条确保「为修一个而坏另一个」会当场红。
    """
    v, items = _mk_grid(qapp, tmp_path)
    try:
        miss = []
        for name, it, w, h in items:
            if v.itemAt(v.visualItemRect(it).center()) is not it:
                miss.append(name)
        assert not miss, "格子中心点不中对应 item（点不中复发）：" + ", ".join(miss)
    finally:
        v.close()


# -------------------------------------------------- 负对照（证明判据真的会红）

def test_square_crop_would_be_caught_by_ar_judge(qapp, tmp_path):
    """负对照：若有人把 crop 改回 True（裁成方块），上面那条 AR 判据必须抓到。

    裁方块后竖图渲染成 128x128（AR 1.0），与原图 0.33 差 200% ⇒ 必红。
    """
    v, items = _mk_grid(qapp, tmp_path, crop=True)
    try:
        img = v.viewport().grab().toImage()
        caught = []
        for name, it, w, h in items:
            ar_src = w / h
            bb = _red_bbox(img, v.visualItemRect(it))
            if bb is None:
                continue
            bw, bh = bb[2] - bb[0] + 1, bb[3] - bb[1] + 1
            if abs((bw / bh) - ar_src) / ar_src > AR_TOLERANCE:
                caught.append(name)
        assert len(caught) >= 2, (
            f"负对照失效：crop=True 时只有 {caught} 被 AR 判据抓到，"
            "说明判据太松，抓不住真正的裁方块回归")
    finally:
        v.close()


def test_ar_judge_rejects_square_judge_when_icon_would_be_distorted(qapp):
    """判据本身的单位检验：给一个"被拉成正方形"的假 AR，判据必须判红。

    防止有人把 AR_TOLERANCE 调到 2.0 之类让整条护栏变成空壳。
    """
    ar_src = 0.33          # 竖图
    ar_distorted = 1.0     # 被拉成正方形
    err = abs(ar_distorted - ar_src) / ar_src
    assert err > AR_TOLERANCE, f"判据把 {err:.0%} 的变形当合格了，护栏已成空壳"


# -------------------------------------------------- 审核网格回归保护

def test_review_grid_same_invariant(qapp, tmp_path):
    """审核网格走同一 apply_grid_layout ⇒ 同样的等比不变量必须成立。

    R134 只改了图库 g_grid；这条确保审核网格没被牵连回归。
    """
    from media_resource_manager.gui.table_view import apply_grid_layout
    v = QListWidget()
    apply_grid_layout(v, 160)          # 审核用 160，与图库 128 不同但同函数
    v.setStyleSheet("QListWidget{background:#ffffff;}")
    v.resize(900, 700)
    v.show()
    for _ in range(10):
        qapp.processEvents()

    from media_resource_manager.gui.table_view import square_thumb_pixmap
    items = []
    for name, w, h in SPECS:
        src = _red_source(tmp_path, "rev_" + name, w, h)
        pm = square_thumb_pixmap(src, size=160, crop=False)
        it = QListWidgetItem("")
        it.setIcon(QPixmap(pm) if not isinstance(QPixmap, type(pm)) else pm)
        v.addItem(it)
        items.append((name, it, w, h))
    for _ in range(10):
        qapp.processEvents()

    try:
        img = v.viewport().grab().toImage()
        bad = []
        for name, it, w, h in items:
            ar_src = w / h
            bb = _red_bbox(img, v.visualItemRect(it))
            if bb is None:
                continue
            bw, bh = bb[2] - bb[0] + 1, bb[3] - bb[1] + 1
            err = abs((bw / bh) - ar_src) / ar_src
            if err > AR_TOLERANCE:
                bad.append(f"{name}: 原图AR={ar_src:.3f} 渲染AR={bw / bh:.3f}")
        assert not bad, "审核网格等比被破坏：\n  " + "\n  ".join(bad)
    finally:
        v.close()


# -------------------------------------------------- 真实调用链（源码层）

def _call_sites() -> dict:
    """找出**产品码里真实调用** square_thumb_pixmap 的地方。

    前面的用例都是测试自己造 pixmap 喂进去；真正的回归形态是有人把
    调用方的 ``crop=False`` 改回 ``crop=True``（R121 记的历史：异步路径
    曾长期与同步路径不一致，正是这么发生的）。这条从源码层钉住。
    """
    import ast
    out = {}
    for rel in ("gui/main_window.py", "gui/review_tab.py"):
        p = ROOT / "media_resource_manager" / rel
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name != "square_thumb_pixmap":
                continue
            kw = {k.arg for k in node.keywords}
            crop_kw = next((k.value for k in node.keywords if k.arg == "crop"), None)
            if crop_kw is None:
                out[f"{rel}:{node.lineno}"] = "NO_KW"          # 没显式传 ⇒ 用默认值
            elif isinstance(crop_kw, ast.Constant):
                out[f"{rel}:{node.lineno}"] = bool(crop_kw.value)
            else:
                out[f"{rel}:{node.lineno}"] = "DYNAMIC"
    return out


def test_product_callers_pass_crop_false_explicitly():
    """★ 产品码里每处网格缩略图调用都必须**显式** crop=False。

    默认值是 crop=True，所以"漏传"和"传 True"等价于回到裁方块。
    变异的现实形态（M2 就是）：有人把 crop=False 改回去 / 删掉。
    """
    sites = _call_sites()
    assert sites, "没扫到任何 square_thumb_pixmap 调用点（解析失败？）"
    bad = {k: v for k, v in sites.items() if v is not False}
    assert not bad, (
        "这些网格缩略图调用点没有显式 crop=False（会裁成方块，违反 R117-P2-7）：\n"
        + "\n".join(f"  {k} => crop={v}" for k, v in sorted(bad.items()))
        + f"\n全部调用点：{sorted(sites)}")


def test_square_thumb_pixmap_default_is_still_crop_true_for_others():
    """反向钉：默认仍是 crop=True（别的调用方依赖它）。

    若有人为图库方便把默认改成 crop=False，会静默改变其它调用方行为。
    """
    import inspect
    from media_resource_manager.gui import table_view
    sig = inspect.signature(table_view.square_thumb_pixmap)
    assert sig.parameters["crop"].default is True, (
        "square_thumb_pixmap 的 crop 默认值被改了（应为 True，"
        "只有网格两处显式传 False）")