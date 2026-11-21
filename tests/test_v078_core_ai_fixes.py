"""V0.0.78 护栏：审查发现的 AI 核心模块真 bug。

  · set_preference 是累加写入，但勾选/自动入库当成覆盖式 → 重复调用权重爆冲；
  · 只有一个词达标时 _freq_weight 给满档 → 单次孤词被当核心词；
  · scan_api 不接受 stop_check、早停还丢弃已扫出的词；
  · EN2ZH 未拆分 "a|b" 多候选值 → 反查永远落空；
  · clip_onnx._run 把 batch 异常也当后端故障 → 误降级污染缓存会话。
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


# ── 权重设值（不是累加）────────────────────────────────────────────────
def test_apply_choices_idempotent(db):
    from media_resource_manager.auto_vocab import apply_choices
    from media_resource_manager.repo import get_preferences

    apply_choices(db, ["喜欢词"], ["讨厌词"])
    w1 = dict(get_preferences(db))
    apply_choices(db, ["喜欢词"], ["讨厌词"])   # 重复勾同样词
    w2 = dict(get_preferences(db))
    assert w1 == w2, f"重复勾选不应累加权重：{w1} → {w2}"
    assert abs(w2["喜欢词"] - 2.0) < 1e-6
    assert abs(w2["讨厌词"] + 2.0) < 1e-6


def test_auto_apply_repeat_no_inflation(db):
    """回归真机隐患：同一批词反复扫，权重不能 2→4→6 爆冲。"""
    from media_resource_manager.auto_vocab import auto_apply_scan
    from media_resource_manager.repo import get_preferences

    res = {"words": [{"word": "重复词", "hits": 80, "samples": []}]}
    auto_apply_scan(db, res)
    w1 = dict(get_preferences(db))
    for _ in range(5):
        auto_apply_scan(db, res)
    w2 = dict(get_preferences(db))
    assert w1 == w2, f"反复扫描不应累加：{w1} → {w2}"
    assert w2["重复词"] <= 3.0, "不该突破自动入库权重上限"


def test_auto_apply_upgrade_lands_exact_target(db):
    """上调要精确落到目标权重，不能在旧值上叠加。"""
    from media_resource_manager.auto_vocab import auto_apply_scan, _freq_weight
    from media_resource_manager.repo import get_preferences

    auto_apply_scan(db, {"words": [{"word": "渐变词", "hits": 10, "samples": []}]})
    before = get_preferences(db)["渐变词"]
    target = _freq_weight(90, 90, 2)
    auto_apply_scan(db, {"words": [{"word": "渐变词", "hits": 90, "samples": []}]})
    after = get_preferences(db)["渐变词"]
    assert after > before
    assert abs(after - target) < 1e-6, f"上调后应恰为目标 {target}，实际 {after}"


def test_freq_weight_single_word_not_maxed():
    from media_resource_manager.auto_vocab import _freq_weight
    # 只有一个低频词达标：不该给它满档核心词的待遇
    single = _freq_weight(2, 2, 2)
    top = _freq_weight(100, 100, 2)
    assert single < top
    assert single < 1.0, f"孤词权重应落在细分档，实际 {single}"


# ── scan_api 停止 / 落盘 ───────────────────────────────────────────────
def test_scan_api_signature_has_stop_check():
    import inspect
    from media_resource_manager import auto_vocab as av
    assert "stop_check" in inspect.signature(av.scan_api).parameters
    src = inspect.getsource(av.scan)
    assert "scan_api(db, cfg, amount, progress, stop_check)" in src, \
        "scan 分派未把 stop_check 透传给 scan_api"


def test_scan_api_honors_stop_check(db):
    """停止信号在循环顶部被检查，第一张前就停下。"""
    from media_resource_manager import auto_vocab as av
    from media_resource_manager.config import AppConfig
    from pathlib import Path

    home = Path(str(db.db_path)).parent
    (img_dir := home / "imgs").mkdir(exist_ok=True)
    for i in range(4):
        p = img_dir / f"{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff")
        db.execute("INSERT INTO images(task_id, source_url, path, "
                   "in_quarantine, blacklisted) VALUES (0,?,?,0,0)",
                   (f"http://x/{i}.jpg", str(p)))

    cfg = AppConfig()
    cfg.ai_api_url = "http://127.0.0.1:9/v1"
    cfg.ai_api_key = "x"
    cfg.ai_api_model = "stub"

    out = av.scan_api(db, cfg, "4", lambda p, m: None, lambda: True)
    assert out.get("stopped") is True, f"应尊重停止信号：{out}"
    assert out["scanned"] == 0, "停止信号应在第一张之前生效"


def test_scan_api_keeps_partial_on_early_stop():
    """早停不能再 return 空——已扫词要保留并落盘。"""
    import inspect
    from media_resource_manager import auto_vocab as av
    src = inspect.getsource(av.scan_api)
    assert "break                # 不再直接 return" in src, \
        "接口连续失败早停不该直接返回空结果"
    assert "save_last(db, dict(out, ts=time.time()))" in src, \
        "scan_api 结束应落盘成果"
    assert '"stopped": stopped' in src, "应带回停止标记"


# ── EN2ZH 多候选 ───────────────────────────────────────────────────────
def test_en2zh_splits_multi_candidates():
    from media_resource_manager.classify.labels_zh import EN2ZH, ZH2EN
    for zh, en in ZH2EN.items():
        for one in str(en).split("|"):
            assert one.strip().lower() in EN2ZH, \
                f"{en} 拆分后 {one} 应能反查到 {zh}"
    assert "festival" in EN2ZH and "holiday" in EN2ZH


# ── clip 降级只针对后端错误 ────────────────────────────────────────────
def test_clip_backend_error_discrimination():
    from media_resource_manager.classify.clip_onnx import ClipONNX
    is_err = ClipONNX._is_backend_error
    assert is_err(RuntimeError("MatMulIntegerToFloat 8007000E")) is True
    assert is_err(RuntimeError("DML provider not supported")) is True
    # batch/shape 类问题不该触发永久降级
    assert is_err(ValueError("shape mismatch: got [1,3,224,224]")) is False
    assert is_err(RuntimeError("input batch size differs")) is False
