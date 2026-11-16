"""r41 / #37：全面清理从「一句笼统二次确认」改成**复选项清单（plan 驱动）**。

锁三条：
1. 清什么 = 勾了什么 —— 只勾 logs 就只能 logs 没，任务表、图库文件原样在；
2. 「已落库图 / 待审核图」勾上＝**真删硬盘**（不走回收站，不受 use_recycle 影响）；
3. 特权项（站点适配器 / AI 模型 / 学习记录）**默认不在清单里**，必须显式勾才动。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QListWidget  # noqa: E402

from media_resource_manager import cleanup  # noqa: E402
from media_resource_manager.cleanup import (CLEAN_ITEMS, DEFAULT_PLAN,  # noqa: E402
                                            perform_full_cleanup)

ALL_KEYS = {k for k, _n, _d, _s in CLEAN_ITEMS}
PRIVILEGED = {"prefs", "rules", "models"}


def _cfg(tmp_path):
    class Cfg:
        library_dir = tmp_path / "library"
        review_dir = tmp_path / "review"
        isolate_dir = tmp_path / "quarantine"
        rules_dir = tmp_path / "rules"
    for d in (Cfg.library_dir, Cfg.review_dir, Cfg.isolate_dir, Cfg.rules_dir):
        d.mkdir(parents=True, exist_ok=True)
    (tmp_path / "library" / "a.jpg").write_bytes(b"x")
    (tmp_path / "review" / "r.jpg").write_bytes(b"x")
    (tmp_path / "quarantine" / "q.jpg").write_bytes(b"x")
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "l.log").write_bytes(b"x")
    return Cfg()


def _db(tmp_path, cfg, n_pref=2):
    from media_resource_manager.db import Database

    db = Database(tmp_path / "m.db")
    db.execute("INSERT INTO tasks(name) VALUES('t1')")
    db.execute("INSERT INTO images(task_id,source_url,path) VALUES(1,'u',?)",
               (str(cfg.library_dir / "a.jpg"),))
    for i in range(n_pref):
        db.execute("INSERT INTO preferences(task_id,label,weight,seen) "
                   "VALUES(0,?,1.0,1)", (f"p{i}",))
    db.execute("INSERT INTO rules(name,version,content) VALUES('r1',1,'x')")
    # Database.execute 自带提交，这里只把上面几条落盘（它没暴露 commit()）
    db._conn.commit()
    return db


def _count(db, tbl):
    return db.query(f"SELECT COUNT(*) c FROM {tbl}")[0]["c"]


# ---------------- 清单常量本身 ----------------
def test_every_item_is_mappable_to_tables_or_dirs():
    """CLEAN_ITEMS 里每个 key 都得真能映射（表或目录），不能有摆设项。"""
    assert len(ALL_KEYS) == len(CLEAN_ITEMS), "key 重复了"
    for key, name, danger, desc in CLEAN_ITEMS:
        assert desc and name, f"{key} 缺名字/说明"
        assert danger in ("normal", "risky"), f"{key} 危险等级非法"
        tables = cleanup._ITEM_TABLES.get(key, ())
        dirs = cleanup._ITEM_DIRS.get(key, ())
        assert tables or dirs, f"{key}（{name}）既不清表也不清目录，是摆设"


def test_default_plan_excludes_privileged_items():
    """默认勾的只能是业务数据：学习记录 / 适配器 / 模型一律不进默认勾选项。"""
    assert not (DEFAULT_PLAN & PRIVILEGED), f"特权项被默认勾上：{DEFAULT_PLAN & PRIVILEGED}"


# ---------------- plan 驱动 ----------------
def test_plan_clears_only_what_is_checked(tmp_path):
    """只勾 logs：日志没了，任务表 / 图库文件 / 学习记录 / 规则全留着。"""
    cfg = _cfg(tmp_path)
    db = _db(tmp_path, cfg)
    try:
        perform_full_cleanup(db, cfg, tmp_path, plan={"logs"})

        assert not (tmp_path / "logs" / "l.log").exists(), "日志没清掉"
        # 其余物理文件原样
        assert (cfg.library_dir / "a.jpg").exists(), "没勾图库别把图删了"
        assert (cfg.review_dir / "r.jpg").exists()
        assert (cfg.isolate_dir / "q.jpg").exists()
        # 表也没动
        assert _count(db, "tasks") == 1
        assert _count(db, "images") == 1
        assert _count(db, "preferences") == 2
        assert _count(db, "rules") == 1
    finally:
        db.close()


def test_risky_items_really_delete_from_disk(tmp_path):
    """「已落库图」勾上 = 真删硬盘，并且不受 use_recycle 影响（清理不做回收站）。"""
    cfg = _cfg(tmp_path)
    db = _db(tmp_path, cfg)
    try:
        perform_full_cleanup(db, cfg, tmp_path,
                             plan={"library", "review", "isolate"})
        for name in ("a.jpg", "r.jpg", "q.jpg"):
            folder = {"a.jpg": cfg.library_dir, "r.jpg": cfg.review_dir,
                      "q.jpg": cfg.isolate_dir}[name]
            assert not (folder / name).exists(), f"{name} 还在硬盘上＝没真删"
            assert folder.is_dir(), "目录要重建，别让下次启动报错"
        assert _count(db, "images") == 0 and _count(db, "reviews") == 0
    finally:
        db.close()


def test_privileged_items_only_cleared_when_checked(tmp_path):
    """特权项：默认 plan 下一条不动；显式勾上才真删（规则表 + 模型目录 + 学习记录）。"""
    cfg = _cfg(tmp_path)
    db = _db(tmp_path, cfg)
    try:
        # 默认：preferences 归并到 task_id=0 但一行不少、rules 在、models 目录在
        (tmp_path / "models").mkdir(exist_ok=True)
        (tmp_path / "models" / "m.bin").write_bytes(b"m")
        perform_full_cleanup(db, cfg, tmp_path)
        assert _count(db, "preferences") == 2
        assert _count(db, "rules") == 1
        assert (tmp_path / "models" / "m.bin").exists()
        # query() 返回的是 sqlite3.Row，不是 dict
        tids = {r["task_id"] for r in db.query("SELECT DISTINCT task_id FROM preferences")}
        assert tids == {0}, f"学习记录没归并到全局：{tids}"

        # 显式勾：全勾（含 prefs/rules/models）
        perform_full_cleanup(db, cfg, tmp_path, plan=ALL_KEYS)
        assert _count(db, "preferences") == 0, "学习记录没真删"
        assert _count(db, "rules") == 0, "规则表没真删"
        assert not (tmp_path / "models" / "m.bin").exists(), "模型目录没删"
    finally:
        db.close()


# ---------------- GUI：复选项清单 ----------------
@pytest.fixture(autouse=True)
def _qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _win():
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


def _patch_exec(monkeypatch, on_exec, set_prefs=False):
    from PySide6.QtCore import Qt

    def fake(self):
        lst = self.findChildren(QListWidget)
        if lst and set_prefs:
            for i in range(lst[0].count()):
                it = lst[0].item(i)
                if it.text().startswith("学习记录"):
                    it.setCheckState(Qt.CheckState.Checked)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", fake)


def test_plan_dialog_defaults_to_business_only(monkeypatch):
    _patch_exec(monkeypatch, None)
    w = _win()
    try:
        plan = w._cleanup_plan_dialog()
        assert plan == set(DEFAULT_PLAN), f"默认勾选跑了：{sorted(plan)}"
        assert "prefs" not in plan and "rules" not in plan
    finally:
        w.close()


def test_plan_dialog_can_take_extra_item(monkeypatch):
    _patch_exec(monkeypatch, None, set_prefs=True)
    w = _win()
    try:
        plan = w._cleanup_plan_dialog()
        assert "prefs" in plan, "勾了「学习记录」却没进 plan"
        assert plan != set(DEFAULT_PLAN)
    finally:
        w.close()


def test_plan_dialog_cancel_returns_none(monkeypatch):
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    w = _win()
    try:
        assert w._cleanup_plan_dialog() is None, "取消却返回了非空 plan"
    finally:
        w.close()
