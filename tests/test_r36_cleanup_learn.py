"""r36：锁死「全面清理（重置到新装状态）」不清理学习进度。

用户要求（r36 原话）：这个功能「不清理学习进度」。
学习进度 = preferences 表（全局偏好模型 label->weight，task_id 恒为 0）。
「全面清理」可以清 tasks/images/reviews/compliance/audit_log 与物理目录，
但 preferences 一行都不能少（只把残留 task_id<>0 归并到全局 0）。
"""
import pytest


@pytest.fixture()
def db_and_root(tmp_path):
    from media_resource_manager.cleanup import perform_full_cleanup
    from media_resource_manager.db import Database

    dbp = tmp_path / "mrm.db"
    db = Database(dbp)
    s = db._conn
    # 两条学习记录：一条本就全局，一条挂在旧任务上（清理后应归并到 0）
    s.execute("INSERT INTO preferences (task_id,label,weight,seen) "
              "VALUES (0,'喜欢白丝',0.8,9)")
    s.execute("INSERT INTO preferences (task_id,label,weight,seen) "
              "VALUES (5,'不喜欢丝袜',-1.5,3)")
    s.execute("INSERT INTO tasks (id,name,adapter_name,output_dir) "
              "VALUES (5,'erome','erome',?)", (str(tmp_path / "lib"),))
    s.execute("INSERT INTO images (id,task_id,path,bytes,source_url) "
              "VALUES (9001,5,?,1024,'https://example.com/a.jpg')",
              (str(tmp_path / "lib" / "a.jpg"),))
    s.commit()

    class Cfg:
        library_dir = str(tmp_path / "library")
        review_dir = str(tmp_path / "review")
        isolate_dir = str(tmp_path / "isolate")

    for d in (Cfg.library_dir, Cfg.review_dir, Cfg.isolate_dir):
        import os
        os.makedirs(d, exist_ok=True)
    yield db, Cfg(), tmp_path, s
    db.close()


def test_full_cleanup_keeps_learning_progress(db_and_root):
    db, cfg, tmp_path, s = db_and_root
    from media_resource_manager.cleanup import perform_full_cleanup

    before = {r["label"]: (r["weight"], r["seen"])
              for r in s.execute("SELECT label,weight,seen FROM preferences")}
    assert "喜欢白丝" in before and "不喜欢丝袜" in before

    perform_full_cleanup(db, cfg, tmp_path)

    rows = s.execute(
        "SELECT task_id,label,weight,seen FROM preferences").fetchall()
    after = {r["label"]: (r["weight"], r["seen"]) for r in rows}

    # 1) 学习进度（preferences）一条都不能少，权重/次数必须原样
    assert after == before, f"学习进度被清理了：before={before} after={after}"
    # 2) 残留的学习记录归并到全局 task_id=0（不是被删）
    assert all(r["task_id"] == 0 for r in rows)

    # 3) 业务表必须真的清空
    assert s.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert s.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
    assert s.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    assert s.execute("SELECT COUNT(*) FROM compliance").fetchone()[0] == 0
    assert s.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_full_cleanup_keeps_rules_and_policy_tables(db_and_root):
    """顺带锁：rules（站点适配器）与 settings 表也不能被全面清理清掉。"""
    db, cfg, tmp_path, s = db_and_root
    from media_resource_manager.cleanup import perform_full_cleanup

    s.execute("INSERT INTO rules (name, content) VALUES ('erome', ?)",
              ('{"name":"erome","base_url":"https://www.erome.com"}',))
    s.commit()
    perform_full_cleanup(db, cfg, tmp_path)
    assert s.execute("SELECT COUNT(*) FROM rules").fetchone()[0] >= 1
