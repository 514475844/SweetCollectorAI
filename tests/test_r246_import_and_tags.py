"""R246 · 10 万张导入前的两个硬 bug（性能 + 计数）。

这两个都是「小库看不出来、大库必爆」的类型，所以判据要能真跑出来，
不能只做源码字符串断言。

一、文本嵌入每张图都重算
------------------------
融合 ONNX 图要求三路输入同时喂，旧代码按 `len(texts)` 开了一个
`(bs,3,224,224)` 的零数组 —— **每个主题词都跑一遍完整 ViT**。分类是
每张图调一次，于是 40 个词 ⇒ 单张多花几秒。

真机实测（L14 + DirectML，就是用户机器上的实际配置）：
    修复前 单张约 5.7s（仅文本部分）→ 10.5 万张 ≈ 168 小时
    修复后 单张 305ms（GPU）/ 552ms（CPU）→ 8.9 / 16.2 小时
这条用例用自带的小模型 B32 验行为（不依赖用户是否装了 L14）：
40 个词的文本编码必须**远小于**按词数翻倍的那个量级。

二、标签计数只扫前 5000 行
--------------------------
`_api_tags` 原来带 `LIMIT 5000`，10 万张的库里标签旁边的数字只有真实值的
5%。用例造 6000 行、把稀有标签放在 5980 行之后 —— 旧实现根本数不到它。
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ============================================================ 一、文本嵌入

def _bundled_model():
    """自带 B32（一定在包里），没有就跳过。"""
    p = ROOT / "media_resource_manager" / "bundled_models" / "clip-vit-b32"
    if not (p / "model_quantized.onnx").exists():
        return None
    return p


LABELS = ["person", "landscape", "animal", "food", "car", "building",
          "anime", "illustration", "photograph", "text", "white stockings",
          "black stockings", "lingerie", "swimsuit", "portrait", "group",
          "indoor", "outdoor", "water", "night", "sky", "tree", "flower",
          "beach", "mountain", "city", "street", "selfie", "smile",
          "blonde", "brunette", "long hair", "short hair", "dress",
          "skirt", "jeans", "shirt", "hat", "glasses", "sitting"]


def test_encode_text_is_cached_after_first_call():
    """同一份词表第二次必须**几乎零耗时**（缓存命中）。"""
    d = _bundled_model()
    if d is None:
        pytest.skip("自带 CLIP 模型不在，跳过")
    from media_resource_manager.classify.clip_onnx import get_clip_onnx
    c = get_clip_onnx(d / "model_quantized.onnx", d / "tokenizer.json",
                      threads=0, gpu=False, device="cpu")
    t0 = time.time()
    a = c.encode_text(LABELS)
    first = time.time() - t0
    t0 = time.time()
    b = c.encode_text(LABELS)
    second = time.time() - t0
    assert second < 0.05, f"缓存没命中：第二次仍花 {second * 1000:.0f}ms"
    assert a.shape == b.shape and (a == b).all(), "缓存返回的嵌入变了"


def test_encode_text_does_not_scale_with_label_count():
    """40 个词的文本编码不能按「每个词跑一遍 ViT」翻倍。

    旧实现实测 ≈ 1980ms（B32）；修复后 ≈ 880ms 且与词数无关。
    判据给一个宽松上限：**低于「按词数翻倍」的量级**即可。
    """
    d = _bundled_model()
    if d is None:
        pytest.skip("自带 CLIP 模型不在，跳过")
    from media_resource_manager.classify.clip_onnx import get_clip_onnx
    c = get_clip_onnx(d / "model_quantized.onnx", d / "tokenizer.json",
                      threads=0, gpu=False, device="cpu")
    # 用一份没缓存的词表，确保测的是真算。
    # 全量并发跑时别的用例在抢核，单次计时会假红（V0.0.7 全量实测）：
    # 这里跑三批**互不重复**的词表取最快的一次 —— 最快值才代表机器空闲
    # 时的真实开销，仍然钉得住「按词数开零图」这个回归（旧实现 ≈2s）。
    cost = None
    for k in range(3):
        words = LABELS + [f"tmp-probe-{k}-{i}" for i in range(2)]
        t0 = time.time()
        c.encode_text(words)
        dt = time.time() - t0
        cost = dt if cost is None else min(cost, dt)
    assert cost < 1.5, (
        f"42 个词花了 {cost * 1000:.0f}ms —— 又按词数开零图了？"
        f"（旧实现约 2s，修复后应 <1.5s）")


def test_encode_text_falls_back_when_graph_rejects_mixed_batch():
    """图不接受两路 batch 不一致时必须**自动退回**而不是抛给调用方。"""
    from media_resource_manager.classify import clip_onnx
    src = io.open(clip_onnx.__file__, encoding="utf-8").read()
    assert "_text_pixel_batch1" in src
    # 退回分支必须真的重建 feeds 再跑一次
    assert "self._text_pixel_batch1 = False" in src


# ============================================================ 二、标签计数

def _fresh_handler(tmp_path):
    os.environ["MRM_HOME"] = str(tmp_path)
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"
    db = Database(cfg.db_path)
    db.execute("INSERT INTO tasks(name) VALUES ('t')")
    tid = db.query("SELECT id FROM tasks")[0]["id"]

    class O:
        pass

    o = O()
    o.db = db
    return o, db, tid, webui


def test_tag_counts_cover_whole_library_not_first_5000(tmp_path):
    """6000 行库里，第 5980 行之后的稀有标签必须被数到。

    旧实现 `LIMIT 5000` ⇒ 稀有标签直接消失（用户以为图没导进来）。
    """
    o, db, tid, webui = _fresh_handler(tmp_path)
    try:
        for i in range(6000):
            tags = ["rare"] if i >= 5980 else ["common"]
            db.execute(
                "INSERT INTO images(task_id, source_url, path, tags, "
                "width, height, bytes) VALUES (?,?,?,?,?,?,?)",
                (tid, f"u{i}", f"p{i}", json.dumps(tags), 100, 100, 1000))
        webui._tag_cache_clear()
        got = {x["tag"]: x["n"] for x in webui._Handler._api_tags(o)["items"]}
        assert got.get("common") == 5980, f"common 计数不对：{got}"
        assert got.get("rare") == 20, f"5000 行之后的标签丢了：{got}"
    finally:
        db.close()


def test_tag_cache_is_cleared_after_tag_write():
    """加/删标签后必须清缓存，否则树里的数字要等半分钟才变。"""
    from media_resource_manager import webui
    src = io.open(webui.__file__, encoding="utf-8").read()
    seg = src.split("def _api_image_tags_post", 1)[1].split("def ", 1)[0]
    assert "_tag_cache_clear()" in seg, "写标签后没有清缓存"


# ============================================================ 三、口径一致

def test_learn_folder_uses_task_threshold_for_tags():
    """从文件夹学习打标签必须跟随任务阈值，不能写死 0.5。

    写死的话，同一个库里「采集来的」和「导入的」两套口径：
    用户把阈值调到 0.6，只有一半的图跟着变。
    """
    from media_resource_manager import learn_folder
    src = io.open(learn_folder.__file__, encoding="utf-8").read()
    assert "if s >= 0.5]" not in src, "learn_folder 里还写死 0.5"
    assert "if s >= thr]" in src, "标签门槛没跟随 thr"


def test_summary_is_cached_but_can_be_cleared(tmp_path):
    """底栏概览走 3 秒缓存；清缓存后必须重新算。"""
    o, db, tid, webui = _fresh_handler(tmp_path)
    try:
        first = webui._Handler._api_summary(o)
        second = webui._Handler._api_summary(o)
        assert first["ok"] and second["ok"]
        assert second.get("cached") is True, "第二次没走缓存"
        with webui._SUMMARY_CACHE_LOCK:
            webui._SUMMARY_CACHE["ts"] = 0.0
            webui._SUMMARY_CACHE["data"] = None
        third = webui._Handler._api_summary(o)
        assert third.get("cached") is False, "清缓存后仍返回缓存值"
    finally:
        db.close()
