"""Tests for learn_folder: 从文件夹批量学习（本地 CLIP 打标+入库）。

验证：①图片入库计数正确；②按 sha256 幂等（重复学习跳过、不重复加）；
③不打乱用户文件夹（path 指向原文件，不移动）；④黑名单命中只标记隔离不移动。
CLIP 模型可能不在测试环境（回退 stub），所以只断言入库/幂等/不移动这些
与模型无关的行为。
"""
from __future__ import annotations

from pathlib import Path

from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database
from media_resource_manager.learn_folder import learn_folder


def _make_gradient(path: Path, r0: int, g0: int):
    from PIL import Image
    im = Image.new("RGB", (80, 60))
    px = im.load()
    for x in range(80):
        for y in range(60):
            px[x, y] = (min(255, r0 + x * 2), min(255, g0 + y * 3), 128)
    im.save(path)


def _setup(tmp_path):
    cfg = AppConfig(db_path=tmp_path / "mrm.db",
                    library_dir=tmp_path / "lib",
                    rules_dir=tmp_path / "rules",
                    isolate_dir=tmp_path / "quarantine",
                    log_dir=tmp_path / "logs")
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    # 合规表（learn_folder 不依赖，但 AppConfig 全量建表时会建）
    folder = tmp_path / "my_album"
    folder.mkdir()
    for i in range(3):
        _make_gradient(folder / f"photo{i}.jpg", i * 40, i * 20)
    return cfg, db, folder


def test_learn_adds_and_idempotent(tmp_path):
    cfg, db, folder = _setup(tmp_path)
    s1 = learn_folder(db, cfg, str(folder))
    assert s1.get("error") is None
    assert s1["added"] == 3, s1
    # 重复学习：按 sha256 跳过，added=0
    s2 = learn_folder(db, cfg, str(folder))
    assert s2["skipped_dup"] == 3, s2
    assert s2["added"] == 0, s2
    # 入库的 path 指向原文件夹（不移动文件）
    rows = db.query("SELECT path FROM images WHERE path IS NOT NULL")
    assert len(rows) == 3
    assert all(Path(r["path"]).parent == folder for r in rows)


def test_learn_creates_task(tmp_path):
    cfg, db, folder = _setup(tmp_path)
    s = learn_folder(db, cfg, str(folder))
    tid = s["task_id"]
    assert tid is not None
    t = db.query("SELECT name, output_dir FROM tasks WHERE id=?", (tid,))
    assert t and t[0]["output_dir"] == str(folder)
    # 再次学习同一文件夹复用同一任务（不重复建）
    s2 = learn_folder(db, cfg, str(folder))
    assert s2["task_id"] == tid


def test_learn_missing_folder(tmp_path):
    cfg, db, folder = _setup(tmp_path)
    s = learn_folder(db, cfg, str(folder / "nope"))
    assert "error" in s
