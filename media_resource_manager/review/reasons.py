"""送审原因统一口径：把引擎内部的英文备注变成用户看得懂的中文。

V0.0.8。背景：图片为什么进待审核队列，以前只写在审计日志的英文备注里
（"ai uncertain for [...](best=风景 0.42, mode=any)"），审核页上一个字都看不到，
用户只能对着缩略图猜。现在引擎在**写库那一刻**就把原因翻成中文存进
``images.review_reason``，桌面审核页和网页审核页直接显示，两端不再各翻一套。

约定：
- 审计日志仍保留原始英文备注（排查问题要看真凭据），这一列只面向用户；
- 认不出的备注原样收进 80 字以内，绝不抛错——原因显示是辅助信息，
  不该把采集主流程拖下水；
- 固定场景（手动移入、抽检、回填等）用下面的常量，别在各调用点手打
  中文串——四个入口各写一份必然对不上。
"""
import re

# ---- 固定场景文案（唯一出处） ----
HIT_BLACKLIST = "命中黑名单规则（黑白照 / 关键词）"
RETROACTIVE = "后来加的黑名单规则，追溯命中了这张图"
MOVE_MANUAL = "你手动把它移进了待审核"
SAMPLE = "AI 自动隔离过的图，随机抽检拉进来复核（防误杀）"
SMART = "AI 最拿不准的图，优先拉进来复核"
BACKFILL_BLACKLIST = "回填本地图库时命中黑名单词"
MANUAL_FLAG = "从图库手动标为待审核"


def _num(s: str) -> str:
    """0.420 → 0.42，去掉多余的零，看着舒服。"""
    try:
        f = float(s)
        return f"{f:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return s


_EXCL_UNCERTAIN = re.compile(r"^ai-exclude uncertain=(.+?) score=([\d.]+)")
_EXCL_HIT = re.compile(r"^ai-exclude hit=(.+?) score=([\d.]+)")
_PREF_DOWN = re.compile(r"^ai preference-down .*?best=(.+?) ([\d.]+)"
                        r".*?pref_eff=([\d.]+)")
_UNCERTAIN = re.compile(r"^ai uncertain for .*?best=(.+?) ([\d.]+)")
_LEVELS = re.compile(r"^ai .*? for .*?\(levels=(\d+).*?best=(.+?) ([\d.]+)")
_NO_MATCH = re.compile(r"^ai: no match for .*?best=(.+?) ([\d.]+)")
_TOO_LARGE = re.compile(r"^(\d+) > (\d+)$")


def humanize(note: str) -> str:
    """引擎备注 → 用户可读中文。空备注返回空串，认不出原样带回。"""
    s = str(note or "").strip()
    if not s:
        return ""
    if s == "grayscale/keyword":
        return HIT_BLACKLIST
    m = _EXCL_UNCERTAIN.match(s)
    if m:
        return (f"命中「别找」的排除词（{m.group(1)}），"
                f"但 AI 只有 {_num(m.group(2))} 分、不敢确定")
    m = _EXCL_HIT.match(s)
    if m:
        return f"命中「别找」的排除词（{m.group(1)}，{_num(m.group(2))} 分）"
    m = _PREF_DOWN.match(s)
    if m:
        return (f"AI 觉得匹配，但你以前判过这类不喜欢"
                f"（最接近 {m.group(1)} {_num(m.group(2))} 分，"
                f"按口味折算后 {_num(m.group(3))} 分）")
    m = _LEVELS.match(s)
    if m:
        return (f"大主题匹配了，细分词还没对上"
                f"（共 {m.group(1)} 层，最接近 {m.group(2)} "
                f"{_num(m.group(3))} 分）")
    m = _UNCERTAIN.match(s)
    if m:
        return f"AI 拿不准：分数正好卡在阈值附近（{m.group(1)} {_num(m.group(2))} 分）"
    m = _NO_MATCH.match(s)
    if m:
        return (f"没匹配上「要找」的词"
                f"（最接近 {m.group(1)}，才 {_num(m.group(2))} 分）")
    m = _TOO_LARGE.match(s)
    if m:
        return f"文件太大（{_size(int(m.group(1)))}，超过上限 {_size(int(m.group(2)))}）"
    return s[:80]


def _size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f}MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f}KB"
    return f"{n}B"
