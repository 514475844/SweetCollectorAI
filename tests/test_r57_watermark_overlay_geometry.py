"""r57（2026-10-06 台式机真机 bug 修复）：水印浮层只占右下角一小块。

台式机真机反馈（下发 2026-10-06 01:30）：
1. 【阻断】所有按钮/菜单/底部统计文字只有鼠标悬停才显示；
2. 右下角 logo 水印巨大且错位，压住「实时抓到」面板；
3. 顶部菜单 / 底部统计文字有淡色重影。

台式机的定性：三个现象同源 —— 水印浮层**被按整个窗口尺寸绘制**了。
代码里确实如此：`WatermarkOverlay` 挂在 stack 上，`setGeometry(0,0,host.w,host.h)`
把**控件本身**铺满整窗，只在 paintEvent 里把 logo 画在右下角 —— 控件占满整窗，
就是一个覆盖整窗的浮层，会干扰兄弟控件重绘。

修法：控件几何 = 右下角小方框（≤BOX_MAX + 2*INSET），不再铺满 host。

本文件锁三点（像素级，不是"控件存在"）：
* 几何：浮层只占右下角小方框，绝不等于整窗；
* 像素：浮层画出来的像素**全部**落在右下角，左上/顶部一条都不许有；
* 真机：真 MainWindow 里水印浮层同样是右下角小方框（不是整窗）。
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


def _blue_pixmap(size=256):
    from PySide6.QtGui import QColor, QPixmap

    pm = QPixmap(size, size)
    pm.fill(QColor("#0000ff"))
    return pm


def _mk_stack(host=(1200, 800)):
    from PySide6.QtWidgets import QStackedWidget, QTableWidget, QTableWidgetItem, QWidget

    from media_resource_manager.gui import theme
    from media_resource_manager.gui.watermark import WatermarkOverlay

    stack = QStackedWidget()
    page = QWidget()
    from PySide6.QtWidgets import QHBoxLayout

    lay = QHBoxLayout(page)
    tbl = QTableWidget(4, 4)
    for r in range(4):
        for c in range(4):
            tbl.setItem(r, c, QTableWidgetItem(f"{r},{c}"))
    lay.addWidget(tbl)
    stack.addWidget(page)

    wm = WatermarkOverlay(parent=stack)
    wm.attach(stack)
    wm.set_pixmap(_blue_pixmap())
    return stack, wm, theme


def test_overlay_geometry_is_small_bottomright_box(qapp):
    """浮层控件几何 = 右下角小方框，绝不是整窗。"""
    # 【2026-10-07】用户新要求：水印相对右下角基准**向左上各偏 50px**
    # （watermark.OFFSET_X / OFFSET_Y = -50）。所以「贴边」不再是硬贴边，
    # 而是「贴边 + 已知偏移」—— 断言必须跟着产品走，别把需求当回归。
    # 这里**从模块读偏移量**而不是写死 -50：将来用户再调数值时，
    # 这条断言自动跟随，而不会又变成一次假失败（同一个错我今天已经犯过两次：
    # 快照式断言 → 功能一改就假失败）。
    from media_resource_manager.gui.watermark import (
        BOX_MAX, INSET, OFFSET_X, OFFSET_Y)

    stack, wm, _ = _mk_stack()
    try:
        stack.resize(1200, 800)
        stack.show()
        qapp.processEvents()
        stack.resize(1201, 800)          # 再触发一次 resize→_fit
        qapp.processEvents()

        g = wm.geometry()
        W, H = stack.width(), stack.height()
        assert g.width() < W and g.height() < H, (
            f"水印浮层又铺满整窗了（{g.getRect()} vs host {W}x{H}）")
        assert g.width() <= BOX_MAX + 2 * INSET
        assert g.height() <= BOX_MAX + 2 * INSET
        # ⚠ R48/A1：水印从**右下角改到左下角**（作业单 A1）。
        #   水平锚定因此从「右边 = W - INSET + OFFSET_X」变成
        #   「左边 = INSET + OFFSET_X」。OFFSET_X 也随之归零 —— 贴左时若
        #   沿用右下角那个 -50，x = INSET-50 = **-38 直接出屏**
        #   （#42「水印不见了」同型坑）。这里同样**从模块读偏移量**，
        #   用户将来再调数值，这条断言自动跟随。
        assert abs((INSET + OFFSET_X) - g.x()) <= 1, \
            f"左边锚定不对（期望 {INSET + OFFSET_X}，实际 {g.x()}）"
        assert abs((H - INSET + OFFSET_Y) - (g.y() + g.height())) <= 1, \
            f"下边锚定不对（期望 {H - INSET + OFFSET_Y}，实际 {g.y() + g.height()}）"
        # 偏移是「向内收」不是「出屏」：左/下边仍必须留在容器内
        assert g.x() >= 0, "水印左边跑出容器了"
        assert g.y() + g.height() <= H, "水印下边跑出容器了"
    finally:
        qapp.setStyleSheet("")


def test_overlay_pixels_only_in_bottomright(qapp):
    """像素级：浮层画出的像素全部落在**左下角**，顶部/右侧一条都不许有。

    ⚠ R48/A1：随几何一起从右下改左下（作业单 A1）。判据语义不变 ——
    「像素不许跑到另一侧去」，只是那一侧从「左/上」换成了「右/上」。
    """
    from media_resource_manager.gui.watermark import INSET

    stack, wm, theme = _mk_stack()
    try:
        qapp.setStyleSheet(theme.LIGHT_QSS)
        stack.resize(1200, 800)
        stack.show()
        qapp.processEvents()
        stack.resize(1201, 800)
        qapp.processEvents()

        img = stack.grab().toImage()
        W, H = img.width(), img.height()
        # 蓝色水印像素的包围盒
        minx, miny, maxx, maxy = 10 ** 9, 10 ** 9, -1, -1
        cnt = 0
        for y in range(H):
            for x in range(W):
                c = img.pixelColor(x, y)
                if c.blue() > 180 and c.red() < 90 and c.green() < 90:
                    cnt += 1
                    if x < minx: minx = x
                    if x > maxx: maxx = x
                    if y < miny: miny = y
                    if y > maxy: maxy = y
        assert cnt > 200, f"没渲染出蓝色水印像素（cnt={cnt}）"
        # 全部落在**左下角**区域内。
        # 区域大小按「BOX_MAX + 偏移」算，不写死数值 —— 写死就是快照式炸弹
        # （R47 教训：写死的值在产品一改需求就集体假失败，看着像真回归）。
        # 语义：像素不许跑到右边/顶部去。
        from media_resource_manager.gui.watermark import (
            BOX_MAX, OFFSET_X, OFFSET_Y)
        region_w = BOX_MAX + abs(OFFSET_X) + 60
        region_h = BOX_MAX + abs(OFFSET_Y) + 60
        assert maxx <= region_w, \
            f"水印像素跑到右边了 maxx={maxx} W={W} 阈值={region_w}"
        assert miny >= H - region_h, \
            f"水印像素跑到上边了 miny={miny} H={H} 阈值={H - region_h}"
        assert minx >= 0 and maxx <= W and maxy <= H
    finally:
        qapp.setStyleSheet("")


def test_mainwindow_watermark_is_not_fullwindow(qapp):
    """真 MainWindow：水印是**左侧导航栏里的一小块**，不铺满任何容器。

    2026-10-29 校正：R233 之后水印从「内容区（stack）左下角」搬到了
    **左侧导航栏（sidebar）自己的左下角** —— 也就是整个软件最左那一列。
    用户原话：「水印就在最左边，定死在那儿」。所以三条期望值同步改口径：
      · 参照系从 stack 换成 sidebar；
      · 左边距用 INSET（12，防裁的硬下限），不再是 R176 的 LEFT_INSET(4)；
      · 纵向不再锚「导航下方」，而是贴 sidebar 底（导航在上、水印在下）。
    与 r48 那组位置判据同口径，避免两套标准互相打架。
    """
    from media_resource_manager.gui.main_window import MainWindow
    from media_resource_manager.gui.watermark import BOX_MAX, INSET

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.resize(1382, 899)
        w.show()
        qapp.processEvents()
        wm = getattr(w, "_watermark", None)
        side = getattr(w, "sidebar", None)
        assert wm is not None and side is not None
        assert wm.parentWidget() is side, "水印应挂在左侧导航栏上（R233 单浮层）"
        g = wm.geometry()
        assert g.width() <= BOX_MAX + 2 * INSET and g.height() <= BOX_MAX + 2 * INSET, \
            f"真机水印浮层不是小方框：{g.getRect()}"
        assert g.width() < side.width() and g.height() < side.height(), \
            "真机水印浮层又铺满 sidebar 了"
        assert abs(INSET - g.x()) <= 2, \
            f"水印左边距 {g.x()}，应≈{INSET}（sidebar 宽 {side.width()}）"
        gap = side.height() - (g.y() + g.height())
        assert 0 <= gap <= INSET + 2, f"水印没贴 sidebar 底，还差 {gap}px"


    finally:
        try:
            w.close()
        except Exception:
            pass

def test_review_page_buttons_are_painted_without_hover(qapp):
    """现象 1 验收（R80c 改向）：不悬停时审核页的按钮就画出来了（像素级）。原主体是任务页工具条按钮，主单项3 撤掉那 4 个按钮后改看审核页；要锁的现象没变 —— 按钮不悬停也被画出来、没被水印浮层/透明 QSS 吃掉。

    台式机反馈「按钮只有鼠标悬停才显示」。这里把真窗口渲染成像素，检查
    顶部工具条那几个按钮的矩形里确实画出了按钮底色（紫色系），而不是一片
    空白/水印 —— 空渲染说明按钮没被画出来。不主张复现真机 GPU 层的问题，
    只锁「代码这一侧按钮是画出来的、没被浮层/透明 QSS 吃掉」。
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QPushButton

    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    # launch() 里才 apply_theme；直接 new MainWindow 不会上主题，这里补上，
    # 否则按钮是 Qt 默认灰底、测不出「有没有被画出来」。
    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.resize(1382, 899)
        w.show()
        qapp.processEvents()
        # R80c：主单项3 撤掉任务页工具条 4 按钮后，原筛选条件
        # （“新建”/“全部”/“编辑”）在全窗口已找不到可见按钮。
        # 现象不变、主体改向审核页（水印三页之一，浮层与按钮同区）。
        w.stack.setCurrentWidget(w._review_tab)
        qapp.processEvents()
        btns = [b for b in w._review_tab.findChildren(QPushButton)
                if b.isVisible() and b.text().strip()]
        assert btns, "审核页一个可见按钮都没有，测试前提不成立"
        img = w.grab().toImage()

        def _painted_frac(b):
            tl = b.mapTo(w, QPoint(0, 0))
            x0, y0 = tl.x(), tl.y()
            x1, y1 = x0 + b.width(), y0 + b.height()
            tot = hit = 0
            for y in range(max(0, y0), min(img.height(), y1)):
                for x in range(max(0, x0), min(img.width(), x1)):
                    c = img.pixelColor(x, y)
                    tot += 1
                    # 「有彩色」= 饱和度足够高（按钮底色紫/橙都是高饱和；
                    # 页面/水印底接近白灰则低饱和）—— 与具体主题色无关
                    sat = max(c.red(), c.green(), c.blue()) - min(
                        c.red(), c.green(), c.blue())
                    if sat > 40:
                        hit += 1
            return hit / max(1, tot)

        frac = max(_painted_frac(b) for b in btns)
        assert frac > 0.15, f"审核页按钮没画出来（最高底色占比仅 {frac:.2%}）"
    finally:
        try:
            w.close()
        except Exception:
            pass
        qapp.setStyleSheet("")


def test_mainpage_watermark_visible_bottomright(qapp):
    """任务页的水印仍然**真的画出来了**（明暗两种皮肤都要）。

    2026-10-29 校正：R233 把水印搬进左侧导航栏后，取证面也必须从 `stack`
    换成 `sidebar` —— 以前截内容区当然看不到它，那条「0px 不可见」是
    **取证位置错了**，不是水印消失（浅色皮肤也一样看得见）。
    判据保持原意：隐藏/显示各截一张，变化像素 >500，且集中在栏目左下。
    """
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    for dark in (False, True):
        qapp.setStyleSheet(theme.DARK_QSS if dark else theme.LIGHT_QSS)
        w = MainWindow()
        w._ensure_single_instance = lambda *a, **k: True
        try:
            w.resize(1382, 899)
            w.show()
            qapp.processEvents()
            w.stack.setCurrentIndex(0)          # 任务页（用户真机默认页）
            for _ in range(4):
                qapp.processEvents()
            side = w.sidebar
            a = side.grab().toImage()
            wm = w._watermark
            wm.hide()
            for _ in range(4):
                qapp.processEvents()
            b = side.grab().toImage()
            wm.show()
            for _ in range(4):
                qapp.processEvents()
            W, H = a.width(), a.height()
            changed = 0
            minx = miny = 10 ** 9
            for y in range(H):
                for x in range(W):
                    ca, cb = a.pixelColor(x, y), b.pixelColor(x, y)
                    if abs(ca.red() - cb.red()) + abs(ca.green() - cb.green()) \
                            + abs(ca.blue() - cb.blue()) > 3:
                        changed += 1
                        minx = min(minx, x)
                        miny = min(miny, y)
            assert changed > 500, f"dark={dark} 左侧水印不可见了（{changed}px）"
            assert minx <= 40, f"dark={dark} 水印不在左缘：minx={minx}（W={W}）"
            g = wm.geometry()
            assert miny >= g.y() - 2, \
                f"dark={dark} 变化像素跑到水印框上方了：miny={miny} vs 框顶 {g.y()}"
        finally:
            try:
                w.close()
            except Exception:
                pass
        qapp.setStyleSheet("")
