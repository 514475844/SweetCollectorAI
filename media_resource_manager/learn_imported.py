"""V0.0.3：导入本地图库 = 用户已经在用脚投票，导入完自动学偏好。

为什么要有这个模块（用户原话）：
    「我就是要导入我原先有的图片，机器不就知道我喜欢什么样的图片了吗。
      并不是机器已经学会了我喜欢啥，才要筛选我本地的图片。」

老逻辑把「导入自己的图库」当成「收了一批陌生素材等你审」：只有用户后来
在图库里一张张点喜欢 / 不喜欢，偏好表才会动。于是导进来 10 万张、偏好表
还是 0 行 —— 软件什么都没学到，还反过来要求用户先教它。

新逻辑：
  · 用户留下来的每一张图，本身就是一次明确的「我喜欢」；
  · 打完标签后，按标签出现频次把偏好一次性算出来写进偏好表；
  · 不需要再逐张点喜欢，也不需要预先配好主题词才敢筛图；
  · 桌面「从文件夹学习」和网页「导入文件夹」两条入口都自动接上；
  · 幂等：按「目标权重 − 上次已写权重」补差额，重复导入不会把权重越堆越高；
  · 本地图库一律不筛选、不隔离（用户已经留着的图，不该被软件再判一次）。

⚠ 这里只学「已经打好的标签」。一张标签都没有时不假装学到了东西，直接
   回报 0，免得界面显示「学习完成」其实权重表还是空的。
"""
from __future__ import annotations

import json
import logging

from .repo import (PREF_ORIGIN_IMPORT, _is_fragment_label, get_preferences,
                   set_preference)
from .simsearch import GENERIC

LOG = logging.getLogger("mrm.learn_imported")

#: 出现频次最高的那个标签，值多少权重（≈ 两次「喜欢」）。
#: 不设太高：用户后来手动点的一张喜欢/不喜欢 (-2.0 ~ +1.0) 要能扳得动它。
BASE_WEIGHT = 2.0
#: V0.0.4：出现 1 次就学。用户导进来的每一张图都等于亲口说「我喜欢这个」，
#: 不该被「至少出现两次」卡掉 —— 口味一分散就一个词都学不到，
#: 那正是用户说「导入了却没学会」的根子。
MIN_HITS = 1
#: 权重差额小于这个数就当没变，避免浮点噪声反复写库。
EPS = 1e-3
#: app_meta 键前缀，值是「上次写进去的目标权重」JSON。
# v1.1：门槛从 2 次改成 1 次，旧水位是按老规矩算的，换个键让旧导入
# 重新学一遍（幂等按差额补，不会把权重堆高）。
_META_PREFIX = "import_learn.v1.1."


def _norm(label: str) -> str:
    """标签统一键：去空白 + 小写。

    写入侧（老代码 split_tags）会把标签转小写，打分侧却拿原样去查，
    两边对不上就是「学了但不生效」。这里和 repo 层共用同一套归一。
    """
    return str(label or "").strip().lower()


def _label_counts(db, task_id: int) -> tuple[dict[str, int], int]:
    """统计某个本地图库任务下所有已打标图片的标签频次。

    返回 (标签 -> 出现次数, 参与统计的图片张数)。
    只统计 JSON 数组格式的 tags；老库里的逗号裸串也兼容。
    """
    counts: dict[str, int] = {}
    images = 0
    try:
        rows = db.query(
            "SELECT tags FROM images WHERE task_id=? "
            "AND tags IS NOT NULL AND tags != '' AND tags != '[]'",
            (int(task_id),))
    except Exception as e:
        LOG.warning("读取图库标签失败 task=%s: %s", task_id, e)
        return {}, 0

    for r in rows:
        raw = r["tags"]
        if isinstance(raw, str):
            t = raw.strip()
            if t.startswith("["):
                try:
                    labels = json.loads(t)
                except Exception:
                    import re
                    labels = re.split(r"[,\s;，、]+", t)
            else:
                import re
                labels = re.split(r"[,\s;，、]+", t)
        elif isinstance(raw, (list, tuple)):
            labels = raw
        else:
            continue
        seen_in_image = set()
        for lab in labels:
            k = _norm(lab)
            if not k or k in GENERIC or _is_fragment_label(k):
                continue
            # 同一张图里同一个标签只算一次，防止重复标签把口味放大
            if k in seen_in_image:
                continue
            seen_in_image.add(k)
            counts[k] = counts.get(k, 0) + 1
        if seen_in_image:
            images += 1
    return counts, images


def _target_weights(counts: dict[str, int]) -> dict[str, float]:
    """频次 → 目标权重。最高频标签给 BASE_WEIGHT，其余按相对频次折算。"""
    kept = {k: c for k, c in counts.items() if c >= MIN_HITS}
    if not kept:
        return {}
    top = max(kept.values())
    return {k: round(BASE_WEIGHT * c / top, 4) for k, c in kept.items()}


def learn_from_imported(db, task_id: int, *, source: str = "local_library") -> dict:
    """把一个已导入的本地图库任务，整体学成用户偏好。

    幂等做法：算出这次应有的「目标权重」，减掉 app_meta 里记的上次目标，
    只把差额写进偏好表。重复导入 → 差额全 0 → 一行都不写；
    后来又补了新图 → 只补新增的那部分，不会把旧权重翻倍。

    返回统计 dict，供界面显示「学到了多少个词」。
    """
    out = {"ok": True, "task_id": int(task_id), "images": 0,
           "labels": 0, "written": 0, "skipped": False, "reason": ""}
    # V0.0.4：导入就该学，不受「学习（AI 偏好）」那个开关管。
    # 那个开关管的是「用户一张张点喜欢/不喜欢要不要记进口味账」，
    # 跟「我主动把整个图库交给你，你从中学会我喜欢什么」是两回事。
    counts, images = _label_counts(db, task_id)
    out["images"] = images
    if not images:
        out.update(ok=False, skipped=True,
                   reason="这批图还没有标签，先做 AI 识别再学")
        return out

    target = _target_weights(counts)
    out["labels"] = len(target)
    if not target:
        out.update(ok=False, skipped=True,
                   reason="这批图没有可用标签，先做 AI 识别再学")
        return out

    key = _META_PREFIX + str(int(task_id))
    try:
        prev = json.loads(db.meta_get(key, "{}") or "{}")
        if not isinstance(prev, dict):
            prev = {}
    except Exception:
        prev = {}

    # 差额：新出现的词加正权重，权重变了的补差，消失的词把旧权重撤掉
    deltas: dict[str, float] = {}
    for lab, w in target.items():
        d = round(w - float(prev.get(lab, 0.0) or 0.0), 4)
        if abs(d) >= EPS:
            deltas[lab] = d
    for lab, old in prev.items():
        if lab not in target:
            d = round(-float(old or 0.0), 4)
            if abs(d) >= EPS:
                deltas[lab] = d

    if not deltas:
        out["skipped"] = True
        out["reason"] = "这批图已经学过，没有变化"
        return out

    written = 0
    for lab, d in deltas.items():
        try:
            set_preference(db, 0, lab, d, kind="pos",
                           origin=PREF_ORIGIN_IMPORT)
            written += 1
        except Exception as e:
            LOG.warning("写偏好失败 %s: %s", lab, e)
    out["written"] = written

    # 留痕：让「样本来源」看板能看出这批权重来自导入的本地图库，
    # 而不是用户一张张点出来的。留痕失败不影响权重已写入。
    try:
        db.record_learn_sample(
            act="import", polarity="pos", weight=BASE_WEIGHT,
            task_id=int(task_id), source=source,
            labels=sorted(target.keys()))
    except Exception:
        pass

    try:
        db.meta_set(key, json.dumps(target, ensure_ascii=False))
    except Exception as e:
        # V0.0.6：以前水位写失败只打条日志，权重却已经累加进去了 ——
        # 下次重复导入读到的还是旧水位，同一笔差额再补一次，同一个词
        # 权重直接翻倍。现在把这次写进去的差额撤回，让下次重导重新学。
        LOG.warning("记录学习水位失败 task=%s: %s，撤回本次权重避免重复累加",
                    task_id, e)
        for lab, d in deltas.items():
            try:
                set_preference(db, 0, lab, -d, kind="pos",
                               origin=PREF_ORIGIN_IMPORT)
            except Exception:
                pass
        out.update(ok=False, written=0,
                   reason="学习进度没记上，本次权重已撤回，下次导入会重新学")

    LOG.info("本地图库偏好学习完成 task=%s 图=%s 词=%s 写入=%s",
             task_id, images, len(target), written)
    return out
