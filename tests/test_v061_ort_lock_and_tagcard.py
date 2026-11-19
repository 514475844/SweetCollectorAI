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


# ── ③ 识别控件位置：在图库工具栏白框里，导入页没有 ───────────────
def test_tagcard_in_gallery_not_import():
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "tagctl" in grid, "图库工具栏必须带 AI 识别控件（V0.0.62 并入白框）"
    gallery = _read("media_resource_manager/webapp/src/views/Gallery.vue")
    assert "TagCard" not in gallery, "独立识别卡已并入工具栏，图库页不再挂 TagCard"
    assert "impnote" not in gallery, "「请到导入与导出页导入」指引句已按用户要求删除"

    imp = _read("media_resource_manager/webapp/src/components/ImportFolder.vue")
    # 导入页不得渲染识别控件（tagImp 相关模板与脚本都已挪走）
    assert "tagImp" not in imp, "导入页不应再引用 tagImp"
    assert "TagCard" not in imp, "导入页不应再引用 TagCard"

def test_tagcard_component_removed():
    tag = ROOT / "media_resource_manager/webapp/src/components/TagCard.vue"
    assert not tag.exists(), "TagCard.vue 已并入 ImageGrid 工具栏，应删除"

def test_no_startup_note_copy():
    # 用户点名删除的说明句，任何页面都不许再出现
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "识别可以随时启停" not in grid


# ── ⑤ V0.0.62：识别控件在工具栏里、说明文字删净、导入页不越权 ────────
def test_toolbar_has_tag_controls_only_no_note():
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "识别随时" not in grid and "识别可以随时启停，不影响导入" not in grid
    # 控件三要素：AI 识别字样、张数、启动按钮
    assert "AI 识别" in grid and "还有" in grid and "识别这" in grid

def test_import_result_card_links_to_gallery_not_retag():
    imp = _read("media_resource_manager/webapp/src/components/ImportFolder.vue")
    assert "retagLeft" not in imp, "导入结果卡不许再起识别作业（统一归图库控制）"
    assert "继续识别剩下的" not in imp

def test_narrow_window_css_present():
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "@media (max-width: 1120px)" in grid, "窄窗（最小 1024）自适应规则缺失"
    assert ".bt { display: none" in grid or ".bt {" in grid, "窄窗下动作按钮文字未收进图标"
