"""「定时运行」字段解析 —— 桌面与网页共用的唯一口径（V0.0.12）。

原先这段逻辑长在 gui/main_window.py 的实例方法里，网页表单要校验定时格式
时只能跳过校验（后端 _task_from_form 直接存原文，填错了要到调度器扫表时
才静默失效——用户以为设上了定时，其实永远不跑）。抽出来两端同用：

  • 桌面：main_window._parse_schedule 薄薄一层转调（行为逐字不变，
    R152-A4 的四条触发判据仍在 _check_schedule）。
  • 网页：_task_from_form 收到 schedule_at 时先校验，格式不对返回中文错误。

格式（与历史完全一致，不新增也不废除任何一种）：
  ""                  不定时
  "08:30"             每天 08:30（daily）
  "2026-10-01 08:30"  一次性（once）
"""
from __future__ import annotations

import re
from datetime import datetime


def parse_schedule(s: str):
    """解析「定时运行」字段，返回 (kind, when, err)。

    • ""                 -> (None, None, "empty")   不定时
    • "08:30"            -> ("daily", **今天**的 08:30, None)
    • "2026-10-01 08:30" -> ("once",  该时刻, None)
    • 其它               -> (None, None, "格式不对（应为 HH:MM 或 日期 HH:MM）")

    R152-A4：daily 只返回**今天**这个时刻，到没到点由调用方（_check_schedule）
    判——返回「最近一个 ≤now 的 08:30」会让新任务一启动就补跑今天已过的点，
    当天双跑。
    """
    s = (s or "").strip()
    if not s:
        return (None, None, "empty")
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return (None, None, "时间超出范围")
        now = datetime.now()
        when = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return ("daily", when, None)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2}) (\d{1,2}):(\d{2})$", s)
    if m:
        try:
            when = datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)),
                            int(m.group(5)), 0)
        except ValueError:
            return (None, None, "日期非法")
        return ("once", when, None)
    return (None, None, "格式不对（应为 HH:MM 或 日期 HH:MM）")
