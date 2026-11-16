"""F19 快捷键自定义：键位注册表 + 解析/冲突（纯逻辑，不碰 Qt、不做 IO）。

订单原文（R154 第二组 F19）：
  设置页「快捷键」表：列出主要动作当前键位 → 点击改键 → 冲突中文提示；
  至少覆盖审核流（喜欢/不喜欢/跳过/翻页）、灯箱、任务启停；
  持久化 cfg，恢复默认按钮。

为什么单独成模块：这套口径要被**五处**共用——桌面设置对话框
（gui/hotkey_dialog.py）、审核页键盘流（gui/review_tab.py eventFilter）、
灯箱按键（gui/lightbox.py keyPressEvent + 按钮文案 + 提示语）、任务启停
快捷键（gui/main_window.py）、F1 快捷键说明（main_window.show_shortcuts）。
散在五处各写一份，迟早出现「设置里写着 L、实际还是 →」这类对不上的缺陷
（本仓「两个口径不一致」的坑已反复踩过），所以收敛在这里。

口径（写死，别各自发挥）：
* config.json 里**只存与默认不同的覆盖**（action_id → 序列串，见
  ``overrides_only``）；默认键位活在本模块。升级上来的老配置没有这个键
  ⇒ 行为与之前逐字节一致。
* 序列串规范：``"Ctrl+Z"`` / ``"→"`` / ``"Space"`` / ``"F5"`` / ``"RightCtrl"``。
  修饰键固定按 Ctrl+Alt+Shift+Meta 排序输出；单键不带修饰键。
* **带修饰键的组合要求精确匹配；单键不看修饰键**（匹配规则在 Qt 桥接层
  hotkeys_qt.event_matches 实现，判定仍走本模块的 parse_sequence）——
  按 ``+`` 键在键盘上天然带 Shift，若因此失灵就是缺陷（现行代码同样只看 key）。
* **冲突只在同一上下文内检测**：审核页的 →（喜欢）与图库灯箱的 →
  （下一张）永远不同时在场，报冲突是误报。上下文见 ACTIONS 的 context 字段。
* **固定别名**（不参与自定义、不参与冲突检测）：灯箱 ↑↓ 恒翻页、
  P 恒幻灯片、Esc 恒关闭/退全屏。留着是为了保住现行行为——撤了就是回归；
  若自定义键与固定别名同键，``alias_clashes`` 会给中文告警（会一起触发）。
* **非法覆盖一律回退默认**（与 rateplan「配错不许卡死」同口径：
  键位配错不许让动作消失）。配置里手写垃圾值不会把界面搞失效。
* 恢复默认 = 清空覆盖字典（``overrides_only`` 对全默认的映射返回 {}）。
"""
from __future__ import annotations

import re

#: 修饰键的规范输出顺序（同时是匹配时的枚举顺序）
MODIFIER_ORDER = ("Ctrl", "Alt", "Shift", "Meta")

#: 特殊键：右 Ctrl。Windows 可按 VK_RCONTROL 区分左右；其他平台等效任意 Ctrl。
RIGHT_CTRL = "RightCtrl"

# ---------------------------------------------------------------- 别名表
# 输入宽容（QKeySequence.toString / 用户手写 / 中文说法都收），输出规范。

_KEY_ALIASES = {
    # 方向键（自映射必须逐字对上：写错一个符号，整套箭头键静默失灵）
    "up": "↑", "↑": "↑", "上": "↑",
    "down": "↓", "↓": "↓", "下": "↓",
    "left": "←", "←": "←",
    "right": "→", "→": "→",
    # 命名键
    "space": "Space", "空格": "Space",
    "esc": "Esc", "escape": "Esc",
    "return": "Enter", "enter": "Enter", "回车": "Enter",
    "tab": "Tab",
    "backspace": "Backspace", "bs": "Backspace", "退格": "Backspace",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert", "ins": "Insert",
    "home": "Home", "end": "End",
    "pgup": "PageUp", "pageup": "PageUp", "prior": "PageUp",
    "pgdown": "PageDown", "pagedown": "PageDown", "next": "PageDown",
    "plus": "+", "+": "+",
    "minus": "-", "-": "-",
    "equal": "=", "=": "=",
    "comma": ",", ",": ",",
    "period": ".", ".": ".",
    "slash": "/", "/": "/",
    "semicolon": ";", ";": ";",
    "bracketleft": "[", "[": "[",
    "bracketright": "]", "]": "]",
    "backslash": "\\", "\\": "\\",
    "apostrophe": "'", "'": "'",
    "quoteleft": "`", "`": "`",
    "rightctrl": RIGHT_CTRL, "右ctrl": RIGHT_CTRL,
}

_MOD_ALIASES = {
    "ctrl": "Ctrl", "control": "Ctrl",
    "alt": "Alt", "option": "Alt",
    "shift": "Shift",
    "meta": "Meta", "win": "Meta", "super": "Meta",
    "cmd": "Meta", "command": "Meta",
}

_FKEY_RE = re.compile(r"^f([1-9]|1[0-2])$")
_ALPHA_RE = re.compile(r"^[a-z]$")
_DIGIT_RE = re.compile(r"^[0-9]$")

#: 界面展示用的中文键名（display() 用）
_DISPLAY = {
    "↑": "↑", "↓": "↓", "←": "←", "→": "→",
    "Space": "空格", "Enter": "回车", "Esc": "Esc",
    RIGHT_CTRL: "右 Ctrl",
}

# ---------------------------------------------------------------- 动作注册表
# context（上下文）同时是设置表里的分组标题与冲突检测的作用域。
# default 必须与**现行硬编码键位逐字节一致**——改了就是行为回归。
ACTIONS: tuple[dict, ...] = (
    # ---- 审核页（缩略图网格，焦点在网格上时）----
    {"id": "review.up", "label": "上一张（选中上一行）",
     "context": "审核页", "default": "↑"},
    {"id": "review.down", "label": "下一张（选中下一行）",
     "context": "审核页", "default": "↓"},
    {"id": "review.like", "label": "喜欢", "context": "审核页", "default": "→"},
    {"id": "review.dislike", "label": "不喜欢", "context": "审核页", "default": "←"},
    {"id": "review.reason", "label": "说原因（判不喜欢并弹原因框）",
     "context": "审核页", "default": "E"},
    {"id": "review.skip", "label": "待定", "context": "审核页", "default": RIGHT_CTRL},
    # ---- 审核灯箱（审核页双击/回车打开的大图，on_decide 非空）----
    {"id": "rlight.like", "label": "喜欢（判完自动下一张）",
     "context": "审核灯箱", "default": "→"},
    {"id": "rlight.prev", "label": "上一张", "context": "审核灯箱", "default": "←"},
    {"id": "rlight.next", "label": "下一张", "context": "审核灯箱", "default": "↓"},
    # ---- 图库灯箱（图库双击打开的大图，无审核上下文）----
    {"id": "glight.prev", "label": "上一张", "context": "图库灯箱", "default": "←"},
    {"id": "glight.next", "label": "下一张", "context": "图库灯箱", "default": "→"},
    # ---- 灯箱（通用）：两种灯箱里语义相同，合并一组 ----
    {"id": "light.zoom_in", "label": "放大", "context": "灯箱（通用）", "default": "+"},
    {"id": "light.zoom_out", "label": "缩小", "context": "灯箱（通用）", "default": "-"},
    {"id": "light.zoom_reset", "label": "适应窗口（缩放复位）",
     "context": "灯箱（通用）", "default": "0"},
    {"id": "light.fullscreen", "label": "全屏 / 退出全屏",
     "context": "灯箱（通用）", "default": "F"},
    {"id": "light.rotate", "label": "旋转", "context": "灯箱（通用）", "default": "R"},
    {"id": "light.slideshow", "label": "幻灯片（播放 / 暂停）",
     "context": "灯箱（通用）", "default": "Space"},
    # ---- 任务（主窗口，焦点在主窗口时）----
    {"id": "task.start", "label": "开始运行选中的任务",
     "context": "任务", "default": "F5"},
    {"id": "task.stop", "label": "停止选中的任务",
     "context": "任务", "default": "F6"},
)

#: 设置表里的分组顺序
CONTEXTS = ("审核页", "审核灯箱", "图库灯箱", "灯箱（通用）", "任务")

_BY_ID = {a["id"]: a for a in ACTIONS}
DEFAULT_HOTKEYS = {a["id"]: a["default"] for a in ACTIONS}

#: 固定别名：始终有效，不参与自定义与冲突检测。
#: (键位, 中文说明, 该别名"归属"的动作 id 集合——同键不算告警)
FIXED_ALIASES = (
    ("↑", "灯箱里上一张", {"rlight.prev", "glight.prev"}),
    ("↓", "灯箱里下一张", {"rlight.next", "glight.next"}),
    ("P", "灯箱幻灯片播放/暂停", {"light.slideshow"}),
    ("Esc", "灯箱关闭 / 退出全屏", set()),
)

#: Esc 单独说明（无对应可自定义动作，仅供 F1 说明与对话框展示）
FIXED_ESC_NOTE = "Esc：灯箱关闭 / 退出全屏"


# ---------------------------------------------------------------- 解析
def _canonical_token(part: str):
    low = part.strip().lower()
    if not low:
        return None
    if low in _KEY_ALIASES:
        return _KEY_ALIASES[low]
    if _FKEY_RE.match(low):
        return "F" + low[1:]
    if _ALPHA_RE.match(low):
        return low.upper()
    if _DIGIT_RE.match(low):
        return low
    return None


def parse_sequence(seq):
    """解析序列串 → ``(frozenset(修饰键), 键位 token)``；非法返回 ``None``。

    非法形态：空串 / 非字符串 / 只按修饰键 / 两个非修饰键 / 重复修饰键 /
    不认识的键名 / ``RightCtrl`` 带修饰键。

    ⚠ ``+`` 既是分隔符又是合法键位（灯箱放大的默认键）——形态
    ``"+"`` / ``"Ctrl++"`` / ``"Ctrl+Alt++"`` 必须认（否则默认键位自己
    就解析不了，等于放大键位是坏的）。
    """
    if not isinstance(seq, str):
        return None
    s = seq.strip()
    if not s:
        return None
    plus_key = False
    if s.endswith("++"):
        if s == "++":
            return None
        s = s[:-2]
        plus_key = True
    elif s == "+":
        plus_key = True
        s = ""
    parts = [p.strip() for p in s.split("+")] if s else []
    if any(p == "" for p in parts):
        return None
    mods: list[str] = []
    key = None
    for p in parts:
        low = p.lower()
        if low in _MOD_ALIASES:
            m = _MOD_ALIASES[low]
            if m in mods:
                return None
            mods.append(m)
            continue
        tok = _canonical_token(p)
        if tok is None or tok in MODIFIER_ORDER:
            return None
        if key is not None:
            return None
        key = tok
    if plus_key:
        if key is not None:
            return None
        key = "+"
    if key is None:
        return None
    if key == RIGHT_CTRL and mods:
        return None
    return (frozenset(mods), key)


def normalize_sequence(seq):
    """规范输出：``"shift+z"`` → ``"Shift+Z"``；非法 → ``None``。"""
    p = parse_sequence(seq)
    if p is None:
        return None
    mods, key = p
    return "+".join([m for m in MODIFIER_ORDER if m in mods] + [key])


def display(seq) -> str:
    """给人看的一行：``"Space"`` → ``"空格"``、``"RightCtrl"`` → ``"右 Ctrl"``。"""
    p = parse_sequence(seq)
    if p is None:
        return str(seq or "")
    mods, key = p
    parts = [m for m in MODIFIER_ORDER if m in mods]
    parts.append(_DISPLAY.get(key, key))
    return "+".join(parts)


# ---------------------------------------------------------------- 解析覆盖
def resolve(hotkeys, action_id: str) -> str:
    """取某个动作的生效键位：覆盖优先，非法/未知 → 默认。"""
    act = _BY_ID.get(action_id)
    if act is None:
        return ""
    ov = (hotkeys or {}).get(action_id) if isinstance(hotkeys, dict) else None
    if isinstance(ov, str):
        n = normalize_sequence(ov)
        if n:
            return n
    return act["default"]


def effective(hotkeys) -> dict:
    """全部动作的生效键位（action_id → 规范序列串）。"""
    return {aid: resolve(hotkeys, aid) for aid in _BY_ID}


def overrides_only(hotkeys) -> dict:
    """与默认不同的部分（落盘用）：全默认 ⇒ ``{}`` = 恢复默认。"""
    eff = effective(hotkeys)
    return {aid: seq for aid, seq in eff.items()
            if seq != DEFAULT_HOTKEYS[aid]}


def sanitize_overrides(hotkeys) -> dict:
    """落盘前清洗：只认已知动作 + 能解析的序列串（手写垃圾不进 config.json）。"""
    if not isinstance(hotkeys, dict):
        return {}
    out = {}
    for aid, seq in hotkeys.items():
        if aid not in _BY_ID:
            continue
        n = normalize_sequence(seq) if isinstance(seq, str) else None
        if n:
            out[aid] = n
    return out


# ---------------------------------------------------------------- 冲突检测
def find_conflicts(hotkeys) -> list:
    """同上下文内两个动作绑同一键 → 冲突列表。

    返回 ``[{"context", "seq", "aids": [...], "labels": [...]}]``。
    跨上下文同键**不是**冲突（两者永不同时在场）。
    """
    buckets: dict = {}
    for aid, seq in effective(hotkeys).items():
        buckets.setdefault((_BY_ID[aid]["context"], seq), []).append(aid)
    out = []
    for (ctx, seq), aids in buckets.items():
        if len(aids) > 1:
            out.append({"context": ctx, "seq": seq, "aids": aids,
                        "labels": [_BY_ID[a]["label"] for a in aids]})
    return out


#: 固定别名**只存在于灯箱里**（↑↓ 翻页 / P 幻灯片 / Esc 关闭）。
#: 审核页网格的 ↑↓ 是可自定义动作（review.up/down），与别名不同时在场
#: ⇒ alias_clashes 只在这些上下文里找，别把审核页的默认键误报成冲突。
_ALIAS_CONTEXTS = frozenset({"审核灯箱", "图库灯箱", "灯箱（通用）"})


def alias_clashes(hotkeys) -> list:
    """自定义键与固定别名同键（且不是该别名的归属动作）→ 告警列表。

    返回 ``[{"seq", "desc", "aids": [...], "labels": [...]}]``。
    """
    eff = effective(hotkeys)
    out = []
    for key, desc, owners in FIXED_ALIASES:
        n = normalize_sequence(key)
        if n is None:
            continue
        hits = [aid for aid, seq in eff.items()
                if seq == n and aid not in owners
                and _BY_ID[aid]["context"] in _ALIAS_CONTEXTS]
        if hits:
            out.append({"seq": n, "desc": desc, "aids": hits,
                        "labels": [_BY_ID[a]["label"] for a in hits]})
    return out


def warnings_for(hotkeys) -> dict:
    """action_id → 中文告警（没有告警的动作不在字典里）。设置表第三列用。

    语义口径：同一个键绑了两个动作时，**实际只有排在前面的生效**
    （按键处理按注册表顺序逐个匹配，命中即 return）——所以文案说
    「被挡住」而不是「一起触发」，说后者就是在骗用户。
    """
    out: dict = {}
    for c in find_conflicts(hotkeys):
        for aid in c["aids"]:
            others = "、".join(lbl for a2, lbl in zip(c["aids"], c["labels"])
                               if a2 != aid)
            out[aid] = (f"和「{others}」绑了同一个键（都在{c['context']}里）："
                        f"实际只有排在前面的生效，另一个会被挡住")
    for c in alias_clashes(hotkeys):
        for aid in c["aids"]:
            msg = (f"这个键同时是灯箱固定别名（{c['desc']}）：改绑的动作生效，"
                   f"固定别名那个会被挡住")
            out[aid] = f"{out[aid]}；{msg}" if aid in out else msg
    return out


def context_actions(context: str) -> tuple:
    """某个上下文下的动作（保持注册表顺序 = 设置表行顺序）。"""
    return tuple(a for a in ACTIONS if a["context"] == context)


def action_label(action_id: str) -> str:
    a = _BY_ID.get(action_id)
    return a["label"] if a else ""


# ── V0.0.21 · SweetWeb 内置窗口的固定键位说明 ───────────────────────
# 桌面端 F1 说明与网页端快捷键说明共用这一份文字，避免同一件事在两端
# 出现两种叫法（用户 2026-11-10 明确要求桌面网页逐字一致）。
# 这里的键位不参与自定义：它们由内核事件层与 nativeShell.js 决策表固定。
SHELL_FIXED_NOTES = (
    {"key": "Ctrl+A", "label": "全选当前列表（输入框内为全选文字）"},
    {"key": "Ctrl+F / F3", "label": "定位全局搜索框（任务页为定位过滤框）"},
    {"key": "Ctrl+S", "label": "保存当前设置或表单"},
    {"key": "F5 / Ctrl+R", "label": "刷新当前数据，不重载界面"},
    {"key": "Ctrl+C", "label": "复制已选中文本（输入框内照常编辑）"},
    {"key": "Ctrl+P、Ctrl+U、F12", "label": "浏览器功能键已停用"},
    {"key": "Ctrl+W、Ctrl+T、Alt+←/→", "label": "浏览器功能键已停用"},
    {"key": "Ctrl+滚轮、Ctrl+±、Ctrl+0", "label": "页面缩放已停用"},
    {"key": "右键菜单", "label": "仅提供文本编辑功能"},
    {"key": "外部链接", "label": "交由系统默认浏览器打开"},
)
SHELL_MODE_NOTE = (
    "内置窗口采用桌面软件键位。打印、另存为、查看源代码、开发者工具、"
    "新标签页、历史记录、收藏、地址栏、页面缩放与前进后退均已停用；"
    "外部链接交由系统默认浏览器打开。"
)
