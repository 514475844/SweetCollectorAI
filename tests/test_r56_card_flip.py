"""r56 #56 翻卡片：审核页灯箱按键改造 + 幻灯片自动播放。

覆盖三点（见作业单 ⑨ D21 / D22 / a，R153 修订 D22）：
- D21：图库不翻卡片 —— 图库灯箱（on_decide 为空）四向仍只翻页，行为不变；
- D22（R153 起）：审核页灯箱（on_decide 非空）**只剩 → 喜欢**，
       ← 恢复为「上一张」翻页，「不喜欢」从灯箱撤掉（网格/列表页保留）；
       ↑↓ 翻页（保留上下张）；
- a  ：幻灯片自动播放 —— 灯箱内置 QTimer，空格 / 按钮开停，默认 3 秒翻一张。

全部 offscreen，不碰真机图库；行首用假路径（render 对缺失文件有容错）。
"""
import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QListWidgetItem


@pytest.fixture(autouse=True)
def qapp(monkeypatch):
    """建任何 Qt 控件前都必须有 QApplication，否则 Qt 直接 fatal。"""
    from PySide6.QtWidgets import QDialog, QMessageBox
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    app = QApplication.instance() or QApplication([])
    # 这些弹窗在自动化里不能真弹（exec 会挂住整个测试）
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.No)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    return app


def _fake_rows(n, start_id=1):
    return [{"path": f"/no/such/img_{i}.webp", "id": start_id + i,
             "width": 8, "height": 6, "score": 0.5,
             "source_url": "https://example.com/x", "tags": ""}
            for i in range(n)]


def _send(widget, key):
    ev = QKeyEvent(QEvent.Type.KeyPress, int(key), Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, ev)


# ----------------------------------------------------- D22（R153）：审核页灯箱只剩 → 喜欢
def test_review_lightbox_right_likes_left_pages(qapp):
    """审核页灯箱（R153）：→ 喜欢 + 翻下一张；← = 上一张（翻页，不判定）；↑↓ 只翻页。"""
    from media_resource_manager.gui.lightbox import Lightbox
    from media_resource_manager.models import ReviewDecision

    decisions = []
    rows = _fake_rows(3)
    lb = Lightbox(rows, 0, None,
                  on_decide=lambda d, r: decisions.append((d, r["id"])))
    try:
        assert len(lb.rows) == 3
        # → 喜欢（命中第 0 张），翻到第 1 张
        _send(lb, Qt.Key.Key_Right)
        assert lb.i == 1, f"→ 后应停在第 1 张，实际 {lb.i}"
        assert decisions == [(ReviewDecision.LIKE, 1)], decisions
        # ← 上一张（R153：不再判「不喜欢」），从第 1 张退到第 0 张
        _send(lb, Qt.Key.Key_Left)
        assert lb.i == 0, f"← 后应回到第 0 张，实际 {lb.i}"
        assert decisions == [(ReviewDecision.LIKE, 1)], (
            f"← 在审核页灯箱只翻页不判定，实际 {decisions}")
        # ↑ 只翻页（从第 0 张绕到第 2 张），不触发判定
        _send(lb, Qt.Key.Key_Up)
        assert lb.i == 2, f"↑ 后应在第 2 张（环绕），实际 {lb.i}"
        assert len(decisions) == 1, f"↑ 不应判定，实际 {decisions}"
        # ↓ 翻页（从第 2 张绕回第 0 张），同样不判定
        _send(lb, Qt.Key.Key_Down)
        assert lb.i == 0, f"↓ 后应在第 0 张（环绕），实际 {lb.i}"
        assert len(decisions) == 1, f"↓ 不应判定，实际 {decisions}"
    finally:
        lb.close()


# ----------------------------------------------------- D21：图库灯箱四向仍翻页
def test_gallery_lightbox_keys_still_navigate(qapp):
    """图库灯箱（无审核上下文）：← → ↑↓ 都只翻页，绝不做判定（D21 图库不翻卡片）。"""
    from media_resource_manager.gui.lightbox import Lightbox

    lb = Lightbox(_fake_rows(3), 0, None)  # on_decide=None
    try:
        assert len(lb.rows) == 3
        # → 到下一张
        _send(lb, Qt.Key.Key_Right)
        assert lb.i == 1
        # ↓ 也到下一张（保留四向翻页）
        _send(lb, Qt.Key.Key_Down)
        assert lb.i == 2
        # ← 上一张
        _send(lb, Qt.Key.Key_Left)
        assert lb.i == 1
        # ↑ 上一张
        _send(lb, Qt.Key.Key_Up)
        assert lb.i == 0
    finally:
        lb.close()


# ----------------------------------------------------- a：幻灯片自动播放
def test_slideshow_toggle_interval_and_tick(qapp):
    """灯箱幻灯片：空格/按钮开停；间隔可设；空行不开播。

    ⚠ R206/F23 翻转「到底绕回」旧契约：循环**默认关 = 播到最后一张
    自动停**（与 Web 端 lbStep「到底就停住」同口径）；绕回只在用户
    显式打开「循环」开关后发生（该半边由 tests/test_r154_f23_slideshow.py
    test_f23_loop_wraps 钉死）。旧断言 `lb._slideshow_tick(); lb.i == 0`
    是 F23 之前的语义，勿改回。
    """
    from media_resource_manager.gui.lightbox import Lightbox

    lb = Lightbox(_fake_rows(3), 0, None)
    try:
        # 默认未开播
        assert lb.is_slideshow_running() is False
        # 开播
        assert lb.toggle_slideshow() is True
        assert lb.is_slideshow_running() is True
        assert lb.b_slideshow.isChecked() is True
        # 设间隔
        lb.set_slideshow_interval(1500)
        assert lb._slideshow_timer.interval() == 1500
        # 定时翻页：0→1→2，循环默认关 → 第三下停在最后一张（F23 新契约）
        lb._slideshow_tick()
        assert lb.i == 1
        lb._slideshow_tick(); assert lb.i == 2
        lb._slideshow_tick()
        assert lb.i == 2, "循环默认关时到底应停，不该绕回第一张"
        assert lb.is_slideshow_running() is False, "到底后应自动停"
        # 到底自动停之后：无参 toggle =「再开播」（暂停/继续语义，F23 口径）；
        # 显式 force=False = 停，且对已停的定时器幂等。
        assert lb.toggle_slideshow(False) is False
        assert lb.is_slideshow_running() is False
        assert lb.b_slideshow.isChecked() is False
    finally:
        lb.close()


def test_slideshow_does_not_start_with_no_rows(qapp):
    """没有可显示的图时，幻灯片不应开播（避免空转定时器）。"""
    from media_resource_manager.gui.lightbox import Lightbox

    lb = Lightbox([], 0, None)
    try:
        assert lb.toggle_slideshow() is False
        assert lb.is_slideshow_running() is False
    finally:
        lb.close()


# ----------------------------------------------------- D22：审核页键盘流（网格）
def test_review_page_tantan_keys_route_to_decide(qapp, tmp_config, tmp_db):
    """审核页（图库不翻卡片）：→ 喜欢 / ← 不喜欢 走 decide；
       ↑↓ 只移动选择、不判定（与灯箱探探左右键一致）。"""
    from unittest.mock import MagicMock
    from media_resource_manager.gui.review_tab import ReviewTab
    from media_resource_manager.models import ReviewDecision

    tab = ReviewTab(tmp_db, tmp_config.isolate_dir, lambda s: None, cfg=tmp_config)
    try:
        # 造两张假条目到审核网格（不依赖真机图库 / DB 记录）
        for rid in (101, 102):
            it = QListWidgetItem(f"#{rid}")
            it.setData(Qt.ItemDataRole.UserRole, rid)
            tab.grid.addItem(it)
        tab.grid.setCurrentItem(tab.grid.item(0))

        spy = MagicMock()
        tab.decide = spy  # 拦截真实判定（避免落库），只验证键盘路由

        # → 喜欢（keyboard=True）
        _send(tab.grid, Qt.Key.Key_Right)
        spy.assert_called_once_with(ReviewDecision.LIKE, keyboard=True)
        spy.reset_mock()
        # ← 不喜欢（keyboard=True）
        _send(tab.grid, Qt.Key.Key_Left)
        spy.assert_called_once_with(ReviewDecision.DISLIKE, keyboard=True)
        spy.reset_mock()
        # ↓ 移动选择到第 1 行，不该触发判定
        _send(tab.grid, Qt.Key.Key_Down)
        assert tab.grid.currentRow() == 1, tab.grid.currentRow()
        spy.assert_not_called()
        # ↑ 移动选择回第 0 行，也不该触发判定
        _send(tab.grid, Qt.Key.Key_Up)
        assert tab.grid.currentRow() == 0, tab.grid.currentRow()
        spy.assert_not_called()
    finally:
        tab.deleteLater()


# ------------------------------------------- 翻页按钮文案必须与真实键位一致
def test_nav_button_labels_match_context_keys(qapp):
    """审核页灯箱（R153 起）只剩 → 是「喜欢」，← 与 ↑↓ 一样只翻页。

    所以翻页按钮**不能**再印「下一张 (→)」—— 照着按钮提示按 → 会得到一次
    「喜欢」而不是翻页（文案与行为相反）；「上一张」印 ← 与真实键位一致。
    """
    from media_resource_manager.gui.lightbox import Lightbox
    from media_resource_manager.models import ReviewDecision

    rows = _fake_rows(3)

    # 审核页上下文（on_decide 非空）→ 文案必须指向 ← / ↓（→ 是喜欢）
    lb = Lightbox(rows, 0, None, lambda d, r, **kw: None)
    try:
        assert lb.b_prev.text() == "上一张 (←)", lb.b_prev.text()
        assert lb.b_next.text() == "下一张 (↓)", lb.b_next.text()
        # 按钮行为不变：仍然只翻页，不触发判定
        called = []
        lb.on_decide = lambda d, r, **kw: called.append(d)
        lb.b_next.click()
        assert lb.i == 1, lb.i
        assert called == [], "点「下一张」按钮不该触发喜欢/不喜欢判定"
        lb.b_prev.click()
        assert lb.i == 0, lb.i
        assert called == [], "点「上一张」按钮不该触发喜欢/不喜欢判定"
    finally:
        lb.deleteLater()

    # 纯浏览上下文（on_decide 为空）→ 四向都翻页，文案用 ←/→
    lb2 = Lightbox(rows, 0, None, None)
    try:
        assert lb2.b_prev.text() == "上一张 (←)", lb2.b_prev.text()
        assert lb2.b_next.text() == "下一张 (→)", lb2.b_next.text()
    finally:
        lb2.deleteLater()


def test_review_lightbox_left_is_page_not_dislike(qapp):
    """R153 反向钉死：审核页灯箱 ← 必须是「上一张」翻页，绝不再判「不喜欢」。

    用户原话：「把灯箱里面的『不喜欢』这个功能直接去掉吧，在网格页或者
    列表页有这个功能就够了」—— 灯箱 ← 恢复翻页，防「照着提示按出误判」。
    """
    from media_resource_manager.gui.lightbox import Lightbox
    from media_resource_manager.models import ReviewDecision

    rows = _fake_rows(4)
    seen = []
    lb = Lightbox(rows, 1, None, lambda d, r, **kw: seen.append(d))
    try:
        # ← 从第 1 张翻回第 0 张，不判定
        _send(lb, Qt.Key.Key_Left)
        assert lb.i == 0, lb.i
        assert seen == [], f"← 在审核页灯箱只翻页，实际 {seen}"
        # → 仍是唯一判定键：喜欢 + 翻下一张
        _send(lb, Qt.Key.Key_Right)
        assert seen == [ReviewDecision.LIKE], seen
        assert lb.i == 1, lb.i
        # 提示条与源码里都不该再向灯箱宣传「不喜欢」
        src_labels = [b.text() for b in (lb.b_prev, lb.b_next)]
        assert not any("不喜欢" in t for t in src_labels), src_labels
    finally:
        lb.deleteLater()
