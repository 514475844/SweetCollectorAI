"""V0.0.7：纠错反馈账本 —— 把「这张图下错了，别再下」真正教给软件。

用户最初讲这个软件时说的最后一步：

    偶尔下错（想要动漫图却来了真人照），把这张放进黑名单，
    软件又学习一次，越用越准。

以前这条链是断的。拉黑一张图时，软件只会把**这张图身上带的标签**记成
一条负权重；而「下次别找什么」那张词表只收用户亲手敲进去的词，模型自动
认出来的标签一律不许进（R174 的防线：模型会认错，一次误标不能把用户
真正想要的类目封死 —— 这是用户自己投诉过并拍板要保住的）。
于是：学是学了，可采集时这些词既不在下载前的比对里，也不在站内找图的
排除条件里，连看图模型的排除候选里都没有 —— 那笔负权重永远没机会生效。

这个模块补的就是最后一环，但**不拆掉 R174 那道防线**：

  1. 用户明确拒收（不喜欢 / 拉黑 / 彻底删除）一张图 → 把这张图的标签
     记进一本独立的账，累计「被你判掉过几次」；
  2. 只判掉过 1 次 → **软反馈**：只进看图模型的排除候选，命中就送审核
     让人再看一眼，不静默删、不写进黑名单；
  3. 同一类判掉 ≥2 次 → **硬反馈**：升级进「别找什么」，下载前文字比对
     和站内找图都开始躲它；
  4. 账本里每个词都能在学习看板上看见、单独删掉、一键清空 —— 学歪了
     随时能反悔，绝不会出现「黑名单里多了我从没写过的词还删不掉」。

⚠ 这本账**不受「启用偏好学习」开关管**：那个开关管的是「要不要记口味
   权重」，用户亲手点「拉黑」表达的是「这类图别再下」，关着开关也不该
   让拉黑失效。两者语义不同，写进代码时刻意分开。

存储用现成的 ``app_meta`` 单键 JSON，不新建表、不改数据库结构，老库
直接能用；读不到 / 写坏了就当空账重来，绝不让审核动作因此失败。
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime

LOG = logging.getLogger("mrm.feedback")

#: app_meta 里的键。带版本号，将来改结构换一个键即可，老数据不互相污染。
META_KEY = "feedback_avoid.v1"
#: 同一个词被明确拒收几次后升级成硬拦截。2 次是刻意的：一次可能是模型
#: 认错或用户手滑，连续两次就是稳定意图了。
HARD_HITS = 2
#: 账本上限。长期使用的库里模型标签可能成百上千，只留最常被拒收的那批，
#: 免得 app_meta 里塞进一个几 MB 的 JSON 拖慢每次读取。
MAX_WORDS = 400
MAX_TAG_LEN = 40
#: 只认这几种「明确不要」的动作。收藏 / 通过 / 还原 / 跳过都不算。
REJECT_ACTS = frozenset({"dislike", "blacklist", "purge"})

# 读-改-写三步不是原子操作：导入线程与界面线程可能同时学习，模块级锁
# 保证同一进程内不丢账（db.meta_set 自带的锁只保护它自己那一条 SQL）。
_LOCK = threading.RLock()

# 模型常打出来的「什么都没说」的词。拿它们当排除词等于把整站都排掉，
# 而且用户从没写过这些词 —— 正是 R174 那次投诉要防的形态。
_STOP_WORDS = frozenset({
    "general", "other", "others", "misc", "unknown", "none", "all",
    "image", "images", "picture", "pictures", "photo", "photos",
    "img", "pic", "pics", "art", "tag", "tags",
    "图片", "照片", "图像", "其它", "其他", "未知", "默认", "未分类",
})

# 文件名 / 网址 / 纯编号这类东西不是「一类图」，别拿去当排除词
_BAD_SHAPE = re.compile(
    r"^(?:https?://|www\.|[a-f0-9]{16,}$|\d+[a-z]{0,3}$|"
    r".+\.(?:jpg|jpeg|png|webp|gif|bmp|mp4|html?)$)", re.I)


def _clean_tag(word) -> str:
    """一个标签能不能当「以后别找它」的词。不能就返回空串。"""
    s = str(word or "").strip().lower()
    if (not s or len(s) > MAX_TAG_LEN or s in _STOP_WORDS
            or _BAD_SHAPE.match(s)):
        return ""
    try:
        from .repo import _is_fragment_label
        if _is_fragment_label(s):
            return ""
    except Exception:
        pass
    try:
        from .simsearch import GENERIC
        if s in GENERIC:
            return ""
    except Exception:
        pass
    return s


def clean_tags(tags) -> list[str]:
    """清洗并去重，保持原顺序。给外部（含测试）单独用。"""
    out: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        k = _clean_tag(t)
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _load(db) -> dict:
    if db is None:
        return {}
    try:
        raw = db.meta_get(META_KEY, "{}") or "{}"
        d = json.loads(raw)
        if not isinstance(d, dict):
            return {}
        out = {}
        for k, v in d.items():
            key = str(k or "").strip().lower()
            if not key:
                continue
            if isinstance(v, dict):
                out[key] = {"hits": int(v.get("hits") or 1),
                            "last": str(v.get("last") or ""),
                            "acts": str(v.get("acts") or "")}
            else:
                # 兼容早期/手工写坏成 {"词": 3} 的形状
                try:
                    n = int(v)
                except Exception:
                    n = 1
                out[key] = {"hits": max(1, n), "last": "", "acts": ""}
        return out
    except Exception as e:
        LOG.warning("纠错反馈账本读取失败，按空账继续：%s", e)
        return {}


def _save(db, data: dict) -> bool:
    try:
        db.meta_set(META_KEY, json.dumps(data, ensure_ascii=False))
        return True
    except Exception as e:
        LOG.warning("纠错反馈账本保存失败（本次反馈未记账）：%s", e)
        return False


def _trim(data: dict) -> dict:
    if len(data) <= MAX_WORDS:
        return data
    kept = sorted(data.items(), key=lambda kv: -kv[1]["hits"])[:MAX_WORDS]
    return dict(kept)


def record_rejection(db, tags, act: str = "dislike") -> dict:
    """记一次「用户明确不要这张图」。返回统计，供日志/界面显示。

    ``tags`` 应当是这张图身上带的标签（AI 打的 + 任务词），不是用户手填的
    排除词 —— 手填词本来就走 ``ai_blacklist``，那条路已经硬生效了。

    任何异常都被吞掉：记账失败不能让「不喜欢/删除」这个动作本身失败。
    """
    out = {"ok": False, "added": 0, "hardened": [], "reason": ""}
    if str(act or "") not in REJECT_ACTS:
        out["reason"] = "不是拒收动作"
        return out
    words = clean_tags(tags)
    if not words:
        out["reason"] = "这张图没有可用标签"
        return out
    with _LOCK:
        data = _load(db)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for w in words:
            row = data.get(w) or {"hits": 0, "last": "", "acts": ""}
            before = int(row.get("hits") or 0)
            row["hits"] = before + 1
            row["last"] = now
            acts = [x for x in str(row.get("acts") or "").split(",") if x]
            if act not in acts:
                acts.append(act)
            row["acts"] = ",".join(acts[-4:])
            data[w] = row
            if before + 1 == HARD_HITS:
                out["hardened"].append(w)
            out["added"] += 1
        out["ok"] = _save(db, _trim(data))
        if not out["ok"]:
            out["added"] = 0
            out["hardened"] = []
            out["reason"] = "账本没写上，本次反馈未记账"
    return out


def list_all(db) -> list[dict]:
    """全部反馈词，按「被判掉次数」从多到少。给看板显示用。"""
    with _LOCK:
        data = _load(db)
    items = [{"word": w, "hits": int(v["hits"]),
              "hard": int(v["hits"]) >= HARD_HITS,
              "last": v.get("last", ""), "acts": v.get("acts", "")}
             for w, v in data.items()]
    items.sort(key=lambda x: (-x["hits"], x["word"]))
    return items


def soft_words(db) -> list[str]:
    """只判掉过 1 次的词：进看图排除候选，命中先送审核，不硬拦。"""
    return [x["word"] for x in list_all(db) if not x["hard"]]


def snapshot(db) -> dict:
    """账本整份快照（撤销用）。V0.0.8：审核撤销要连「学到的词」一起回滚。"""
    with _LOCK:
        data = _load(db)
    return {w: dict(v) for w, v in data.items()}


def rollback(db, before: dict | None, after: dict | None) -> int:
    """把「这次操作动过的词」退回操作前状态，返回回滚的词数。

    只处理 before→after 之间有差异的词（本次判退 incremented / 新建的），
    快照之后别的入口新加的词**不碰** —— 撤销一步审核不该抹掉并发写。
    before/after 任一为 None（老快照、读失败）时什么都不做，宁可不动。
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return 0
    touched = set()
    for w, v in after.items():
        b = before.get(w)
        if b is None or int(b.get("hits") or 0) != int(v.get("hits") or 0):
            touched.add(w)
    if not touched:
        return 0
    with _LOCK:
        data = dict(_load(db))
        for w in touched:
            b = before.get(w)
            if b is None:
                data.pop(w, None)
            else:
                data[w] = dict(b)
        _save(db, _trim(data))
    return len(touched)


def hard_words(db) -> list[str]:
    """判掉 ≥2 次的词：升级成「别找什么」，下载前就拦。"""
    return [x["word"] for x in list_all(db) if x["hard"]]


def remove(db, word) -> bool:
    """从账本里删掉一个词（用户觉得学歪了，随时反悔）。"""
    k = str(word or "").strip().lower()
    if not k:
        return False
    with _LOCK:
        data = _load(db)
        if k not in data:
            return False
        del data[k]
        return _save(db, data)


def clear(db) -> int:
    """清空整本账，返回清掉的条数。"""
    with _LOCK:
        n = len(_load(db))
        if not n:
            return 0
        return n if _save(db, {}) else 0
