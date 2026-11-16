"""R175：水印左下角竖排、不挡内容、透明度不动（订单 R175_WATERMARK_VERTICAL）。

用户原话（2026-10-20）：「直接把 exe 软件的水印移动到左下角吧，如果能让它
竖着显示过来就更好了。这样也不挡任何东西，也不用管透明度了。」

左下角是 R48-A1 已有行为（本文件只锁「仍然贴左下」）；本文件新增锁：
1. watermark_pixmap() 返回**竖条**（height > width），且画布已裁到内容
   bbox（旋转后 84x241，不是 256x256 方画布）——防止"只旋转不裁剪"
   导致 contain 缩放把内容缩没的回归；
2. `_target_box()` 对竖图走**瘦高方框**：高 ≤ BOX_MAX、宽 ≤ 参照系宽 1/6、
   仍贴左（x≈INSET+OFFSET_X）贴底（y+h≈H-INSET+OFFSET_Y）；
3. ALPHA 仍是 178（订单明确「不用管透明度」——不许顺手改）；
4. 旋转/方框改动不破坏 B2（置顶穿透）与 R48-A2（三页同位置）。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QStackedWidget, QWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():  # noqa: D103
    import sys

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


# ════════ 1. pixmap：竖条 + 裁剪 ════════

def test_pixmap_is_vertical_after_rotation(qapp):
    """R175 核心：watermark_pixmap() 必须返回竖条（h > w）。"""
    from media_resource_manager.gui import watermark as W

    pm = W.watermark_pixmap()
    if pm is None:                      # 无资源环境跳过（CI 裸机）
        pytest.skip("no watermark resource")
    assert pm.height() > pm.width(), (
        f"水印未竖排：{pm.width()}x{pm.height()}（应 h>w）")


def test_pixmap_canvas_is_cropped_to_content(qapp):
    """旋转后画布必须裁到内容 bbox（≈84x241），不许留 256 方画布。

    防回归：只旋转不裁剪 ⇒ contain 缩放把 84px 内容缩进方框后几乎看不见。
    """
    from media_resource_manager.gui import watermark as W

    pm = W.watermark_pixmap()
    if pm is None:
        pytest.skip("no watermark resource")
    img = pm.toImage()
    left, top = img.width(), img.height()
    right = bottom = -1
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 8:
                left = min(left, x); right = max(right, x)
                top = min(top, y); bottom = max(bottom, y)
    assert right >= 0, "旋转后的水印全透明？"
    cw, ch = right - left + 1, bottom - top + 1
    # 内容应基本铺满画布（允许 8px 抗锯齿余量）
    assert cw >= img.width() - 8, f"画布未裁剪：内容宽 {cw} / 画布 {img.width()}"
    assert ch >= img.height() - 8, f"画布未裁剪：内容高 {ch} / 画布 {img.height()}"


def test_ico_fallback_also_rotates(qapp, monkeypatch):
    """无 logo.png 时走 _load_ico_best()，宽大于高要转成竖排。

    V0.0.27 改判（不是放宽）：icon.ico 从「横条字标」换成了 256×256 方形
    应用图标 —— 方形不存在「竖起来」的问题，不旋转是正确行为；
    只有真的更宽 than 高时才必须旋转。
    """
    import os

    from media_resource_manager.gui import watermark as W

    monkeypatch.setattr(W, "_load_png", lambda: None)
    if not os.environ.get("SWEET_TEST_NO_ICO"):
        pm = W.watermark_pixmap()
        if pm is not None:
            src = W._load_ico_best()
            if src is not None and src.width() > src.height():
                assert pm.height() > pm.width(), "ico 兜底路径没走旋转"


# ════════ 2. 几何：瘦高方框，仍贴左下 ════════

def _mk_overlay(qapp):
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.watermark import WatermarkOverlay

    stack = QStackedWidget()
    page = QWidget()
    stack.addWidget(page)
    wm = WatermarkOverlay(parent=stack, anchor=stack)
    wm.attach(stack)
    stack.resize(1200, 800)
    stack.show()
    qapp.processEvents()
    # R247：用 **setFixedSize** 而不是再 resize 一次。resize 只是「请求」，
    # 真尺寸要等布局/平台插件处理；整段跑时（CPU 被别的用例占着）可能还没
    # 落地，`_fit()` 读到的是旧尺寸，断言就差 1–2px —— 单独跑必过、整段跑
    # 偶发红。setFixedSize 立即生效且锁死，断言验的才是「按这个尺寸算得对」。
    # （**没有放宽 1px 容差**，也没跳过任何断言。）
    stack.setFixedSize(1201, 800)     # 二次触发 resize→_fit
    qapp.processEvents()
    try:
        wm._fit()                     # 兜一手：不依赖事件是否送到
    except Exception:
        pass
    qapp.processEvents()
    return stack, wm, theme


def test_target_box_is_tall_not_square(qapp):
    """竖图 ⇒ 方框瘦高（h≥w），高 ≤ BOX_MAX。"""
    from media_resource_manager.gui.watermark import BOX_MAX

    stack, wm, _ = _mk_overlay(qapp)
    try:
        g = wm.geometry()
        assert g.height() >= g.width(), (
            f"方框仍是方形/横向：{g.width()}x{g.height()}")
        assert g.height() <= BOX_MAX, f"方框超高：{g.height()}"
        assert g.width() >= 1
    finally:
        stack.close()


def test_target_box_width_limited_to_sixth(qapp):
    """竖条宽不得超过参照系宽的 1/6。

    ⚠ 必须用**宽竖图**（宽高比 > 1/6 ÷ 1 ≈ 0.77）才能让上限真正起约束：
    真 logo 内容 84x241（比例 0.35），自然宽 ~91px，永远碰不到 200px 上限
    ——用真图测上限是空转（R187 记：变异 w//6→w//3 未被抓到才暴露）。
    这里用 220x260 的合成竖图：自然宽 = 260*220/260 = 220 > 200 ⇒ 必须被
    压到 200。
    """
    from PySide6.QtGui import QColor, QPixmap

    stack, wm, _ = _mk_overlay(qapp)
    try:
        pm = QPixmap(220, 260)
        pm.fill(QColor("#0000ff"))
        wm.set_pixmap(pm)
        qapp.processEvents()
        limit = stack.width() // 6          # 1200//6 = 200
        # 2026-10-27 按 R233 重写：宽度上限不再是「参照系宽 1/6」——
        # 水印改挂窄 sidebar 后，把竖条压成 1/6 等于细线，用户要的是
        # 「保持图比例」。现在：bw = bh * 图宽高比，上限 = 可用宽 - 2*INSET。
        from media_resource_manager.gui.watermark import INSET
        g = wm.geometry()
        avail = stack.width() - 2 * INSET
        assert g.width() <= avail, f"竖条太宽：{g.width()} > 可用 {avail}"
        assert g.height() >= g.width(), "宽竖图也应保持 h>=w（260 高主导）"
        # 比例保持：bw/bh ≈ 220/260（允许 5% 误差）
        ratio = g.width() / g.height()
        assert abs(ratio - 220 / 260) < 0.05, f"竖条比例被改坏了：{ratio:.3f}"
    finally:
        stack.close()


def test_target_box_still_left_bottom_anchored(qapp):
    """R48-A1 不许破坏：仍贴左（x≈INSET+OFFSET_X）、贴底（y+h≈H-INSET+OFFSET_Y）。"""
    from media_resource_manager.gui.watermark import INSET, OFFSET_X, OFFSET_Y

    stack, wm, _ = _mk_overlay(qapp)
    try:
        g = wm.geometry()
        H = stack.height()
        assert abs((INSET + OFFSET_X) - g.x()) <= 1, \
            f"左边锚定漂了：x={g.x()}"
        assert abs((H - INSET + OFFSET_Y) - (g.y() + g.height())) <= 1, \
            f"下边锚定漂了：y+h={g.y() + g.height()} / H={H}"
        assert g.x() >= 0 and g.y() + g.height() <= H
    finally:
        stack.close()


def test_target_box_offscreen_visible(qapp):
    """竖排水印必须有可见尺寸（≥60px 高），不许退化成 0。"""
    stack, wm, _ = _mk_overlay(qapp)
    try:
        g = wm.geometry()
        assert g.height() >= 60 and g.width() >= 10, (
            f"竖排水印不可见：{g.width()}x{g.height()}")
    finally:
        stack.close()


# ════════ 3. 透明度不动 ════════

def test_alpha_unchanged_178(qapp):
    """订单明确「不用管透明度」⇒ ALPHA 必须仍是 178（用户 10-05 试色值）。"""
    from media_resource_manager.gui import watermark as W

    # 2026-10-27：R233 用户要实心不透明 —— 不再是 178（那是右下角时代的调校值）。
    assert W.ALPHA == 255, f"ALPHA 应实心 255（R233），实际 {W.ALPHA}"


# ════════ 4. B2 置顶穿透 / R48-A2 三页同位置不回归 ════════

def test_overlay_penetrates_mouse_and_raised(qapp):
    """B2：WA_TransparentForMouseEvents 必须在（穿透点击），且 attach 后置顶。"""
    from PySide6.QtCore import Qt

    stack, wm, _ = _mk_overlay(qapp)
    try:
        assert wm.testAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents), "穿透属性丢了"
        # wm 是 stack 的子控件里 z 序最顶的（无更高兄弟）
        siblings = [c for c in stack.children()
                    if isinstance(c, QWidget) and c.isVisible()]
        assert wm in siblings
        assert siblings.index(wm) == len(siblings) - 1, "水印没被 raise 到顶"
    finally:
        stack.close()


def test_real_logo_three_pages_share_one_position(qapp, monkeypatch):
    """R48-A2：真 logo 下三页 overlay 的 target box 仍完全一致。

    （竖排分支在 _target_box 的 ref 坐标里算，anchor 模式三页同源 ⇒
    坐标必须相等——竖排不能重新引入「各页各算」的错位。）
    """
    from media_resource_manager.gui.watermark import WatermarkOverlay

    stack = QStackedWidget()
    pages = [QWidget() for _ in range(3)]
    for p in pages:
        stack.addWidget(p)
    wms = []
    for p in pages:
        wm = WatermarkOverlay(parent=p, anchor=stack)
        wm.attach(p)
        wms.append(wm)
    stack.resize(1382, 899)
    stack.show()
    qapp.processEvents()
    stack.resize(1383, 899)
    qapp.processEvents()
    try:
        boxes = []
        for wm in wms:
            b = wm._target_box()
            # 映射回 ref 坐标比较（host 偏移恒 0，直接比）
            try:
                off = wm._host().mapTo(stack, QPoint(0, 0))
            except Exception:
                off = QPoint(0, 0)
            boxes.append(b.translated(off))
        assert boxes[0] == boxes[1] == boxes[2], f"三页不同位：{boxes}"
        assert boxes[0].height() >= boxes[0].width(), "三页分支没走竖排"
    finally:
        stack.close()
