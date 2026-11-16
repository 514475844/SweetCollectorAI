"""r40：30 分钟按钮扫描实测出来的两个问题，锁死。

1) **启动首帧卡 35s**：看门狗连续 6 次 qt_loop=STALL，主线程栈全停在
   stats.py:30 的 `Path(r["path"]).exists()` —— 7180 张图逐条 stat。
   改成 `_existing_paths()`：先 os.scandir 列父目录，再做字符串比较。
   这里锁：新旧结果必须**完全一致**（不能为了快而算错 on_disk）。

2) **「相似图聚类去重」直接抛异常**：长耗时操作跑在后台线程，期间图库视图
   可能被刷新重建，QPushButton 已被 shiboken 回收，主线程再
   btn.setEnabled(False) 就是
   RuntimeError: libshiboken: Internal C++ object already deleted。
   这里锁：按钮已经死了也必须安静跳过，不能掀翻整个操作。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from media_resource_manager import stats


def test_existing_paths_matches_path_exists(tmp_path):
    """新旧实现结果必须一模一样（含不存在的路径、重复路径、目录本身）。"""
    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"y")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.webp").write_bytes(b"z")

    paths = [
        str(tmp_path / "a.jpg"),
        str(tmp_path / "b.png"),
        str(sub / "c.webp"),
        str(tmp_path / "missing.jpg"),      # 不存在
        str(tmp_path / "missing2.jpg"),     # 同目录再缺一个
        str(tmp_path / "a.jpg"),            # 重复
        "",                                 # 空串（旧写法直接跳过）
        None,
    ]
    old = set(p for p in paths if p and Path(p).exists())
    new = stats._existing_paths(paths)
    assert new == old, f"新实现与 Path.exists() 不一致：多 {new-old} 少 {old-new}"
    assert len(new) == 3


def test_existing_paths_handles_missing_dir(tmp_path):
    """父目录整个不存在时不能抛（列不到就当作全不存在）。"""
    paths = [str(tmp_path / "no_such_dir" / "x.jpg")]
    assert stats._existing_paths(paths) == set()


def test_library_stats_on_disk_matches_slow_truth(tmp_path):
    """library_stats 报出来的 on_disk 必须等于逐条 exists() 的真值。"""
    from media_resource_manager.db import Database

    db = Database(tmp_path / "t.db")
    try:
        (tmp_path / "keep.jpg").write_bytes(b"abc")
        for i, (p, b) in enumerate([(str(tmp_path / "keep.jpg"), True),
                                    (str(tmp_path / "gone.jpg"), True),
                                    (None, False)]):
            db.execute(
                "INSERT INTO images (path, source_url, bytes, task_id, "
                "collected_at) VALUES (?,?,?,?,?)",
                (p, "http://x/%d" % i, 10, i, "2026-01-01 00:00:00"))
        st = stats.library_stats(db, tmp_path)
        assert st["total_records"] == 3
        assert st["on_disk"] == 1, "on_disk 只能算真正落盘的那张"
    finally:
        db.close()


def test_dedupe_survives_dead_button(qapp_like=None):
    """V0.0.5 改判：聚类去重功能已**彻底移除**（用户拍板不造轮子）。

    原来这条测的是「按钮被 shiboken 回收后调用不炸」——现在按钮和方法
    都不存在了，钉反方向：MainWindow 不许再有这两个成员，界面源码里
    也不许再有「相似图聚类去重」入口。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.main_window import MainWindow

    assert not hasattr(MainWindow, "gallery_cluster_dedupe"), \
        "聚类去重入口必须已移除"
    import inspect
    src = inspect.getsource(MainWindow)
    assert "相似图聚类去重" not in src or "已撤" in src, \
        "界面上不该再有聚类去重按钮"


def test_dedupe_button_helper_never_raises(qapp_like=None):
    """_set_btn 语义：按钮是 None 或已死，都必须安静吞掉。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QPushButton

    app = QApplication.instance() or QApplication([])
    b = QPushButton("x")
    b.deleteLater()
    app.processEvents()

    def _set_btn(btn, enabled):
        try:
            if btn is not None:
                btn.setEnabled(enabled)
        except RuntimeError:
            pass

    _set_btn(None, False)   # None
    _set_btn(b, False)      # 已死
    _set_btn(QPushButton("y"), True)   # 活着的


def test_engine_classifier_slots_always_exist():
    """Engine 一实例化就必须有 _clf / _excl_clf，不能等到 run() 里才建。

    r40 真机抓到：AttributeError: 'Engine' object has no attribute '_excl_clf'
    → 整条任务判失败。原因为这两句写在 run() 里、且位于「gallery-dl 快路径
    提前 return」之后，走快路径时压根没执行。
    """
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager.engine import Engine
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = AppConfig()
        cfg.data_dir = td
        db = Database(td + "/t.db")
        try:
            eng = Engine(cfg, db)
            # 刚建好就该是 None（不是 AttributeError）
            assert hasattr(eng, "_clf")
            assert hasattr(eng, "_excl_clf")
            assert eng._clf is None
            assert eng._excl_clf is None
            # 走「无分类器」分支也不能炸
            assert eng._ai_excluded(None, r"D:/nonexistent/x.jpg")[0] == "pass"
        finally:
            try:
                eng._ex.shutdown(wait=False)
            except Exception:
                pass
            db.close()


def test_ai_exclude_state_passes_without_classifier():
    """没有排除分类器时必须返回 pass，绝不能 AttributeError。"""
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager.engine import Engine
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = AppConfig()
        cfg.data_dir = td
        db = Database(td + "/t.db")
        eng = None
        try:
            eng = Engine(cfg, db)

            class _T:
                ai_threshold = 0.5
                ai_blacklist = ["x"]
            res = eng._ai_excluded(_T(), r"D:/nonexistent/x.jpg")
            assert res[0] == "pass", res
        finally:
            if eng is not None:
                try:
                    eng._ex.shutdown(wait=False)
                except Exception:
                    pass
            db.close()


def test_db_concurrent_writes_do_not_lock():
    """多线程共用一个 Database 连接写库，不能抛 database is locked。

    r40 第二轮 30 分钟扫描实录：「相似图聚类去重」的 QThread 和「清理过期
    回收站」各自撞 sqlite3.OperationalError: database is locked，
    QThread 整个 run() 抛异常被 Qt 静默吞掉 → 操作做到一半就没了。
    根因：WAL + busy_timeout 只管「多连接」，管不了「同一个连接对象被两个线程
    同时 execute/commit」（Python 的 sqlite3 连接不是线程安全的）。
    """
    import threading
    import tempfile
    from media_resource_manager.db import Database

    with tempfile.TemporaryDirectory() as td:
        db = Database(td + "/c.db")
        errs = []

        def writer(tag, n):
            try:
                for i in range(n):
                    db.execute(
                        "INSERT INTO audit_log(actor, action, target, detail) "
                        "VALUES (?,?,?,?)", (tag, "w%d" % i, "-", "-"))
                    db.query("SELECT COUNT(*) FROM audit_log")
            except Exception as e:
                errs.append("%s: %s: %s" % (tag, type(e).__name__, e))

        def reader(tag, n):
            try:
                for _ in range(n):
                    db.query("SELECT COUNT(*) FROM images")
            except Exception as e:
                errs.append("%s: %s: %s" % (tag, type(e).__name__, e))

        ts = [threading.Thread(target=writer, args=("w%d" % k, 120))
              for k in range(3)]
        ts += [threading.Thread(target=reader, args=("r%d" % k, 120))
               for k in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        try:
            assert not errs, "并发写库炸了：" + " | ".join(errs[:5])
            assert db.query("SELECT COUNT(*) c FROM audit_log")[0]["c"] == 360
        finally:
            db.close()


def test_db_close_is_idempotent_and_locked():
    """close() 必须幂等且走锁（r35 语义不能被 r40 的锁改坏）。"""
    import tempfile
    from media_resource_manager.db import Database

    with tempfile.TemporaryDirectory() as td:
        db = Database(td + "/d.db")
        assert db.is_closed() is False
        db.close()
        db.close()          # 幂等
        assert db.is_closed() is True
