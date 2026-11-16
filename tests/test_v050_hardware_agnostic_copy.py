"""V0.0.50 护栏：模型硬件描述面向全谱系设备（开源后千人千机）。

用户：「要考虑以后出现 3090/4090 甚至 5090 的显卡，模型描述放宽一点，
不要只想着我只有 1080。软件以后要开源（适配器不开源），不同用户不同电脑
装上效果都会不一样。」

落地：
  · 模型元数据从「最低门槛」改成「适配范围」——基础配置 + 显卡越强越好；
  · hardware_line 不再透着"下限"框架；
  · 模型页加面向各类硬件的总说明 + 按本机显存给出的档位建议（前端用已有
    sys.gpus.vram_total 判断，不新增接口）；
  · 「门槛：」改成「适配：」。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "media_resource_manager/models_lib.py"
VIEWS = ROOT / "media_resource_manager/webapp/src/views"


# ─────────────── ① 元数据面向全谱系 ───────────────
def test_no_specific_gpu_model_or_minimum_framing():
    s = MOD.read_text(encoding="utf-8")
    for bad in ("GTX 10", "RTX 30", "RTX 40", "RTX 50", "最低 GPU",
                "最低 CPU", "最低门槛", "i3-4160", "i5-4460", "i7-4770"):
        assert bad not in s, f"硬件描述又写死/退回下限框架：{bad!r}"


def test_models_state_upward_scaling():
    """描述要说清"显卡越强越好、可上更高精度档"，而非暗示当前硬件是上限。"""
    s = MOD.read_text(encoding="utf-8")
    for good in ("各类显卡均可加速", "显卡性能越强", "显存越充裕越能发挥",
                 "大显存显卡适配", "越大越快"):
        assert good in s, f"缺向上兼容的正向口径：{good}"


def test_high_end_tier_described_as_capability_not_bar():
    """最高精度档：说"适配大显存显卡、越强越快"，不是"必须 12GB 才能跑"。"""
    s = MOD.read_text(encoding="utf-8")
    seg = s.split('"id": "clip-vit-l14-336"', 1)[1].split("\n    },", 1)[0]
    assert "精度最优档" in seg
    assert "大显存显卡适配" in seg
    assert "发挥最佳" in seg
    assert "必须有" not in seg and "跑不动" not in seg


# ─────────────── ② 界面面向开源多硬件 ───────────────
def test_models_page_explains_device_variation():
    s = (VIEWS / "Models.vue").read_text(encoding="utf-8")
    assert "各类设备上" in s and "硬件性能决定可选的精度档位与处理速度" in s, \
        "模型页没说清不同设备效果不同"
    assert "核显" not in s or "各类设备" in s  # 不用过窄术语，或已用通用说法


def test_hardware_label_is_adaptation_not_threshold():
    s = (VIEWS / "Models.vue").read_text(encoding="utf-8")
    assert "适配：" in s, "卡片里还写着「门槛：」"
    assert "门槛：" not in s


def test_big_vram_hint_uses_existing_sysinfo():
    """大显存提示必须复用已检测的 sys.gpus，不新增后端接口。"""
    s = (VIEWS / "Models.vue").read_text(encoding="utf-8")
    assert "bigVram" in s and "vram_total" in s
    assert "12 * 1073741824" in s, "大显存阈值应为 12GB"
    assert "hint-vram" in s


# ─────────────── ③ 防止「computed 未导入」白屏 ───────────────
def test_vue_composables_used_are_imported():
    """V0.0.50 踩坑：用了 computed 却没 import，Vite 构建不报、界面整页白屏。

    凡是模板/script 里用到的 Vue 组合式 API，必须从 'vue' 导入。
    """
    s = (VIEWS / "Models.vue").read_text(encoding="utf-8")
    imp_line = ""
    for line in s.splitlines():
        if "from 'vue'" in line or 'from "vue"' in line:
            imp_line = line
            break
    for api in ("computed", "ref", "onMounted", "onUnmounted"):
        if f"{api}(" in s or f"{api}(" in s:
            assert api in imp_line, f"{api} 被使用但未从 vue 导入 —— 会整页白屏"
