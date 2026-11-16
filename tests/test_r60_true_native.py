# -*- coding: utf-8 -*-
"""R60 / NATIVE_SKIN_ORDER §3 —— 「真原生」渲染的行为级护栏。

用户诉求原文：「原始工程风的皮肤不太对吧？不是应该是 Windows 的原始
窗口吗。为什么好多按键还是自绘的？」（NATIVE_SKIN_ORDER §0）

本文件遵守本项目三条铁律，写之前先答一遍：

【铁律A：断言「关系」，不「数值」】——但**本文件恰恰要钉数值**，
   因为「原生 = QSS 极短」这件事本身就是契约：不钉长度上限，
   「有人往原生分支里偷偷加回了自绘 QSS」这个回归就抓不到。
   数值取自 tools/_r60_probe_native.py 的实测：完整 QSS = 7177 字符，
   _WM_PENETRATE = 1781 字符。判据写成「< 完整 QSS 的一半」这种
   **关系**，而不是「== 1781」，免得常量一改就假红。

【铁律B：护栏钉「行为」，不钉「名字」】——R54 教训。这里全部断言
   「QSS 变成什么」「登记表还剩几条」「图标 isNull 与否」，
   唯一读源码的地方用 AST 查**语义**（原生分支必须显式 reset 调色板）。

【铁律C：护栏不能因为「默认路径也满足」而绿】——R59 教训2。
   本文件每一条都先确认：把实现改坏/删掉，它真的会红吗？
   ⇒ tools/_r60_reverse_check.py 用 7 个变异逐条验证。
"""
import ast
import io
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QLabel, QPushButton  # noqa: E402

from media_resource_manager.gui import icons, theme  # noqa: E402

GUI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "media_resource_manager", "gui")


@pytest.fixture()
def qapp():
    """项目铁律：碰 QWidget 的测试文件必须自带 qapp，且绝不 app.quit()。"""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _clean_registries():
    """每个用例前后清空行内样式/图标登记表**与 `_NATIVE_MODE`**。

    ⚠ 必须清：`_INLINE_STYLES` 是模块级 list，跨用例累积。本文件
      的断言**直接读它的长度**（「原生模式必须保留登记」），
      若不隔离，后一个用例会因为前一个用例登记的控件而假红。

    ⚠⚠ `_NATIVE_MODE` 也必须清 —— 这条是**实测踩出来的**：
      本文件里 `apply_preset(qapp, "classic")` 把它置成 True，而
      `css()` 在原生模式返 ""。测试跑到本文件之后，
      tests/test_r67_inline_style_skin.py 的 8 条 css 用例
      （test_css_resolves_known_keys 等）全部拿到 '' 而红 ——
      单跑 r67 是绿的，只有全量才炸。
      ⇒ 这是「单跑绿 ≠ 在真实调用环境里绿」（R56 铁律）的又一次复发，
      只不过这次污染源是**我自己新写的文件**。
    """
    theme._reset_style_registry()
    icons._reset_themed_registry()
    saved_native = theme._NATIVE_MODE
    theme._NATIVE_MODE = False
    yield
    theme._NATIVE_MODE = saved_native
    theme._reset_style_registry()
    icons._reset_themed_registry()


@pytest.fixture()
def styled():
    """一个带行内样式 + 一个带主题化图标的真实控件。"""
    lab = QLabel("任务名")
    btn = QPushButton("开始")
    theme.style(lab, False, "color:{accent}; background:{accent_soft};")
    theme.style(btn, False, "color:{text};")
    icons.set_themed_icon(btn, "play", "{text}", 16)
    return lab, btn


# ──────────────────────────────────────── ① 原生预设判定

def test_only_classic_is_native():
    """只有 classic 是原生；其余四个观感不许动（NATIVE_SKIN_ORDER §1）。"""
    native = [k for k in theme.PRESETS if theme.preset_is_native(k)]
    assert native == ["classic"], (
        f"原生预设应恰好只有 classic，实际 {native}"
    )


def test_every_non_native_preset_still_gets_real_qss(qapp):
    """非原生预设必须仍然拿到**完整** QSS —— 原生改造不能把别的皮肤连坐。

    这是本文件对「别顺手改坏」的正面钉死：四个非原生预设逐个
    apply_preset 后，QSS 长度都要接近完整 QSS。
    """
    for key in ("violet_look", "modern", "night", "qbittorrent"):
        theme.apply_preset(qapp, key)
        qss = qapp.styleSheet()
        assert len(qss) > 3000, (
            f"{key} 的 QSS 只有 {len(qss)} 字符 —— 被原生分支连坐了"
        )


# ────────────────────────���──────────── ② 原生模式的 QSS 形态

def test_native_mode_installs_only_the_watermark_penetration_qss(qapp):
    """原生模式下 app 身上只能有 _WM_PENETRATE，其余一律不套。"""
    theme.apply_preset(qapp, "classic")
    assert qapp.styleSheet() == theme._WM_PENETRATE


def test_native_qss_is_far_smaller_than_a_painted_one(qapp):
    """钉「关系」：原生 QSS 必须远小于完整自绘 QSS。

    ⚠ 为什么还要这一条：上一条只说「等于 _WM_PENETRATE」。
      如果有人把 _WM_PENETRATE 本身塞进一堆自绘规则（它是常量！），
      上一条照样绿。这条用「不到完整 QSS 的一半」把体积钉住。
    """
    theme.apply_preset(qapp, "violet_look")
    painted = len(qapp.styleSheet())
    theme.apply_preset(qapp, "classic")
    native = len(qapp.styleSheet())
    assert native < painted / 2, (
        f"原生 QSS {native} 字符 vs 完整 {painted} —— 只差不到一半，"
        f"说明自绘 QSS 仍然被套上去了"
    )


def test_native_mode_really_has_no_rounded_corner_or_accent_rules(qapp):
    """原生模式不该再出现圆角/淡紫等自绘痕迹（NATIVE_SKIN_ORDER §3.2/§3.5）。

    这是给「肉眼判据」一个可测的代理：真原生 = 控件交给 Windows 画，
    所以 QSS 里就不该再有 `border-radius` 这类自绘痕迹。
    ⚠ 判据用「原生里出现、而完整 QSS 里也出现的自绘规则」的**交集**，
      不用「不许出现 border-radius」这种绝对禁令 —— `_WM_PENETRATE`
      本身可能因水印链需要而含个别属性，绝对禁令会假红。
    """
    theme.apply_preset(qapp, "violet_look")
    painted = qapp.styleSheet()
    theme.apply_preset(qapp, "classic")
    native = qapp.styleSheet()
    marker = "border-radius"
    assert marker in painted, "前置：完整 QSS 里应当有自绘圆角，否则本用例无意义"
    assert marker not in native, (
        "原生 QSS 里仍出现 border-radius —— 自绘样式没被真正剥掉"
    )


# ──────────────────────────────────────── ③ 行内样式必须一并失效

def test_inline_styles_die_in_native_mode(qapp, styled):
    """行内 QSS 必须与全局 QSS 一起失效（本单最容易漏的一条）。

    行内 `setStyleSheet` 自带优先级，全局不套 QSS **盖不掉**它。
    漏了就是「按钮原生、任务列表选中态还是紫的」，比不改更割裂。
    """
    lab, btn = styled
    theme.apply_preset(qapp, "classic")
    assert lab.styleSheet() == "", f"label 行内样式未失效：{lab.styleSheet()!r}"
    assert btn.styleSheet() == "", f"button 行内样式未失效：{btn.styleSheet()!r}"


def test_inline_styles_are_nonempty_in_a_painted_preset(qapp, styled):
    """反向前置：非原生预设下行内样式**必须**有色。

    ⚠ R59 教训2 的正面应用：如果这条不存在，上一条可能在
      「行内样式从头到尾就没生效过」的世界里也绿 —— 那就不是
      「原生让它失效了」，而是「它一直没生效」。两条必须成对。
    """
    lab, _ = styled
    theme.apply_preset(qapp, "violet_look")
    assert lab.styleSheet() != "", "前置失败：violet_look 下行内样式本该有色"


def test_css_returns_empty_string_in_native_mode(qapp):
    """css() 在原生模式返空串 —— 返 template 会把 `{text}` 原样交给 Qt。"""
    theme.apply_preset(qapp, "classic")
    assert theme.css(False, "color:{accent};") == ""
    theme.apply_preset(qapp, "violet_look")
    assert theme.css(False, "color:{accent};") != ""


# ───────────────────────────────── ④ 登记表/图标登记必须保留

def test_native_mode_keeps_the_inline_registry(qapp, styled):
    """原生模式清样式，但**必须保留登记** —— 否则切回去永久裸奔。

    这是补丁头「坑2」的钉死。登记表是模块级 list，只减不清 ⇒
    用户从 classic 切回 violet_look 时还能被重新上色。
    """
    _, _ = styled
    assert len(theme._INLINE_STYLES) == 2, "前置：应登记了 2 条"
    theme.apply_preset(qapp, "classic")
    assert len(theme._INLINE_STYLES) == 2, (
        f"原生模式把行内样式登记表清了（剩 "
        f"{len(theme._INLINE_STYLES)} 条）⇒ 切回别的皮肤将永久裸奔"
    )


def test_native_mode_keeps_the_icon_registry(qapp, styled):
    """同理：图标清了要能烤回来。"""
    _, _ = styled
    theme.apply_preset(qapp, "classic")
    assert len(icons._THEMED) == 1, (
        f"原生模式把图标登记表清了（剩 {len(icons._THEMED)} 条）"
        f"⇒ 切回别的皮肤图标将永久消失"
    )


# ──────────────────────────────────────── ⑤ 往返恢复（最要紧）

def test_round_trip_back_to_a_painted_preset_restores_inline_styles(qapp, styled):
    """classic → violet_look 往返：行内样式必须**完全恢复**。

    ⚠ 这是本文件最重要的一条。只测「当前外观」的用例永远发现不了
      往返丢失 —— 它在切回去的那一刻就已经坏了，而那一刻没人看。
    """
    lab, _ = styled
    theme.apply_preset(qapp, "violet_look")
    before = lab.styleSheet()
    theme.apply_preset(qapp, "classic")
    assert lab.styleSheet() == ""
    theme.apply_preset(qapp, "violet_look")
    assert lab.styleSheet() == before, (
        f"往返后行内样式没恢复：{lab.styleSheet()!r} != {before!r}"
    )


def test_round_trip_restores_the_full_stylesheet_byte_for_byte(qapp):
    """往返后 QSS 必须与基线**逐字节相同**。"""
    theme.apply_preset(qapp, "violet_look")
    base = qapp.styleSheet()
    theme.apply_preset(qapp, "classic")
    theme.apply_preset(qapp, "violet_look")
    assert qapp.styleSheet() == base


def test_round_trip_restores_icons(qapp, styled):
    """往返后图标必须回来（不是永久纯文字）。"""
    _, btn = styled
    theme.apply_preset(qapp, "classic")
    assert btn.icon().isNull() is True, "前置：原生模式图标应已被清"
    theme.apply_preset(qapp, "violet_look")
    assert btn.icon().isNull() is False, "往返后图标没回来"


def test_five_preset_round_trip_is_stable(qapp, styled):
    """五预设连续切 3 轮，终态必须与只切一次完全一致。

    防的是「只在两两相邻时可逆」这类半吊子实现（状态残留）。
    """
    lab, _ = styled
    theme.apply_preset(qapp, "violet_look")
    one = (qapp.styleSheet(), lab.styleSheet())
    for _ in range(3):
        for k in ("classic", "violet_look", "modern", "night", "qbittorrent"):
            theme.apply_preset(qapp, k)
    theme.apply_preset(qapp, "violet_look")
    assert (qapp.styleSheet(), lab.styleSheet()) == one


# ──────────────────────────────────── ⑥ 调色板必须显式 reset

def test_native_mode_resets_a_previously_dark_palette(qapp):
    """先暗夜风再切原始工程风 ⇒ 调色板必须回**系统**，不能留着深色。

    这是补丁头「坑3」：只「不设」的话，窗口会变成
    「QSS 原生 + 调色板深色」的半原生，比不改更割裂。
    """
    theme.apply_preset(qapp, "night")
    dark = qapp.palette().color(qapp.palette().ColorRole.Window).name()
    assert theme._CURRENT_DARK is True
    theme.apply_preset(qapp, "classic")
    now = qapp.palette().color(qapp.palette().ColorRole.Window).name()
    assert now != dark, (
        f"原生模式没有 reset 调色板，仍是 {now}（night 时是 {dark}）"
    )
    assert theme._CURRENT_DARK is False, "原生模式恒浅色"


def test_native_palette_equals_a_default_constructed_one(qapp):
    """原生模式的调色板必须**就是**系统默认调色板（钉关系不钉色值）。

    为什么不写死某个 hex：系统调色板因 Windows 版本/主题而异，
    写死会假红。这里断言「与新建 QPalette() 完全一致」。
    """
    from PySide6.QtGui import QPalette
    theme.apply_preset(qapp, "night")
    theme.apply_preset(qapp, "classic")
    syspal = QPalette()
    for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base,
                 QPalette.ColorRole.Text, QPalette.ColorRole.WindowText):
        assert (app_p := qapp.palette().color(role)) == syspal.color(role), (
            f"{role} 与系统调色板不一致：{app_p.name()} != "
            f"{syspal.color(role).name()}"
        )


# ─────────────────────────────── ⑦ 直调 apply_theme 不许变原生

def test_direct_apply_theme_call_is_not_native(qapp):
    """测试里那 43 处 `apply_theme(app, dark, skin)` 直调必须仍是老行为。

    ⚠ 这条钉的是**接口形状**：native 只能由 `apply_preset` 这条真实
      用户路径传入。若有人改成从 `_CURRENT_PRESET` 隐式推导，
      模块刚 import 时它就是 classic ⇒ 43 处直调一夜全变原生，
      R57b 那一整套护栏会同时爆掉，且看起来完全像真回归。

    ⚠⚠ 必须**显式把 `_CURRENT_PRESET` 钉成 classic** 再直调
      （反向验证 M7 抓出来的）：不钉的话，这条能否抓红取决于
      「上一个用例恰好把预设留成了什么」—— 那正是 R59 教训3
      「恰好等于不是契约」。钉死之后，隐式推导这条路一改必红。
    """
    saved = theme._CURRENT_PRESET
    try:
        theme._CURRENT_PRESET = theme.DEFAULT_PRESET      # classic = 原生预设
        theme.apply_theme(qapp, False, "violet")
        assert theme.native_mode() is False, (
            "直调 apply_theme 竟进入了原生模式 —— native 被从 "
            "_CURRENT_PRESET 隐式推导了（坑1）"
        )
        assert len(qapp.styleSheet()) > 3000
    finally:
        theme._CURRENT_PRESET = saved


def test_widgets_created_while_already_native_get_no_icon(qapp):
    """原生模式下**新建**的控件也不该带自绘图标（按钮是纯文字）。

    ⚠ 这条是反向验证 M8 逼出来的：原护栏只在「先建控件、再切皮肤」
      的顺序下测，而那条路径上的清图标由 `clear_themed_icons()`
      负责，`set_themed_icon` 的原生分支根本没被走到 ——
      变异删掉它，护栏与探针**双双全绿**（= M8 LOOSE）。
      真实场景却是存在的：切到原始工程风之后，任务刷新/翻页仍会
      **新建** TaskRow（main_window.py:6915），那些新按钮若带着
      自绘图标，就是「同一屏里两种观感」。所以必须造这个事实。
    """
    theme.apply_preset(qapp, "classic")
    late = QPushButton("开始")
    icons.set_themed_icon(late, "play", "{text}", 16)
    assert late.icon().isNull() is True, (
        "原生模式下新建的控件仍带自绘图标 —— 按钮没有退回纯文字"
    )
    # 反向前置：非原生模式下同一个调用必须真的设上图标
    theme.apply_preset(qapp, "violet_look")
    late2 = QPushButton("开始")
    icons.set_themed_icon(late2, "play", "{text}", 16)
    assert late2.icon().isNull() is False, (
        "前置失败：violet_look 下图标本该设上 —— "
        "否则上一条可能是因为图标本来就设不上而绿"
    )


def test_widgets_styled_while_native_are_still_registered(qapp):
    """⚠ 反向验证 M4 逼出来的：原生模式下**新建**的行内样式必须登记。

    背景：上一条护栏（登记保留）用的是 `styled` fixture —— 控件是在
    **切到 classic 之前**建的，`style()` 那时 `native_mode()` 还是
    False。于是变异「`style()` 在原生模式不登记」完全不影响它
    ⇒ 护栏全绿（= M4 LOOSE），而真实场景已坏：
    用户停在原始工程风时新建的控件，切回 violet_look 后**不会被上色**。
    ⇒ 判据必须把 `style()` 调用放在 native **之内**。
    """
    theme.apply_preset(qapp, "classic")
    assert theme.native_mode() is True, "前置：应处于原生模式"
    late = QLabel("切皮肤之后新建的控件")
    theme.style(late, False, "color:{accent};")
    assert len(theme._INLINE_STYLES) == 1, (
        f"原生模式下新建的行内样式没有登记（剩 "
        f"{len(theme._INLINE_STYLES)} 条）⇒ 切回 violet_look 后它不会被上色"
    )
    # 真正的后果：切回去必须真的拿到颜色
    theme.apply_preset(qapp, "violet_look")
    assert late.styleSheet() != "", (
        "原生模式下建的控件，切回非原生后仍是裸的（永久裸奔）"
    )


def test_native_mode_forces_light_regardless_of_the_dark_argument(qapp):
    """`apply_theme(native=True)` 即使传 dark=True 也必须按浅色处理。

    ⚠ 反向验证 M9 逼出来的：只经 `apply_preset` 测的话，
      `classic` 在 PRESETS 里 dark=False ⇒ 传下去永远是 False，
      变异「_CURRENT_DARK 仍跟随传入值」与真代码**完全等价**
      （这正是 R59 教训2「默认路径也满足它」的静态版本），
      于是护栏与探针双双全绿。必须直接传 dark=True 把事实造出来。
    """
    theme.apply_theme(qapp, True, "violet", native=True)
    assert theme._CURRENT_DARK is False, (
        "原生模式传了 dark=True 却把 _CURRENT_DARK 设成深色 —— "
        "自绘控件会继续按深色取色，界面半深半浅"
    )


def test_native_flag_defaults_to_false():
    """签名层：`native` 必须是**有默认值的参数**，不能是必填。"""
    import inspect
    sig = inspect.signature(theme.apply_theme)
    assert "native" in sig.parameters, "apply_theme 少了 native 参数"
    assert sig.parameters["native"].default is False, (
        "native 默认值必须是 False，否则 43 处直调全变原生"
    )


def test_apply_preset_is_the_only_place_that_passes_native():
    """AST 钉：全模块只有 apply_preset 显式传 native=True。

    判据写成「**没有任何一处**在 apply_preset 之外传 native」，
    而不是「apply_preset 里出现了 native」—— 后者在别人也传时照样绿
    （R58 教训：判「X 与 Y 不同处」要写成「没有任何一处同时含 X 和 Y」）。
    """
    with io.open(os.path.join(GUI, "theme.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    callers = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "apply_theme"):
            for k in node.keywords:
                if k.arg == "native":
                    callers.append((node.lineno, k.value))
    assert len(callers) == 1, (
        f"应只有 1 处显式传 native，实际 {len(callers)} 处："
        f"{[(ln, ast.dump(v)[:40]) for ln, v in callers]}"
    )
    lineno, val = callers[0]
    # ⚠ 判「值来自 preset_is_native()」必须看**值节点**。
    #   我第一版写成 `"preset_is_native" in kw`（kw 是**关键字名**的集合，
    #   恒等于 {'native'}）⇒ 无论传什么值都红 —— 仪器读错，不是代码错
    #   （R53 铁律：判「探针报红」先分清是产物坏还是仪器读错）。
    assert (isinstance(val, ast.Call)
            and isinstance(val.func, ast.Name)
            and val.func.id == "preset_is_native"), (
        f"theme.py:{lineno} 的 native 值是 {ast.dump(val)[:80]}，"
        f"必须来自 preset_is_native() —— 写死 True/False 会让"
        f"「哪些预设原生」这件事有两个真相"
    )


def test_native_branch_explicitly_resets_the_palette():
    """AST 钉：原生分支里必须**显式** setPalette（坑3 的静态面）。

    用 AST 而不是全文搜 `setPalette` —— 全文搜会被本文件自己的
    docstring / 注释骗到（R37 铁律）。
    """
    with io.open(os.path.join(GUI, "theme.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "apply_theme")
    # 找 `if _NATIVE_MODE:` 这一支
    native_ifs = [n for n in ast.walk(fn)
                  if isinstance(n, ast.If) and "_NATIVE_MODE" in ast.dump(n.test)]
    assert native_ifs, "apply_theme 里没有 _NATIVE_MODE 分支"
    has_reset = any(
        isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "setPalette"
        for branch in native_ifs
        for c in ast.walk(branch)
    )
    assert has_reset, (
        "原生分支没有显式 app.setPalette(QPalette()) —— "
        "「不设」不等于「reset」，深色调色板会留着（坑3）"
    )


# ──────────────────────────────────────── ⑧ 未知键回落一致性

def test_unknown_preset_falls_back_consistently(qapp):
    """未知键必须在**所有**判定里一致回落成默认预设，包括原生判定。

    否则会得到「标签/取色按默认、原生判定却不按默认」的半吊子外观
    （同一件事两个真相）。

    【V0.0.19-E】回落目标从写死 classic 改为钉 DEFAULT_PRESET ——默认皮肤
    已按用户拍板改成现代风（非原生预设）。本条护栏的本意从来是「未知键
    与默认预设**处处一致**」，所以钉一致性关系：先按默认预设渲染取基准
    样式表，再用未知键渲染，两者必须逐字相等；原生判定同样钉关系。
    """
    assert theme.preset_is_native("不存在") == theme.preset_is_native(
        theme.DEFAULT_PRESET)
    assert theme.preset_is_native(None) == theme.preset_is_native(
        theme.DEFAULT_PRESET)
    theme.apply_preset(qapp, theme.DEFAULT_PRESET)
    base_sheet = qapp.styleSheet()
    theme.apply_preset(qapp, "不存在")
    assert qapp.styleSheet() == base_sheet, (
        "未知预设 apply_preset 后应与默认预设的渲染结果一致")
    theme.apply_preset(qapp, "不存在也不存在的键")
    assert qapp.styleSheet() == base_sheet
    # 默认预设若是原生键（classic 时代的行为），一致性自动等价于
    # 「等于 _WM_PENETRATE」；现代风下则等价于「等于 modern 的 QSS」。
    if theme.preset_is_native(theme.DEFAULT_PRESET):
        assert base_sheet == theme._WM_PENETRATE


def test_native_preset_survives_a_set_skin_round_trip(qapp):
    """经主窗口的 set_skin 切到 classic 也必须真原生（真实用户路径）。"""
    from media_resource_manager.gui import main_window as mw
    src = io.open(os.path.join(GUI, "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "set_skin"), None)
    assert fn is not None, "找不到 set_skin"
    calls = [c for c in ast.walk(fn)
             if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute)
             and c.func.attr == "apply_preset"]
    assert calls, "set_skin 没有调 apply_preset —— 切皮肤将不生效"
    assert mw is not None