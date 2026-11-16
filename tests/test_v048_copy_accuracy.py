"""V0.0.48 护栏：界面描述准确 + 专业书面。

用户 2026-11-16 反馈两类问题：
  A) 事实描述错 —— 模型页写「最低 GPU GTX 1080 / 显存 8GB」，用户用 L14
     实测显卡占用仅 35%、显存 1.3GB，与「最低 8GB」严重不符；还有
     「CPU 上慢 3~5 倍」「纯 CPU 基本跑不动」「必须有独显」这类无依据的
     精确倍数与绝对门槛。
  B) 口语化 —— 「不知道选哪个就什么都不用动」「拿不准」「用什么算」
     「点「继续」」「试试」等，r240/v013 词表原先没盖住。

本文件钉住这两类，防止回退。r240/v013 已扩词表覆盖 B 的通用部分，
这里补的是**语义级**判据（具体型号、倍数、门槛语气）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "media_resource_manager" / "models_lib.py"
VIEWS = ROOT / "media_resource_manager" / "webapp" / "src" / "views"


# ─────────────── ① 硬件门槛不写死具体型号 / 无依据倍数 ───────────────
def test_no_hardcoded_gpu_model_in_hardware_line():
    """「最低 GPU GTX 1080」这类写法会误导用户以为必须高端卡。

    改成能力档描述（无需独显 / 可选独显加速 / 推荐独显 / 需要独显）。
    """
    s = MODELS.read_text(encoding="utf-8")
    for bad in ("GTX 10", "RTX 30", "RTX 40", "i3-4160", "i5-4460",
                "i7-4770", "i5-10400", "最低 GPU", "最低 CPU"):
        assert bad not in s, f"模型元数据又把硬件门槛写死成 {bad!r}"


def test_no_unbacked_speed_claims():
    s = MODELS.read_text(encoding="utf-8")
    for bad in ("3~5 倍", "基本跑不动", "必须有独显", "最快、最小",
                "精度最高", "默认就是这个"):
        assert bad not in s, f"模型描述出现无依据/绝对化表述 {bad!r}"


def test_hardware_line_uses_capability_wording():
    s = MODELS.read_text(encoding="utf-8")
    assert ("无显卡亦可运行" in s or "无需独立显卡" in s), "缺无显卡可运行的能力档描述"
    assert ("各类显卡均可加速" in s or "可选独立显卡" in s), "缺各类显卡可加速的能力档描述"
    assert ("独立显卡加速更佳" in s or "推荐独立显卡" in s), "缺独显加速更佳的能力档描述"
    # hardware_line 不再用「最低」这种暗示高门槛的措辞
    seg = s.split("def hardware_line", 1)[1].split("def ", 1)[0]
    assert "最低" not in seg


def test_model_notes_are_professional():
    s = MODELS.read_text(encoding="utf-8")
    for good in ("体积最小、识别最快，纯处理器即可运行",
                 "识别精度明显提升",
                 "不依赖文本对齐，仅输出视觉特征向量"):
        assert good in s, f"模型说明缺书面表述：{good}"


# ─────────────── ② 设备状态文案准确 ───────────────
def test_device_report_has_no_misleading_cpu_claim():
    s = MODELS.read_text(encoding="utf-8")
    # 旧文案「这些模型在CPU上通常还更快」对小模型成立、对 L14 不成立，已删
    assert "这类模型在 CPU 上通常还更快" not in s
    assert "这些模型在 CPU 上通常还更快" not in s
    assert "正在使用显卡加速" in s
    assert "不属于故障" in s


def test_models_page_no_misleading_cpu_note():
    s = (VIEWS / "Models.vue").read_text(encoding="utf-8")
    assert "这些模型在 CPU 上通常还更快" not in s
    assert "所选模型档位在 CPU 上通常具有相近或更优的推理效率" in s
    assert "不知道选哪个" not in s
    assert "硬件性能决定可选的精度档位与处理速度" in s, "缺面向各类硬件的适配说明"


# ─────────────── ③ 设置页术语书面化 ───────────────
def test_settings_labels_are_formal():
    s = (VIEWS / "Settings.vue").read_text(encoding="utf-8")
    assert "label: '推理设备'" in s, "「用什么算」还在"
    assert "label: '待审核区间'" in s, "「拿不准的范围」还在"
    assert "CPU（最慢，但一定能用）" not in s
    assert "CPU（速度较慢，兼容性最佳）" in s
