"""R48 / THOROUGH_AUDIT_ORDER C：保存目录行重排（编辑框+浏览+沿用同一行）。

来源：作业单 C（用户第 4 条）—— 把「保存目录编辑框」+「浏览」按钮挪到与
「沿用上次目录」**同一行**、排在它后面；并**去掉独立的「上次目录」显示**，
上次目录直接显示在编辑框里。

⚠ 沿用本仓库铁律：**断言关系，不写死数值**。用「同一父控件 / 同一布局 /
顺序索引」表达「同一行」，不用 y 坐标 —— y 会随字体、DPI、面板高度变。

⚠⚠⚠ `win` 必须是 **module 粒度**（R48-h 实测踩出来的挂死）：
   本文件 11 个用例，`MainWindow` 是全项目最重的控件（构造约 9s），
   而 conftest 的 `_reap_top_level_widgets` 是 **module 粒度**回收
   —— 它只在**文件跑完**后回收，文件内部一个都不收。
   function 粒度 ⇒ 一个文件内堆出 11 个 MainWindow
   ⇒ **全量跑到本文件时挂死**（`-v` 实测确定停在第 10 个用例，
   日志不再增长、CPU 归零、junit 不生成；单跑本文件 11/11 全过，
   前面 `fill_all_history` 12 个窗口叠上来就必挂）。
   这与 `test_r48_watermark_left_consistency.py` 的注释写的是同一件事，
   那边已改 module 粒度，**本文件之前漏改了**。
   改成 module 粒度的配套：下面那个 `_reset_shared_state` autouse fixture
   （共享窗口必须逐用例复位，否则状态顺着用例顺序漏下去）。
"""
import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QPushButton  # noqa: E402


@pytest.fixture(scope="module")
def qapp():  # noqa: D103
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def win(qapp):  # noqa: D103
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    w._ensure_task_form()          # 表单面板懒构建
    for _ in range(3):
        qapp.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass
    qapp.setStyleSheet("")


@pytest.fixture(autouse=True)
def _reset_shared_state(win):
    """共享窗口**逐用例复位**（module 粒度的必要配套）。

    会改共享状态的用例：
      · `test_checking_reuse_fills_the_edit_box` 勾上勾选框并写 `cfg.last_output_dir`
      · `test_unchecking_reuse_clears_the_edit_box` 勾上再取消
    不复位的话，`test_reuse_checkbox_still_starts_unchecked`
    会因为**上一个用例留下的勾选状态**而假红 —— 而且症状极像真回归
    （「新建任务默认不勾」明明没改代码却红了），会引着人去回退正确的产品代码。
    """
    last_dir = getattr(win.cfg, "last_output_dir", "")
    try:
        yield
    finally:
        win._cb_reuse_last.setChecked(False)
        win.f_out.setText("")
        win.cfg.last_output_dir = last_dir


def _browse_btn(win):
    btns = [b for b in win._out_row.findChildren(QPushButton)
            if b.text().startswith("浏览")]
    assert btns, "这一行没有「浏览…」按钮"
    return btns[0]


# ============================================================ 同一行

def test_output_row_is_edit_plus_browse(win):
    """2026-10-27 按 R52 设计重写：这一行 = [编辑框][浏览]，仅此两个。

    R52 把「沿用上次目录」挪到了**独立一行**（避免挤占编辑框宽度——
    用户要求「保存到」编辑框与代理下拉框同宽）。旧断言（三控件同行）
    描述的是 R48/C 的设计，已过期。
    """
    row = win._out_row
    assert win.f_out.parentWidget() is row, "编辑框不在这一行里"
    assert _browse_btn(win).parentWidget() is row, "浏览按钮不在这一行里"
    assert win._cb_reuse_last.parentWidget() is not row, (
        "R52 起「沿用上次目录」应独立成行，不该挤在这一行里")


def test_order_is_edit_then_browse(win):
    """顺序 = 编辑框 → 浏览。"""
    row = win._out_row
    lay = row.layout()
    widgets = [lay.itemAt(i).widget() for i in range(lay.count())]
    widgets = [w for w in widgets if w is not None]
    ids = [id(w) for w in widgets]
    assert ids.index(id(win.f_out)) < ids.index(id(_browse_btn(win))), \
        "浏览按钮应排在编辑框之后"


def test_reuse_checkbox_still_exists_after_row(win):
    """「沿用上次目录」没被删，只是挪走了——它还得能勾。"""
    assert win._cb_reuse_last is not None
    assert win._cb_reuse_last.text().startswith("沿用上次目录")


def test_row_has_no_second_layer(win):
    """C：不再有「第二层」（R47 那版拆成上下两层，用户还是不顺手）。

    「同一行」最直接的形态就是这个容器里只有**一层**横向布局。
    """
    lay = win._out_row.layout()
    nested = [lay.itemAt(i).layout() for i in range(lay.count())
              if lay.itemAt(i).layout() is not None]
    assert not nested, f"这一行里还有嵌套子布局：{len(nested)} 层（应只有一层）"


# ============================================================ 去掉独立的上次目录显示

def test_separate_last_dir_label_is_hidden(win):
    """C：**去掉独立的「上次目录」显示**。

    ⚠ 判据用 `isHidden()`，不是 `isVisible()`：offscreen 下表单面板从不
    show，整条链 isVisible() 恒 False，用它断言会得到假象。
    """
    assert win._lbl_last_out.isHidden(), (
        "独立的「上次目录」标签还在显示 —— 作业单要求去掉它")


def test_open_new_task_does_not_unhide_the_label(win):
    """⚠ 开「新建任务」也不能把它显示回来（这是最容易漏的一处）。

    红→绿：`_open_task_form` 里原来有 `lbl.setVisible(bool(last))`，
    会把隐藏的标签重新点亮 —— 等于把需求退回去。

    ⚠⚠ **绝不真调 `_open_task_form`**：它末尾是 `dlg.exec()`（真模态框），
    在这里调会**永久阻塞整个测试进程**（实测真的挂住了，跑了 7 分钟只出 4 个点）。
    本仓库铁律：末行是 `menu.exec()` / `dlg.exec()` 的方法绝不能真调。
    所以改成**从磁盘读源码**做静态断言 —— 这也是仓库里既有的做法
    （改码只动 main_window.py，源码即事实源）。
    """
    import ast  # noqa: F811
    from pathlib import Path  # noqa: F811

    main_py = Path(__file__).resolve().parents[1] / "media_resource_manager" \
        / "gui" / "main_window.py"
    tree = ast.parse(main_py.read_text(encoding="utf-8"))

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_open_task_form"), None)
    assert fn is not None, "找不到 _open_task_form"

    # 该函数体内不得出现「把 _lbl_last_out 显示出来」的调用
    bad = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not isinstance(f, ast.Attribute) or f.attr != "setVisible":
            continue
        tgt = f.value
        if isinstance(tgt, ast.Attribute) and tgt.attr == "_lbl_last_out":
            arg = node.args[0] if node.args else None
            # setVisible(False) 是对的（保持隐藏）；setVisible(<非 False>) 是错的
            if not (isinstance(arg, ast.Constant) and arg.value is False):
                bad.append(ast.dump(node)[:120])
    assert not bad, (
        f"_open_task_form 里又把「上次目录」标签显示出来了：{bad}")


# ============================================================ 上次目录进编辑框

def test_checking_reuse_fills_the_edit_box(win, qapp):
    """C：勾上「沿用上次目录」→ 上次目录**直接出现在编辑框里**。"""
    want = "C:/Users/Admin/Desktop/Sweet/data"
    win.cfg.last_output_dir = want
    win._cb_reuse_last.setChecked(True)
    qapp.processEvents()
    assert win.f_out.text() == want, (
        f"勾了沿用但编辑框没填上次目录：{win.f_out.text()!r}")


def test_unchecking_reuse_clears_the_edit_box(win, qapp):
    """取消勾选 → 编辑框清空（用户能明确看到「没勾 = 不用旧目录」）。"""
    win.cfg.last_output_dir = "C:/some/last/dir"
    win._cb_reuse_last.setChecked(True)
    qapp.processEvents()
    win._cb_reuse_last.setChecked(False)
    qapp.processEvents()
    assert win.f_out.text() == "", "取消勾选后编辑框没清空"


def test_edit_box_shows_the_directory_not_a_label(win, qapp):
    """编辑框内容就是**目录路径本身**（不是「上次用的目录：xxx」这种文案）。

    作业单要求「上次目录直接显示在编辑框里」—— 加前缀就等于没做到。
    """
    want = "D:/data/gallery"
    win.cfg.last_output_dir = want
    win._cb_reuse_last.setChecked(True)
    qapp.processEvents()
    txt = win.f_out.text()
    assert txt == want
    assert "上次" not in txt and "目录：" not in txt


def test_browse_button_still_works_after_merge(win):
    """合并成一行后「浏览」按钮没被弄丢，且仍接着处理函数。

    ⚠ 两条约束：
    1. 不能真点 —— `pick_output_dir` 会弹**原生文件对话框**（B10 记录过：
       只有 dir_only 才恢复原生框），点下去测试进程会挂。
    2. 不能用 `receivers()` 数连接 —— PySide6 对 Python 槽返回 0（实测），
       会得到「没接处理函数」的假象。改用源码静态断言。
    """
    b = _browse_btn(win)
    assert b.isEnabled(), "合并后浏览按钮被禁用了"

    main_py = Path(__file__).resolve().parents[1] / "media_resource_manager" \
        / "gui" / "main_window.py"
    src = main_py.read_text(encoding="utf-8")
    tree = ast.parse(src)

    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_build_task_form_panel"), None)
    assert fn is not None, "找不到 _build_task_form_panel"
    connected = False
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"):
            # 形如 `b_out.clicked.connect(...)`：`b_out.clicked` 是 Attribute，
            # 它的 .value 才是那个 Name。第一版只查了 func.value.id == "b_out"，
            # 那是 `b_out.connect(...)` 的形状，匹配不到 → 假红。
            sig = node.func.value
            if isinstance(sig, ast.Attribute) and \
                    isinstance(sig.value, ast.Name) and sig.value.id == "b_out":
                connected = True
    assert connected, "浏览按钮（b_out.clicked）没接处理函数 —— 重排时弄丢了"


def test_reuse_checkbox_still_starts_unchecked(win):
    """新建任务默认**不勾**（沿用旧目录会误存到旧位置）。"""
    assert win._cb_reuse_last.isChecked() is False


def test_tooltip_mentions_the_edit_box(win):
    """提示语要说清「填进上面的编辑框」（行为变了，文案要跟上）。"""
    tip = win._cb_reuse_last.toolTip()
    assert "编辑框" in tip