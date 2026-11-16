"""全面清理：保留适配器/模型/学习记录/设置/Cookie，清空任务/图/审核/隔离区/缩略图/备份/日志。"""
from pathlib import Path

from media_resource_manager.db import Database
from media_resource_manager.config import AppConfig
from media_resource_manager.cleanup import perform_full_cleanup


def _tmp_cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig(
        db_path=tmp_path / "mrm.db",
        library_dir=tmp_path / "library",
        rules_dir=tmp_path / "rules",
        isolate_dir=tmp_path / "quarantine",
        review_dir=tmp_path / "review",
        log_dir=tmp_path / "logs",
    )
    cfg.ensure_dirs()
    # 顺手写几个物理文件，验证清理会真删
    for name in ("library/a.jpg", "review/r.jpg", "quarantine/q.jpg",
                 "thumbs/t.jpg", "backups/b.zip", "logs/l.log"):
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    # 模型目录（永远不清理）也造一个文件（用普通扩展名，避开沙箱对 .onnx 的拦截）
    (tmp_path / "models" / "m.bin").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "models" / "m.bin").write_bytes(b"model")
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "cookies.json").write_text("{}", encoding="utf-8")
    return cfg


def test_full_cleanup_preserves_learning_and_rules(tmp_path: Path):
    cfg = _tmp_cfg(tmp_path)
    db = Database(cfg.db_path)

    # 造数据：任务 + 图 + 学习权重(旧 task_id=5) + 适配器规则行
    db.execute("INSERT INTO tasks(name) VALUES(?)", ("t1",))
    db.execute("INSERT INTO images(task_id,source_url,path) VALUES(?,?,?)",
               (1, "https://x/a.jpg", str(tmp_path / "library" / "a.jpg")))
    db.execute("INSERT INTO preferences(task_id,label,weight,seen) VALUES(?,?,?,?)",
               (5, "cat", 1.0, 2))
    db.execute("INSERT INTO rules(name,version,content) VALUES(?,?,?)",
               ("pictoa", 1, "x"))
    db.execute("INSERT INTO reviews(image_id,decision) VALUES(?,?)", (1, "like"))

    perform_full_cleanup(db, cfg, root=tmp_path)

    # 1) 学习记录保留，且合并到全局 task_id=0
    prefs = db.query("SELECT task_id,label,weight FROM preferences")
    assert len(prefs) == 1, f"学习记录应保留，实际 {prefs}"
    assert prefs[0]["task_id"] == 0 and prefs[0]["label"] == "cat", prefs[0]

    # 2) 适配器规则表保留
    assert [dict(r) for r in db.query("SELECT name FROM rules")] == [{"name": "pictoa"}]

    # 3) 业务表清空
    for tbl in ("tasks", "images", "reviews", "compliance", "audit_log"):
        assert db.query(f"SELECT COUNT(*) AS n FROM {tbl}")[0]["n"] == 0, tbl

    # 4) 物理文件夹：业务目录被清空（文件没了，目录重建为空）
    for name in ("library/a.jpg", "review/r.jpg", "quarantine/q.jpg",
                 "thumbs/t.jpg", "backups/b.zip", "logs/l.log"):
        assert not (tmp_path / name).exists(), f"{name} 应被删除"
    for d in ("library", "review", "quarantine", "thumbs", "backups", "logs"):
        assert (tmp_path / d).is_dir(), f"{d} 应被重建"

    # 5) 特权目录一律不碰
    assert (tmp_path / "models" / "m.bin").exists(), "模型不能清"
    assert (tmp_path / "config.json").exists(), "设置不能清"
    assert (tmp_path / "cookies.json").exists(), "Cookie 不能清"
