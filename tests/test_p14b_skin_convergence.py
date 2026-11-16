"""P1-4 收口护栏：GUI 里不许再有裸 `#xxxxxx` 硬编码色。

作业单要求「全仓 grep hex 归零」，但**字面照做会把已经正确的代码改坏** ——
本仓这三类 hex 是合法的，判它们违规就是错：

  ① `theme.py` 的**调色板定义区**：色值就该住在这里，它是唯一的真值源。
  ② **取色兜底默认值**：`_tc(dark, "accent", "#8b7cf8")` 的第三参。
     取得到键时压根不用它；取不到时崩掉比「显示不出颜色」更糟。
  ③ 模块级 `_C_*` 兜底常量（learning_board 约定）与 `icons._resolve_key`
     的最后兜底 —— 同 ②。

所以护栏用 **AST** 而不是 grep，分三类放行/拦阻。**这条护栏的价值已被
实测验证过**（R27）：只改色值字面量、忘了改调用方式，占位符会原样进 Qt，
而 **Qt 遇非法 QSS 是静默丢弃整段规则、不报任何错** → 界面「什么都没发生」
却永远测不出来。护栏同时盯「不许出现裸 hex」与「不许把占位符当色值传」。
"""
import ast
import io
import pathlib
import re
import tokenize

import pytest

PKG = pathlib.Path(__file__).resolve().parent.parent / "media_resource_manager"
THEME = PKG / "gui" / "theme.py"

HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
# 合法的「取色入口」别名：都是 theme.c 的不同叫法
COLOR_FNS = {"c", "_c", "_tc"}


def _py_files():
    for p in sorted(PKG.rglob("*.py")):
        if "__pycache__" not in p.parts:
            yield p


def _hexes(text: str):
    return HEX_RE.findall(text)


def _strip_comments_and_docstrings(src: str, tree):
    """返回「不算注释/文档」的字符串常量 [(lineno, value)]。

    ⚠ 只剥 docstring 不剥注释，或反过来，都会大面积误判 —— 本项目关键信息
    大量写在注释里（R26/R27 各踩一次）。两条都要，且 docstring 要按 AST 的
    body[0] 精确识别（不能按三引号正则切，会把含三引号的普通字符串切坏）。
    """
    comment_lines = set()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            comment_lines.add(tok.start[0])

    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                doc_lines.update(range(body[0].lineno, body[0].end_lineno + 1))

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in comment_lines or node.lineno in doc_lines:
                continue
            out.append((node.lineno, node.value))
    return out


def _fallback_lines(tree):
    """算出「合法兜底」的行号集合（= 放行 ② 与 ③）。"""
    lines = set()
    for node in ast.walk(tree):
        # 取色函数的第三参
        if isinstance(node, ast.Call) \
                and getattr(node.func, "id", "") in COLOR_FNS \
                and len(node.args) >= 3 \
                and isinstance(node.args[2], ast.Constant) \
                and isinstance(node.args[2].value, str):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        # 多行表达式里若整块都含兜底，按节点区间覆盖
        elif isinstance(node, ast.Call) \
                and getattr(node.func, "id", "") in COLOR_FNS:
            seg = ast.unparse(node)
            if len(node.args) >= 3 and _hexes(seg):
                lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    # 模块级 _C_* 兜底常量
    for node in tree.body:
        tgts = []
        if isinstance(node, ast.Assign):
            tgts = node.targets
        elif isinstance(node, ast.AnnAssign):
            tgts = [node.target]
        for tgt in tgts:
            if isinstance(tgt, ast.Name) and tgt.id.startswith("_C_"):
                lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def _violations(path: pathlib.Path):
    """返回该文件里真正违规的 [(lineno, hex, 该行源码)]。"""
    src = path.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    fb = _fallback_lines(tree)
    rel = path.relative_to(PKG.parent).as_posix()
    if rel.endswith("gui/icons.py"):
        # _resolve_key 的最后兜底 return（theme 全挂时的浅色字）
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_resolve_key":
                fb.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    lines = src.split("\n")
    bad = []
    for lineno, val in _strip_comments_and_docstrings(src, tree):
        hexes = _hexes(val)
        if not hexes or lineno in fb:
            continue
        bad.append((lineno, ",".join(sorted(set(hexes))),
                    lines[lineno - 1].strip()[:80]))
    return bad


# ── 静态护栏（作业单的「grep 归零」）───────────────────────────────────
def test_no_hardcoded_hex_outside_theme():
    """theme.py 之外不许再有裸 hex 字面量。

    theme.py **本身**也参与扫描但不在断言范围 —— 它是调色板定义区，
    色值必须住在那里，否则「色值的唯一真值源」就散了。
    """
    offenders = {}
    for p in _py_files():
        bad = _violations(p)
        if bad and p.resolve() != THEME.resolve():
            offenders[p.relative_to(PKG.parent).as_posix()] = bad
    assert not offenders, (
        "以下文件又出现硬编码色（换皮肤时这些地方不会跟着变）：\n"
        + "\n".join("  %s: %s" % (f, v) for f, v in offenders.items())
        + "\n修法：QSS 用 theme.style(控件, \"...{key}...\")；"
          "要具体色值用 theme.c(dark, \"key\", \"#兜底\")。")


def test_theme_palette_defines_every_key_both_modes():
    """调色板新增的语义位，dark/light 两边都必须有 —— 缺一边就是半成品。

    这是本轮加键时最容易犯的错：只补了 dark 忘了 light，于是深色下正常、
    浅色下静默退回兜底色（看起来「有个颜色」，所以永远测不出来）。
    """
    from media_resource_manager.gui import theme
    dk = set(theme.PALETTE["dark"])
    lt = set(theme.PALETTE["light"])
    missing_light = sorted(dk - lt)
    missing_dark = sorted(lt - dk)
    assert not missing_light, "深色有、浅色缺的语义键：%s" % missing_light
    assert not missing_dark, "浅色有、深色缺的语义键：%s" % missing_dark


def test_palette_still_has_the_old_contract_keys():
    """老键不许被本轮改名/删除 —— 已有大量调用点按名字取色。

    断言「键还在」而不是「键的值是多少」：换皮肤时值本来就该变。
    """
    from media_resource_manager.gui import theme
    need = {"bg", "surface", "surface_alt", "border", "text", "text_dim",
            "on_accent", "warn_bg", "warn_border", "warn_text", "warn_solid",
            "danger", "neg", "ok"}
    for mode in ("dark", "light"):
        missing = sorted(need - set(theme.PALETTE[mode]))
        assert not missing, "%s 模式缺老键：%s" % (mode, missing)


# ── 动态护栏：换皮肤后这些控件必须真的变色 ────────────────────────────
def test_status_dot_color_follows_skin():
    """任务状态点必须跟随皮肤 —— `st_done` 原先写死 #8b7cf8（violet 专属）。

    断言**关系**（换皮肤后值不同）而不是某个具体 hex。
    """
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import status_color

    seen = {}
    for skin in theme.SKINS:
        theme._CURRENT_SKIN = skin
        try:
            seen[skin] = status_color("done", dark=False)
        finally:
            theme._CURRENT_SKIN = "violet"
    # 「已完成」跟随皮肤 → 不同皮肤应给出不同（至少不是全部相同）
    assert len(set(seen.values())) > 1, (
        "状态点「已完成」色不随皮肤变化 = 皮肤半成品：%s" % seen)


def test_status_colors_are_dark_mode_aware():
    """状态点色必须区分深浅 —— 深色下把深色字画在深底上等于看不见。"""
    from media_resource_manager.gui.main_window import status_color
    # 用 text 类色位验证深浅切换链路是通的（不写死具体 hex）
    lt = status_color("idle", dark=False)
    dk = status_color("idle", dark=True)
    assert isinstance(lt, str) and isinstance(dk, str)
    assert lt.startswith("#") and dk.startswith("#")


def test_task_row_text_colors_come_from_palette():
    """任务行三档文字色（name/sub/st）必须取自调色板而非三元写死。

    断言走 **AST 字符串常量**而不是全文搜 —— 上一版用 `'"#e7eaf0" if' not in src`
    结果匹配到我自己写的那段**解释性注释**（注释里正 quoting 着改掉前的写法），
    假红。教训与本文件开头一致：注释里全是「曾经错在哪」的说明。
    """
    p = PKG / "gui" / "main_window.py"
    src = p.read_text(encoding="utf-8", errors="ignore")
    tree = ast.parse(src)
    fb = _fallback_lines(tree)
    # 找出所有「三元的条件分支里出现 hex」的表达式 = 写死的双套配色。
    # ⚠ 必须排除**兜底行**：`_tc(dark,"grade_ok","#22c55e") if cond else ...`
    # 这种三元里那个 hex 只是 `theme.c` 的第三参（取不到键时的兜底），
    # **不是**在选颜色 —— 三元选的是「哪一档语义色」。判它违规就误伤了。
    ternaries = [n for n in ast.walk(tree)
                 if isinstance(n, ast.IfExp) and _hexes(ast.unparse(n))
                 and n.lineno not in fb]
    assert not ternaries, (
        "还有 %d 处「深色字 if _dark else 浅色字」式的写死配色（行 %s）—— "
        "两套值应住在 theme.PALETTE，代码里只表达要哪一档"
        % (len(ternaries), sorted({n.lineno for n in ternaries})))
    # 第三档文字色仍要走调色板的 text_faint 键。
    # ⚠ R80：原判据是字面量 `'_tc(_dark, "text_faint"' in src`，TaskRow 三处
    #   文字改走 theme.style 登记式模板（_tstyle）后那条恒假红 —— 判据该问
    #   「用没用 text_faint 这个键」，不该问「用哪个函数」。改成 AST：
    #   存在一个 _tstyle 调用，其模板里带 {text_faint}。
    _tf = [n for n in ast.walk(tree)
           if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_tstyle"
               and any(isinstance(a, ast.Constant) and "{text_faint}" in str(a.value)
                       for a in n.args))]
    assert _tf, (
        "TaskRow 第三档文字没有走调色板 text_faint 键（R80 起应是 "
        "_tstyle(..., \"color:{text_faint};...\") 形式）")


def test_icons_default_color_follows_theme():
    """`icons.icon()` 不传色时必须按当前皮肤取色。

    原先默认参数是写死的深色字 —— 任何没显式传色的调用点，
    在深色主题下都是**看不见的图标**。
    """
    from media_resource_manager.gui import icons, theme
    a = icons._resolve_key("{text}")
    assert a.startswith("#"), "取色失败应回退到合法 hex"
    old = theme._CURRENT_DARK
    try:
        theme._CURRENT_DARK = False
        lt = icons._resolve_key("{text}")
        theme._CURRENT_DARK = True
        dk = icons._resolve_key("{text}")
    finally:
        theme._CURRENT_DARK = old
    assert lt != dk, (
        "图标默认色不随深浅变化 —— 深色主题下默认深色字 = 图标不可见")


def test_icons_fallback_is_light_enough_for_dark_mode():
    """兜底色必须够浅。

    `_resolve_key` 的兜底只在「theme 取不到色」时用，而最常见的触发原因
    **正是深色主题** —— 兜底给深色字等于「在最需要它的场景下恰好失效」。

    怎么测兜底分支：不能靠删 `sys.modules`（`icons` 里 `from . import theme`
    会在包命名空间留下引用，删了也不生效 —— 上一版就是这么假通过的）。
    改用 monkeypatch 把 `theme.css` 弄成抛异常，逼它真的走 except。
    """
    from media_resource_manager.gui import icons, theme

    def _boom(*_a, **_k):
        raise RuntimeError("模拟 theme 整个不可用")

    orig = theme.css
    theme.css = _boom
    try:
        out = icons._resolve_key("{text_dim}")
    finally:
        theme.css = orig
    assert out.startswith("#"), "兜底必须返回合法 hex，实际 %r" % out
    r, g, b = int(out[1:3], 16), int(out[3:5], 16), int(out[5:7], 16)
    assert (r + g + b) / 3 >= 140, (
        "兜底色亮度太低（%s）—— 深色主题下会看不见" % out)


def test_themed_icon_toggle_buttons_registered():
    """两个视图切换按钮必须走 set_themed_icon（否则图标不跟随皮肤）。"""
    p = PKG / "gui" / "main_window.py"
    src = p.read_text(encoding="utf-8", errors="ignore")
    assert 'icons.icon("grid" if' not in src, (
        "main_window 的视图切换按钮又用 icons.icon(color=...) 了 —— "
        "QIcon 构造那刻就把颜色烤死，必须走 set_themed_icon 才会重烤")
    p2 = PKG / "gui" / "review_tab.py"
    src2 = p2.read_text(encoding="utf-8", errors="ignore")
    assert 'icons.icon("grid" if' not in src2, (
        "review_tab 的视图切换按钮同样必须走 set_themed_icon")


def test_semantic_colors_stay_fixed_across_skins():
    """语义色（评分三档 / 状态点 / 排除红）刻意**不**跟随皮肤 —— 护住它。

    为什么反着断言：这两个需求方向相反，只钉「不许写死」会让人把这些
    色也接上皮肤，结果换肤后「失败」和强调色撞色、状态就读不出来了。
    """
    from media_resource_manager.gui import theme
    vals = {}
    for skin in theme.SKINS:
        theme._CURRENT_SKIN = skin
        try:
            vals[skin] = theme.palette(False).get("grade_bad")
        finally:
            theme._CURRENT_SKIN = "violet"
    assert len(set(vals.values())) == 1, (
        "grade_bad 是「不合格」的语义色，不该跟皮肤变（现在会变）：%s" % vals)


def test_style_placeholders_are_resolvable():
    """所有 `_tstyle` 模板里的 `{key}` 都必须真的能从调色板取到。

    ⚠ 这条护栏的价值：Qt 遇**非法 QSS 是静默丢弃整段规则、不报任何错**。
    所以「模板里占位符拼错了」在界面上表现为「什么都没发生」，
    跑测试永远发现不了 —— 只能在这里静态拦住。
    """
    import re
    from media_resource_manager.gui import theme
    pat = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
    bad = {}
    for p in _py_files():
        src = p.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)
        fb = _fallback_lines(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") in ("_tstyle", "style")):
                continue
            if len(node.args) < 2 or not isinstance(node.args[1], ast.Constant):
                continue
            if not isinstance(node.args[1].value, str):
                continue
            tpl = node.args[1].value
            missing = []
            for key in pat.findall(tpl):
                # 只查语义色位（{border} 之类），放过 QSS 自己的选择器语法
                if key in ("border", "background", "color", "font-size"):
                    continue
                if key in theme.palette(True) or key in theme.palette(False):
                    continue
                missing.append(key)
            if missing:
                bad.setdefault(
                    p.relative_to(PKG.parent).as_posix(), set()).update(missing)
    assert not bad, (
        "模板里的占位符取不到对应色位（Qt 会静默丢弃整段规则，界面无反应）："
        "%s" % bad)


@pytest.mark.parametrize("name", ["st_running", "st_queued", "st_idle",
                                  "st_done", "st_error", "st_stopped",
                                  "grade_ok", "grade_warn", "grade_bad",
                                  "card_bg", "card_border", "row_hover",
                                  "text_faint"])
def test_new_semantic_keys_resolve(name):
    """本轮新增的语义键，dark/light 都要能取到合法 hex。"""
    from media_resource_manager.gui import theme
    for mode, dark in (("dark", True), ("light", False)):
        got = theme.c(dark, name, None)
        assert got is not None, "%s 模式取不到键 %s" % (mode, name)
        assert re.match(r"^#[0-9a-fA-F]{6}$", got), \
            "%s 模式的 %s 不是合法 hex：%r" % (mode, name, got)
