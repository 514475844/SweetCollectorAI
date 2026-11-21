# -*- coding: utf-8 -*-
"""V0.0.71 · AI 看图自己说词 → 用户勾选 → 变成口味。

用户要的闭环（原话大意）：
    「让它自己生成词，然后在首页让我筛选哪些词是我需要的，
      这才是让 AI 学习我自己图库的目的。」

与旧的 ``learn_imported`` 区别：
    · 旧：只数图片上**已有**的标签（那些标签当初也是从候选词表里挑的），
         等于"把用户已选的词再统计一遍"，跳不出原词表。
    · 新：真的重新看图，让模型**说出**画面里有什么，汇总成候选词清单给用户勾。
         勾选结果再写回偏好表（正权/负权），接回采集链路。

三种生成方式（``method``）：
    vocab  —— 内置通用词库 + 本地 CLIP 逐图打分，统计高频命中词。
              离线、快、不花钱；词只能来自词库（但词库已扩到约 300 词）。
    local  —— 本地图像描述模型（BLIP 之类）逐图生成描述再抽词。
              真能造词；需要额外下载模型，本环境尚未内置 ⇒ 明确降级提示。
    api    —— 外部视觉大模型（OpenAI 兼容 /chat/completions）描述画面再抽词。
              真能造词；需要用户配置能看图的 API，未配置 ⇒ 明确降级提示。

三者输出统一：``list[{"word": str, "hits": int, "samples": [id, ...]}]``，
按 hits 降序。勾选写库走 ``apply_choices``，与 ``repo.set_preference`` 同一口径。

设计纪律：
    · 扫描跑在 webui 的后台作业线程里，本模块只管算法、不碰 HTTP；
    · progress(pct, msg) 由调用方注入，绝不在本模块里起线程；
    · 任何一步失败都返回已算出的部分结果 + 明确提示，不让整批白跑。
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

LOG = logging.getLogger("mrm.auto_vocab")

# vocab 方式的命中阈值：CLIP 对这些通用词的打分，0.15 以上才算"图里真有"。
# 阈值太低会把所有词都算命中（词频失真），太高则只剩最显著的几个。
VOCAB_THRESHOLD = 0.18
# 每张图最多计入几个词：避免一张图给几十个词都 +1（刷高频）。
TOP_PER_IMAGE = 4

# 未配置时的降级提示（写进作业 message，前端直接显示）
# V0.0.74：local 已真正接入 BLIP，不再有"暂不可用"死提示；
# 这里只留 api 方式未配置的提示。
_LOCAL_MISSING = ("本地描述模型还没下载。去「AI 模型」页找"
                  "「BLIP 看图说话（AI 造词用）」点下载（约 950 MB），下完再来扫。")
_API_NOT_READY = "外部视觉模型未配置，请在设置里填写能看图的 API 地址后重试；本次未生成词。"


# ── 通用词库（vocab 方式候选）──────────────────────────────────────────
# V0.0.74 起：词库**直接从 labels_zh.ZH2EN 派生**——每个词都保证有英文映射，
# CLIP（含英文模型）才认得出；以前手写清单里 53 个词没映射、打分近乎随机。
# 想加词：在 labels_zh.ZH2EN 里加「中文: 英文」即可，本词库自动跟上。
VOCAB_WORDS: list[str] = []


def _vocab_words() -> list[str]:
    if not VOCAB_WORDS:
        from .classify.labels_zh import ZH2EN
        VOCAB_WORDS.extend(ZH2EN.keys())
    return VOCAB_WORDS


def _uniq(seq: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for x in seq:
        k = str(x or "").strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out


def _expand_candidates(db=None) -> tuple[list[str], dict[str, str]]:
    """把词库展开成 CLIP 候选标签，并留"展开词 → 原词"的反查表。

    统计时按**原词**聚合：白丝 与 white stockings 的命中都算到"白丝"头上，
    否则同一概念被拆成两行、谁都上不了高频。
    """
    from .classify.labels_zh import expand_labels
    labels: list[str] = []
    origin: dict[str, str] = {}
    for w in _vocab_words():
        for x in expand_labels([w]):
            labels.append(x)
            origin.setdefault(x.lower(), w)
    return _uniq(labels), origin


def _sample_rows(db, amount: str) -> list[dict]:
    """从图库抽样图片。amount: '1000' / '5000' / 'all'。"""
    try:
        n = int(amount)
    except (TypeError, ValueError):
        n = 1000
    if amount == "all" or n <= 0:
        sql = ("SELECT id, path FROM images WHERE path IS NOT NULL "
               "AND in_quarantine=0 AND blacklisted=0 ORDER BY id DESC")
        return list(db.query(sql))
    # 抽样：SQLite 的 ORDER BY RANDOM() 对十万行也够快（本机实测毫秒级），
    # 比在 Python 里 load 全表再 sample 省内存。
    sql = ("SELECT id, path FROM images WHERE path IS NOT NULL "
           "AND in_quarantine=0 AND blacklisted=0 "
           "ORDER BY RANDOM() LIMIT ?")
    return list(db.query(sql, (int(n),)))


def _new_classifier(labels: list[str], cfg):
    """复用现有的 CLIP 分类器构造（与 engine._prepare_ai 同口径）。"""
    from .classify.clip_model import LocalClassifier
    from .config import ai_device_effective
    return LocalClassifier(
        labels=labels,
        model_dir=(getattr(cfg, "ai_model_path", "") or None),
        threads=int(getattr(cfg, "ai_threads", 0) or 0),
        gpu=bool(getattr(cfg, "ai_gpu", True)),
        device=ai_device_effective(cfg),
    )


def scan_vocab(db, cfg, amount: str, progress: Callable[[int, str], None],
               stop_check: Callable[[], bool] | None = None) -> dict:
    """内置词库方式：抽样 → CLIP 逐图打分 → 按原词聚合命中。"""
    labels, origin = _expand_candidates(db)
    clf = _new_classifier(labels, cfg)
    if getattr(clf, "backend", "stub") == "stub":
        return {"words": [], "warning": "本地 AI 模型不可用（CLIP 未就绪），本次未生成词。"}

    rows = _sample_rows(db, amount)
    total = len(rows)
    if not total:
        return {"words": [], "warning": "图库里没有可扫描的图片。"}

    hits: dict[str, int] = defaultdict(int)
    samples: dict[str, list[int]] = defaultdict(list)
    scanned = 0
    stopped = False
    for r in rows:
        if stop_check is not None and stop_check():
            stopped = True
            break
        try:
            pairs = clf.classify(Path(r["path"]))
        except Exception as e:  # 单张坏图不该中断整批
            LOG.debug("跳过高分图 id=%s: %s", r["id"], e)
            pairs = []
        counted = 0
        for lab, score in pairs:
            if score < VOCAB_THRESHOLD or counted >= TOP_PER_IMAGE:
                continue
            word = origin.get(str(lab).strip().lower())
            if not word:
                continue
            hits[word] += 1
            if len(samples[word]) < 6:
                samples[word].append(int(r["id"]))
            counted += 1
        scanned += 1
        if scanned % 20 == 0 or scanned == total:
            progress(int(scanned * 90 / total),
                     f"已看图 {scanned}/{total} 张")
        if scanned % 50 == 0 and scanned > 0:
            # 与 scan_local/scan_api 同口径：每 50 张落盘，长扫描中途停/重启
            # 都不丢已扫出的词（V0.0.78 补齐，vocab 方式以前只在末尾存）。
            interim = [{"word": w, "hits": c, "samples": samples.get(w, [])}
                       for w, c in sorted(hits.items(), key=lambda kv: -kv[1])]
            save_last(db, {"words": interim, "scanned": scanned,
                           "total": total, "method": "vocab",
                           "stopped": False, "partial": True,
                           "ts": time.time()})

    words = [
        {"word": w, "hits": c, "samples": samples.get(w, [])}
        for w, c in sorted(hits.items(), key=lambda kv: -kv[1])
    ]
    out = {"words": words, "scanned": scanned, "total": total,
           "method": "vocab", "stopped": stopped}
    if stopped:
        progress(95, f"已停止：扫了 {scanned} 张，说出 {len(words)} 个词（成果已保留）")
    else:
        progress(95, f"统计完成，共 {len(words)} 个候选词")
    save_last(db, dict(out, ts=time.time()))
    return out


def _blip_model_dir(db) -> Path:
    """描述模型目录 = 数据目录/models/blip-caption-base（与模型库同口径）。"""
    from .config import user_data_dir
    return user_data_dir() / "models" / "blip-caption-base"


def scan_local(db, cfg, amount: str, progress: Callable[[int, str], None],
               stop_check: Callable[[], bool] | None = None) -> dict:
    """本地描述模型方式：BLIP 逐图生成英文描述 → 拆词聚合。

    真·AI 自己造词——不依赖词表，描述里出现什么名词就统计什么。
    慢是预期内的（用户原话：哪怕 10 分钟蹦一个词也证明功能正常）。
    V0.0.76：支持随时停止（stop_check），且每 50 张把成果落盘一次，
    中断/重启都不丢已扫出的词。
    """
    from .classify.blip_caption import (BlipCaptioner, extract_words,
                                        model_ready)
    mdir = _blip_model_dir(db)
    if not model_ready(mdir):
        return {"words": [], "method": "local", "warning":
                "本地描述模型还没下载。去「AI 模型」页找「BLIP 看图说话（AI 造词用）」点下载（约 950 MB），下完再来扫。"}
    progress(3, "加载描述模型…")
    try:
        cap = BlipCaptioner(mdir)
    except Exception as e:
        LOG.exception("BLIP 加载失败")
        return {"words": [], "method": "local", "warning":
                f"描述模型加载失败：{type(e).__name__}: {e}"}

    rows = _sample_rows(db, amount)
    total = len(rows)
    if not total:
        return {"words": [], "method": "local",
                "warning": "图库里没有可扫描的图片。"}

    hits: dict[str, int] = defaultdict(int)
    samples: dict[str, list[int]] = defaultdict(list)
    scanned = ok_cnt = 0
    stopped = False
    for r in rows:
        if stop_check is not None and stop_check():
            stopped = True
            break
        try:
            text = cap.caption(Path(r["path"]))
            ok_cnt += 1
        except Exception as e:  # 单张坏图/坏文件不中断整批
            LOG.debug("BLIP 跳过 id=%s: %s", r["id"], e)
            text = ""
        for w in extract_words(text):
            hits[w] += 1
            if len(samples[w]) < 6:
                samples[w].append(int(r["id"]))
        scanned += 1
        if scanned % 5 == 0 or scanned == total:   # BLIP 慢，进度勤报
            progress(int(scanned * 92 / total),
                     f"已描述 {scanned}/{total} 张（成功 {ok_cnt}）")
        if scanned % 50 == 0 and scanned > 0:      # 每 50 张落盘，随时可断
            interim = [{"word": w, "hits": c, "samples": samples.get(w, [])}
                       for w, c in sorted(hits.items(), key=lambda kv: -kv[1])]
            save_last(db, {"words": interim, "scanned": scanned,
                           "total": total, "method": "local",
                           "stopped": False, "partial": True,
                           "ts": time.time()})

    words = [{"word": w, "hits": c, "samples": samples.get(w, [])}
             for w, c in sorted(hits.items(), key=lambda kv: -kv[1])]
    out = {"words": words, "scanned": scanned, "total": total,
           "method": "local", "stopped": stopped}
    if stopped:
        progress(95, f"已停止：描述了 {scanned} 张，说出 {len(words)} 个词（成果已保留）")
    elif ok_cnt == 0:
        out["warning"] = "所有图都描述失败——模型文件可能损坏，建议删掉重下。"
        progress(95, f"统计完成，共 {len(words)} 个候选词")
    else:
        progress(95, f"统计完成，共 {len(words)} 个候选词")
    save_last(db, dict(out, ts=time.time()))
    return out



def scan_api(db, cfg, amount: str, progress: Callable[[int, str], None],
             stop_check: Callable[[], bool] | None = None) -> dict:
    """外部视觉大模型方式：把图发给配置的接口，让它说出画面里的词。

    复用 ApiClassifier 的 OpenAI 兼容 /chat/completions 通道，但提示词不同：
    不是"给词表打分"，而是"自由描述 → 抽 5~12 个关键词"。
    没配置接口时明确降级；配置了就能直接测（用户在设置里填即可）。
    """
    base = (getattr(cfg, "ai_api_url", "") or "").strip()
    if not base:
        return {"words": [], "warning": _API_NOT_READY, "method": "api"}
    key = getattr(cfg, "ai_api_key", "") or ""
    model = getattr(cfg, "ai_api_model", "") or "gpt-4o-mini"

    import base64
    import json as _json
    import urllib.request as _rq

    sys_prompt = (
        "看图说词。列出这张图里最重要的视觉概念（物体/人物/服饰/场景/风格），"
        "5 到 12 个，全部用小写英文单词或两三个词的短语，只输出一个 JSON 数组，"
        "例如 [\"woman\",\"white stockings\",\"beach\"]。不要输出其他文字。")

    def _ask(path: Path) -> list[str]:
        b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        payload = {"model": model, "temperature": 0,
                   "messages": [{"role": "user", "content": [
                       {"type": "text", "text": sys_prompt},
                       {"type": "image_url", "image_url": {
                           "url": f"data:image/jpeg;base64,{b64}"}}]}]}
        req = _rq.Request(
            base.rstrip("/") + "/chat/completions",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {key}"} if key else {})},
            method="POST")
        with _rq.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content")
        if isinstance(text, list):
            text = "".join(str(p.get("text", "")) for p in text
                           if isinstance(p, dict))
        if not text:
            text = msg.get("text", "")
        m = re.search(r"\[.*\]", text or "", re.S)
        if not m:
            return []
        try:
            arr = _json.loads(m.group(0))
        except Exception:
            return []
        return [str(x).strip().lower() for x in arr
                if isinstance(x, str) and 1 < len(str(x).strip()) < 40]

    rows = _sample_rows(db, amount)
    total = len(rows)
    if not total:
        return {"words": [], "warning": "图库里没有可扫描的图片。",
                "method": "api"}

    hits: dict[str, int] = defaultdict(int)
    samples: dict[str, list[int]] = defaultdict(list)
    scanned = ok_cnt = 0
    fail_streak = 0
    stopped = False
    early_warn = None
    for r in rows:
        if stop_check is not None and stop_check():
            stopped = True
            break
        try:
            ws = _ask(Path(r["path"]))
            ok_cnt += 1
            fail_streak = 0
        except Exception as e:
            LOG.debug("API 描述失败 id=%s: %s", r["id"], e)
            ws = []
            fail_streak += 1
            if fail_streak >= 8:   # 接口明显不可用（key 错/断网），早停省钱
                early_warn = (f"外部接口连续 {fail_streak} 次失败，已提前停止。"
                              f"请检查 API 地址/密钥/模型名（最后错误：{type(e).__name__}）。")
                progress(50, "接口连续失败，提前停止（已扫部分保留）")
                break                # 不再直接 return——已扫出的词要保留并落盘
        for w in ws:
            hits[w] += 1
            if len(samples[w]) < 6:
                samples[w].append(int(r["id"]))
        scanned += 1
        if scanned % 10 == 0 or scanned == total:
            progress(int(scanned * 92 / total),
                     f"已描述 {scanned}/{total} 张（成功 {ok_cnt}）")
        if scanned % 50 == 0 and scanned > 0:
            interim = [{"word": w, "hits": c, "samples": samples.get(w, [])}
                       for w, c in sorted(hits.items(), key=lambda kv: -kv[1])]
            save_last(db, {"words": interim, "scanned": scanned,
                           "total": total, "method": "api",
                           "stopped": False, "partial": True, "ts": time.time()})

    words = [{"word": w, "hits": c, "samples": samples.get(w, [])}
             for w, c in sorted(hits.items(), key=lambda kv: -kv[1])]
    if stopped:
        progress(95, f"已停止：描述了 {scanned} 张，说出 {len(words)} 个词（成果已保留）")
    else:
        progress(95, f"统计完成，共 {len(words)} 个候选词")
    out = {"words": words, "scanned": scanned, "total": total,
           "method": "api", "stopped": stopped}
    if early_warn:
        out["warning"] = early_warn
    elif ok_cnt == 0:
        out["warning"] = "所有请求都失败了——请确认接口能看图（多模态）并且配置正确。"
    save_last(db, dict(out, ts=time.time()))
    return out



def scan(db, cfg, method: str, amount: str,
         progress: Callable[[int, str], None],
         stop_check: Callable[[], bool] | None = None) -> dict:
    progress(2, "准备看图…")
    if method == "local":
        return scan_local(db, cfg, amount, progress, stop_check)
    if method == "api":
        return scan_api(db, cfg, amount, progress, stop_check)
    return scan_vocab(db, cfg, amount, progress, stop_check)


# ── 扫描成果落盘（V0.0.76）────────────────────────────────────────────
# 全库扫描可能跑十几个小时，进程重启/用户停止都不能让已扫出的词白白丢掉。
# 每扫 50 张把当前成果 JSON 写进 app_meta（单行覆盖，成本可忽略）。
LAST_KEY = "autovocab.last"


def save_last(db, payload: dict) -> None:
    import json
    try:
        db.meta_set(LAST_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001
        LOG.debug("autovocab 落盘失败: %s", e)


def last_scan(db) -> dict | None:
    import json
    raw = db.meta_get(LAST_KEY)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _words_of(result: dict) -> list[dict]:
    return result.get("words") or []


# ── 两种学习法的对比数据（看板用）─────────────────────────────────────
_TAGSET_CACHE: dict = {}


def _tag_wordset(db) -> set:
    """全库图片标签的去重集合（300 秒缓存——十万行不必每次重算）。"""
    import time as _t
    key = "tags"
    now = _t.time()
    hit = _TAGSET_CACHE.get(key)
    if hit and now - hit[0] < 300:
        return hit[1]
    words: set = set()
    try:
        for r in db.query("SELECT tags FROM images WHERE tags IS NOT NULL AND tags != ''"):
            for w in str(r["tags"] or "").replace("，", ",").split(","):
                w = w.strip()
                if w:
                    words.add(w.lower())
    except Exception as e:  # noqa: BLE001
        LOG.debug("标签集合统计失败: %s", e)
    _TAGSET_CACHE[key] = (now, words)
    return words


def compare_stats(db) -> dict:
    """学习看板的"实实在在的数据区别"：标签统计 vs AI 看图造词。

    - tag_based：从已有标签学（旧方式）的口径——全库有多少种标签词。
    - ai_scan：最近一次 AI 扫描的成果——扫了多少张、说出多少词、是不是停止的。
    - novel：AI 说出来、但全库标签里从来没出现过的词 = AI 造的新词，
      这正是两种方式的核心差别：旧方式只会用认识的词，AI 能冒出词表外的词。
    """
    from .repo import get_preferences
    prefs = get_preferences(db)
    last = last_scan(db) or {}
    tagset = _tag_wordset(db)
    ai_words = [str(w.get("word", "")) for w in (last.get("words") or [])]
    novel = [w for w in ai_words if w and w.lower() not in tagset]
    return {
        "tag_based": {
            "distinct_tags": len(tagset),
            "learned_words": len(prefs),
        },
        "ai_scan": {
            "method": str(last.get("method") or ""),
            "scanned": int(last.get("scanned") or 0),
            "total": int(last.get("total") or 0),
            "words": len(ai_words),
            "stopped": bool(last.get("stopped")),
            "ts": float(last.get("ts") or 0),
            "top": (last.get("words") or [])[:12],
        },
        "novel_words": novel[:60],
        "novel_count": len(novel),
    }


# ── 勾选结果写回口味库 ────────────────────────────────────────────────
def apply_choices(db, yes: list[str], no: list[str]) -> dict:
    """把勾选结果写进偏好表：要的 = 正权，不要的 = 负权。

    与 ``learn_imported`` / 手动喜欢共用 ``set_preference``（同一张表、同一口径），
    写完采集链路（find_terms.collect → engine 打分）自然生效。
    幂等：重复应用同样的勾选不会把权重越堆越高（set_preference 覆盖式写入）。

    ⚠ 来源用 ``user_typed``：这是用户**亲手勾的**，是他明确表达的口味，
      不是"模型自动猜的"。这样正权在 find_terms 里排最高优先级，负权进
      "用户明确不要"的黑名单 —— 正是"我来筛选哪些词是我需要的"的本意。
    """
    from .repo import PREF_ORIGIN_USER
    w_yes, w_no = 2.0, -2.0
    n_yes = n_no = 0
    for w in _uniq(yes or []):
        try:
            _set_pref_abs(db, w, w_yes, "pos", PREF_ORIGIN_USER)
            n_yes += 1
        except Exception as e:
            LOG.warning("写正偏好失败 %s: %s", w, e)
    for w in _uniq(no or []):
        try:
            _set_pref_abs(db, w, w_no, "neg", PREF_ORIGIN_USER)
            n_no += 1
        except Exception as e:
            LOG.warning("写负偏好失败 %s: %s", w, e)
    return {"ok": True, "accepted": n_yes, "rejected": n_no}


def _set_pref_abs(db, word, target: float, kind: str, origin: str) -> None:
    """把某词的权重**设为** target（不是累加）。

    repo.set_preference 的 UPSERT 语义是 ``weight = weight + excluded.weight``，
    它给"每命中一次加一点"的学习场景用。但勾选/自动入库是"这个词就该是
    这个分量"，重复应用必须幂等——直接传 target 会 2→4→6 无限膨胀
    （V0.0.78 实测发现的真 bug）。这里按"当前总量"算差额写入，口径与
    set_preference 内部的大小写合并完全一致（先合计所有 lower(label) 相同的
    行，再传差额；内部合并变体行后 UPSERT 正好落到 target）。
    """
    from .repo import GLOBAL_TASK_ID, _norm_label, set_preference
    key = _norm_label(word)
    rows = db.query(
        "SELECT weight FROM preferences WHERE task_id=? AND lower(label)=?",
        (GLOBAL_TASK_ID, key))
    cur = sum(float(r["weight"] or 0.0) for r in rows)
    set_preference(db, 0, key, float(target) - cur, kind=kind, origin=origin)


# ── V0.0.77：扫完自动入口味库（无需手动勾选）────────────────────────
# 用户定稿：白名单 = 口味库，是同一个东西；AI 造的词按出现频率自动增量
# 加进去，频率决定分量（高频 = 核心词、优先找；低频 = 细分词、权重低）。
# 采集侧 _learned_words() 已把正权词按权重排序送进 want，写进来就自然生效。
AUTO_W_BASE = 0.6       # 低频细分词
AUTO_W_HIGH = 2.0       # 高频核心词（与手动勾选的 +2.0 同档）
AUTO_W_CAP = 3.0        # 权重上限（不超过 repo 的 PREF_WEIGHT_CAP）
AUTO_MIN_HITS = 2       # 只出现 1 次的词当噪声丢掉


def _freq_weight(hits: int, max_hits: int, min_hits: int) -> float:
    """出现频率 → 口味权重。频率越高权重越高（核心词）。

    两段合成：
      · 相对档位（本次扫描内排名）：最低 0.6，最高 +1.4；
      · 绝对频次加成：命中越多越"典型"，用对数压刻度（10→+0.3, 100→+0.6），
        这样跨多次扫描时，同一词攒了更多命中也能单调上调，不会被"这次只有
        它一个词"卡在满档。
    最终夹到 [AUTO_W_BASE, AUTO_W_CAP]。
    """
    import math
    if max_hits <= min_hits:
        # 只有一个词达标 / 全体同频：没有"相对高低"可言，别把大家一律顶到
        # 满档（否则单次只出一个词也被当成核心词，权重虚高）。此时完全由
        # 绝对频次加成决定分量。
        rel = 0.0
    else:
        span = max_hits - min_hits
        rel = max(0.0, min(1.0, (max(0, int(hits)) - min_hits) / span))
    absolute = math.log10(max(1, int(hits))) * 0.3   # 10→0.3, 100→0.6
    raw = AUTO_W_BASE + (AUTO_W_HIGH - AUTO_W_BASE) * rel + absolute
    return round(max(AUTO_W_BASE, min(AUTO_W_CAP, raw)), 3)


def auto_apply_scan(db, result: dict | None = None) -> dict:
    """把一次扫描结果按频率自动写进口味库。扫描完成/停止都会调用。

    行为：
      · 从 ``last_scan`` 或传入的 result 取词；只收 hits>=AUTO_MIN_HITS 的词；
      · 频率→权重（见 ``_freq_weight``），origin=model_auto（机器自动词，
        不是用户亲手勾的，保留将来"一键清机器词"的能力）；
      · **不覆盖用户手填/手动勾选的词**（那些 origin=user_typed）——只补
        机器词，用户明确表达过的口味永远优先；
      · 同一概念已在口味库时：机器词只在"新权重更高"时上调，不会把用户
        或上一轮的高权重压低（幂等、单调，不会越扫越虚高）。

    返回统计，供扫描作业 message 显示"已自动加入 N 个词到口味库"。
    """
    from .repo import (PREF_ORIGIN_USER, PREF_ORIGIN_IMPORT,
                       PREF_ORIGIN_MODEL)
    result = result or last_scan(db) or {}
    words = _words_of(result)
    if not words:
        return {"ok": True, "added": 0, "updated": 0, "skipped": 0}

    usable = []
    for w in words:
        word = str(w.get("word") or "").strip()
        hits = int(w.get("hits") or 0)
        if word and hits >= AUTO_MIN_HITS:
            usable.append((word, hits))
    if not usable:
        return {"ok": True, "added": 0, "updated": 0, "skipped": len(words)}

    max_hits = max(h for _, h in usable)
    # 现有词的 origin + 权重：用户手填/导入的不许碰；机器词只上调不压低。
    existing = _pref_state(db)
    added = updated = skipped = 0
    for word, hits in usable:
        key = word.strip().lower()
        state = existing.get(key)
        if state and state[0] in (PREF_ORIGIN_USER, PREF_ORIGIN_IMPORT):
            skipped += 1
            continue
        weight = min(AUTO_W_CAP, _freq_weight(hits, max_hits, AUTO_MIN_HITS))
        if state is not None and state[1] >= weight:
            skipped += 1          # 已有更高/相等权重，幂等跳过
            continue
        try:
            _set_pref_abs(db, word, weight, "pos", PREF_ORIGIN_MODEL)
            if state is None:
                added += 1
            else:
                updated += 1
        except Exception as e:  # noqa: BLE001
            LOG.warning("自动写入口味词失败 %s: %s", word, e)
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped}


def _pref_state(db) -> dict:
    """读当前口味库：{小写词: (origin, weight)}。单次查询，避免逐词查库。"""
    out: dict = {}
    try:
        from .repo import _pref_origin
        rows = db.query("SELECT label, weight, source FROM preferences "
                        "WHERE task_id=0")
        for r in rows:
            k = str(r["label"] or "").strip().lower()
            if k:
                out[k] = (_pref_origin(r), float(r["weight"] or 0.0))
    except Exception as e:  # noqa: BLE001
        LOG.debug("读口味库状态失败: %s", e)
    return out
