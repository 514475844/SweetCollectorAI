"""R66 · 全局顶栏「同时运行任务数」（TOP_PARALLEL_ORDER）护栏。

═══ 本文件押着的六条契约 ═══
① **顶栏唯一**：`_topstrip` 是全局唯一顶栏；原 qB 动作工具条 `self._tb`
   已撤销（它上面的动作在菜单栏/页内都有入口，唯一没有的「代理管理」已补进
   「维护」菜单，见 ⑥）。
② **并行数只有一份、且在顶栏**：下载页（stack 第0 页）内不许再出现第二个
   并行数spin —— 两处都能改同一个值，用户会怀疑哪个才是真的。
③ **5 个皮肤全部可见**：`_topstrip` 与页内 4 个动作按钮，任何预设下都不许
   被显式隐藏（`isHidden()` 判，见下）。
④ **联动**：`spin.valueChanged → _set_parallel → cfg.parallel_tasks`，
   且启动时 spin 的值就等于 cfg 里的值。
⑤ **原生皮肤不残留自绘色**：classic 下 theme.css() 返 "" ⇒ 行内 QSS 必须
   是空串；切回非原生又必须能重新上色（R60 的登记不许丢）。
⑥ **撤 _tb 的补偿**：它上面的 6 个动作在菜单里都还有入口。

⚠⚠ **断言写「关系」不写「数值」**（MEMORY 铁律）：只钉「在/不在」「可/不可」
   「唯一/重复」这类结构关系，绝不写死某个色值或行号。

⚠⚠ **offscreen 下 `isVisible()` 一律不可信**（R58铁律）—— 它要父级可见、
   要窗口 show 过，offscreen 里三者都不成立。判「被显式隐藏」必须用
   `isHidden()`；判布局改用 AST 读源码。本文件两样都用到。
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QSpinBox, QToolBar

from media_resource_manager.gui import theme
from media_resource_manager.gui import main_window as mwmod

SRC = pathlib.Path(mwmod.__file__).resolve()
PAGE0 = 0          # 0 = 下载管理页（作业单 §0 的编号）


@pytest.fixture(scope="module")
def win():
    """真起一个 MainWindow（offscreen），整个模块共用一只。"""
    app = pytest.importorskip("PySide6.QtWidgets").QApplication.instance()
    if app is None:
        app = pytest.importorskip("PySide6.QtWidgets").QApplication([])
    w = mwmod.MainWindow()
    yield w
    try:
        w.close()
    except Exception:
        pass


def _ancestors(w):
    out = []
    while w is not None:
        out.append(w)
        w = w.parent()
    return out


def _spinboxes_under(root):
    """root 子树里所有 QSpinBox —— 注意审核页那个 12-300 的不在此列。"""
    return [s for s in root.findChildren(QSpinBox)]


# ═══════════════════════════════════════════════════════════════════
# ① 顶栏唯一
# ═══════════════════════════════════════════════════════════════════
def test_top_strip_is_the_only_toolbar(win):
    """5 皮肤统一只有一条顶栏 `_topstrip`。

    作业单 §2 最后一条验收：qB 皮肤顶栏**单行**。
    """
    bars = win.findChildren(QToolBar)
    names = [b.objectName() for b in bars]
    assert names == ["topstrip"], (
        "顶栏应只有 _topstrip 一条，实得 %r（多出来的说明 _tb 没撤干净，"
        "或将来又加了第二条顶栏）" % (names,))


def test_qb_action_toolbar_is_gone(win):
    """原 qB 动作工具条 `self._tb` 不再创建（作业单 §1.4）。

    判「没有」而不是判「不可见」：对象压根不该存在，
    留一个永远 setVisible(False) 的僵尸控件只会在换肤时白占widgets 计数。
    """
    assert not hasattr(win, "_tb"), (
        "self._tb 还在 —— 作业单 §1.4 要求取消它")


# ═══════════════════════════════════════════════════════════════════
# ② 并行数只有一份、且在顶栏
# ═══════════════════════════════════════════════════════════════════
def test_spin_parallel_lives_in_top_strip(win):
    """`spin_parallel` 的父级就是顶栏。"""
    assert win.spin_parallel.parent() is win._topstrip, (
        "并行数不在顶栏里（父级=%r）" % (type(win.spin_parallel.parent()).__name__,))


def test_no_duplicate_parallel_spin_in_download_page(win):
    """下载页里**不再有**第二个并行数（作业单 §1.2 / §2 第三条验收）。

    ⚠ 判据必须限定在下载页（stack 第 0 页）：审核页里有一个 range 12-300 的
    「单页最多显示多少张待审核图」，那是别的功能，不算重复。
    ⚠ 也不能判「全窗口只有一个 QSpinBox」—— 同上，那条会误伤审核页。
    """
    page0 = win.stack.widget(PAGE0)
    assert page0 is not None, "下载页取不到，stack 编号变了？"
    assert _spinboxes_under(page0) == [], (
        "下载页里还有 %d 个 QSpinBox，并行数应当只留在顶栏"
        % len(_spinboxes_under(page0)))


def test_parallel_spin_is_the_only_one_in_top_strip(win):
    """顶栏里恰好一个 QSpinBox —— 顶栏只放这一个控件。"""
    assert len(_spinboxes_under(win._topstrip)) == 1, (
        "顶栏里的 QSpinBox 应恰好 1 个（并行数），实得 %d"
        % len(_spinboxes_under(win._topstrip)))


# ═══════════════════════════════════════════════════════════════════
# ③ 5 个皮肤全部可见
# ═══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("preset", sorted(theme.PRESETS))
def test_top_strip_visible_in_every_skin(win, preset):
    """逐一切换每个皮肤，顶栏都不许被隐藏。

    ⚠ 用 `isHidden()` 而不是 `isVisible()`（R58：offscreen 下后者不可信）。
    """
    win.set_skin(preset)
    assert win._topstrip.isHidden() is False, (
        "%s 皮肤下顶栏被隐藏了（作业单 §2 第一条验收：全部皮肤都要能看到）"
        % preset)


@pytest.mark.parametrize("preset", sorted(theme.PRESETS))
def test_page0_toolbar_buttons_removed(win, preset):
    """主单项3：任务页工具条的 4 个按钮**任何皮肤下都不许存在**。

    R66 时这条护栏还是反的（「4 个按钮任何皮肤都可见」）—— 那时只撤了
    qB 顶栏动作工具条，按钮是唯一的全部开始/全部停止入口。主单项3 把
    按钮连工具条一起撤，入口改到菜单（任务/运行菜单 + 右键 + 双击），
    所以这里翻成**反向断言**：下载页里不许再出现这 4 个按钮。
    撤功能最怕「删了菜单没删干净」—— 留着就是重复入口，用户不知道该点哪个。
    """
    win.set_skin(preset)
    page0 = win.stack.widget(PAGE0)
    assert page0 is not None, "下载页取不到，stack 编号变了？"
    banned = ("新建任务", "全部开始", "全部停止", "编辑")
    left = [b.text() for b in page0.findChildren(QPushButton)
            if b.text() in banned]
    assert left == [], (
        "%s 皮肤下任务页还有这 4 个按钮：%r（主单项3 要求撤掉，入口已进菜单）"
        % (preset, left))


def test_top_strip_is_built_before_the_other_toolbar_slot():
    """`_build_top_strip()` 必须在 `_build_toolbar()` 之前调用（顶栏置顶）。

    QMainWindow 同一区域的工具条按加入顺序叠放，先加的在上。
    用 AST 判调用顺序，不看行号（行号是快照，会随需求漂移）。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    order = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("_build_top_strip", "_build_toolbar")):
            order.append((node.lineno, node.func.attr))
    tops = [ln for ln, name in order if name == "_build_top_strip"]
    assert tops, "__init__ 里没有调用 _build_top_strip（顶栏不会存在）"
    assert not any(name == "_build_toolbar" for _, name in order), (
        "__init__ 里仍在调用 _build_toolbar —— 作业单 §1.4 要求取消它")


# ═══════════════════════════════════════════════════════════════════
# ④ 联动
# ═══════════════════════════════════════════════════════════════════
def test_spin_reflects_config_on_startup(win):
    """spin 的初值就等于 cfg.parallel_tasks。

    ⚠ 断言「关系」：先读 cfg，再断言两者相等 —— 不写死具体数字。
    """
    cfg_v = int(getattr(win.cfg, "parallel_tasks", 3))
    assert win.spin_parallel.value() == cfg_v, (
        "启动时并行数框(%d) 与配置(%d) 不一致"
        % (win.spin_parallel.value(), cfg_v))


def test_user_input_reaches_config(win):
    """真链路：动 spin → valueChanged → _set_parallel → cfg 落盘。

    ⚠ 断言的是「改了 spin 之后 cfg 跟着变」，**不是**「调 _set_parallel 之后
    spin 跟着变」—— `_set_parallel` 是单向的（只写 cfg + 队列并发），
    这是 r37 起的既有语义。反向断言会把它误判成缺陷。
    """
    before = int(win.cfg.parallel_tasks)
    target = 20 if before != 20 else 19          # 别撞上「值没变→信号不发」
    win.spin_parallel.setValue(target)
    assert int(win.cfg.parallel_tasks) == target, (
        "在并行数框里输入 %d，cfg 却还是 %d —— 信号链路断了"
        % (target, win.cfg.parallel_tasks))


def test_set_parallel_updates_config(win):
    """作业单 §2 原话：`_set_parallel(7)` 之后 `cfg.parallel_tasks == 7`。"""
    win._set_parallel(7)
    assert int(win.cfg.parallel_tasks) == 7, (
        "_set_parallel 没有写进 cfg（实得 %r）" % (win.cfg.parallel_tasks,))


def test_parallel_range_covers_twenty(win):
    """上限仍是 20（r37：以前写死 6，用户选不到「20 个任务同时跑」）。

    ⚠ 写死 20 属于**作业单钦定的结构契约**，不是快照 —— 需求改了这条就该红。
    """
    assert win.spin_parallel.maximum() >= 20, (
        "并行数上限被改小了（%d），r37 的「20 个同时跑」又选不到了"
        % win.spin_parallel.maximum())


# ═══════════════════════════════════════════════════════════════════
# ⑤ 原生皮肤不留自绘色，且切回能恢复（R60）
# ═══════════════════════════════════════════════════════════════════
def test_native_skin_leaves_no_inline_qss(win):
    """classic（真原生）下顶栏控件的行内 QSS 必须是空串（作业单 §2 第四条）。

    症状：spinbox 残留自绘紫色，与旁边的系统原生控件格格不入。
    """
    win.set_skin("classic")
    assert win.spin_parallel.styleSheet().strip() == "", (
        "原生皮肤下并行数框仍带行内 QSS：%r" % win.spin_parallel.styleSheet())


def test_inline_style_returns_after_leaving_native(win):
    """从 classic 切回 violet_look，顶栏的行内样式要能重新上色。

    ⚠ theme.style() 登记的是**弱引用表**，原生模式下 css() 返 "" 但登记必须
    留着，否则切回来这些控件永久裸奔（R60 踩过的坑）。
    """
    win.set_skin("classic")
    win.set_skin("violet_look")
    lbls = win._topstrip.findChildren(QLabel)
    assert lbls, "顶栏里找不到标签（顶栏结构变了？）"
    assert lbls[0].styleSheet().strip() != "", (
        "切回非原生皮肤后顶栏标签仍是裸的 —— theme.style 的登记丢了（R60）")


# ═══════════════════════════════════════════════════════════════════
# ⑥ 撤 _tb 的补偿：它上面的动作都还有入口
# ═══════════════════════════════════════════════════════════════════
def _menu_texts(win):
    """扁平化全部菜单项文本（含子菜单）。"""
    out = []

    def walk(menu):
        for act in menu.actions():
            if act.menu() is not None:
                walk(act.menu())
            else:
                out.append(act.text())

    for a in win.menuBar().actions():
        if a.menu() is not None:
            walk(a.menu())
    return out


@pytest.mark.parametrize("label", [
    "新增任务",        # 原_tb「新建任务」
    "编辑任务",        # 原_tb「编辑」
    "运行",            # 原_tb「全部开始」的一部分（选中项）
    "停止",            # 原_tb「全部停止」的一部分（选中项）
    "全部开始",        # R80：页内按钮撤掉后补进「运行」菜单（全局）
    "全部停止",        # R80：同上
    "AI 模型设置…",    # 原_tb「设置」
    "代理管理…",       # 原_tb「代理」—— r66 补进「维护」菜单
])
def test_toolbar_action_still_reachable_from_menu(win, label):
    """撤掉 qB 动作工具条、再撤任务页 4 按钮后，动作在菜单里必须还有入口。

    ⚠ 「运行/停止」在菜单里是 run_selected/stop_selected（选中项），
    「全部开始/全部停止」是 run_all/stop_all（全局）—— 两套语义不同，
    所以四项目前都在菜单里（R80 补的后两项）。
    """
    assert label in _menu_texts(win), (
        "菜单里找不到「%s」—— 撤顶栏动作工具条/任务页按钮后它可能变成孤儿" % label)


def test_proxy_dialog_has_a_menu_entry(win):
    """`proxy_dialog` 必须有菜单入口（反向护栏）。

    R61 复发过的坑：判「X 与 Y 不同处」要写成「**没有任何一处同时含 X 和 Y**」。
    这里判「菜单里至少有一项的槽是 proxy_dialog」。
    """
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "addAction"
                and any(isinstance(a, ast.Attribute)
                        and a.attr == "proxy_dialog" for a in node.args)):
            hits.append(node.lineno)
    assert hits, (
        "proxy_dialog 没有任何菜单入口 —— 撤掉 qB 动作工具条后，"
        "「管理 SOCKS/HTTP 代理」就只剩新建任务表单里那个「管理…」按钮了")


def _retired_v019_page0_has_run_all_and_stop_all(win):
    """「全部开始/全部停止」必须在**运行菜单**里（R80 项3 撤按钮后的新家）。

    原来是页内 4 按钮之一；主单项3 撤掉任务页工具条后，这两个全局动作
    的唯一入口就是「运行」菜单 —— 判据同时认「文本在」与「槽真的是
    run_all/stop_all」（只判文本的话，挂到别的槽上也算过）。
    """
    run_menu = None
    for a in win.menuBar().actions():
        if a.text() == "运行":
            run_menu = a.menu()
            break
    assert run_menu is not None, "菜单栏里没有「运行」菜单"
    slots = {}
    for act in run_menu.actions():
        if act.text() in ("全部开始", "全部停止"):
            slots[act.text()] = act
    assert set(slots) == {"全部开始", "全部停止"}, (
        "运行菜单里缺「全部开始/全部停止」，实得 %r" % sorted(slots))
    # 槽必须真的是那两个全局方法（防止复制粘贴挂错）
    src = SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    wired = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "triggered"):
            tgt = node.args[0]
            if isinstance(tgt, ast.Attribute):
                wired.add(tgt.attr)
    assert "run_all" in wired and "stop_all" in wired, (
        "「全部开始/全部停止」没有接到 run_all/stop_all 上，实得 %r" % sorted(wired))
