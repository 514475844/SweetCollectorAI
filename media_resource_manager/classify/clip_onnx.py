"""Real local CLIP inference via ONNX Runtime (fully offline at runtime).

Uses the transformers.js style export (e.g. Xenova/clip-vit-base-patch32):
  inputs : input_ids[77], attention_mask[77], pixel_values[3,224,224]
  outputs: image_embeds, text_embeds, logits_per_image, logits_per_text

No torch, no network at inference time. The model file is user-supplied (see
tools/fetch_model.py); if it is absent, callers fall back to the stub tagger.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

import numpy as np

# CLIP normalization constants (OpenAI). Hardcoded to avoid config downloads.
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)


def preprocess(image, size: int = 224) -> np.ndarray:
    """PIL Image -> CHW float32 array, CLIP-style resize + center crop."""
    w, h = image.width, image.height
    scale = size / min(w, h)
    nw, nh = max(size, round(w * scale)), max(size, round(h * scale))
    from PIL import Image
    im = image.convert("RGB").resize((nw, nh), Image.BICUBIC)
    left, top = (nw - size) // 2, (nh - size) // 2
    im = im.crop((left, top, left + size, top + size))
    arr = np.asarray(im, dtype=np.float32) / 255.0
    arr = (arr - MEAN) / STD
    return np.transpose(arr, (2, 0, 1))[None]


def _default_threads() -> int:
    """AI 推理线程数默认值：绝不占满全部核心。

    onnxruntime 默认会按 CPU 核心数开满线程（本机 10 核 20 线程），
    再叠加任务的并发下载，机器会被推理线程拖垮（用户实测「6 并发就卡爆」）。
    这里默认最多 4 线程、且不超过核心数的一半，剩核留给下载与界面。
    """
    import os
    cpu = os.cpu_count() or 4
    return max(1, min(4, cpu // 2))


# ══════════════════════════════════════════════════════════════════
# 2026-10-26：进程级模型会话缓存。
# 真机问题：每个任务启动都 new 一次 LocalClassifier → ClipONNX →
# onnxruntime.InferenceSession（153MB 模型），同步加载约 6.8 秒，而且
# 发生在事件循环线程里 ⇒ 全部开始时任务被**一个一个**卡着启动（用户
# 原话"为什么一个一个启动"）。模型权重与任务无关（只有 labels 不同），
# 会话完全可以复用：这里按 (模型路径, 词表路径, 线程数, gpu) 缓存实例。
# ══════════════════════════════════════════════════════════════════
_CLIP_CACHE: dict = {}
_CLIP_CACHE_LOCK = threading.Lock()


def get_clip_onnx(model_path, tokenizer_path, threads: int = 0,
                  gpu: bool = True, device: str | None = None):
    """取（或首次创建）共享的 ClipONNX 实例。

    相同参数复用同一个 InferenceSession —— 第二个任务起加载耗时≈0。
    并发安全：持锁检查/创建（创建慢，但不持锁做双检会让两个任务同时
    加载同一模型，内存白翻倍）。
    """
    # 2026-10-27: key 必须含 device —— 否则用户从 auto 切到「强制显卡」
    # 会复用原来那个 CPU 会话，开关看起来没生效。
    key = (str(model_path), str(tokenizer_path), int(threads or 0),
           bool(gpu), (device or '').lower())
    with _CLIP_CACHE_LOCK:
        inst = _CLIP_CACHE.get(key)
        if inst is not None:
            return inst
        inst = ClipONNX(Path(model_path), Path(tokenizer_path),
                        threads=threads, gpu=gpu, device=device)
        _CLIP_CACHE[key] = inst
        return inst


class ClipONNX:
    def __init__(self, model_path: Path, tokenizer_path: Path,
                 threads: int = 0, gpu: bool = True,
                 device: str | None = None):
        """
        gpu=True  : 优先 CUDA / DirectML（有独显就走显卡，快得多）；
                    建不成会话再自动回退 CPU（不影响功能）。
        gpu=False : 强制纯 CPU。显卡驱动抽风、显存被别的程序占满、
                    GPU 推理把界面拖卡时用这个（设置 → AI用GPU加速 关掉）。
        """
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # 限制推理线程：一次推理最多用 N 个核，避免多任务并发时线程爆炸
        n = int(threads or 0)
        if n <= 0:
            n = _default_threads()
        n = max(1, min(n, 16))
        try:
            so.intra_op_num_threads = n
            so.inter_op_num_threads = 1
        except Exception:
            pass
        self.threads = n
        self.gpu = bool(gpu)
        self.device = (device or '').lower()
        # V0.0.46：记住模型文件位置 —— 语义搜索给向量打「哪个模型算的」
        # 标签时从这里取目录名（clip-vit-b32 等）。
        try:
            self.model_path = Path(model_path)
        except Exception:                              # noqa: BLE001
            self.model_path = None
        # 2026-10-27 实测：bundled 的 CLIP-B32 量化模型（153MB）在 DirectML 上
        # 5.6s/张、CPU 只要 0.37s/张 —— 小模型在显卡上"分片+搬运"开销远大于收益。
        # 所以 auto 档按**模型体积**判：<200MB 用小模型策略（CPU 更快），
        # >200MB 视为大模型（显卡更划算）。强制档不受此限。
        try:
            self._small_model = Path(model_path).stat().st_size < 200 * 1024 * 1024
        except Exception:
            self._small_model = True
        providers = self._pick_providers(ort, self.gpu, self.device,
                                        self._small_model)
        try:
            self.sess = ort.InferenceSession(str(model_path), so,
                                             providers=providers)
            self.provider = providers[0]
        except Exception:
            # 有些环境（如缺 CUDA 运行时的容器）声明了后端却建不了会话 → 回退 CPU
            self.sess = ort.InferenceSession(str(model_path), so,
                                             providers=["CPUExecutionProvider"])
            self.provider = "CPUExecutionProvider"
        self.input_names = {i.name for i in self.sess.get_inputs()}
        self.output_names = [o.name for o in self.sess.get_outputs()]
        self._tok = self._load_tokenizer(tokenizer_path)
        self.seq_len = 77
        # 2026-10-29：文本嵌入缓存（词表 → 嵌入矩阵）。分类时每张图都会问
        # 一次同一份词表，缓存命中即 0 开销。_text_pixel_batch1 见 encode_text。
        self._text_cache: dict = {}
        self._text_lock = threading.Lock()
        self._text_pixel_batch1 = True

    @staticmethod
    def _pick_providers(ort, gpu: bool = True,
                        device: str | None = None,
                        small_model: bool = True) -> list[str]:
        """跨平台选择执行后端（不硬编码单一平台）。

        gpu=True（默认，设置里「AI 用 GPU 加速」开着）：
          - NVIDIA 独显（如 GTX 1080）→ CUDA 最快，优先；
          - Windows 其它显卡 / 核显 → DirectML；
          - Linux、ARM docker 等没有上面两种后端 → CPU。
        gpu=False（开关关掉＝强制纯 CPU）：
          只给 CPUExecutionProvider。显卡驱动抽风、显存被别的程序占满、
          或 GPU 推理把界面拖卡时，用它兜底，行为完全可预期。
        """
        avail = set(ort.get_available_providers())
        # 2026-10-27：四档 —— auto / gpu / npu / cpu（强制）
        mode = (device or "").lower().strip()
        # NPU 后端（各厂商各一套，装了哪个用哪个）：
        #   QNNExecutionProvider    高通骁龙 NPU（ARM 设备 / 安卓 / Windows on ARM）
        #   VitisAIExecutionProvider AMD Ryzen AI（XDNA NPU）
        #   OpenVINOExecutionProvider 英特尔（NPU / 核显 / CPU 都能跑）
        npu_pref = ["QNNExecutionProvider", "VitisAIExecutionProvider",
                    "OpenVINOExecutionProvider"]
        # auto 模式下老开关 gpu=False 仍然表示"强制 CPU"（兼容旧配置）
        if mode == "cpu" or ((not mode or mode == "auto") and not gpu):
            return ["CPUExecutionProvider"]
        gpu_pref = ["CUDAExecutionProvider", "DmlExecutionProvider"]
        if mode == "npu":
            # 强制 NPU：只给 NPU 后端（没有就只剩 CPU，调用方会标降级）
            chosen = [p for p in npu_pref if p in avail]
            return chosen or ["CPUExecutionProvider"]
        if mode == "gpu":
            # 强制显卡：只给显卡 provider（没有就只剩 CPU，调用方会标 degraded）
            chosen = [p for p in gpu_pref if p in avail]
            return chosen or ["CPUExecutionProvider"]
        # auto（默认）：小模型走 CPU（实测快 15 倍），大模型才上显卡/NPU
        if mode in ("", "auto") and small_model:
            return ["CPUExecutionProvider"]
        # auto 且有大模型：显卡优先，其次 NPU，最后 CPU
        pref = gpu_pref + npu_pref + ["CPUExecutionProvider"]
        chosen = [p for p in pref if p in avail]
        return chosen or ["CPUExecutionProvider"]

    @staticmethod
    def _load_tokenizer(path: Path):
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(str(path))
        tok.enable_truncation(max_length=77)
        tok.enable_padding(length=77)
        return tok

    def encode_text(self, texts: list[str]) -> np.ndarray:
        """文本嵌入（**带缓存**）。

        ⚠ 2026-10-29 真机实测（这台 Xeon + bundled B32 量化模型）：
        这个融合图要求三路输入同时喂，原来的写法按 `len(texts)` 开了一个
        ``(bs, 3, 224, 224)`` 的零数组，于是**每个主题词都要跑一遍完整 ViT**。
        耗时与词数严格线性：1 词 60ms、8 词 371ms、20 词 940ms、40 词 1980ms。
        而分类是**每张图都调一次**，40 个词 ⇒ 单张 2231ms，10 万张要 65 小时。

        零图只是个占位符，文本分支根本不用它 → 两处一起改：
          ① ``pixel_values`` 固定 batch=1（跑一次就够，不再按词数翻倍）；
          ② 结果按词表缓存 —— 同一任务的词表整轮不变，只需算一次。
        实测单张 2231ms → 84ms。

        万一某个导出的图不允许两路 batch 不一致（run 抛异常），
        自动退回「按词数开零图」的老写法并记住，功能不受影响 —— 只是慢回去。
        """
        key = tuple(texts)
        with self._text_lock:
            hit = self._text_cache.get(key)
        if hit is not None:
            return hit

        ids, mask = [], []
        for t in texts:
            enc = self._tok.encode(t)
            ids.append(enc.ids)
            mask.append(enc.attention_mask)
        feeds = {
            "input_ids": np.array(ids, dtype=np.int64),
            "attention_mask": np.array(mask, dtype=np.int64),
        }
        if "pixel_values" in self.input_names:
            bs = 1 if self._text_pixel_batch1 else len(texts)
            feeds["pixel_values"] = np.zeros((bs, 3, 224, 224), dtype=np.float32)
        try:
            out = self.sess.run(None, feeds)
        except Exception:
            if not self._text_pixel_batch1:
                raise
            # 这个图不接受 batch 不一致：退回老写法，并且以后都别再试
            self._text_pixel_batch1 = False
            feeds["pixel_values"] = np.zeros((len(texts), 3, 224, 224),
                                             dtype=np.float32)
            out = self.sess.run(None, feeds)
        emb = dict(zip(self.output_names, out))["text_embeds"]
        emb = emb / np.linalg.norm(emb, axis=-1, keepdims=True)
        with self._text_lock:
            if len(self._text_cache) >= 32:      # 词表数量有限，留够余量即可
                self._text_cache.clear()
            self._text_cache[key] = emb
        return emb

    def encode_image(self, image) -> np.ndarray:
        feeds = {"pixel_values": preprocess(image)}
        if "input_ids" in self.input_names:
            feeds["input_ids"] = np.zeros((1, 77), dtype=np.int64)
            feeds["attention_mask"] = np.zeros((1, 77), dtype=np.int64)
        out = self.sess.run(None, feeds)
        emb = dict(zip(self.output_names, out))["image_embeds"]
        return emb / np.linalg.norm(emb, axis=-1, keepdims=True)

    def similarity(self, image, texts: list[str]) -> list[float]:
        ie = self.encode_image(image)
        te = self.encode_text(texts)
        # cosine sim already normalized; CLIP also uses a learned logit scale
        sims = (ie @ te.T)[0]
        return [float(s) for s in sims]

    @classmethod
    def available(cls, model_path: Path, tokenizer_path: Path) -> bool:
        return Path(model_path).exists() and Path(tokenizer_path).exists()
