"""r41 / #37-D3：权重明细从三列扩成四列（连表结构，不是只改文案）。

D3 要的是「主题词命中 / 排除词命中 / 权重 / 来源」四列，而 preferences 表以前
只有 seen 一个「命中次数」、看不出方向。所以：
* 表结构加 hit_pos / hit_neg 两列（建表带上 + _migrate 给老库补列）；
* set_preference 收 kind=pos/neg，learn() 把「喜欢」记 pos、「不喜欢/拉黑」记 neg；
* 「权重」列不再把来源折进同一个格，来源单列出来。
"""
import pytest


def _mkdb(tmp_path, name="m.db", with_cols=True):
    import sqlite3

    p = tmp_path / name
    con = sqlite3.connect(str(p))
    cols = ("hit_pos INTEGER DEFAULT 0,\n    hit_neg INTEGER DEFAULT 0,"
            if with_cols else "")
    con.executescript(
        "CREATE TABLE preferences ("
        f"task_id INTEGER NOT NULL,\n    label TEXT NOT NULL,\n"
        "weight REAL DEFAULT 0.0,\n    seen INTEGER DEFAULT 0,\n"
        f"    {cols}\n    PRIMARY KEY (task_id, label))")
    if not with_cols:
        con.execute("INSERT INTO preferences(task_id,label,weight,seen) "
                    "VALUES (0,'cat',1.0,3)")
    con.commit()
    con.close()
    from media_resource_manager.db import Database
    return Database(p)


def test_old_db_gets_hit_columns(tmp_path):
    """老库（没有 hit_pos/hit_neg）打开一次就要补上 —— 不能只有新装才有这两列。"""
    from media_resource_manager.db import Database

    db = _mkdb(tmp_path, "old.db", with_cols=False)
    try:
        cols = {r["name"] for r in
                db._conn.execute("PRAGMA table_info(preferences)")}
        assert {"hit_pos", "hit_neg"} <= cols, f"老库没补上命中列：{cols}"
        assert db.query("SELECT COUNT(*) c FROM preferences")[0]["c"] == 1, \
            "迁移不能把老数据弄没"
    finally:
        db.close()


def test_set_preference_records_direction(tmp_path):
    from media_resource_manager.repo import set_preference
    from media_resource_manager.db import Database

    db = _mkdb(tmp_path)
    try:
        set_preference(db, 0, "cat", 1.0, kind="pos")
        set_preference(db, 0, "cat", 1.0, kind="pos")
        set_preference(db, 0, "cat", -2.0, kind="neg")
        set_preference(db, 0, "dog", 0.5)          # 无方向（导入偏好之类）
        r = db.query("SELECT weight,seen,hit_pos,hit_neg FROM preferences "
                     "WHERE label='cat'")[0]
        assert r["hit_pos"] == 2 and r["hit_neg"] == 1, dict(r)
        assert (r["seen"], r["weight"]) == (3, 0.0), dict(r)
        d = db.query("SELECT hit_pos,hit_neg FROM preferences WHERE label='dog'")[0]
        assert (d["hit_pos"], d["hit_neg"]) == (0, 0), dict(d)
    finally:
        db.close()


def test_learn_tags_hit_columns_by_decision(tmp_path):
    from media_resource_manager.classify.preference import PreferenceLearner
    from media_resource_manager.db import Database

    db = _mkdb(tmp_path, "l.db")
    try:
        l = PreferenceLearner(db, 0)
        l.learn("like", ["cat", "dog"])
        l.learn("dislike", ["cat"])
        l.learn("blacklist", ["cat"])
        l.learn("skip", ["cat"])                   # 跳过＝没表态，不该记命中
        got = {r["label"]: (r["hit_pos"], r["hit_neg"])
               for r in db.query("SELECT label,hit_pos,hit_neg FROM preferences")}
        assert got["cat"] == (1, 2), f"cat 的命中方向不对：{got['cat']}"
        assert got["dog"] == (1, 0), f"dog 的命中方向不对：{got['dog']}"
    finally:
        db.close()


# ---------------- 表格：四列 + 内容 ----------------
@pytest.fixture(autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _tab_of(show_prefs_table, monkeypatch):
    """跑一次 show_prefs_table，拦下弹出的 QTableWidget（exec 打桩成 Accepted）。

    ⚠ review_tab 是**函数内** import QDialog（没在模块顶层 import），所以只能
    直接给 QDialog 这个类打 exec 的桩 —— 和 #37 那个清理清单窗口一个手法。
    """
    from PySide6.QtWidgets import QDialog, QTableWidget

    box = {}

    def fake_exec(self):
        for w in self.findChildren(QTableWidget):
            box["tbl"] = w
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake_exec)
    show_prefs_table()
    return box.get("tbl")


def test_weight_table_is_four_columns(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.review_tab import ReviewTab

    db = _mkdb(tmp_path, "t.db")
    try:
        db.execute("INSERT INTO preferences(task_id,label,weight,seen,"
                   "hit_pos,hit_neg) VALUES(0,'cat',1.5,3,2,1)")
        db.execute("INSERT INTO preferences(task_id,label,weight,seen,"
                       "hit_pos,hit_neg) VALUES(0,'dog',-0.5,1,0,1)")
        db._conn.commit()

        from media_resource_manager.config import AppConfig
        cfg = AppConfig()
        tab = ReviewTab(db, cfg.isolate_dir, lambda s: None, cfg=cfg)
        try:
            tbl = _tab_of(tab.show_prefs_table, monkeypatch)
            assert tbl is not None, "权重明细没弹表格"
            heads = [tbl.horizontalHeaderItem(i).text()
                     for i in range(tbl.columnCount())]
            assert heads == ["标签", "主题词命中", "排除词命中", "权重 / 来源"], heads
            assert tbl.rowCount() == 2
            r0 = [tbl.item(0, i).text() for i in range(4)]
            assert r0[1] == "2" and r0[2] == "1", r0
            assert "主题词（喜欢）" in r0[3], r0[3]
            r1 = [tbl.item(1, i).text() for i in range(4)]
            assert r1[1] == "0" and r1[2] == "1", r1
            assert "排除词（不喜欢/拉黑）" in r1[3], r1[3]
        finally:
            tab.close()
    finally:
        db.close()
