"""V0.0.19 · 现代化视觉样式（毛玻璃模式的 Qt 侧配套）。

``gui/acrylic.py`` 负责 Windows 系统级背板（Acrylic / Mica / 半透明近似），
本模块只负责 Qt 侧的观感配套：圆角、留白、浮层半透明与柔和描边。

纪律：

1. **默认不生效**。只有 ``cfg.glass_enabled`` 为真且系统背板调用成功时，
   才由主窗口把这段样式追加到现有样式表末尾；关闭时原样摘除。
   这样默认外观、皮肤像素护栏、全部既有测试都不受任何影响。
2. **不碰语义色**。只加圆角 / 间距 / 中性半透明（``rgba(127,127,127,...)``
   在深浅两套皮肤下都成立），不改写强调色、文字色与状态色。
3. **不设全窗半透明**。``WA_TranslucentBackground`` 会让文字一起发虚，
   这里刻意不启用；玻璃感来自系统背板 + 浮层卡片，而不是把整窗调透明。
"""
from __future__ import annotations

BEGIN = "/* V0.0.19-GLASS-BEGIN */"
END = "/* V0.0.19-GLASS-END */"

GLASS_QSS = """
/* V0.0.19-GLASS-BEGIN */
QMenuBar { background: transparent; }
QMenu { border-radius: 10px; padding: 4px; }
QToolTip { border-radius: 7px; padding: 5px 8px; }
QGroupBox, QTabWidget::pane {
    background: rgba(127, 127, 127, 0.055);
    border-radius: 10px;
}
QFrame[frameShape="1"], QFrame[frameShape="4"] { border-radius: 10px; }
QPushButton { border-radius: 8px; padding: 5px 10px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit,
QTextEdit, QDateEdit {
    border-radius: 7px;
    padding: 4px 8px;
}
QTabBar::tab { border-top-left-radius: 7px; border-top-right-radius: 7px; }
QProgressBar { border-radius: 6px; }
QSplitter::handle { background: rgba(127, 127, 127, 0.12); }
QStatusBar { background: rgba(127, 127, 127, 0.045); }
/* V0.0.19-GLASS-END */
"""


def glass_stylesheet(base: str = "") -> str:
    """把玻璃样式追加到现有样式表末尾；重复调用不会叠加。"""
    cur = base or ""
    if BEGIN in cur:
        return cur
    sep = "\n" if cur.strip() else ""
    return cur + sep + GLASS_QSS


def strip_glass(base: str = "") -> str:
    """摘除玻璃样式，恢复默认外观（找不到标记时原样返回）。"""
    cur = base or ""
    i = cur.find(BEGIN)
    if i < 0:
        return cur
    # 标记前的注释换行也一并去掉，避免留下空行
    start = max(0, i - 1)
    j = cur.find(END, i)
    if j < 0:
        return cur[:start]
    stop = j + len(END)
    while stop < len(cur) and cur[stop] in "\r\n":
        stop += 1
    return (cur[:start] + cur[stop:]).rstrip()


def apply_look(window, on: bool) -> None:
    """给窗口开/关现代化视觉。任何异常都静默跳过，绝不影响软件启动。"""
    try:
        sheet = window.styleSheet() or ""
        window.setStyleSheet(glass_stylesheet(sheet) if on
                             else strip_glass(sheet))
    except Exception:
        pass
