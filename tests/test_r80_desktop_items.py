"""r80：主单桌面剩余 5 项（3 / 6 / 7 / 11 / 14）的功能锁。

一项一闭环，全部**行为级可证**（真起窗口、真点按钮、真读样式），
AST 只做「入口还在 / 没烤死颜色」这类结构性兜底。

  项3  任务页工具条删 新建任务/全部开始/全部停止/编辑 4 个按钮；
       全部开始/全部停止 补进「运行」菜单（R66 核对过菜单里原本没有）。
  项6  对话框「全部填满」按钮左移到最左、顶住左边（撤掉 92px 标签占位）。
  项7  审核页加「刷新」按钮（「待审核」标题最右、自动补满下方）。
  项11 所有列表文字看不清 → 真凶是 TaskRow 把颜色**建行时烤死**，
       换肤不改任务指纹 ⇒ L2 增量刷新不重建行 ⇒ 浅色切暗夜风后
       深字坐暗底（实测对比度 1.4）。修法：三处文字改走 theme.style
       登记式行内样式，换肤时 _restyle_registered 统一重渲。
  项14 清空隔离区后列表未刷新 → 隔离区管理对话框四个回调原来只刷
       主窗口（self.refresh()），对话框自己的表格纹丝不动；改成
       reload_rows()（对话框表格）+ self.refresh()（主窗口）都刷。

铁律（本项目多次复发，写判据前默念）：
  · 断言「关系」不「数值」；判崩溃/看不见用 WCAG 对比度这种可量化关系；
  · 撤功能要改反向断言（项3 把 R66 两条护栏翻向，见 test_r66 同轮修改）；
  · 变异测试必须真会红（R73/R79 都抓到过空壳判据）。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (QApplication, QDialog, QLabel,  # noqa: E402
                               QMessageBox, QPushButton)

from media_resource_manager.gui import main_window as mwmod  # noqa: E402
from media_resource_manager.gui import theme  # noqa: E402
from media_resource_manager.models import ImageRecord, TaskConfig, TaskStatus  # noqa: E402
from media_resource_manager.repo import list_tasks, save_task, upsert_image  # noqa: E402

SRC = pathlib.Path(mwmod.__file__).resolve()
PAGE0 = 0


# ----------------------------------------------------------------------- #
# 夹具：隔离 MRM_HOME 的真窗口（不碰真实数据目录）
# ----------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def win(tmp_path_factory):
    home = tmp_path_factory.mktemp("r80_home")
    old = os_environ_get("MRM_HOME")
    set_environ("MRM_HOME", str(home))
    set_environ("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, False, "violet_look")
    w = mwmod.MainWindow()
    # 灌一条任务 + 三张隔离区图（TaskRow / 隔离区表格要有东西可测）
    save_task(w.db, TaskConfig(
        id=None, name="R80任务", adapter_name="generic",
        entry_urls=["https://example.invalid/r80"], keywords=["r80"],
        status=TaskStatus.IDLE))
    tid = list_tasks(w.db)[0].id
    for i in range(3):
        upsert_image(w.db, ImageRecord(
            id=None, source_url=f"https://example.invalid/r80/q{i}.jpg",
            task_id=tid, width=640, height=480, bytes=12345, path=None,
            score=0.2, in_quarantine=True, needs_review=False, tags=[],
            fmt="JPEG"))
    w.refresh()
    # show 一下让所有布局真正激活（offscreen 下不 show 时 mapTo 全是 0，
    # 位置类断言会假红/假绿 —— R53 纪律：先怀疑仪器）。
    w.show()
    app.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass
    if old is None:
        pop_environ("MRM_HOME")
    else:
        set_environ("MRM_HOME", old)


def os_environ_get(k):
    import os
    return os.environ.get(k)


def set_environ(k, v):
    import os
    os.environ[k] = v


def pop_environ(k):
    import os
    os.environ.pop(k, None)


# ----------------------------------------------------------------------- #
# WCAG（与 test_r68_skin_presets 同口径）
# ----------------------------------------------------------------------- #
def _lum(rgb):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(fg, bg):
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _hx(s):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _style_color(widget):
    """从行内样式表里抠 color:#rrggbb（Qt 渲染时用的就是这个值）。"""
    m = re.search(r"color:\s*(#[0-9a-fA-F]{6})", widget.styleSheet() or "")
    return m.group(1) if m else None


def _row_widgets(win):
    """任务列表里所有 TaskRow 行控件（卡片视图）。"""
    out = []
    for i in range(win.task_list.count()):
        it = win.task_list.item(i)
        w = win.task_list.itemWidget(it) if it is not None else None
        if w is not None:
            out.append(w)
    return out


def _text_labels(row):
    """行内带行内样式的文字标签（跳过那颗状态圆点 dot —— 它是纯 pixmap）。

    ⚠ 不能按 metaObject().className() == "QLabel" 筛：name/sub 是
    _ElideLabel（QLabel 的 Python 子类），className 是 "_ElideLabel"，
    按字符串筛会把它们全滤掉（本轮仪器错，StopIteration）。
    """
    return [c for c in row.findChildren(QLabel)
            if "font-size" in (c.styleSheet() or "")]


# ═══════════════════════════════════════════════════════════════════
# 项3：任务页 4 按钮撤掉 + 运行菜单补「全部开始/全部停止」
# ═══════════════════════════════════════════════════════════════════
BANNED_BTN_TEXTS = ("新建任务", "全部开始", "全部停止", "编辑")


@pytest.mark.parametrize("preset", sorted(theme.PRESETS))
def test_download_page_has_no_four_action_buttons(win, preset):
    """项3：任务页工具条 4 个按钮任何皮肤下都不许存在（撤功能要撤干净）。"""
    win.set_skin(preset)
    page0 = win.stack.widget(PAGE0)
    left = [b.text() for b in page0.findChildren(QPushButton)
            if b.text() in BANNED_BTN_TEXTS]
    assert left == [], "%s 皮肤下任务页还剩这些按钮：%r" % (preset, left)


@pytest.mark.parametrize("preset", ["violet_look", "modern", "night",
                                    "qbittorrent"])
def test_every_skin_still_shows_its_accent(win, preset):
    """项3 的补偿护栏：撤掉 4 个按钮后，每个皮肤的画面上仍要看得到强调色。

    真事：4 个按钮是 modern 画面里**唯一**的强调色来源（QPushButton 默认
    样式就是强调色底），撤 buttons 后实测 modern 整窗 0 个强调色像素 ——
    换皮肤用户看不出来（test_r68 的「强调色因果」当场抓红）。
    修法：导航选中态改强调色实底（见 theme.py #navbtn:checked）。
    这条把 r68 只查 modern 的那条**推广到全部四个非原生皮肤**
    （violet_look 原先也是 0 —— r68 的覆盖缺口）。

    阈值取 100 个取样点（step=2）：选中导航块约 210×56px ≈ 2.9k 取样点，
    留两个数量级余量，又不是「有就行」的摆设。
    """
    win.set_skin(preset)
    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()
    img = win.grab().toImage()
    # 暗色皮肤画的是 ACCENT_D（深色 QSS 用 {ACCENT_D}），浅色画 ACCENT ——
    # 拿浅色那档去数暗夜风，恒 0（本轮仪器错，别拿去"修"产品）。
    accent = theme.SKINS[theme.preset_accent(preset)][
        1 if theme.preset_dark(preset) else 0]
    r, g, b = _hx(accent)
    n = 0
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            p = img.pixel(x, y)
            if (((p >> 16) & 255), ((p >> 8) & 255), p & 255) == (r, g, b):
                n += 1
    assert n > 100, (
        "%s 皮肤画面里强调色 %s 只剩 %d 个取样点 —— 换皮肤看不出来"
        % (preset, accent, n))


def _menu_texts(win, title):
    for a in win.menuBar().actions():
        if a.text() == title and a.menu() is not None:
            return [act.text() for act in a.menu().actions()]
    return []


def test_run_menu_carries_run_all_and_stop_all(win):
    """项3：「全部开始/全部停止」有菜单入口（V0.0.19 运行并入任务）。"""
    texts = _menu_texts(win, "任务")
    assert "全部开始" in texts and "全部停止" in texts, (
        "任务菜单里找不到「全部开始/全部停止」，实得 %r" % (texts,))


def test_task_menu_still_has_new_and_edit(win):
    """项3：新建/编辑的入口没被顺手带走（任务菜单 + 右键 + 双击）。"""
    texts = _menu_texts(win, "任务")
    assert "新增任务" in texts and "编辑任务" in texts, (
        "任务菜单缺入口：%r" % (texts,))


def test_run_all_actions_wired_to_global_slots():
    """项3：AST 兜底 —— 两个新 QAction 真的接到 run_all/stop_all 上。

    只判「菜单里有这两项」不够：挂到 run_selected 上也叫「有入口」，
    但语义变成「只跑选中的那个」—— 那正是 R66 踩过的坑。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    wired = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "triggered"):
            tgt = node.args[0] if node.args else None
            if isinstance(tgt, ast.Attribute) and isinstance(
                    tgt.value, ast.Name) and tgt.value.id == "self":
                wired.add(tgt.attr)
    assert {"run_all", "stop_all"} <= wired, (
        "run_all/stop_all 没有接到 triggered 上，实得 %r" % sorted(wired))


# ═══════════════════════════════════════════════════════════════════
# 项6：「全部填满」按钮顶到对话框最左
# ═══════════════════════════════════════════════════════════════════
def test_fill_buttons_are_flush_left(win):
    """项6：四行「全部填满」按钮整体顶到表单最左（不再有 92px 标签占位）。

    判据是**关系**：填充行左缘 == 面板内容左缘，且比上方输入框
    （f_ai_labels，前面有 92px 标签）还靠左至少一个标签宽。
    旧代码用 full_cell("", row) 套了一层 92px 占位标签 ⇒ 按钮缩在
    输入框右侧起排，用户看到的就是「没顶到左边」。
    """
    win._ensure_task_form()
    panel = getattr(win, "_form_panel", None)
    assert panel is not None, "任务表单面板没建出来（_ensure_task_form 静默失败）"
    # 面板平时是 hide 的（挂在主窗口下不占地方）⇒ 布局没激活过，
    # 不 activate 的话所有 x() 都是 0，位置断言全是假的。
    # ⚠ 只 activate 顶层布局不够：full_cell 里的**子**布局（92px 标签 +
    #   输入框）也不会被激活 ⇒ 输入框的 mapTo 仍返回 0。show 一下最稳。
    panel.show()
    app = QApplication.instance()
    app.processEvents()
    # 面板内容左缘：任一行 full_cell 的左缘（用一级主题词那一行当基准）
    labels_row = win.f_ai_labels.parent()          # half/full cell 容器
    base_x = labels_row.mapTo(panel, labels_row.rect().topLeft()).x()
    for attr in ("b_hist_labels", "b_hist_l2", "b_hist_l3", "b_hist_excl"):
        row = getattr(win, attr, None)
        assert row is not None, "%s 不见了（曾用词行被删了？）" % attr
        row_x = row.mapTo(panel, row.rect().topLeft()).x()
        assert abs(row_x - base_x) <= 1, (
            "%s 左缘 x=%d，应等于面板内容左缘 %d（还套着占位标签？）"
            % (attr, row_x, base_x))
        # 按钮必须是行内最左的那个控件
        btn = row.layout().itemAt(0).widget()
        assert btn is not None and btn.text() == "全部填满", (
            "%s 行内第一个控件不是「全部填满」按钮" % attr)
        btn_x = btn.mapTo(row, btn.rect().topLeft()).x()
        assert btn_x <= 1, "按钮在行内没顶左（x=%d）" % btn_x
        # 与上方输入框的关系：输入框前面有 92px 标签
        input_x = win.f_ai_labels.mapTo(
            panel, win.f_ai_labels.rect().topLeft()).x()
        assert input_x - row_x >= 80, (
            "按钮没有比输入框更靠左（row_x=%d input_x=%d）" % (row_x, input_x))


def test_fill_one_history_still_fills_its_own_field(win):
    """项6 的回归：挪位置不许把接线弄丢（只填当前这一级，不串级）。"""
    win._ensure_task_form()
    win.f_ai_labels.setText("")
    win.cfg.ai_label_history = ["白丝", "风景"]
    n = win._fill_one_history("ai_label_history", win.f_ai_labels)
    assert n >= 1 and "白丝" in win.f_ai_labels.text(), (
        "「全部填满」点了没反应：n=%r text=%r" % (n, win.f_ai_labels.text()))


# ═══════════════════════════════════════════════════════════════════
# 项7：审核页「刷新」按钮
# ═══════════════════════════════════════════════════════════════════
def test_review_page_has_refresh_button(win):
    """项7：审核页有「刷新」按钮，且在「待审核」标题那一行。"""
    rt = getattr(win, "_review_tab", None)
    assert rt is not None, "审核页组件不在（属性名变了？）"
    b = getattr(rt, "b_refresh", None)
    assert b is not None and b.text() == "刷新", "审核页没有「刷新」按钮"


def test_review_refresh_button_sits_right_of_title(win):
    """项7：按钮在标题**最右**（x 比标题大，且是行内最后一个控件）。"""
    rt = win._review_tab
    # 审核页是 stack 里的隐藏页 ⇒ 布局没激活，先 activate 再量位置
    rt.layout().activate()
    QApplication.instance().processEvents()
    row = rt.b_refresh.parent()
    assert row is not None, "刷新按钮没有父容器（不在信息行里）"
    xs = [w.x() for w in row.findChildren(QPushButton)]
    assert xs and rt.b_refresh.x() == max(xs), (
        "刷新按钮不在行内最右：x=%r all=%r" % (rt.b_refresh.x(), xs))
    assert rt.b_refresh.x() > rt.info.x(), "按钮跑到了标题左边"


def test_review_refresh_button_reloads(win, monkeypatch):
    """项7：点「刷新」真的调 reload(fill=True)（与右键菜单同一方法）。"""
    rt = win._review_tab
    calls = []
    monkeypatch.setattr(rt, "reload",
                        lambda fill=True: calls.append(fill))
    rt.b_refresh.click()
    assert calls == [True], "刷新按钮没有触发 reload(fill=True)：%r" % (calls,)


# ═══════════════════════════════════════════════════════════════════
# 项11：TaskRow 文字跟随皮肤（本轮真缺陷的回归锁）
# ═══════════════════════════════════════════════════════════════════
def test_task_row_labels_use_registered_styles():
    """项11（AST）：TaskRow 三处文字必须走 _tstyle 登记，不许再烤死颜色。

    反向判据：TaskRow.__init__ 里不许出现 `setStyleSheet(f"...color:{...}")`
    这种**建行时求值**的写法（那就是 R80 抓到的缺陷形态）。
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    row_cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "TaskRow")
    baked = []
    for node in ast.walk(row_cls):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setStyleSheet"
                and node.args and isinstance(node.args[0], ast.JoinedStr)):
            baked.append(node.lineno)
    assert baked == [], (
        "TaskRow 还有 f-string 烤死的行内样式（行 %r）—— 换肤不会重渲" % baked)
    tstyled = 0
    for node in ast.walk(row_cls):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_tstyle"):
            tstyled += 1
    assert tstyled >= 3, "TaskRow 里 _tstyle 只有 %d 处（三处文字都该走登记）" % tstyled


def test_task_row_text_follows_skin_switch(win):
    """项11（行为·核心回归）：浅色 → 暗夜风，旧行的字必须换成浅色。

    复现路径就是用户那条：行早就建好（L2 增量刷新不重建行）→ 换肤 →
    旧行拿着浅色调色板的深字坐在暗底上。修好后 _restyle_registered
    会把登记过的模板按新皮肤重渲，无需重建行。
    """
    win.set_skin("violet_look")
    win.refresh()
    rows = _row_widgets(win)
    assert rows, "卡片视图里没有 TaskRow（灌的任务没刷出来？）"
    row = rows[0]
    labels = _text_labels(row)
    assert labels, "TaskRow 里一个带样式的文字标签都没有（判据失效）"
    name_lbl = next(c for c in labels if "font-weight" in (c.styleSheet() or ""))
    light_color = _style_color(name_lbl)
    assert light_color is not None, "换肤前 name 标签就没有颜色（判据失效）"
    pal_l = theme.palette(False)
    assert light_color.lower() == pal_l["text"].lower(), (
        "浅色下 name 字色不是 palette.text：%r vs %r"
        % (light_color, pal_l["text"]))

    # ── 关键动作：只换肤，不重建行 ──
    win.set_skin("night")
    dark_color = _style_color(name_lbl)
    pal_d = theme.palette(True)
    assert dark_color is not None and dark_color.lower() == pal_d["text"].lower(), (
        "切到暗夜风后 name 字色没跟着换：%r（应为 %r）—— "
        "这就是「列表文字看不清」的缺陷形态" % (dark_color, pal_d["text"]))
    # 再切回浅色也要回得去（登记不能丢，R60 同款）
    win.set_skin("violet_look")
    back = _style_color(name_lbl)
    assert back is not None and back.lower() == pal_l["text"].lower(), (
        "切回浅色后字色没恢复：%r" % (back,))


@pytest.mark.parametrize("preset", sorted(theme.PRESETS))
def test_task_row_text_contrast_in_every_skin(win, preset):
    """项11：每个皮肤下 TaskRow 三处文字与底色的 WCAG 对比度 ≥ 4.5。

    原生皮肤（classic）下 css() 返 ""（行内样式整体失效、回系统调色板），
    那是 NATIVE_SKIN_ORDER §3.3 定下的行为 —— 这种皮肤判「样式为空」，
    不判对比度（系统调色板的可读性由 Windows 负责）。
    """
    win.set_skin(preset)
    win.refresh()
    rows = _row_widgets(win)
    if not rows:
        pytest.skip("%s 是表格视图，没有 TaskRow（由表格/系统调色板负责）" % preset)
    pal = theme.palette(theme.preset_dark(preset))
    native = theme.preset_is_native(preset)
    bg = _hx(pal["bg"])
    worst = 99.0
    checked = 0
    for row in rows:
        for lbl in _text_labels(row):
            color = _style_color(lbl)
            if color is None:
                if not native:
                    pytest.fail("%s 皮肤下标签没有行内颜色（登记丢了？）" % preset)
                continue
            checked += 1
            worst = min(worst, _contrast(_hx(color), bg))
    if not native:
        assert checked >= 3, (
            "%s 皮肤下只验到 %d 处文字（应至少 name/sub/status 三处）"
            % (preset, checked))
    assert worst >= 4.5, (
        "%s 皮肤下 TaskRow 文字对比度只有 %.2f（<4.5）" % (preset, worst))


@pytest.mark.parametrize("dark", [False, True])
def test_palette_text_tiers_all_readable(dark):
    """项11（调色板层）：深浅两套里 text/text_dim/text_faint 三档
    对各自底色的 WCAG 对比度全部 ≥ 4.5。

    这一条是「所有列表文字看不清」的通用锁：TaskRow 的三处文字正好
    一一对应这三档（name=text / sub=text_dim / status=text_faint），
    而 #6a7080 那个旧值就是 4.37 漏过去的。判据是**关系**（≥4.5），
    不写死色值 —— 将来再调色只要别跌破线就行。
    """
    pal = theme.palette(dark)
    bg = _hx(pal["bg"])
    for key in ("text", "text_dim", "text_faint"):
        cr = _contrast(_hx(pal[key]), bg)
        assert cr >= 4.5, (
            "%s 模式下 %s(%s) 对底 %s 的对比度只有 %.2f"
            % ("暗色" if dark else "浅色", key, pal[key], pal["bg"], cr))


# ═══════════════════════════════════════════════════════════════════
# 项14：隔离区管理对话框四个回调刷自己的表格
# ═══════════════════════════════════════════════════════════════════
def test_quarantine_callbacks_refresh_dialog_table(win, monkeypatch):
    """项14（行为）：隔离区页里点「清空隔离区」，**表格自己**必须变空。

    旧代码四个回调只调 self.refresh()（主窗口）⇒ 对话框表格纹丝不动，
    用户看到「日志说清空了，列表还有一堆行」。这里真点一次按钮，
    断言表格行数归零、库里也没了。

    ⚠ R80 变异测试（M8）抓到本判据一度**空转**：对话框本来就是打开时
    空表（初始不填，预存行为），不先点「刷新」的话「打回缺陷原型」
    （四个回调只留 self.refresh()）后 rowCount 依然是 0 —— 断言恒真。
    所以必须先真点一次「刷新」把表填满、断言有行，再清空、断言归零。
    """
    from PySide6.QtWidgets import QTableWidget

    cnt_before = win.db.query(
        "SELECT COUNT(*) c FROM images WHERE in_quarantine=1")[0]["c"]
    assert cnt_before >= 1, "夹具没灌进隔离区图（判据会空转）"

    # 模态一律打桩：offscreen 下没人点确定/关闭
    monkeypatch.setattr(QDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k:
                                     QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: 1))

    win.quarantine_manager()

    # R181：隔离区不再是弹窗 —— 直接取 stack 里的内嵌页
    page = getattr(win, "_quar_tab", None)
    assert page is not None, "隔离区内嵌页（_quar_tab）没建出来"
    btn = next((b for b in page.findChildren(QPushButton)
                if b.text() == "清空隔离区"), None)
    assert btn is not None, "隔离区页里没有「清空隔离区」按钮"
    tbl = next((t for t in page.findChildren(QTableWidget)), None)
    assert tbl is not None, "隔离区页里没有表格"

    # 先真点一次「刷新」把表格填满（M8 教训：不预填则归零断言空转）
    b_ref = next((b for b in page.findChildren(QPushButton)
                  if b.text() == "刷新"), None)
    assert b_ref is not None, "隔离区页里没有「刷新」按钮"
    b_ref.click()
    n_before = tbl.rowCount()
    assert n_before >= 1, (
        "点刷新后表格仍是空的（判据会空转：库里有 %d 条却没填进表）"
        % cnt_before)

    btn.click()

    assert tbl.rowCount() == 0, (
        "清空后对话框表格还有 %d 行（列表未刷新）" % tbl.rowCount())
    cnt_after = win.db.query(
        "SELECT COUNT(*) c FROM images WHERE in_quarantine=1")[0]["c"]
    assert cnt_after == 0, "库里的隔离区图没删干净：%d" % cnt_after


def test_quarantine_callbacks_call_reload_rows():
    """项14（AST）：四个回调都必须调对话框局部 reload_rows()。

    只判行为不够——行为那条只点了「清空隔离区」一个按钮；
    这里把「清理缺失/还原/彻底删除/清空」四条路一起钉住，
    并且反向钉住「不许退化成只调 self.refresh()」。
    """
    src = SRC.read_text(encoding="utf-8")
    i = src.index("def quarantine_manager")
    j = src.index("def open_purged_snapshot")
    seg = src[i:j]
    for fn in ("clean_missing", "restore", "purge", "purge_all"):
        k = seg.index("def %s():" % fn)
        nxt = seg.find("\n        def ", k + 1)
        body = seg[k:nxt if nxt > 0 else len(seg)]
        assert "reload_rows()" in body, (
            "%s 回调没有调 reload_rows()（对话框表格不会刷新）" % fn)
        assert "self.refresh()" in body, (
            "%s 回调没有同步主窗口（图库计数/统计不会变）" % fn)
    # R143：reload_rows 增加 more=False 形参以支持「加载更多」分批翻页，
    # 定义签名从 `def reload_rows():` 变为 `def reload_rows(more=False):`；
    # 守卫意图是「对话框局部 reload_rows 闭包存在且被各回调调用」，
    # 故按定义起始 `def reload_rows(` 匹配（兼容带/不带 more 形参两种写法）。
    assert re.search(r"def reload_rows\(", seg), "对话框局部 reload_rows 闭包不见了"
