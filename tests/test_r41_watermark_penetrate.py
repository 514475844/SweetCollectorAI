"""r41 / #43：表格页必须看得见水印 —— 像素级验收。

交接单原话：验收必须**像素级**（右下 vs 左上平均色差 >3），只看「控件存在」
等于没验。

根因回顾（写在这里防止以后被"大片白底"悄悄改回去）：
全局 QSS 里 `QWidget { background-color:#ffffff }`（深色模式 #0f1115）是通配
类型选择器，把 QStackedWidget 的各页、QTableWidget / viewport / item 全刷成
实心不透明色；而水印是 `WatermarkOverlay` 挂在 stack 上并 `lower()` 到最底，
一层实心子控件一盖，水印就成了"代码里有、界面上看不见"。
修法：stack 的**后代** QWidget + 表格整条链设 transparent，stack **自身**底色
保留（它才是水印的画布）。

本文件用「右下角 vs 左上角平均色差 > 3」来锁这件事：
* 穿透没修好 → 右下角被表格实心底盖死 → 色差 ≈ 0 → 测试红；
* 穿透修好了 → 水印透出 → 色差明显 > 3 → 测试绿。
"""
import pytest


# ---------------------------------------------------------------------------
# 构造：stack（含水印）+ 一张表格页，这就是「表格页看不见水印」的最小复现
# ---------------------------------------------------------------------------
def _blue_pixmap(size=256):
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor

    pm = QPixmap(size, size)
    pm.fill(QColor("#0000ff"))          # 纯蓝：白底上只要透出一点就差值很大
    return pm


def _build(qapp, apply_qss=True, size=(900, 620)):
    """造一棵 stack + 表格页 + 水印，返回 (stack, overlay)。"""
    from PySide6.QtWidgets import (QApplication, QHBoxLayout, QStackedWidget,
                                   QTableWidget, QTableWidgetItem, QWidget)

    from media_resource_manager.gui import theme
    from media_resource_manager.gui.watermark import WatermarkOverlay

    if apply_qss:
        qapp.setStyleSheet(theme.LIGHT_QSS)
    else:
        qapp.setStyleSheet("")

    stack = QStackedWidget()

    page = QWidget()                       # stack 页：QSS 会把它刷成不透明白底
    lay = QHBoxLayout(page)
    tbl = QTableWidget(6, 5)               # 表格铺满整页 —— 这就是"表格页"
    tbl.setHorizontalHeaderLabels(["a", "b", "c", "d", "e"])
    for r in range(6):
        for c in range(5):
            tbl.setItem(r, c, QTableWidgetItem(f"{r},{c}"))
    lay.addWidget(tbl)
    stack.addWidget(page)

    # 水印挂在 stack 上、lower() 到最底（和 main_window 里一模一样）
    wm = WatermarkOverlay(parent=stack)
    wm.attach(stack)
    wm.set_pixmap(_blue_pixmap())          # 图源固定，别让 logo 找不着的运气

    stack.resize(*size)
    stack.show()
    qapp.processEvents()
    # 再转一轮，确保 overlay 已经 _fit 成 stack 的尺寸（resize→show→paint）
    stack.resize(size[0] + 1, size[1])
    qapp.processEvents()
    return stack, wm


def _avg_rgb(img, x0, y0, x1, y1):
    """区域平均色（把 grab 出来的 QImage 扫一遍）。"""
    tot = [0, 0, 0]
    n = 0
    w, h = img.width(), img.height()
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0), min(w, x1)):
            c = img.pixelColor(x, y)
            tot[0] += c.red()
            tot[1] += c.green()
            tot[2] += c.blue()
            n += 1
    if not n:
        return (0, 0, 0)
    return (tot[0] // n, tot[1] // n, tot[2] // n)


def test_watermark_visible_over_table_page(qapp):
    """#43 主验收：表格页右下角的水印必须透出来（右下 vs 左上平均色差 > 3）。"""
    stack, wm = _build(qapp)
    try:
        assert wm.has_image, "水印没图源，这条验收就失去意义了"
        img = stack.grab().toImage()

        W, H = img.width(), img.height()
        # 水印几何和 watermark.py 里一致：右下角、四边留 12px、宽 ≤260
        ww = min(W // 3, 260)
        wh = ww
        # 左下角取一块和右下角同尺寸的区域做对照（同页、同底色、没水印）
        xr0 = W - ww - 12
        yr0 = H - wh - 12
        br = _avg_rgb(img, xr0, yr0, xr0 + ww, yr0 + wh)   # 右下：有水印
        bl = _avg_rgb(img, 12, yr0, 12 + ww, yr0 + wh)     # 左下：没水印
        diff = sum(abs(a - b) for a, b in zip(br, bl)) / 3
        assert diff > 3, f"表格页水印看不见：右下{br} vs 左下{bl} 平均色差仅 {diff:.2f}"
    finally:
        qapp.setStyleSheet("")


def test_stack_page_and_table_are_transparent(qapp):
    """QSS 必须真的把 stack 页 / 表格链刷成 transparent（不是"我以为"）。

    Qt 没有「查 QSS 背景」的公开口子，所以这里用实测色来判：穿透生效时，
    表格空白处应当透出 stack 的白色底（255,255,255）；一旦被刷成实心底也
    还是 255 —— 所以这条主要靠上面那条像素验收兜，这里锁的是**规则存在**，
    免得以后有人手滑把这段 QSS 删了、测试只红上面那条不好定位。
    """
    from media_resource_manager.gui import theme

    qss = theme.LIGHT_QSS
    # 2026-10-06 修正：必须是**子选择器** `QStackedWidget > QWidget`（只命中
    # stack 的直接子页）。原来的后代选择器会把 stack 内所有后代控件（按钮/标签）
    # 一起刷透明，真机表现为「按钮/文字不悬停就不显示」。
    assert "QStackedWidget > QWidget" in qss, "缺 #43 的 stack 子页穿透规则"
    assert "QStackedWidget QWidget" not in qss, (
        "别再用后代选择器 —— 它会误刷后代控件透明（真机 bug 根因）")
    for sel in ("QTableWidget::item", "QTableWidget::viewport", "QTableView"):
        assert sel in qss, f"缺 #43 的表格穿透规则：{sel}"


def test_watermark_visible_even_without_penetrate_qss(qapp):
    """R47/B2 改判：拿掉穿透 QSS，水印**仍然**可见（这是新设计的必然结果）。

    ⚠ 这条原本叫 `test_penetrate_regressed_when_qss_removed`，断言的是
    「拿掉穿透 QSS → 水印就看不见（色差 <=3）」。它成立的前提是水印
    `lower()` 到最底、**靠 QSS 透明链**从内容底下透出来。

    R47 / BUG_REPORT_ORDER B2 已把这条路否掉：真机实测网格/列表页
    可见水印像素 = **0**（网格是 QListWidget+setItemWidget 自绘条目，
    透明 QSS 根本管不到），所以改成**置顶浮层 + 穿透点击**。
    置顶之后水印不再依赖任何 QSS 透明规则 —— 拿掉 QSS 当然照样可见，
    这正是我们要的「不靠脆弱的 QSS 链」。

    所以把断言**反过来钉**：拿掉 QSS 后水印必须**仍然可见**。
    这才是新架构的真不变量（顺带证明「可见性不依赖 QSS」）。
    """
    from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget
    from media_resource_manager.gui.watermark import WatermarkOverlay

    qapp.setStyleSheet("")            # 啥规则都不给 → 只有 Qt 默认（各平台不透明）

    stack = QStackedWidget()
    page = QWidget()
    page.setStyleSheet("background-color:#ffffff")   # 实底页，盖在下面
    stack.addWidget(page)
    wm = WatermarkOverlay(parent=stack)
    wm.attach(stack)
    wm.set_pixmap(_blue_pixmap())
    stack.resize(900, 620)
    stack.show()
    qapp.processEvents()

    img = stack.grab().toImage()
    ww = min(900 // 3, 260)
    wh = ww
    br = _avg_rgb(img, 900 - ww - 12, 620 - wh - 12, 900 - 12, 620 - 12)
    bl = _avg_rgb(img, 12, 620 - wh - 12, 12 + ww, 620 - 12)
    diff = sum(abs(a - b) for a, b in zip(br, bl)) / 3
    assert diff > 3, (
        f"拿掉穿透 QSS 后水印反而不不见了（色差 {diff:.2f}）—— "
        f"B2 要求水印置顶、可见性不依赖 QSS 透明链")
    qapp.setStyleSheet("")


@pytest.fixture
def qapp():  # noqa: D103
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app
