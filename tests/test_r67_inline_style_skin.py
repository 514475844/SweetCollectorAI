# -*- coding: utf-8 -*-
"""P1-4 第二步：行内样式 / 图标也必须跟随皮肤（theme.css + style + 主题化图标）。

作业单原文（FIX_ALL_20261007.md §P1-4）说皮肤「只能改 QSS 覆盖到的颜色，
自绘的部分可能不跟随」。第一轮（R26）补了调色板 + QPalette + unpolish，
但**行内 setStyleSheet 和 QIcon 这两条路仍是黑洞**：
  · 行内 QSS 自带优先级，QSS/unpolish 都盖不住它；
  · QIcon 的颜色在setIcon() 那一刻就烤进 QPixmap 了，之后刷什么都不管用。
本文件把这两条路钉死。

⚠ 本文件遵守项目铁律「断言关系，不写数值」：
  几乎所有断言都是「A==B」（两个都从调色板取），而不是「A=='#5b3fd6'」。
  唯一允许写死字面量的地方是 **violet 那一列**——那不是"当前值"，
  是"这次收敛承诺零视觉变化"的**契约**，改了它就等于悄悄改了默认皮肤。
"""
from __future__ import annotations

import ast
import io
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

GUI = os.path.join(ROOT, "media_resource_manager", "gui")

# 本轮改过的文件（行内色值收敛 + 图标主题化）
TOUCHED = ["theme.py", "main_window.py", "review_tab.py", "lightbox.py",
           "dislike_dialog.py", "similar_web.py", "simsearch_dialog.py",
           "learning_board.py", "icons.py"]


def _src(name: str) -> str:
    with io.open(os.path.join(GUI, name), encoding="utf-8") as f:
        return f.read()


def _ast(name: str):
    return ast.parse(_src(name))


@pytest.fixture
def qapp():
    """本文件自带的 QApplication。

    ⚠ 项目铁律：碰 QWidget 的测试文件必须**自带** qapp fixture
    （别的文件也是这么干的，不在 conftest 里集中声明）；
    绝不 app.quit() —— 别的文件的 fixture 靠同一个 app 才收敛。
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    # 只清本测试登记的东西，不 quit


@pytest.fixture(autouse=True)
def _restore_theme():
    """每个用例结束后把皮肤/深浅/登记表还原，别污染别的用例。"""
    from media_resource_manager.gui import theme, icons
    skin, dark = theme._CURRENT_SKIN, theme._CURRENT_DARK
    yield
    theme._CURRENT_SKIN, theme._CURRENT_DARK = skin, dark
    theme._reset_style_registry()
    icons._reset_themed_registry()


# ══════════════════════════════════════════════════════════════════
# 1. css() 模板解析器本身
# ══════════════════════════════════════════════════════════════════

def test_css_resolves_known_keys():
    """已知色位必须被换成真实色值，且换出来的是合法 #rrggbb。"""
    from media_resource_manager.gui.theme import css, palette
    out = css(False, "color:{text_dim};background:{accent_soft};")
    assert out == "color:%s;background:%s;" % (palette(False)["text_dim"],
                                                palette(False)["accent_soft"])
    for hexv in re.findall(r"#[0-9a-fA-F]{3,8}", out):
        assert len(hexv) in (4, 7, 9), "占位符没解析干净：%s" % out


def test_css_leaves_unknown_placeholders_and_qss_braces_alone():
    """不认识的占位**原样留下**，QSS 自己的花括号绝不能被当成占位吃掉。

    ⚠ 这是本函数最容易写坏的地方：QSS 里满是 `QPushButton{...}`，
    一旦用 str.format / replace('{','}') 那种粗暴做法，
    整段样式会被打成非法 QSS，然后被 Qt **静默丢弃**（实测过：加了规则
    却一点没生效还查不出原因）。
    """
    from media_resource_manager.gui.theme import css
    tpl = "QPushButton{color:#fff;} color:{no_such_key}; {text}"
    out = css(False, tpl)
    assert out.startswith("QPushButton{color:#fff;}"), out
    assert "{no_such_key}" in out, "未知占位被吃掉了"
    assert "{text}" not in out, "已知占位没解析"


def test_css_is_skin_sensitive():
    """同一个模板在不同皮肤下必须解析出不同颜色 —— 这就是 P1-4 的核心诉求。"""
    from media_resource_manager.gui import theme
    tpl = "color:{accent};bg:{accent_soft};deep:{accent_deep};"
    theme._CURRENT_SKIN = "violet"
    vio = theme.css(False, tpl)
    theme._CURRENT_SKIN = "teal"
    tea = theme.css(False, tpl)
    assert vio != tea, "换皮肤后模板解析结果没变 —— 皮肤仍是半成品"
    # 且必须与调色板给的值一致（不是各解析各的）
    p = theme.palette(False)
    assert tea == "color:%s;bg:%s;deep:%s;" % (p["accent"], p["accent_soft"],
                                               p["accent_deep"])


def test_css_is_dark_mode_sensitive():
    """同一个模板在深浅两种模式下也必须不同（深色下白底白字是最典型的瞎）。"""
    from media_resource_manager.gui import theme
    tpl = "color:{text};bg:{surface};"
    assert theme.css(True, tpl) != theme.css(False, tpl)


def test_css_never_raises_on_broken_template():
    """模板再离谱也不许把调用方的绘制/布局路径炸掉。"""
    from media_resource_manager.gui.theme import css
    for bad in ["{", "}", "{{{text}}}", "{ }", "{text", None or "{}", "{a:b}"]:
        assert isinstance(css(False, bad), str)


def test_css_accepts_module_level_constants():
    """`{ACCENT}` 这类大写常量也能用（QSS 里本来就有这些名字）。"""
    from media_resource_manager.gui import theme
    assert theme.css(False, "color:{ACCENT};") == "color:%s;" % theme.ACCENT


# ══════════════════════════════════════════════════════════════════
# 2. 调色板完整性与「零视觉变化」契约
# ══════════════════════════════════════════════════════════════════

def _used_placeholders() -> set:
    """扫出真正当作**调色板占位**用的 `{key}`。

    三重过滤，否则全是假阳性（本项目实测踩过）：
      ① 只看「喂给样式入口的字符串实参」—— 批量改名模板 `{n}{date}`、
         反搜URL 模板 `{url}`、以及大量 docstring 里举例的 `{key}`
         都不是调色板占位；
      ② 排除 f-string（那是 Python 插值，另一套机制）；
      ③ 排除 `.format()` 用法。
    """
    used = set()
    for name in TOUCHED:
        src = _src(name)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("style", "_tstyle")):
                continue
            for arg in node.args:
                for sub in ast.walk(arg):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        used |= set(re.findall(
                            r"\{([a-z_][a-z0-9_]*)\}", sub.value))
    return used


def test_palette_has_every_key_css_templates_reference():
    """本轮用到的每个 {key} 都必须能被 css() 解析出来。

    断言的是「关系」：扫描实际样式模板里的占位 ⊆ 调色板键 ∪ 模块常量。
    少一个键 → css() 会把 `{key}` 原样塞进 QSS → Qt 静默丢弃整段规则，
    界面上「什么都没发生」，还不报错。
    """
    from media_resource_manager.gui import theme
    used = _used_placeholders()
    assert used, "一个占位都没扫到 —— 扫描逻辑本身坏了（测试失去意义）"
    # 模块级常量也是合法占位（css() 会去 globals() 里找）
    allowed = {n for n in dir(theme) if n.isupper()}
    allowed |= set(theme.palette(False))          # palette() 派生出来的也算
    missing = used - allowed
    assert not missing, "css() 会解析不出来的占位符：%s" % missing


def test_palette_violet_matches_pre_migration_literals():
    """**契约**：violet 一列必须与收敛前的行内字面量逐位相同。

    这次收敛的承诺是「默认皮肤下用户看不出任何差别，只是不再写死」。
    如果哪天有人为了"更好看"调了 violet 那一列，这条会红——
    那时必须是一次**有意的**视觉变更决策，而不是顺手改的。

    ⚠⚠ R59 补的关键一行：**必须显式把 `_CURRENT_SKIN` 钉成 violet**。
    这条断言的是「violet 那一列」，可它原来读的是 `palette()` 里的
    **环境状态** `_CURRENT_SKIN` —— 谁在前面跑过 `apply_preset` 就跟着谁。
    R58 之前它一直绿，纯粹是因为 `classic` 的强调色**恰好就是 violet**；
    R59 把 classic 改成钢灰蓝（真原生）后，同一个文件里先跑过切肤的用例
    就让它读到了 steel，于是报「violet 的 accent_soft 被改了」——
    而 violet 一个字节都没变。

    ⇒ 这正是 MEMORY 里那条「**护栏绿了 ≠ 护栏对**」的同型：不是产品回归，
    是护栏的**基线写错了**（没钉住自己声称要测的对象）。
    修法是钉住变量，不是放宽断言、更不是回退产品码。
    """
    from media_resource_manager.gui import theme
    prev = theme._CURRENT_SKIN
    theme._CURRENT_SKIN = "violet"
    try:
        p = theme.palette(False)
    finally:
        theme._CURRENT_SKIN = prev
    frozen = {"accent_soft": "#efeaff", "accent_soft_hover": "#e0d6ff",
              "accent_deep": "#5b3fd6", "accent": "#8b7cf8",
              "accent_hover": "#7466e8", "title": "#7c6cf0",
              "surface_alt": "#f4f6fa", "border": "#d6dbe4"}
    for k, v in frozen.items():
        assert p[k] == v, ("violet 的 %s 被改了（%s），默认皮肤会出现视觉变化"
                           " —— 这必须是一次显式的设计决定" % (k, p[k]))


def test_every_skin_has_soft_triple():
    """每款皮肤都必须给全 soft / soft_hover / deep 三个衍生物色位。"""
    from media_resource_manager.gui.theme import SKINS, SKIN_SOFT
    for name in SKINS:
        assert name in SKIN_SOFT, "皮肤 %s 缺 SKIN_SOFT 配色" % name
        triple = SKIN_SOFT[name]
        assert len(triple) == 3, "皮肤 %s 的 SKIN_SOFT 应为三元组" % name
        for v in triple:
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", v), v


def test_deep_variant_differs_from_soft_variant():
    """deep（文字）与 soft（底）必须不是同一个色 —— 同色=文字看不见。"""
    from media_resource_manager.gui.theme import SKIN_SOFT
    for name, (soft, soft_h, deep) in SKIN_SOFT.items():
        assert deep != soft, "皮肤 %s 的 deep 与 soft 同色" % name
        assert deep != soft_h, "皮肤 %s 的 deep 与 soft_hover 同色" % name


def test_warn_colors_differ_between_modes():
    """告警色在深浅下必须不同。

    深色下如果还用「浅底 + 深棕字」，整条横幅在深色主题里等于隐形
    （这正是收敛前 `#fff4e5/#8a4b00` 的问题）。
    """
    from media_resource_manager.gui.theme import palette
    d, l = palette(True), palette(False)
    for k in ("warn_bg", "warn_text", "warn_border"):
        assert d[k] != l[k], "告警色位 %s 深浅相同 —— 深色下看不见" % k


# ══════════════════════════════════════════════════════════════════
# 3. style() 登记与重渲（行内样式的生命周期）
# ══════════════════════════════════════════════════════════════════

def test_style_applies_and_registers(qapp):
    """style() 要真的设上样式，并在登记表里留下这个控件。"""
    from PySide6.QtWidgets import QLabel
    from media_resource_manager.gui import theme
    lab = QLabel("x")
    theme._reset_style_registry()
    theme.style(lab, "color:{text_dim};")
    assert theme.css(False, "color:{text_dim};") in lab.styleSheet()
    assert len(theme._INLINE_STYLES) == 1, "没登记 → 切皮肤时不会重渲"


def test_style_rerenders_after_skin_switch(qapp):
    """**本轮的核心断言**：换皮肤后，已存在的控件样式必须跟着变。

    这条正是 P1-4 说的「用户觉得皮肤是半成品」——控件建好之后换皮肤，
    之前它纹丝不动（因为行内 QSS 优先级高，unpolish 刷不到）。
    """
    from PySide6.QtWidgets import QLabel
    from media_resource_manager.gui import theme
    lab = QLabel("x")
    theme._reset_style_registry()
    theme.style(lab, "color:{accent_deep};")

    theme._CURRENT_SKIN = "violet"
    theme._restyle_registered(False)
    violet = lab.styleSheet()

    theme._CURRENT_SKIN = "teal"
    theme._restyle_registered(False)
    teal = lab.styleSheet()

    assert violet != teal, "换皮肤后行内样式没变 —— 这就是「皮肤半成品」本身"
    assert "{accent_deep}" not in teal, "重渲后仍留着未解析的占位符"
    assert teal == "color:%s;" % theme.palette(False)["accent_deep"]


def test_style_rerenders_after_dark_switch(qapp):
    """切深浅同样要重渲（白底白字是深色主题下最常见的瞎）。"""
    from PySide6.QtWidgets import QLabel
    from media_resource_manager.gui import theme
    lab = QLabel("x")
    theme._reset_style_registry()
    theme.style(lab, "color:{text};background:{surface};")
    theme._restyle_registered(False)
    light = lab.styleSheet()
    theme._restyle_registered(True)
    dark = lab.styleSheet()
    assert light != dark
    assert dark == "color:%s;background:%s;" % (
        theme.palette(True)["text"], theme.palette(True)["surface"])


def test_style_default_dark_comes_from_module_state(qapp):
    """省略 dark 时必须由 theme._CURRENT_DARK 回答（漏传不可能发生）。"""
    from PySide6.QtWidgets import QLabel
    from media_resource_manager.gui import theme
    lab = QLabel("x")
    theme._CURRENT_DARK = True
    theme._reset_style_registry()
    theme.style(lab, "color:{text};")
    assert lab.styleSheet() == "color:%s;" % theme.palette(True)["text"]


def test_style_drops_dead_widgets(qapp):
    """控件销毁后要从登记表里摘掉，否则长跑会话里弱引用无限累积。"""
    from PySide6.QtWidgets import QLabel
    from media_resource_manager.gui import theme
    theme._reset_style_registry()
    lab = QLabel("x")
    theme.style(lab, "color:{text};")
    assert len(theme._INLINE_STYLES) == 1
    lab.deleteLater()
    from PySide6.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme._restyle_registered(False)
    assert theme._INLINE_STYLES == [], "已销毁控件还挂在登记表里"


def test_apply_theme_wires_restyle_and_icons(qapp):
    """apply_theme 必须同时驱动「行内样式重渲」和「图标重烤」。

    少了任何一条，用户看到的就还是旧色 —— 而且**不报错**，
    属于那种「测试全绿但功能没做」的最坏情况，所以钉死调用关系。
    """
    seg = _src("theme.py")
    seg = seg[seg.find("def apply_theme("):]
    for tok in ("_restyle_registered", "restyle_themed_icons",
                "_CURRENT_DARK = bool(dark)"):
        assert tok in seg, "apply_theme 缺 %s —— 切皮肤后有控件不跟随" % tok


def test_apply_theme_tracks_dark_mode(qapp):
    """apply_theme 必须把当前深浅写回模块状态，否则省略 dark 的调用点全错。"""
    from media_resource_manager.gui import theme
    theme.apply_theme(qapp, True, "violet")
    assert theme._CURRENT_DARK is True
    theme.apply_theme(qapp, False, "violet")
    assert theme._CURRENT_DARK is False


# ══════════════════════════════════════════════════════════════════
# 4. 图标主题化
# ══════════════════════════════════════════════════════════════════

def test_themed_icon_uses_palette(qapp):
    from PySide6.QtWidgets import QPushButton
    from media_resource_manager.gui import icons, theme
    icons._reset_themed_registry()
    b = QPushButton()
    icons.set_themed_icon(b, "play", "{text_dim}", 15)
    assert not b.icon().isNull()
    assert len(icons._THEMED) == 1


def test_themed_icon_rebakes_after_skin_switch(qapp):
    """QIcon 颜色烤死在 QPixmap 里 —— 换皮肤必须**重建**，否则旧色不变。"""
    from PySide6.QtWidgets import QPushButton
    from media_resource_manager.gui import icons, theme
    icons._reset_themed_registry()
    b = QPushButton()
    icons.set_themed_icon(b, "play", "{accent}", 15)

    theme._CURRENT_SKIN = "violet"
    icons.restyle_themed_icons()
    vio_key = ("play", theme.palette(False)["accent"], 15)
    theme._CURRENT_SKIN = "teal"
    icons.restyle_themed_icons()
    tea_key = ("play", theme.palette(False)["accent"], 15)

    assert vio_key != tea_key, "换皮肤后图标颜色没变"
    # 新颜色必须真的被渲染进缓存（即控件拿到的图标是重烤过的）
    assert tea_key in icons._CACHE, "新皮肤的颜色没进渲染缓存"


def test_plain_icon_call_is_unchanged(qapp):
    """普通色值的 icon() 必须与从前**逐位一致**（不登记、不解析占位）。"""
    from media_resource_manager.gui import icons
    icons._reset_themed_registry()
    ic = icons.icon("play", color="#3a4150", size=15)
    assert isinstance(ic, type(icons.icon("stop")))
    assert not ic.isNull()
    assert icons._THEMED == [], "普通色值不该进主题化登记表"


def test_themed_registry_drops_dead_widgets(qapp):
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtCore import QCoreApplication, QEvent
    from media_resource_manager.gui import icons
    icons._reset_themed_registry()
    b = QPushButton()
    icons.set_themed_icon(b, "play", "{text}", 15)
    b.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    icons.restyle_themed_icons()
    assert icons._THEMED == []


# ══════════════════════════════════════════════════════════════════
# 5. 反向护栏：行内色值不许复活 / 不许绕过 css()
# ══════════════════════════════════════════════════════════════════

def test_no_inline_color_literals_left_in_gui():
    """`gui/` 下（theme.py 除外）不许再有 `color:#xxxxxx` 行内字面量。

    theme.py 是**调色板本身**，它的十六进制是数据不是违规。
    """
    bad = []
    for name in os.listdir(GUI):
        if not name.endswith(".py") or name == "theme.py":
            continue
        for i, line in enumerate(_src(name).split("\n"), 1):
            if re.search(r"color:\s*#[0-9a-fA-F]{3,8}", line):
                bad.append("%s:%d %s" % (name, i, line.strip()[:70]))
    assert not bad, "又写死了行内色值（P1-4 要求收敛到调色板）：\n" + "\n".join(bad)


def test_every_inline_stylesheet_goes_through_style():
    """**防呆护栏**：含 `{key}` 占位的 setStyleSheet 必须经 theme 解析。

    ⚠ 本轮真的踩过：`#task_list::item:selected{...color:{text}...}` 那一段
    换色时只改了字面量、没改调用方式，占位符原样进了 Qt ——
    Qt 遇到非法 QSS 是**静默丢弃整段规则**的，不报任何错。
    所以不能靠"测试全绿"发现它，得静态钉死。
    """
    offenders = []
    for name in os.listdir(GUI):
        if not name.endswith(".py"):
            continue
        src = _src(name)
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "setStyleSheet"):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if not re.search(r"\{[a-z_]+\}", seg):
                continue
            # f-string 插值（{变量}）是**另一套**机制，不算占位符漏网
            if isinstance(node.args[0], ast.JoinedStr):
                continue
            offenders.append("%s:%d %s" % (name, node.lineno, seg[:80]))
    assert not offenders, ("有占位符没走 theme 解析（Qt 会静默丢弃整段 QSS）："
                           "\n" + "\n".join(offenders))


def test_untouched_skin_qss_still_plain():
    """反向护栏：QSS 模板本身不许被改成含 `{key}` 占位。

    theme.py 里 DARK_QSS / LIGHT_QSS 走的是**追加替换**（_recolor），
    不许跟行内模板那套混起来 —— 混了就是几百行插值，
    占位错位会静默变成非法 QSS（文件头注释里写过这个坑）。
    """
    src = _src("theme.py")
    for const in ("DARK_QSS", "LIGHT_QSS"):
        m = re.search(const + r"\s*=\s*f?\"\"\"(.*?)\"\"\"", src, re.S)
        assert m, "找不到 %s 定义" % const
        body = m.group(1)
        assert not re.search(r"\{[a-z_]+\}", body), \
            "%s 混进了行内占位符语法" % const


def test_learning_board_draws_colors_at_paint_time(qapp):
    """看板必须在 paintEvent 里现取色。

    它原来把色值写成**模块级常量**（导入那刻就定死），是「皮肤半成品」
    最扎眼的一处：整个看板永远是紫配浅灰，换皮肤纹丝不动。

    用 AST 定位每个类的 paintEvent（正则切类体容易切串），
    再断言里面确实在调取色入口 —— 而不是继续用模块级常量。
    """
    tree = ast.parse(_src("learning_board.py"))
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    for fn in ("_BarChart", "_TrendChart"):
        assert fn in classes, "找不到 class %s" % fn
        paints = [m for m in classes[fn].body
                  if isinstance(m, ast.FunctionDef) and m.name == "paintEvent"]
        assert paints, "%s 没有 paintEvent" % fn
        seg = ast.get_source_segment(_src("learning_board.py"), paints[0]) or ""
        assert "_pc(" in seg, (
            "%s 的 paintEvent 没现取调色板（还在用模块级常量？）"
            " —— 换皮肤不变色" % fn)
    assert "def _pc(" in _src("learning_board.py"), "缺 _pc() 取色入口"


def test_main_window_imports_the_style_helper():
    """main_window 必须真的导入了 style，否则那些 _tstyle 调用会 NameError。"""
    tree = _ast("main_window.py")
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if a.asname:
                    names.add(a.asname)
                else:
                    names.add(a.name)
    assert "_tstyle" in names, "main_window 没导入 style（P1-4 收敛会 NameError）"


def test_all_touched_modules_parse():
    """本轮动过的每个文件都必须能 parse —— 迁移脚本最容易漏缩进。"""
    for name in TOUCHED:
        ast.parse(_src(name))