"""F19：hotkeys.py 的 Qt 桥接（token↔Qt.Key、事件匹配、按键捕获）。

纯逻辑在 ``hotkeys.py``（可无 Qt 单测）；这里只做「键位串 ↔ Qt 事件」的
翻译。⚠ **判定口径仍然只有一份**（``hotkeys.parse_sequence``）——本模块
不重写任何键位规则，只翻译；散两份规则迟早出现「对话框认得、界面不认」。

已知平台事实（2026-10-22 实测，PySide6 6.11.2）：
* ``QKeySequence("→")`` / ``QKeySequence("F5")`` / ``QKeySequence("Ctrl+Z")``
  都能解析 ⇒ 任务快捷键直接拿规范串构造 QKeySequence 即可；
* ``QKeySequence("RightCtrl")`` 解析后 ``toString()`` 是空串 ⇒ 表达不了，
  ``qt_sequence`` 对它返回 None（调用方跳过该快捷键，见 main_window）；
* ``ev.key()`` 是 int、``ev.modifiers()`` 是 flag（取 ``.value`` 或 ``&``）；
* 合成 QKeyEvent 的 ``nativeVirtualKey()`` 恒 0（真实右 Ctrl 在 Windows 上
  才是 0xA3）⇒ 单测里右 Ctrl 分支按当前代码同款语义走（Windows 上合成
  事件不算右 Ctrl，其他平台算）。
"""
from __future__ import annotations

import re
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from ..hotkeys import MODIFIER_ORDER, RIGHT_CTRL, parse_sequence

#: 命名键 token → Qt.Key
_NAMED = {
    "↑": Qt.Key.Key_Up, "↓": Qt.Key.Key_Down,
    "←": Qt.Key.Key_Left, "→": Qt.Key.Key_Right,
    "Space": Qt.Key.Key_Space, "Esc": Qt.Key.Key_Escape,
    "Enter": Qt.Key.Key_Return, "Tab": Qt.Key.Key_Tab,
    "Backspace": Qt.Key.Key_Backspace, "Delete": Qt.Key.Key_Delete,
    "Insert": Qt.Key.Key_Insert, "Home": Qt.Key.Key_Home,
    "End": Qt.Key.Key_End, "PageUp": Qt.Key.Key_PageUp,
    "PageDown": Qt.Key.Key_PageDown,
    "+": Qt.Key.Key_Plus, "-": Qt.Key.Key_Minus, "=": Qt.Key.Key_Equal,
    ",": Qt.Key.Key_Comma, ".": Qt.Key.Key_Period, "/": Qt.Key.Key_Slash,
    ";": Qt.Key.Key_Semicolon, "[": Qt.Key.Key_BracketLeft,
    "]": Qt.Key.Key_BracketRight, "\\": Qt.Key.Key_Backslash,
    "'": Qt.Key.Key_Apostrophe, "`": Qt.Key.Key_QuoteLeft,
}

#: 修饰键 → Qt 键盘修饰 flag
_MOD_FLAGS = {
    "Ctrl": Qt.KeyboardModifier.ControlModifier,
    "Alt": Qt.KeyboardModifier.AltModifier,
    "Shift": Qt.KeyboardModifier.ShiftModifier,
    "Meta": Qt.KeyboardModifier.MetaModifier,
}

_VK_RCONTROL = 0xA3


def qt_key(token: str):
    """token → Qt.Key；认不出返回 None。"""
    if token in _NAMED:
        return _NAMED[token]
    if re.fullmatch(r"[A-Z0-9]", token) or re.fullmatch(r"F([1-9]|1[0-2])", token):
        return getattr(Qt.Key, "Key_" + token, None)
    return None


def _right_ctrl_pressed(ev) -> bool:
    """是否按了**右** Ctrl。口径与改前代码逐字节一致：
    Windows 按 VK_RCONTROL 区分；其他平台（含合成事件）恒 True。"""
    try:
        if int(ev.key()) != int(Qt.Key.Key_Control):
            return False
        if sys.platform.startswith("win"):
            return ev.nativeVirtualKey() == _VK_RCONTROL
    except Exception:
        return True
    return True


def event_matches(ev, seq: str) -> bool:
    """这次按键是否命中序列 ``seq``（判定规则全在 hotkeys.parse_sequence）。

    口径：
    * 带修饰键的组合**要求精确匹配**（按齐才算）；
    * 单键**不看修饰键**——按 ``+`` 天然带 Shift，若因此失灵就是缺陷
      （现行代码同样只看 key，这里保持行为不变）；
    * ``RightCtrl``：只要「右 Ctrl 被按下」即命中（ modifiers 不参与，
      它自己就是那个修饰键的本体）。
    """
    p = parse_sequence(seq)
    if p is None:
        return False
    mods, key = p
    if key == RIGHT_CTRL:
        return _right_ctrl_pressed(ev)
    k = qt_key(key)
    if k is None or int(ev.key()) != int(k):
        return False
    if mods:
        for m, flag in _MOD_FLAGS.items():
            if bool(ev.modifiers() & flag) != (m in mods):
                return False
    return True


def seq_from_event(ev):
    """把一次真实按键翻成规范序列串；认不出 / 只按修饰键 → None。

    捕获规则（对话框录键用）：
    * **没有 Ctrl/Alt/Meta 时丢掉 Shift**——``Shift+E`` 就是 ``E``，
      ``+`` 键（Shift+=）就是 ``+``；带 Ctrl/Alt 时 Shift 照留
      （``Ctrl+Shift+Z`` 是合法意图）。
    * 右 Ctrl 单独成键（``RightCtrl``）；只按左修饰键 → None（继续等）。
    """
    key = int(ev.key())
    mods = ev.modifiers()
    names = [m for m in MODIFIER_ORDER if (mods & _MOD_FLAGS[m])]
    if key == int(Qt.Key.Key_Control) and _right_ctrl_pressed(ev):
        return RIGHT_CTRL if not names else None
    if key in (int(Qt.Key.Key_Control), int(Qt.Key.Key_Shift),
               int(Qt.Key.Key_Alt), int(Qt.Key.Key_Meta)):
        return None
    tok = token_from_qt(key)
    if tok is None or tok == RIGHT_CTRL:
        return None
    if not [m for m in names if m != "Shift"]:
        names = []
    return "+".join(names + [tok])


def token_from_qt(key: int):
    """Qt 键值 → 规范 token；认不出返回 None。"""
    for tok, k in _NAMED.items():
        if int(k) == key:
            return tok
    if 0x30 <= key <= 0x39:                       # 0-9
        return chr(key)
    if 0x41 <= key <= 0x5A:                       # A-Z
        return chr(key)
    if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F12):
        return "F%d" % (key - int(Qt.Key.Key_F1) + 1)
    return None


def qt_sequence(seq: str):
    """规范序列串 → QKeySequence（任务快捷键用）；表达不了 → None。

    ``RightCtrl`` QKeySequence 表达不了（实测 toString 为空串）⇒ None。
    """
    p = parse_sequence(seq)
    if p is None:
        return None
    if p[1] == RIGHT_CTRL:
        return None
    ks = QKeySequence(seq)
    return ks if ks.toString() else None
