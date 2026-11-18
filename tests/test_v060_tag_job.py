# -*- coding: utf-8 -*-
"""V0.0.60 护栏：AI 识别独立作业 —— 与导入分开、各管各的进度/停止。

用户三条诉求：
  1. 进度条各管各的：导入一条、识别一条，不再混在整体流程里（96%/19% 的怪现象）；
  2. 导入时能识别、识别时能导入（两把独立的锁，可同时跑）；
  3. 识别能随时启动/停止，不影响导入。
"""
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _img(path: Path, color=(80, 120, 200)):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (48, 36), color).save(path)
    return path


@pytest.fixture()
def db(tmp_path):
    from media_resource_manager.db import Database
    d = Database(tmp_path / "mrm.db")
    yield d
    d.close()


@pytest.fixture()
def cfg(tmp_path):
    from media_resource_manager.config import AppConfig
    c = AppConfig()
    c.db_path = tmp_path / "mrm.db"
    c.ai_model_path = ""
    c.ai_labels = ["person", "sky"]
    c.ai_threshold = 0.5
    c.ai_gpu = False
    c.ai_threads = 2
    return c


@pytest.fixture()
def fake_clf(monkeypatch):
    from media_resource_manager.classify import clip_model

    class _Fake:
        backend = "clip"
        model_note = ""
        calls = 0

        def __init__(self, labels=None, model_dir=None, threads=0, gpu=True):
            self.labels = list(labels or [])

        def classify(self, path):
            _Fake.calls += 1
            return [("person", 0.9), ("sky", 0.2)]

    monkeypatch.setattr(clip_model, "LocalClassifier", _Fake)
    return _Fake


def _seed(db, cfg, tmp_path, n=6):
    """先入库（不识别），造出 n 张 ai_state=0 的图。"""
    from media_resource_manager import import_folder as imp
    lib = tmp_path / "lib"
    for i in range(n):
        _img(lib / f"{i}.jpg", color=(30 + i * 7, 90, 160))
    r = imp.import_folder(db, cfg, str(lib), do_tag=False, mark_pending=True)
    assert r["ok"] and r["indexed"] == n
    return r


# ── ① 独立识别作业能跑完，进度是识别自己的口径 ──────────────
def test_tag_job_runs_independently(db, cfg, tmp_path, fake_clf):
    from media_resource_manager import tag_job
    _seed(db, cfg, tmp_path, 6)
    progress = []
    res = tag_job.start_tagging(
        db, cfg, progress=lambda *a: progress.append(a))
    assert res["ok"] and res["tagged"] == 6, res
    assert res["pending"] == 0
    # 进度 phase 恒为 tag，pct 是识别自己的口径（走到 100）
    assert progress and all(p[0] == "tag" for p in progress)
    assert progress[-1][5] == 100, f"识别进度没到100：{progress[-1]}"
    assert tag_job.count_pending(db) == 0


# ── ② 识别与导入互不挡锁（可同时跑） ─────────────────────────
def test_import_and_tag_locks_are_independent(db, cfg, tmp_path, fake_clf):
    from media_resource_manager import import_folder as imp, tag_job
    lib = tmp_path / "lib2"
    for i in range(4):
        _img(lib / f"{i}.jpg", color=(20 + i * 9, 70, 180))
    # 导入锁被占住时，识别锁仍能拿到（模拟"导入中也能启动识别"）
    assert imp._RUNNING.acquire(blocking=False)
    try:
        got = tag_job._TAG_RUNNING.acquire(blocking=False)
        assert got, "识别锁不该被导入锁挡住"
        tag_job._TAG_RUNNING.release()
    finally:
        imp._RUNNING.release()


# ── ③ 边导入边识别：捞不到行时会等，不提前收工 ────────────────
def test_tag_waits_for_import(db, cfg, tmp_path, fake_clf):
    from media_resource_manager import tag_job
    # 此刻库里还没有任何图（导入还没产出）
    importing = {"v": True}
    result = {}

    def _later_seed():
        time.sleep(1.2)                     # 识别作业起来 1.2 秒后才入库
        _seed(db, cfg, tmp_path, 3)

    t = threading.Thread(target=_later_seed)
    t.start()
    res = tag_job.start_tagging(
        db, cfg, stop=lambda: False,
        import_running=lambda: importing["v"], wait_idle_rounds=5)
    importing["v"] = False
    t.join()
    assert res["ok"] and res["tagged"] == 3, \
        f"识别没等到导入产出就收工了：{res}"


# ── ④ 停止信号能被识别作业响应 ────────────────────────────────
def test_tag_stop_signal(db, cfg, tmp_path, fake_clf):
    from media_resource_manager import tag_job
    _seed(db, cfg, tmp_path, 20)
    res = tag_job.start_tagging(db, cfg, stop=lambda: True)
    assert res["ok"]
    assert res["stopped"] is True
    assert res["tagged"] == 0, "停止后不该还打了标"


# ── ⑤ 接口/路由都在 ────────────────────────────────────────────
def test_tag_endpoints_routed():
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    for ep in ["/api/import/tag-start", "/api/import/tag-cancel",
               "/api/import/tag-job", "/api/import/tag-pending"]:
        assert ep in src, f"缺识别作业接口 {ep}"
    for fn in ["def _api_tag_start", "def _api_tag_cancel",
               "def _api_tag_job", "def _api_tag_pending", "def _start_tag_job"]:
        assert fn in src, f"缺方法 {fn}"


def test_version_bumped():
    import re
    from media_resource_manager import VERSION
    m = re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 60, f"版本回退：{VERSION}"
