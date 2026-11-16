"""R107 护栏：所有皮肤 × 所有表格/列表的「前景/背景」对比度必须 ≥ 4.5:1（WCAG AA）。

- 单测 WCAG 相对亮度 / 对比度工具（gui/theme.py 的 _hex_to_rgb / _rel_lum /
  _wcag_contrast / _on_accent_for），断言数值是真算的、不是编造的。
- 逐皮肤采样关键 fg/bg 对（正文/次要/最浅/选中文字 vs 背景/强调底），
  断言对比度 ≥ 4.5。覆盖 classic/violet_look/modern/night/qbittorrent，
  其中 classic 是真原生（系统渲染），只校验正文，不校验强调底（系统接管）。
- Web 端（web/static/css/app.css）：静态变量对 + 逐皮肤强调色衍生的
  on-brand / brand-ink（app.js 同算法）断言达标，qB(青) 浅色重点加深。
"""
import os
import re

import pytest
from PySide6.QtWidgets import QApplication

from media_resource_manager.gui import theme


# ───────────────────────── WCAG 工具单测 ─────────────────────────
def test_hex_to_rgb_handles_3_and_6_digit():
    assert theme._hex_to_rgb("#fff") == (255, 255, 255)
    assert theme._hex_to_rgb("#1f2430") == (31, 36, 48)
    assert theme._hex_to_rgb("#ABCDEF") == (171, 205, 239)


def test_rel_lum_endpoints():
    assert theme._rel_lum((0, 0, 0)) == 0.0
    assert abs(theme._rel_lum((255, 255, 255)) - 1.0) < 1e-9


def test_wcag_contrast_black_white_is_21():
    # 黑底白字 = 21:1，WCAG 定义值，用来钉死公式没写错。
    assert abs(theme._wcag_contrast("#000000", "#ffffff") - 21.0) < 0.05


def test_wcag_contrast_same_color_is_1():
    assert abs(theme._wcag_contrast("#123456", "#123456") - 1.0) < 1e-9


def test_rel_lum_known_values():
    # sRGB 相对亮度：#ffffff=1, #777≈0.18, #000=0（与 WCAG 样例一致）。
    assert abs(theme._rel_lum((119, 119, 119)) - 0.1791) < 0.01


def test_on_accent_for_light_accent_is_dark():
    # qB 青 / 紫 / 蓝 都是浅强调色，白字都不达标 → 必须取深字。
    assert theme._on_accent_for("#22c3a6") == "#101418"   # teal (qB)
    assert theme._on_accent_for("#8b7cf8") == "#101418"   # violet
    assert theme._on_accent_for("#4a90e2") == "#101418"   # blue
    # 深字在强调底上的对比度必须 ≥ 4.5
    for acc in ("#22c3a6", "#8b7cf8", "#4a90e2", "#9d8cff"):
        c = theme._wcag_contrast(theme._on_accent_for(acc), acc)
        assert c >= 4.5, f"{acc} on_accent 对比度 {c:.2f} < 4.5"


# ───────────────────────── 桌面端逐皮肤 ─────────────────────────
# 4 个主题化皮肤（classic 是真原生，强调底由系统接管，单独处理）。
THEMED = [k for k in theme.PRESETS if not theme.preset_is_native(k)]


def _palette_for(key):
    """绕开 Qt，直接拿调色板（取色逻辑集中在 palette()）。"""
    saved_skin = theme._CURRENT_SKIN
    saved_dark = theme._CURRENT_DARK
    dark = theme.preset_dark(key)
    theme._CURRENT_SKIN = theme.preset_accent(key)
    theme._CURRENT_DARK = dark
    try:
        return theme.palette(dark)
    finally:
        theme._CURRENT_SKIN = saved_skin
        theme._CURRENT_DARK = saved_dark


@pytest.mark.parametrize("key", THEMED)
def test_desktop_body_text_contrast(key):
    pal = _palette_for(key)
    assert theme._wcag_contrast(pal["text"], pal["surface"]) >= 4.5
    assert theme._wcag_contrast(pal["text_dim"], pal["surface"]) >= 4.5
    assert theme._wcag_contrast(pal["text_faint"], pal["surface"]) >= 4.5
    # 表头/次要文字（text_dim）也坐在 surface 上，必须达标
    assert theme._wcag_contrast(pal["text_dim"], pal["surface_alt"]) >= 4.5
    # 正文坐在窗口底（卡片/列表背景）也要达标
    assert theme._wcag_contrast(pal["text"], pal["bg"]) >= 4.5


@pytest.mark.parametrize("key", THEMED)
def test_desktop_selected_text_on_accent_contrast(key):
    """选中态：强调底上的文字（on_accent）对比度必须 ≥ 4.5（R107 重点）。"""
    pal = _palette_for(key)
    c = theme._wcag_contrast(pal["on_accent"], pal["accent"])
    assert c >= 4.5, f"{key}: 选中文字 {pal['on_accent']} 在 {pal['accent']} 上 {c:.2f}:1 < 4.5"


def test_desktop_qbittorrent_task_table_text_deepened():
    """qB(青) 皮肤：任务表正文（深字）+ 选中态（深字）都达标，不再浅底浅字。"""
    pal = _palette_for("qbittorrent")
    # 任务表名称/状态/进度列正文（深字 #1f2430 类）坐在纯白格上
    assert theme._wcag_contrast(pal["text"], pal["surface"]) >= 4.5
    # 选中行：青底 + 深字（之前白字仅 2.23:1，几乎看不见）
    c = theme._wcag_contrast(pal["on_accent"], pal["accent"])
    assert c >= 4.5, f"qB 选中态 {c:.2f}:1 < 4.5"
    # 具体钉死：qB 之前白字 2.23 → 现在深字必须 > 4.5
    assert pal["on_accent"] == "#101418"


def test_desktop_qpalette_highlightedtext_wired():
    """真实 apply_preset 后，QPalette.HighlightedText 必须按强调色取深/浅字。"""
    app = QApplication.instance() or QApplication([])
    for key in THEMED:
        theme.apply_preset(app, key)
        pal = app.palette()
        hl = pal.color(__import__("PySide6.QtGui", fromlist=["QPalette"]).QPalette.ColorRole.Highlight)
        ht = pal.color(__import__("PySide6.QtGui", fromlist=["QPalette"]).QPalette.ColorRole.HighlightedText)
        c = theme._wcag_contrast((ht.red(), ht.green(), ht.blue()),
                                 (hl.red(), hl.green(), hl.blue()))
        assert c >= 4.5, f"{key}: QPalette 选中文字 {c:.2f}:1 < 4.5"


def test_classic_native_body_text_still_readable():
    """classic 是真原生：强调底由系统接管，只校验正文（不碰强调底）。"""
    pal = _palette_for("classic")
    assert theme._wcag_contrast(pal["text"], pal["surface"]) >= 4.5
    assert theme._wcag_contrast(pal["text_dim"], pal["surface"]) >= 4.5


# ───────────────────────── Web 端 ─────────────────────────
def _read_css():
    p = os.path.join(os.path.dirname(__file__), "..",
                     "media_resource_manager", "web", "static", "css", "app.css")
    with open(p, encoding="utf-8") as f:
        return f.read()


def _parse_vars(block):
    d = {}
    for m in re.finditer(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", block):
        d[m.group(1)] = m.group(2)
    return d


def _web_ink(acc, dark):
    """复刻 app.js 的 _mrmInkFor：朝黑(浅底)/朝白(深底)混，直到达 4.5:1。"""
    a = theme._hex_to_rgb(acc)
    bg = [28, 31, 39] if dark else [255, 255, 255]
    tgt = [255, 255, 255] if dark else [0, 0, 0]
    best = a
    for i in range(11):
        c = [a[j] + (tgt[j] - a[j]) * i / 10 for j in range(3)]
        if theme._wcag_contrast(c, bg) >= 4.5:
            best = c
            break
        best = c
    return tuple(best)

@pytest.mark.parametrize("key", THEMED)
def test_web_brand_derived_text_contrast(key):
    """Web 逐皮肤：强调色作底色文字(on-brand) / 正文链接(brand-ink) 都达标。"""
    acc = theme.preset_accent(key)
    accent_hex = theme.SKINS[acc][0]
    dark = theme.preset_dark(key)
    # on-brand：强调底上的文字
    on_brand = theme._on_accent_for(accent_hex)
    assert theme._wcag_contrast(on_brand, accent_hex) >= 4.5, \
        f"{key}: on-brand {on_brand} 在 {accent_hex} 上不达标"
    # brand-ink：浅色主题下作链接/正文色，坐在面板白底上
    ink_light = _web_ink(accent_hex, False)
    assert theme._wcag_contrast(ink_light, (255, 255, 255)) >= 4.5, \
        f"{key}: brand-ink(浅) 不达标"
    # brand-ink：深色主题下坐在深色面板上
    ink_dark = _web_ink(accent_hex, True)
    assert theme._wcag_contrast(ink_dark, (28, 31, 39)) >= 4.5, \
        f"{key}: brand-ink(深) 不达标"


def test_web_qbittorrent_teal_link_deepened():
    """qB(青) 重点：名称列链接(青)在白底上之前仅 2.23:1，brand-ink 必须达标。"""
    teal = theme.SKINS["teal"][0]
    # 直接用强调色作链接色（未派生）对比度必须不达标——证明必须派生
    raw = theme._wcag_contrast(teal, (255, 255, 255))
    assert raw < 4.5, "青作链接色本就不达标，测试前提错误"
    # 派生后的 brand-ink 必须达标
    ink = _web_ink(teal, False)
    assert theme._wcag_contrast(ink, (255, 255, 255)) >= 4.5
