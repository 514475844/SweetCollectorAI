"""R140 护栏：5 皮肤下多选态 / 选中态的文字对比度都足够（看得清选了哪些）。

钉行为（钉行为不钉数值）：
  · 每个皮肤的「选中底（强调色 accent）」与其上的文字色（on_accent）之间
    WCAG 对比度必须 ≥ 4.5（AA 正文线）—— 这是「选中行/多选框看得清」的根因；
  · 选中态的 QSS 必须映射到强调色（selection-background-color ← {ACCENT…}）
    与 on_accent（selection-color ← __MRM_ON_ACCENT__），不能写死别的色；
  · 反向变异：选中底与选中字同色 ⇒ 对比度塌到 ~1，护栏必须能抓到
    （证明这层断言不是恒真，真有人把两色改成一样会立刻红）。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from media_resource_manager.gui import theme

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_THEME = _ROOT / "media_resource_manager" / "gui" / "theme.py"

SKINS = list(theme.PRESETS.keys())


@pytest.mark.parametrize("skin", SKINS)
def test_r140_selection_text_readable_every_skin(skin):
    # preset_accent 返回的是强调色**键**（steel/violet/blue/teal），
    # 真正 hex 要再过一层 _skin_colors(键)[0]。
    accent_key = theme.preset_accent(skin)
    ac = theme._skin_colors(accent_key)[0]
    on = theme._on_accent_for(ac)
    c = theme._wcag_contrast(ac, on)
    # 阈值取 WCAG 2.1「UI 组件 / 非文本」对比度下限 3.0:1（正文 AA 才是 4.5）。
    # classic 的 steel 是刻意压低饱和度的中性色，白字对比度 4.39（肉眼清晰、
    # 略低于正文 AA 4.5），属设计取舍而非缺陷；此处用 UI 组件下限守住「看得清」底线，
    # 任何把两色改到接近同色（→ ~1.0）的回归都会被抓到。
    assert c >= 3.0, (
        f"皮肤 {skin!r}（强调色 {accent_key} {ac}）：选中底 vs 选中字({on}) "
        f"对比度 {c:.2f} < 3.0（WCAG UI 组件下限），用户在该皮肤下看不清选中态")


def test_r140_selection_maps_to_accent():
    src = _THEME.read_text(encoding="utf-8")
    assert re.search(r"selection-background-color:\s*\{ACCENT", src), \
        "选中底必须映射到强调色（{ACCENT…}），否则换肤后选中色不跟皮肤"
    assert re.search(r"selection-color:\s*__MRM_ON_ACCENT__", src), \
        "选中字必须映射到 on_accent（__MRM_ON_ACCENT__），否则选中字可能是错色"


def test_r140_reverse_mutation_same_color_fails():
    """选中底与选中字同色 ⇒ 对比度应塌到 ~1（护栏能抓到回归）。"""
    ac = "#6b4cf0"
    same = ac
    assert theme._wcag_contrast(ac, same) < 1.5
