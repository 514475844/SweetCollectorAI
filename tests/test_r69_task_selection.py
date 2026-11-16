"""R69 · 「全量重建把任务选中态清掉」护栏（PART2 §2.1 生产测试产出）。

═══ 缺陷现场（真跑复现，见 tools/r69_probe_repro_selection_bug.py）═══
`_rebuild_task_list()` 开头 `self.task_list.clear()` ⇒ QListWidget 的
`currentRow` 归 -1 ⇒ **选中态没了**。走到这条路的条件很常见：
`plan_incremental` 判「改动比例 > 50%」/ 顺序变了 / 首屏
（首屏时 `_task_row_sigs` 为空 ⇒ 每行都算「变了」⇒ 必然全量重建）。
另有同型一处：`TaskTableView.set_tasks` 里 `set_rows` 走
`beginResetModel/endResetModel` ⇒ Qt 把 selectionModel 的当前索引一并作废。

═══ 用户可见后果 ═══
选中一个正在跑的任务 → 状态一变（`_tick` 每秒兜底刷新）→ 整表重建
→ 选中态没了 → 此刻点卡片上的「停止」，`selected_id()` 返回 None
⇒ **停不掉那个任务**。`selected_id` 的 docstring 自己就警告过
「停错了任务」这类问题，这一条是它的另一面：**什么都停不了**。
作业单 §2.1 也明确要求「切完**不重建窗口**，任务选中、翻页状态必须还在」。

═══ 本文件押着的契约 ═══
① 卡片：`_rebuild_task_list` 重建后，选中态必须还在（真起窗口验）。
② 表格：`set_tasks` 换数据后，选中态必须还在。
③ 任务已从列表里消失时，选中态**理应**丢（不许「复活」一个不存在的 id）。
④ AST 层：`_rebuild_task_list` 里确实调了 `_select_by_id`，
   `TaskTableView.set_tasks` 里确实调了 `select_id`
   —— 钉结构，不钉字面量（MEMORY 铁律：断言「关系」不「数值」）。
⑤ 反向护栏：`_select_by_id` 两套都设的既有行为不许被顺手删掉。

⚠⚠ **offscreen 下 `isVisible()` 不可信**（R58 铁律）—— 判显隐用 `isHidden()`。
⚠⚠ **本文件不许 emit 会开模态对话框的信号**（`_open_task_form` 里有
   `dlg.exec()`）—— offscreen 下会**永久挂死**且没有 traceback。
   要验接线就把处理器换成记录器（见 r69_probe_part21.py 的做法）。
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from PySide6.QtWidgets import QApplication

from media_resource_manager.gui import main_window as mwmod
from media_resource_manager.gui import task_table as ttmod
from media_resource_manager.models import TaskConfig, TaskStatus
from media_resource_manager.repo import list_tasks, save_task

MAIN_SRC = pathlib.Path(mwmod.__file__).resolve()
TBL_SRC = pathlib.Path(ttmod.__file__).resolve()

NTASKS = 6          # >1 就行；太小会让 plan_incremental 的比例判断走偏


@pytest.fixture(scope="module")
def win():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    w = mwmod.MainWindow()
    w.set_skin("violet_look")          # cards 模式
    try:
        yield w
    finally:
        try:
            w.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def tasks(win):
    """本模块共用一批任务（模块级夹具，只造一次）。"""
    made = []
    for i in range(NTASKS):
        tid = save_task(win.db, TaskConfig(
            id=None,
            name=f"r69护栏任务{i:02d}",
            adapter_name="generic",
            entry_urls=[f"https://example.invalid/r69/{i:02d}"],
            keywords=["r69"],
            status=TaskStatus.IDLE,
        ))
        made.append(tid)
    win._rebuild_task_list(list_tasks(win.db))
    QApplication.processEvents()
    return made


def _cur_tid(w):
    """当前选中的任务 id（没有则 None）。"""
    try:
        it = w.task_list.currentItem()
        if it is None:
            return None
        return w._id_of_item(it)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════
# ① 卡片：全量重建后选中态必须还在
# ═══════════════════════════════════════════════════════════════════
def test_cards_selection_survives_full_rebuild(win, tasks):
    """`_rebuild_task_list` 之后仍选中同一条任务（本缺陷的核心断言）。

    ⚠ 必须真起窗口真建行：`currentRow` 是运行时状态，读源码证明不了。
    """
    win.task_list.setCurrentRow(2)
    QApplication.processEvents()
    want = _cur_tid(win)
    assert want is not None, "前置：得先选中一行"

    win._rebuild_task_list(list_tasks(win.db))
    QApplication.processEvents()

    assert _cur_tid(win) == want, (
        f"全量重建后选中态丢了：期望 {want}，实得 {_cur_tid(win)}"
        f"（currentRow={win.task_list.currentRow()}）")


def test_cards_selection_survives_status_change_refresh(win, tasks):
    """更贴近真机的路径：任务状态一变 → refresh → 选中态仍在。

    这条比上面那条更重要：`_tick` 每秒兜底刷新一次，只要任务状态有变化
    就会走到全量重建 ⇒ 用户「什么都没干，选中就没了」。
    """
    win.task_list.setCurrentRow(1)
    QApplication.processEvents()
    want = _cur_tid(win)
    assert want is not None, "前置：得先选中一行"

    # 只改状态，不碰任何 UI 代码（用户点「运行」在库里就是这个效果）
    win.db.execute("UPDATE tasks SET status = ? WHERE id = ?",
                   ("running", want))
    # 让 _task_row_sigs 与现状脱节 —— 模拟「首屏/大面积变动」那条全量路径
    win._task_row_sigs = {}
    win.refresh()
    QApplication.processEvents()

    assert _cur_tid(win) == want, (
        f"状态变化 + refresh 后选中态丢了：期望 {want}，实得 {_cur_tid(win)}")


def test_cards_selection_survives_skin_switch(win, tasks):
    """作业单 §2.1 原话：「切完不重建窗口，任务选中状态必须还在」。"""
    win.task_list.setCurrentRow(1)
    QApplication.processEvents()
    want = _cur_tid(win)
    assert want is not None, "前置：得先选中一行"

    for key in ("classic", "violet_look", "modern", "night", "qbittorrent",
                "violet_look"):
        win.set_skin(key)
        QApplication.processEvents()
        assert _cur_tid(win) == want, (
            f"切到 {key} 后选中态丢了：期望 {want}，实得 {_cur_tid(win)}")


def test_selected_id_survives_rebuild(win, tasks):
    """`selected_id()` 也要跟着活 —— 用户的「停止」按钮靠的就是它。

    ⚠ 这条比 UI 断言更贴近后果：`selected_id()` 返回 None 时，
    卡片上的「停止」按钮点了不会有任何反应（停不掉那个任务）。
    """
    win.task_list.setCurrentRow(3)
    QApplication.processEvents()
    want = win.selected_id()
    assert want is not None, "前置：得先选中一行"

    win._rebuild_task_list(list_tasks(win.db))
    QApplication.processEvents()

    assert win.selected_id() == want, (
        f"重建后 selected_id() 变了：期望 {want}，实得 {win.selected_id()}")


# ═══════════════════════════════════════════════════════════════════
# ③ 任务已消失时，选中态理应丢（不许复活不存在的 id）
# ═══════════════════════════════════════════════════════════════════
def test_selection_dropped_when_task_gone(win, tasks):
    """被选中的任务真被删了 ⇒ 选中态必须跟着没（不是缺陷，是正确行为）。

    ⚠ 这条是**反向护栏**：防止「修复」变成「永远选着某个幽灵 id」。
    """
    win.task_list.setCurrentRow(1)
    QApplication.processEvents()
    want = _cur_tid(win)
    assert want is not None, "前置：得先选中一行"

    win.db.execute("DELETE FROM tasks WHERE id = ?", (want,))
    win._rebuild_task_list(list_tasks(win.db))
    QApplication.processEvents()

    assert _cur_tid(win) != want, "任务已删除，选中态却还指着它（幽灵选中）"

    # 复原：把这条任务加回去，免得影响同模块后面的用例
    save_task(win.db, TaskConfig(
        id=None, name="r69护栏复原", adapter_name="generic",
        entry_urls=["https://example.invalid/r69/again"], keywords=["r69"],
        status=TaskStatus.IDLE))
    win._rebuild_task_list(list_tasks(win.db))
    QApplication.processEvents()


# ═══════════════════════════════════════════════════════════════════
# ② 表格：set_tasks 换数据后选中态必须还在
# ═══════════════════════════════════════════════════════════════════
def test_table_selection_survives_set_tasks(win, tasks):
    """`beginResetModel/endResetModel` 会作废当前索引 —— 必须复原。"""
    win.set_skin("classic")              # table 模式
    QApplication.processEvents()

    ts = list_tasks(win.db)
    win.task_table.set_tasks(ts, None)
    QApplication.processEvents()
    win.task_table.select_id(ts[2].id)
    QApplication.processEvents()
    want = win.task_table.selected_id()
    assert want == ts[2].id, "前置：表格得先选中一行"

    # 换一批**内容真的不同**的数据 —— `set_rows` 只在指纹变了才发信号，
    # 内容一样会直接 return False，那条复原路径根本不会被走到（= 假绿）。
    ts2 = list(ts)
    for t in ts2:
        t.status = TaskStatus.RUNNING
    win.task_table.set_tasks(ts2, None)
    QApplication.processEvents()

    assert win.task_table.selected_id() == want, (
        f"表格换数据后选中态丢了：期望 {want}，"
        f"实得 {win.task_table.selected_id()}")
    win.set_skin("violet_look")
    QApplication.processEvents()


# ═══════════════════════════════════════════════════════════════════
# ④ AST 层：钉结构，不钉字面量
# ═══════════════════════════════════════════════════════════════════
def _fn_calls(path, func_name):
    """取某个函数体里所有 `self.xxx(...)` / `xxx(...)` 的被调名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    assert fn is not None, f"{path.name} 里找不到函数 {func_name}"
    return [c.func.attr if isinstance(c.func, ast.Attribute)
            else (c.func.id if isinstance(c.func, ast.Name) else None)
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and c.func is not None]


def _reachable_attr_calls(fn, want_attr):
    """找出**不在字面量假条件分支里**的 `xxx.<want_attr>()` 调用。

    ⚠ 为什么要判可达性：护栏第一版只判「函数体里出现过这个属性名」——
    而 `if False and tt is not None: tt.select_id(tid)` 里属性名照样在，
    护栏照样绿，可产品已经坏了。
    **变异测试（M3）当场抓到了这个空壳**，这条判据就是那次的产物。

    ⚠ 第二版又踩了一次（假红）：walker 只认 `If` / `Try` 两种语句，
      而 `try:` 里的语句是 `Expr`（包着那个 Call）—— 不认就永远返回空。
      ⇒ 正解：**通用递归 + 只在「恒假 If」处剪枝**，别枚举语句类型。
    """
    found: list[str] = []

    def _lit_false(test):
        """这个 If 的条件是不是字面量意义上的恒假？"""
        if isinstance(test, ast.Constant):
            return test.value is False or test.value == 0
        if isinstance(test, ast.BoolOp):
            vals = [v.value for v in test.values if isinstance(v, ast.Constant)]
            # `False and …` / `0 or False` 这类：全 False 或末位为 False
            if vals and all(v is False or v == 0 for v in vals):
                return True
        return False

    def visit(node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == want_attr:
                found.append(node.func.attr)
        if isinstance(node, ast.If) and _lit_false(node.test):
            return                      # 恒假分支：整支剪掉，不下去
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in fn.body:
        visit(stmt)
    return found


def _get_fn(path, func_name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == func_name), None)


def test_rebuild_task_list_restores_selection():
    """钉结构：重建里必须有**可达的**复原选中那一步。"""
    fn = _get_fn(MAIN_SRC, "_rebuild_task_list")
    assert fn is not None, "找不到 _rebuild_task_list"
    assert _reachable_attr_calls(fn, "_select_by_id"), (
        "_rebuild_task_list 里没有【可达的】复原选中的调用"
        "（藏在恒假分支里等于没接）")


def test_table_set_tasks_restores_selection():
    """钉结构：表格换数据后必须有**可达的**复原选中。"""
    fn = _get_fn(TBL_SRC, "set_tasks")
    assert fn is not None, "找不到 TaskTableView.set_tasks"
    assert _reachable_attr_calls(fn, "set_rows"), (
        "set_tasks 不再【可达地】调 set_rows？")
    assert _reachable_attr_calls(fn, "select_id"), (
        "TaskTableView.set_tasks 里没有【可达的】复原选中的调用")


def test_select_by_id_sets_both_views():
    """反向护栏：`_select_by_id` 必须**两套都设**（既有行为不许被顺手删）。

    只设一套的后果（该函数 docstring 已写）：切皮肤后「选中」看起来丢了，
    或者操作作用到错误的行。

    ⚠⚠ **这条护栏第一版是空的，变异测试当场抓住**（tools/r69_mutation_test.py M3）：
    原来只判「函数体里出现过 `select_id` 这个属性名」——
    而把代码改成 `if False and tt is not None: tt.select_id(tid)`
    **属性名照样在**，护栏照样绿，可产品已经坏了。
    ⇒ 正解：判**可达性** —— 那句 `select_id` 不能挂在字面量假条件底下。
    """
    fn = _get_fn(MAIN_SRC, "_select_by_id")
    assert fn is not None, "找不到 _select_by_id"
    assert _reachable_attr_calls(fn, "select_id"), (
        "_select_by_id 不再【可达地】设表格选中"
        "（卡片/表格两套必须都设；藏在恒假分支里等于没设）")
    assert _reachable_attr_calls(fn, "setCurrentRow"), (
        "_select_by_id 不再【可达地】设卡片选中"
        "（卡片/表格两套必须都设；藏在恒假分支里等于没设）")


# ═══════════════════════════════════════════════════════════════════
# ⑤ 本文件自身的卫生：不许在源码树里造数据目录
# ═══════════════════════════════════════════════════════════════════
def test_probe_does_not_write_into_source_tree():
    """回归探针不许在源码树里造假数据目录（R67 踩过：残留目录要改名处理）。

    钉「仓库根下没有本轮探针可能造出的临时目录」——
    只查有代表性的前缀，不扫全盘。
    """
    root = MAIN_SRC.parent.parent.parent
    for prefix in ("r69p21_", "r69sel_", "r69wc_", "r69bug_", "r69menu_"):
        leftovers = [p.name for p in root.glob(f"{prefix}*")]
        assert not leftovers, f"源码树里残留探针临时目录：{leftovers}"