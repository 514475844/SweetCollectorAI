"""R232 · 数据备份 / 偏好导入导出的**共享纯函数层**。

订单出处：``.workbuddy/PARITY_MATRIX.md`` 缺失项第 1 条 —— 桌面「数据」菜单
有「每日自动备份数据库 / 导出偏好 / 导入偏好」三件事，Web 端一个都没有
（``webui.py`` 里既没有 ``/api/backup`` 也没有 ``/api/preferences/*``）。
换机 / 重装时 Web 用户拿不回自己的数据和几个月攒下来的学习成果。

为什么单独开一层（与 R228 的 ``tags_util``、R230 的 ``learn_switch`` 同型）：
备份目录名、备份文件名、保留份数、导出 JSON 的结构、导入时「哪些算一条合法
偏好」，这些桌面与网页若各写一遍，迟早漂成「桌面导出的文件网页导入不了」。
这里只放**不碰 IO 的纯函数**（落盘 / 写库 / 在线快照由调用方自己做），
所以两端能用、单测也好喂数据：不需要真库、真磁盘。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

#: 备份目录（放在用户数据目录下，与桌面端 ``_daily_backup`` 同一个位置）。
BACKUP_DIRNAME = "backups"
#: 备份文件名：``mrm_db_backup_20261023.db``（一天一份）。
BACKUP_PREFIX = "mrm_db_backup_"
BACKUP_SUFFIX = ".db"
#: 保留份数（cfg 老配置无键时的取值，与桌面端 ``keep = 7`` 同源）。
KEEP_FIELD = "backup_keep"
KEEP_DEFAULT = 7
#: 「每日自动备份」开关（cfg 字段名；桌面读同一键，老配置无键 = 开）。
DAILY_FIELD = "daily_backup"
DAILY_DEFAULT = True
#: 导出文件的版本号（导入侧以后按它分叉，现在固定 1）。
PREFS_VERSION = 1


def backup_dir(base: Any) -> Path:
    """备份目录 = 用户数据目录下的 ``backups``（与桌面端同一个位置）。"""
    return Path(base) / BACKUP_DIRNAME


def backup_name(now: Optional[datetime] = None) -> str:
    """备份文件名（**按天**命名，与桌面端逐字一致）。

    同一天再备份一次 = 覆盖当天那份：桌面端是「当天已存在就跳过」，网页端
    「立即备份」是**用户手点的**，他要的就是此刻的快照，跳过会让按钮看起来
    没反应（永远只留当天第一份，等于当天之后改的东西一份都留不下）。
    """
    return "%s%s%s" % (BACKUP_PREFIX, (now or datetime.now()).strftime("%Y%m%d"),
                       BACKUP_SUFFIX)


def backup_path(base: Any, now: Optional[datetime] = None) -> Path:
    """某天备份文件的完整路径。"""
    return backup_dir(base) / backup_name(now)


def is_backup_name(name: str) -> bool:
    """是不是本工具自己产出的备份文件名（别的手工 .db 别被列入清单）。"""
    n = str(name or "")
    return (n.startswith(BACKUP_PREFIX) and n.endswith(BACKUP_SUFFIX)
            and len(n) == len(BACKUP_PREFIX) + 8 + len(BACKUP_SUFFIX))


def prune(names: Iterable[str], keep: Any) -> list:
    """该删哪些备份：按文件名升序（= 按日期）保留最近 ``keep`` 份。

    ⚠ 按**名字**排而不是按 mtime 排：文件名里就是日期，mtime 会被拷贝、
    解压、恢复操作改掉，用它排会把最新的那份删掉。

    ``keep <= 0`` = 不清理（桌面端同口径：``if keep > 0`` 才动）。
    """
    try:
        k = int(keep or 0)
    except (TypeError, ValueError):
        k = 0
    if k <= 0:
        return []
    seq = sorted(str(n) for n in names)
    if len(seq) <= k:
        return []
    return seq[:len(seq) - k]


def daily_enabled(cfg: Any) -> bool:
    """读「每日自动备份」开关（老配置无键 = 开，与桌面端默认值同源）。"""
    return bool(getattr(cfg, DAILY_FIELD, DAILY_DEFAULT))


def apply_daily(cfg: Any, value: bool,
                save: Optional[Callable[[], Any]] = None) -> bool:
    """写开关：改 cfg →（给了 ``save`` 才）落盘。

    ``save`` 走**参数注入**（与 learn_switch 同款）：网页端落盘走
    ``config.save_config``，桌面端走自己的保存链；在这里 import 死一个，
    另一端就会被绕过去（只改内存 = 重启丢设置）。
    """
    v = bool(value)
    setattr(cfg, DAILY_FIELD, v)
    if save is not None:
        save()
    return v


def human_size(size: Any) -> str:
    """字节数 → 中文界面上能直接显示的字符串（桌面/Web 同一口径）。"""
    try:
        n = float(size or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n < 1024:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
    return "%.1f TB" % n


def prefs_payload(rows: Iterable[Any], now: Optional[datetime] = None) -> dict:
    """把 preferences 表（或任意映射序列）拼成导出用的 JSON 结构。

    与桌面端 ``export_preferences`` **同一个结构**（version / exported_at /
    preferences），所以 Web 导出的文件能直接拿去桌面导入，反之亦然。

    顺手把关：标签为空的行不导出（导出一条空标签，导入侧必然当非法项丢掉，
    不如这里就别写出来，免得用户看到「导出了 200 条、导入只有 198 条」）。
    """
    items = []
    for r in rows or ():
        try:
            d = dict(r)
        except (TypeError, ValueError):
            d = r if isinstance(r, dict) else {}
        label = str(d.get("label") or "").strip()
        if not label:
            continue
        try:
            weight = float(d.get("weight", 0.0) or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        try:
            seen = int(d.get("seen", 0) or 0)
        except (TypeError, ValueError):
            seen = 0
        items.append({"task_id": int(d.get("task_id", 0) or 0),
                      "label": label, "weight": weight, "seen": seen})
    return {"version": PREFS_VERSION,
            "exported_at": (now or datetime.now()).isoformat(timespec="seconds"),
            "count": len(items),
            "preferences": items}


def normalize_prefs(data: Any) -> tuple:
    """导入前的把关：``(合法条目列表, 跳过的条数)``。

    认两种形态：``{"preferences": [...]}``（桌面 / Web 导出文件的本体）与
    裸数组（手搓或别处导出）。逐条过滤而不是整体拒绝 —— 100 条里有 2 条
    坏数据就整份不导，用户只会以为「导入功能坏了」。

    标签必须是非空字符串；weight / seen 坏值**退化成 0 而不是跳过**：
    权重的缺省值是 0，一条刚建立的偏好本来就是 0，因为缺字段丢掉整条
    才是真丢数据。
    """
    if isinstance(data, dict):
        raw = data.get("preferences")
    elif isinstance(data, list):
        raw = data
    else:
        raw = None
    if not isinstance(raw, list):
        return [], 0
    out, skipped = [], 0
    for it in raw:
        if not isinstance(it, dict):
            skipped += 1
            continue
        label = str(it.get("label") or "").strip()
        if not label:
            skipped += 1
            continue
        try:
            weight = float(it.get("weight", 0.0))
        except (TypeError, ValueError):
            weight = 0.0
        try:
            seen = int(it.get("seen", 0))
        except (TypeError, ValueError):
            seen = 0
        out.append({"label": label, "weight": weight, "seen": seen})
    return out, skipped


#: 导入 / 覆盖用的 UPSERT 语句（桌面端逐字同款：已存在的覆盖权重与表态次数）。
PREFS_UPSERT_SQL = (
    "INSERT INTO preferences(task_id, label, weight, seen) "
    "VALUES (?,?,?,?) "
    "ON CONFLICT(task_id, label) DO UPDATE SET "
    "weight=excluded.weight, seen=excluded.seen"
)
