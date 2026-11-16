# -*- coding: utf-8 -*-
"""#64 / A5-1：词表桥接 —— 把「用户手填的词」接到 AI 真正会打出来的标签上。

## 要解决的断链

学习权重是**按标签字符串**存的（``preferences.label``），而打分时拿的是
CLIP 对每张图跑出来的标签（``LocalClassifier.classify`` 的输出，必然来自
``labels`` 这张词表）。于是：

    用户手填「金发」→ 权重写到 label='金发'
    CLIP 给图打的标签是 'blond hair'
    → score() 查权重查不到 → **学了什么等于没学**

这就是「AI 学习闭环」断掉的那一环：**手填词不在词表里，权重就永远打不着地**。

## 桥接怎么做（性能是硬约束）

把整张词表 **一次性 batch encode 成一个矩阵**并缓存下来；用户手填一个词时
只 encode 这**一个**词，跟矩阵做点积取 top-k：

    词表 1000 个词  →  1 轮 encode（分批只是不撑爆内存，仍是批量）
    手填 1 个词     →  1 轮 encode

⚠️ **绝对不能逐词 encode**（对每个标签各调一次 ``encode_text([w])``）：
1000 个词就是 1000 次前向，CPU 上要几分钟，用户会以为软件卡死。
:attr:`LabelBridge.encode_calls` 就是给测试钉死这件事的计数器。

## 三级命中来源（``source``）

============  ====================================================
``exact``     手填词本身就在词表里（小写归一后相等）
``dict``      内置中英词典（``labels_zh``）翻出来的候选命中词表
              —— **CPU-only 无模型机器的主力路径**（CLIP 认中文很不可靠）
``clip``      CLIP 文本塔余弦 top-1 超过阈值才采信
============  ====================================================

## 相似度缺失一律是 None，不是 0.0

跟 #60 转移原文显示同一个铁律：0.0 会被读成「算过了，完全不像」，
而「没模型 / 没命中」是「没算过」。两者必须分开。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

# 一次 encode_text 最多塞多少个词。只影响内存峰值，**不影响「批量」这个事实**
# （1000 个词按 256 分批 = 4 次前向，而不是 1000 次）。
BATCH = 256

# 采信门槛：**原始 CLIP 余弦**（不是映射后的 0~1）。
#
# ⚠️ 为什么不在映射后的 0~1 上卡阈值：软件其它地方的 0.35 是给**图像-文本**
# 相似度用的（CLIP 图文余弦多在 0.2~0.3），而文本-文本余弦天生高得多
# （毫不相干的两个词也有 0.3~0.5，相关的能到 0.8+）。拿 0.35 去卡映射后的值
# 等于 cos >= -0.3 —— **什么词都过**，桥接会变成"永远命中"，那不如不做。
# 文本-文本用原始余弦 0.55 卡：相关的词基本都过，不相关的过不了。
MIN_COS = 0.55

# 对外展示的相似度仍按软件统一口径映射到 0~1（跟 #60 转移原文显示一致）。
# 展示值不能当判据用 —— 这是本文件最容易踩的一个坑。
DISPLAY_SHIFT = 1.0

_CJK = None


def _has_cjk(s: str) -> bool:
    global _CJK
    if _CJK is None:
        import re
        _CJK = re.compile(r"[\u4e00-\u9fff]")
    return bool(_CJK.search(s or ""))


@dataclass(frozen=True)
class BridgeHit:
    """一次桥接的结果。

    ``sim`` 为 None 表示**没算相似度**（exact / dict 命中，或压根没模型），
    界面必须显示「相似度 —」，不能拿 0.0 冒充。
    """

    word: str                 # 用户手填的原文
    label: str                # 真正要记权重的词表标签
    sim: Optional[float]      # 0~1；None = 没算
    source: str               # exact / dict / clip


def _norm_key(s: str) -> str:
    """词表比对用的归一键：小写 + 收空白。"""
    return " ".join((s or "").strip().lower().split())


def dedupe_labels(labels: Iterable[str]) -> list[str]:
    """去重保序（按归一键去重，但**保留原词形**——标签要拿去跟 CLIP 比对）。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in labels or []:
        lab = (raw or "").strip()
        if not lab:
            continue
        k = _norm_key(lab)
        if k in seen:
            continue
        seen.add(k)
        out.append(lab)
    return out


class LabelBridge:
    """词表桥接器：手填词 → 词表标签。

    用法::

        br = LabelBridge(vocab, clf=my_clf)
        hit = br.resolve("金发")
        if hit:
            PreferenceLearner(db, tid).learn("dislike", [hit.label])

    ``clf`` 省略或没有 CLIP 后端时**不报错**，退到 exact/dict 两级 ——
    这台CPU-only笔记本没装 90MB 权重是常态，桥接必须照样能用。
    """

    def __init__(self, labels: Iterable[str], clf=None,
                 min_cos: Optional[float] = None,
                 cache_dir: Optional[Path] = None):
        self.labels = dedupe_labels(labels)
        self.clf = clf
        self.min_cos = MIN_COS if min_cos is None else float(min_cos)
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._mat = None                    # (n, d) 已归一化；None = 没建
        self._dict_map: dict[str, str] = {}  # 词典候选归一键 -> 词表标签
        self._exact: dict[str, str] = {}
        for lab in self.labels:
            self._exact.setdefault(_norm_key(lab), lab)
        #: 测试钉死「整表只批量 encode 一次」的计数器（见模块 docstring）
        self.encode_calls = 0
        self.cache_hit = False

    # ---------------- 可用性 ----------------
    @property
    def has_clip(self) -> bool:
        return bool(getattr(self.clf, "backend", "") == "clip"
                    and getattr(self.clf, "_clip", None) is not None)

    @property
    def ready(self) -> bool:
        """能不能给出「相似度」这个数字。"""
        return self.has_clip and self._mat is not None

    # ---------------- 矩阵构建（批量，一次） ----------------
    def _cache_key(self) -> str:
        payload = json.dumps(
            # 排序：词表是集合，顺序不同不该导致缓存 miss（见 get_bridge 里的同款理由）
            {"labels": sorted(_norm_key(x) for x in self.labels),
             "model": str(getattr(self.clf, "model_dir", "") or "")},
            ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]

    def _cache_path(self) -> Optional[Path]:
        if self._cache_dir is None:
            return None
        return Path(self._cache_dir) / f"label_vecs_{self._cache_key()}.npz"

    def build(self) -> bool:
        """把整张词表 encode 成矩阵。返回是否真建成（False = 无模型/失败）。

        磁盘缓存：同一份词表 + 同一个模型，只 encode 一次，之后启动直接读npz。
        缓存坏了（行数对不上 / 全零向量 / 读不出来）一律丢掉重建，
        **绝不让缓存把功能卡死**。
        """
        if self._mat is not None:
            return True
        if not self.labels or not self.has_clip:
            return False
        path = self._cache_path()
        if path is not None and path.exists():
            mat = self._load_cached(path)
            if mat is not None:
                self._mat = mat
                self.cache_hit = True
                return True
        try:
            import numpy as np
            clip = self.clf._clip
            chunks = []
            for i in range(0, len(self.labels), BATCH):
                part = self.labels[i:i + BATCH]
                vecs = clip.encode_text(list(part))   # ← 批量，不是逐词
                self.encode_calls += 1
                chunks.append(np.asarray(vecs, dtype="float32"))
            mat = np.vstack(chunks)
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            self._mat = mat / norms
        except Exception:
            self._mat = None
            return False
        if path is not None:
            self._save_cached(path)
        return True

    def _load_cached(self, path: Path):
        """读磁盘缓存并**按当前词表顺序重排行**。任何不对劲都返回 None。

        为什么必须重排：缓存文件名用的是**排序后**的词表指纹（顺序无关），
        而矩阵行序是 ``self.labels`` 的顺序。两者顺序不一致时直接用就会
        「词 A 拿到词 B 的向量」——静默出错，比缓存 miss 严重得多。
        """
        try:
            import numpy as np
            z = np.load(str(path), allow_pickle=False)
            mat = z["mat"]
            keys = [str(x) for x in z["labels"]]
            if mat.ndim != 2 or mat.shape[0] != len(keys):
                return None
            want = [_norm_key(x) for x in self.labels]
            if sorted(keys) != sorted(want) or len(self.labels) != len(keys):
                return None                       # 词表本身变了，不是顺序问题
            pos = {k: i for i, k in enumerate(keys)}
            order = [pos[k] for k in want]
            mat = np.asarray(mat, dtype="float32")[order]
            if not bool((mat * mat).sum(axis=1).min() > 0):
                return None                       # 有全零行 = 坏缓存
            return mat
        except Exception:
            return None

    def _save_cached(self, path: Path) -> None:
        try:
            import numpy as np
            path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                str(path), mat=self._mat,
                labels=np.array([_norm_key(x) for x in self.labels]))
        except Exception:
            pass                          # 缓存写不进去不影响功能

    # ---------------- 查询 ----------------
    def topk(self, word: str, k: int = 5) -> list[tuple[str, Optional[float], str]]:
        """手填词 → 词表里的 top-k 候选 ``[(label, sim, source)]``。

        顺序：exact → dict（仅中文词）→ clip。命中即返回，不做无意义的混合排序
        ——用户要的是「我这句话到底被当成哪个词」，一个明确答案比一串候选有用。
        """
        w = (word or "").strip()
        if not w or not self.labels:
            return []
        key = _norm_key(w)
        exact = self._exact.get(key)
        if exact is not None:
            return [(exact, 1.0, "exact")]
        hit = self._dict_lookup(w, key)
        if hit is not None:
            return [(hit, None, "dict")]
        if not self.build():
            return []
        try:
            import numpy as np
            q = np.asarray(self.clf._clip.encode_text([w]), dtype="float32")[0]
            n = float(np.linalg.norm(q))
            if n > 0:
                q = q / n
            sims = self._mat @ q                # ← 一次矩阵点积
            order = np.argsort(-sims)[:max(1, int(k))]
            out = []
            for i in order:
                cos = float(sims[i])
                if cos < self.min_cos:
                    break
                out.append((self.labels[int(i)],
                            max(0.0, min(1.0, (cos + DISPLAY_SHIFT) / 2.0)),
                            "clip"))
            return out
        except Exception:
            return []

    def _dict_lookup(self, word: str, key: str) -> Optional[str]:
        """内置中英词典 → 词表标签。

        中文词先查（CLIP 认中文不可靠，词典才是CPU-only 机器的主力路径）；
        英文词也查一次，能覆盖「同义但拼写不同」。
        """
        if not self._dict_map:
            try:
                from .labels_zh import ZH2EN, EN2ZH
            except Exception:
                return None
            cand: list[str] = []
            if _has_cjk(word):
                raw = ZH2EN.get(word) or ZH2EN.get(key) or ""
                cand = [x.strip() for x in raw.split("|") if x.strip()]
            else:
                raw = EN2ZH.get(word) or EN2ZH.get(key) or ""
                zh = raw.strip()
                if zh:
                    en = ZH2EN.get(zh) or ""
                    cand = [x.strip() for x in en.split("|") if x.strip()]
                if not cand:
                    cand = [word]
            for c in cand:
                tgt = self._exact.get(_norm_key(c))
                if tgt is not None:
                    return tgt
            return None
        return self._dict_map.get(key)

    def resolve(self, word: str) -> Optional[BridgeHit]:
        """只取 top-1。采信规则：命中即采信（D54）。

        ``sim`` 是 None 时（exact / dict 命中）**照样采信** —— 那是确定命中，
        不是「算不出来」。clip 来源的 topk 里已经卡过 ``min_cos``，
        能返回就说明过线了（低于阈值时 topk 直接 break，返回空列表）。
        """
        got = self.topk(word, k=1)
        if not got:
            return None
        label, sim, source = got[0]
        return BridgeHit(word=(word or "").strip(), label=label,
                         sim=sim, source=source)

    def resolve_all(self, words: Iterable[str]) -> list[BridgeHit]:
        return [h for h in (self.resolve(w) for w in (words or [])) if h]

    # ---------------- 展示 ----------------
    @staticmethod
    def hint(hit: Optional[BridgeHit]) -> str:
        """给界面用的一行文案：命中即采信，命中词看得见。

        「金发 → blond hair（词典对应）」/「…（相似度 0.71）」/
        没命中「『xxx』不在词表里，这条只按原词学」。
        """
        if hit is None:
            return ""
        if hit.source == "clip":
            sim = "—" if hit.sim is None else f"{hit.sim:.2f}"
            return f"{hit.word} → {hit.label}（相似度 {sim}）"
        if hit.source == "dict":
            return f"{hit.word} → {hit.label}（词典对应）"
        return f"{hit.word}（就是词表里的词）"


def preview_lines(words, bridge) -> list[str]:
    """手填词的桥接预演（V0.0.12 抽成共享实现）。

    以前长在桌面 dislike_dialog 的 `_bridge_preview`（收 vocab 自建桥）；
    网页判退模态要显示同一句话，两处各写一份早晚分叉。这里收**桥对象**
    （调用方自带共享桥，桌面/网页都省重复建矩阵）：
        金发 → blond hair（词典对应）
        猫 → cat（相似度 0.71）
        量子纠缠 → 不在词表里，只按原词记
    """
    out = []
    for w in (words or []):
        w = (str(w) or "").strip()
        if not w:
            continue
        hit = bridge.resolve(w) if bridge is not None else None
        out.append(LabelBridge.hint(hit) if hit else f"{w} → 不在词表里，只按原词记")
    return out


# ================= 全局词表（学习权重是全局的，词表也得全局） =================
def global_vocabulary(db) -> list[str]:
    """收集**所有任务**的词表：一/二/三级主题词 + 排除词 + 普通黑名单，双语扩展。

    为什么必须收全局的：``preferences`` 表已经统一到 ``task_id=0``（见 db.py），
    也就是说 A 任务学到的词要能影响 B 任务。词表只给当前任务看，桥接就等于
    「换个任务就失忆」，闭环还是断的。

    排除词也在词表里 —— 用户在「不喜欢」弹窗里勾的排除词，本来就该被认出来。
    """
    out: list[str] = []
    try:
        from ..repo import list_tasks
        tasks = list_tasks(db)
    except Exception:
        tasks = []
    for t in tasks or []:
        if t is None:
            continue
        # 2026-10-28：主题词支持无限分级，用 flat_words 一次收全（1..N 层）。
        # 只收前三层会让第 4 层以后的词在"不喜欢"弹窗里认不出来。
        try:
            from ..label_levels import flat_words
            out.extend(flat_words(t))
        except Exception:
            for attr in ("ai_labels", "ai_labels_l2", "ai_labels_l3"):
                out.extend(getattr(t, attr, None) or [])
        for attr in ("ai_blacklist", "blacklist"):
            out.extend(getattr(t, attr, None) or [])
    try:
        from .labels_zh import expand_labels
        out = expand_labels(out)
    except Exception:
        pass
    return dedupe_labels(out)


def cache_dir() -> Optional[Path]:
    """向量缓存目录（``<数据目录>/cache``）。取不到就返回 None = 只用内存。"""
    try:
        from ..config import user_data_dir
        return Path(user_data_dir()) / "cache"
    except Exception:
        return None


_shared: dict[str, LabelBridge] = {}


def get_bridge(db, clf=None, labels: Optional[list[str]] = None) -> LabelBridge:
    """进程内共享一个桥接器（词表矩阵建一次要几百毫秒，别每次判定都重建）。

    词表变了（用户改了主题词）会自然重建：key 里含词表的指纹。
    """
    vocab = dedupe_labels(labels) if labels is not None else global_vocabulary(db)
    # ⚠️ key 用**排序后**的词表：词表的顺序对桥接毫无意义（只是集合），
    # 但用户改个顺序就换一个 key 会白白重建几百毫秒的矩阵。
    # 同理 ``_cache_key`` 里也排序 —— 否则换个顺序就miss 磁盘缓存。
    key = hashlib.sha1(
        json.dumps(sorted(_norm_key(x) for x in vocab), ensure_ascii=False
                   ).encode("utf-8")).hexdigest()[:16]
    br = _shared.get(key)
    if br is None:
        br = LabelBridge(vocab, clf=clf, cache_dir=cache_dir())
        _shared[key] = br
    elif clf is not None and br.clf is None:
        br.clf = clf                      # 后到的模型实例补上，别白建一次
    return br


def reset_shared() -> None:
    """清掉进程内缓存（测试用；换了模型目录后也该调）。"""
    _shared.clear()