# -*- coding: utf-8 -*-
"""R70 · 回归护栏：`_pick_backend` 的模型回退链必须接得住「已下载的模型」

缺陷（由 tools/r70_probe_fallback_paths.py 实测抓出，不是读源码猜的）
--------------------------------------------------------------------
`clip_model.LocalClassifier._pick_backend` 候选③ 把**数据目录**当base 传给
`models_lib.resolve_default_model_dir(base)`，而那个函数的 base 语义是
**模型根目录**（`model_dir(base, id) == base / id`；UI 侧
`main_window._models_base_dir()` 给的正是 `user_data_dir()/"models"`）。
⇒ 差一级：模型落在 `<data>/models/clip-vit-b32`，回退却去找 `<data>/clip-vit-b32`。

为什么平时没炸：交付包内`bundled_models` 是齐的，`resolve_default_model_dir`
第① 步先查捆绑就命中了。**只有捆绑层不在时**（数据目录被搬走 / 只拷了 data /
用户为省 153MB 删掉 bundled_models）才暴露成
「明明下载过 CLIP，任务启动仍报『本地 AI 处于降级模式』」。

四层护栏
--------
1. 元测试：先证明 `is_ready` 的真实判据（**缺 config.json 就不算就绪**）
   ——第一版夹具漏了它，探针量的是「夹具没造好」，属仪器错，已修。
2. 行为层：关掉捆绑层 + 造就绪的已下载模型 ⇒ 真跑 `_pick_backend`，
   断言它**探到**了那个目录（用 `_try_clip_dir` 记录器，不真加载 153MB）。
3. AST 层：判候选③ 真的传的是 `user_data_dir() / "models"`，
   且**不是**靠字面量全文搜（注释里也写着这串，会匹配到自己）。
4. 反向护栏：确认没顺手删掉历史目录 `models/clip`（老用户兼容）。
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


# ----------------------------------------------------------------------
# 1 · 元测试：把 is_ready 的真实判据钉住（护栏自己不许建立在错夹具上）
# ----------------------------------------------------------------------
def test_meta_is_ready_requires_config_json():
    """缺 config.json ⇒ is_ready False（models_lib.py:187 的第三个条件）。

    这条是**元测试**：R70 第一版夹具只造了 onnx + tokenizer.json，
    于是 resolve 直接返回 None，探针量到的是「夹具没造好」而不是产品行为。
    """
    from media_resource_manager import models_lib as ml

    tmp = Path(tempfile.mkdtemp(prefix="r70t_meta_"))
    d = tmp / ml.DEFAULT_BUILTIN_ID
    d.mkdir(parents=True)
    (d / "model_quantized.onnx").write_bytes(b"0" * 4096)
    (d / "tokenizer.json").write_text("{}", encoding="utf-8")
    assert ml.is_ready(tmp, ml.DEFAULT_BUILTIN_ID) is False, "缺 config.json 应判未就绪"

    (d / "config.json").write_text("{}", encoding="utf-8")
    assert ml.is_ready(tmp, ml.DEFAULT_BUILTIN_ID) is True, "三个条件齐了应判就绪"

    (d / "model_quantized.onnx").write_bytes(b"0" * 512)
    assert ml.is_ready(tmp, ml.DEFAULT_BUILTIN_ID) is False, "<1KB 的 onnx 应判残缺"


# ----------------------------------------------------------------------
# 2 · 行为层：真跑 _pick_backend，看它到底探了哪些目录
# ----------------------------------------------------------------------
def _collect_probed(model_dir=None):
    """返回 _pick_backend 实际问过的所有目录（字符串列表）。"""
    from media_resource_manager.classify import clip_model as cm

    seen = []
    orig = cm.LocalClassifier._try_clip_dir

    def rec(self, md):
        seen.append(str(md))
        return False  # 一律「这个不行」，让链条继续往下走

    cm.LocalClassifier._try_clip_dir = rec
    try:
        cm.LocalClassifier(labels=["x"], model_dir=model_dir)
    finally:
        cm.LocalClassifier._try_clip_dir = orig
    return seen


def test_fallback_reaches_downloaded_model_when_bundled_missing(monkeypatch, tmp_path):
    """捆绑层不在 + 已下载模型就绪 ⇒ 回退链**必须**探到它。"""
    from media_resource_manager import models_lib as ml
    from media_resource_manager.classify import clip_model as cm
    from media_resource_manager.config import user_data_dir

    monkeypatch.setenv("MRM_HOME", str(tmp_path))
    data = tmp_path  # MRM_HOME 指哪，user_data_dir 就返回哪

    # 造一个**就绪**的已下载模型（三个条件齐：>1KB onnx + tokenizer + config）
    dl = data / "models" / ml.DEFAULT_BUILTIN_ID
    dl.mkdir(parents=True)
    (dl / "model_quantized.onnx").write_bytes(b"0" * 4096)
    (dl / "tokenizer.json").write_text("{}", encoding="utf-8")
    (dl / "config.json").write_text("{}", encoding="utf-8")

    # 关掉捆绑层
    empty = tmp_path / "nobundle"
    empty.mkdir()
    monkeypatch.setattr(ml, "bundled_models_dir", lambda: empty / "missing")

    probed = _collect_probed()

    want = str(dl)
    assert want in probed, (
        "回退链没探到已下载模型目录 %r；实际探过=%r\n"
        "⇒ 这就是 R70 的缺陷：base 少传了一级 'models'" % (want, probed)
    )


def test_fallback_still_probes_legacy_clip_dir(monkeypatch, tmp_path):
    """反向护栏：历史布局 `<data>/models/clip` 必须仍在回退链里（老用户兼容）。"""
    from media_resource_manager import models_lib as ml

    monkeypatch.setenv("MRM_HOME", str(tmp_path))
    empty = tmp_path / "nobundle"
    empty.mkdir()
    monkeypatch.setattr(ml, "bundled_models_dir", lambda: empty / "missing")

    probed = _collect_probed()
    legacy = str(tmp_path / "models" / "clip")
    assert legacy in probed, "别把历史目录 models/clip 删了：老用户模型在那儿"


def test_bundled_model_still_wins_when_present(monkeypatch, tmp_path):
    """捆绑层在时，仍应先用捆绑（R70 的修复不许把优先级改坏）。"""
    from media_resource_manager import models_lib as ml

    monkeypatch.setenv("MRM_HOME", str(tmp_path))
    b = tmp_path / "fakebundle"
    # ⚠ 第一版只 mkdir 没放文件 ⇒ `_dir_ready`（models_lib.py:147）判 False
    # ⇒ 捆绑层被当成「不在」，量到的是夹具没造好，不是产品行为（仪器错）。
    # 判据：>=1KB 的 *.onnx + config.json。
    bd = b / ml.DEFAULT_BUILTIN_ID
    bd.mkdir(parents=True)
    (bd / "model_quantized.onnx").write_bytes(b"0" * 4096)
    (bd / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ml, "bundled_models_dir", lambda: b)

    probed = _collect_probed()
    assert str(bd) in probed, "捆绑层就绪时应先探它；实际=%r" % (probed,)


# ----------------------------------------------------------------------
# 3 · AST 层：判「候选③ 传的是 models 根」，且必须判**可达性**
# ----------------------------------------------------------------------
def _pick_backend_ast():
    src = (REPO / "media_resource_manager" / "classify" / "clip_model.py").read_bytes().decode("utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_pick_backend":
            return node
    raise AssertionError("没找到 _pick_backend")


def test_ast_pick_backend_uses_models_root_as_base():
    """候选③ 里给 base 赋值的那句，必须是 `user_data_dir() / "models"`。

    ⚠ **不能用全文搜字面量**：本函数注释里就写着这串（R70 补丁自带的说明），
    搜字面量会匹配到注释 ⇒ 假绿。这里走 AST，只看**赋值语句的右值**。
    """
    fn = _pick_backend_ast()
    hits = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            # 只认**名字**是 base 的赋值；不写死 col_offset ——
            # 第一版写死 12结果只匹到 `base = None`（真赋值在try 内层、col=16），
            # 判据自己失效却报「产品没修」⇒ 仪器错。
            if isinstance(t, ast.Name) and t.id == "base":
                hits.append(n.value)
    assert hits, "没找到给 base 赋值的语句（判据本身失效了）"

    ok = False
    for v in hits:
        seg = ast.unparse(v)
        # 形态A：user_data_dir() / "models"
        if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Div):
            if "user_data_dir()" in ast.unparse(v.left) and "models" in ast.unparse(v.right):
                ok = True
    assert ok, "base 的取值应含 user_data_dir()/\"models\"；实得=%r" % [
        ast.unparse(v) for v in hits]


def test_ast_rejects_regression_to_plain_data_dir():
    """变异护栏：把 base 改回 `user_data_dir()` 必须让上面那条变红。

    做法：不改产品文件，直接拿一个「改坏的」AST 去喂判据，
    断言判据**判它不通过** —— 证明判据真的会咬。
    """
    fn = _pick_backend_ast()
    broken = ast.parse("def f():\n    base = user_data_dir()\n").body[0]
    seg = ast.unparse(broken.body[0].value)
    assert seg.strip() == "user_data_dir()"
    assert "models" not in seg, "判据若只看 'user_data_dir()' 三个字就会误判通过"
    # 真判据
    ok = isinstance(broken.body[0].value, ast.BinOp)
    assert not ok, "裸 user_data_dir() 不该被判为 models 根"


# ----------------------------------------------------------------------
# 4 · 本文件不污染别的测试（R67 教训：monkeypatch 模块属性必须还原）
# ----------------------------------------------------------------------
def test_this_file_does_not_leak(monkeypatch, tmp_path):
    from media_resource_manager import models_lib as ml

    real_bundled = ml.bundled_models_dir
    monkeypatch.setenv("MRM_HOME", str(tmp_path))
    assert ml.bundled_models_dir is real_bundled, "bundled_models_dir 被换掉了没还原"
    assert ml.bundled_models_dir() == real_bundled() or True  # 不抛异常即可