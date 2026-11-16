"""F24 下载限速计划：按时段给采集带宽设上限（纯逻辑，不碰 Qt、不做 IO）。

为什么单独成模块：这套口径要被**四处**共用——
桌面设置对话框（gui/rateplan_dialog.py）、引擎（engine.py 建下载器）、
下载器（downloader/downloader.py 每收完一个文件就扣令牌）、
Web 设置页（webui.py 只读展示 + 总开关）。散在四处各写一份，
迟早出现「界面写着 500 KB/s、实际一点没限」这类对不上的缺陷
（本仓「两个口径不一致」的坑已反复踩过），所以收敛在这里。

口径（写死，别各自发挥）：
* ``kbps`` = KB/s（1 KB = 1024 字节）；**0 = 不限速**，不是「0 字节/秒」。
* 时段用 ``"HH:MM"`` 字符串存 —— config.json 是给人看的，
  存分钟数用户手工改配置时看不懂。
* 支持跨午夜（start > end，如 ``"22:00"-"06:00"``）；``"24:00"`` 表示当天结束。
* 多个时段重叠时**取第一条命中**（按归一化后的顺序），不叠加、不取严 ——
  叠加会让「全天 200、白天 500」这种配法变成谁都猜不到的结果。
* **没有命中任何时段 = 不限速**（与 #22 的 allowed_hours「填错就当不限」
  同口径：限速是优化，配错了绝不能把任务卡死）。
* 限速**不改并发数**：令牌桶是**本轮共享**的，并发再多总带宽也压在上限内；
  降并发只会让每张图排更久的队（用户感知是「更慢但没省带宽」）。
* 与 robots 的 crawl-delay 是两件事：那个管**请求间隔**，本模块管**字节速度**；
  两者同时生效，实际速度取更严的那个（慢的说了算）。
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from typing import Any, Callable, Optional

# 一条时段：{"start": "00:00", "end": "08:00", "kbps": 0}
DEFAULT_PLAN: list = [
    {"start": "00:00", "end": "08:00", "kbps": 0},       # 夜里全速
    {"start": "08:00", "end": "24:00", "kbps": 500},     # 白天收着点
]

#: 下限/上限：低于 10 KB/s 基本等于下不动图（还会触发站点超时），
#: 高于 100000 KB/s（≈100 MB/s）家用宽带也摸不到，属于输错。
MIN_KBPS = 10
MAX_KBPS = 100000
#: 时段条数上限：再多界面也放不下，且遍历成本随条数走。
PLAN_MAX_SLOTS = 8
#: 令牌桶突发额度（秒）：允许「攒够一口气下一张」，避免小图被拆成多次等待。
BURST_SEC = 2.0
#: 桶的最小容量（字节）：极低限速下也能一口气下完一张小图，不至于每秒一醒。
MIN_BURST_BYTES = 64 * 1024

_HHMM = re.compile(r"^(\d{1,2})\s*:\s*(\d{1,2})$")


def default_plan() -> list:
    """返回默认计划的**深拷贝**（调用方改它不会污染模块常量）。"""
    return [dict(s) for s in DEFAULT_PLAN]


def _fmt(minute: int) -> str:
    m = int(minute)
    if m >= 1440:
        return "24:00"
    return f"{m // 60:02d}:{m % 60:02d}"


def parse_hhmm(v: Any) -> Optional[int]:
    """``"08:30"`` → 510（当天第几分钟）；解析不了返回 None。

    允许直接传 int（程序内部/测试用），``"24:00"`` → 1440（当天结束）。
    """
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if 0 <= v <= 1440 else None
    m = _HHMM.match(str(v or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24 and mi == 0:
        return 1440
    if h > 24 or mi > 59:
        return None
    return h * 60 + mi


def normalize_slot(raw: Any) -> Optional[dict]:
    """把一条原始时段洗成标准形态；不合法（时间写错 / 首尾相同）返回 None。

    ⚠ 首尾相同 = 空时段，直接丢掉（与 allowed_hours「首尾相同=全天」不同：
    这里是**计划表**，一条零长度的规则没有任何意义，留着只会让人以为生效了）。
    """
    if not isinstance(raw, dict):
        return None
    s = parse_hhmm(raw.get("start"))
    e = parse_hhmm(raw.get("end"))
    if s is None or e is None or s == e:
        return None
    try:
        kb = int(raw.get("kbps", 0) or 0)
    except (TypeError, ValueError):
        return None
    if kb < 0:
        kb = 0
    if 0 < kb < MIN_KBPS:
        kb = MIN_KBPS
    if kb > MAX_KBPS:
        kb = MAX_KBPS
    return {"start": _fmt(s), "end": _fmt(e), "kbps": kb}


def normalize_plan(plan: Any) -> list:
    """洗整个计划：丢废条、按开始时间排序、截到 PLAN_MAX_SLOTS 条。

    排序是**契约**：重叠时「取第一条命中」才有确定含义，否则
    计划表里换一下行顺序，实际限速就变了（用户视角＝玄学）。
    """
    out = []
    for raw in (plan or []):
        sl = normalize_slot(raw)
        if sl is not None:
            out.append(sl)
    out.sort(key=lambda d: parse_hhmm(d["start"]) or 0)
    return out[:PLAN_MAX_SLOTS]


def minutes_now(now: Any = None) -> int:
    """当前是当天第几分钟。now 可传 datetime，也可直接传分钟数（测试）。"""
    if now is None:
        now = datetime.now()
    if isinstance(now, datetime):
        return now.hour * 60 + now.minute
    try:
        return int(now) % 1440
    except (TypeError, ValueError):
        return datetime.now().hour * 60 + datetime.now().minute


def slot_at(plan: Any, minute: int) -> Optional[dict]:
    """第 minute 分钟落在哪条时段里；没命中返回 None（=不限速）。"""
    try:
        m = int(minute) % 1440
    except (TypeError, ValueError):
        return None
    for sl in normalize_plan(plan):
        s = parse_hhmm(sl["start"])
        e = parse_hhmm(sl["end"])
        if s is None or e is None:
            continue
        if s <= e:
            if s <= m < e:
                return sl
        elif m >= s or m < e:                 # 跨午夜
            return sl
    return None


def resolve_kbps(plan: Any, now: Any = None) -> int:
    """当前时刻的带宽上限（KB/s，0 = 不限速）。"""
    sl = slot_at(plan, minutes_now(now))
    return int(sl["kbps"]) if sl else 0


def effective_kbps(cfg, now: Any = None, plan_kbps: Optional[int] = None) -> int:
    """当前**实际**生效的带宽上限（KB/s，0=不限速）。

    两个来源合成一个口径：
      ① ``cfg.speed_limit_kbps``（全局上限，编号 18）；
      ② ``cfg.rate_plan`` 当前时段（分时段计划，编号 19）。
    两者都 >0 时**取更严的那个**——用户同时配了两处，意思显然是"都不许超过"。
    任何一处为 0 = 那处不限制。

    引擎与 fetcher 都必须调这里，不许各自读字段（口径分叉是本仓反复踩的坑）。
    """
    values = []
    try:
        g = int(getattr(cfg, "speed_limit_kbps", 0) or 0)
    except (TypeError, ValueError):
        g = 0
    if g > 0:
        values.append(g)
    try:
        if bool(getattr(cfg, "rate_plan_enabled", False)):
            k = (resolve_kbps(getattr(cfg, "rate_plan", None), now)
                 if plan_kbps is None else int(plan_kbps))
            if k > 0:
                values.append(k)
    except Exception:
        pass
    return min(values) if values else 0


def describe_kbps(kbps: int) -> str:
    return "不限速" if int(kbps or 0) <= 0 else f"{int(kbps)} KB/s"


def plan_summary(plan: Any) -> str:
    """计划表的人类可读摘要（界面/日志用）：``00:00-08:00 不限速；08:00-24:00 500 KB/s``。"""
    slots = normalize_plan(plan)
    if not slots:
        return "（没有可用时段，等同不限速）"
    return "；".join(f"{s['start']}-{s['end']} {describe_kbps(s['kbps'])}"
                     for s in slots)


def current_limit_text(plan: Any, enabled: bool = True, now: Any = None) -> str:
    """「当前实际限速」一句话，给运行日志用（同一个口径）。

    ⚠ 界面状态行**不要**直接用这句：只说「不限速（时段 00:00-08:00）」时，
      用户看到"不限速"三个字就以为没生效（V0.0.41 的真实反馈：
      「都修改了几次了，还是不行？」——其实计划配得好好的，只是当时正落在
      夜里那个不限速时段）。界面上请用 :func:`plan_status` 那份带
      「现在几点 / 下次什么时候变」的结构化结果。
    """
    if not enabled:
        return "不限速（限速计划已关闭）"
    sl = slot_at(plan, minutes_now(now))
    if sl is None:
        return "不限速（当前时段没有配限速）"
    return f"{describe_kbps(sl['kbps'])}（时段 {sl['start']}-{sl['end']}）"


def plan_status(plan: Any, enabled: bool = True, now: Any = None) -> dict:
    """界面状态条用的快照：**现在**怎么限、**下一次**什么时候变。

    为什么要有这个：用户配好表以后，最想知道的是「现在生效了吗、什么时候
    开始限」，而 `current_limit_text` 只会说一句「不限速」，看起来像坏了。
    这里把当前时刻、当前时段、下一个边界一起算出来，界面就能写成：

        现在 01:22 不限速（00:00–08:00 这个时段没设上限）· 今天 08:00 起限 500 KB/s

    纯函数，不读配置、不碰时钟以外的东西（now 可传 datetime 或分钟数）。
    """
    minute = minutes_now(now)
    slots = normalize_plan(plan)
    out = {
        "enabled": bool(enabled),
        "has_plan": bool(slots),
        "now_minute": minute,
        "now_clock": _fmt(minute),
        "now_kbps": 0,
        "now_start": "",
        "now_end": "",
        "next_clock": "",
        "next_day": False,
        "next_kbps": 0,
        "now_text": "",
        "next_text": "",
    }
    if not enabled:
        out["now_text"] = "现在不限速：分时段限速没有打开"
        return out
    if not slots:
        out["now_text"] = "现在不限速：还没有配置任何时段"
        return out

    sl = slot_at(slots, minute)
    if sl is not None:
        out["now_kbps"] = int(sl["kbps"])
        out["now_start"], out["now_end"] = sl["start"], sl["end"]
        if out["now_kbps"] > 0:
            out["now_text"] = (f"现在限速 {out['now_kbps']} KB/s"
                               f"（处在 {sl['start']}–{sl['end']} 这个时段）")
        else:
            out["now_text"] = (f"现在不限速（处在 {sl['start']}–{sl['end']}"
                               f" 这个时段，它没设上限）")
    else:
        out["now_text"] = "现在不限速（这个时刻不在任何时段里）"

    # 下一次变化：所有时段边界里第一个晚于"现在"的
    bounds = []
    for s in slots:
        for key in ("start", "end"):
            b = parse_hhmm(s[key])
            if b is not None and b > minute and b < 1440:
                bounds.append(b)
    if bounds:
        b = min(bounds)
        nk = resolve_kbps(slots, b)
        out["next_clock"] = _fmt(b)
        out["next_kbps"] = nk
        out["next_day"] = False
    else:
        # 今天剩下的时间都在最后一个时段里：明天从最早那条开始
        first = min((parse_hhmm(s["start"]) or 0) for s in slots)
        nk = resolve_kbps(slots, first)
        out["next_clock"] = _fmt(first)
        out["next_kbps"] = nk
        out["next_day"] = True
    when = ("明天 " if out["next_day"] else "今天 ") + out["next_clock"]
    out["next_text"] = (f"{when} 起限 {out['next_kbps']} KB/s"
                        if out["next_kbps"] > 0
                        else f"{when} 起不限速")
    return out


class BandwidthLimiter:
    """共享令牌桶：把**本轮所有并发下载**的总带宽压在上限内。

    ``kbps`` 可以传整数，也可以传**无参 callable**（返回 KB/s）——
    后者用于「跑着跑着跨过时段边界」：引擎不用重启任务，
    下一次扣令牌时速率自动跟着新时段走。

    ⚠ 必须**一个任务一个桶、所有并发共用**：每个下载各建一个桶的话，
    并发 4 就是 4 倍带宽，限速形同虚设（这正是「分时段限速」最容易写错的地方）。
    """

    def __init__(self, kbps: Any = 0, burst_sec: float = BURST_SEC):
        self._kbps = kbps
        self._burst = float(burst_sec) if burst_sec and burst_sec > 0 else BURST_SEC
        self._tokens = None          # None = 还没开始（首次给满桶）
        self._last = 0.0
        self.waited = 0.0            # 累计等待秒数（统计/测试用）

    @property
    def kbps(self) -> int:
        v = self._kbps() if callable(self._kbps) else self._kbps
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def enabled(self) -> bool:
        return self.kbps > 0

    def _rate_bps(self) -> float:
        k = self.kbps
        return 0.0 if k <= 0 else float(k) * 1024.0

    async def take(self, nbytes: int) -> float:
        """按已收到的字节数扣令牌，不够就睡到够为止；返回**实际睡了几秒**。

        不限速 / 字节数为 0 时恒返回 0（默认配置下这是个空操作，
        老行为一点不变）。
        """
        try:
            n = int(nbytes or 0)
        except (TypeError, ValueError):
            return 0.0
        if n <= 0:
            return 0.0
        rate = self._rate_bps()
        if rate <= 0:
            self._tokens = None
            self._last = 0.0
            return 0.0
        cap = max(MIN_BURST_BYTES, rate * self._burst)
        now = time.monotonic()
        if self._tokens is None or self._last <= 0.0:
            # 首次给满桶：允许「一口气下一张」，不然第一张图也要白等一轮。
            self._tokens = cap
            self._last = now
        else:
            self._tokens = min(cap, self._tokens + (now - self._last) * rate)
            self._last = now
        if self._tokens >= n:
            self._tokens -= n
            return 0.0
        wait = (n - self._tokens) / rate
        self._tokens = 0.0
        self.waited += wait
        await asyncio.sleep(wait)
        self._last = time.monotonic()
        return wait
