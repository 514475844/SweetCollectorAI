"""V0.0.46 · 语义搜索 —— 「输入一句话，按画面内容找图」。

原理（和飞牛 Seek 同类）：CLIP 是图文双塔模型，图片编码成向量、文本也
编码成向量，两边余弦越接近就越相关。打标流水线里图片向量本来就算过但
扔掉了，这里把它存进 `images.clip_vec`（float16 字节串，1~1.5KB/张），
搜索时把查询文本编码成一个向量，全库一次矩阵乘法排序，几万张毫秒级。

三条纪律：
  1. **向量必须记来源模型**（clip_model 列）。用户换 AI 档位后新旧向量
     不在同一语义空间（维度都可能不同），搜索只信对得上的那些，
     对不上的提示「重新计算」而不是混着排序出鬼结果。
  2. **中文先过词典再问在线**。CLIP 英文塔对「比基尼」「白丝」这类
     视觉词，词典里现成有（labels_zh.ZH2EN，打标桥接用的同一本，口径
     一致）；词典盖不住的整句（「阳光下沙发」）走已配置的在线 AI 翻成
     英文短语，结果缓存；两条都不通就原样编码并在返回里说明。
  3. **回填是独立可停作业**。历史存量（含采集任务攒下的图）逐张补算，
     进度进作业表，随时可停 —— 停了下回从 ai_state 同款「查 NULL」
     接着来，不另存断点。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

#: 相似度下限：低于这个的「命中」基本是噪声，宁可说没找到。
#: CLIP 余弦 0.20+ 通常已肉眼相关（官方对 0.25 左右算稳命中）。
MIN_SIM = 0.18

#: 单次搜索最多回传多少张（前端灯箱翻页用，全库排序后截断）。
MAX_HITS = 500

_LOCK = threading.Lock()
#: 中文整句 → 英文短语 的在线翻译缓存（进程级，重启即清，无所谓）。
_TR_CACHE: "dict[str, str]" = {}


# ───────────────────────── 向量存取 ─────────────────────────

def vec_to_blob(vec) -> bytes:
    """numpy 向量 → float16 字节串（一半精度省一半空间，排序足够）。"""
    import numpy as np
    return np.asarray(vec, dtype=np.float16).tobytes()


def save_image_vec(db, image_id: int, vec, model_id: str) -> bool:
    """把一张图的向量写库。vec 无效（None/空/全零）返回 False。"""
    if vec is None:
        return False
    try:
        import numpy as np
        a = np.asarray(vec, dtype=np.float32)
        if a.size < 64:
            return False
        blob = vec_to_blob(a)
    except Exception:                                  # noqa: BLE001
        return False
    db.execute("UPDATE images SET clip_vec=?, clip_model=? WHERE id=?",
               (blob, str(model_id or ""), int(image_id)))
    return True


def _load_matrix(db, model_id: str, task_id: int = 0):
    """读全部「对得上模型」的向量，返回 (ids, 归一化矩阵)。

    每次搜索现读现算：3.5 万行 × 1KB ≈ 36MB 读 + 拼接，实测百毫秒级，
    比维护一套缓存失效逻辑划算（库随时被导入/删除改动，缓存一致性
    是更大的坑）。
    """
    import numpy as np
    sql = ("SELECT id, clip_vec FROM images "
           "WHERE clip_vec IS NOT NULL AND clip_model=?")
    args: list = [model_id]
    if task_id:
        sql += " AND task_id=?"
        args.append(int(task_id))
    rows = db.query(sql, args)
    if not rows:
        return [], None
    dim = len(rows[0]["clip_vec"]) // 2
    mat = np.empty((len(rows), dim), dtype=np.float32)
    ids = []
    for i, r in enumerate(rows):
        mat[i] = np.frombuffer(r["clip_vec"], dtype=np.float16).astype(np.float32)
        ids.append(int(r["id"]))
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    mat = mat / np.maximum(norms, 1e-9)
    return ids, mat


# ───────────────────────── 查询翻译 ─────────────────────────

def _cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def translate_query(cfg, query: str) -> tuple[list[str], str]:
    """查询 → 英文候选列表 + 人话说明（界面要告诉用户实际拿什么去搜的）。

    返回 (texts, note)。texts 里中英都可能有：CLIP 英文塔对纯中文基本
    不认，所以中文部分能翻就翻；英文原样保留。
    """
    from .classify.labels_zh import ZH2EN, translate_one

    q = " ".join(str(query or "").split())
    if not q:
        return [], ""
    if not _cjk(q):
        return [q], ""

    texts: list[str] = []
    # ① 逐词/逐片段过词典（打标桥接用的同一本，两边口径一致）
    parts = []
    for token in q.split():
        if not _cjk(token):
            parts.append(token)
            continue
        hit = translate_one(token)
        if hit:
            parts.extend(hit)
            continue
        # 词典没有整词：按「最长键优先」切片段翻（白丝大腿 → white stockings 大腿）
        seg, rest = token, ""
        for key in sorted((k for k in ZH2EN if k in token),
                          key=len, reverse=True):
            en = translate_one(key)
            if en:
                seg = token.replace(key, en[0], 1)
                break
        parts.append(seg)
    dict_en = " ".join(parts)
    if dict_en and not _cjk(dict_en):
        texts.append(dict_en)
        return texts, f"按画面特征「{dict_en}」查找"

    # ② 词典盖不住的整句：问在线 AI（配置了才问；失败静默走 ③）
    key = q
    with _LOCK:
        cached = _TR_CACHE.get(key)
    if cached:
        texts.append(cached)
        return texts, f"按画面特征「{cached}」查找"
    if str(getattr(cfg, "ai_backend", "") or "") == "api":
        try:
            from .adapter_ai import chat
            ans = chat(cfg,
                       "你是图片检索查询翻译器。把用户的中文查询翻译成"
                       "一段英文的图片描述短语（CLIP 风格，逗号分隔的视觉"
                       "特征，不要句子、不要解释、只输出英文）。",
                       q, timeout=20).strip()
            ans = ans.splitlines()[0][:120] if ans else ""
            if ans and not _cjk(ans):
                with _LOCK:
                    if len(_TR_CACHE) > 512:
                        _TR_CACHE.clear()
                    _TR_CACHE[key] = ans
                texts.append(ans)
                return texts, f"按画面特征「{ans}」查找"
        except Exception:                              # noqa: BLE001
            pass

    # ③ 兜底：原样编码（多半是纯中文词表外句子），如实说明效果可能差
    texts.append(q)
    return texts, "这句没翻出英文特征，按原文硬找，结果可能不准"


# ───────────────────────── 搜索 ─────────────────────────

def make_classifier(cfg, model_dir_hint: str = ""):
    """给搜索/回填建一个 CLIP 分类器（不需要标签词表，只要双塔编码）。"""
    import os
    from .classify.clip_model import LocalClassifier
    threads = int(getattr(cfg, "ai_threads", 0) or 0)
    if threads <= 0:
        threads = max(1, (os.cpu_count() or 4) // 2)
    clf = LocalClassifier(labels=[],
                          model_dir=(model_dir_hint or
                                     getattr(cfg, "ai_model_path", "") or None),
                          threads=threads,
                          gpu=bool(getattr(cfg, "ai_gpu", True)))
    return clf


def search(db, cfg, query: str, *, task_id: int = 0, limit: int = 200):
    """语义搜索主入口。

    返回 ``{"ok": True, "hits": [(image_id, sim), ...], "total": N,
    "note": 人话, "model": 档位}``。**只回 id + 相似度** —— 把行拼成前端
    要的 item（灯箱大图地址、flag、i18n 标签等）交给调用方复用现成的
    `_img_item`，不在这里重抄一套字段映射。失败时回 ``{"ok": False, ...}``。
    """
    import numpy as np
    query = str(query or "").strip()[:80]
    if not query:
        return {"ok": False, "error": "先输入要找的画面特征"}
    limit = max(1, min(int(limit or 200), MAX_HITS))

    clf = make_classifier(cfg)
    if getattr(clf, "backend", "") != "clip":
        return {"ok": False,
                "error": "本地 AI 模型不可用，按意思找需要 CLIP 模型（AI 模型页可下载）"}
    model_id = clf.model_id

    ids, mat = _load_matrix(db, model_id, task_id)
    if mat is None:
        return {"ok": False,
                "error": "图库还没有可用的画面向量，请先点「补算画面特征」",
                "need_backfill": True}

    texts, note = translate_query(cfg, query)
    if not texts:
        return {"ok": False, "error": "查询是空的"}
    try:
        te = clf._clip.encode_text(texts)              # (k, dim) 已归一化
    except Exception as e:                              # noqa: BLE001
        return {"ok": False, "error": f"查询编码失败：{e}"}
    if te.shape[1] != mat.shape[1]:
        return {"ok": False,
                "error": f"当前模型（{model_id}）和已存向量维度对不上，请重新补算画面特征",
                "need_backfill": True}

    sims = (mat @ te.T).max(axis=1)
    order = np.argsort(-sims)[:limit]
    hits = [(int(ids[i]), float(sims[i])) for i in order
            if float(sims[i]) >= MIN_SIM]
    return {"ok": True, "hits": hits, "total": int(mat.shape[0]),
            "note": note or "", "model": model_id}


def pending_count(db, model_id: str) -> int:
    """还差多少张没算向量（含「算过但模型换了」的）。"""
    try:
        n = int(db.query(
            "SELECT COUNT(*) c FROM images WHERE clip_vec IS NULL "
            "OR (clip_model<>? AND clip_model<>'')", (model_id,))[0]["c"] or 0)
    except Exception:                                  # noqa: BLE001
        n = 0
    return n


# ───────────────────────── 回填作业 ─────────────────────────

def backfill(db, cfg, jobs_mod, jid: str, *, stop_flag, workers: int = 0) -> dict:
    """给全库缺向量的图补算。可停：stop_flag() 真就尽快收工。

    与打标同款节奏：短事务外并行推理、一批一次写回，界面查询不卡。
    """
    import os
    from concurrent.futures import ThreadPoolExecutor

    clf = make_classifier(cfg)
    if getattr(clf, "backend", "") != "clip":
        jobs_mod.update(jid, state="failed",
                        error="本地 AI 模型不可用，无法补算画面特征")
        return {"ok": False}
    model_id = clf.model_id

    total = pending_count(db, model_id)
    if total <= 0:
        jobs_mod.update(jid, state="done", pct=100,
                        message="所有图片都已有画面特征", done=0, total=0)
        return {"ok": True, "done": 0, "total": 0}

    if workers <= 0:
        workers = int(getattr(cfg, "ai_threads", 0) or 0)
    workers = max(1, min(8, workers or max(1, (os.cpu_count() or 4) // 2)))

    done = saved = 0
    BATCH = 64
    t0 = time.time()
    jobs_mod.update(jid, total=total, pct=0,
                    message=f"正在补算画面特征 0 / {total:,}")
    while True:
        if stop_flag():
            jobs_mod.update(jid, state="stopped",
                            message=f"已停止：补算了 {saved:,} / {total:,}")
            break
        rows = db.query(
            "SELECT id, path FROM images WHERE clip_vec IS NULL "
            "OR (clip_model<>? AND clip_model<>'') ORDER BY id LIMIT ?",
            (model_id, BATCH))
        if not rows:
            jobs_mod.update(jid, state="done", pct=100,
                            message=f"补算完成，共 {saved:,} 张",
                            done=saved, total=total)
            break

        def _one(item):
            rid, p = item
            if stop_flag():
                return rid, None
            try:
                return rid, clf.encode_image_vec(Path(p))
            except Exception:                          # noqa: BLE001
                return rid, None

        items = [(int(r["id"]), str(r["path"] or "")) for r in rows]
        with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
            results = list(ex.map(_one, items))
        with db.transaction():
            for rid, vec in results:
                done += 1
                if vec is not None and save_image_vec(db, rid, vec, model_id):
                    saved += 1
        pct = min(99, int(done * 100 / max(1, total)))
        el = time.time() - t0
        eta = f" · 约剩 {int((total - done) * el / max(1, done))} 秒" if done else ""
        jobs_mod.update(jid, pct=pct, done=done, total=total,
                        message=f"正在补算画面特征 {done:,} / {total:,}{eta}")
    return {"ok": True, "done": done, "saved": saved, "total": total}
