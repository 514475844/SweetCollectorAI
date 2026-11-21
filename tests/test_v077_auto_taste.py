"""V0.0.77 护栏：AI 造词按频率自动入口味库 + 运行中立即生效。

用户定稿：
  · 白名单 = 口味库，是同一个东西；
  · AI 扫出的词按出现频率自动增量加进去（高频权重高 = 核心词），不再手动勾；
  · 用户手填/勾选的词不许被机器覆盖；
  · 重复扫描幂等、单调（不会越扫越虚高，也不压低用户词）；
  · 白/黑名单增量加入后，运行中的任务下一张图就能感知（词表热更新）。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def db(tmp_path):
    from media_resource_manager.db import Database
    home = tmp_path / "home"
    home.mkdir()
    d = Database(home / "mrm.db")
    yield d
    d.close()


def test_freq_weight_monotonic():
    from media_resource_manager.auto_vocab import _freq_weight
    lo = _freq_weight(2, 100, 2)
    mid = _freq_weight(50, 100, 2)
    hi = _freq_weight(100, 100, 2)
    assert lo < mid < hi, "频率越高权重应越高"
    assert lo >= 0.6 and hi <= 3.0, "权重应落在 [0.6, 3.0]"
    # 同一词绝对命中数增长（跨多次扫描），权重也应单调上涨
    assert _freq_weight(100, 100, 2) > _freq_weight(10, 10, 2), \
        "绝对频次增长应能上调权重"


def test_auto_apply_basic_and_freq_rank(db):
    from media_resource_manager.auto_vocab import auto_apply_scan
    from media_resource_manager.repo import get_preferences

    res = {"words": [
        {"word": "高频词", "hits": 50, "samples": []},
        {"word": "中频词", "hits": 20, "samples": []},
        {"word": "噪声词", "hits": 1, "samples": []},   # 只 1 次 → 丢掉
    ]}
    out = auto_apply_scan(db, res)
    assert out["added"] == 2, f"应写入 2 个词，实际 {out}"
    prefs = get_preferences(db)
    assert prefs["高频词"] > prefs["中频词"], "高频词权重应更高"
    assert "噪声词" not in prefs, "只出现 1 次的噪声词不该进口味库"


def test_auto_apply_protects_user_word(db):
    from media_resource_manager.auto_vocab import auto_apply_scan
    from media_resource_manager.repo import (
        PREF_ORIGIN_USER, get_preferences, set_preference)

    # 用户亲手填的"动漫"权重 1.0；AI 也想把它写成高频 2.0
    set_preference(db, 0, "动漫", 1.0, kind="pos", origin=PREF_ORIGIN_USER)
    out = auto_apply_scan(db, {"words": [
        {"word": "动漫", "hits": 99, "samples": []},
        {"word": "新词", "hits": 30, "samples": []},
    ]})
    prefs = get_preferences(db)
    assert out["skipped"] >= 1, "用户手填词应被跳过"
    assert prefs["动漫"] == 1.0, "用户手填词的权重不许被机器改动"
    assert "新词" in prefs, "新词仍应正常写入"


def test_auto_apply_idempotent_and_monotonic(db):
    from media_resource_manager.auto_vocab import auto_apply_scan
    from media_resource_manager.repo import get_preferences

    low = {"words": [{"word": "词甲", "hits": 10, "samples": []},
                     {"word": "词乙", "hits": 5, "samples": []}]}
    auto_apply_scan(db, low)
    w1 = dict(get_preferences(db))
    # 重复扫同样结果：幂等，不重复加
    out2 = auto_apply_scan(db, low)
    w2 = dict(get_preferences(db))
    assert out2["added"] == 0 and w1 == w2, "重复扫描应幂等"

    # 更高频率扫到同一个词：单调上调，不压低其他词
    high = {"words": [{"word": "词甲", "hits": 100, "samples": []}]}
    auto_apply_scan(db, high)
    w3 = get_preferences(db)
    assert w3["词甲"] > w1["词甲"], "更高频应上调权重"
    assert w3["词乙"] == w1["词乙"], "没扫到的词权重不该变"


def test_scan_callback_auto_applies():
    w = (_read("media_resource_manager/webui.py"))
    assert "av.auto_apply_scan(self.db, res)" in w, "扫描完成回调未自动写入口味库"
    assert 'res["applied"]' in w, "扫描结果未带回自动写入统计"


def test_engine_hot_reload_terms():
    e = (_read("media_resource_manager/engine.py"))
    assert "def _taste_version(" in e, "缺口味库指纹函数"
    assert "def _refresh_terms(" in e, "缺运行中词表热更新函数"
    assert "if self._refresh_terms(task):" in e, "_process_image 未接热更新"
    assert "self._taste_ver = None" in e, "缺初始版本记录"


def test_clip_runtime_fallback_locked_and_present():
    """V0.0.77 真机：量化 CLIP 在 DirectML 上建会话成功、每张图执行才报
    MatMulIntegerToFloat 8007000E —— 旧包扫描 18 张全 0 词。必须运行时降级。"""
    s = _read("media_resource_manager/classify/clip_onnx.py")
    assert "def _run(" in s, "缺统一的 sess.run 入口"
    assert "运行时失败" in s and "CPUExecutionProvider" in s, "缺运行时降级逻辑"
    assert "永久降级 CPU" in s, "缺降级日志"
    # 三处推理必须都走 _run，不许残留直接 self.sess.run 在外面
    assert s.count("self.sess.run(None, feeds)") == 2, \
        "sess.run 只应出现在 _run 内部（正常 + 降级重跑各一次）"


def test_clip_runtime_fallback_switches_session():
    """真正模拟：GPU 会话每张图抛错 → _run 一次性重建 CPU 会话并跑通。"""
    from media_resource_manager.classify import clip_onnx

    c = clip_onnx.ClipONNX.__new__(clip_onnx.ClipONNX)
    c._run_lock = __import__("threading").Lock()
    c.provider = "DmlExecutionProvider"
    c._model_file = "dummy.onnx"
    c._so = None

    import onnxruntime as ort
    c._ort = ort

    calls = {"n": 0}

    class _FailingSess:
        def run(self, _out, feeds):
            calls["n"] += 1
            raise RuntimeError("MatMulIntegerToFloat 8007000E")

    c.sess = _FailingSess()

    def fake_build(model_file, so, providers):
        # 降级时重建的 CPU 会话：能跑通
        class _Ok:
            def run(self, _out, feeds):
                calls["n"] += 1
                return ["ok-from-cpu"]
        return _Ok()

    ort.InferenceSession = fake_build   # type: ignore[assignment]
    out = c._run({"x": 1})
    assert out == ["ok-from-cpu"], "运行时失败应降级 CPU 并返回结果"
    assert c.provider == "CPUExecutionProvider", "provider 应切到 CPU"
    # 第二张不再抛错（会话已换），也不该再重建
    calls["n"] = 0
    ort.InferenceSession = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
        AssertionError("不应再重建会话"))
    assert c._run({"x": 2}) == ["ok-from-cpu"]


def _read(rel):
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
