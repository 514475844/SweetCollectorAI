"""Local preference learning (no network, no external calls).

Stores per-task label weights in SQLite. Scores an image from its tags, and
updates weights from explicit human decisions. "Active learning" only surfaces
samples whose score is near the accept threshold (uncertain), so the user
reviews the minimum necessary.

#64 / A5-2（本文件新增的部分）：**词表桥接接进学习入口**。

以前 :meth:`learn` 只把词原样写进 ``preferences.label``。而打分时查的是
CLIP 打出来的标签（必然来自词表），所以**手填的词学进去永远打不着地** ——
「AI 学习闭环」断的就是这一环。现在多一个 :meth:`learn_manual`：

    learn()        —— 词已经是词表里的（CLIP 打出来的），直接写，不绕桥
    learn_manual() —— 用户手填的词，先桥到词表标签再写；桥不到就按原词写

两条路故意分开：给 :meth:`learn` 加桥接会让每次判定都去建矩阵（几百毫秒
× 每张图），而那条路上的词本来就在词表里，桥接纯属白干。
"""
from __future__ import annotations

from typing import Optional

from ..db import Database
from ..repo import (get_preferences, set_preference, _is_fragment_label,
                    PREF_ORIGIN_MODEL, PREF_ORIGIN_USER, PREF_WEIGHT_CAP)
from .clip_model import LocalClassifier

#: 权重上限（与 repo.PREF_WEIGHT_CAP 同一个数，这里只是给学习侧一个短名字）。
WEIGHT_CAP = PREF_WEIGHT_CAP

# R149-B：全局「学习」开关。由 AI 菜单勾选项驱动（cfg.learn_enabled，默认开）。
# 关掉时 PreferenceLearner.learn 直接 return，不持久化任何偏好 —— 但审核 /
# 删除 / 隔离等动作本身照常进行。模块级标志让桌面 + Web（同进程线程）共用同一
# 份状态，且 Web / 桌面各自启动时都会用各自的 cfg 重新校准一次，互不依赖。
_LEARNING_ENABLED = True


def set_learning_enabled(value: bool) -> None:
    global _LEARNING_ENABLED
    _LEARNING_ENABLED = bool(value)


def is_learning_enabled() -> bool:
    return _LEARNING_ENABLED


LIKE, DISLIKE, BLACKLIST = 1.0, -1.0, -2.0
# R166：隔离区「彻底删除」= 强负样本，比普通「不喜欢」更重（用户明确不要，
# 且是**不可还原**的动作，比可还原的 dislike 更确定）。取值与拉黑同档 -2.0：
# 两者语义都是"别再来"，再往上加会把单张图的权重推到压过整批审核的程度。
PURGE = -2.0

#: 决策 → 权重。**R166 起不再只有「不喜欢/拉黑」才学习**：用户在图库 /
#: 审核 / 隔离区做出的每一种判断都进这张表（订单 R166「我要求全部进行学习」）。
#:   like     审核通过              → 正样本（这张图命中的标签值得留）
#:   restore  隔离区还原回图库      → 正样本（同上；与 like 分开只为让看板
#:                                   能区分「通过」与「还原」两种来源）
#:   favorite 图库收藏              → 正样本（同上，且是"特别想留"）
#:   dislike  不喜欢 / 图库删除      → 负样本
#:   blacklist 拉黑                  → 强负样本
#:   purge    隔离区彻底删除         → 强负样本（不可还原，用户最确定的一次）
#:   skip     跳过                   → 0（只是"这页先不看"，不表态，不算样本）
#: ⚠ 新增键**只改「记不记」的覆盖面，不改任何既有动作的权重** ——
#:   like/dislike/blacklist 的取值一个没动，老学习结果不受影响。
_DECISION_W = {"like": LIKE, "dislike": DISLIKE,
               "blacklist": BLACKLIST, "skip": 0.0,
               "restore": LIKE, "favorite": LIKE, "purge": PURGE}
_DECISION_KIND = {"like": "pos", "dislike": "neg", "blacklist": "neg",
                  "restore": "pos", "favorite": "pos", "purge": "neg"}


class PreferenceLearner:
    def __init__(self, db: Database, task_id: int,
                 classifier: Optional[LocalClassifier] = None,
                 threshold: float = 0.0):
        self.db = db
        self.task_id = task_id
        self.clf = classifier
        self.threshold = threshold

    def score(self, tags: list[str]) -> float:
        weights = get_preferences(self.db, self.task_id)
        if not tags:
            return 0.0
        # average weight of matched tags; fall back to classifier confidence.
        # V0.0.3：查表前归一。写入侧是小写，这里拿原始标签查会永远扑空。
        matched = [weights.get(str(t).strip().lower(), 0.0) for t in tags]
        return sum(matched) / len(matched)

    def score_image(self, image_path) -> float:
        if self.clf is not None:
            preds = self.clf.classify(image_path)
            tags = [lbl for lbl, s in preds if s >= 0.5]
        else:
            tags = []
        return self.score(tags)

    def learn(self, decision: str, tags: list[str],
              origin: str = PREF_ORIGIN_MODEL):
        """记权重。``origin`` = 这条词的来源（R174）。

        ⚠ 默认 ``model_auto`` —— 本方法进来的是**这张图命中的 AI 标签**，
        是模型的判断不是用户写的。要记用户手填的词请用 :meth:`learn_manual`
        （它显式传 ``user_typed``）。这条默认值是本单的防线：漏传参数时
        落在"不进硬黑名单"那一端。

        ★ R169 第 3 条：**模型自动的碎片词不进统计**。

        为什么过滤放在这里而不在 ``repo.set_preference``：本方法是"模型标签
        入库"的**唯一咽喉**，过滤只该作用在这条路上。放进低层写入器会把
        导入偏好、权重明细手工改、测试造数据一起改掉 —— 那些写入是用户
        明确要求的，不该被"防噪声"顺手改行为。

        碎片词的判定见 ``repo._is_fragment_label``（单拉丁字母 / 纯符号 /
        纯数字；单一汉字**放行**，"猫"是完整类别词）。
        """
        # R149-B：全局「学习」开关关闭时跳过持久化（只跳过学习本身，
        # 不跳过调用方的审核/删除/隔离逻辑）。老配置无键 = 默认开。
        if not is_learning_enabled():
            return
        # #37/D3：除了权重，还要记「命中方向」—— 权重明细要分列显示
        # 「主题词命中」和「排除词命中」（skip 不算命中，只是没表态，方向为空）。
        w = _DECISION_W.get(decision, 0.0)
        kind = _DECISION_KIND.get(decision)
        for t in tags:
            if not (t or "").strip():
                continue
            # ★ R169：模型自动的碎片词在这里被挡下（用户手填的不经本方法，
            #   走 learn_manual ⇒ 同样调 self.learn 但 origin=user_typed，
            #   所以下面必须判 origin，不能无条件过滤）。
            if origin == PREF_ORIGIN_MODEL and _is_fragment_label(t):
                continue
            set_preference(self.db, self.task_id, t, w, kind=kind,
                           origin=origin)

    # ================= R166：所有判断动作都参与学习 =================
    def learn_action(self, act: str, tags: list[str], source: str = "",
                     image_id=None, weight_scale: float = 1.0,
                     sample_task_id=None) -> bool:
        """「一次判断动作」的统一学习入口：**记权重 + 记来源留痕**。

        用户要求（R166）：「我要求全部进行学习」—— 图库 / 审核 / 隔离区里
        做出的每一种判断都要进学习引擎。之前各调用点各写各的（甚至有的动作
        压根没写），于是"点了很多不喜欢却学不到""点了通过不涨正样本"这类
        断链反复出现。现在收成**一个**入口，四个端（桌面图库 / 桌面审核 /
        桌面隔离区 / 网页端）都调它，覆盖面不会再漏。

        参数
        ----
        act            : 决策值（like / restore / favorite / dislike /
                         blacklist / purge / skip），见 ``_DECISION_W``。
        tags           : 这次判断针对的标签（该图命中的 AI 标签）。
        source         : 来源视图（``gallery`` / ``review`` / ``quarantine`` /
                         ``web_gallery`` / ``web_quarantine`` …）。**只用于
                         留痕统计，不影响权重** —— 用户要能看出样本是哪来的。
        image_id       : 对应的图（图片行可能已被真删除，只作留痕）。
        weight_scale   : 权重倍率（默认 1.0）。仅供「单张 vs 批量」这类
                         场景微调，正常不用传。
        sample_task_id : 只写进留痕的「这张图当时属于哪个任务」。省略时用
                         ``self.task_id``。⚠ 权重归属永远是全局（task_id=0，
                         见 ``repo.set_preference``），这个参数**改不了权重**，
                         别拿它当"按任务分开学"的开关。

        返回是否真的记了（学习开关关着、或没有有效标签时为 False）。

        ⚠ 学习开关：关着时**只不动权重**；是否还记来源留痕？
          答案是不记 —— 留痕的意义是解释"权重为什么是这样"，开关关着时
          没有权重产生，留痕会变成对不上账的噪声。看板以权重为准。
        """
        clean = [str(t) for t in (tags or []) if str(t or "").strip()]
        self.last_feedback = None
        # V0.0.7：用户明确拒收一张图时，把这张图的标签记进「以后别找」的
        # 纠错账本。刻意放在学习开关**之前**：开关只管「要不要记口味权重」，
        # 管不了「这张图别再下」。以前拉黑只写一条负权重，而负权重从来
        # 没变成采集时的排除条件（模型候选里只有「要找的词」），闭环是断的。
        # V0.0.8：记账结果挂到 self.last_feedback，审核页据此告诉用户
        # 「这次点掉学到了哪些词」——只存不显示，用户永远不知道点了有用。
        if act in ("dislike", "blacklist", "purge") and clean:
            try:
                from ..feedback import record_rejection
                self.last_feedback = record_rejection(self.db, clean, act)
            except Exception:
                pass        # 记账失败不影响这次判断本身
        if not is_learning_enabled():
            return False
        w = _DECISION_W.get(act, 0.0)
        if not clean or w == 0.0:
            # skip 与未知动作：不产生样本（"这页先不看"不是表态）。
            # ⚠ 但仍要落一条**零权重留痕**吗？不落 —— 那会让看板的
            #   「样本来源」计数比真实权重条数还多，用户会以为学漏了。
            return False
        self.learn(act, clean)
        try:
            self.db.record_learn_sample(
                act=act, polarity=_DECISION_KIND.get(act, ""),
                weight=w * float(weight_scale or 1.0),
                image_id=image_id,
                task_id=(self.task_id if sample_task_id is None
                         else sample_task_id),
                source=source, labels=clean)
        except Exception:
            pass        # 留痕失败不影响权重已记成的事实
        return True

    # ================= #64 / A5-2词表桥接 =================
    def learn_manual(self, decision: str, words: list[str],
                     bridge=None, clf=None) -> list:
        """手填词学习：**先桥到词表标签，再记权重**（D54 命中即采信）。

        参数
        ----
        bridge : 省略时用 :func:`~.label_bridge.get_bridge` 取进程内共享的那个
                 （词表矩阵建一次几百毫秒，共享才划算）。
        clf    : 只在本函数内部临时建桥时用得上（省得调用方自己造分类器）。

        返回命中的 :class:`~.label_bridge.BridgeHit` 列表（给界面显示
        「金发 → blond hair（相似度 0.71）」用）。桥接成功时**原词与词表标签
        两条都记**；桥不到就只记原词—— 桥接只做加法，永远不让用户白填。

        ⚠️ **绝不因为桥接把词丢**：本方法只可能**增加**一条词表标签的权重，
        不会让用户填的那个词学不到。丢词 = 闭环还是断的。
        """
        from .label_bridge import get_bridge

        if not words:
            return []
        br = bridge if bridge is not None else get_bridge(self.db, clf=clf)
        hits = []
        extra: list[str] = []
        for w in words:
            w = (w or "").strip()
            if not w:
                continue
            extra.append(w)          # 原词永远保留（见下面那句「绝不丢词」）
            hit = br.resolve(w) if br is not None else None
            if hit is not None:
                hits.append(hit)
                if hit.label and hit.label != w:
                    # 桥接成功 → **额外**给词表标签也记一条：CLIP 打出来的是它，
                    # 不记它的话这次学习照样打不着地。
                    extra.append(hit.label)
        if extra:
            # ★ R174：手填词 = user_typed —— 这是**唯一**允许进硬黑名单的来源。
            #   桥接出来的词表标签也跟着记 user_typed：它是照用户写的那个词
            #   找出来的，属于"用户意图"，不是模型自作主张打的标签。
            self.learn(decision, list(dict.fromkeys(extra)),
                       origin=PREF_ORIGIN_USER)
        return hits

    def is_uncertain(self, score: float, margin: float = 0.2) -> bool:
        return abs(score - self.threshold) <= margin

    def serialize(self) -> dict:
        return {"task_id": self.task_id, "threshold": self.threshold,
                "weights": get_preferences(self.db, self.task_id)}

    def reset(self):
        # 学习记录是全局的（task_id=0），reset 清空全部学习权重。
        self.db.execute("DELETE FROM preferences WHERE task_id=0")