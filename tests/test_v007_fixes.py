"""V0.0.7：这一轮核对出来的 4 个真毛病 + 桌面漏掉的勾，逐条钉住。

1. 桌面「从文件夹学习」被「学习开关」整体拦死（网页端从来不拦）；
2. 网页「只入库先不打标签」把行写成「已打标」，永远补不了标、永远学不到偏好；
3. 桌面导入把「下载的新图存哪」改写成 F 盘原图库目录；
4. 桌面导入不过滤隐藏目录，跟网页学到的口味对不上；
5. 桌面导入小窗口缺「只入库先不打标签」勾（决定清单要求两端同一个勾）。
"""
from __future__ import annotations

import json
from pathlib import Path

from media_resource_manager import import_folder as imp
from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database
from media_resource_manager.learn_folder import learn_folder


class _FakeClf:
    """别在测试里加载真模型；只要能给出一条标签即可。"""

    backend = "clip"
    model_note = ""

    def __init__(self, *a, **kw):
        pass

    def classify(self, path):
        return [("猫", 0.9)]


def _setup(tmp_path):
    from PIL import Image
    cfg = AppConfig(db_path=tmp_path / "mrm.db",
                    library_dir=tmp_path / "lib",
                    rules_dir=tmp_path / "rules",
                    isolate_dir=tmp_path / "quarantine",
                    log_dir=tmp_path / "logs")
    cfg.ensure_dirs()
    cfg.ai_labels = ["猫"]
    db = Database(cfg.db_path)
    folder = tmp_path / "my_album"
    folder.mkdir()
    for i in range(3):
        Image.new("RGB", (32, 24), (i * 40, 60, 90)).save(folder / f"p{i}.jpg")
    return cfg, db, folder


# ---------- 网页：只入库以后还能补标 ----------

def test_wake_untagged_only_touches_empty_rows(tmp_path):
    cfg = AppConfig(db_path=tmp_path / "mrm.db")
    db = Database(cfg.db_path)
    db.execute("INSERT INTO images(task_id, source_url, path, tags, ai_state) "
               "VALUES (7,'file://a','/a.jpg','[]',1)")
    db.execute("INSERT INTO images(task_id, source_url, path, tags, ai_state) "
               "VALUES (7,'file://b','/b.jpg','[\"猫\"]',1)")
    assert imp.wake_untagged_for_retag(db, 7, "file://a", "/a.jpg") == 1
    # 已经有标签的行不许被反复拖回待打标队列
    assert imp.wake_untagged_for_retag(db, 7, "file://b", "/b.jpg") == 0
    rows = {r["source_url"]: r["ai_state"]
            for r in db.query("SELECT source_url, ai_state FROM images")}
    assert rows["file://a"] == 0 and rows["file://b"] == 1
    db.close()


# ---------- 桌面：只入库 → 重导补标，且不重复入库、不碰文件 ----------

def test_index_only_then_retag(tmp_path, monkeypatch):
    from media_resource_manager.classify import clip_model
    monkeypatch.setattr(clip_model, "LocalClassifier", _FakeClf)
    cfg, db, folder = _setup(tmp_path)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}

    s1 = learn_folder(db, cfg, str(folder), do_tag=False)
    assert s1["added"] == 3
    rows = db.query("SELECT tags FROM images")
    assert len(rows) == 3
    assert all(not r["tags"] or r["tags"] == "[]" for r in rows)

    s2 = learn_folder(db, cfg, str(folder), do_tag=True)
    assert s2["added"] == 3, s2
    rows = db.query("SELECT tags, path FROM images")
    assert len(rows) == 3, "补标不许新建第二行"
    assert all("猫" in str(r["tags"]) for r in rows), rows
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before, "原图被改了"
    db.close()


def test_desktop_import_skips_hidden_dirs_like_web(tmp_path, monkeypatch):
    from PIL import Image
    from media_resource_manager.classify import clip_model
    monkeypatch.setattr(clip_model, "LocalClassifier", _FakeClf)
    cfg, db, folder = _setup(tmp_path)
    (folder / ".thumbnails").mkdir()
    Image.new("RGB", (32, 24), (1, 2, 3)).save(folder / ".thumbnails" / "t.jpg")
    (folder / "@eaDir").mkdir()
    Image.new("RGB", (32, 24), (4, 5, 6)).save(folder / "@eaDir" / "t.jpg")
    s = learn_folder(db, cfg, str(folder), do_tag=False)
    assert s["total"] == 3, f"隐藏目录里的缩略图被导进来了: {s['total']}"
    db.close()


# ---------- 桌面：不再被学习开关拦、不再污染下载输出目录 ----------

def test_model_error_still_imports_the_picture(tmp_path, monkeypatch):
    """模型偶发打分失败，不许连累入库：标签留空也要把图收进来。

    全量并发跑时这条真的复现过（learn_folder 的 copied=0、added=0），
    根因是 classify 抛异常被外层 try 吞成「这张图失败」。
    """
    from media_resource_manager.classify import clip_model

    class _BoomClf(_FakeClf):
        def classify(self, path):
            raise RuntimeError("onnx 显存不足")

    monkeypatch.setattr(clip_model, "LocalClassifier", _BoomClf)
    cfg, db, folder = _setup(tmp_path)
    s = learn_folder(db, cfg, str(folder), copy_to_library=True)
    assert s["added"] == 3, s
    assert s.get("copied") == 3, s
    assert all("猫" not in str(r["tags"]) for r in db.query("SELECT tags FROM images"))
    db.close()


def test_desktop_dialog_is_fixed():
    from media_resource_manager.gui import main_window
    src = Path(main_window.__file__).read_text(encoding="utf-8")
    i = src.index("def learn_from_folder(self):")
    seg = src[i:i + 5200]
    assert "学习已关闭" not in seg, "学习开关还在拦「导入自己的图库」"
    assert "self.cfg.last_output_dir = d" not in seg, "下载输出目录又被写成原图库了"
    assert "last_learn_dir" in seg
    assert "只入库、先不识别" in seg, "桌面缺网页端那个勾"
    assert "do_tag=_do_tag" in seg


def test_import_index_only_label_identical_on_both_ends():
    """两端这个勾必须逐字一致 —— 决定清单要求「同一个勾」。

    V0.0.27 改判（不是放宽）：网页导入区已搬到 `components/ImportFolder.vue`
    （设置页 → 图库页 → 导入与导出页），控件位置变了，判据跟着改。
    """
    from media_resource_manager.gui import main_window
    web = (Path(main_window.__file__).parents[1]
           / "webapp/src/components/ImportFolder.vue")
    desktop = Path(main_window.__file__).read_text(encoding="utf-8")
    label = "只入库、先不识别"
    assert label in desktop and label in web.read_text(encoding="utf-8")


def test_learn_worker_forwards_do_tag():
    from media_resource_manager.gui import main_window
    src = Path(main_window.__file__).read_text(encoding="utf-8")
    i = src.index("class _LearnWorker")
    seg = src[i:i + 2200]
    assert "self.do_tag = do_tag" in seg
    assert "do_tag=self.do_tag" in seg


def test_last_learn_dir_roundtrip_and_independence():
    c = AppConfig.from_dict({"last_learn_dir": "D:/原图库",
                             "last_output_dir": "D:/下载"})
    assert isinstance(c.last_learn_dir, Path)
    assert c.last_learn_dir != c.last_output_dir
    c2 = AppConfig.from_dict(c.to_dict())
    assert c2.last_learn_dir == c.last_learn_dir
    assert c2.last_output_dir == c.last_output_dir
