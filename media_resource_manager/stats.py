"""Statistics: library analytics + run performance numbers.

Everything is computed from the local SQLite DB and the library folder -
nothing leaves the machine.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .db import Database

EXT_LABELS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF",
              "webp": "WebP", "avif": "AVIF", "bmp": "BMP", "tiff": "TIFF"}


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} PB"


def _existing_paths(paths) -> set:
    """批量判断哪些文件真实存在。

    r40 扫描实录：旧写法对 7180 张图逐条 Path(path).exists()，启动首帧就把
    主线程堵死 35 秒（看门狗 qt_loop=STALL 连续 6 次，栈全停在 stats.py:30）。
    现在改成「先 os.scandir 把父目录列一遍，再做纯字符串比较」：
    只有真正出现过的父目录才会去列，目录数从 7000 降到几十，耗时基本为零。
    列不到的目录一律当作不存在（与 exists() 的语义一致）。
    """
    dirs = {}
    for p in paths:
        if p:
            d = os.path.dirname(p)
            if d not in dirs:
                names = None
                try:
                    with os.scandir(d) as it:
                        names = {e.name for e in it}
                except OSError:
                    names = set()
                dirs[d] = names
    hit = set()
    for p in paths:
        if not p:
            continue
        d = os.path.dirname(p)
        if os.path.basename(p) in dirs.get(d, ()):
            hit.add(p)
    return hit


def library_stats(db: Database, library_dir: Path) -> dict:
    rows = db.query(
        # sha256 是需求 21「按任务重复率」必须的列，别再漏（漏了直接 IndexError）。
        """SELECT id, path, bytes, width, height, score, blacklisted,
                  in_quarantine, favorite, task_id, collected_at, sha256
           FROM images""")
    total = len(rows)
    exists = _existing_paths([r["path"] for r in rows])
    on_disk = [r for r in rows if r["path"] and r["path"] in exists]
    bytes_total = sum((r["bytes"] or 0) for r in on_disk)
    for r in on_disk:                       # fill in missing DB sizes
        if not r["bytes"]:
            try:
                bytes_total += Path(r["path"]).stat().st_size
            except Exception:
                pass

    by_ext: dict[str, int] = {}
    for r in on_disk:
        ext = Path(r["path"]).suffix.lstrip(".").lower()
        label = EXT_LABELS.get(ext, ext or "?")
        by_ext[label] = by_ext.get(label, 0) + 1

    per_task: dict[int, int] = {}
    for r in rows:
        per_task[r["task_id"]] = per_task.get(r["task_id"], 0) + 1

    # 需求 21：按任务的「重复率 + 平均 AI 分」分布。
    # 原来只有全局一个值，看不出「哪个站点抓回来的图重复最多 / AI 分最低」，
    # 而这正是选任务时最该看的两件事。全在已取出的 rows 上算，不额外扫表。
    # 注意 rows 是 sqlite3.Row（不是 dict），要用 r["列名"] 取值。
    per_task_quality: dict[int, dict] = {}
    seen_sha: dict[tuple, int] = {}
    for r in rows:
        tid = r["task_id"]
        d = per_task_quality.get(tid)
        if d is None:
            d = per_task_quality[tid] = {"n": 0, "score_sum": 0.0,
                                         "dup": 0, "quar": 0}
        d["n"] += 1
        d["score_sum"] += float(r["score"] or 0.0)
        if r["in_quarantine"]:
            d["quar"] += 1
        h = r["sha256"]
        if h:
            k = (tid, h)
            seen_sha[k] = seen_sha.get(k, 0) + 1
            if seen_sha[k] > 1:      # 同一任务内第 2 张起算重复
                d["dup"] += 1
    for d in per_task_quality.values():
        n = d["n"] or 1
        d["dup_rate"] = d["dup"] / n * 100
        d["avg_score"] = d["score_sum"] / n
        d.pop("score_sum", None)

    sha_rows = db.query(
        "SELECT sha256, COUNT(*) c FROM images WHERE sha256 IS NOT NULL "
        "AND in_quarantine=0 GROUP BY sha256 HAVING c>1")
    dup_files = sum(int(r["c"]) - 1 for r in sha_rows)
    kept = total - sum(1 for r in rows if r["in_quarantine"])
    dup_rate = (dup_files / kept * 100) if kept else 0.0

    return {
        "total_records": total,
        "on_disk": len(on_disk),
        "quarantined": sum(1 for r in rows if r["in_quarantine"]),
        "blacklisted": sum(1 for r in rows if r["blacklisted"]),
        "favorites": sum(1 for r in rows if r["favorite"]),
        "bytes_total": bytes_total,
        "bytes_human": _human(bytes_total),
        "by_ext": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])),
        "per_task": dict(sorted(per_task.items())),
        # 需求 21：按任务的重复率/均AI分，UI 上渲染成任务分布表
        "per_task_quality": dict(sorted(per_task_quality.items())),
        "duplicate_files": dup_files,
        "duplicate_rate_pct": round(dup_rate, 2),
        "avg_score": (sum(r["score"] or 0 for r in rows) / total) if total else 0.0,
    }


def short_reason(err: str) -> str:
    """把原始错误压成一句**中文短句**（需求 24）。

    用户原话：「直接写缺少 cookie 或者站点无法连接这类简短的、清晰的描述」。
    之前任务行里塞的是原始异常，比如
        "fetch failed: https://x.com/: All connection attempts failed"
    —— 又长又是英文，看完不知道该干什么。规则按「用户下一步要做什么」排：
    缺 cookie → 去浏览器插件取；连不上 → 站点已挂/需要代理；被墙 → 换代理。
    认不出来就原样返回（宁可长但准确，也不要给一个猜错的结论）。
    """
    s = (err or "").strip()
    if not s:
        return ""
    low = s.lower()
    # 顺序有讲究：先判特征最明确的，避免被后面更宽的规则截胡
    if ("cookie" in low or "cookies" in low
            or "需要登录" in s or "未登录" in s or "login required" in low):
        return "缺少 cookie：请用浏览器插件取 Cookie 后重试"
    if ("all connection attempts failed" in low
            or "name or service not known" in low
            or "getaddrinfo failed" in low
            or "temporary failure in name resolution" in low
            or "nodename nor servname" in low
            or "站点无法连接" in s):
        return "站点无法连接：检查网址是否正确，或该站点已失效"
    if "timed out" in low or "timeout" in low:
        return "连接超时：网络慢或站点无响应，可加代理后重试"
    if "connection refused" in low or "connection reset" in low:
        return "连接被拒绝：站点可能已关闭或需要代理"
    if "403" in s or "forbidden" in low:
        return "被站点拒绝（403）：需要 Cookie 或代理"
    if "401" in s or "unauthorized" in low:
        return "未授权（401）：需要登录 Cookie"
    if "404" in s or "not found" in low:
        return "页面不存在（404）：网址可能已失效"
    if "429" in s or "too many requests" in low:
        return "请求太频繁（429）：已被限流，建议调大延迟"
    if "robots" in low:
        return "被 robots.txt 拒绝：可在合规设置里放宽"
    if "no extractor" in low or "不支持" in s:
        return "gallery-dl 不支持该站点：需为它写适配器规则"
    if "该网址 gallery-dl 不支持" in s:
        return "gallery-dl 不支持该站点：需为它写适配器规则"
    if "local ai" in low or "本地ai" in s:
        return "本地 AI 不可用：检查模型目录或改用在线 API"
    if "no space" in low or "disk" in low or "磁盘" in s:
        return "磁盘空间不足：清理后重试"
    if "未产出图" in s or "落空" in s or "没找到" in s:
        return "没抓到图：可能需要 Cookie、换适配器或该页已无图"
    if "selector" in low:
        return "页面结构变了：适配器选择器需要更新"
    return s[:60]


# R152-D6：词界匹配独立的 "ai"（local ai / ai 后端），不吃 domain/email 子串。
_AI_WORD_RE = re.compile(r"\bai\b", re.I)


def _looks_ai_error(s: str) -> bool:
    """R152-D6：判断一条错误串是不是"本地 AI"相关。

    原来用 `"ai" in s` 子串匹配——**domain / email / detail / said** 这些
    常见词里都含着 "ai"，于是一条 DNS 解析失败会被统计成"本地AI 故障"，
    用户看着"AI 挂了"去查模型，真正坏的是网络。改成：
      · `\\bai\\b` 词界匹配（"local ai unavailable" 命中，"domain" 不命中）；
      · 中文/组件名标记（模型 / 分类器 / 降级 / clip / onnx / tokenizer）。
    """
    if _AI_WORD_RE.search(s):
        return True
    return any(m in s for m in ("模型", "分类器", "降级", "clip", "onnx",
                                "tokenizer", "classifier"))


def failure_reasons(errors: list[str]) -> dict[str, int]:
    """Bucket raw error strings into user-readable categories."""
    cats = {"robots/访问控制": 0, "网络/超时": 0, "选择器失效": 0,
            "本地AI": 0, "磁盘": 0, "其他": 0}
    for e in errors:
        s = (e or "").lower()
        if "robots" in s or "403" in s or "401" in s or "429" in s:
            cats["robots/访问控制"] += 1
        elif ("timeout" in s or "timed out" in s or "connection" in s
              or "fetch failed" in s):
            cats["网络/超时"] += 1
        elif "selector" in s:
            cats["选择器失效"] += 1
        elif _looks_ai_error(s):
            cats["本地AI"] += 1
        elif "disk" in s or "low_disk" in s:
            cats["磁盘"] += 1
        else:
            cats["其他"] += 1
    return {k: v for k, v in cats.items() if v}


def human_bytes(n: float) -> str:
    return _human(n)
