"""#60 / A2-2 + A2-3：权重明细单条改权重 + 转移原文显示相似度。

A2-2（改权重）：面板以前只有「删除选中」，用户学歪了只能整条删（连seen/hit_pos/
hit_neg 一起没了）。这里要求「改成多少就是多少」，且**命中次数是历史事实要保住**。

A2-3（转移原文）：用户要看到「金发 → blond hair（相似度 0.71）」。
其中「没有 CLIP 模型」那档必须显式区分成None，不能拿 0.0 冒充——
0.0 会被读成「算过了，完全不像」。
"""
import pytest


# ==================== A2-2 改权重 ====================
def _mkdb(tmp_path, name="p.db"):
    import sqlite3
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE preferences (task_id INTEGER NOT NULL, label TEXT,"
        " weight REAL DEFAULT 0.0, seen INTEGER DEFAULT 0,"
        " hit_pos INTEGER DEFAULT 0, hit_neg INTEGER DEFAULT 0,"
        " PRIMARY KEY (task_id, label))")
    # ⚠ R174：preferences 多了 source 列 → **位置式** INSERT（不写列名）
    #   会因为"列数对不上"直接炸。这里改成显式列名：测试造的是「老库」，
    #   列由 Database 打开时的迁移补，数量不该被硬编码在测试里。
    _ins = ("INSERT INTO preferences(task_id,label,weight,seen,hit_pos,hit_neg)"
            " VALUES(?,?,?,?,?,?)")
    con.execute(_ins, (0, 'cat', 1.5, 3, 2, 1))
    con.execute(_ins, (0, 'dog', -0.5, 1, 0, 1))
    con.commit()
    con.close()
    from media_resource_manager.db import Database
    return Database(p)


def test_set_weight_sets_not_accumulates(tmp_path):
    """核心语义：直接设为，不是累加。"""
    from media_resource_manager.repo import set_preference_weight
    db = _mkdb(tmp_path)
    try:
        assert set_preference_weight(db, "cat", 0.4) is True
        w = db.query("SELECT weight FROM preferences "
                     "WHERE label='cat'")[0]["weight"]
        assert w == pytest.approx(0.4), f"不是直接设置：{w}"
    finally:
        db.close()


def test_set_weight_keeps_hit_history(tmp_path):
    """命中次数是被点过几次的历史事实，改权重不该抹掉它。"""
    from media_resource_manager.repo import set_preference_weight
    db = _mkdb(tmp_path)
    try:
        set_preference_weight(db, "cat", 2.0)
        r = db.query("SELECT seen,hit_pos,hit_neg FROM preferences "
                     "WHERE label='cat'")[0]
        assert (r["seen"], r["hit_pos"], r["hit_neg"]) == (3, 2, 1), dict(r)
    finally:
        db.close()


def test_set_weight_can_go_negative(tmp_path):
    """改成负数 = 把这个词从「喜欢」掰成「排除词」方向，这是核心修法。"""
    from media_resource_manager.repo import set_preference_weight
    db = _mkdb(tmp_path)
    try:
        set_preference_weight(db, "cat", -1.2)
        assert db.query("SELECT weight w FROM preferences "
                        "WHERE label='cat'")[0]["w"] == pytest.approx(-1.2)
    finally:
        db.close()


def test_set_weight_missing_label_returns_false(tmp_path):
    from media_resource_manager.repo import set_preference_weight
    db = _mkdb(tmp_path)
    try:
        assert set_preference_weight(db, "nope", 1.0) is False
    finally:
        db.close()


def test_set_weight_only_touches_global_task(tmp_path):
    """权重是全局的（task_id=0）；别的任务的行不许被误改。"""
    from media_resource_manager.repo import set_preference_weight
    db = _mkdb(tmp_path)
    try:
        db.execute("INSERT INTO preferences(task_id,label,weight,seen,"
                   "hit_pos,hit_neg) VALUES(7,'cat',9.9,0,0,0)")
        set_preference_weight(db, "cat", 0.1)
        rows = {r["task_id"]: r["weight"] for r in
                db.query("SELECT task_id,weight FROM preferences "
                         "WHERE label='cat'")}
        assert rows == {0: pytest.approx(0.1), 7: 9.9}, rows
    finally:
        db.close()


def test_delete_preference_helper(tmp_path):
    from media_resource_manager.repo import delete_preference
    db = _mkdb(tmp_path)
    try:
        assert delete_preference(db, "cat") is True
        assert delete_preference(db, "cat") is False   # 幂等
        assert db.query("SELECT COUNT(*) c FROM preferences "
                        "WHERE label='cat'")[0]["c"] == 0
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _open_prefs(tab, monkeypatch, name):
    """跑一次 show_prefs_table，截获对话框，返回 (dlg, tbl, 按钮)。"""
    from PySide6.QtWidgets import QDialog, QTableWidget, QPushButton

    box = {}

    def fake_exec(self):
        box["dlg"] = self
        for w in self.findChildren(QTableWidget):
            box["tbl"] = w
        box["btns"] = {b.text(): b for b in self.findChildren(QPushButton)
                       if b.parent() is self or True}
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    tab.show_prefs_table()
    return box


def test_prefs_panel_has_edit_weight_button(tmp_path, monkeypatch):
    from media_resource_manager.gui.review_tab import ReviewTab
    from media_resource_manager.config import AppConfig

    db = _mkdb(tmp_path, "t.db")
    try:
        cfg = AppConfig()
        tab = ReviewTab(db, cfg.isolate_dir, lambda s: None, cfg=cfg)
        try:
            box = _open_prefs(tab, monkeypatch, "t.db")
            titles = set(box["btns"])
            assert "改选中权重…" in titles, titles
            assert "删除选中权重" in titles, "改权重不能顶掉删除"
        finally:
            tab.close()
    finally:
        db.close()


def test_edit_button_writes_weight(tmp_path, monkeypatch):
    """点「改选中权重…」→ 输入 0.4 → 库里那条真变成 0.4。"""
    from PySide6.QtWidgets import QDialog, QInputDialog, QTableWidget
    from PySide6.QtWidgets import QAbstractItemView
    from media_resource_manager.gui.review_tab import ReviewTab
    from media_resource_manager.config import AppConfig

    db = _mkdb(tmp_path, "e.db")
    try:
        cfg = AppConfig()
        tab = ReviewTab(db, cfg.isolate_dir, lambda s: None, cfg=cfg)
        try:
            box = {}
            dlg_holder = {}

            def fake_exec(self):
                dlg_holder["d"] = self
                for w in self.findChildren(QTableWidget):
                    box["tbl"] = w
                return QDialog.DialogCode.Accepted

            monkeypatch.setattr(QDialog, "exec", fake_exec)
            monkeypatch.setattr(QInputDialog, "getDouble",
                                staticmethod(lambda *a, **k: (0.4, True)))
            tab.show_prefs_table()
            tbl = box["tbl"]
            # cat 权重 1.5 排第一（ABS 排序）
            tbl.selectRow(0)
            # cat 权重 1.5 排第一（ABS 排序）。显示层走 label_text，
            # 但 i18n 映射表未必收这个词 —— 所以只断言「第一行是cat 那条」。
            from media_resource_manager.i18n import label_text
            assert tbl.item(0, 0).text() == label_text("cat"), \
                tbl.item(0, 0).text()
            from PySide6.QtWidgets import QPushButton
            b_edit = [b for b in dlg_holder["d"].findChildren(QPushButton)
                      if b.text() == "改选中权重…"][0]
            b_edit.click()
            w = db.query("SELECT weight FROM preferences "
                         "WHERE label='cat'")[0]["weight"]
            assert w == pytest.approx(0.4), f"没改成：{w}"
        finally:
            tab.close()
    finally:
        db.close()


def test_edit_button_cancel_keeps_weight(tmp_path, monkeypatch):
    """取消输入不许改任何东西。"""
    from PySide6.QtWidgets import QDialog, QInputDialog, QTableWidget, QPushButton
    from media_resource_manager.gui.review_tab import ReviewTab
    from media_resource_manager.config import AppConfig

    db = _mkdb(tmp_path, "c.db")
    try:
        cfg = AppConfig()
        tab = ReviewTab(db, cfg.isolate_dir, lambda s: None, cfg=cfg)
        try:
            holder, tblbox = {}, {}

            def fake_exec(self):
                holder["d"] = self
                for w in self.findChildren(QTableWidget):
                    tblbox["t"] = w
                return QDialog.DialogCode.Accepted

            monkeypatch.setattr(QDialog, "exec", fake_exec)
            monkeypatch.setattr(QInputDialog, "getDouble",
                                staticmethod(lambda *a, **k: (0.0, False)))
            tab.show_prefs_table()
            tblbox["t"].selectRow(0)
            [b for b in holder["d"].findChildren(QPushButton)
             if b.text() == "改选中权重…"][0].click()
            assert db.query("SELECT weight FROM preferences "
                            "WHERE label='cat'")[0]["weight"] == pytest.approx(1.5)
        finally:
            tab.close()
    finally:
        db.close()


# ==================== A2-3 转移原文显示 ====================
def test_transfer_display_exact_format():
    from media_resource_manager.classify.label_transfer import transfer_display
    assert transfer_display("金发", "blond hair", 0.71) == \
        "金发 → blond hair（相似度 0.71）"


def test_transfer_display_without_similarity():
    """没模型 → 「相似度 —」，**不是 0.00**。"""
    from media_resource_manager.classify.label_transfer import transfer_display
    s = transfer_display("金发", "blond hair", None)
    assert s == "金发 → blond hair（相似度 —）", s
    assert "0.00" not in s


def test_transfer_display_rounds_two_decimals():
    from media_resource_manager.classify.label_transfer import transfer_display
    assert "0.70" in transfer_display("a", "b", 0.7034)
    assert "1.00" in transfer_display("a", "b", 1.0)


def test_transfer_display_bad_sim_is_dash():
    """垃圾值不能崩、也不能显示成数字。"""
    from media_resource_manager.classify.label_transfer import transfer_display
    assert "相似度 —" in transfer_display("a", "b", object())


def test_transfer_display_empty_inputs():
    from media_resource_manager.classify.label_transfer import transfer_display
    assert transfer_display("", "", 0.5) == ""


def test_text_similarity_none_without_model(monkeypatch):
    """CPU-only 笔记本常态：没有本地 CLIP → None。"""
    from media_resource_manager.classify import label_transfer

    class Stub:
        backend = "stub"
        _clip = None

    assert label_transfer.text_similarity("金发", clf=Stub()) is None


def test_text_similarity_none_for_empty():
    from media_resource_manager.classify.label_transfer import text_similarity
    assert text_similarity("", clf=object()) is None
    assert text_similarity(None, clf=object()) is None


def test_text_similarity_uses_clip_when_available():
    """有 CLIP 时用文本塔余弦，且映射到 0~1（与阈值同口径）。"""
    import numpy as np
    from media_resource_manager.classify import label_transfer

    class FakeClip:
        @staticmethod
        def encode_text(texts):
            v = np.array([[3.0, 4.0]])      # 模长 5
            return v / np.linalg.norm(v, axis=-1, keepdims=True)

    class FakeClf:
        backend = "clip"
        _clip = FakeClip()

    sim = label_transfer.text_similarity("金发", clf=FakeClf())
    assert sim == pytest.approx(1.0, abs=1e-6)   # 自比=完全一样→1.0


def test_text_similarity_swallows_exceptions():
    from media_resource_manager.classify.label_transfer import text_similarity

    class Boom:
        backend = "clip"

        class _clip:
            @staticmethod
            def encode_text(t):
                raise RuntimeError("模型炸了")

    assert text_similarity("金发", clf=Boom()) is None


def test_transfer_hint_end_to_end():
    from media_resource_manager.classify.label_transfer import transfer_hint

    class Stub:
        backend = "stub"
        _clip = None

    assert transfer_hint("金发", "blond hair", clf=Stub()) == \
        "金发 → blond hair（相似度 —）"


def test_transfer_note_explains_missing_model():
    from media_resource_manager.classify.label_transfer import transfer_note
    s = transfer_note("金发", "blond hair", None)
    assert "没装本地 CLIP 模型" in s, s
    assert "金发 → blond hair（相似度 —）" in s
    # 有相似度时不加解释行
    assert "没装本地 CLIP 模型" not in transfer_note("金发", "blond hair", 0.71)


def test_tag_action_text_shows_english_target():
    """审核页细分词菜单项要写成「金发 → blond hair」让用户看见比的是哪个词。"""
    from media_resource_manager.gui.review_tab import ReviewTab
    assert ReviewTab._tag_action_text("长发") == "长发 → long hair"


def test_tag_action_text_no_translation_passthrough():
    from media_resource_manager.gui.review_tab import ReviewTab
    assert ReviewTab._tag_action_text("自造词") == "自造词"