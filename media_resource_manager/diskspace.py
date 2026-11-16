"""R154-F18 · 磁盘空间看板的**纯逻辑层**（不碰 Qt；只做最小的文件 IO）。

订单原文：各目录（library/quarantine/review/thumbs/models）占用条形图
+ 7 日增长趋势（每日一快照落库）；超阈值中文告警（阈值走设置，
联动现有磁盘预警若有）。

为什么单独拆一层：与 ``reporting`` / ``rateplan`` 同一个理由 —— 口径和
界面分开，才能**不起 QApplication 就单测**，也才能让桌面端与网页端拿到
**同一个数字**（两端各扫一遍目录、各写一份目录表，用户就会看到
「手机上没有 models 这一项」「桌面说 3.2 GB、手机说 3.4 GB」）。

⚠ 三条口径纪律（改本文件前必读）
1. **目录清单只有一个出处**：``DIR_SPECS`` 同时供桌面端与网页端使用。
   加目录只改这一处；在某一端私加，另一端永远少一项。
2. **快照按天一行**：``record_snapshot`` 用日期做主键、先 UPDATE 后 INSERT
   （便携 upsert，不依赖 SQLite 3.24+ 的 ON CONFLICT，与 failed_urls /
   task_pages 同款）—— 同一天重复记录只留最后一次。一天一行才叫
   「增长趋势」；一天八行那是日志，看板上会出现八根一样高的柱子。
3. **告警阈值复用 ``cfg.min_free_mb``**（#54 既有的「磁盘剩余空间预警」，
   0 = 不检查）：另起一个阈值字段 = 用户在设置里改了却看不懂为什么
   两边不联动。fraction（条形图比例）是**相对最大目录**，不是相对磁盘
   总量 —— 绝对字节条形图在只剩 2% 空闲的盘上会全是满的，没有信息量。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
from pathlib import Path

from .config import user_data_dir

#: 看板要统计的目录。``(key, 显示名, 解析规格)``：
#   ``cfg:xxx`` 取 AppConfig 上的目录字段；``root:xxx`` 取数据根目录下的子目录。
#   ⚠ 顺序即显示顺序（占用大的排前面更直观，但目录清单刻意与订单原文一致）。
DIR_SPECS = (
    ("library", "图库", "cfg:library_dir"),
    ("review", "待审核", "cfg:review_dir"),
    ("isolate", "隔离区", "cfg:isolate_dir"),
    ("thumbs", "缩略图缓存", "root:thumbs"),
    ("models", "AI 模型", "root:models"),
)

#: 增长趋势默认天数（订单：7 日）。上限 30 —— 再长的趋势在这个看板上
#: 只是把 7 根柱子压成 30 根，没有额外信息。
DEFAULT_DAYS = 7
MAX_DAYS = 30

#: 字节进制：1024（硬盘盒上的 1000 进制会让数字偏小 7%，和资源管理器对不上）。
_UNITS = (("TB", 1024 ** 4), ("GB", 1024 ** 3),
          ("MB", 1024 ** 2), ("KB", 1024))


# ---------------------------------------------------------------- 基础工具

def _i(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _today_str(today=None) -> str:
    if today is None:
        return _dt.date.today().isoformat()
    if isinstance(today, _dt.date):
        return today.isoformat()
    return str(today)


def _base_day(today=None) -> _dt.date:
    if isinstance(today, _dt.date):
        return today
    if today:
        try:
            return _dt.date.fromisoformat(str(today))
        except ValueError:
            pass
    return _dt.date.today()


def format_size(n) -> str:
    """字节数 → 中文可读的一行（``0`` / ``None`` / 异常都翻成 ``"0 B"``）。"""
    try:
        b = float(n or 0)
    except (TypeError, ValueError):
        b = 0.0
    if b != b or b in (float("inf"), float("-inf")):     # NaN / inf
        b = 0.0
    if b < 0:
        b = 0.0
    for unit, factor in _UNITS:
        if b >= factor:
            return f"{b / factor:.1f} {unit}"
    return f"{int(b)} B"


def dir_size(path) -> int:
    """目录占用字节数（递归，**不跟随符号链接** —— os.walk 默认行为）。

    任何异常都吞成 ``0``：看板不许因为一个目录读不了（被占用 / 已删 /
    路径不存在）就整体打不开。宁可显示 0 B，也不能让用户点开一个报错框。
    """
    try:
        p = Path(path) if path else None
        if p is None or not p.is_dir():
            return 0
    except Exception:
        return 0
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(str(p)):
            for fn in filenames:
                try:
                    total += (Path(dirpath) / fn).stat().st_size
                except OSError:
                    continue          # 单个文件读不到不拖垮整棵树
    except Exception:
        return 0
    return total


def free_space(path) -> int | None:
    """``path`` 所在盘的剩余字节；测不到给 ``None``（调用方按「未知」处理）。"""
    try:
        return int(shutil.disk_usage(str(path)).free)
    except Exception:
        return None


# ---------------------------------------------------------------- 目录清单

def _resolve(root: Path, cfg, spec: str) -> Path | None:
    if spec.startswith("cfg:"):
        v = getattr(cfg, spec[4:], None)
        return Path(v) if v else None
    return Path(root) / spec.split(":", 1)[1]


def scan_targets(cfg, root=None) -> list[dict]:
    """看板要统计的目录清单（**唯一出处**，桌面/Web 共用）。

    每项 ``{"key", "name", "path"}``；``path`` 可能是 ``None``（配置里没设），
    此时 ``dir_size(None)`` 返回 0，看板照常渲染（显示 0 B）。
    """
    root = Path(root) if root is not None else user_data_dir()
    return [{"key": key, "name": name, "path": _resolve(root, cfg, spec)}
            for key, name, spec in DIR_SPECS]


def collect_usage(cfg, root=None) -> dict:
    """扫一遍目录，返回看板要的全部数字。

    返回 ``{"dirs", "total_bytes", "total_text", "free_bytes",
    "threshold_mb", "alert"}``；``dirs`` 每项带 ``bytes`` / ``text`` /
    ``fraction``（相对**最大目录**的比例，条形图直接用）。

    ⚠ 这里扫的是真实文件系统：大库扫一遍要几秒，所以「每日一快照」
    由 ``snapshot_today_if_missing`` 保证一天只扫一次（对话框/网页打开时）。
    """
    root = Path(root) if root is not None else user_data_dir()
    dirs = []
    for t in scan_targets(cfg, root):
        b = dir_size(t["path"])
        dirs.append({**t, "bytes": b, "text": format_size(b)})
    mx = max((d["bytes"] for d in dirs), default=0)
    for d in dirs:
        d["fraction"] = (d["bytes"] / mx) if mx > 0 else 0.0
    total = sum(d["bytes"] for d in dirs)
    # 剩余空间挑一个**一定存在**的路径来测：目录都还没建时测数据根本身。
    probe = next((d["path"] for d in dirs if d["path"]), root)
    free = free_space(probe)
    try:
        threshold_mb = int(getattr(cfg, "min_free_mb", 0) or 0)
    except (TypeError, ValueError):
        threshold_mb = 0
    return {"dirs": dirs, "total_bytes": total, "total_text": format_size(total),
            "free_bytes": free, "threshold_mb": threshold_mb,
            "alert": alert_text(free, threshold_mb)}


def alert_text(free_bytes, min_free_mb) -> str:
    """剩余空间低于阈值时的中文告警；没超 / 关闭检查 / 测不到 ⇒ 空串。

    阈值语义与 #54 磁盘预警**完全一致**：``min_free_mb=0`` = 不检查。
    """
    try:
        mb = int(min_free_mb or 0)
    except (TypeError, ValueError):
        mb = 0
    if mb <= 0 or free_bytes is None:
        return ""
    if int(free_bytes) >= mb * 1024 * 1024:
        return ""
    return (f"保存盘剩余空间只剩 {format_size(free_bytes)}，已低于预警值 "
            f"{mb} MB：新任务会自动暂停，可清理隔离区 / 备份 / 缩略图缓存。")


def headline(usage: dict) -> str:
    """一行中文概览（桌面标签与网页顶部共用同一句）。"""
    dirs = usage.get("dirs") or []
    if not dirs:
        return "还没有可统计的目录。"
    top = max(dirs, key=lambda d: d.get("bytes") or 0)
    line = (f"各目录共 {usage.get('total_text', '0 B')}，"
            f"最大的是{top['name']}（{top.get('text', '0 B')}）")
    if usage.get("alert"):
        line += "；" + usage["alert"]
    return line


# ---------------------------------------------------------------- 快照（落库）

def _values_from_dirs(dirs) -> dict:
    out = {}
    for d in dirs or []:
        try:
            out[str(d["key"])] = int(d.get("bytes") or 0)
        except Exception:
            continue
    return out


def record_snapshot(db, dirs, free_bytes=None, day=None) -> None:
    """把某天的各目录占用写进快照表（**同日覆盖**，先 UPDATE 后 INSERT）。

    ``dirs``：``collect_usage()["dirs"]`` 或任何 ``[{"key","bytes"}, ...]``。
    ``free_bytes``：当天剩余空间（趋势的一部分：目录没涨但盘满了，
    那是别的东西在吃空间，只看目录总量会漏掉）。
    """
    d = _today_str(day)
    values = _values_from_dirs(dirs)
    detail = json.dumps(values, ensure_ascii=False)
    total = sum(values.values())
    cur = db.execute(
        "UPDATE disk_snapshots SET total_bytes=?, detail=?, free_bytes=?, "
        "recorded_at=datetime('now') WHERE day=?",
        (total, detail, free_bytes, d))
    if not getattr(cur, "rowcount", 0):
        db.execute(
            "INSERT INTO disk_snapshots(day, total_bytes, detail, free_bytes) "
            "VALUES (?,?,?,?)", (d, total, detail, free_bytes))


def has_snapshot(db, day=None) -> bool:
    """某天是否已有快照（``snapshot_today_if_missing`` 的判据）。"""
    d = _today_str(day)
    try:
        rows = db.query("SELECT 1 FROM disk_snapshots WHERE day=? LIMIT 1", (d,))
    except Exception:
        return False
    return bool(rows)


def load_history(db, days: int = DEFAULT_DAYS, today=None) -> list[dict]:
    """最近 ``days`` 天的快照，**逐日补行**（没有快照的日子给 ``None``）。

    返回 ``[{"date", "total_bytes", "delta", "text", "delta_text",
    "recorded"}]``，**旧的在前**（趋势从左往右读）。
    ``delta`` = 与「上一个**有记录**的日子」的差；没有可比基数 ⇒ ``None``
    （不跟「补行的空日」比 —— 那会得出「昨天掉了 3 GB」的假结论）。
    """
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = DEFAULT_DAYS
    n = max(1, min(MAX_DAYS, n))
    rows = {}
    try:
        for r in db.query(
                "SELECT day, total_bytes, free_bytes FROM disk_snapshots") or []:
            rows[str(r["day"])] = (int(r["total_bytes"] or 0), r["free_bytes"])
    except Exception:
        rows = {}                      # 老库没这张表 ⇒ 全「无记录」，不崩
    base = _base_day(today)
    out = []
    prev = None
    for i in range(n - 1, -1, -1):
        k = (base - _dt.timedelta(days=i)).isoformat()
        got = rows.get(k)
        if got is None:
            out.append({"date": k, "total_bytes": None, "delta": None,
                        "text": "无记录", "delta_text": "—", "recorded": False})
            continue                   # prev 不变：与上一个有记录的日比
        total = got[0]
        delta = (total - prev) if prev is not None else None
        if delta is None:
            delta_text = "—"
        elif delta > 0:
            delta_text = "+" + format_size(delta)
        elif delta < 0:
            delta_text = "-" + format_size(-delta)
        else:
            delta_text = "0 B"
        out.append({"date": k, "total_bytes": total, "delta": delta,
                    "text": format_size(total), "delta_text": delta_text,
                    "recorded": True})
        prev = total
    return out


def snapshot_today_if_missing(db, cfg, root=None, today=None) -> dict:
    """今天还没快照就扫一遍记上；返回本次（或已存在的）用量。

    对话框 / 网页打开时调它 ——「每日一快照」不能要求用户记得点按钮，
    但同一天重复扫也没意义（大库扫一遍要好几秒），所以有记录就跳过。
    记录失败（库被锁 / 只读）不许炸掉看板：吞掉，用量照常返回。
    """
    usage = collect_usage(cfg, root)
    if not has_snapshot(db, today):
        try:
            record_snapshot(db, usage["dirs"],
                            free_bytes=usage["free_bytes"], day=today)
        except Exception:
            pass
    return usage
