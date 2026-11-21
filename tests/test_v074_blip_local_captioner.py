"""V0.0.74 护栏：本地描述模型（BLIP）真正接入「AI 自己造词」。

用户拍板：「本地扫描生词的模型现在开始接入。不用管我能不能跑得动，
最起码得保证功能是正常的——哪怕 10 分钟蹦一个词，也证明功能正常，
只是我电脑带不动。」

钉住这些要点：
- models_lib 清单里有 BLIP 描述模型条目（repo/files 齐全，能走现成下载通道）
- blip_caption.py：纯 onnxruntime 推理（无 torch）、短语+停用词拆词、
  中文反查（PHRASE_ZH/EN2ZH），签名兼容不同命名导出
- auto_vocab.scan_local 真正调描述模型逐图造词，未下载时给可操作提示
- auto_vocab.scan_api 实装（OpenAI 兼容通道 + 连续失败早停）
- 前端 local 档解除禁用
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_blip_in_model_catalog():
    m = _read("media_resource_manager/models_lib.py")
    assert "blip-caption-base" in m, "模型清单没有 BLIP 描述模型条目"
    assert "ningpp/blip-image-captioning-base-ONNX" in m, "repo 指向不存在的仓库"
    assert "blip_vision_encoder.onnx" in m and "blip_text_decoder.onnx" in m, \
        "下载文件清单缺双 onnx"
    assert "看图说话" in m or "造词" in m, "清单里没有面向用户的中文说明"


def test_blip_caption_pure_onnxruntime():
    c = _read("media_resource_manager/classify/blip_caption.py")
    assert "onnxruntime" in c, "推理不是纯 onnxruntime（打包环境没有 torch）"
    assert "import torch" not in c, "混进了 torch 依赖"
    assert "DmlExecutionProvider" in c, "没走 DirectML 优先的既有约定"
    assert "def caption" in c and "def extract_words" in c
    assert "PHRASE_ZH" in c, "缺短语→中文映射"
    # 停用词表要盖住 BLIP 套话（实测虚词）
    for junk in ("camera", "posing", "wearing"):
        assert junk in c, f"停用词缺 BLIP 套话 {junk}"


def test_blip_locate_tolerates_naming():
    c = _read("media_resource_manager/classify/blip_caption.py")
    assert "def _locate" in c, "缺文件名自适应（不同导出命名不一）"
    assert "model_ready" in c, "缺可用性探测"


def test_scan_local_real():
    a = _read("media_resource_manager/auto_vocab.py")
    seg = a[a.index("def scan_local"):]
    seg = seg[:seg.index("def scan_api")]
    assert "BlipCaptioner" in seg, "scan_local 没接描述模型"
    assert "extract_words" in seg, "scan_local 没做拆词聚合"
    assert "_blip_model_dir" in seg, "scan_local 没定位模型目录"
    assert "去「AI 模型」页" in seg or "去『AI 模型』页" in seg or "下载" in seg, \
        "未下载时的提示不可操作"
    assert "% 5 == 0" in seg, "BLIP 慢，进度上报应比 vocab 勤"


def test_scan_api_real():
    a = _read("media_resource_manager/auto_vocab.py")
    seg = a[a.index("def scan_api"):]
    seg = seg[:seg.index("\ndef ", seg.index("def scan_api") + 5)]
    assert "chat/completions" in seg, "scan_api 没接 OpenAI 兼容通道"
    assert "fail_streak" in seg, "缺连续失败早停（防烧钱）"
    assert "image_url" in seg, "没把图发给模型"


def test_frontend_local_enabled():
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    line = [x for x in c.splitlines() if "name: '本地描述模型'" in x][0]
    assert "disabled: true" not in line, "local 档还挂着禁用"
    assert "暂不可用" not in line, "local 档还标着暂不可用"
    assert "BLIP" in c, "前端没告诉用户要下哪个模型"


def test_version_and_changelog():
    import re
    v = _read("media_resource_manager/__init__.py")
    m = re.search(r'VERSION = "0\.0\.(\d+)"', v)
    assert m and int(m.group(1)) >= 74, "版本号未升到 0.0.74"
    ch = _read("CHANGELOG.md")
    assert "V0.0.74" in ch, "更新日志缺 V0.0.74"
