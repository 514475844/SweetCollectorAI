"""R242 · 诊断包（Web 端「维护 → 导出诊断包」的对称实现）。

订单出处：``docs/r239_proxy_page_20261028.md`` 遗留项 —— 桌面端
「维护 → 导出诊断包…」打包的是「脱敏配置 + 库统计 + 环境 + 日志 + 库副本」，
Web 端没有；用户在网页上遇到问题只能截屏，排障只能靠猜。

与桌面端的两点**刻意不同**：
* 桌面端把整个 zip 再整体加密成 ``.mrmx``（需求 19），只能由本软件打开。
  网页端下载下来就是个普通 ``.zip`` —— 因为 Web 用户拿到文件是为了**发给
  别人**看（客服/朋友），加密了对方反而打不开。代价是**必须脱敏**，
  所以这里对配置做逐键打码（口令类全掩、URL 里的 user:pass 段掩掉）。
* 桌面端日志取自界面文本框（``log_view``），Web 没有这个控件 ⇒ 这里直接读
  ``logs/`` 目录下最新的几个文件，各取尾部若干 KB（整份日志动辄几十 MB，
  全塞进去对方也打不开）。

⚠ 包里**不含任何图片字节**；库副本只有元数据（路径/标签/统计），
含下载来源网址 —— 说明.txt 里明确写出来，让用户知道发出去的是什么。
"""
from __future__ import annotations

import json
import platform
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

#: 包内说明文件名（放在包根，第一眼能看到）。
README_NAME = "说明.txt"
#: 每个日志文件最多带多少字节的尾巴。
LOG_TAIL = 512 * 1024
#: 最多带几个日志文件。
LOG_FILES = 6
#: 文件名里出现这些词的键，值一律整段打码。
SECRET_KEY_HINT = ("password", "passwd", "secret", "token", "api_key",
                   "apikey", "cookie", "credential", "master")
#: URL 里的 ``user:pass@`` 段 → ``***@``（代理地址常见形态）。
_URL_CRED = re.compile(r"(://)([^/@\s]+):([^/@\s]+)@")


def mask_cred(text: str) -> str:
    """把 URL 里的用户名口令掩掉（保留主机端口，排障要看这个）。"""
    if not isinstance(text, str) or not text:
        return text
    return _URL_CRED.sub(r"\1***@", text)


def _looks_secret(key: str) -> bool:
    k = str(key or "").lower()
    return any(h in k for h in SECRET_KEY_HINT)


def mask_config(data: Any) -> Any:
    """配置字典脱敏：口令类整段打码，URL 类只掩 user:pass，其余原样。

    为什么口令类**整段**打码而不是留前几位：主密码/接口密钥短，露 3 位
    加已知格式就可能被穷举；而排障根本不需要它 —— 需要的是「配没配」。
    """
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if _looks_secret(k) and v not in (None, "", 0, False):
                out[k] = "***"
            else:
                out[k] = mask_config(v)
        return out
    if isinstance(data, list):
        return [mask_config(x) for x in data]
    if isinstance(data, str):
        return mask_cred(data)
    return data


def _cfg_dict(cfg: Any) -> dict:
    """取配置字典（优先 ``cfg.to_dict()``，没有就按 dataclass 字段拼）。"""
    try:
        d = cfg.to_dict()
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    try:
        import dataclasses
        return {f.name: getattr(cfg, f.name) for f in dataclasses.fields(cfg)}
    except Exception:
        return {k: v for k, v in vars(cfg).items()
                if not str(k).startswith("_")}


def db_stats(db: Any) -> dict:
    """库概况（只读聚合，不读图片字节）。与桌面端同一套口径。"""
    out: dict = {}
    q = db.query
    pairs = (
        ("tasks_total", "SELECT COUNT(*) c FROM tasks WHERE is_template=0"),
        ("images_total", "SELECT COUNT(*) c FROM images"),
        ("images_in_library",
         "SELECT COUNT(*) c FROM images WHERE in_quarantine=0 AND blacklisted=0"),
        ("images_quarantined",
         "SELECT COUNT(*) c FROM images "
         "WHERE in_quarantine=1 AND needs_review=0"),
        ("images_favorite", "SELECT COUNT(*) c FROM images WHERE favorite=1"),
        ("images_needs_review", "SELECT COUNT(*) c FROM images WHERE needs_review=1"),
        ("preferences_total", "SELECT COUNT(*) c FROM preferences"),
        ("rules_total", "SELECT COUNT(*) c FROM rules"),
        ("reviews_total", "SELECT COUNT(*) c FROM reviews"),
        ("failed_urls_total", "SELECT COUNT(*) c FROM failed_urls"),
    )
    for k, sql in pairs:
        try:
            out[k] = int(q(sql)[0]["c"])
        except Exception:
            out[k] = None
    try:
        out["tasks_by_status"] = {
            str(r["status"]): int(r["c"]) for r in
            q("SELECT status, COUNT(*) c FROM tasks WHERE is_template=0 "
              "GROUP BY status")}
    except Exception:
        out["tasks_by_status"] = {}
    try:
        out["per_task"] = [
            {"task_id": r["task_id"], "name": r["name"], "images": int(r["c"])}
            for r in q("SELECT t.id task_id, t.name name, COUNT(i.id) c "
                       "FROM tasks t LEFT JOIN images i ON i.task_id=t.id "
                       "WHERE t.is_template=0 GROUP BY t.id "
                       "ORDER BY c DESC LIMIT 20")]
    except Exception:
        out["per_task"] = []
    try:
        rows = q("PRAGMA quick_check")
        out["quick_check"] = str(rows[0][0]) if rows else "ok"
    except Exception as e:
        out["quick_check"] = "自检失败：%s" % e
    try:
        out["journal_mode"] = str(q("PRAGMA journal_mode")[0][0])
    except Exception:
        out["journal_mode"] = ""
    return out


def env_info(cfg: Any = None, extra: Optional[dict] = None) -> dict:
    """运行环境（排障第一条：什么版本、什么系统、打包没打包）。"""
    from . import __version__
    out = {
        "app": "Sweet采集AI版",
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "encoding_fs": sys.getfilesystemencoding(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        import onnxruntime as ort
        out["onnx_providers"] = list(ort.get_available_providers())
    except Exception:
        out["onnx_providers"] = "unavailable"
    try:
        from .diskspace import free_space
        out["disk_free"] = free_space(Path("."))
    except Exception:
        pass
    if extra:
        out.update(extra)
    return out


def tail_text(path: Path, limit: int = LOG_TAIL) -> str:
    """读文件尾部（超大日志不全量读进内存）＋ 明确标注「省略前 N 行」。"""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with open(path, "rb") as fh:
            if size > limit:
                fh.seek(size - limit)
                fh.readline()          # 丢掉被截断的半行
                raw = fh.read()
                head = "[仅保留最后 %d KB，共 %d KB]\n" % (limit // 1024,
                                                          size // 1024)
            else:
                raw = fh.read()
                head = ""
        return head + raw.decode("utf-8", "replace")
    except OSError:
        return ""


def collect_logs(log_dir: Any, *, files: int = LOG_FILES,
                 tail: int = LOG_TAIL) -> list:
    """按修改时间取最新几个日志文件，返回 ``[(名字, 内容), ...]``（新的在前）。"""
    d = Path(log_dir)
    if not d.is_dir():
        return []
    try:
        cand = [f for f in d.iterdir() if f.is_file() and f.suffix.lower() in
                (".log", ".txt", ".out", ".err")]
        cand.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return []
    out = []
    for f in cand[:max(1, int(files))]:
        txt = tail_text(f, tail)
        if txt:
            out.append((f.name, txt))
    return out


#: 日志里算「可疑」的文本关键词（大小写不敏感）。
_TEXT_KEYS = ("error", "exception", "traceback", "failed", "错误", "失败",
              "异常", "超时", "拒绝", "无法", "崩溃",
              "bad gateway", "too many requests", "unauthorized", "forbidden",
              "timed out", "connection reset")
#: HTTP 状态码**必须带上下文**才认（``HTTP 502`` / ``status: 403`` / ``状态码 429``）。
#:
#: ⚠ 别退回到「裸 4xx/5xx 数字」那条路 —— 实测真机日志里满屏都是它：
#: 时间戳毫秒 ``,474``、``?page=400``、``pool_id=484``、``/post/show/409407``、
#: ``3_429.jpg``。以前靠「前后不是字母数字」也挡不住（``=400`` 有 ``=`` 打头），
#: 结果是摘要里塞满 INFO 发现页日志，用户按它排查等于没看。
_STATUS = re.compile(
    r"(?:HTTP[ /]?|status[ =：:]*|状态码?[ =：:]*)(4\d{2}|5\d{2})")


def error_digest(texts: list, limit: int = 60) -> str:
    """从日志里挑出可疑行（ERROR/异常/打不开），拼成一段「先看这些」。

    只挑不解析：排障要的是**线索位置**，不是统计报表。同一行重复出现
    只保留一次（日志里同一条错误刷几百遍是常态）。
    """
    seen, out = set(), []
    for name, txt in texts:
        for line in txt.splitlines():
            low = line.lower()
            hit = any(k in low for k in _TEXT_KEYS) or bool(_STATUS.search(line))
            if not hit:
                continue
            key = low.strip()[:160]
            if key in seen:
                continue
            seen.add(key)
            out.append("[%s] %s" % (name, line.strip()[:300]))
            if len(out) >= limit:
                return "\n".join(out)
    return "\n".join(out)


def build_bundle(dest: Any, *, cfg: Any, db: Any, extra_env: Optional[dict] = None,
                 include_db: bool = True,
                 progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """生成诊断包 zip（**同步**执行，通常 <2 秒；调用方负责下载/落盘）。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = env_info(cfg, extra_env)
    stats = db_stats(db)
    cfg_masked = mask_config(_cfg_dict(cfg))
    logs = collect_logs(getattr(cfg, "log_dir", None) or
                        (Path(str(getattr(cfg, "db_path", "."))).parent / "logs"))
    digest = error_digest(logs) or "（日志里没有匹配到明显错误行）"

    tmpdir = Path(tempfile.mkdtemp(prefix="mrm_diag_"))
    snap = tmpdir / "mrm_db.sqlite"
    has_db = False
    if include_db:
        try:
            db.backup(snap)
            has_db = True
        except Exception:
            has_db = False
    if progress:
        progress(60, "正在打包…")
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(README_NAME, _readme(has_db))
            zf.writestr("config_masked.json",
                        json.dumps(cfg_masked, indent=2, ensure_ascii=False))
            zf.writestr("db_stats.json",
                        json.dumps(stats, indent=2, ensure_ascii=False))
            zf.writestr("environment.json",
                        json.dumps(env, indent=2, ensure_ascii=False))
            zf.writestr("error_digest.txt", digest)
            if logs:
                for name, txt in logs:
                    safe = re.sub(r"[^\w.\-]+", "_", name)[:60] or "log.txt"
                    zf.writestr("logs/" + safe, txt)
            else:
                zf.writestr("logs/(没有找到日志文件).txt", "")
            if has_db:
                zf.write(snap, "mrm_db.sqlite")
    finally:
        try:
            if snap.exists():
                snap.unlink()
        except OSError:
            pass
        try:
            tmpdir.rmdir()
        except OSError:
            pass
    if progress:
        progress(100, "完成")
    return {"bytes": dest.stat().st_size, "has_db": has_db,
            "log_files": len(logs), "name": dest.name}


def _readme(has_db: bool) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        "Sweet采集AI版 · 诊断包\n"
        "生成时间：%s\n\n"
        "包里有什么：\n"
        "  · 说明.txt            本文件\n"
        "  · environment.json    版本 / 系统 / Python / 打包方式 / 推理后端\n"
        "  · config_masked.json  配置（口令类整段打码，代理地址只保留主机端口）\n"
        "  · db_stats.json       库统计（任务数、图片数、各状态计数、自检结果）\n"
        "  · error_digest.txt    日志里挑出来的可疑行（先看这个）\n"
        "  · logs/               最近几个日志文件的尾部\n"
        "  · mrm_db.sqlite       %s\n\n"
        "⚠ 包里**没有**任何图片文件。\n"
        "⚠ 数据库副本含元数据：图片路径、来源网址、标签、任务名称。\n"
        "   发给别人前请确认这些内容可以外传；只发日志请只取 logs/ 那部分。\n"
        % (now, "数据库副本（只有元数据，不含图片字节）" if has_db
           else "（本次未包含，可导出时勾选）")
    )
