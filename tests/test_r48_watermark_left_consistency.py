"""水印守卫 —— 2026-10-27 按 R233 现设计重写（旧文件已按两代前的设计写死）。

设计沿革（每代都是用户拍板的，别互相"修正"）：
    R48：右下角 → 左下角，三页各一个浮层 + anchor=stack。
    R175：logo 旋转 90° 变竖条，方框随图比例变瘦高。
    R176：可挂在某个控件正下方（below）。
    R233：**水印改挂左侧导航栏 self.sidebar**（跨页固定的一列），
          贴它左下角 = 全软件最左列；alpha 用户要**实心不透明 255**；
          stack/review/gallery 三处浮层删除。

所以本文件现在守的是：
    ① 恰好一个水印，挂在 sidebar 上（parent 与 anchor 都是它）；
    ② 贴 sidebar 左缘（x ≈ INSET）、贴 sidebar 底（y+高 ≈ 高-INSET）；
    ③ 竖条（高 > 宽）；
    ④ alpha 跟随 cfg（默认 255 实心）；
    ⑤ 切页 / 改窗口尺寸都不跳；
    ⑥ z 序置顶不退化。
关系断言为主，不写死像素坐标。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    import sys

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def win(qapp):
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.LIGHT_QSS)
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
    qapp.setStyleSheet("")


@pytest.fixture(autouse=True)
def _reset_shared_win(win, qapp):
    """共享窗口逐用例复位几何与当前页（防快照式串味）。"""
    yield
    try:
        win.resize(1382, 899)
        if getattr(win, "stack", None) is not None:
            win.stack.setCurrentIndex(0)
        for _ in range(3):
            qapp.processEvents()
    except Exception:
        pass


def _wm(win):
    wms = list(getattr(win, "_watermarks", []))
    assert wms, "窗口上没有水印浮层"
    return wms[0]


def _geo_in_sidebar(win):
    """水印在 **sidebar 坐标系** 里的矩形（真正该比的东西）。"""
    wm = _wm(win)
    host = wm.parentWidget()
    g = wm.geometry()
    return host.mapTo(win.sidebar, g.topLeft()), g.size()


# ============================================================ ① 结构

def test_exactly_one_watermark_on_sidebar(win):
    """R233：三处浮层删除，只剩挂在 sidebar 上的这一个。"""
    wms = list(getattr(win, "_watermarks", []))
    assert len(wms) == 1, f"应只有 1 个水印（R233 单浮层），实际 {len(wms)}"
    wm = wms[0]
    assert wm.parentWidget() is win.sidebar, "水印应挂在左侧导航栏上"
    assert getattr(wm, "_anchor", None) is win.sidebar, "anchor 应是 sidebar"


# ============================================================ ② 位置

def test_watermark_sits_at_left_edge(win):
    """贴 sidebar 左缘（x ≈ INSET），绝不在右半边。"""
    from media_resource_manager.gui.watermark import INSET

    pos, size = _geo_in_sidebar(win)
    assert abs(pos.x() - INSET) <= 2, f"水印左边距 {pos.x()}，应≈{INSET}"
    assert pos.x() < win.sidebar.width() / 4, "水印偏右，不在左列"


def test_watermark_sits_near_sidebar_bottom(win):
    """贴 sidebar 底部（导航按钮下方、隔离区那一条的下方）。"""
    from media_resource_manager.gui.watermark import INSET

    pos, size = _geo_in_sidebar(win)
    gap = win.sidebar.height() - (pos.y() + size.height())
    assert 0 <= gap <= INSET + 2, (
        f"水印底边距 sidebar 底 {gap}px，不像贴底（高 {win.sidebar.height()}）")


def test_watermark_is_vertical_strip(win):
    """R175：logo 旋转 90° 成竖条 —— 高必须大于宽。"""
    _, size = _geo_in_sidebar(win)
    assert size.height() > size.width(), (
        f"水印不是竖条（{size.width()}x{size.height()}）")


def test_watermark_inside_sidebar_bounds(win):
    pos, size = _geo_in_sidebar(win)
    assert pos.x() >= 0 and pos.y() >= 0, "水印跑出 sidebar 左/上边界"
    assert pos.x() + size.width() <= win.sidebar.width(), "水印超出 sidebar 右缘"
    assert pos.y() + size.height() <= win.sidebar.height(), "水印超出 sidebar 底缘"


# ============================================================ ④ 透明度

def test_alpha_follows_config_default_opaque(win):
    """R233：默认实心 255；控件值必须与 cfg 同步（滑条没连上也曾是坑）。"""
    wm = _wm(win)
    # 只断言「控件与 cfg 同步」——本机 config.json 里存过的历史值（如 178）
    # 是用户自己的数据，测试不能写死 255（新装默认才是 255）。
    assert wm._alpha == int(win.cfg.watermark_alpha), "控件 alpha 与 cfg 不同步"
    assert 0 <= wm._alpha <= 255, f"alpha 越界：{wm._alpha}"


# ============================================================ ⑤ 稳定性

def test_position_survives_page_switching(win, qapp):
    """水印挂的是跨页固定的 sidebar ⇒ 切页几何必须一动不动。"""
    seen = []
    for idx in range(win.stack.count()):
        win.stack.setCurrentIndex(idx)
        for _ in range(3):
            qapp.processEvents()
        seen.append(_geo_in_sidebar(win))
    assert len(set(seen)) == 1, f"切页时水印位置跳了：{seen}"


def test_position_survives_window_resize(win, qapp):
    """改窗口尺寸后：仍贴 sidebar 左缘+底部（attach 的 resize 转发没漏）。"""
    from media_resource_manager.gui.watermark import INSET

    win.resize(1100, 700)
    for _ in range(4):
        qapp.processEvents()
    pos, size = _geo_in_sidebar(win)
    assert abs(pos.x() - INSET) <= 2, "resize 后左边距丢了"
    gap = win.sidebar.height() - (pos.y() + size.height())
    assert 0 <= gap <= INSET + 2, "resize 后不再贴底"


# ============================================================ ⑥ z 序

def test_watermark_stays_raised(win, qapp):
    """不能退化回 lower()：在 sidebar 的子控件 z 序里必须仍在最后。"""
    wm = _wm(win)
    sibs = [c for c in win.sidebar.children()
            if getattr(c, "isWidgetType", lambda: False)()]
    assert sibs and sibs[-1] is wm, "水印不在 sidebar 子控件 z 序最上层"
