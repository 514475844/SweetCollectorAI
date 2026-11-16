"""R84 · 第 49 项性能：「任务列表全量重建 = 两遍式插入」的常驻护栏。

═══ 背景（实测，tools/r84_ab_twopass.py + r84_measure_after.py，offscreen）═══
R83 压测测出「cards 模式全量重建」是唯一超线性的路径：
    150 行 7.77ms/行 → 300 行 8.83 → 600 行 11.30   ← 每行的成本随 N 涨
微基准（tools/r84_micro_row.py）把成本拆开，排除了「行控件本身贵」：
    addItem 单独 0.02ms/行 ｜ setItemWidget 单独 0.17ms/行 ｜
    TaskRow 构造 2.17ms/行  ⇒ 三项相加 2.36ms/行，而交错写法要 8.8ms/行
⇒ 多出来的部分全部来自「每插一行就重排一次已在列表里的每一行」。
改成两遍式（先铺满 item，再逐行挂控件）后重排次数降到线性：
    150 行 6.71ms/行（1.006s）｜300 行 6.63（1.989s）｜600 行 7.18（4.310s）
    总耗时 -13.7% / -25% / **-36.5%**；外推 2000 行 ~14.4s（原 42.8s，-66%）。

═══ 本文件押的契约 ═══
⚠⚠ **只押结构不变量 + 行为等价，不押计时**：
计时护栏在这个尺度上**分不出**两遍式与交错式（两者都在 7~9ms/行，
抖动比差距大）⇒ 计时判据是空壳（R73 同型：判据现在是绿的不算数，
必须变异一次确认它真会红）。结构不变量则与机器快慢无关：只要
`_rebuild_task_list` 退回「一个循环里 addItem 紧接着 setItemWidget」，
代价就是平方级。⇒ 本文件的判据全部变异验证过（tools/r84_mutation_test.py）。

⚠ R69/R73 教训：ast.unparse 会把 **docstring 一起**读出来，而注释里
  大量引用两种写法 ⇒ 先剥掉 docstring 再拼可执行语句。
⚠ R67：env 是进程级全局 ⇒ 模块级夹具的还原在 finally 里**当场断言**。
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from r83_stress_common import seed_images, seed_tasks  # noqa: E402

N_TASKS = 30


# ---------------------------------------------------------------- AST 层
def _rebuild_body_src():
    """取 `_rebuild_task_list` 去掉 docstring 后的可执行语句源码。"""
    src = (ROOT / "media_resource_manager" / "gui" / "main_window.py") \
        .read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_rebuild_task_list"), None)
    assert fn is not None, "找不到 _rebuild_task_list"
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def _for_loops_with(fn_name: str, *names: str):
    """返回函数体里「出现过某些名字」的 For 循环节点（按出现顺序）。"""
    src = (ROOT / "media_resource_manager" / "gui" / "main_window.py") \
        .read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    assert fn is not None, f"找不到 {fn_name}"
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    out = []
    for node in body:
        if not isinstance(node, ast.For):
            continue
        seg = ast.unparse(node)
        if all(nm in seg for nm in names):
            out.append((node, seg))
    return out


def test_rebuild_has_additem_loop_without_setitemwidget():
    """第一遍：只 addItem，不挂控件。"""
    loops = _for_loops_with("_rebuild_task_list", "addItem")
    assert loops, "找不到铺 item 的循环 —— 两遍式被退回成交错式了"
    assert any("setItemWidget" not in seg for _n, seg in loops), \
        "每个含 addItem 的循环里都跟着 setItemWidget ⇒ 又变成「插一行重排一次」"


def test_rebuild_has_later_setitemwidget_loop():
    """第二遍：单独一轮挂控件，且**必须排在第一遍之后**。"""
    src = (ROOT / "media_resource_manager" / "gui" / "main_window.py") \
        .read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_rebuild_task_list"), None)
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    add_i = sw_i = -1
    for k, node in enumerate(body):
        if not isinstance(node, ast.For):
            continue
        seg = ast.unparse(node)
        if "addItem" in seg and add_i < 0:
            add_i = k
        if "setItemWidget" in seg and sw_i < 0:
            sw_i = k
    assert add_i >= 0 and sw_i >= 0, f"两遍缺一遍 (add={add_i}, set={sw_i})"
    assert add_i < sw_i, \
        f"挂控件的循环（第 {sw_i} 句）排在铺 item（第 {add_i} 句）之前 —— 顺序反了"


def test_item_sizehint_still_78px():
    """两遍式**不许**顺手改行高：78px 是行控件的布局契约。"""
    code = _rebuild_body_src()
    assert "QSize(0, 78)" in code, \
        "行高不再是 78px —— 行内三按钮/两行文字会挤爆或留大片空白"


# ---------------------------------------------------------------- 反向护栏
def test_insert_task_row_still_exists_and_single_row():
    """增量路径（_refresh_task_list）仍走 `_insert_task_row` 的单行插入。

    两遍式只对「一次铺 N 行」有意义，套到增量路径（一次插一行）反而多绕一圈。
    这条钉住：不许把增量路径也改成两遍。
    """
    src = (ROOT / "media_resource_manager" / "gui" / "main_window.py") \
        .read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_insert_task_row"), None)
    assert fn is not None, "_insert_task_row 被删了 —— 增量新增行没有入口"
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))
    assert "addItem" in code and "setItemWidget" in code, \
        "_insert_task_row 不再自己 addItem+setItemWidget —— 增量新增行会变空行"


def test_rebuild_still_builds_real_taskrow():
    """挂上去的必须是真的 TaskRow，不是省事的裸 QWidget。

    ⚠ 这是「性能优化把功能优化没了」的典型形态：换成裸控件能再快 3 倍，
    但运行/停止/编辑按钮和状态点全没了（R74 已确认行内三按钮是 table
    皮肤之外**唯一**的启停入口）。
    """
    code = _rebuild_body_src()
    assert "TaskRow(self, t)" in code, \
        "重建不再构造 TaskRow —— 行内按钮/状态会整排消失"


# ---------------------------------------------------------------- 行为层
@pytest.fixture(scope="module")
def gui_win(tmp_path_factory):
    """模块级真窗口：30 任务 + 300 图。"""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    tmp = tmp_path_factory.mktemp("r84gui")
    old = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    os.environ["MRM_HOME"] = str(tmp)
    os.environ["MRM_NO_MIGRATE"] = "1"
    try:
        from media_resource_manager.db import Database
        from media_resource_manager.gui import main_window as mwmod
        db = Database(tmp / "mrm.db")
        seed_tasks(db, N_TASKS)
        seed_images(db, 300, N_TASKS)
        w = mwmod.MainWindow()
        w.set_skin("violet_look")
        w.resize(1400, 900)
        w.show()
        QApplication.processEvents()
        try:
            yield w
        finally:
            try:
                w.close()
            except Exception:
                pass
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        leaked = [k for k in ("MRM_HOME", "MRM_NO_MIGRATE")
                  if os.environ.get(k) != old.get(k)]
        assert not leaked, f"模块级夹具泄漏环境变量: {leaked}"


def test_rebuild_keeps_row_count_and_id_order(gui_win):
    """两遍式与交错式的**结果必须逐项一致**：行数、行控件数、id 顺序。

    ⚠ 这条是「性能优化没把功能优化没了」的行为侧兜底：只判结构不判结果，
    万一第二遍 zip 错位（items 与 tasks 长度/顺序不一致）就会静默串行。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui.main_window import TaskRow
    from media_resource_manager.repo import list_tasks
    tasks = list_tasks(gui_win.db)
    assert len(tasks) == N_TASKS
    gui_win._task_row_sigs = {}
    gui_win._rebuild_task_list(tasks)
    QApplication.processEvents()
    assert gui_win.task_list.count() == N_TASKS, "行数不对"
    rows = gui_win.task_list.findChildren(TaskRow)
    assert len(rows) == N_TASKS, \
        f"行控件 {len(rows)} != 行数 {N_TASKS} —— 有行没挂上控件（会显示为空白行）"
    ids = [gui_win.task_list.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(gui_win.task_list.count())]
    assert ids == [t.id for t in tasks], "id 顺序与 tasks 不一致 —— 第一遍铺错了"
    # ⚠⚠ 只判 item 的 id 是**不够的**：id 在第一遍就写进 item 了，
    # 第二遍把**别的任务的行控件**挂上去，item 的 id 照样是对的
    # ⇒ 那条断言对「两遍串行」是空壳（变异 M5/M7 当场抓出来：rc=0 全绿）。
    # 正解：逐行比对「行控件自己记的任务 id」与「它所在 item 的 id」。
    # 症状（真发生时）：点某行的「停止」停掉的是**另一个**任务 ——
    # selected_id() 返回的是 item 的 id，而按钮 lambda 抓的是 TaskRow 的 t.id，
    # 两者一分家就是「停错任务」，界面上完全看不出来。
    for i in range(gui_win.task_list.count()):
        it = gui_win.task_list.item(i)
        wid = gui_win.task_list.itemWidget(it)
        assert wid is not None, f"第 {i} 行没有行控件（会显示成空白行）"
        assert wid._tid == it.data(Qt.ItemDataRole.UserRole), \
            (f"第 {i} 行挂错了：行控件属于任务 {wid._tid}，"
             f"却挂在任务 {it.data(Qt.ItemDataRole.UserRole)} 的位置上 "
             f"—— 两遍串行，点「停止」会停错任务")


def test_rebuild_restores_selection(gui_win):
    """R69 不回归：两遍式重建后**选中态必须还在**。

    R69 修的就是「状态一变就整表重建 ⇒ 选中没了 ⇒ 点停止停不掉」。
    两遍式改的是填行顺序，`clear()` 归 -1currentRow 的事实没变
    ⇒ 复原逻辑必须仍然生效。
    """
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.repo import list_tasks
    tasks = list_tasks(gui_win.db)
    gui_win._task_row_sigs = {}
    gui_win._rebuild_task_list(tasks)
    QApplication.processEvents()
    target = tasks[3]
    gui_win._select_by_id(target.id)
    QApplication.processEvents()
    assert gui_win.selected_id() == target.id, "前置：选中没设上，测试本身失效"
    gui_win._task_row_sigs = {}
    gui_win._rebuild_task_list(tasks)
    QApplication.processEvents()
    assert gui_win.selected_id() == target.id, \
        "重建后选中态丢了 —— R69 回归（点「停止」会停不掉任何任务）"


def test_rebuild_empty_shows_hint(gui_win):
    """空列表仍走「还没有任务」提示那条路（两遍式不许把它绕过去）。"""
    from PySide6.QtWidgets import QApplication
    gui_win._rebuild_task_list([])
    QApplication.processEvents()
    assert gui_win.task_list.count() == 1
    it = gui_win.task_list.item(0)
    assert "暂无任务" in (it.text() or ""), "空列表没有提示项"


def test_two_loops_share_the_same_item_list():
    """两遍必须**共用同一个 item 集合**，不许各铺各的。

    ⚠ 这是我的第一版元测试写错后换上来的判据：原本写「断言 MRM_HOME 已还原」，
    而模块级夹具要等**整个模块跑完**才 teardown ⇒ 模块内的测试永远看得到
    设着的值 ⇒ 那条断言恒红，是我自己的仪器错（R83 文档里就写过这个坑）。
    换成下面这条结构不变量：第一遍收集到的 list 变量，第二遍必须遍历它。
    否则 zip(items, tasks) 一错位就是「行控件挂在别的任务上」——静默串行，
    界面上完全看不出来（每行都长得差不多），是本改动最危险的错误形态。
    """
    src = (ROOT / "media_resource_manager" / "gui" / "main_window.py") \
        .read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_rebuild_task_list"), None)
    assert fn is not None
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]
    # 函数体里形如 `X = []` 的集合名
    lists = []
    for node in body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List) \
                and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            lists.append(node.targets[0].id)
    assert lists, "两遍式没收集 item 集合 —— 第二遍无从挂控件"
    loops = [(ast.unparse(n)) for n in body if isinstance(n, ast.For)]
    add_loops = [s for s in loops if "addItem" in s]
    set_loops = [s for s in loops if "setItemWidget" in s]
    assert add_loops and set_loops
    shared = [nm for nm in lists
              if any(nm in s for s in add_loops)
              and any(nm in s for s in set_loops)]
    assert shared, \
        f"两遍没有共用同一个 item 集合（集合名 {lists}）—— 第二遍挂错行的风险"
