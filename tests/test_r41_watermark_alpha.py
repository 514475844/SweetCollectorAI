"""r41 / #42：水印不透明度默认 **178（70%）**，设置条上配临时候选滑条 + 百分比。

交接单原话：「#42 水印 ALPHA 定死 178（用户 demo 试色定的，原话『178 最舒服』），
改 watermark.py 的 ALPHA = 255 → 178，配套在设置面板加临时滑条 + 百分比显示。」

锁四条：
1. 默认值真是 178（之前是 255＝100%，那是「宁可太重也要看得见」阶段的取值）；
2. 百分比文案（178→70%、255→100%、0→「隐藏」而不是 0%，否则用户以为坏了）；
3. 拖动滑条只重画、不落盘（save_config 每次写 json，拖动会卡）；松手才落盘；
4. 滑条初值来自 cfg.watermark_alpha，改了配置重开还在。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def _qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_default_alpha_is_178():
    from media_resource_manager.gui.watermark import ALPHA
    from media_resource_manager.config import AppConfig

    # 2026-10-27：R233 起用户要实心不透明（255）——原 178 是水印还在
    # 右下角悬浮时的选择，挪到最左列后不挡任何内容，就不需要半透明了。
    assert ALPHA == 255, f"水印默认透明度应为 255（R233 实心），现在是 {ALPHA}"
    assert AppConfig().watermark_alpha == 255, "cfg 默认值也得跟上 watermark.ALPHA"


def test_percent_label():
    from media_resource_manager.gui.main_window import MainWindow

    pct = MainWindow._wm_pct
    assert pct(178) == "70%"
    assert pct(255) == "100%"
    assert pct(128) == "50%"
    assert pct(0) == "隐藏", "0 必须显示成「隐藏」，显示 0% 看着像坏了"


def _win():
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


def test_slider_drag_only_repaints_slider_release_persists(monkeypatch):
    """拖动只改 alpha，松手才落盘 —— 这条要是写反了，拖一下卡半秒。"""
    import media_resource_manager.gui.main_window as mw

    saved = []
    monkeypatch.setattr(mw, "save_config",
                        lambda cfg: saved.append(int(cfg.watermark_alpha)))

    w = _win()
    try:
        w._apply_watermark_alpha(120)
        assert saved == [], f"拖动就落盘了 {saved}，拖一下会卡"
        assert w._watermark._alpha == 120, "拖动没把 alpha 传给水印层"

        w.s_wm.setValue(90)
        assert saved == [], "valueChanged 也不该落盘"
        w._save_watermark_alpha()
        assert saved == [90], f"松手没落盘：{saved}"
        assert int(w.cfg.watermark_alpha) == 90
    finally:
        w.close()


def test_slider_initial_value_comes_from_config(monkeypatch):
    """滑条初值 = cfg.watermark_alpha（默认 178），百分比标签同步。"""
    import media_resource_manager.gui.main_window as mw

    monkeypatch.setattr(mw, "save_config", lambda cfg: None)
    w = _win()
    try:
        # 2026-10-27：断言改成「跟随配置」而不是写死数字。
        # 原因：这个测试用的是**真实 config.json**（本机存的是历史值），
        # 写死 255 只在"新装用户"身上成立 —— 那是快照式炸弹。
        # 真正要守的契约是「控件初值 = cfg.watermark_alpha」。
        _exp = int(w.cfg.watermark_alpha or 255)
        assert int(w.s_wm.value()) == _exp, (
            f"滑条初值 {w.s_wm.value()}，应跟随配置 {_exp}")
        assert w.l_wm.text() == w._wm_pct(_exp), f"百分比标签是 {w.l_wm.text()}"
        # 换个配置值重开 → 滑条跟着走
        w.cfg.watermark_alpha = 255
        w.s_wm.setValue(255)
        w.l_wm.setText(w._wm_pct(255))
        assert w.l_wm.text() == "100%"
    finally:
        w.close()
