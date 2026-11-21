"""AI 模型库与自动下载（需求 9）。

用户要求：「AI与过滤功能内提供多个模型的自动下载」。
原来只有一个「自定义本地模型目录」输入框 —— 想换模型得自己上网找、
下完再手填路径，实际等于没有模型管理。

这里做的是：
- 内置一张**模型清单**（名字 / 仓库标识 / 大小 / 一句话说明 / 默认后端），
  界面上列出来让用户点一下就能下；
- 下载走**后台线程**（模型动辄几百 MB，绝不能堵主线程），带进度回调；
- 校验落盘大小 —— 下载不完整时明确报错，不让用户拿一个坏模型去跑分类；
- 已经下好的直接显示「已就绪」，不重复下。

只用标准库 urllib，不引入 requests；断点续传用 Range 头，能续就续。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# ---- 内置模型清单 -------------------------------------------------------
# repo/filename 指向 HuggingFace 的公开文件。这里列的都是 onnx 为主的
# CLIP / DINOv2 类视觉模型 —— 打标签只关心「图里有什么」，不需要大语言模型。
#
# R86 #51：每个模型必须带**硬件要求**（处理器 / 显卡 / 显存能力档）与一句
# 「优点」短词，界面上直接列出来。为什么写在清单里而不是界面里：
# 门槛是**模型属性**，跟着模型走；塞进 UI 代码就等于每个皮肤/每个页面
# 各抄一份，改一个数字要找五处。
#
# ⚠ ``ms`` = 魔搭（ModelScope）上的 ``命名空间/模型名``，用于国内镜像兜底。
#   R149-A：已联网逐个核实 6 个模型的 ModelScope 命名空间，全部存在
#   （均位于 ``Xenova/`` 前缀下，而非旧的 ``AI-ModelScope/`` —— 后者只镜像了
#   部分大模型，小模型返回 404）。下载失败会给出明确中文报错而不是静默半截文件。
CLIP_FILES = ["onnx/model_quantized.onnx",
              "config.json", "preprocessor_config.json",
              "tokenizer.json", "tokenizer_config.json",
              "special_tokens_map.json", "vocab.json", "merges.txt"]

# V0.0.51：中文 CLIP 的词表是 vocab.txt（不是英文 CLIP 的 vocab.json +
# merges.txt）。少一个文件就会在下载阶段 404，所以单独一份清单。
CHINESE_CLIP_FILES = ["onnx/model_quantized.onnx",
                      "config.json", "preprocessor_config.json",
                      "tokenizer.json", "tokenizer_config.json",
                      "special_tokens_map.json", "vocab.txt"]

DINO_FILES = ["onnx/model_quantized.onnx", "config.json",
              "preprocessor_config.json"]

CATALOG = [
    {
        "id": "clip-vit-b32",
        "name": "CLIP ViT-B/32（内置默认）",
        "repo": "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main",
        "ms": "Xenova/clip-vit-base-patch32",
        "files": list(CLIP_FILES),
        "size_hint": "约 90 MB",
        "note": "体积最小、识别最快，纯处理器即可运行。系统默认选用此档；显卡性能更强的设备可选用下方更高精度档位。",
        "backend": "builtin",
        # ↓ R86 #51：硬件门槛（用户点名要在界面上标出来）
        "pros": "体积最小·无需独显",
        "min_cpu": "主流处理器即可",
        "min_gpu": "无显卡亦可运行",
        "vram": "不依赖显存",
        "input": 224,
    },
    {
        "id": "clip-vit-b16",
        "name": "CLIP ViT-B/16（较默认更精细）",
        "repo": "https://huggingface.co/Xenova/clip-vit-base-patch16/resolve/main",
        "ms": "Xenova/clip-vit-base-patch16",
        "files": list(CLIP_FILES),
        "size_hint": "约 170 MB",
        "note": "图像切分更细，识别精度高于 B/32，推理耗时适中。各类显卡均可加速，性能越强速度越快。",
        "backend": "local",
        "pros": "精度较高",
        "min_cpu": "主流处理器即可",
        "min_gpu": "各类显卡均可加速",
        "vram": "约 2 GB 即可流畅",
        "input": 224,
    },
    {
        "id": "clip-vit-l14",
        "name": "CLIP ViT-L/14（更准）",
        "repo": "https://huggingface.co/Xenova/clip-vit-large-patch14/resolve/main",
        "ms": "Xenova/clip-vit-large-patch14",
        "files": list(CLIP_FILES),
        "size_hint": "约 430 MB",
        "note": "识别精度明显提升。配备独立显卡时速度显著加快，显存越充裕越能发挥该档精度；大显存显卡可进一步选用最高精度档。",
        "backend": "local",
        "pros": "精度明显提升",
        "min_cpu": "主流处理器即可",
        "min_gpu": "独立显卡加速更佳",
        "vram": "4 GB 起，越大越快",
        "input": 224,
    },
    {
        "id": "clip-vit-l14-336",
        "name": "CLIP ViT-L/14@336（精度最优）",
        "repo": "https://huggingface.co/Xenova/clip-vit-large-patch14-336/resolve/main",
        "ms": "Xenova/clip-vit-large-patch14-336",
        "files": list(CLIP_FILES),
        "size_hint": "约 450 MB",
        # ④ 是精度最优档：输入 336×336，token 数是 224 版的 2.25 倍，
        # 低性能设备上推理较慢。写清适配范围，便于用户按硬件条件选择。
        "note": "输入分辨率 336，细节还原最充分，为当前精度最优档。适配大显存显卡；显卡性能越强，处理速度越快。",
        "backend": "local",
        "pros": "精度最优",
        "min_cpu": "较新多核处理器",
        "min_gpu": "大显存显卡适配",
        "vram": "12 GB 及以上发挥最佳",
        "input": 336,
    },
    {
        "id": "dinov2-small",
        "name": "DINOv2-S/14（纯视觉特征）",
        "repo": "https://huggingface.co/Xenova/dinov2-small/resolve/main",
        "ms": "Xenova/dinov2-small",
        "files": list(DINO_FILES),
        "size_hint": "约 90 MB",
        "note": "不依赖文本对齐，仅输出视觉特征向量，适用于以图搜图。纯处理器即可运行，显卡可加速。",
        "backend": "local",
        "pros": "以图搜图·不依赖文本",
        "min_cpu": "主流处理器即可",
        "min_gpu": "无显卡亦可运行",
        "vram": "不依赖显存",
        "input": 224,
    },
    {
        "id": "dinov2-base",
        "name": "DINOv2-B/14（纯视觉·精度更优）",
        "repo": "https://huggingface.co/Xenova/dinov2-base/resolve/main",
        "ms": "Xenova/dinov2-base",
        "files": list(DINO_FILES),
        "size_hint": "约 350 MB",
        "note": "S/14 的高精度版本，以图搜图匹配质量更优。显卡性能越强，处理速度越快。",
        "backend": "local",
        "pros": "以图搜图精度更优",
        "min_cpu": "主流处理器即可",
        "min_gpu": "各类显卡均可加速",
        "vram": "约 2 GB 即可流畅",
        "input": 224,
    },
    # ── V0.0.51：用户要「更多模型、大显存也要有档位」。以下四个都按
    #    已核实的公开仓库补全，文件清单各自对齐，不造链接。 ──
    {
        "id": "chinese-clip-vit-b16",
        "name": "中文 CLIP ViT-B/16（中文词更准）",
        "repo": "https://huggingface.co/Xenova/chinese-clip-vit-base-patch16/resolve/main",
        "ms": "Xenova/chinese-clip-vit-base-patch16",
        "files": list(CHINESE_CLIP_FILES),
        "size_hint": "约 190 MB",
        "note": "在中文图文上训练，识别中文主题词更贴近语义。处理器即可运行，显卡越快越流畅。",
        "backend": "local",
        "pros": "中文词更准",
        "min_cpu": "主流处理器即可",
        "min_gpu": "各类显卡均可加速",
        "vram": "约 2 GB 即可流畅",
        "input": 224,
    },
    {
        "id": "chinese-clip-vit-l14",
        "name": "中文 CLIP ViT-L/14（中文精度更高）",
        "repo": "https://huggingface.co/Xenova/chinese-clip-vit-large-patch14/resolve/main",
        "ms": "Xenova/chinese-clip-vit-large-patch14",
        "files": list(CHINESE_CLIP_FILES),
        "size_hint": "约 410 MB",
        "note": "中文图文对齐的高精度档，适合中文标签较细的图库。独立显卡加速更明显，显存越充裕越能发挥。",
        "backend": "local",
        "pros": "中文高精度",
        "min_cpu": "较新多核处理器",
        "min_gpu": "独立显卡加速更佳",
        "vram": "4 GB 起，8 GB 及以上更从容",
        "input": 224,
    },
    {
        "id": "chinese-clip-vit-l14-336",
        "name": "中文 CLIP ViT-L/14@336（大显存高精度）",
        "repo": "https://huggingface.co/Xenova/chinese-clip-vit-large-patch14-336px/resolve/main",
        "ms": "Xenova/chinese-clip-vit-large-patch14-336px",
        "files": list(CHINESE_CLIP_FILES),
        "size_hint": "约 410 MB",
        "note": "中文档里细节还原最充分的一档（336 输入）。面向 12 GB 及以上大显存显卡：显存越充裕，速度越稳；小显存也能跑但会明显变慢。",
        "backend": "local",
        "pros": "中文最高精度",
        "min_cpu": "较新多核处理器",
        "min_gpu": "大显存显卡适配（12 GB 及以上更从容）",
        "vram": "12 GB 及以上发挥最佳",
        "input": 336,
    },
    {
        "id": "dinov2-large",
        "name": "DINOv2-L/14（大显存·以图搜图）",
        "repo": "https://huggingface.co/Xenova/dinov2-large/resolve/main",
        "ms": "Xenova/dinov2-large",
        "files": list(DINO_FILES),
        "size_hint": "约 310 MB",
        "note": "纯视觉特征档里精度更高的一档，适合以图搜图、找相似画面。大显存显卡可充分发挥；不依赖文本对齐。",
        "backend": "local",
        "pros": "大显存·相似图更准",
        "min_cpu": "较新多核处理器",
        "min_gpu": "大显存显卡适配（8 GB 及以上更从容）",
        "vram": "8 GB 及以上发挥最佳",
        "input": 224,
    },
    # ── V0.0.74：看图说话（描述模型），给「AI 自己造词」的 local 方式用。
    #    不是 CLIP 那种"给词打分"，是真的对每张图生成一句英文描述，再拆词。
    #    仓库是社区导出的现成 ONNX（blip_vision_encoder + blip_text_decoder
    #    两个文件，平铺在根目录），本机没有 torch 环境也能跑。
    #    ⚠ 无魔搭命名空间 → mirror_urls 会自动跳过魔搭，只走 hf-mirror→HF。
    {
        "id": "blip-caption-base",
        "name": "BLIP 看图说话（AI 造词用）",
        "repo": "https://huggingface.co/ningpp/blip-image-captioning-base-ONNX/resolve/main",
        "files": ["config.json", "preprocessor_config.json", "vocab.txt",
                  "blip_vision_encoder.onnx", "blip_text_decoder.onnx"],
        "size_hint": "约 950 MB",
        "note": "让 AI 自己看图说出画面里的词，不用先给它词表。体积较大、首次扫描较慢；有独立显卡会快一些，处理器也能跑，只是慢。仅供「AI 认识图库 → 扩词」功能使用。",
        "backend": "local",
        "pros": "AI 自由造词",
        "min_cpu": "主流处理器即可（较慢）",
        "min_gpu": "独立显卡明显加速",
        "vram": "约 2 GB 即可",
        "input": 384,
    },
]

#: R86 #51：界面上「4 档 CLIP」的展示顺序（默认包只带第一个）。
#: 其余清单项（DINOv2 之类）照常列出，但排在这 4 个之后 —— 用户点名要的是
#: 这 4 档，别让它们淹在一堆模型里。
CLIP_ORDER = ("clip-vit-b32", "clip-vit-b16", "clip-vit-l14",
              "clip-vit-l14-336")


def sorted_catalog():
    """按「4 档 CLIP 在前、其余在后」排序的清单副本（不改动 CATALOG 本体）。

    ⚠ 返回**副本**：直接 sort 原列表会让 id 顺序随调用变，测试与调用方
    拿到的顺序就不稳定（R63 教训：顺序不稳定 = 偶发红灯）。
    """
    def _key(m):
        try:
            return (0, CLIP_ORDER.index(m["id"]))
        except (ValueError, KeyError):
            return (1, 0)
    return sorted(CATALOG, key=_key)


def hardware_line(m: dict) -> str:
    """模型硬件适配的一行说明（界面 / 日志共用）。

    口径是「适配范围」而非「下限要求」：既讲清基础配置，也说明显卡越强
    表现越好，避免让用户误以为这是该模型的硬件上限。软件面向开源后的
    各类设备（核显 / 入门独显 / 高端显卡），因此不写死具体型号。
    """
    return (f"CPU {m.get('min_cpu') or '—'}　"
            f"GPU（显卡）{m.get('min_gpu') or '—'}　"
            f"显存 {m.get('vram') or '—'}")


# ---- R172：推理后端探测（桌面 + 网页同一份，别两端各写一套） -----------
def available_providers() -> list[str]:
    """本机 onnxruntime 实际可用的执行后端（探测失败返回空列表）。

    ⚠ 一次探测会 import onnxruntime（几百 ms）。调用点都在**打开设置对话框**
    这种低频路径上，不要放进循环或每帧刷新的地方。
    """
    try:
        import onnxruntime as ort
        return [str(p) for p in ort.get_available_providers()]
    except Exception:
        return []


def device_report(gpu_wanted: bool,
                   providers: list[str] | None = None,
                   device: str | None = None,
                   small_model: bool = True) -> dict:
    """R172：把「配置里想要什么」+「本机实际能给什么」算成一句人话。

    用户反馈的核心是「开关在菜单深处、打开了也不知道生没生效」。所以这里
    不只回一个布尔，而是回**能直接显示的状态**：

        {"want": "gpu"|"cpu",      配置里选的
         "have": "gpu"|"cpu",      本机实际会用的（决定性）
         "providers": [...],       本机可用后端
         "degraded": bool,         want=gpu 但只能 CPU ⇒ 界面要讲明白
         "text": "..."}            一句中文，桌面与网页共用同一措辞

    判定口径与 ``clip_onnx.ClipONNX._pick_providers`` **必须一致**：
    那边是「gpu=True 时优先 CUDA → DirectML → CPU，一个都没有才 CPU」。
    这里用同一份名单判断「有没有非 CPU 后端可挑」。两处若漂移，就会出现
    「设置页说在用 GPU、日志说在用 CPU」——正是用户抱怨的那类不一致。
    """
    prov = list(providers) if providers is not None else available_providers()
    gpu_backends = [p for p in prov
                    if p not in ("CPUExecutionProvider",
                                 "AzureExecutionProvider")]
    want_gpu = bool(gpu_wanted)
    have_gpu = bool(gpu_backends)
    # 2026-10-27：强制 CPU 时即便有显卡后端也不能报 gpu（否则设置页
    # 显示"正在用显卡"，实际跑的是 CPU —— 正是用户最烦的那种假绿）。
    _mode = (device or '').lower().strip()
    # ⚠ 2026-10-27：auto 档且是**小模型**时，真实跑的是 CPU（实测快 15 倍）——
    # 这里必须跟着说实话，否则设置页显示"正在用显卡"、实际 CPU，就是假绿。
    # NPU 档：有没有 NPU 后端单说，先把它算进"是不是加速后端"
    npu_backends = [p for p in prov
                    if p in ('QNNExecutionProvider',
                             'VitisAIExecutionProvider',
                             'OpenVINOExecutionProvider')]
    have_npu = bool(npu_backends)
    use_gpu = (want_gpu and have_gpu and _mode not in ('cpu', 'npu')
               and not (_mode in ('', 'auto') and small_model))
    # V0.0.40：这些句子会**原样显示在界面**上，所以不许出现后端英文名
    # （DmlExecutionProvider / CUDAExecutionProvider …）——用户不需要知道
    # 具体走的哪套接口，只需要知道"现在用的是显卡还是 CPU、为什么"。
    if not prov:
        text = "读不到本机推理组件，已采用 CPU 处理（识别功能不受影响）"
    elif use_gpu:
        text = "正在使用显卡加速。"
    elif want_gpu and not have_gpu:
        # 2026-10-27：用户看到这句话以为"软件没认出我的显卡"。原文案只说
        # "没有可用的显卡后端"，没说**为什么**、也没说**其实不影响速度**。
        # 实测：本项目的 CLIP-B32 量化模型在 CPU 上 0.37s/张，装 DirectML
        # 走 GTX1080 反而 5.6s/张（小模型在显卡上分片开销大于收益）。
        # 所以这里改成"说人话 + 给出原因 + 给出选择"。
        text = ("已选择显卡加速，但本机未能启用显卡推理，当前采用 CPU。\n"
                "小型模型在 CPU 上通常具有更优的推理效率，此情况不属于故障；\n"
                "如需启用显卡加速，请更新显卡驱动至支持 DirectX 12 的版本，"
                "随后重启软件。加速组件已随软件内置。")
    elif _mode == "npu":
        if have_npu:
            text = "正在用 NPU 加速。"
        else:
            text = "已选择 NPU 加速，但本机未检测到可用的 NPU，当前采用 CPU。"
    elif _mode in ("", "auto") and have_gpu:
        # 自动档且本机有显卡：说实话——小模型（内置 153MB 那个）在显卡上更慢，
        # 所以自动会用 CPU。用户想强制上显卡，切"强制显卡"即可。
        text = ("自动模式：本机具备显卡，但当前模型档位采用 CPU 推理效率更优，故使用 CPU。"
                "如需强制使用显卡，请在设置中将「推理设备」调整为「显卡」。")
    elif not want_gpu:
        text = "按设置使用 CPU" + ("（本机具备显卡，可调整为自动或显卡）"
                                   if have_gpu else "")
    else:
        text = "当前采用 CPU 推理"
    # 2026-10-27：三档 —— 用户明确选了「强制显卡 / 强制 CPU」时，
    # 文案要说清"这是你的选择"，而不是含糊的"已降级"。
    mode = (device or "").lower().strip()
    if mode == "cpu":
        text = "按设置使用 CPU" + ("（本机具备显卡，可调整为自动或显卡）"
                                   if have_gpu else "")
    elif mode == "gpu" and not have_gpu:
        text = ("已选择显卡加速，但本机未能启用显卡推理，已回退至 CPU。\n"
                "通常为显卡驱动版本过低所致；更新驱动后重启软件即可。")
    elif mode == "gpu" and have_gpu:
        text = "正在使用显卡加速。"
    return {"want": ("npu" if _mode == "npu" else ("gpu" if want_gpu else "cpu")),
            "have": ("npu" if (_mode == "npu" and have_npu)
                     else ("gpu" if use_gpu else "cpu")),
            "npu_have": have_npu,
            "npu_providers": npu_backends,
            "providers": prov,
            "degraded": bool(want_gpu and not have_gpu),
            "text": text}


def gpu_switch_text(gpu_wanted: bool,
                    providers: list[str] | None = None) -> str:
    """R172：AI 模型设置对话框顶部开关旁的**实际状态**一句话。"""
    return device_report(gpu_wanted, providers)["text"]



def bundled_ready(model_id: str) -> bool:
    """这个模型是不是**随软件捆绑**且可用（不用下载）。"""
    try:
        return _dir_ready(bundled_models_dir() / model_id)
    except Exception:
        return False


# ---- R168：下载请求身份 + 分源超时 -------------------------------------
# ⚠ **必须带浏览器式 UA**：hf-mirror.com 对标准库 urllib 的默认 UA
#   （``Python-urllib/3.x``）与任何看起来不像浏览器的短 UA 直接返回
#   **403 Forbidden** —— 用户真机实测「hf-mirror 裸 urllib 403，带浏览器
#   UA 200」。以前这里写的是 ``mrm/1.0``，等于自己把自己挡在门外。
#   本字符串同时用于普通请求与 Range 续传请求（同一个 ``_open_url`` 出口）。
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/122.0.0.0 Safari/537.36 SweetAI/1.0")

#: 默认（魔搭 / hf-mirror）超时。国内镜像响应快，30s 足够。
TIMEOUT = 30
#: R168：HF 直连的单源超时**收紧**。国内裸网访问 huggingface.co 是
#: 「握手后长期无响应」而不是秒拒 —— 用默认 30s 会让 auto 用户在死源上
#: 干等 30s × 8 个文件。15s 内拿不到头就换下一源，让 auto 真正"自动"。
TIMEOUT_HF = 15


def _timeout_for(url: str) -> int:
    """按源给超时：HF 直连收紧，其余用默认（R168）。"""
    return TIMEOUT_HF if "huggingface.co" in (url or "") else TIMEOUT


# ---- 默认模型解析（C8）------------------------------------------------
#: 捆绑模型放在包内这个子目录（build_nodel.py 的 datas 会带它）。
BUNDLED_SUBDIR = "bundled_models"
#: 清单里``backend == "builtin"`` 的第一个 id —— 也就是「内置默认」那个。
DEFAULT_BUILTIN_ID = "clip-vit-b32"

# ---- R152-D5：模型「假就绪」三件套 -------------------------------------
#: 下载成功时写下的「实证大小」边车（模型目录内，JSON：文件名 → 字节数）。
#: is_ready 用它校验 onnx 与下载时**实证**的大小一致 —— 连接中断留下的
#: 半截文件不再能冒充「已就绪」。老目录没有边车 → 维持原宽松判据
#: （升级不把老用户模型误判成未就绪）。
SIZES_SIDECAR = ".mrm_sizes.json"


def bundled_models_dir() -> Path:
    """包内捆绑模型目录（**只读**，不往这里下载）。

    路径按优先级找，找到第一个存在的就返回（找不到返回一个不存在的路径，
    调用方靠 ``.is_dir()`` 判断即可，不必处理 None）。

    为什么要「捆绑」而不是让用户自己下：用户反馈「内置 clip 是空」——
    打开 AI 模型设置看见一个空的模型目录，等于这个功能压根不能用。
    捆绑一份基础 CLIP（ONNX/CPU 版，约 90MB）能让软件**装完即用**，
    第一次启动不必联网、不必手动配路径。
    """
    here = Path(__file__).resolve().parent# media_resource_manager/
    cands = [
        # 打包后：_internal/ 或 exe 同级下的 bundled_models
        here.parent / BUNDLED_SUBDIR,
        here / BUNDLED_SUBDIR,
        here.parent / "data" / BUNDLED_SUBDIR,
    ]
    for c in cands:
        try:
            if c.is_dir():
                return c
        except OSError:
            continue
    return cands[0]


def resolve_default_model_dir(base: Path) -> Optional[Path]:
    """C8：解析「开箱即用」的默认模型目录，返回 None 表示本机没有可用模型。

    查找顺序（先捆绑后下载 —— 捆绑的版本是随软件一起验过的，最稳）：

    1. **包内捆绑** ``bundled_models/clip-vit-b32``（CPU-onlyONNX 版）
    2. **已下载** ``<数据目录>/models/clip-vit-b32``（用户点过「下载」）
    3. 退而求其次：任意一个 ``is_ready()`` 为真的清单项

    返回的是**目录路径**，直接喂给 ``LocalClassifier(model_dir=...)``。
    """
    # 1) 捆绑
    d = bundled_models_dir() / DEFAULT_BUILTIN_ID
    if _dir_ready(d):
        return d
    # 2) 已下载
    try:
        d = model_dir(base, DEFAULT_BUILTIN_ID)
        if is_ready(base, DEFAULT_BUILTIN_ID):
            return d
    except Exception:
        pass
    # 3) 任意就绪的清单项
    try:
        for m in CATALOG:
            if is_ready(base, m["id"]):
                return model_dir(base, m["id"])
    except Exception:
        pass
    return None


def _dir_ready(d: Path) -> bool:
    """目录是否含一个像样的 onnx（判据与 :func:`is_ready` 一致）。"""
    try:
        d = Path(d)
        if not d.is_dir():
            return False
        for f in d.rglob("*.onnx"):
            try:
                if f.stat().st_size >= 1024:
                    return (d / "config.json").exists()
            except OSError:
                continue
    except Exception:
        return False
    return False


def model_dir(base: Path, model_id: str) -> Path:
    return Path(base) / model_id


def is_ready(base: Path, model_id: str) -> bool:
    """模型是否已下好。

    判据不是「目录存在」，而是**主权重文件存在且大小合理**：
    下载中断留下的空目录会让每次启动都以为下好了，一跑就报 onnx 加载失败。

    R152-D5 追加：目录下有 ``.mrm_sizes.json`` 边车（下载成功时写下的
    实证大小）时，onnx 必须与边车记录**逐一致** —— 连接中断留下的半截
    文件不再能冒充「已就绪」；边车缺失/损坏 = 老目录，维持原宽松判据
    （升级不把老用户模型误判成未就绪）。
    """
    d = model_dir(base, model_id)
    onnx = list(d.glob("*.onnx")) if d.is_dir() else []
    if not onnx:
        return False
    for f in onnx:
        try:
            if f.stat().st_size < 1024:      # 1KB 以下的 onnx 一定是残缺
                return False
        except OSError:
            return False
    if not (d / "config.json").exists():
        return False
    return _sizes_match(d, onnx)


def _sizes_match(d: Path, onnx: list) -> bool:
    """边车在位时校验 onnx 大小与下载时实证一致；边车不可用 = 老目录放行。"""
    try:
        raw = json.loads((d / SIZES_SIDECAR).read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(raw, dict):
        return True
    for f in onnx:
        exp = raw.get(f.name)
        if isinstance(exp, int) and exp > 0:
            try:
                if f.stat().st_size != exp:
                    return False
            except OSError:
                return False
    return True


def local_dirs(base: Path) -> list[tuple[str, Path, bool]]:
    """列出磁盘上所有模型（清单里的 + 用户自建的），返回 (id, 路径, 就绪)。"""
    base = Path(base)
    out: list[tuple[str, Path, bool]] = []
    known = {m["id"] for m in CATALOG}
    if base.is_dir():
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.name in known:
                continue
            out.append((d.name, d, bool(list(d.glob("*.onnx")))))
    for m in CATALOG:
        d = model_dir(base, m["id"])
        if d.is_dir():
            out.append((m["id"], d, is_ready(base, m["id"])))
    return out


MIRROR_HF = "hf"
MIRROR_HFM = "hf-mirror"
MIRROR_MS = "modelscope"

#: 下载源（界面下拉用）。``auto`` = 按国内实测连通性自动串。
#: R149-A：新增 hf-mirror.com 国内镜像（HF 的完整镜像，国内访问稳）。
#: R168：**auto 顺序重排为 hf-mirror → HF → 魔搭**。理由（用户真机实测）：
#:   · huggingface.co 在国内裸网**超时**（用户 cfg.proxy=null），放第一位等于
#:     每个文件先白等一轮；
#:   · hf-mirror.com 国内最稳最快，且它的仓库结构与 HF 完全一致（连
#:     /resolve/main/ 路径都一样）⇒ 覆盖面也最全，当第一顺位最合理；
#:   · HF 放第二：给「挂了代理/能直连」的用户，不再是所有人的必经弯路；
#:   · 魔搭放最后兜底：它的 URL 是**另一套文件接口**，且部分模型没有镜像。
MIRRORS = ((MIRROR_HF, "HuggingFace"),
           (MIRROR_HFM, "hf-mirror 国内镜像"),
           (MIRROR_MS, "魔搭 ModelScope"),
           ("auto", "自动（hf-mirror → HF → 魔搭）"))

#: auto 模式的源尝试顺序（R168 重排，理由见 ``MIRRORS`` 注释）。
_AUTO_ORDER = (MIRROR_HFM, MIRROR_HF, MIRROR_MS)


def _hf_url(m: dict, rel: str) -> str:
    return f"{m['repo']}/{rel}"


def _hfm_url(m: dict, rel: str) -> str:
    # hf-mirror.com 是 HF 的国内镜像，URL 结构与 HF 完全一致（仅换域名）。
    tail = m["repo"].split("huggingface.co/", 1)[-1]
    return f"https://hf-mirror.com/{tail}/{rel}"


def _ms_url(m: dict, rel: str) -> str:
    # 魔搭走文件接口，与 HF 的 /resolve/main/<rel> 不是一套，必须单独拼。
    return ("https://modelscope.cn/api/v1/models/%s/repo"
            "?Revision=master&FilePath=%s" % (m["ms"], rel))


def _mirror_builders():
    return {
        MIRROR_HF: _hf_url,
        MIRROR_HFM: _hfm_url,
        MIRROR_MS: _ms_url,
    }


def mirror_urls(m: dict, rel: str, prefer: str = "auto") -> list[str]:
    """一个文件的候选下载地址（按偏好排序）。

    ⚠ 魔搭走的是**文件接口**（``/api/v1/models/<ns>/<name>/repo?Revision=
    master&FilePath=<rel>``），与 HF 的 ``/resolve/main/<rel>`` 不是一套，
    所以必须按源分别拼 URL，不能只换个域名。
    """
    builders = _mirror_builders()
    order = {
        "auto": list(_AUTO_ORDER),
        MIRROR_HF: [MIRROR_HF],
        MIRROR_HFM: [MIRROR_HFM],
        MIRROR_MS: [MIRROR_MS],
    }.get(str(prefer or "auto").lower(), list(_AUTO_ORDER))
    urls = []
    for k in order:
        if k == MIRROR_MS and not m.get("ms"):
            continue
        try:
            urls.append(builders[k](m, rel))
        except Exception:
            pass
    return urls


def source_of_url(url: str) -> str:
    """把下载地址归类成中文源名（用于进度/错误提示）。"""
    if "hf-mirror.com" in url:
        return "hf-mirror 国内镜像"
    if "modelscope.cn" in url:
        return "魔搭 ModelScope"
    if "huggingface.co" in url:
        return "HuggingFace"
    return "未知来源"


# ---- 代理支持（R149-A）------------------------------------------------
# 软件「代理」设置（cfg.proxy）优先；没设则 urllib 自动走系统/环境变量代理；
# 都没有就直连。main_window / webui 启动时用 cfg 校准一次。
_PROXY: Optional[str] = None


def set_download_proxy(url: Optional[str]) -> None:
    """设置下载代理（http/https 共用同一地址）。传 None/空 = 交给系统代理。"""
    global _PROXY
    _PROXY = (url or "").strip() or None


def _open_url(req: urllib.request.Request):
    """带代理感知地打开请求：设了 cfg 代理就用它，否则 urllib 默认（含系统代理）。

    R168：超时按 URL 分源（HF 直连 15s，其余 30s），并且**统一在这里补 UA**
    —— 所有下载（普通请求 + Range 续传）都过这个出口，一处生效。
    """
    timeout = _timeout_for(getattr(req, "full_url", "") or "")
    if _PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": _PROXY, "https": _PROXY}))
        return opener.open(req, timeout=timeout)
    # 不设代理时也显式走带超时的默认 opener（会自动应用系统/环境变量代理）。
    return urllib.request.urlopen(req, timeout=timeout)


def _download_one(url: str, dest: Path, on_progress: Optional[Callable],
                  cancel: Optional[Callable], source: str = "") -> None:
    """下载单个文件，支持断点续传。写到 .part 再改名，避免半截文件冒充成品。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    # R168：带浏览器式 UA（hf-mirror 对裸 urllib UA 直接 403）。
    # Range 续传请求同样是这个 req，所以一处加、两种请求都生效。
    req = urllib.request.Request(url, headers={"User-Agent": UA_BROWSER})
    if have:
        req.add_header("Range", f"bytes={have}-")
    try:
        r = _open_url(req)
    except Exception as e:
        # 403/超时/连接重置等都转成带源名的中文报错，便于上层判断"换下一来源"。
        raise RuntimeError(
            f"来源 {source or source_of_url(url)} 失败"
            f"（{type(e).__name__}: {e}）") from e
    try:
        # R152-D5：Content-Length 是「期望值」—— 收到字节数与之不符即判
        # 下载中断，**绝不把半截文件 replace 成成品**。旧实现连接一断就
        # 静默成功，残 file 照样过 is_ready 的 >=1KB 检查 = 假就绪。
        # 期望值按模式算：整份重下(200) = cl；续传(206) = cl(剩余)+have。
        # cl=0（chunked 等无 Content-Length 场景）→ total=0 跳过校验。
        cl = int(r.headers.get("Content-Length") or 0)
        mode = "ab" if (have and r.status == 206) else "wb"
        if mode == "wb":
            have = 0
            total = cl
        else:
            total = cl + have
        if cl <= 0:
            total = 0
        done = have
        last = 0.0
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel():
                    return
                chunk = r.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                now = _now()
                if on_progress and now - last > 0.2:
                    on_progress(done, total, source)
                    last = now
        if total and done != total:
            raise RuntimeError(
                f"来源 {source or source_of_url(url)} 下载中断："
                f"收到 {done} 字节，应为 {total} 字节")
    finally:
        try:
            r.close()
        except Exception:
            pass
    part.replace(dest)


def _now() -> float:
    import time
    return time.monotonic()


def download_model(base: Path, model_id: str,
                   on_progress: Optional[Callable] = None,
                   cancel: Optional[Callable] = None,
                   mirror: str = "auto") -> tuple[bool, str]:
    """把清单里某个模型下到 ``base/model_id``。返回 (成功, 说明)。

    **必须在后台线程调用**（几百 MB，堵主线程会卡死界面）。

    ``mirror``：``hf`` / ``hf-mirror`` / ``modelscope`` / ``auto``（默认）。
    auto = **hf-mirror → HF → 魔搭**（R168 重排），一个源失败就换下一个 ——
    国内裸网直连 HF 会超时，把最稳的 hf-mirror 放第一位，下载按钮才不会
    变成一个永远转圈的按钮。
    """
    m = next((x for x in CATALOG if x["id"] == model_id), None)
    if m is None:
        return False, f"清单里没有这个模型：{model_id}"
    dest_dir = model_dir(base, model_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    done_n = 0
    tried_sources: list[str] = []
    src_errors: dict[str, str] = {}
    # R168：记录「换源成功」的轨迹，成功时回一句用户能看懂的说明。
    switched: list[str] = []
    # R152-D5：各文件实证大小（边车 = is_ready 的 Content-Length 期望值
    # 校验依据）。必须在循环**外**声明——放循环内会被每个文件重置，
    # 边车只剩最后一个文件的大小（护栏 test_sidecar_written_after_download
    # 抓到的真 bug）。
    sizes: dict = {}
    for rel in m["files"]:
        if cancel is not None and cancel():
            return False, "已取消"
        target = dest_dir / Path(rel).name

        def _p(d, t, _rel=rel, _src=""):
            if on_progress:
                on_progress(_rel, d, t, _src)
        last_err = ""
        ok = False
        candidates = mirror_urls(m, rel, mirror)
        for idx, url in enumerate(candidates):
            src = source_of_url(url)
            if src not in tried_sources:
                tried_sources.append(src)
            # R152-D5：**换源即重置 .part** —— 不同镜像的同一文件可能不是
            # 同一份字节（镜像同步滞后/改版），跨源 Range 续传会把两份内容
            # 拼成一个「能过大小检查的坏文件」。续传只对**同一源**的重试
            # （再次点「下载」）有意义。
            if idx:
                part = target.with_suffix(target.suffix + ".part")
                try:
                    part.unlink()
                except OSError:
                    pass
            try:
                _download_one(url, target, _p, cancel, source=src)
                ok = True
                if idx and src not in switched:
                    switched.append(src)
                break
            except urllib.error.HTTPError as e:
                last_err = f"{rel}（来源 {src}）失败：HTTP {e.code}"
                src_errors[src] = f"HTTP {e.code}"
            except Exception as e:
                last_err = f"{rel}（来源 {src}）失败：{type(e).__name__}: {e}"
                src_errors[src] = f"{type(e).__name__}: {e}"
        if not ok:
            # R168：失败提示要**中文 + 可操作**，而不是把异常原文摔给用户。
            return False, _fail_hint(rel, last_err, tried_sources, src_errors)
        # R152-D5：记录本文件实证大小（边车 = is_ready 的 Content-Length
        # 期望值校验依据）。个别文件不在盘上（测试替身/异常残留）跳过。
        try:
            sizes[Path(rel).name] = target.stat().st_size
        except OSError:
            pass
        done_n += 1
    if not is_ready(base, model_id):
        # R168：校验不过也顺带查一下是不是「只剩半截 .part」。
        return False, ("下载完成但文件校验没过（onnx 小于 1KB，通常是网络中断）。"
                       "可再点一次「下载」续传，或到设置里填代理后重试。")
    # R152-D5：全部文件就绪后把实证大小写进边车（失败不影响下载结果）。
    try:
        (dest_dir / SIZES_SIDECAR).write_text(
            json.dumps(sizes, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    if switched:
        return True, (f"{m['name']} 已就绪（{done_n} 个文件）"
                      f"；已自动切换到{'、'.join(switched)}成功")
    return True, f"{m['name']} 已就绪（{done_n} 个文件）"


def _fail_hint(rel: str, last_err: str, tried_sources: list[str],
               src_errors: dict[str, str]) -> str:
    """R168：把「哪个源、什么错、下一步怎么办」拼成一句中文可操作提示。"""
    tried = "、".join(tried_sources) or "无可用来源"
    detail = ""
    if src_errors:
        detail = "（" + "；".join(f"{k}: {v}" for k, v in src_errors.items()) + "）"
    hint = ""
    # 全部源都失败时给一条明确的下一步 —— 用户多半是裸网 + HF 被墙。
    if len(tried_sources) >= 3 or not tried_sources:
        hint = "　三源都失败：可到「设置 → 网络代理」填代理后重试。"
    elif any("403" in v for v in src_errors.values()):
        hint = "　被镜像站拒绝（403），请重试或换个下载源。"
    elif any("timeout" in v.lower() or "Timeout" in v for v in src_errors.values()):
        hint = "　连接超时，可到「设置 → 网络代理」填代理后重试。"
    return (f"{rel} 下载失败{detail}｜已试来源：{tried}{hint}"
            if not last_err else
            f"{last_err}{detail}｜已试来源：{tried}{hint}")


class DownloadWorker(threading.Thread):
    """后台下载线程。``result`` 结束后是 (成功, 说明)。"""

    def __init__(self, base: Path, model_id: str,
                 on_progress: Optional[Callable] = None,
                 mirror: str = "auto"):
        super().__init__(daemon=True, name=f"mrm-model-dl-{model_id}")
        self.base = Path(base)
        self.model_id = model_id
        self.on_progress = on_progress
        self.mirror = mirror or "auto"
        self.result: tuple[bool, str] = (False, "未开始")
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self.result = download_model(
                self.base, self.model_id, self.on_progress,
                lambda: self._cancel, mirror=self.mirror)
        except Exception as e:
            self.result = (False, f"{type(e).__name__}: {e}")


def disk_usage(base: Path) -> int:
    """模型目录总占用（字节）；目录不存在返回 0。"""
    base = Path(base)
    if not base.is_dir():
        return 0
    total = 0
    for p in base.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def remove_model(base: Path, model_id: str) -> bool:
    """删除一个本地模型目录。返回是否真的删了。"""
    d = model_dir(base, model_id)
    if not d.is_dir():
        return False
    try:
        shutil.rmtree(d)
        return True
    except Exception:
        return False
