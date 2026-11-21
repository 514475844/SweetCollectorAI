"""本地看图说话（BLIP image captioning）推理——纯 onnxruntime + numpy。

V0.0.74 接入：`auto_vocab` 的 local 方式调本模块对每张图生成一句英文描述，
再拆成候选词统计。设计约束（打包环境只有 onnxruntime/PIL，没有 torch）：

- 模型文件 = optimum 导出的标准布局：``vision_encoder.onnx`` +
  ``text_decoder.onnx`` + ``vocab.txt`` + ``config.json``（同目录）。
  导出产物随模型包放在 ``data/models/blip-image-captioning-base/``。
- 解码用**无 KV-cache 的整序列重算**（text_decoder.onnx 一次吃全部
  已生成 token）。比 with_past 慢，但实现简单、不易错；单图几个 token，
  慢可接受——用户原话「哪怕 10 分钟蹦一个词，也证明功能正常」。
- 任何一步失败（缺文件、坏图）都抛异常，由调用方逐图兜底，
  绝不让整批扫描白跑。

预处理与 BLIP 官方一致：短边缩放到 384、中心裁剪、/255 后 (x-0.5)/0.5，
CHW、fp32。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

# 描述里这些词没有信息量（停用词 + BLIP 高频套话），拆词时直接丢。
_STOP = {
    "a", "an", "the", "of", "in", "on", "at", "with", "and", "or", "to",
    "is", "are", "there", "this", "that", "image", "photo", "picture",
    "close", "up", "shot", "view", "some", "one", "two", "three", "many",
    "small", "large", "big",
}
# ⚠ 白/黑单独留着其实有价值（white/black stockings 语境），但裸 "white"
# 多是背景色。折中：裸颜色词丢弃，组合词（如 white stockings）由短语
# 合并逻辑保留。
_STOP |= {"background", "front", "next", "top", "bottom", "side", "while",
          "white", "black", "red", "blue", "green", "yellow", "pink",
          "gray", "grey", "brown", "dark", "light"}
# BLIP 套话：动作/构图虚词，不是画面主体（实测描述高频出现）。
_STOP |= {"for", "camera", "posing", "sitting", "standing", "laying",
          "lying", "wearing", "holding", "looking", "smiling", "wears",
          "sits", "stands", "lays", "holds", "looks", "smiles", "is",
          "being", "next", "toward", "against", "across", "down", "over",
          "under", "near", "behind", "around", "between", "into", "out",
          "off", "about", "such", "very", "most", "more", "other",
          "both", "each", "only", "just", "also", "here", "where",
          # 裸形容词碎片（long hair 会整体命中短语，剩 single long 无意义）
          "young", "old", "long", "short", "pretty", "beautiful",
          "cute", "hot", "full", "half", "close", "wide", "low", "high",
          # 代词/所有格（实测 'a woman and her...' 高频出 her）
          "her", "his", "their", "its", "my", "our", "your"}

# 描述里常见的短语整体保留："white stockings" 当一个词，不拆碎。
# 顺序敏感：长短语在前，避免 "white stockings" 被 "stockings" 抢先。
_PHRASES = [
    "white stockings", "black stockings", "white pantyhose",
    "black pantyhose", "fishnet stockings", "white underwear",
    "black underwear", "school uniform", "wedding dress", "summer dress",
    "long hair", "short hair", "high heels", "bikini top",     "swimming pool", "on the beach", "in the water", "by the lake",
    "sitting on the bed", "long legs", "black hair", "blonde hair",
    "brown hair", "silver hair", "red hair",
]

# 短语 → 中文显示名（勾选界面与口味库同口径；查不到就保留英文原词，
# 用户照样看得懂要不要）。
PHRASE_ZH = {
    "white stockings": "白丝", "black stockings": "黑丝",
    "white pantyhose": "白连裤袜", "black pantyhose": "黑连裤袜",
    "fishnet stockings": "网袜", "white underwear": "白色内衣",
    "black underwear": "黑色内衣", "school uniform": "校服",
    "wedding dress": "婚纱", "summer dress": "夏装",
    "long hair": "长发", "short hair": "短发",
    "high heels": "高跟鞋", "bikini top": "比基尼",
    "swimming pool": "泳池", "on the beach": "海边",
    "in the water": "水中", "by the lake": "湖边",
    "sitting on the bed": "床上", "black hair": "黑发",
    "blonde hair": "金发", "brown hair": "棕发",
    "silver hair": "银发", "red hair": "红发", "long legs": "长腿",
}


class BlipCaptioner:
    """加载 BLIP 导出目录，对图片生成英文描述。线程不安全：单例串行用。"""

    def __init__(self, model_dir: Path):
        self.dir = Path(model_dir)
        self._vision_path, self._decoder_path = self._locate()
        # 延迟导入：只有真正用 local 方式才依赖 onnxruntime。
        import numpy as np  # noqa: F401
        import onnxruntime as ort

        # DirectML EP 优先（和 CLIP 同一套 providers 顺序），失败回落 CPU。
        providers = []
        try:
            if "DmlExecutionProvider" in ort.get_available_providers():
                providers.append("DmlExecutionProvider")
        except Exception:
            pass
        providers.append("CPUExecutionProvider")

        self._vision = ort.InferenceSession(
            str(self._vision_path), providers=providers)
        self._decoder = ort.InferenceSession(
            str(self._decoder_path), providers=providers)
        with open(self.dir / "vocab.txt", encoding="utf-8") as f:
            self._vocab = [ln.strip() for ln in f if ln.strip() != ""]
        self._id2tok = {i: t for i, t in enumerate(self._vocab)}
        self.cfg = json.loads((self.dir / "config.json").read_text("utf-8"))
        # BLIP 文本解码器是 BERT 词表 + 特殊 [DEC] 开头：
        # bos=30522（[DEC]）、eos=2（[SEP]）、pad=0 —— 以配置为准，兜底默认。
        tc = self.cfg.get("text_config", {})
        self._bos = int(tc.get("bos_token_id") or 30522)
        self._eos = int(tc.get("eos_token_id") if tc.get("eos_token_id") is not None else 2)
        self._pad = int(tc.get("pad_token_id") or 0)
        # CLIP 风格归一化参数（BlipImageProcessor 默认值）
        pc = self._preproc_cfg()
        self._mean = pc.get("image_mean", [0.48145466, 0.4578275, 0.40821073])
        self._std = pc.get("image_std", [0.26862954, 0.26130258, 0.27577711])

    def _preproc_cfg(self) -> dict:
        p = self.dir / "preprocessor_config.json"
        try:
            if p.exists():
                return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
        return {}

    def _locate(self) -> tuple[Path, Path]:
        """找两个 ONNX：兼容不同导出命名（vision*/text*decoder*.onnx）。"""
        def _pick(*keys):
            for p in sorted(self.dir.glob("*.onnx")):
                low = p.name.lower()
                if all(k in low for k in keys):
                    return p
            return None
        vis = _pick("vision") or _pick("encoder")
        dec = _pick("text", "decoder") or _pick("decoder")
        # 特殊导出（split_0/split_1 那种）靠大小猜：视觉小、解码大。
        if vis is None or dec is None:
            onnx = sorted(self.dir.glob("*.onnx"),
                          key=lambda p: p.stat().st_size)
            if len(onnx) >= 2:
                vis, dec = onnx[0], onnx[-1]
        if vis is None or dec is None or vis is dec:
            raise FileNotFoundError(
                f"描述模型目录 {self.dir} 里找不到视觉编码/文本解码两个 onnx")
        for extra in ("vocab.txt", "config.json"):
            if not (self.dir / extra).exists():
                raise FileNotFoundError(
                    "描述模型文件不全，缺：" + extra + f"（目录 {self.dir}）")
        return vis, dec

    # ── 预处理 ──────────────────────────────────────────────────────
    def _preprocess(self, path):
        import numpy as np
        from PIL import Image
        size = (self.cfg.get("vision_config", {})
                .get("image_size", 384))
        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            # 短边缩放 + 中心裁剪（与 BLIP 官方一致）
            scale = size / min(w, h)
            nw, nh = int(round(w * scale)), int(round(h * scale))
            im = im.resize((nw, nh), Image.BICUBIC)
            l = (nw - size) // 2
            t = (nh - size) // 2
            im = im.crop((l, t, l + size, t + size))
            arr = np.asarray(im, dtype="float32") / 255.0
        mean = np.asarray(self._mean, dtype="float32")
        std = np.asarray(self._std, dtype="float32")
        arr = (arr - mean) / std
        arr = arr.transpose(2, 0, 1)[None]  # CHW, batch=1
        return arr

    # ── 分词（BertTokenizer 的 WordPiece 最小子集）─────────────────
    def _tokenize(self, text: str):
        # 中文词间补空格（BERT 惯例），再按标点切
        text = re.sub(r"([\u4e00-\u9fff])", r" \1 ", text)
        text = re.sub(r"([^\w\s\u4e00-\u9fff])", r" \1 ", text)
        ws = []
        for w in text.lower().split():
            if len(w) <= 100:
                ws.append(w)
        # WordPiece
        out = []
        for w in ws:
            start_i = 0
            ok = True
            pieces = []
            while start_i < len(w):
                end_i = len(w)
                cur = None
                while start_i < end_i:
                    sub = w[start_i:end_i]
                    cand = sub if start_i == 0 else "##" + sub
                    if cand in self._tokset():
                        cur = cand
                        break
                    end_i -= 1
                if cur is None:
                    ok = False
                    break
                pieces.append(cur)
                start_i = end_i
            if ok:
                out.extend(pieces)
        return out

    def _tokset(self):
        if not hasattr(self, "__tokset"):
            self.__tokset = set(self._vocab)
        return self.__tokset

    # ── 生成 ────────────────────────────────────────────────────────
    def caption(self, path, max_new: int = 20) -> str:
        import numpy as np
        px = self._preprocess(path)
        vname = self._vision.get_inputs()[0].name
        feats = self._vision.run(None, {vname: px})[0]  # (1, S, D)

        # 无 KV-cache：每步把完整 token 序列重喂 text_decoder。
        # BLIP 导出（text_decoder.onnx）输入 = input_ids, encoder_hidden_states。
        ids = [self._bos]
        in_names = [i.name for i in self._decoder.get_inputs()]
        for _ in range(max_new):
            feed = {"encoder_hidden_states": feats.astype("float32"),
                    "input_ids": np.asarray([ids], dtype="int64")}
            # 兼容不同命名：按位置兜底填 input_ids / hidden
            if "input_ids" not in in_names:
                feed = {in_names[0]: np.asarray([ids], dtype="int64"),
                        in_names[1]: feats.astype("float32")}
            logits = self._decoder.run(None, feed)[0]  # (1,L,V)
            nxt = int(np.argmax(logits[0, -1]))
            if nxt == self._eos:
                break
            ids.append(nxt)
        toks = [self._id2tok.get(i, "") for i in ids[1:]]
        text = " ".join(t for t in toks
                        if t and t not in ("[CLS]", "[SEP]", "[PAD]"))
        return text.strip()


def extract_words(caption: str) -> list[str]:
    """描述句 → 候选词列表（短语优先整体保留，再单词，小写、去停用词）。

    能反查到中文的词返回中文（和你口味库里的词同一口径），
    查不到的保留英文原词——让用户在勾选界面自己判断要不要。
    """
    from .labels_zh import EN2ZH
    low = caption.lower()
    out: list[str] = []
    matched_spans: list[tuple[int, int]] = []

    def _covered(pos: int, ln: int) -> bool:
        return any(s <= pos + ln - 1 and e >= pos for s, e in matched_spans)

    for ph in _PHRASES:
        start = 0
        while True:
            i = low.find(ph, start)
            if i < 0:
                break
            # 词边界：前后不能再接字母（防 "white" 误配 "whitewash"）
            before_ok = i == 0 or not low[i - 1].isalnum()
            j = i + len(ph)
            after_ok = j >= len(low) or not low[j].isalnum()
            if before_ok and after_ok:
                out.append(ph)
                matched_spans.append((i, j - 1))
            start = j

    # 去掉标点再切单词（逐字符替换为空格，clean 与 low 位置一一对应）
    clean = re.sub(r"[^a-z0-9\s]", " ", low)
    for m in re.finditer(r"[a-z0-9]+", clean):
        w = m.group()
        if w in _STOP or len(w) < 3 or w.isdigit():
            continue
        # 落在已命中短语内部的单词跳过（white stockings 里的 white）
        if _covered(m.start(), len(w)):
            continue
        if w not in out:
            out.append(w)

    # 英文 → 中文：短语查 PHRASE_ZH，单词查 EN2ZH；查不到保留英文原词，
    # 用户在勾选界面照样能判断要不要。
    final: list[str] = []
    for w in out:
        final.append(PHRASE_ZH.get(w) or EN2ZH.get(w) or w)
    seen, uniq = set(), []
    for w in final:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq


def model_ready(model_dir: Path) -> bool:
    """目录里是否有一套可用的 onnx（两个）+ vocab/config。"""
    d = Path(model_dir)
    if not ((d / "vocab.txt").exists() and (d / "config.json").exists()):
        return False
    onnx = [p for p in d.glob("*.onnx") if p.stat().st_size > 1 << 20]
    return len(onnx) >= 2
