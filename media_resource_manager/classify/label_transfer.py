# -*- coding: utf-8 -*-
"""#60 / A2-3：转移原文显示——把「改成哪个词、跟原词有多像」讲清楚。

用户原话：转移的时候要看到原文，即
    「金发 → blond hair（相似度 0.71）」
以前界面上只显示改完之后那个词，用户根本不知道系统拿他的词去比了哪个英文词、
像不像。所以这里把三件事拆成三个纯函数（都能离线单测，不依赖 Qt/CLIP）：

* :func:`transfer_display` —— 拼那句文案（相似度缺失时显示「相似度 —」，
  **不能瞎编一个数字**：没模型就说没有，别假装算过）。
* :func:`text_similarity` —— 两个词之间的相似度。有本地 CLIP 就用 CLIP 的文本
  塔算余弦；**没有模型返回 None**（不是 0.0——0.0 会被误读成「一点都不像」）。
* :func:`transfer_hint` —— 串起来：给定原词 + 候选目标词，返回可直接显示的
  「原词 → 目标词（相似度 x.xx）」。

相似度口径统一用 LocalClassifier.similarity 那套：cosine 从 [-1,1] 映射到 [0,1]，
跟阈值口径一致（阈值也是 0~1）。
"""
from __future__ import annotations

# 相似度显示精度：两位小数，跟软件里其它分值显示一致（0.71 而不是 0.7123456）
SIM_DECIMALS = 2


def _fmt_sim(sim) -> str:
    """把相似度格式化成显示串；None（没模型/算不出来）显示破折号，不编数字。"""
    if sim is None:
        return "—"
    try:
        return f"{float(sim):.{SIM_DECIMALS}f}"
    except (TypeError, ValueError):
        return "—"


def transfer_display(src: str, dst: str, sim=None) -> str:
    """「金发 → blond hair（相似度 0.71）」

    sim 为 None（没有本地 CLIP 模型 / 算失败）时显示「相似度 —」，
    如实告诉用户「没算」，而不是拿0.00 冒充结果。
    """
    src = (src or "").strip()
    dst = (dst or "").strip()
    if not src and not dst:
        return ""
    return f"{src} → {dst}（相似度 {_fmt_sim(sim)}）"


def text_similarity(a: str, clf=None) -> float | None:
    """两个词的语义相似度0~1；**没有本地 CLIP 模型返回 None**。

    为什么不返回 0.0：0.0 是个有效分数，表示「确定不相似」。拿它当「算不出来」
    的返回值，界面上就会把「没模型」显示成「相似度 0.00」，用户会以为系统
    真的比过了、结论是完全不像 —— 这是误导，必须用 None 把两种情况分开。

    clf 省略时自己建一个 LocalClassifier；调用方一般该把自己的实例传进来
    （模型加载很贵，别每次现建）。
    """
    a = (a or "").strip()
    if not a:
        return None
    own = False
    if clf is None:
        try:
            from .clip_model import LocalClassifier
            clf = LocalClassifier()
            own = True
        except Exception:
            return None
    try:
        backend = getattr(clf, "backend", "stub")
        if backend != "clip":
            return None            # 无本地 CLIP 权重（CPU-only 笔记本常态）
        clip = getattr(clf, "_clip", None)
        if clip is None:
            return None
        import numpy as np
        te = clip.encode_text([a])
        # 单位向量点积 = 余弦；跟 LocalClassifier.similarity 一样映射到 0~1
        cos = float((te[0] @ te[0]))
        return max(0.0, min(1.0, (cos + 1.0) / 2.0))
    except Exception:
        return None
    finally:
        if own:
            # LocalClassifier 没有显式 close，_clip 的 onnx session 由 GC 回收
            pass


def transfer_hint(src: str, dst: str, clf=None) -> str:
    """转移提示全流程：算相似度 + 拼文案。给界面直接用。"""
    sim = None
    if (src or "").strip() and (dst or "").strip():
        sim = text_similarity(src, clf=clf)
    return transfer_display(src, dst, sim)


def transfer_note(src: str, dst: str, sim=None) -> str:
    """给人看的说明（含为什么相似度可能没有）。给日志/提示框用。"""
    base = transfer_display(src, dst, sim)
    if sim is None and (src or "").strip() and (dst or "").strip():
        base += "\n（没装本地 CLIP 模型，算不出相似度——只按词典直译）"
    return base