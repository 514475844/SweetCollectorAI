"""R117 第3项（P1）：不喜欢弹窗白名单主题词 L1/L2/L3 分层 —— **整链路**护栏。

背景：R121 修的是数据层（`_task_ai_labels_by_tier` 三层合并）与渲染落地点
（`_make_column(groups=...)` 三段小标题）；R131 真 GUI 取证时想验「弹窗整链路」，
但探针的间谍用**字典**记录 `_make_column` 的每次调用 —— 右栏（排除词列，
groups=None by design）那次调用把左栏的 groups **覆盖**掉了，读出来是 None，
只得记 SKIP（3a/3b），落点函数 3a' 补位。

本文件把整链路钉死（判据钉**行为**不钉数值）：
  1. 左栏按 L1/L2/L3 三段渲染（小标题逐字一致、每段非空、六个词都在）；
  2. 右栏（排除词）**刻意**拍平（groups=None）——两栏语义不同，绝不混渲；
  3. ★ 勾选语义分流：左栏勾的词进 ``unmatched``（只做负向学习），
     右栏勾的 + 手填的进 ``exclusions``（才写 ai_blacklist）——
     左栏的词**绝不**泄漏进排除词（R117 订单原文的红线）；
  4. 老调用方（不传 labels_by_tier）行为不变：左栏拍平；
  5. 空词表不弹窗，直接 pure。

跑法：全部 offscreen、不起 MainWindow、不进 exec 事件循环（exec 被替身成
「模拟用户点按钮」），每个用例都有硬性断言，不会挂。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest

_TIERS = {1: ["一级A", "一级B"], 2: ["二级A", "二级B"], 3: ["三级A", "三级B"]}
_FLAT = _TIERS[1] + _TIERS[2] + _TIERS[3]
_GROUP_TITLES = ["一级主题词", "二级主题词（一级的细分）", "三级主题词"]


@pytest.fixture()
def qt_app(monkeypatch):
    """一个 offscreen 的 QApplication（复用进程内已有实例）。"""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def spy_calls(monkeypatch):
    """间谍 ``_make_column``：记录**每一次**调用（列表，不是字典！）。

    R131 的教训：用字典存会被后一次调用覆盖（右栏 groups=None 覆盖左栏），
    必须逐次追加。
    """
    import media_resource_manager.gui.dislike_dialog as dd
    calls = []

    def _spy(title, color, items, footer=None, groups=None):
        calls.append({"title": title, "items": list(items or []),
                      "groups": groups, "footer": footer})
        return dd.__dict__["_orig_make_column"](
            title, color, items, footer=footer, groups=groups)

    orig = dd._make_column
    dd.__dict__["_orig_make_column"] = orig
    monkeypatch.setattr(dd, "_make_column", _spy)
    yield calls
    dd.__dict__.pop("_orig_make_column", None)


def _run_dialog(monkeypatch, qt_app, spy_calls, *, labels_by_tier,
                blacklist=(), click=None, check_left=(), check_right=(),
                custom_text=None):
    """跑一次 ask_dislike_reason，exec 替身模拟用户操作后返回结果。"""
    from PySide6.QtWidgets import QDialog, QCheckBox, QLineEdit, QPushButton
    import media_resource_manager.gui.dislike_dialog as dd

    def _fake_exec(self):
        boxes = self.findChildren(QCheckBox)
        for cb in boxes:
            if cb.text() in check_left or cb.text() in check_right:
                cb.setChecked(True)
        if custom_text:
            edits = self.findChildren(QLineEdit)
            assert edits, "对话框里应有一个手填 QLineEdit"
            edits[0].setText(custom_text)
        if click:
            hit = False
            for b in self.findChildren(QPushButton):
                if b.text() == click:
                    b.click()
                    hit = True
                    break
            assert hit, f"对话框里应有个按钮「{click}」"
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", _fake_exec)
    ret = dd.ask_dislike_reason(
        None, list(_FLAT), blacklist=list(blacklist), db=None,
        labels_by_tier=labels_by_tier)
    return ret


# ---------------------------------------------------------------- 整链路
def test_full_chain_left_column_renders_three_tiers(qt_app, spy_calls,
                                                    monkeypatch):
    """整链路：左栏三段小标题逐字一致、每段非空、六个词都在；右栏拍平。"""
    ret = _run_dialog(monkeypatch, qt_app, spy_calls,
                      labels_by_tier=_TIERS)
    # 两次 _make_column：左（主题词，三段）+ 右（排除词，拍平）
    assert len(spy_calls) == 2, f"应调两次 _make_column，实际 {len(spy_calls)}"
    left, right = spy_calls
    groups = left["groups"]
    assert groups is not None, "左栏必须带 groups（分层渲染）"
    assert [g[0] for g in groups] == _GROUP_TITLES, \
        f"三段小标题必须逐字一致，实际 {[g[0] for g in groups]}"
    assert all(len(g[1]) > 0 for g in groups), "每一段都必须有词"
    shown = [w for g in groups for w in g[1]]
    for w in _FLAT:
        assert w in shown, f"词「{w}」必须在左栏出现"
    # 右栏（排除词列）刻意拍平：两栏语义不同，绝不混渲
    assert right["groups"] is None, "右栏（排除词）必须拍平，不分组"
    # 无交互 ⇒ cancel，什么都不做
    assert ret == ([], [], "cancel"), f"无交互应返回 cancel，实际 {ret}"


def test_full_chain_check_semantics_never_leak(qt_app, spy_calls,
                                               monkeypatch):
    """★ 勾选语义分流：左栏勾的词只进 unmatched，右栏勾的+手填的才进
    exclusions；左栏的词绝不泄漏进排除词。"""
    ret = _run_dialog(
        monkeypatch, qt_app, spy_calls,
        labels_by_tier=_TIERS, blacklist=["黑词A"],
        click="加入排除并删除",
        check_left=("一级A", "二级B", "三级A"),
        check_right=("黑词A",),
        custom_text="手填词X")
    unmatched, exclusions, mode = ret
    assert mode == "exclude"
    # 整链路前提：左栏确实是按三段分组渲染的（防「拍平退化」与语义测试假绿）
    assert spy_calls[0]["groups"] is not None and \
        len(spy_calls[0]["groups"]) == 3, \
        "语义分流测试的前提是左栏三段分组渲染"
    # 左栏三个不同层级的词都进了 unmatched（顺序=勾选项顺序）
    assert unmatched == ["一级A", "二级B", "三级A"], \
        f"左栏勾选应全部进 unmatched，实际 {unmatched}"
    # 右栏勾的 + 手填的进 exclusions
    assert exclusions == ["黑词A", "手填词X"], \
        f"右栏勾选+手填应进 exclusions，实际 {exclusions}"
    # ★ 红线：左栏的词绝不出现在排除词里
    for w in unmatched:
        assert w not in exclusions, f"左栏词「{w}」泄漏进了排除词！"
    # 手填词单独暂存，供调用方走 learn_manual
    import media_resource_manager.gui.dislike_dialog as dd
    assert dd.consume_manual_words() == ["手填词X"]


def test_legacy_caller_left_column_stays_flat(qt_app, spy_calls,
                                              monkeypatch):
    """老调用方（不传 labels_by_tier）行为不变：左栏拍平、词全在。"""
    ret = _run_dialog(monkeypatch, qt_app, spy_calls,
                      labels_by_tier=None, blacklist=["黑词A"])
    left = spy_calls[0]
    assert left["groups"] is None, "老调用方左栏必须保持拍平（行为不回退）"
    assert sorted(left["items"]) == sorted(_FLAT), \
        f"拍平后六个词都应在，实际 {left['items']}"
    assert ret == ([], [], "cancel")


def test_pure_and_cancel_paths(qt_app, spy_calls, monkeypatch):
    """单纯不喜欢 / 取消 两条路径的返回值。"""
    ret = _run_dialog(monkeypatch, qt_app, spy_calls,
                      labels_by_tier=_TIERS, click="单纯不喜欢")
    assert ret == ([], [], "pure"), f"单纯不喜欢应 pure，实际 {ret}"
    ret2 = _run_dialog(monkeypatch, qt_app, spy_calls,
                       labels_by_tier=_TIERS, click="取消")
    assert ret2 == ([], [], "cancel"), f"取消应 cancel，实际 {ret2}"


def test_empty_words_no_dialog(qt_app, spy_calls, monkeypatch):
    """词表全空时不弹窗，直接 pure（间谍一次都不该被调）。"""
    from PySide6.QtWidgets import QDialog
    import media_resource_manager.gui.dislike_dialog as dd
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)
    ret = dd.ask_dislike_reason(None, [], blacklist=[], db=None,
                                labels_by_tier=_TIERS)
    assert ret == ([], [], "pure")
    assert spy_calls == [], "空词表不该构造对话框"
