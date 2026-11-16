"""Local image classifier facade.

Backend priority:
  1. Real local CLIP via ONNX Runtime (if model files are present) -> true
     text/image similarity so you can filter by free-text theme/keyword.
  2. A single user-supplied ONNX classifier (labels list) -> tag scoring.
  3. Stub: reads tags from the download metadata sidecar / filename.

Everything runs locally; nothing is uploaded. Falls through gracefully so the
pipeline keeps working even with no model installed.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

# ══════════════════════════════════════════════════════════════════
# 2026-10-30 治本：CLIP 打分口径。
#
# 真机实测（NAS l14 量化模型 + 35 张真实图片，逐项量过）：
#   · 图像分支正常：同图自比 1.000、不同图 0.61~0.65；
#   · 文本分支正常：同义词 0.90~0.94、无关词 0.59~0.78；
#   · **跨模态余弦只有 +0.12 ~ +0.28**，跨度 0.06 左右。
#
# 而原来的写法是 `(cos + 1) / 2` —— 把 0.12~0.28 线性映射到 0.56~0.64，
# 于是**每一张图都同时命中全部标签**（16 个标签全过 0.35），标签等于没打。
# 这不是阈值高低的问题：实测正负样本 AUC≈0.47（等同于随机），
# 无论把阈值调到 0.6 还是 0.9，只是在"全中/全不中"之间翻转。
#
# 根因是**丢掉了 CLIP 的 logit_scale**。CLIP 是对比学习训练的，它天生
# 要用 `cos * logit_scale`（原版训练出来的 scale≈100）把余弦那点差异放大，
# 再做 softmax 才是它的正常输出。丢掉这 100 倍放大，等于把模型当成了
# "余弦值 = 概率"，可余弦根本不是概率。
#
# 改法就是回到 OpenAI 原版 zero-shot 的两步：
#   ① cos * logit_scale（放大差异）
#   ② softmax（跨标签归一化成概率）
# 实测效果（同一批图上）：目标标签均值 0.718、10 个中性锚点被压到
# 0.001~0.09，组内极差从旧方案的 0.011 提升到 0.65 —— 这才叫能区分。
# ══════════════════════════════════════════════════════════════════
CLIP_LOGIT_SCALE = 100.0

# 中性锚点：softmax 的"背景分母"。
#
# 为什么必须要它：**单标签时 softmax(cos*scale) 数学上恒等于 1.0**
# （exp(a)/exp(a)=1），等于每张图都 100% 命中——用户 25 个任务恰好全是
# 单标签 ["NSFW"]，不改这一条就会从"全中"变成"全中且是 1.0"，更糟。
# 锚点是一批与用户主题无关的通用描述，把分母撑开，让"命中/不命中"
# 有一个真实的可比对象。锚点本身的分数会被压得很低，不影响判定。
CLIP_ANCHORS = (
    "a photo of a person wearing clothes",
    "a photo of a landscape",
    "a photo of food",
    "a photo of a vehicle",
    "a photo of a building",
    "a screenshot of text",
    "a photo of an animal",
    "a photo of an indoor room",
    "a close-up product photograph",
    "a photo of an object on a table",
)

# CLIP 官方 zero-shot 的提示模板。裸词（尤其 NSFW 这种缩写）直接送进
# 文本编码器几乎没有有效语义——CLIP 的文本塔是在**自然语言句子**上训的。
# 实测裸 "NSFW" 的跨模态分数与随机无异；套上模板才有区分度。
_PROMPT_PREFIX = "a photo of "


def clip_promptize(label: str) -> str:
    """把用户填的词变成 CLIP 能理解的提示句。

    已经是完整句子的原样送；裸词/缩写套 CLIP 官方模板。
    中文原样送 —— 加英文前缀会把语义带偏（中英桥接由 labels_zh 负责）。
    """
    s = (label or "").strip()
    if not s:
        return s
    # 含 CJK 就原样，别乱加英文前缀
    if any("一" <= ch <= "鿿" for ch in s):
        return s
    low = s.lower()
    if low.startswith(("a photo of ", "an photo of ", "a photograph of ",
                       "the ", "a ", "an ", "this is ", "a close-up ",
                       "a screenshot of ")):
        return s
    # 三个词以上的视为已经是描述了（"white stockings" 是两个词，要套模板）
    if len(s.split()) >= 3:
        return s
    return _PROMPT_PREFIX + s


def clip_scores(raw_cos, labels, scale: float = CLIP_LOGIT_SCALE):
    """余弦 -> 0..1 概率（CLIP 官方口径：cos*scale 后 softmax）。

    `raw_cos` 是**已经带上锚点**的余弦数组，长度 = len(labels) + len(ANCHORS)。
    返回长度 = len(labels) 的概率（锚点只当分母，不返回给调用方）。
    """
    import numpy as np
    v = np.asarray(raw_cos, dtype=np.float64)
    if v.size == 0:
        return []
    x = v * float(scale)
    x = x - x.max()                     # 数值稳定：减最大值再 exp
    e = np.exp(x)
    p = e / e.sum()
    return [float(x) for x in p[:len(labels)]]


# 全局推理闸门：同时只允许 N 张图在做 AI 推理。
# 本机是纯 CPU（Xeon 无核显），CLIP 推理很重；多任务并发时如果不限制，
# 每张图都开满线程会把机器拖到卡死（用户实测：6 并发直接卡爆）。
_INFER_SLOTS = max(1, min(2, (os.cpu_count() or 4) // 4))
_infer_sem = threading.Semaphore(_INFER_SLOTS)


class LocalClassifier:
    def __init__(self, model_path: Optional[Path] = None,
                 labels: Optional[list[str]] = None,
                 model_dir: Optional[Path] = None,
                 tokenizer_path: Optional[Path] = None,
                 threads: int = 0, gpu: bool = True,
                 device: str | None = None):
        self.model_path = Path(model_path) if model_path else None
        self.model_dir = Path(model_dir) if model_dir else None
        self.labels = labels or []
        self._threads = int(threads or 0)
        self._gpu = bool(gpu)
        # auto / gpu（强制）/ cpu（强制）；空串 = 按 gpu 布尔走老逻辑
        self._device = (device or '').lower()
        self._clip = None
        self._session = None
        self._backend = "stub"
        # 2026-10-29：**回退必须说出来**。真机踩到过：配置里写的是
        # clip-vit-l14，但那个目录只有 config.json/merges.txt（没有权重、
        # 没有 tokenizer），于是每次都在静默回退到 bundled 的 B32 ——
        # 用户以为在用大模型，其实一直是最小的那个，界面上一个字都没有。
        # 现在把「为什么回退、退到了哪」记在 model_note 上，调用方（任务备注 /
        # 从文件夹学习的结果）会把它带给用户，同时写一条 WARNING 日志。
        self.model_note = ""
        self._pick_backend()

    # -------- backend selection --------
    def _pick_backend(self):
        # R47/B6b：依次尝试多个模型目录，任一成功即用，不再硬降级成 stub。
        #
        # 真机现象：任务启动就提示「本地 AI 处于降级状态」（标签为空）。
        # 根因：这里**只认** `user_data_dir()/models/clip/`，而 onedir 部署下
        # 那个目录要么不存在、要么缺 `tokenizer.json`，于是 ClipONNX 构造
        # 抛异常 → 直接 `return` 落到 stub。用户明明随软件装了 153MB 的
        # CLIP，却一直显示降级。
        #
        # 修法（作业单 B6b 原文「优先用 bundled_models 回退根除」）：
        # 候选顺序 = ① 显式传入的 model_dir ② 用户数据目录 ③ **包内
        # bundled_models/clip-vit-b32**（resolve_default_model_dir 已经会
        # 按「捆绑 → 已下载」找好）。任一能构造成功就用它。
        # 不靠部署时手动拷文件 —— 那是治标。
        cands = []
        if self.model_dir is not None:
            cands.append(Path(self.model_dir))
        try:
            from ..config import user_data_dir
            cands.append(user_data_dir() / "models" / "clip")
        except Exception:
            pass
        try:
            from ..models_lib import resolve_default_model_dir
            base = None
            try:
                from ..config import user_data_dir
                # R70：base 必须是**模型根目录**，不是数据目录。
                # resolve_default_model_dir 里 model_dir(base,id)==base/id，
                # UI 侧 _models_base_dir() 给的也是 user_data_dir()/"models"。
                # 原来这里传user_data_dir() 少一级 ⇒ 已下载的模型落在
                # <data>/models/clip-vit-b32，回退却去找 <data>/clip-vit-b32，
                # 于是「明明下载过 CLIP」仍被判成降级（实测见
                # tools/r70_probe_fallback_paths.py）。
                base = user_data_dir() / "models"
            except Exception:
                base = None
            d = resolve_default_model_dir(base) if base is not None else None
            if d is not None:
                cands.append(Path(d))
        except Exception:
            pass

        seen = set()
        for md in cands:
            try:
                key = str(md)
            except Exception:
                continue
            if key in seen:
                continue
            seen.add(key)
            if self._try_clip_dir(md):
                self._backend = "clip"
                # 用户显式配的那个目录没建成、最后用了别的 ⇒ 必须留话
                if self.model_dir is not None and str(md) != str(self.model_dir):
                    self.model_note = (
                        "配置的 AI 模型目录不可用（缺 model_quantized.onnx 或 "
                        "tokenizer.json），已回退到：%s" % md)
                    try:
                        import logging
                        logging.getLogger("mrm.ai").warning(self.model_note)
                    except Exception:
                        pass
                return

        # CLIP 都没起来，才尝试「单个用户 onnx 分类器」，最后退 stub
        if self.model_path and self.model_path.exists():
            try:
                import onnxruntime as ort  # noqa
                self._session = ort.InferenceSession(str(self.model_path))
                self._backend = "onnx"
            except Exception:
                self._backend = "stub"
        else:
            self._backend = "stub"

    def _try_clip_dir(self, md) -> bool:
        """试着用 `md` 构造 CLIP。成功返回 True（并装好 self._clip）。"""
        try:
            md = Path(md)
            if not md.is_dir():
                return False
            cand_model = next((md / n for n in ("model_quantized.onnx", "model.onnx")
                               if (md / n).exists()), None)
            cand_tok = md / "tokenizer.json"
            if not (cand_model and cand_tok.exists()):
                return False
            # 2026-10-26：走进程级缓存工厂（同参数复用会话），
            # 不再每个任务重复加载 153MB 模型（每次 ~6.8s 并阻塞事件循环）。
            from .clip_onnx import get_clip_onnx
            self._clip = get_clip_onnx(cand_model, cand_tok,
                                       threads=self._threads, gpu=self._gpu,
                                       device=self._device)
            return True
        except Exception:
            return False

    @property
    def backend(self) -> str:
        return self._backend

    # -------- public API --------
    def classify(self, image_path: Path) -> list[tuple[str, float]]:
        """Return [(label, score in 0..1), ...] sorted desc."""
        if self._backend == "clip":
            return self._classify_clip(image_path)
        if self._backend == "onnx":
            return self._classify_onnx(image_path)
        return self._classify_stub(image_path)

    def similarity(self, image_path: Path, query: str) -> float:
        """Text<->image similarity in 0..1 (CLIP only; 0.0 without it)."""
        if self._backend != "clip":
            return 0.0
        try:
            pil = self._open(image_path)
            if pil is None:
                return 0.0
            # 推理闸门：多任务并发时排队做推理，不让 CPU 被同时打满
            with _infer_sem:
                sim = self._clip.similarity(pil, [query])[0]
            # CLIP cosine spans roughly [-1,1]; map to 0..1 for thresholds.
            return max(0.0, min(1.0, (sim + 1.0) / 2.0))
        except Exception:
            return 0.0

    def query_filter(self, image_path: Path, theme: str,
                     threshold: float = 0.5) -> tuple[bool, float]:
        """Does the image match `theme` above `threshold`? Returns (ok, score)."""
        s = self.similarity(image_path, theme)
        return (s >= threshold), s

    # ---- V0.0.46 · 语义搜索：图片向量 ----
    @property
    def model_id(self) -> str:
        """当前真正在用的模型档位标识（目录名，如 clip-vit-b32）。

        向量必须和它一起存：换了档位，新旧向量维度/语义空间都对不上，
        搜索时只信 `clip_model` 等于当前 id 的那些向量。
        """
        try:
            if self._clip is not None:
                return Path(self._clip.model_path).parent.name
        except Exception:                              # noqa: BLE001
            pass
        return ""

    def encode_image_vec(self, image_path: Path):
        """图片 → 归一化向量（float32 numpy）。后端不是 CLIP 时返回 None。"""
        if self._backend != "clip":
            return None
        pil = self._open(image_path)
        if pil is None:
            return None
        try:
            with _infer_sem:
                return self._clip.encode_image(pil)[0]
        except Exception:                              # noqa: BLE001
            return None

    def classify_with_vec(self, image_path: Path):
        """一次推理同时返回 (标签对, 图片向量)。

        打标流水线专用 —— 向量本来就是这次推理的中间产物，顺手带出来
        零成本；分开调等于每张图跑两遍 ViT。CLIP 后端以外（onnx/stub）
        向量给 None，调用方照常只拿标签。
        """
        if self._backend != "clip":
            return self.classify(image_path), None
        pil = self._open(image_path)
        if pil is None:
            return [], None
        if not self.labels:
            with _infer_sem:
                ie = self._clip.encode_image(pil)[0]
            return [], ie
        prompts = [clip_promptize(l) for l in self.labels]
        all_prompts = prompts + list(CLIP_ANCHORS)
        with _infer_sem:
            ie = self._clip.encode_image(pil)[0]
            te = self._clip.encode_text(all_prompts)
            raw = (ie @ te.T)
        probs = clip_scores(raw, self.labels, CLIP_LOGIT_SCALE)
        pairs = sorted(zip(self.labels, probs), key=lambda x: -x[1])
        return pairs, ie

    # -------- internals --------
    @staticmethod
    def _open(path):
        try:
            from PIL import Image
            from ..dedup.hash import _bound_decode, DECODE_MAX_SIDE
            im = Image.open(path)
            # R152-D3：CLIP 只需 224×224，但原来 .convert("RGB") 会把 100MP
            # 原图整张解码进内存（~1GB，并发 2 直接拖死机器）。先 draft+thumbnail
            # 把解码峰值钉在 DECODE_MAX_SIDE 内，再 convert。
            _bound_decode(im, DECODE_MAX_SIDE)
            return im.convert("RGB")
        except Exception:
            return None

    def _classify_clip(self, image_path: Path) -> list[tuple[str, float]]:
        pil = self._open(image_path)
        if pil is None or not self.labels:
            return []
        # 2026-10-30 治本：一次图像推理 + 一次文本编码（文本有进程级缓存），
        # 余弦带上中性锚点后按 CLIP 官方口径 softmax 成概率。
        # 原来这里用 self._clip.similarity(pil, self.labels) 拿裸余弦再
        # 线性映射 (cos+1)/2 —— 丢掉 logit_scale，实测导致"每张图命中全部
        # 标签"（详见顶部 CLIP_LOGIT_SCALE 段的长注释）。
        prompts = [clip_promptize(l) for l in self.labels]
        all_prompts = prompts + list(CLIP_ANCHORS)
        with _infer_sem:      # 同上：并发时排队推理，不把 CPU 打满
            ie = self._clip.encode_image(pil)[0]
            te = self._clip.encode_text(all_prompts)
            raw = (ie @ te.T)
        probs = clip_scores(raw, self.labels, CLIP_LOGIT_SCALE)
        pairs = [(l, p) for l, p in zip(self.labels, probs)]
        # R152-D4：**不再截断 top-10**。调用方（engine._apply_ai /
        # _ai_exclude 的 "all" 匹配模式）用 all(s>=thr) 做全词表判定，
        # 截断成 10 条等于只看了分数最高的 10 个词 → all 模式语义失真
        # （低排位词不达标也被当成达标）。词表量级 ~40/层，全量返回无压力。
        return sorted(pairs, key=lambda x: -x[1])

    def _classify_onnx(self, image_path: Path) -> list[tuple[str, float]]:
        import numpy as np
        pil = self._open(image_path)
        if pil is None:
            return []
        arr = np.asarray(pil.convert("RGB").resize((224, 224))).astype("float32") / 255.0
        arr = np.transpose(arr, (2, 0, 1))[None]
        out = self._session.run(None, {"input": arr})[0][0]
        scores = out.tolist() if hasattr(out, "tolist") else list(out)
        pairs = sorted(zip(self.labels or [str(i) for i in range(len(scores))],
                           scores), key=lambda x: -x[1])
        # 同 _classify_clip：R152-D4 不截断，all 模式要看到全量标签
        return pairs

    def _classify_stub(self, image_path: Path) -> list[tuple[str, float]]:
        import json
        sidecar = Path(str(image_path) + ".json")
        tags: list[str] = []
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    tags = data.get("tags", []) or []
                elif isinstance(data, list):
                    tags = data
                elif isinstance(data, str):
                    tags = [data]
            except Exception:
                tags = []
        if isinstance(tags, str):
            tags = [tags]
        return [(str(t), 1.0) for t in tags] + [(Path(image_path).stem, 0.0)]
