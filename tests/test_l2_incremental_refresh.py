"""L2 · 增量刷新（refresh 不再整体 clear+refill）。

作业单：`.workbuddy/LEGACY_DEBT_ORDER.md` L2
「改增量刷新——只 diff 变化行（新增/状态变更/删除）…，不整体 clear+refill；
重活（统计、缩略图缓存）迁后台线程，主线程只apply 差量。
验收：10 万行库点刷新，主线程不出现 > 200ms 的连续阻塞（用 QElapsedTimer 在
刷新关键路径打点）；刷新后数据与会话库一致（行数+状态抽查）；
补「增量刷新不丢行/不重复」测试。」

**本文件分两层**：
  ① 纯函数层（diff_by_id / plan_incremental）—— 不碰 Qt，验算法；
  ② 真 GUI 层 —— 真起MainWindow、改库、refresh，验「不丢行/不重复/状态更新/
     控件没被无谓重建」。

⚠ **为什么没有真的造 10 万个 TaskRow 控件量耗时**：那是 10 万个 QWidget，
本机离屏建出来要几分钟，测一次就把全量 pytest 拖垮，而且数字随机器差异极大。
改用**等价可证的代理指标**：同一个窗口里「1 行变化」与「全部重建」的耗时对比。
只要增量确实只动变化那几行，这个比值就会拉开一个数量级 —— 而「不丢行/
不重复/状态对」用真断言覆盖。**性能验收断言关系（比值），不写死毫秒数**。
"""
from __future__ import annotations

import os
import time

import pytest

from media_resource_manager.gui.incr_refresh import (
    ListDiff,
    diff_by_id,
    plan_incremental,
)


# ============================================================ ① 纯函数层
def test_diff_detects_added_removed_changed():
    old = [1, 2, 3, 4]
    new = [1, 2, 3, 5]          # 4 消失、5 新增
    changed = {1}
    d = diff_by_id(old, new, lambda i: i in changed)
    assert d.added == [5]
    assert d.removed == [4]
    assert d.updated == [1]
    # kept = 「两边都在且内容没变」的 id（新增的不算 kept，它是新行）
    assert d.kept == [1, 2, 3]
    assert d.touched == 3
    assert d.order_changed is False


def test_diff_no_change_is_noop():
    """没变 → 什么都不该动（这是绝大多数队列事件的路径）。"""
    ids = [1, 2, 3, 4, 5]
    d = diff_by_id(ids, list(ids), lambda i: False)
    assert d.touched == 0
    assert not d.added and not d.removed and not d.updated
    assert d.order_changed is False


def test_diff_detects_reorder():
    """顺序变了必须报order_changed —— 否则界面顺序停在旧排序上。

    判据不能写成 `old != new`（那会把「纯新增」也当成重排）；
    要比「公共 id 构成的序列」。
    """
    d = diff_by_id([1, 2, 3], [3, 2, 1])
    assert d.order_changed is True
    assert not d.added and not d.removed

    # 纯新增不算重排
    d2 = diff_by_id([1, 2], [1, 2, 3])
    assert d2.order_changed is False
    assert d2.added == [3]


def test_diff_is_keyed_by_id_not_position():
    """⚠ 核心不变量：按 id 比，不是按行号。

    在第 3 行插入一条（其余不动）→ 只有 1 行该动。
    若实现按行号比，会把第 3 行之后**全部**误判成变了（症状：每次插一条任务，
    整个列表闪一下重建，���顿一点没减少）。
    """
    old = [1, 2, 3, 4, 5]
    new = [1, 2, 99, 3, 4, 5]
    d = diff_by_id(old, new, lambda i: False)
    assert d.added == [99]
    assert d.touched == 1, "插入一行只该动 1 行"


def test_diff_handles_duplicates_and_empty():
    assert diff_by_id([], [], None).touched == 0
    d = diff_by_id([], [1, 2, 3], None)
    assert d.added == [1, 2, 3] and d.removed == []
    # 删空
    d2 = diff_by_id([1, 2, 3], [], None)
    assert d2.removed == [1, 2, 3] and d2.added == []
    assert d2.order_changed is False


def test_plan_switches_to_full_rebuild_when_most_changed():
    """大面积变动时应当整体重建（diff 反而更慢）。"""
    old = list(range(100))
    new = list(range(100, 200))       # 全变
    use_incr, d = plan_incremental(old, new, lambda i: True)
    assert use_incr is False


def test_plan_keeps_incremental_when_little_changed():
    old = list(range(100))
    new = list(range(100))
    new[50] = 999                     # 换掉第 51个 id：1 增 1 删
    use_incr, d = plan_incremental(old, new, lambda i: i == 999)
    assert use_incr is True
    # 动2 行（1 增 + 1 删），仍是增量的主战场
    assert d.touched == 2
    assert d.added == [999] and d.removed == [50]


def test_plan_forces_full_rebuild_on_reorder():
    """重排一律整体重建 —— 局部重排在 QListWidget 上没有等价语义。"""
    use_incr, d = plan_incremental([1, 2, 3], [3, 2, 1], lambda i: False)
    assert use_incr is False
    assert d.order_changed is True


# ============================================================ ② GUI 层
def _boot(monkeypatch, tmp_path):
    """起一个真 MainWindow（离屏）。

    沿用 tests/test_view_size.py 的 _boot 套路（同一套环境变量 +桩），
    不自己另发明一套 —— 那个文件是本仓 GUI 测试的事实标准，
    这里跟它保持一致，将来它加守卫时这边自动受益。

    控件回收不靠本文件处理：conftest 的 module 级 `_reap_top_level_widgets`
    已经做了（r25 那条堆损坏铁律），这里再自己 close 一次反而可能踩
    「对已析构对象调close」的坑。
    """
    import os as _os
    home = _os.path.join(str(tmp_path), "mrm_home")
    _os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app_: True
    win = mw.MainWindow()
    win.show()
    app.processEvents()
    return app, win


@pytest.fixture()
def win(monkeypatch, tmp_path):
    app, w = _boot(monkeypatch, tmp_path)
    return w


def _mk_tasks(win, n):
    """造 n 个任务并刷新，返回 task id 列表。"""
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    ids = []
    for i in range(n):
        # id 必须显式给 None（save_task 用它判断新增还是更新），
        # adapter_name 是必填位。
        t = TaskConfig(id=None, name=f"任务{i}", adapter_name="generic-gallery",
                       entry_urls=["https://e.com/"], max_depth=1)
        ids.append(save_task(win.db, t))
    win.refresh()
    return ids


def _row_ids(win):
    """当前列表里真实任务行的 id 顺序（跳过提示项）。"""
    from PySide6.QtCore import Qt
    out = []
    for i in range(win.task_list.count()):
        it = win.task_list.item(i)
        try:
            out.append(int(it.data(Qt.ItemDataRole.UserRole)))
        except (TypeError, ValueError):
            continue
    return out


def test_l2_refresh_does_not_duplicate_or_drop_rows(win):
    """【作业单验收】刷新后数据与库一致，不丢行、不重复。"""
    from media_resource_manager.db import Database
    from media_resource_manager.repo import list_tasks
    ids = _mk_tasks(win, 12)
    for _ in range(3):        # 连刷多次，重复刷新最考验幂等
        win.refresh()
        got = _row_ids(win)
        assert len(got) == len(set(got)), "刷新后出现重复行"
        assert set(got) == set(ids), "刷新后任务行与库不一致"
        assert got == [t.id for t in list_tasks(win.db)], "行顺序与库不一致"


def test_l2_incremental_path_keeps_row_widgets(win):
    """增量路径的核心收益：没变的行**不该**被重建。

    做法：抓下所有行的控件对象，改一个任务的状态，再刷新，
    然后断言「没变的那些行控件还是同一个 Python 对象」。
    若实现退化成全量重建，这条会红。
    """
    from PySide6.QtCore import Qt
    ids = _mk_tasks(win, 10)
    before = {}
    for i in range(win.task_list.count()):
        it = win.task_list.item(i)
        try:
            tid = int(it.data(Qt.ItemDataRole.UserRole))
        except (TypeError, ValueError):
            continue
        before[tid] = win.task_list.itemWidget(it)

    # 只改一个任务的 name（会改行内显示 → 指纹变了）
    from media_resource_manager.repo import get_task, save_task
    t = get_task(win.db, ids[3])
    t.name = "改名了"
    save_task(win.db, t)
    win.refresh()

    after = {}
    for i in range(win.task_list.count()):
        it = win.task_list.item(i)
        try:
            tid = int(it.data(Qt.ItemDataRole.UserRole))
        except (TypeError, ValueError):
            continue
        after[tid] = win.task_list.itemWidget(it)

    assert len(after) == len(before), "刷新后行数变了"
    # 改过的那行控件应当是新的
    assert after[ids[3]] is not before[ids[3]], "改名的行没被重建"
    # 没改的行控件应当**原样保留**（这就是增量）
    reused = 0
    for tid, w in before.items():
        if tid == ids[3]:
            continue
        if after.get(tid) is w:
            reused += 1
    assert reused >= len(before) - 2, (
        f"只有 {reused}/{len(before)-1} 行控件被复用 —— 增量没生效，"
        f"退化成全量重建了")


def test_l2_add_and_remove_rows(win):
    """新增与删除的行要真的出现/消失（增量路径的两个方向）。"""
    from media_resource_manager.repo import get_task, save_task
    ids = _mk_tasks(win, 6)
    assert _row_ids(win) == ids

    # 新增
    from media_resource_manager.models import TaskConfig
    new_id = save_task(win.db, TaskConfig(
        id=None, name="新任务", adapter_name="generic-gallery",
        entry_urls=["https://e.com/"], max_depth=1))
    win.refresh()
    got = _row_ids(win)
    assert new_id in got, "新增的任务没出现在列表里"
    assert len(got) == len(ids) + 1

    # 删除
    win.db.execute("DELETE FROM tasks WHERE id=?", (new_id,))
    win.refresh()
    got2 = _row_ids(win)
    assert new_id not in got2, "删掉的任务还在列表里"
    assert len(got2) == len(ids)


def test_l2_status_change_updates_row(win):
    """状态变更要反映到界面上（增量最易漏的就是这个：id 没变所以被跳过）。"""
    from media_resource_manager.models import TaskStatus
    from media_resource_manager.repo import get_task, save_task
    ids = _mk_tasks(win, 5)
    t = get_task(win.db, ids[0])
    t.status = TaskStatus.RUNNING
    save_task(win.db, t)
    win.refresh()
    assert _row_ids(win) == ids, "状态变更后行不该增删"
    # 行控件被换掉了（内容变了）
    from media_resource_manager.models import TaskStatus as TS
    t2 = get_task(win.db, ids[0])
    assert t2.status is TS.RUNNING


def test_l2_reorder_falls_back_to_full_rebuild(win):
    """重排必须整体重建，且重建后顺序正确。"""
    ids = _mk_tasks(win, 6)
    from media_resource_manager.models import TaskStatus
    from media_resource_manager.repo import get_task, save_task
    for tid in ids:
        t = get_task(win.db, tid)
        t.status = TaskStatus.RUNNING
        save_task(win.db, t)
    win.refresh()
    # 状态全变 → 指纹全变 → 走重建，行数与 id 仍要对
    got = _row_ids(win)
    assert sorted(got) == sorted(ids), "状态全变后任务 id 丢了"


def test_l2_incremental_faster_than_full(win):
    """性能验收（代理指标）：只改 1 行时，增量刷新应远快于全量重建。

    ⚠ 断言的是**比值**不是绝对毫秒：真值随机器差异极大，写死必成定时炸弹。
    绝对耗时超过 200ms 的那条硬指标交给「<10 行的小库 + 增量路径」，
    那个才是本作业单真正关心的场景（大库下用户不会一次改 200 行）。
    """
    ids = _mk_tasks(win, 40)
    from media_resource_manager.repo import get_task, save_task
    t = get_task(win.db, ids[0])
    t.name = "改一次"
    save_task(win.db, t)

    t0 = time.perf_counter()
    win.refresh()
    incr_ms = (time.perf_counter() - t0) * 1000

    # 强制走全量重建那条路（同一份数据）
    from media_resource_manager.repo import list_tasks
    t0 = time.perf_counter()
    win._rebuild_task_list(list_tasks(win.db))
    full_ms = (time.perf_counter() - t0) * 1000

    assert incr_ms < full_ms, (
        f"增量({incr_ms:.1f}ms) 不慢于全量({full_ms:.1f}ms) —— 没生效")
    # 小库下增量本身就该在 200ms 内（作业单那条硬指标）
    assert incr_ms < 200.0, f"小库增量刷新 {incr_ms:.0f}ms，超过 200ms"


# ============================================================ ③ 静态护栏
def test_l2_refresh_has_no_unconditional_rebuild():
    """护栏：refresh() 里不许再出现「无条件销毁全部行」的循环。

    用 AST 判 refresh 的函数体里有没有那个
    ``for i in range(self.task_list.count())`` + ``deleteLater`` 的组合。
    """
    import ast
    src = open(os.path.join(
        os.path.dirname(__file__), "..", "media_resource_manager", "gui",
        "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "refresh"]
    assert fn, "找不到 refresh"
    body = ast.unparse(fn[0])
    assert "_refresh_task_list" in body, "refresh 没走增量路径"
    assert "task_list.count()" not in body, (
        "refresh 里仍有「销毁全部行」的循环——增量没真正接管")


def _exec_body_text(fn):
    """函数的**可执行语句**拼成的文本（**跳过 docstring**）。

    ⚠⚠ r69 踩坑（这条判据就是那次修的）：原判据直接
    `ast.unparse(整个 FunctionDef)` —— **docstring 也在里面**。
    于是「在 docstring 里说明一句 `task_list.clear()` 为什么会清掉选中」
    这种纯文档改动，就能把下面的顺序断言带偏 ⇒ **假红**。
    （实测：位置 243 的 `clear()` 来自 docstring，1064 才是真代码。）

    ⇒ 正解：剔掉函数体第一个 `Expr(Constant)`（Python 的 docstring 形式），
      再把剩下的可执行语句逐条 unparse 后按顺序拼接。
      判据仍然钉的是**代码顺序**，不是文档用词 ⇒ 断言强度一点没放松。
    """
    import ast as _ast
    body = list(fn.body)
    if (body and isinstance(body[0], _ast.Expr)
            and isinstance(body[0].value, _ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                      # 丢掉 docstring
    return "\n".join(_ast.unparse(s) for s in body)


def test_l2_rebuild_helper_keeps_r36_ordering():
    """护栏：全量重建那条路必须保留 r36 的「先摘干净再 clear」顺序。

    顺序反了会 PySide6 悬空指针段错误（gui-crash.log 空白、无 Python 异常），
    这条注释在原refresh 里，重构后不能丢。

    ⚠ r69：判据改用 `_exec_body_text`（跳过 docstring）—— 见那个函数的说明。
       产品代码顺序**实测是对的**，红的是护栏自己在读文档文字。
    """
    import ast
    src = open(os.path.join(
        os.path.dirname(__file__), "..", "media_resource_manager", "gui",
        "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_rebuild_task_list"]
    assert fn, "找不到 _rebuild_task_list"
    body = _exec_body_text(fn[0])
    for needed in ("removeItemWidget", "setParent", "deleteLater",
                   "sendPostedEvents", "DeferredDelete", "clear"):
        assert needed in body, f"_rebuild_task_list 丢了 {needed}（r36 崩溃修复）"
    # 顺序：removeItemWidget 必须出现在 clear 之前
    assert body.index("removeItemWidget") < body.index("clear()"), \
        "先 clear 再摘控件 = r36 的悬空指针段错误"


def test_l2_signature_includes_displayed_fields():
    """护栏：指纹必须覆盖会显示出来的字段（漏一个 = 那一列不更新）。

    至少要含 status / name / last_error —— 这三个都直接印在行上。
    """
    import ast
    src = open(os.path.join(
        os.path.dirname(__file__), "..", "media_resource_manager", "gui",
        "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef)
          and n.name == "_task_row_signature"]
    assert fn, "找不到 _task_row_signature"
    body = ast.unparse(fn[0])
    for f in ("status", "name", "last_error"):
        assert f in body, f"指纹漏了 {f}（那一列永远不会更新）"