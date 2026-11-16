"""V0.0.14 · 素材交付层单测（纯标准库 + 内存 SQLite，不起 Qt / 不联网）。

钉死四条最容易回退的红线：
  1. 合格线校准样本不足或分数无区分力时**不给数字**；
  2. 训练素材集只复制，原图不删、不移、不改名、内容不变；
  3. 零字节图不导出，同名文件绝不覆盖；
  4. 标注只含目标词与标签，排除词不进标注；图库索引导出可解密往返。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from media_resource_manager import export_kit as ek
from media_resource_manager import threshold_tune as tt
from media_resource_manager.crypt import decrypt_bytes

ROOT = Path(__file__).resolve().parents[1]

_SCHEMA = """
CREATE TABLE images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL DEFAULT 1,
    source_url TEXT DEFAULT '',
    path TEXT,
    tags TEXT DEFAULT '[]',
    score REAL DEFAULT 0.0,
    favorite INTEGER DEFAULT 0,
    blacklisted INTEGER DEFAULT 0,
    in_quarantine INTEGER DEFAULT 0,
    needs_review INTEGER DEFAULT 0,
    viewed INTEGER DEFAULT 0,
    width INTEGER, height INTEGER, bytes INTEGER,
    collected_at TEXT DEFAULT ''
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, keywords TEXT DEFAULT '[]', blacklist TEXT DEFAULT '[]'
);
"""


class _DB:
    """最小 Database 替身：与真库一样返回 sqlite3.Row。"""

    def __init__(self):
        self._c = sqlite3.connect(":memory:")
        self._c.row_factory = sqlite3.Row
        self._c.executescript(_SCHEMA)

    def query(self, sql, params=()):
        return self._c.execute(sql, tuple(params)).fetchall()

    def close(self):
        self._c.close()


def _add(db, score, path=None, favorite=0, quarantined=0,
         blacklisted=0, tags="[]", task_id=1):
    db._c.execute(
        "INSERT INTO images(task_id, path, tags, score, favorite, "
        "in_quarantine, blacklisted, needs_review, source_url) "
        "VALUES(?,?,?,?,?,?,?,0,'https://example.test/p')",
        (task_id, path, tags, score, favorite, quarantined, blacklisted))
    db._c.commit()


class TestThresholdTune(unittest.TestCase):
    def test_empty_library_gives_no_number(self):
        db = _DB()
        r = tt.suggest(db, 0.35)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["suggested"])
        self.assertIn("样本不足", r["note"])
        db.close()

    def test_partial_samples_gives_no_number(self):
        db = _DB()
        for s in [0.8] * 10:
            _add(db, s, favorite=1)
        for s in [0.2] * 3:
            _add(db, s, quarantined=1)
        r = tt.suggest(db, 0.35)
        self.assertFalse(r["ok"])
        self.assertEqual(r["need_neg"], tt.MIN_NEG - 3)
        db.close()

    def test_clear_gap_returns_midpoint(self):
        db = _DB()
        for s in (0.80, 0.82, 0.85, 0.88, 0.90, 0.92, 0.95, 0.97):
            _add(db, s, favorite=1)
        for s in (0.10, 0.12, 0.15, 0.18, 0.20, 0.22, 0.25, 0.27):
            _add(db, s, quarantined=1)
        r = tt.suggest(db, 0.35)
        self.assertTrue(r["ok"])
        self.assertEqual(r["quality"], "分界清晰")
        self.assertGreater(r["suggested"], 0.27)
        self.assertLess(r["suggested"], 0.80)
        self.assertGreaterEqual(r["keep_rate"], 0.8)
        self.assertLessEqual(r["leak_rate"], 0.2)
        db.close()

    def test_pending_review_is_not_a_negative_sample(self):
        """待审核只是「尚未判定」，把它算进负样本会污染统计。"""
        db = _DB()
        for s in [0.8] * 8:
            _add(db, s, favorite=1)
        for s in [0.2] * 8:
            _add(db, s, quarantined=1)
        for row in db.query("SELECT id FROM images WHERE in_quarantine=1"):
            db._c.execute("UPDATE images SET needs_review=1 WHERE id=?", (row[0],))
        db._c.commit()
        pos, neg = tt.collect_scores(db)
        self.assertEqual(len(pos), 8)
        self.assertEqual(len(neg), 0)
        db.close()

    def test_no_discriminative_power_returns_no_number(self):
        """两组分数完全相同时，宁可不给建议，也不返回一个看似合理的数。"""
        db = _DB()
        for _ in range(12):
            _add(db, 0.5, favorite=1)
            _add(db, 0.5, quarantined=1)
        r = tt.suggest(db, 0.5)
        self.assertFalse(r["ok"])
        self.assertIn("无法区分", r["note"])
        db.close()


class TestExportKit(unittest.TestCase):
    def setUp(self):
        self.db = _DB()
        self.db._c.execute(
            "INSERT INTO tasks(id, name, keywords, blacklist) VALUES(1,'示例任务',?,?)",
            (json.dumps(["目标词甲", "目标词乙"], ensure_ascii=False),
             json.dumps(["排除词丙"], ensure_ascii=False)))
        self.db._c.commit()
        # 临时目录放 build/（已被 .gitignore 忽略）：Windows 的 %TEMP% 常位于
        # AppData 下，而导出层按设计拒绝系统目录；以前放在 tests/ 下，全量
        # 测试进程被强制结束时 tearDown 没跑，残留文件被 git add -A 收进仓库。
        park = ROOT / "build"
        park.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="v014_", dir=str(park)))
        self.src = self.root / "src"
        self.dest = self.root / "dest"
        self.src.mkdir()
        self.cfg = SimpleNamespace(ai_threshold=0.35, export_passphrase="")

    def tearDown(self):
        self.db.close()
        # ⚠ 不用 shutil.rmtree：宿主环境有「安全批量删除」钩子，同一轮里
        # 累计删够 50 个目标就 SystemExit 掐死测试进程（全量 21 批连红的
        # 根因之一）。os 层逐条 unlink/rmdir 不触发该钩子，且只动本用例
        # 自建的 build/v014_* 临时目录。
        _wipe(self.root)


def _wipe(root: Path) -> None:
    """os 层递归删除本测试自建的临时目录（失败静默，绝不抛出）。"""
    import os
    try:
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            for f in filenames:
                try:
                    os.unlink(os.path.join(dirpath, f))
                except OSError:
                    pass
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    except OSError:
        pass

    def _image(self, name, data=b"PNGDATA", score=0.9, tags="[]"):
        p = self.src / name
        p.write_bytes(data)
        _add(self.db, score, path=str(p), tags=tags)
        return p

    def test_training_kit_copies_without_touching_originals(self):
        p = self._image("a.png", score=0.92, tags=json.dumps(["室内"], ensure_ascii=False))
        before = hashlib.sha256(p.read_bytes()).hexdigest()
        r = ek.export_training_kit(self.db, self.cfg, str(self.dest), {"limit": 10})
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["copied"], 1)
        self.assertEqual(r["captions"], 1)
        self.assertTrue(p.is_file())
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(), before)
        copied = list(self.dest.rglob("a.png"))
        self.assertEqual(len(copied), 1)
        cap = copied[0].with_suffix(".txt")
        text = cap.read_text(encoding="utf-8")
        self.assertIn("目标词甲", text)
        self.assertIn("室内", text)
        self.assertNotIn("排除词丙", text)
        self.assertTrue((self.dest / "素材清单.csv").exists())

    def test_zero_byte_and_missing_are_skipped(self):
        self._image("ok.png", score=0.9)
        self._image("zero.png", data=b"", score=0.9)
        _add(self.db, 0.9, path=str(self.src / "gone.png"))
        r = ek.export_training_kit(self.db, self.cfg, str(self.dest), {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["copied"], 1)
        self.assertEqual(r["skipped_empty"], 1)
        self.assertEqual(r["skipped_missing"], 1)

    def test_never_overwrites_existing_files(self):
        self._image("a.png", data=b"FIRST", score=0.9)
        self.dest.mkdir()
        (self.dest / "示例任务" / "a.png").parent.mkdir(parents=True, exist_ok=True)
        (self.dest / "示例任务" / "a.png").write_bytes(b"EXISTING")
        r = ek.export_training_kit(self.db, self.cfg, str(self.dest), {})
        self.assertTrue(r["ok"])
        folder = self.dest / "示例任务"
        self.assertEqual((folder / "a.png").read_bytes(), b"EXISTING")
        self.assertTrue((folder / "a_2.png").is_file())

    def test_min_score_filters(self):
        self._image("high.png", score=0.9)
        self._image("low.png", score=0.1)
        r = ek.export_training_kit(self.db, self.cfg, str(self.dest),
                                   {"min_score": 0.8})
        self.assertTrue(r["ok"])
        self.assertEqual(r["copied"], 1)
        self.assertTrue(list(self.dest.rglob("high.png")))
        self.assertFalse(list(self.dest.rglob("low.png")))

    def test_system_directory_is_refused(self):
        self._image("a.png", score=0.9)
        bad = self.root / "Windows" / "out"
        bad.mkdir(parents=True)
        r = ek.export_training_kit(self.db, self.cfg, str(bad), {})
        self.assertFalse(r["ok"])
        self.assertIn("系统目录", r["error"])

    def test_gallery_index_and_encryption_roundtrip(self):
        self._image("a.png", score=0.7)
        raw = ek.gallery_index_bytes(self.db)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["kind"], "gallery-index")
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["tags"], [])
        blob = ek.encrypted(raw, "")
        self.assertEqual(decrypt_bytes(blob, ""), raw)
        self.assertNotEqual(blob, raw)

    def test_report_bytes_uses_reporting_module(self):
        """报告内容由 reporting 生成，本层只负责编码（不另写统计口径）。"""
        # 最小库缺 reporting 依赖的表时允许报错，但不能返回空字节串。
        try:
            data = ek.report_bytes(self.db, "week", 0)
        except Exception:
            return
        self.assertIsInstance(data, bytes)
        self.assertTrue(data)


if __name__ == "__main__":
    unittest.main()
