"""R51 生产级真跑·**桌面端**：把`tools/probe_r51_prod_desktop.py` 探针固化成回归测试。

⚠ 为什么这些断言值得留下（而不是「探针跑过一次就完了」）：
  探针的定位是**当场抓缺陷**，抓不到就说明这一轮没问题；
  但探针跑完就消失，下次改代码没人再跑。所以要把「仍然成立」钉成测试。

⚠ 三条踩过的坑，写在这里免得下次重犯：
  ① **模块级 `win` fixture**（R49 教训）：函数级 `win` 会造 46 个 MainWindow
     （每个约 9s），而 conftest 的 `_reap_top_level_widgets` 只在**模块**粒度回收，
     于是一路挂到52% 不动。模块级 + autouse 复位是唯一稳的写法。
  ② **不臆造 API 名**：类名是 `Lightbox`（小写 b），我第一版写成 `LightBox`
     直接 AttributeError。MEMORY 已记「6 个猜错 = 14 个假红」，这里所有名字
     都是 grep 核实过的。
  ③ **断言关系不.assert 数值**：视图切换断言「往返后状态复原」，
     不写死`list_mode == False` —— 那是快照式断言，产品一改就假红。

真起 MainWindow + 真点按钮，覆盖作业单桌面段点名的：
  图库网格/列表、审核网格/列表、两页开关互不串台、翻卡片契约、
  批量动作、曾用词全部填满、主题词永不进排除词。
"""
import ast
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication   # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def win(qapp):
    """真 MainWindow（模块粒度，签名与理由照抄 R50 的已验证写法）。"""
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    w._ensure_task_form()
    for _ in range(4):
        qapp.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass
    qapp.setStyleSheet("")


@pytest.fixture(autouse=True)
def _restore_view_modes(win, qapp):
    """每个用例前后把两页的视图模式复原 —— 用例之间不许互相留状态。"""
    def snap():
        return bool(win.g_list_mode), bool(win._review_tab._list_mode)
    before = snap()
    yield
    gal, rev = snap()
    if gal != before[0]:
        win.g_mode.setChecked(before[0])
        win.g_mode.clicked.emit()
    if rev != before[1]:
        win._review_tab.b_rmode.setChecked(before[1])
        win._review_tab.b_rmode.toggled.emit(before[1])
    qapp.processEvents()


# --------------------------------------------------------- 图库网格/列表

def test_gallery_mode_toggle_roundtrip(win, qapp):
    """图库「列表模式」按钮真点一遍，状态必须真的翻转再翻回来。"""
    start = bool(win.g_list_mode)
    win.g_mode.setChecked(not start)
    win.g_mode.clicked.emit()
    qapp.processEvents()
    assert bool(win.g_list_mode) is (not start), \
        f"点一次没切：{start} -> {win.g_list_mode}"
    win.g_mode.setChecked(start)
    win.g_mode.clicked.emit()
    qapp.processEvents()
    assert bool(win.g_list_mode) is start, "切回去失败"


def test_gallery_mode_switches_visibility(win, qapp):
    """切模式必须**真的换控件可见性**，不是只改了个布尔量。

    ⚠ 判据用 `isHidden()`（本控件自己是否被隐藏），不用 `isVisible()`
       —— 后者要求连祖先链全可见，在页签里恒为 False，判据会形同虚设。
    """
    start = bool(win.g_list_mode)
    win.g_mode.setChecked(not start)
    win.g_mode.clicked.emit()
    qapp.processEvents()
    assert win.g_grid.isHidden() is bool(win.g_list_mode), (
        f"列表模式={win.g_list_mode} 但 g_grid.isHidden()={win.g_grid.isHidden()}")
    assert win.g_split.isHidden() is (not bool(win.g_list_mode)), \
        f"g_split 可见性没跟着切：isHidden={win.g_split.isHidden()}"


# --------------------------------------------------------- 审核网格/列表

def test_review_mode_toggle_roundtrip(win, qapp):
    """审核页视图切换在**它自己的** b_rmode 上（不是图库那个 g_mode）。"""
    rv = win._review_tab
    assert hasattr(rv, "b_rmode"), \
        "审核页按钮真名是 b_rmode（不是 b_rmode 之外的名字，也不是 g_mode）"
    start = bool(rv._list_mode)
    rv.b_rmode.setChecked(not start)
    rv.b_rmode.toggled.emit(not start)
    qapp.processEvents()
    assert bool(rv._list_mode) is (not start), \
        f"审核视图没切：{start} -> {rv._list_mode}"
    rv.b_rmode.setChecked(start)
    rv.b_rmode.toggled.emit(start)
    qapp.processEvents()
    assert bool(rv._list_mode) is start, "审核视图切回去失败"


def test_review_mode_button_text_follows_state(win, qapp):
    """按钮文案必须跟着状态变（用户靠它知道现在在哪一档）。"""
    rv = win._review_tab
    rv.b_rmode.setChecked(True)
    rv.b_rmode.toggled.emit(True)
    qapp.processEvents()
    t_on = rv.b_rmode.text()
    rv.b_rmode.setChecked(False)
    rv.b_rmode.toggled.emit(False)
    qapp.processEvents()
    t_off = rv.b_rmode.text()
    assert t_on != t_off, f"切档了文案却没变：{t_on!r}"


# ------------------------------------------------- 两页开关不许互相串台

def test_gallery_switch_does_not_touch_review(win, qapp):
    """★ 只点图库的开关，审核页必须纹丝不动。

    两页都叫「列表模式」，最容易出的bug 就是有人把它们接到同一份状态上；
    在一页上点点会把另一页也切掉，而这种缺陷肉眼极难发现。
    """
    rv = win._review_tab
    rev_before = bool(rv._list_mode)
    gal_before = bool(win.g_list_mode)
    win.g_mode.setChecked(not gal_before)
    win.g_mode.clicked.emit()
    qapp.processEvents()
    assert bool(rv._list_mode) is rev_before, (
        f"只点了图库的开关，审核页却被切了：{rev_before} -> {rv._list_mode}")


def test_review_switch_does_not_touch_gallery(win, qapp):
    """反向：只点审核页的开关，图库不许跟着变。"""
    rv = win._review_tab
    gal_before = bool(win.g_list_mode)
    rev_before = bool(rv._list_mode)
    rv.b_rmode.setChecked(not rev_before)
    rv.b_rmode.toggled.emit(not rev_before)
    qapp.processEvents()
    assert bool(win.g_list_mode) is gal_before, (
        f"只点了审核的开关，图库却被切了：{gal_before} -> {win.g_list_mode}")


# ------------------------------------------------------------- 翻卡片契约

def _sync_labels(on_decide_present: bool):
    """在**不构造真对话框**的前提下取 _sync_nav_buttons 之后的真实文案。

    ⚠ 为什么用 `__new__` + 假按钮：Lightbox.__init__ 要 rows/parent/QDialog，
       真构造会走渲染与图标加载；而这里要验的只是「文案按on_decide 切换」
       这一个纯函数式契约。
    """
    from media_resource_manager.gui import lightbox as lbmod

    lb = lbmod.Lightbox.__new__(lbmod.Lightbox)      # 类名小写 b
    lb.rows = [1, 2, 3]
    lb.on_decide = (lambda *a, **k: None) if on_decide_present else None
    lb._hotkeys = {}                                 # F19：_sync_nav_buttons 调 _hk() 需要
    texts = {}

    def mk(name):
        def setText(t, _n=name):
            texts[_n] = t
        btn = type("B", (), {"setEnabled": lambda self, v: None})()
        # ⚠ setText 必须挂成**实例属性**：写进类体就成了绑定方法，
        #   self 会被当成第一个实参传进去，于是 dict 里存的是按钮对象而不是文案
        #   （第一版就这么翻车的，断言拿到 '<B object at 0x...>'）。
        btn.setText = setText
        return btn

    lb.b_prev = mk("b_prev")
    lb.b_next = mk("b_next")
    lb._sync_nav_buttons()
    return texts


def test_lightbox_nav_labels_follow_context():
    """审核页灯箱用 ↑↓，图库灯箱用 ←→（MEMORY 铁律第 4 条）。

    ⚠ R160 改判（不是放水，契约被 R153 换过）：R153 把审核态键位改成
      「→ = 喜欢，← / ↑ 上一张，↓ 下一张」后，_sync_nav_buttons 的
      **按钮文案**同步定为 decide 态「上一张 (←) / 下一张 (↓)」
      （R153-DONE 逐条对账第 1 条原文）—— ↑ 是**键位**，不是按钮文案。
      本条旧断言写在 R153 之前，按新契约改看按钮文案；「↑ 键在审核态
      仍翻页不判定」由 test_r153_r155_lightbox_quit.py 守着，不重复造。
    """
    decide = _sync_labels(True)
    plain = _sync_labels(False)
    assert "←" in decide.get("b_prev", "") and "↓" in decide.get("b_next", ""), \
        f"审核态按钮文案该是 上一张(←)/下一张(↓)（R153 口径）：{decide}"
    assert "←" in plain.get("b_prev", "") and "→" in plain.get("b_next", ""), \
        f"图库灯箱该用 ←→：{plain}"


def test_lightbox_nav_buttons_disabled_when_single_row():
    """只有 1 张时上下张按钮必须置灰（r37：不禁用的话用户按了以为坏了）。"""
    from media_resource_manager.gui import lightbox as lbmod

    lb = lbmod.Lightbox.__new__(lbmod.Lightbox)
    lb.rows = [1]
    lb.on_decide = None
    lb._hotkeys = {}                                 # F19：_sync_nav_buttons 调 _hk() 需要
    enabled = {}

    def mk(name):
        return type("B", (), {
            "setEnabled": lambda self, v, _n=name: enabled.__setitem__(_n, v),
            "setText": lambda self, t: None})()

    lb.b_prev = mk("b_prev")
    lb.b_next = mk("b_next")
    lb._sync_nav_buttons()
    assert enabled.get("b_prev") is False and enabled.get("b_next") is False, \
        f"只有 1 张时按钮该置灰，实际 enabled={enabled}"


def test_sync_nav_buttons_does_not_navigate():
    """★ _sync_nav_buttons 只同步文案/可用性，**不许**顺手翻页。

    ⚠ 这条是「文案与行为分离」的护栏：翻页行为恒为 step(±1)，
       一旦有人把 step 混进同步函数，点一次翻页就会跳两张。
       用 AST 查而不是全文搜（MEMORY 第 8 条：全文搜会命中自己写的注释）。
    """
    from media_resource_manager.gui import lightbox as lbmod

    tree = ast.parse(Path(lbmod.__file__).read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_sync_nav_buttons"), None)
    assert fn is not None, "找不到 _sync_nav_buttons"
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "step" not in called, \
        f"_sync_nav_buttons 不该翻页（只同步文案/可用性）：{called}"
    assert any(isinstance(n, ast.FunctionDef) and n.name == "step"
               for n in ast.walk(tree)), "灯箱没有 step() —— 上下张导航没了"


# --------------------------------------------------------------- 批量动作

@pytest.mark.parametrize("m", ["gallery_batch_like",
                                "gallery_batch_dislike",
                                "gallery_batch_delete"])
def test_gallery_batch_actions_are_real_methods(win, m):
    """批量喜欢/不喜欢/删除必须是真方法（作业单桌面段）。

    用 `callable` 而不是 `hasattr`：动态属性也能满足 hasattr。
    """
    assert callable(getattr(win, m, None)), f"缺批量动作 {m}"


# --------------------------------------------------- 曾用词「全部填满」

def test_fill_all_history_returns_int(win):
    n = win._fill_all_history()
    assert isinstance(n, int) and n >= 0, f"_fill_all_history 返回 {n!r}"


def test_fill_all_puts_theme_words_into_whitelist_only(win):
    """★铁律：主题词永远不进排除词，而一级白名单必须真的拿到它。

    ⚠ 判据是**关系**（排除词一个字都没多 / 白名单多了这个词），
      不是某个具体数字 —— 快照式断言会随需求变化集体假红。
    """
    import media_resource_manager.config as cfgmod

    orig = cfgmod.save_config
    marker = "R51专属主题词ZZZ"
    try:
        cfgmod.save_config = lambda *a, **k: None
        win.cfg.ai_label_history = list(win.cfg.ai_label_history or []) + [marker]
        excl_before = win.f_ai_excl.text()
        labels_before = win.f_ai_labels.text()

        win._fill_all_history()

        excl_after = win.f_ai_excl.text()
        labels_after = win.f_ai_labels.text()
        assert marker not in excl_after, "主题词漏进排除词了（违反铁律）"
        assert excl_before == excl_after, \
            f"排除词被改了：{excl_before!r} -> {excl_after!r}"
        # 反向也钉死：确实填进去了（否则上面两条可能因压根没跑而恒真）
        assert marker in labels_after, \
            f"主题词没进一级白名单（全部填满没生效）：{labels_before!r} -> {labels_after!r}"
        assert labels_after != labels_before, "白名单一个字都没多"
    finally:
        cfgmod.save_config = orig
        win.cfg.ai_label_history = [
            x for x in (win.cfg.ai_label_history or []) if x != marker]