"""V0.0.40：把「AI 模型」页和「设置 → AI 与审核」按用户能看懂、逻辑不打架重写。

用户原话：「左边的 AI 模型和设置里面的 AI 与审核，他好多描述我完全看着一头雾水。
好多功能我也感觉逻辑上都有问题。」

查出来**真有问题**的六处（不是文案问题）：
  ① 「用显卡加速」勾选 + 「推理设备」下拉是同一件事的两套开关，
     引擎只在 device=auto 时才看布尔值 ⇒ 勾了/取消都可能没反应；
  ② 「AI 来源」的 builtin 与 local 在引擎里走同一条路径（都读 ai_model_path），
     而网页上根本没有「自定义目录」入口 ⇒ local 是个点不出效果的死选项；
  ③ 在线接口的地址/密钥/模型名常驻显示，选了本地模型也摆在那儿，像必填；
  ④ review_auto_fill / review_key_ask 只被旧桌面端审核窗口读，壳里没人用；
  ⑤ blur_sensitive（敏感图打码）网页审核页**从来没实现**，开关是假的；
  ⑥ 随包内置的模型在网页上显示「未下载」、只给下载按钮，实际后端已支持直接选用。

判据钉关系与结论，不钉死文案字节数。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager"
WEB = SRC / "webapp" / "src"


def _read(rel: str) -> str:
    p = ROOT / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


def _web(rel: str) -> str:
    p = WEB / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


def _ai_section() -> str:
    vue = _web("views/Settings.vue")
    return vue.split("title: 'AI 与审核'", 1)[1].split("},\n  {", 1)[0]


# ───────────────── ① 删掉会打架 / 根本不生效的项 ─────────────────
def test_dead_and_conflicting_items_removed():
    seg = _ai_section()
    for key in ("ai_gpu", "review_auto_fill", "review_key_ask",
                "blur_sensitive"):
        assert f"k: '{key}'" not in seg, f"「{key}」又回到 AI 设置里了"


def test_device_setting_is_single_source():
    """档位只能在一处改，另一处只显示 + 给入口。"""
    seg = _ai_section()
    assert "k: 'ai_device'" in seg, "设置页没有推理档位"
    models = _web("views/Models.vue")
    assert "setDevice" not in models, "AI 模型页还在自己写 ai_device（两处会打架）"
    assert "goSettings" in models and "tab: 'ai'" in models, \
        "模型页没有跳去设置改档位的入口"


# ───────────────── ② 在线接口字段跟着「识别方式」走 ─────────────────
def test_api_fields_follow_backend():
    seg = _ai_section()
    for key in ("ai_api_url", "ai_api_key", "ai_api_model"):
        i = seg.index(f"k: '{key}'")
        block = seg[i:i + 260]
        assert "when:" in block, f"{key} 没有按「识别方式」隐藏"
        assert "ai_backend === 'api'" in block
    # 只剩两种识别方式，local 那条死选项不许回来
    assert "{ v: 'local'" not in seg, "又出现了 builtin/local 两种本地选项"
    vue = _web("views/Settings.vue")
    assert "form.ai_backend === 'local'" in vue, "老配置里的 local 没有归一"


def test_when_filter_is_implemented_generically():
    vue = _web("views/Settings.vue")
    assert "it.when" in vue, "设置页没实现「按条件显示」"
    # 搜索时不套 when：用户明确在找这一项
    assert "searching.value || !it.when" in vue


# ───────────────── ③ 模型页：术语、状态、内置可用 ─────────────────
def test_models_page_has_no_provider_jargon():
    models = _web("views/Models.vue")
    for bad in ("onnx_providers", "gpu_providers", "DmlExecutionProvider",
                "CUDA", "执行器"):
        assert bad not in models, f"模型页还显示技术名词 {bad}"
    assert "输入：" not in models, "模型卡还写着没人懂的「输入：224」"
    assert "m.ready || m.builtin" in models, \
        "随包内置的模型还是只给下载按钮（不能用）"


def test_device_report_text_is_plain_chinese():
    from media_resource_manager.models_lib import device_report
    cases = [
        (True, ["CUDAExecutionProvider", "CPUExecutionProvider"]),
        (True, ["CPUExecutionProvider"]),
        (False, ["CUDAExecutionProvider"]),
        (True, []),
    ]
    for want, prov in cases:
        text = device_report(want, prov, small_model=False)["text"]
        assert text, "状态说明不能为空"
        for bad in ("ExecutionProvider", "onnxruntime", "DirectML", "QNN",
                    "VitisAI", "OpenVINO"):
            assert bad not in text, f"界面状态里还有后端名 {bad}：{text}"


# ───────────────── ④ 后端：钳制与同步 ─────────────────
def test_backend_clamps_and_syncs():
    w = _read("media_resource_manager/webui.py")
    assert 'if k == "ai_threads"' in w and "min(iv, 32)" in w, \
        "线程数没有上限（裸接口能塞 999）"
    assert 'if k == "review_margin"' in w and "min(fv, 1.0)" in w, \
        "拿不准的范围没有钳制"
    assert 'if "ai_device" in changed' in w and "self.cfg.ai_gpu = _dev" in w, \
        "改档位时没有同步老的 ai_gpu 字段"
    # 选模型不再偷偷把 ai_backend 改成 local
    use = w.split("def _api_model_use", 1)[1].split("\n    def ", 1)[0]
    assert "ai_backend = \"local\"" not in use


def test_version_bumped():
    import re as _re
    from media_resource_manager import VERSION
    m = _re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 40, f"版本回退：{VERSION}"
    assert f"V{VERSION}" in _read("build/pyinstaller.spec"), \
        "打包注释版本与 __init__.py 不一致"
