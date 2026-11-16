# -*- coding: utf-8 -*-
"""R115 护栏 —— 锁住 R107 / R113 / R114 三处「选中态可读性」改动。

背景：这三轮改的全是同一类问题 —— **选中行/状态点的文字压在底色上看不清**。
   · R107：浅/深皮肤 `st_idle` `#b6bcc7` -> `#6b7280`（白底 1.70:1 -> 4.83:1）；
   · R113：原生(classic)分支按系统 Highlight 真实亮度写回 HighlightedText；
   · R114：三套 QSS（原生穿透链 / 深色 / 浅色）都补显式
           `QTableView::item:selected, QTableWidget::item:selected`。
三轮都是「改完没人钉」，本文件把它们钉住。

写之前先答三条铁律：

【铁律A：断言「关系」，不「数值」】
   对比度一律**现算**（theme._wcag_contrast），不写死 4.83/5.01 这类数字；
   判据用 WCAG 门槛（正文 4.5:1 / 非文本图形 3:1）这种**标准值**，
   换色位不会假红，换回浅灰立刻红。

【铁律B：护栏钉「行为」，不钉「名字」】
   R113 那条读的是**真的 QPalette**（apply_preset("classic") 之后），
   不是 grep 源码里有没有 `_ink_for_bg` 这几个字 —— 源码里有但没接上
   调色板的话，这条照样红。

【铁律C：护栏不能因为「默认路径也满足」而绿】
   每条都验过反向：把实现改坏/摘掉，它真的红吗？
   ⇒ .workbuddy/_r115_reverse_check.py 逐个变异验证（见其输出）。
   已知理论边界：`_ink_for_bg` 取黑白中**较优**的一支，最坏情况
   （底色相对亮度 L≈0.193）那一支也只剩 4.33:1 —— 若将来某台机器系统
   Highlight 正好落在这个死区，本文件 R113 那条会红，那不是回归，
   是真·够不着 4.5，届时应改调色板而不是放宽判据。
"""
import io
import os
import re
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from media_resource_manager.gui import theme  # noqa: E402

GUI = os.path.join(ROOT, "media_resource_manager", "gui")

WCAG_TEXT = 4.5      # WCAG 2.1 AA 正文对比度
WCAG_GRAPHIC = 3.0   # WCAG 2.1 AA 非文本（图形/状态点）对比度


def _src(name: str = "theme.py") -> str:
    with io.open(os.path.join(GUI, name), encoding="utf-8") as f:
        return f.read()


def _const_body(src: str, const: str) -> str:
    """截出 CONST 的三引号正文（非贪婪，与上游 r67 同口径）。

    ⚠ 函数说明里千万别再写三引号字面量 —— Python 会把 docstring 提前截断，
      收集期直接 SyntaxError（本文件第一版就是这么炸的）。
    """
    m = re.search(const + r"\s*=\s*f?\"\"\"(.*?)\"\"\"", src, re.S)
    assert m, "找不到 %s 定义" % const
    return m.group(1)


def _rule_block(body: str, selector: str) -> str:
    """从 selector 所在行起，取到**括号配平**为止的整条 QSS 规则。"""
    lines = body.splitlines()
    start = next((i for i, ln in enumerate(lines) if selector in ln), None)
    assert start is not None, "找不到含 %s 的行" % selector
    buf = []
    for ln in lines[start:]:
        buf.append(ln)
        seg = "\n".join(buf)
        if seg.count("}") >= seg.count("{") > 0:
            return seg
    return "\n".join(buf)


@pytest.fixture()
def qapp():
    """项目铁律：碰 QWidget 的测试文件必须自带 qapp，且绝不 app.quit()。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _isolate_native_mode():
    """`apply_preset("classic")` 会把 `_NATIVE_MODE` 置 True 并**跨文件残留**。

    这条是 R60 实测踩出来的：不清的话，后面 test_r67 那批 `css()` 用例
    会因为原生模式返回 "" 而集体假红。所以每个用例前强制 False、用例后还原。
    """
    saved = theme._NATIVE_MODE
    theme._NATIVE_MODE = False
    try:
        yield
    finally:
        theme._NATIVE_MODE = saved


# ───────────────────────── R113：原生选中行文字 ─────────────────────────
def test_r113_native_selected_row_text_passes_wcag(qapp):
    """原生模式下选中行：系统 Highlight 底 + 现算墨色，对比度必须过 AA。

    这里读的是**应用后真实的 QPalette**，不是源码。R113 之前的行为是
    HighlightedText 直接用系统白 ⇒ 白字压中等钢灰（用户截图「灰底白字」）。
    """
    theme.apply_preset(qapp, "classic")
    # 前置：classic 必须真的走进原生分支，否则下面验的是别的路径（假绿）
    assert theme._NATIVE_MODE is True, "classic 预设没有进入原生模式"

    pal = qapp.palette()
    hi = pal.color(QPalette.ColorRole.Highlight)
    hit = pal.color(QPalette.ColorRole.HighlightedText)

    # ① 取色必须是「按底色现算」的那支（钉住 R113 的实现接上了调色板）
    assert hit.name() == QColor(theme._ink_for_bg(hi.name())).name(), (
        "原生 HighlightedText %s 不是按系统 Highlight %s 现算的墨色 %s"
        % (hit.name(), hi.name(), theme._ink_for_bg(hi.name())))

    # ② 且这支墨色真的够看（钉住「算出来的够不够」）
    ratio = theme._wcag_contrast(hit.name(), hi.name())
    assert ratio >= WCAG_TEXT, (
        "原生选中行文字 %s 压在 Highlight %s 上只有 %.2f:1，未达 AA 4.5:1"
        % (hit.name(), hi.name(), ratio))


# ──────────────── R114：三套 QSS 都要有显式表格选中规则 ────────────────
def test_r114_every_qss_pins_table_selection_rule():
    """原生穿透链 / 深色 / 浅色三套 QSS，都必须点名表格选中态。

    为什么必须逐个点名：Qt 的 `QTableView::item:selected` 不会继承
    `QListWidget::item:selected`，任务表之前就靠继承 —— 于是选中行是
    灰底白字。三套各写各的，少一套就有一套皮肤回到老样子。
    """
    src = _src()
    # (常量, 该套 QSS 里选中文字该长什么样)
    cases = (
        ("_WM_PENETRATE", "#101418"),        # 原生：写死墨色（无占位符机制）
        ("DARK_QSS", "__MRM_ON_ACCENT__"),   # 深/浅：交给 R107 的占位符替换
        ("LIGHT_QSS", "__MRM_ON_ACCENT__"),
    )
    for const, ink in cases:
        body = _const_body(src, const)
        assert "QTableView::item:selected" in body, (
            "%s 缺 QTableView::item:selected 显式规则" % const)
        assert "QTableWidget::item:selected" in body, (
            "%s 缺 QTableWidget::item:selected 显式规则" % const)
        # 规则里必须真的带文字色，光有底色还是「看不清」。
        # ⚠ 按**括号配平**取整条规则，别按 `}` 截断也别只取一行：
        #   原生链的规则是三行（选择器 / 属性 / 收尾），深/浅两套是单行
        #   f-string 写成 `{{ ... }}` —— 第一版按 `}` 切、第二版只取一行，
        #   两种切法都假红过，这里一次做对。
        block = _rule_block(body, "QTableView::item:selected")
        assert ink in block, (
            "%s 的表格选中规则里没有文字色 %s：%s"
            % (const, ink, block.replace("\n", " ")[:120]))


def test_r114_native_selection_rule_survives_watermark_chain():
    """原生模式下表格透明链与选中规则必须**同时**在（PART3 §3 的红线）。

    水印方案未定稿，但 `_WM_PENETRATE` 不许被删（删了水印彻底消失）；
    而 R114 往里加选中规则时，也不许把透明链挤掉。两者都验。
    """
    body = _const_body(_src(), "_WM_PENETRATE")
    assert "QTableView::item:selected" in body, "原生链里的选中规则被删了"
    for sel in ("QTableWidget, QTableView,", "QTableView::item", "::viewport"):
        assert sel in body, "水印穿透透明链被破坏（缺 %s）" % sel


# ───────────────────────── R107：状态点 / 占位符 ─────────────────────────
def test_r107_status_idle_dot_readable_on_its_own_backgrounds():
    """idle 状态点在各自底色上必须够看（非文本 3:1）。

    只算不动手的色位：状态点是**图形**，门槛按 WCAG 1.4.11 的 3:1。
    """
    for dark in (False, True):
        pal = theme.palette(dark)
        for bg_key in ("bg", "surface", "surface_alt"):
            ratio = theme._wcag_contrast(pal["st_idle"], pal[bg_key])
            assert ratio >= WCAG_GRAPHIC, (
                "%s 皮肤：st_idle %s 压在 %s %s 上只有 %.2f:1（<3:1）"
                % ("深色" if dark else "浅色", pal["st_idle"], bg_key,
                   pal[bg_key], ratio))


def test_r107_idle_dot_on_white_passes_text_grade():
    """R107 的原始诉求：白底上状态点要达到正文级 4.5:1。

    改回 `#b6bcc7` 时这条立刻红（1.70:1），是本文件最脆的一条。
    """
    for dark in (False, True):
        pal = theme.palette(dark)
        ratio = theme._wcag_contrast(pal["st_idle"], "#ffffff")
        assert ratio >= WCAG_TEXT, (
            "%s 皮肤 st_idle %s 在纯白底上只有 %.2f:1（<4.5:1）"
            % ("深色" if dark else "浅色", pal["st_idle"], ratio))


def test_r107_on_accent_placeholder_is_substituted_at_runtime(qapp):
    """`__MRM_ON_ACCENT__` 必须在落盘前被真色替换掉。

    漏替换的后果很阴：Qt 遇到非法 QSS 是**静默丢弃整条规则**，
    界面看起来没变、日志也没有一行 —— 只能靠这条把它钉出来。
    """
    theme.apply_preset(qapp, "violet_look")
    assert not theme.native_mode(), "violet_look 不该是原生模式（否则本条空转）"
    sheet = qapp.styleSheet()
    assert sheet, "非原生模式下应用样式表为空"
    assert "__MRM_ON_ACCENT__" not in sheet, (
        "应用样式表里残留未替换的 __MRM_ON_ACCENT__ 占位符 —— 该条规则会被 Qt 静默丢弃")
