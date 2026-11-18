# -*- coding: utf-8 -*-
"""V0.0.61 护栏：DirectML 会话并发崩溃修复 + AI 识别卡挪到图库页。

背景（用户实测闪退）：
  桌面机用 clip-vit-l14（432MB，DirectML 后端），识别流水线开多个 worker
  并发对同一个共享 InferenceSession 调 sess.run()，onnxruntime-directml
  原生层访问违规（0xc0000005），整个进程被杀死。复现：5 worker 并发必崩。

本文件钉住：
  ① clip_onnx.py 的 encode_image / encode_text 里所有 sess.run() 都持
     _run_lock（串行化）；
  ② clip_model.py 的 _classify_onnx 回退路径同样持锁；
  ③ 会话创建带互斥锁字段。
  ④ AI 识别卡（TagCard）在图库页，导入页不再渲染识别卡
     （识别是对已入库图片做的，放导入页语义错位）。
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── ① clip_onnx：sess.run 必须持 _run_lock ────────────────────────
def test_clip_onnx_run_lock_exists():
    s = _read("media_resource_manager/classify/clip_onnx.py")
    assert "self._run_lock = threading.Lock()" in s, "ClipONNX 缺少会话推理互斥锁"


def test_clip_onnx_all_run_calls_locked():
    s = _read("media_resource_manager/classify/clip_onnx.py")
    # 所有 sess.run 出现处都必须在 with self._run_lock 块内。
    # 逐行检查：出现 sess.run 的行，其前文最近一个 with 必须是 _run_lock。
    import re
    lines = s.splitlines()
    run_lines = [i for i, l in enumerate(lines) if "self.sess.run(" in l]
    assert run_lines, "找不到 sess.run 调用"
    for i in run_lines:
        # 向上找最近的非空 with 行
        for j in range(i, -1, -1):
            st = lines[j].strip()
            if st.startswith("with "):
                assert "_run_lock" in st, (
                    f"clip_onnx.py L{i+1} 的 sess.run 没有持 _run_lock："
                    f"上一行是 {lines[j].strip()}")
                break
        else:
            raise AssertionError(f"clip_onnx.py L{i+1} 的 sess.run 上方找不到 with 块")


# ── ② clip_model：onnx 回退路径也持锁 ─────────────────────────────
def test_clip_model_onnx_run_locked():
    s = _read("media_resource_manager/classify/clip_model.py")
    assert "self._onnx_run_lock = threading.Lock()" in s, "LocalClassifier 缺少 onnx 会话锁"
    import re
    lines = s.splitlines()
    run_lines = [i for i, l in enumerate(lines) if "self._session.run(" in l]
    assert run_lines, "找不到 _session.run 调用"
    for i in run_lines:
        for j in range(i, -1, -1):
            st = lines[j].strip()
            if st.startswith("with "):
                assert "_onnx_run_lock" in st, (
                    f"clip_model.py L{i+1} 的 _session.run 没有持 _onnx_run_lock")
                break
        else:
            raise AssertionError(f"clip_model.py L{i+1} 的 _session.run 上方找不到 with 块")


# ── ③ 识别卡位置：图库页有 TagCard，导入页没有识别卡 ───────────────
def test_tagcard_in_gallery_not_import():
    gallery = _read("media_resource_manager/webapp/src/views/Gallery.vue")
    assert "TagCard" in gallery, "图库页必须渲染 TagCard（AI 识别卡）"

    imp = _read("media_resource_manager/webapp/src/components/ImportFolder.vue")
    # 导入页不得再渲染识别卡（tagImp 相关模板与脚本都已挪走）
    assert "tagcard" not in imp, "导入页不应再渲染 AI 识别卡"
    assert "tagImp" not in imp, "导入页不应再引用 tagImp"
    assert "TagCard" not in imp, "导入页不应再引用 TagCard"


def test_tagcard_component_exists_and_uses_importjob():
    tag = _read("media_resource_manager/webapp/src/components/TagCard.vue")
    assert "from '../importJob'" in tag, "TagCard 必须复用 importJob 的识别作业状态"
    assert "startTagging" in tag and "cancelTagging" in tag, "TagCard 必须有启停能力"
