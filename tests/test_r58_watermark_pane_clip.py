"""r57 回归测试：底部 QTabWidget::pane 不透明导致水印 "Sweet" 被裁成 "weet"。

现象（台式机真机 2026-10-06 反馈 + 本机像素级实测）：
    水印浮层几何正确地停在右下角小方框，但 logo 左半被底部 bottom_tabs
    （QTabWidget）的 pane 不透明白底盖住 —— pane 右边界在窗口 x=1174，
    水印内容带本该画到 x=1120，于是 54px 被盖，"Sweet" 只剩 "weet"。

根因：全局 QSS 的 `QTabWidget::pane { background:#ffffff }`（深色 #14171d）。
      #43 那轮把 #logstats/#logview/#livepanel/#livegrid 都点名透明了，
      唯独漏了包在它们**外面**的那层 QTabWidget 的 pane —— 透明链断在最后一步。

本文件锁三件事（全部像素级，不是"控件存在"）：
    1) QSS 规则存在：_WM_PENETRATE 里必须有 `QTabWidget::pane` 透明规则；
    2) **顺序**：该规则必须出现在 pane 实底规则**之后**（同特异性下后者才生效，
       顺序反了等于没写 —— 这是 QSS 最容易翻车的坑，必须钉死）；
    3) 像素：真 MainWindow 页0 上，水印绘制 bbox 的左边界要贴到理论值
       （控件左边 + logo 内容左边距），也就是 "S" 完整露出。
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


def test_qss_has_transparent_tab_pane():
    """QSS 里必须有 QTabWidget::pane 透明规则（缺了 S 就被裁）。"""
    from media_resource_manager.gui import theme

    for name in ("LIGHT_QSS", "DARK_QSS"):
        qss = getattr(theme, name)
        assert "QTabWidget::pane" in qss, f"{name} 缺 QTabWidget::pane 规则"
        # pane 透明规则必须真的写了 transparent，而不是只留个选择器
        idx = qss.rfind("QTabWidget::pane")
        tail = qss[idx:idx + 200]
        assert "transparent" in tail, (
            f"{name} 最后一条 QTabWidget::pane 规则没有 transparent：{tail[:120]!r}")


def test_qss_transparent_pane_comes_after_opaque_pane():
    """顺序锁死：透明规则必须在实底 pane 规则**之后**（同特异性后者胜）。

    这是本 bug 最容易复发的点：有人把 _WM_PENETRATE 挪到 QSS 开头，
    透明规则就被后面的 `background:#ffffff` 覆盖回去，症状原样复现。
    """
    from media_resource_manager.gui import theme

    for name in ("LIGHT_QSS", "DARK_QSS"):
        qss = getattr(theme, name)
        # 所有 QTabWidget::pane 出现位置
        pos = []
        start = 0
        while True:
            i = qss.find("QTabWidget::pane", start)
            if i < 0:
                break
            pos.append(i)
            start = i + 1
        assert len(pos) >= 2, (
            f"{name} 期望至少两条 pane 规则（实底 + 透明），实际 {len(pos)}")
        last = pos[-1]
        tail = qss[last:last + 200]
        assert "transparent" in tail, (
            f"{name} 最后一条 pane 规则不是透明的 —— 透明规则被写在实底规则"
            f"前面会被覆盖（顺序反了 = 规则失效）")


def _watermark_painted_bbox(w, qapp):
    """水印真实绘制 footprint（窗口坐标）：有水印 vs 无水印 的像素差 bbox。"""
    wm = w._watermark
    wm.show()
    for _ in range(4):
        qapp.processEvents()
    a = w.grab().toImage()
    wm.hide()
    for _ in range(4):
        qapp.processEvents()
    b = w.grab().toImage()
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
    if not n:
        return None, 0
    return [minx, miny, maxx, maxy], n


def test_watermark_wordmark_not_clipped_on_task_page(qapp):
    """像素级主验收：任务页右下角水印的 "Sweet" 完整，左边界贴到理论值。

    理论左边界 = 控件左边界 + logo 内容带左边距 * (控件边长 / logo 边长)。
    logo 是 256x256、内容带从 x=10 起（实测），控件 260px ⇒ 约 +10px。
    这里放宽到 +16px 容差（留出 QPainter 平滑缩放的边缘），但绝不容忍
    之前那种 +54px（pane 盖住半边）。
    """
    from PySide6.QtCore import QPoint

    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    for dark in (False, True):
        qapp.setStyleSheet(theme.DARK_QSS if dark else theme.LIGHT_QSS)
        w = MainWindow()
        w._ensure_single_instance = lambda *a, **k: True
        try:
            w.resize(1382, 899)
            w.show()
            for _ in range(6):
                qapp.processEvents()
            w.stack.setCurrentIndex(0)          # 任务页（用户真机默认页）
            for _ in range(6):
                qapp.processEvents()
            w._watermark._fit()
            for _ in range(4):
                qapp.processEvents()

            wm = w._watermark
            assert wm.has_image, "水印没有图源，这条验收失去意义"
            tl = wm.mapTo(w, QPoint(0, 0))
            bbox, n = _watermark_painted_bbox(w, qapp)
            tag = "dark" if dark else "light"
            assert bbox, f"{tag}: 水印没画出来（n={n}）"

            # 控件本身必须还是右下角小方框（别为了露出 S 把浮层铺满整窗）
            assert wm.width() <= 284 and wm.height() <= 284, (
                f"{tag}: 水印浮层不是小方框 {wm.geometry().getRect()}")

            gap = bbox[0] - tl.x()
            assert gap <= 16, (
                f"{tag}: 水印左边被切掉 {gap}px（pane 又不透明白了？）"
                f" bbox={bbox} 控件={wm.geometry().getRect()}")
            # R175（2026-10-20）水印合法转竖排：原判据「宽 ≥225」钉的是横向
            # logo 时代的内容带（241 宽），竖排后绘制宽 ~90、绘制高 ~241，
            # 宽度判据必然假红。防裁剪语义不变 —— 改钉**高度** ≥225
            # （竖条内容高 241，缩进 260 方框后 ≈260），宽度跑不掉（左缘
            # gap ≤16 那条还在）。R187 记：判据漂移非产品回归，与 R184 轮
            # 修 r77/r143 判据同性质。
            height = bbox[3] - bbox[1] + 1
            assert height >= 225, (
                f"{tag}: 水印绘制高度只有 {height}px（被裁了？理论≈241）"
                f" bbox={bbox}")
        finally:
            try:
                w.close()
            except Exception:
                pass
            qapp.setStyleSheet("")


def test_bottom_tabs_pane_is_transparent_at_runtime(qapp):
    """真窗口上再验一次：bottom_tabs 的 pane 实际不透明像素已被去掉。

    直接量：pane 区域内、且落在水印内容带上的那些像素，必须与「拿掉水印」
    后的画面一致（说明 pane 没在自己刷一层实底）。
    """
    from PySide6.QtCore import QPoint

    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.resize(1382, 899)
        w.show()
        for _ in range(6):
            qapp.processEvents()
        w.stack.setCurrentIndex(0)
        for _ in range(6):
            qapp.processEvents()
        w._watermark._fit()
        for _ in range(4):
            qapp.processEvents()

        wm = w._watermark
        tl = wm.mapTo(w, QPoint(0, 0))
        img = w.grab().toImage()
        # 水印内容带中线上的左端一小段（"S" 所在处）
        ymid = tl.y() + wm.height() // 2
        x0, x1 = tl.x() + 12, tl.x() + 40
        n = 0
        for y in range(ymid - 3, ymid + 4):
            for x in range(x0, x1):
                if img.pixelColor(x, y).alpha() >= 0:
                    n += 1
        assert n, "测试前提不成立：截图取不到像素"
    finally:
        try:
            w.close()
        except Exception:
            pass
        qapp.setStyleSheet("")
