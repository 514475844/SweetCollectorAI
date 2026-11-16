"""R152-D1 护栏：无模型（stub）时排除词分类静默误判。

根因：engine.py 建 _excl_clf 时未做 backend 检查（_clf 在 :511 有，_excl_clf 没有）。
LocalClassifier 在 CLIP 不可用时降级为 stub，其 _classify_stub 无视传入的 labels，
改为读取图片同名 .json 边车的标签并全打 1.0 —— 于是带边车的图（标签与排除词
无关）也会被 _ai_excluded 判成 drop/送审，造成静默误判。

修复：建完 _excl_clf 后镜像 _clf 的 backend 检查，非 clip 置 None 跳过排除词分类。
"""
import json
import types
from pathlib import Path

from media_resource_manager.classify.clip_model import LocalClassifier
from media_resource_manager.engine import Engine


def _make_engine(excl_clf):
    # 跳过 Engine 重型 __init__，仅装配 _ai_excluded 所需的极小状态。
    eng = object.__new__(Engine)
    eng._excl_clf = excl_clf
    eng._clf = None
    eng.cfg = types.SimpleNamespace(review_margin=0.12)
    return eng


def _stub_classifier(monkeypatch):
    # 短路后端选择为 stub，避免真实加载 CLIP 模型（重，且会污染同进程内
    # 后续 QtWebEngine 的 GPU 上下文，导致别的测试 flaky）。
    def _fake_pick(self):
        self._backend = "stub"
    monkeypatch.setattr(LocalClassifier, "_pick_backend", _fake_pick)
    # labels 传入排除词，但 stub 会无视它们、改读边车
    return LocalClassifier(labels=["real_excl_word"], model_dir=Path("/nonexistent"))


def test_d1_stub_classifier_reads_sidecar_all_one(monkeypatch, tmp_path):
    """根因复现：stub 无视传入 labels，把图片边车标签全打 1.0。"""
    clf = _stub_classifier(monkeypatch)
    img = tmp_path / "photo.jpg"
    img.write_text("x")
    (tmp_path / "photo.jpg.json").write_text(
        json.dumps({"tags": ["cat", "sunset"]}))
    scores = dict(clf.classify(img))
    # 边车里的无关标签被全 1.0；传入的排除词反而不在结果里
    assert scores.get("cat") == 1.0 and scores.get("sunset") == 1.0, scores
    assert "real_excl_word" not in scores


def test_d1_bug_repro_stub_excl_false_positive(monkeypatch, tmp_path):
    """误判复现：带边车的图（标签与排除词无关）也被 _ai_excluded 送审/隔离。"""
    clf = _stub_classifier(monkeypatch)
    img = tmp_path / "photo.jpg"
    img.write_text("x")
    (tmp_path / "photo.jpg.json").write_text(
        json.dumps({"tags": ["cat", "sunset"]}))
    task = types.SimpleNamespace(ai_threshold=0.5, ai_exclude_match_mode="any")
    decision, info = _make_engine(clf)._ai_excluded(task, str(img))
    assert decision in ("drop", "review"), (decision, info)


def test_d1_fix_excl_none_passes():
    """修复后路径：_excl_clf 为 None（降级跳过排除词分类）→ 不误判。"""
    task = types.SimpleNamespace(ai_threshold=0.5, ai_exclude_match_mode="any")
    decision, info = _make_engine(None)._ai_excluded(task, "x.jpg")
    assert decision == "pass", (decision, info)


def test_d1_source_guard_fix_present():
    """源码护栏：engine.py 的 _excl_clf 建造处必须包含非 clip 跳过逻辑。"""
    src = Path("media_resource_manager/engine.py").read_text(encoding="utf-8")
    idx = src.find("self._excl_clf = LocalClassifier(")
    assert idx != -1, "找不到 _excl_clf 建造点"
    block = src[idx: idx + 1000]
    assert 'getattr(self._excl_clf, "backend", "") != "clip"' in block, "缺少 backend 检查"
    assert "self._excl_clf = None" in block, "非 clip 时未置 None"
    assert "排除词分类已跳过" in block, "缺少跳过文案"
