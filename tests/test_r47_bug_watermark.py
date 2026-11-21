"""R47 / BUG_REPORT_ORDER B1+B2：水印偏移 20px + 网格/列表看不见。

真机（2026-10-08，台式机 exe 14891518）两条反馈：
  B1 水印整体向下偏移约 20px；
  B2 审核/图库**网格**模式完全看不到水印，**列表**模式被内容盖住。

本文件是**先量后判**的像素级验收，不是"控件存在"那种假验收。
根因与修法（都写在 watermark.py 里，这里只钉住行为）：

* B2 真因：`attach()` 原来 `lower()`，靠「把 stack 后代 QWidget 刷
  transparent」让水印从内容底下透出来。实测**网格/列表页可见水印像素
  = 0**（任务页那格是 QTableWidget，恰好被 #43 穿透规则覆盖，所以
  「只有任务页看得见」）。网格是 `QListWidget + setItemWidget` 自绘条目
  + 每项自己的实底，透明 QSS 管不到。修法 = **置顶浮层 + 穿透点击**。
* B1 真因：`_target_box()` 用 `host.height()` 当底边，但 host 底边不是
  用户眼里的内容底边 —— 页面自己还有一条贴底的按钮条/状态文字。

⚠ 断言一律写「关系」不写「数值」：水印该多靠上取决于真机字体/DPI，
写死坐标就是自己给自己埋一颗快照式定时炸弹（同一个错本项目犯过多次）。

⚠ 2026-10-29（R233 适配）：水印已从「每页一个浮层」改成「左侧导航栏
sidebar 单浮层」，本文件里钉「审核页/各页有独立子水印」的结构断言已过时
（源码没变；同类 r50/r57 测试的处置见 docs/r246...md 第 36/40-42 行）。
两条过时断言改为钉单浮层在 sidebar 上的「置顶 + 贴左贴底 + 不出可视区」，
B2 像素主验收（test_watermark_visible_on_every_page）原样不动。
"""
import pytest

pytest.importorskip("PySide6")


@pytest.fixture
def qapp():  # noqa: D103
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _win(qapp, dark=False):
    """起真 MainWindow 并跑到某一页。返回 (window, stack)。"""
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.DARK_QSS if dark else theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    for _ in range(6):
        qapp.processEvents()
    return w, w.stack


def _settle(qapp, w, ix):
    """切到第 ix 页并让水印重新贴合。"""
    w.stack.setCurrentIndex(ix)
    for _ in range(8):
        qapp.processEvents()
    for wm in getattr(w, "_watermarks", []):
        wm._fit()
    for _ in range(4):
        qapp.processEvents()


def _painted_bbox(qapp, w):
    """(bbox, n)：当前页水印真正画出来的像素范围（隐藏水印做基线对比）。"""
    a = w.grab().toImage()
    for wm in getattr(w, "_watermarks", []):
        wm.hide()
    for _ in range(4):
        qapp.processEvents()
    b = w.grab().toImage()
    for wm in getattr(w, "_watermarks", []):
        wm.show()
    for _ in range(2):
        qapp.processEvents()

    minx = miny = 10 ** 9
    maxx = maxy = -1
    n = 0
    for y in range(a.height()):
        for x in range(a.width()):
            ca, cb = a.pixelColor(x, y), b.pixelColor(x, y)
            if (abs(ca.red() - cb.red()) + abs(ca.green() - cb.green())
                    + abs(ca.blue() - cb.blue())) > 3:
                n += 1
                minx = min(minx, x)
                maxx = max(maxx, x)
                miny = min(miny, y)
                maxy = max(maxy, y)
    return (minx, miny, maxx, maxy), n


# --------------------------------------------------------------------- B2

@pytest.mark.parametrize("ix", [0, 1, 2])
def test_watermark_visible_on_every_page(qapp, ix):
    """B2 主验收：三个页面上水印都真的画出来了（像素差 > 0）。

    修复前实测：页0 n=13948、**页1 n=0、页2 n=0**（网格/列表完全看不见）。
    """
    w, stack = _win(qapp)
    try:
        _settle(qapp, w, ix)
        bbox, n = _painted_bbox(qapp, w)
        assert n > 500, (
            f"页{ix} 看不见水印（可见像素仅 {n}）—— B2 未修复："
            f"网格/列表内容把 lower() 到底的水印盖死了")
        # ⚠ R48/A1：水印从右下角改到左下角（作业单 A1）。
        #   可见性判据（n > 500）**完全不变** —— 那是 B2 的核心；
        #   只有「落在哪个角」这一条跟着规格改。
        assert bbox[0] <= 420 and bbox[1] >= w.height() - 420, (
            f"页{ix} 水印不在左下角：bbox={bbox} 窗口={w.width()}x{w.height()}")
    finally:
        w.close()
        qapp.setStyleSheet("")


def test_watermark_is_topmost_not_lowered(qapp):
    """B2 结构性钉死：水印必须在 host 子控件的**最上层**。

    用 `lower()` 时水印是下标 0（最底）；置顶后它应该在最后一个。
    这条不依赖像素，比像素差更早、更准地定位回归。
    """
    w, stack = _win(qapp)
    try:
        _settle(qapp, w, 1)          # 审核页（网格）
        # R233 起水印只有一处，挂左侧导航栏 sidebar（跨页固定），不再给
        # 每页（含审核页）各挂一个浮层。原断言找 review_tab 的子水印是按
        # R47/R48 旧结构写的，已过时；见 docs/r246...md 第 36/40-42 行
        # 「水印代码一个字没动，现状即正确」的同类处置。
        wm = w._watermark
        host = wm.parentWidget()
        assert host is w.sidebar, (
            f"水印应挂左侧导航栏（R233 单浮层），实际 host={type(host).__name__}")
        kids = [k for k in host.children() if k.isWidgetType() and not k.isWindow()]
        # 用 `is` 扫下标：PySide6 的 `in`/`.index()` 会因 wrapper 身份不同
        # 误判「不在列表里」（实测踩过）。
        i_wm = next((i for i, k in enumerate(kids) if k is wm), -1)
        assert i_wm >= 0, "水印不是 host 的子控件"
        assert i_wm > 0, (
            "水印又在下标 0 —— 说明退回 lower() 了，B2 会复发")
    finally:
        w.close()
        qapp.setStyleSheet("")


def test_watermark_does_not_eat_mouse_clicks(qapp):
    """B2 的另一半：置顶**不能**挡住点击。

    作业单要求「置顶浮层 + WA_TransparentForMouseEvents」。置顶的水印如果
    吃鼠标事件，用户就会遇到「右下角那一块点不动」—— 比看不见更难排查。

    ⚠ 这里**只断言属性，不做「真点一下」**：`QT_QPA_PLATFORM=offscreen` 下
    窗口不做命中测试，`QTest.mouseClick` 投过去的事件压根不会派发给任何
    子控件（实测：连点击按钮自己，对照组也是空）。拿它当穿透证据等于
    自欺欺人。真正决定穿透的是那个属性，Qt 靠它做 `WA_TransparentForMouseEvents`
    命中测试，与平台无关。
    """
    from PySide6.QtCore import Qt

    w, stack = _win(qapp)
    try:
        for ix in (0, 1, 2):
            _settle(qapp, w, ix)
            for wm in w._watermarks:
                assert wm.testAttribute(
                    Qt.WidgetAttribute.WA_TransparentForMouseEvents), \
                    f"水印(host={type(wm.parentWidget()).__name__}) 必须 " \
                    f"WA_TransparentForMouseEvents，否则置顶会挡住点击"
                assert wm.testAttribute(
                    Qt.WidgetAttribute.WA_NoSystemBackground), \
                    "水印必须 WA_NoSystemBackground（否则是一块实底方块）"
                assert wm.focusPolicy() == Qt.FocusPolicy.NoFocus, \
                    "水印不该能吃焦点（否则置顶会把键盘焦点从按钮上抢走）"
    finally:
        w.close()
        qapp.setStyleSheet("")


# --------------------------------------------------------------------- B1

@pytest.mark.parametrize("ix", [0, 1, 2])
def test_watermark_above_bottom_strip(qapp, ix):
    """B1 精神延续 + R233 适配：水印必须留在 host 可视区内、贴底、不被缩小。

    R233 起水印只有一处，挂左侧导航栏 sidebar（R47/R48 时代是每页一个浮层、
    还要避开页内那条横贯底栏）—— 原「水印底边 ≤ 页内底栏顶」判据的前提已
    不存在：sidebar 没有那条底栏，水印由 `_place()` 贴 sidebar 底（见已适配的
    `test_r57_watermark_overlay_geometry.py::test_mainwindow_watermark_is_not_fullwindow`）。
    这里改成钉「在 host 内 + 贴左 + 贴底 + 尺寸合理」，语义仍是 B1 的
    「不许跑出可视区 / 不许被 _content_bottom 缩成一点」。
    """
    from media_resource_manager.gui import watermark as W

    w, stack = _win(qapp)
    try:
        _settle(qapp, w, ix)
        wm = w._watermark
        host = wm.parentWidget()
        assert host is w.sidebar, (
            f"水印应挂左侧导航栏（R233 单浮层），实际 host={type(host).__name__}")
        g = wm.geometry()
        host_h, host_w = host.height(), host.width()
        # 水印必须整个还在可视区内（不能为了避底栏自己跑出屏）
        assert 0 <= g.x() and g.x() + g.width() <= host_w, (
            f"页{ix} 水印横向跑出 sidebar：{g.getRect()} sidebar宽={host_w}")
        assert 0 <= g.y() and g.y() + g.height() <= host_h, (
            f"页{ix} 水印纵向跑出 sidebar：{g.getRect()} sidebar高={host_h}")
        # 贴左 + 贴底（R48-A1 的「左下角」语义，R233 后落到 sidebar）
        assert abs(g.x() - W.INSET) <= 2, (
            f"页{ix} 水印没贴 sidebar 左边：x={g.x()}（INSET={W.INSET}）")
        assert 0 <= host_h - (g.y() + g.height()) <= W.INSET + 2, (
            f"页{ix} 水印没贴 sidebar 底：底距={host_h - (g.y()+g.height())}")
        # 竖条分支宽可以很窄，但高必须够（旧断言 width>60 对竖条不适用）
        assert g.height() > 60, (
            f"页{ix} 水印被缩得过小（多半是 _content_bottom 判错了）：{g.getRect()}")
    finally:
        w.close()
        qapp.setStyleSheet("")


def test_content_bottom_ignores_structural_and_scrolled(qapp):
    """`_content_bottom()` 的两条防呆：结构容器与滚动超坐标控件都不算数。

    这两条都是实测踩出来的：页0 里一个 QSplitter(y=49,h=791) 被当成底栏，
    水印缩成 25x25；滚动区里的 TaskRow mapTo 出来 y=6000+，gap 是大负数，
    照样满足「贴底 <=40」。任一条退化都会让水印几何崩掉。
    """
    w, stack = _win(qapp)
    try:
        _settle(qapp, w, 0)
        wm = w._watermark
        host = wm.parentWidget()
        cb = wm._content_bottom()
        assert 0 < cb <= host.height(), f"_content_bottom 越界：{cb}"
        # 结构容器贴底不算
        assert cb > host.height() * 0.2, (
            f"_content_bottom={cb} 太小（host高 {host.height()}）—— "
            f"多半把 QSplitter/QScrollArea 当成底栏了")
        assert wm.width() > 60, (
            f"水印宽度 {wm.width()} 过小，_content_bottom 判错")
    finally:
        w.close()
        qapp.setStyleSheet("")
