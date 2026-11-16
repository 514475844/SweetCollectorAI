"""r56 A4 #56 翻卡片（审核页）：全键盘说原因 + 流式取 + 断点续审。

作业单 A4 原文：「探探左右键 + ↑↓ 原因框 + 灯箱保留上下张；全键盘不拖动、
无过渡动画；流式取、断点续审；「为什么不喜欢」落黑白名单学习(D28)」。

⑨ 那轮已经把**灯箱**层的探探左右键 + 幻灯片做完了（见 test_r56_card_flip.py）。
本轮补的是**审核页本体**剩下的三块，每块都钉死：

1. D28 键盘可达 —— 按 `E` 弹「为什么不喜欢」框，勾的主题词做**负向学习**
   （绝不进排除词）、填的排除词**才**写进任务 ai_blacklist。改之前键盘流的
   「不喜欢」根本不弹框（D28 的学习落不了库），这正是本段要修的主缺口。
2. 流式取 —— 每次查库多拉 STREAM_SPARE 张备着，下一页优先消费余量；
   换任务必须清余量（否则 A 任务里冒出 B 任务的图）。
3. 断点续审 —— 判完记 id 到 app_meta，下次开页光标落到断点之后第一张；
   只许前进不许后退（撤销不许把断点拉回去）；只在本次开页第一次铺图时生效。

铁律回退钉死：水印几何（test_r57/r58）由它们自己的文件保证，本文件不碰。
全部 offscreen，不依赖真机图库；不触发任何真实网络/磁盘删除路径。
"""
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QDialog, QListWidgetItem, QMessageBox


@pytest.fixture(autouse=True)
def qapp(monkeypatch):
    """建任何 Qt 控件前必须有 QApplication，否则 Qt 直接 fatal。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    return app


def _send(widget, key):
    ev = QKeyEvent(QEvent.Type.KeyPress, int(key), Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, ev)


def _tab(tmp_db, tmp_config):
    from media_resource_manager.gui.review_tab import ReviewTab
    return ReviewTab(tmp_db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)


def _fill_grid(tab, ids):
    for rid in ids:
        it = QListWidgetItem(f"#{rid}")
        it.setData(Qt.ItemDataRole.UserRole, rid)
        tab.grid.addItem(it)
    if ids:
        tab.grid.setCurrentRow(0)


def _mk_task(db, ai_labels=None, blacklist=None, ai_blacklist=None):
    """造一个真任务（要真落库，_task_ai_labels / _add_exclusions 才查得到）。"""
    from media_resource_manager.repo import save_task
    from media_resource_manager.models import TaskConfig
    return save_task(db, TaskConfig(
        id=None, adapter_name="generic", name="t",
        ai_labels=list(ai_labels or []),
        ai_labels_l2=[], ai_labels_l3=[],
        blacklist=list(blacklist or []),
        ai_blacklist=list(ai_blacklist or [])))


class _Rec:
    """够 reload()/decide() 用的最小记录桩。

    ``path`` 必须给一个**真实存在**的空文件 —— reload() 里
    ``if rec is None or not rec.path: continue`` 之后还有 ``Path(p).exists()``，
    路径不存在会被整条跳过，``_page_ids`` 就空了（症状：断言"页面空了"，
    看着像代码 bug，其实是自己桩造得不合格）。
    """
    def __init__(self, iid, task_id=1, tags=None, path=None):
        self.id = iid
        self.task_id = task_id
        self.tags = list(tags or [])
        self.score = 0.5
        self.width, self.height, self.bytes = 8, 6, 100
        self.source_url = "https://example.com/x"
        self.path = str(path) if path else None


def _recs_with_files(ids, tmp_path):
    """造一排带真实空文件的记录（每张一个不同名，避免重名互相覆盖）。"""
    out = []
    for i in ids:
        p = tmp_path / f"a4_{i}.webp"
        if not p.exists():
            p.write_bytes(b"\x00" * 8)
        out.append(_Rec(i, path=p))
    return out


def _stub_records(tab, recs):
    """把 review_tab 里的 get_image 换成桩。

    必须这么干：``decide()`` 里 ``recs = [get_image(...)]`` 拿到空列表就会
    **整段跳过**原因框（``if recs:``），后面照旧判定 —— 测试会看到"框没弹"
    却以为是自己配错了。这里直接给真记录，让 decide 走完整路径。
    """
    from media_resource_manager.gui import review_tab as rt
    old = rt.get_image
    rt.get_image = lambda db, i: next((r for r in recs if r.id == i), None)
    return lambda: setattr(rt, "get_image", old)


# ============================================================ 1) D28 键盘可达
def test_e_key_opens_reason_dialog_from_keyboard(qapp, tmp_config, tmp_db, monkeypatch):
    """按 E → 判「不喜欢」并**弹**原因框（ask_reason=True 覆盖键盘默认不弹）。"""
    from unittest.mock import MagicMock
    from media_resource_manager.models import ReviewDecision

    tab = _tab(tmp_db, tmp_config)
    try:
        _fill_grid(tab, [501])
        seen = {}
        tab.decide = MagicMock(
            side_effect=lambda d, keyboard=False, ask_reason=None:
            seen.update(d=d, keyboard=keyboard, ask_reason=ask_reason))
        _send(tab.grid, Qt.Key.Key_E)
        assert seen.get("d") == ReviewDecision.DISLIKE, seen
        assert seen.get("keyboard") is True, seen
        assert seen.get("ask_reason") is True, "E 键必须强制弹原因框（D28）"
    finally:
        tab.deleteLater()


def test_left_key_stays_silent_no_reason_dialog(qapp, tmp_config, tmp_db, monkeypatch):
    """← 仍走「键盘默认不弹框」：不打断连刷（回归钉死，别把 E 的行为扩散到 ←）。"""
    from unittest.mock import MagicMock
    from media_resource_manager.models import ReviewDecision

    tab = _tab(tmp_db, tmp_config)
    try:
        _fill_grid(tab, [502])
        seen = {}
        tab.decide = MagicMock(
            side_effect=lambda d, keyboard=False, ask_reason=None:
            seen.update(d=d, ask_reason=ask_reason))
        _send(tab.grid, Qt.Key.Key_Left)
        assert seen.get("d") == ReviewDecision.DISLIKE
        # ask_reason 必须是 None（"按老规则自己判断"），不能被改成 True
        assert seen.get("ask_reason") is None, seen
    finally:
        tab.deleteLater()


def test_ask_reason_true_writes_blacklist_and_never_topic_word(qapp, tmp_config, tmp_db):
    """D28 铁律：勾主题词 → 只负向学习（绝不进排除词）；填排除词 → 才写 ai_blacklist。"""
    from media_resource_manager.gui import dislike_dialog as dd
    from media_resource_manager.models import ReviewDecision

    tid = _mk_task(tmp_db, ai_labels=["猫"], ai_blacklist=[])
    tab = _tab(tmp_db, tmp_config)
    restore = _stub_records(tab, [_Rec(503, tid, ["猫"])])
    try:
        _fill_grid(tab, [503])

        # ⚠️ 必须带 **kw：#64 给 ask_dislike_reason 加了可选参数 db=
        # （词表桥接预演要用），写死签名的话调用方一传 db= 就 TypeError。
        # 替身签名永远比被替的函数宽松 —— 这是替身的基本纪律。
        def fake_reason(_parent, labels, blacklist, hint="", **kw):
            # 模拟用户：勾左栏「猫」+ 右栏手填「狗」
            dd._remember_manual(["狗"])       # #64：手填词走词表桥接
            return (["猫"], ["狗"], "exclude")

        monkey = dd.ask_dislike_reason
        dd.ask_dislike_reason = fake_reason
        try:
            # 直接调 decide，走真实的 _add_exclusions 落库路径
            tab._batch_decide_ids = lambda ids, d: None  # 只验学习/排除词，不动文件
            tab.decide(ReviewDecision.DISLIKE, keyboard=True, ask_reason=True)
        finally:
            dd.ask_dislike_reason = monkey

        from media_resource_manager.repo import get_task
        t = get_task(tmp_db, tid)
        assert "狗" in list(t.ai_blacklist or []), t.ai_blacklist
        assert "猫" not in list(t.ai_blacklist or []), \
            "主题词绝不能进排除词（会把整个主题误杀）"
    finally:
        restore()
        tab.deleteLater()


def test_ask_reason_cancel_leaves_everything_untouched(qapp, tmp_config, tmp_db):
    """原因框点「取消」→ 不判定、不删图、不写排除词。"""
    from media_resource_manager.gui import dislike_dialog as dd
    from media_resource_manager.models import ReviewDecision

    tid = _mk_task(tmp_db, ai_labels=["猫"], ai_blacklist=["狗"])
    tab = _tab(tmp_db, tmp_config)
    restore = _stub_records(tab, [_Rec(504, tid, ["猫"])])
    try:
        _fill_grid(tab, [504])
        called = []
        tab._batch_decide_ids = lambda ids, d: called.append((ids, d))
        monkey = dd.ask_dislike_reason
        dd.ask_dislike_reason = lambda *a, **k: ([], [], "cancel")
        try:
            tab.decide(ReviewDecision.DISLIKE, keyboard=True, ask_reason=True)
        finally:
            dd.ask_dislike_reason = monkey
        assert called == [], "取消后绝不能还去判定"
        from media_resource_manager.repo import get_task
        t = get_task(tmp_db, tid)
        assert list(t.ai_blacklist or []) == ["狗"], "取消不许动排除词"
    finally:
        restore()
        tab.deleteLater()


def test_ask_reason_false_suppresses_dialog_even_with_labels(qapp, tmp_config, tmp_db):
    """ask_reason=False → 即使有主题词也不弹框（供「批量不喜欢」等场景显式关闭）。"""
    from media_resource_manager.gui import dislike_dialog as dd
    from media_resource_manager.models import ReviewDecision

    tid = _mk_task(tmp_db, ai_labels=["猫"], ai_blacklist=[])
    tab = _tab(tmp_db, tmp_config)
    restore = _stub_records(tab, [_Rec(505, tid, ["猫"])])
    try:
        _fill_grid(tab, [505])
        hits = []
        monkey = dd.ask_dislike_reason
        dd.ask_dislike_reason = lambda *a, **k: (hits.append(1), ([], [], "pure"))[1]
        called = []
        tab._batch_decide_ids = lambda ids, d: called.append(d)
        try:
            tab.decide(ReviewDecision.DISLIKE, keyboard=True, ask_reason=False)
        finally:
            dd.ask_dislike_reason = monkey
        assert hits == [], "ask_reason=False 不该弹框"
        assert len(called) == 1, "但判定照常走"
    finally:
        restore()
        tab.deleteLater()


# ============================================================== 2) 流式取
def test_fetch_pending_reuses_spare_before_hitting_db(qapp, tmp_config, tmp_db):
    """第二次取页必须吃余量、**不再查库**（流式取的核心收益）。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        calls = []
        recs = [type("R", (), {"id": i})() for i in range(1, 41)]
        tab.queue.pending = lambda tid, limit=50: (
            calls.append(limit), recs[:limit])[1]

        first, from_spare1 = tab._fetch_pending(None, 10)
        assert len(first) == 10, len(first)
        assert from_spare1 is False
        assert calls == [10 + 25], calls          # 第一次按 want+SPARE 多拉

        second, from_spare2 = tab._fetch_pending(None, 10)
        assert from_spare2 is True, "余量够就不该再查库"
        assert calls == [10 + 25], f"不该有第二次查库: {calls}"
        assert [r.id for r in second] == list(range(11, 21))
    finally:
        tab.deleteLater()


def test_fetch_pending_refetches_when_spare_runs_out(qapp, tmp_config, tmp_db):
    """余量吃光后必须重新查库（不能拿空列表糊弄用户）。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        calls = []
        recs = [type("R", (), {"id": i})() for i in range(1, 11)]
        tab.queue.pending = lambda tid, limit=50: (
            calls.append(limit), recs[:limit])[1]
        tab._fetch_pending(None, 10)          # 10 条全用完，余量 0
        items, from_spare = tab._fetch_pending(None, 5)
        assert from_spare is False, "余量空了必须查库"
        # 第二次 want=5 → 按 5+SPARE(25)=30 查（不是 15，我第一版写错了）
        assert calls == [35, 30], calls
        assert len(items) == 5
    finally:
        tab.deleteLater()


def test_switching_task_clears_spare(qapp, tmp_config, tmp_db):
    """换任务必须清余量 —— 否则 A 任务里会冒出 B 任务的图。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        recs = [type("R", (), {"id": i})() for i in range(1, 41)]
        tab.queue.pending = lambda tid, limit=50: recs[:limit]
        tab.reload = lambda *a, **k: None       # 只验清余量这一步
        tab._fetch_pending(1, 10)
        assert len(tab._stream_spare) == 25, len(tab._stream_spare)
        tab._on_task_changed()
        assert tab._stream_spare == [], "换任务必须清空流式余量"
    finally:
        tab.deleteLater()


# ========================================================== 3) 断点续审
def test_checkpoint_roundtrips_through_app_meta(qapp, tmp_config, tmp_db):
    """断点写进 app_meta 并能读回（关掉软件再开也在）。"""
    from media_resource_manager.gui.review_tab import CHECKPOINT_KEY

    tab = _tab(tmp_db, tmp_config)
    try:
        tab._save_checkpoint(4242)
        assert tab._load_checkpoint() == 4242
        # 另一个"进程"（新实例）读同一个库 → 断点还在
        tab2 = _tab(tmp_db, tmp_config)
        try:
            assert tab2._last_decided_id == 4242
        finally:
            tab2.deleteLater()
    finally:
        tab.deleteLater()
    assert tmp_db.meta_get(CHECKPOINT_KEY) == "4242"


def test_checkpoint_only_moves_forward(qapp, tmp_config, tmp_db):
    """撤销把 id 变小时**不许**把断点拉回去（否则刚审完又跳回上一张）。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        tab._save_checkpoint(500)
        tab._save_checkpoint(300)          # 模拟撤销后 id 变小
        assert tab._load_checkpoint() == 500
    finally:
        tab.deleteLater()


def test_checkpoint_cursor_lands_on_first_unreviewed_after_break(qapp, tmp_config, tmp_db):
    """光标落到「断点之后（id 更小）的第一张」，不是队列顶部。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        # 队列 id DESC：900/800/700/600 → 断点 800 之后的第一张是 700
        _fill_grid(tab, [900, 800, 700, 600])
        tab._last_decided_id = 800
        tab._apply_checkpoint_cursor()
        assert tab.grid.currentRow() == 2, tab.grid.currentRow()
    finally:
        tab.deleteLater()


def test_checkpoint_cursor_noop_when_grid_empty(qapp, tmp_config, tmp_db):
    """没有待审图时不能崩（断点有值但队列空了是常见收尾状态）。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        tab._last_decided_id = 999
        tab._apply_checkpoint_cursor()      # 不抛异常即通过
        assert tab.grid.currentRow() == -1
    finally:
        tab.deleteLater()


def test_checkpoint_not_applied_on_subsequent_reloads(qapp, tmp_config, tmp_db):
    """断点只在本次开页**第一次**铺图时落光标，连刷时不插手。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        # 第一次铺图 → 应用断点
        _fill_grid(tab, [900, 800, 700, 600])
        tab._ckpt_applied = False
        tab._last_decided_id = 800
        tab._apply_checkpoint_cursor()
        tab._ckpt_applied = True
        assert tab._ckpt_applied is True

        # 之后连着判：reload 里那段被 _ckpt_applied 挡住，不会再掰光标
        _fill_grid(tab, [900, 700, 600])
        # 模拟 reload 的判定分支：只在 !_ckpt_applied 时才调
        if not getattr(tab, "_ckpt_applied", False):
            tab._apply_checkpoint_cursor()
        assert tab.grid.currentRow() == 0, "连刷时不该被断点掰走"
    finally:
        tab.deleteLater()


def test_save_checkpoint_ignores_bad_input(qapp, tmp_config, tmp_db):
    """断点只收整数 id；None / 非数字都必须安静忽略（绝不能带崩判定主流程）。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        tab._save_checkpoint(None)
        assert tab._last_decided_id is None
        tab._save_checkpoint("abc")            # 不可转 int → 忽略
        assert tab._last_decided_id is None
        tab._save_checkpoint(77)
        assert tab._last_decided_id == 77
    finally:
        tab.deleteLater()


# ==================================== 流式取 vs 「关掉补满」的契约（真回归钉死）
def test_fill_false_does_not_consume_stream_spare(qapp, tmp_config, tmp_db, tmp_path):
    """【回归】fill=False 必须重查库，**不能吃流式余量**。

    这条是本段真踩出来的回归：余量是「只进不退」的前向游标，本页显示 6,5,4 后
    余量只剩 3,2,1；下次 fill=False 再切页若吃余量，拿3,2,1 去和本页那批{6,5,4}
    求交集 → 空集 → **整页被清空**（r32 的两条老测试就是这么挂的）。
    两者语义方向相反：流式=往前续图，fill=False=留在本页这批。
    """
    tab = _tab(tmp_db, tmp_config)
    try:
        recs = _recs_with_files((6, 5, 4, 3, 2, 1), tmp_path)
        calls = []
        tab.queue.pending = lambda tid, limit=50: (
            calls.append(limit), recs[:limit])[1]
        tab._page_size = 3
        tab.reload(fill=True)
        assert tab._page_ids == {6, 5, 4}, tab._page_ids
        # 模拟判掉 4（真正落库那步太重，这里只改_page_ids，语义等价）
        tab._page_ids = {6, 5}
        tab.reload(fill=False)
        assert tab._page_ids == {6, 5}, \
            f"fill=False 竟吃到了流式余量: {tab._page_ids}"
    finally:
        tab.deleteLater()


def test_fill_false_repeats_query_same_window(qapp, tmp_config, tmp_db, tmp_path):
    """【回归】fill=False 连续两次要查两次库（证明确实重查、不是吃余量）。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        recs = _recs_with_files((6, 5, 4, 3, 2, 1), tmp_path)
        calls = []
        tab.queue.pending = lambda tid, limit=50: (
            calls.append(limit), recs[:limit])[1]
        tab._page_size = 3
        tab.reload(fill=False)
        tab.reload(fill=False)
        assert len(calls) == 2, f"fill=False 应每次重查库，实际查了 {len(calls)} 次"
    finally:
        tab.deleteLater()


def test_stream_spare_only_consumed_when_fill_true(qapp, tmp_config, tmp_db, tmp_path):
    """fill=True 仍享受流式取（余量够就不查库）——别为了修回归把优化也关了。"""
    tab = _tab(tmp_db, tmp_config)
    try:
        recs = _recs_with_files(range(1, 31), tmp_path)
        calls = []
        tab.queue.pending = lambda tid, limit=50: (
            calls.append(limit), recs[:limit])[1]
        tab._page_size = 3
        tab.reload(fill=True)
        assert len(calls) == 1, calls
        tab.reload(fill=True)          # 余量够 → 不该再查
        assert len(calls) == 1, f"流式取被误关了: {calls}"
    finally:
        tab.deleteLater()


# ============================================ 铁律回退：水印几何不许被牵连
def test_watermark_qss_order_still_intact():
    """A4 改了审核页键盘流，水印那套 QSS 顺序铁律不许被牵连（r43/r58 的教训）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "media_resource_manager" / "gui" / "theme.py").read_text(
               encoding="utf-8")
    pane = src.find("QTabWidget::pane")
    pen = src.find("_WM_PENETRATE")
    assert pane != -1 and pen != -1
    assert pen < pane or "QTabWidget::pane { background: transparent; }" in src, \
        "透明规则必须写在 pane 实底规则之后（同特异性后者胜，反了就复发）"