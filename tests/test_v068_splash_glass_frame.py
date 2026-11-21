"""V0.0.68 护栏：启动画面加黑边框 + 磨砂玻璃质感。

用户原话「给整个这个加个黑边框，要不太不明显了，然后能不能把它做成磨砂玻璃的效果」。
只改观感，不动 logo 在上 / 转圈在下 / 3 秒收场这些既有约定。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _splash_fn() -> str:
    src = (ROOT / "media_resource_manager/gui/web_shell.py").read_text(encoding="utf-8")
    fn = src[src.find("def _show_splash"):]
    return fn[:fn.find("\ndef ")]


def test_splash_has_dark_border_color():
    src = (ROOT / "media_resource_manager/gui/web_shell.py").read_text(encoding="utf-8")
    assert "_C_SPLASH_BORDER" in src, "缺黑边框色常量"
    assert "_C_SPLASH_FROST" in src, "缺磨砂玻璃面色常量"


def test_splash_draws_border_then_frost():
    fn = _splash_fn()
    assert fn.index("_C_SPLASH_BORDER") < fn.index("_C_SPLASH_FROST"), \
        "必须先铺黑边框再画磨砂面，顺序反了描边会被盖掉"
    assert "drawRoundedRect(0, 0, W, H, 17, 17)" in fn, "黑边框没铺满"
    assert "drawRoundedRect(2, 2, W - 4, H - 4, 15, 15)" in fn, "磨砂面没内缩留边"


def test_splash_has_frost_grain_and_highlight():
    fn = _splash_fn()
    assert "grain" in fn and "drawPixmap(0, 0, grain)" in fn, "磨砂颗粒没叠上去"
    assert "Random(0x5EED)" in fn, "颗粒种子不固定，转圈时会闪"
    assert "QColor(255, 255, 255, 190)" in fn, "缺玻璃内高光"


def test_splash_keeps_logo_spinner_and_no_gradient_bar():
    """新效果不能破坏 V0.0.66 的布局约定。"""
    fn = _splash_fn()
    assert "QTimer" in fn and "drawArc" in fn, "旋转圈没了"
    assert "QRectF(dot_x" in fn, "旋转圈深色描点没了"
    assert "46, logo" in fn and "cx, cy, r = W // 2, 182" in fn, "logo/转圈上下布局被改"
    assert "QLinearGradient" not in fn, "顶部渐变装饰带不能加回来"
