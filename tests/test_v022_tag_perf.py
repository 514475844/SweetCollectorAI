"""V0.0.22：打标提速 + 不再卡界面。

真机现象（2026-11-13）：导入 10 万张的图库，打标只有 100 张/分钟，
而且软件网页接口能卡到 45 秒超时。根因是老写法把「AI 推理」整段塞进
``db.transaction()`` 里 —— 事务期间软件那条共用 SQLite 连接被写锁占着，
网页查询全排队；界面线程的长查询又反过来把打标拖慢。

现口径（这三条就是本文件的护栏）：
  ① 推理在事务**外**做，且多线程并行；
  ② 写回只占一次短事务（一批一次），进度回调也改成一批一次；
  ③ 停止请求在两个批次之间生效（仍可断点续打）。
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager" / "import_folder.py"


def _img(path: Path, w: int = 64, h: int = 48, color=None):
    from PIL import Image
    if color is None:
        n = abs(hash(str(path)))
        color = (60 + n % 60, 40 + (n // 7) % 60, 40 + (n // 11) % 60)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), color).save(path)
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
    c.ai_labels = ["person"]
    c.ai_threshold = 0.5
    c.ai_gpu = False
    return c


@pytest.fixture()
def fake_clf(monkeypatch):
    from media_resource_manager.classify import clip_model

    class _Fake:
        backend = "clip"
        model_note = ""
        calls = 0
        threads = []

        def __init__(self, labels=None, model_dir=None, threads=0, gpu=True):
            self.labels = list(labels or [])
            _Fake.threads.append(threads)

        def classify(self, path):
            _Fake.calls += 1
            return [("person", 0.9), ("sky", 0.2)]

    monkeypatch.setattr(clip_model, "LocalClassifier", _Fake)
    return _Fake


def _tag_stage_src() -> str:
    src = SRC.read_text(encoding="utf-8")
    i = src.index("def _tag_stage")
    return src[i:]


def test_inference_runs_outside_transaction():
    """推理必须在事务外：先并行算完，再一次性写回。"""
    body = _tag_stage_src()
    assert "ThreadPoolExecutor" in body, "打标没走多线程"
    assert "ex.map" in body or ".map(_tag_one" in body, "并行没接上"
    i_pool = body.index("ThreadPoolExecutor")
    i_tx = body.index("with db.transaction():")
    assert i_pool < i_tx, "事务在推理之前开，说明推理又回到事务里了"
    # 事务块内不许再出现分类调用
    tx_block = body[i_tx:]
    assert "classify(" not in tx_block.split("stats[")[0], \
        "事务里还在做推理 —— 界面会被这把锁卡死"


def test_classify_threads_divided_by_workers():
    """并发 × 单次线程数 超过物理核会互相抢，构造分类器时要把线程数除下来。"""
    body = _tag_stage_src()
    assert "_workers_hint" in body, "没按并发数收敛单次推理线程数"


def test_parallel_loop_tags_all_rows(db, cfg, fake_clf, tmp_path):
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    for i in range(7):
        _img(lib / f"{i}.jpg")
    r = imp.import_folder(db, cfg, str(lib), do_tag=True)
    assert r["ok"] and r["tagged"] == 7
    rows = db.query("SELECT tags, ai_state, score FROM images")
    assert all(x["ai_state"] == 1 for x in rows)
    for x in rows:
        assert json.loads(x["tags"]) == ["person"], x["tags"]
        assert abs(float(x["score"]) - 0.9) < 1e-6


def test_progress_is_per_batch_not_per_image(db, cfg, fake_clf, tmp_path):
    """一图一次进度回调在 10 万张时纯属浪费；改成一批一次。"""
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    for i in range(120):
        # 尺寸按 i 变，避免 8×8 小图被 JPEG 压成同一份而按 sha256 去重
        _img(lib / f"{i}.jpg", w=16 + i % 30, h=16 + i // 30)
    calls = []

    def prog(phase, done, total, name, eta, pct):
        if phase == "tag":
            calls.append(done)

    r = imp.import_folder(db, cfg, str(lib), do_tag=True, progress=prog)
    total_rows = db.query("SELECT COUNT(*) c FROM images")[0]["c"]
    assert r["tagged"] == total_rows == 120
    # 120 张 / 每批 50 = 3 批 + 收尾 1 次
    assert len(calls) <= 6, f"进度回调太频繁：{len(calls)} 次（应为按批）"


def test_stop_is_honored_between_batches(db, cfg, fake_clf, tmp_path):
    from media_resource_manager import import_folder as imp

    lib = tmp_path / "lib"
    for i in range(160):
        _img(lib / f"{i}.jpg", w=8, h=8)
    state = {"batches": 0}

    def prog(phase, done, total, name, eta, pct):
        if phase == "tag":
            state["batches"] += 1

    def stop():
        return state["batches"] >= 1

    r = imp.import_folder(db, cfg, str(lib), do_tag=True, progress=prog, stop=stop)
    assert r["stopped"] is True
    assert r["tagged"] < 160, "停止没生效，整库都打完了"
    left = db.query("SELECT COUNT(*) c FROM images WHERE ai_state=0")[0]["c"]
    assert left > 0, "停止后应留下可续打的待办行"
