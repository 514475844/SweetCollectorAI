"""Main desktop window (PySide6).

Layout (Windows-style narrow left rail):
  LEFT  (~210px) : task list + queue controls/status
  RIGHT (tabs)   : 任务 (config form) | 图库 (thumbnail grid) | 日志

Runs go through the TaskQueue so several tasks can progress at once; the UI
thread never blocks.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import warnings
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from PySide6.QtCore import (Qt, QTimer, QSize, Signal, QEvent, QThread, QUrl,
                            QObject, QRunnable, QThreadPool, QRect, QPoint)
from PySide6.QtGui import (QPixmap, QAction, QIcon, QPainter, QColor,
                           QActionGroup, QTextCursor, QDesktopServices)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QListWidget, QListWidgetItem,
    QStackedWidget,
    QLineEdit, QTextEdit, QPushButton, QSpinBox, QToolButton, QDoubleSpinBox, QCheckBox,
    QComboBox, QFormLayout, QHBoxLayout, QVBoxLayout, QSplitter, QProgressBar,
    QFileDialog, QLabel, QTabWidget, QMessageBox, QToolBar, QGroupBox,
    # #qB皮肤：底部状态栏改用真正的 QStatusBar（原来是一个自绘透明 QWidget）
    QStatusBar,
    QInputDialog, QSlider, QFrame,
    # r66：全局顶栏 _top_strip 的右对齐占位块要靠 Expanding 顶到右端。
    # 作业单片段直接用了 QSizePolicy 但本文件没import 它 ⇒ NameError。
    QSizePolicy,
    # r39b：pyflakes 揪出下面这几个在整个文件里被用了却从没 import 过，
    # 一旦点到对应菜单就是 NameError（弹窗直接打不开）。统一提到顶层。
    QDialog, QDialogButtonBox, QProgressDialog, QLayout,
)


# ---------------------------------------------------------------------------
# R47/B11：可换行的流式布局 + 文本省略
# ---------------------------------------------------------------------------
# 为什么要自己写：Qt 没有内置 FlowLayout（PySide6 也没暴露 QtWidgets 那个
# 私有类）。而「常用词」这一行是**不定长**的内容（用户历史词 30 条），
# 用 QHBoxLayout 只会一路往右撑，把相邻控件顶掉/盖住（真机反馈）。
# 60 行换一个「不重叠、不溢出」的列表，划算。
class _FlowLayout(QLayout):
    """按宽度自动换行地摆放子控件。"""

    def __init__(self, parent=None, margin: int = 0, spacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):  # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        if 0 <= i < len(self._items):
            return self._items.pop(i)
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, test_only=False)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for it in self._items:
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _layout(self, rect: QRect, test_only: bool):
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = eff.x(), eff.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            nx = x + hint.width()
            if line_h > 0 and nx > eff.right() + 1:      # 放不下 → 换行
                x = eff.x()
                y = y + line_h + self.spacing()
                nx = x + hint.width()
                line_h = 0
            if not test_only:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx + self.spacing()
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class _FlowRefit(QObject):
    """R179：容器宽度变了（拖窗口/布局抖动）就重算流式容器的高度。

    双通道（缺一不可，实测踩出来的）：
    ① eventFilter：容器**可见**时 resize 事件照常来，实时重算；
    ② QTimer 120ms 宽度巡检：**隐藏/未显示**的 widget resize 根本不发
      QEvent.Resize（Qt 对不可见 widget 跳过事件投递，探针实测
      eventFilter 收不到）——靠低频比对宽度缓存兜底，宽度没变就零动作，
      四个实例的成本可忽略。
    构造即安装（调用形态 ``_FlowRefit(box, bl)``，护栏按此锚源码）。
    """

    def __init__(self, box, lay):
        super().__init__(box)
        self._box = box
        self._lay = lay
        self._last_w = -1
        try:
            box.installEventFilter(self)
        except Exception:
            pass
        try:
            self._tick = QTimer(self)
            self._tick.setInterval(120)
            self._tick.timeout.connect(self._check)
            self._tick.start()
        except Exception:
            self._tick = None
        try:
            # 首次校准：0ms 定时器在本轮事件循环里就会跑 —— 容器刚建好
            # （或刚被重建）时宽度可能已就位，先 fit 一次，不等多巡检。
            QTimer.singleShot(0, self._check)
        except Exception:
            pass

    def _check(self):
        try:
            w = int(self._box.width() or 0)
            if w != self._last_w:
                self._last_w = w
                fit_flow_height(self._box, self._lay)
        except Exception:
            pass

    def eventFilter(self, obj, ev):
        try:
            if obj is self._box and ev.type() == QEvent.Type.Resize:
                fit_flow_height(self._box, self._lay)
        except Exception:
            pass
        return False


def fit_flow_height(box, lay) -> int:
    """R179/R203：把流式容器**显式撑高**到「装得下当前宽度下全部行」。

    为什么不能指望 heightForWidth 自动传播：容器高度被父布局按**单行**
    给死（实测 29px），HFW 的多行结果根本没人采纳 ⇒ 第二行被裁到容器外
    （B11「常用词盖住旁边内容」的复发根因）。算完直接 setMinimumHeight，
    数字握在自己手里。宽 0（控件还没显示）/异常一律兜底 24（单行高），
    绝不让 GUI 槽炸掉。
    ⚠ R47-c：算 hfw 前必须把每个 chip **ensurePolished**——刚 setText+
    setStyleSheet 的按钮在 polish 前 sizeHint 是 (0,0)（pytest 打点实测），
    24 个全按 0 高算 → hfw 低估成 1~2 行 → setMinimumHeight 偏小 →
    真正布局时 chip 溢出容器。polish 是同步动作，不依赖事件循环。
    """
    h = 24
    try:
        for i in range(lay.count()):
            it = lay.itemAt(i)
            wg = it.widget() if it is not None else None
            if wg is not None:
                wg.ensurePolished()
    except Exception:
        pass
    try:
        w = int(box.width() or 0)
        want = int(lay.heightForWidth(w if w > 0 else 320))
        h = max(24, want)
    except Exception:
        h = 24
    try:
        box.setMinimumHeight(h)
    except Exception:
        pass
    return h


def _elide(fm, text: str, width: int) -> str:
    """按像素宽度省略过长文本（B11：chip 不许把整行撑爆）。"""
    t = str(text)
    try:
        return fm.elidedText(t, Qt.TextElideMode.ElideRight, int(width))
    except Exception:
        return t

from PySide6.QtGui import QIcon

from ..config import (load_config, save_config, default_exclusions,
                      user_data_dir)
from ..db import Database
from ..stats import failure_reasons, library_stats
from ..resources import icon_path
from ..utils.url import domain_of
from ..crawler.fetcher import Fetcher
from .zh_labels import zh_buttons
from ..cookies import get_cookie, set_cookie, del_cookie
from .review_tab import ReviewTab
from ..logutil import setup_logging
# P1-4：行内 setStyleSheet 自带优先级，QSS 换肤盖不住它 → 一律走
# `_tstyle(控件, "模板")`，模板里的 {key} 由 theme.css() 按当前皮肤解析，
# 并登记进 theme 的重渲表（切皮肤时统一刷新，不必各处补回调）。
from .theme import style as _tstyle
# P1-4 收口：自绘/取色路径统一入口（`c(dark, key, fallback)`）。
# 与 `_tstyle` 分工：`_tstyle` 管**行内 QSS 模板**（`{key}` 占位 + 自动重渲），
# `_tc` 管**字符串里要一个具体色值**的地方（QColor / HTML span / setPen）。
from .theme import c as _tc
# R128：换肤时重画「颜色烤进 QPixmap」的控件（任务行状态点等）。
# QSS 的 unpolish/polish 刷不动已画好的位图，必须显式重画一次。
from .theme import register_redraw as _register_redraw
# 网格几何 / 方形缩略图 / 自适应预览：图库、审核、实时抓图墙共用一套（r31）
from .table_view import PreviewLabel, apply_grid_layout, square_thumb_pixmap
from . import icons
from .ssim_dialog import SimilarDialog, find_similar_groups
from ..models import TaskConfig, TaskStatus
from ..proxy import ProxyPool, ProxyEntry
# r40：界面标签统一走 i18n（label_text=显示名、tag_key=存库/比对的原始英文）。
# 排序、召回、入库一律用 tag_key / 原始英文标签，语言包只改「显示」。
from ..i18n import label_text, tag_key, translate_tags
from ..repo import get_task, list_tasks, save_task
from ..repo import images_cursor_for, list_images_page
from ..scheduler.queue import TaskQueue, enqueue_auto_tasks


STATUS_TXT = {"running": "下载中", "queued": "排队中", "idle": "就绪",
              "done": "已完成", "error": "失败", "stopped": "已停止"}
# P1-4 收口：状态点颜色原先是一张写死的 hex 表（换皮肤不动 = 半成品）。
# 现在改成**函数**，每次按当前深浅 + 当前皮肤现取。注意不能做成模块级
# 常量 dict —— 那等于把「取色时机」钉死在 import 那一刻，用户后来换皮肤
# 就还是旧颜色（与 icons 同理，见 icons.set_themed_icon 的注释）。
# 语义键见 theme.PALETTE：st_done 跟随皮肤，其余是语义色刻意不跟。
def status_color(status: str, dark: bool | None = None) -> str:
    """任务状态点取色（running/queued/idle/done/error/stopped → hex）。"""
    from .theme import c as _c, _CURRENT_DARK
    _dark = _CURRENT_DARK if dark is None else bool(dark)
    return _c(_dark, "st_" + (status if status in STATUS_TXT else "idle"),
              "#b6bcc7")


# 保留旧名给外部/测试引用（值改成动态取色的快照，仅供「有哪些状态」判断）
STATUS_COLORS = {k: status_color(k) for k in STATUS_TXT}
# 解锁尺寸约束用的"无限大"（PySide6 没导出 QWIDGETSIZE_MAX，别再 import）
_WIDGET_MAX_SIZE = 1 << 24


def _query_images_by_ids(db, ids, columns: str = "id, path") -> list:
    """按 id 批量取图，分批避开 SQLite 的绑定变量上限。

    V0.0.6：图库全选后批量导出 / 重命名，选中数超过变量上限（旧版
    999、新版 32766）时 `IN (?,?,…)` 直接抛 OperationalError，按钮点了
    没反应。这里按 400 个一批查完合并，调用方不用改别的。
    """
    out: list = []
    items = list(ids or [])
    for i in range(0, len(items), 400):
        chunk = items[i:i + 400]
        if not chunk:
            continue
        ph = ",".join("?" * len(chunk))
        try:
            out.extend(list(db.query(
                f"SELECT {columns} FROM images WHERE id IN ({ph})", chunk)))
        except Exception:
            continue          # 单批失败不影响其它批
    return out


def _pages_brief(db, tid: int) -> str:
    """R154-F9：任务行的「已抓 x/y 页」简报（没有页面账本时返回空串）。

    纯函数（只吃 db + task_id，不碰 Qt）：离线可测 —— 判据函数写成
    **模块级**是 R141 的教训（方法在纯 Python 替身上会 AttributeError，
    被替身自己的 except 吞掉后变成假绿）。数字口径 = db.task_page_stats
    （累计，跨运行），与 Web 端 /api/tasks 同一出处，别处不另算。
    """
    try:
        st = db.task_page_stats(tid) or {}
    except Exception:
        return ""
    total = int(st.get("total", 0) or 0)
    if total <= 0:
        return ""
    return f"已抓 {int(st.get('done', 0) or 0)}/{total} 页"


class _ElideLabel(QLabel):
    """超宽自动变「…」的标签：绝不把整行撑爆。

    任务行按钮「消失」的真因：QLabel 的最小宽度=整段文字宽度，任务名是
    长 URL 时 QHBoxLayout 被撑爆，右侧 状态/运行/停止/编辑 被挤出可视区。
    这里把最小宽度钉到 0 + 绘制时手动省略，按钮就永远在。
    """

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setMinimumWidth(0)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):  # noqa: N802
        return QSize(0, super().sizeHint().height())

    def paintEvent(self, ev):  # noqa: N802
        from PySide6.QtGui import QPainter
        p = QPainter(self)
        fm = self.fontMetrics()
        text = fm.elidedText(self.text(), Qt.TextElideMode.ElideRight,
                             max(0, self.width() - 2))
        p.drawText(self.rect(),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   text)
        p.end()


class TaskRow(QWidget):
    """迅雷式任务行：名称 + 忙碌条 + 统计 + 状态 + 运行/停止按钮。"""

    def __init__(self, win, t):
        super().__init__()
        self._win = win
        self._tid = t.id
        # 行内所有文字都必须是透明底：全局主题里有 `QWidget{background:#ffffff}`，
        # 只设 color/font 的 QLabel 会从那里"泄漏"出白底，把选中态的紫色卡片
        # 割成一条条白线（用户反馈的"白线"）。用 ID 选择器把整行 + 所有 QLabel
        # 显式接管成 transparent，特异性高于全局的 QWidget 规则。
        self.setObjectName("task_row")
        self.setStyleSheet(
            "#task_row{background:transparent;}"
            "#task_row QLabel{background:transparent;}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        dot = QLabel()
        _st = t.status.value

        def _repaint_dot():
            dot.setPixmap(win._dot_icon(_st).pixmap(QSize(12, 12)))

        _repaint_dot()
        # ⚠ R128：这颗点的颜色在建行那一刻就烤进 QPixmap 了 —— 换皮肤时
        #   QSS 的 unpolish/polish 刷不动位图，不登记重画的话，用户切到
        #   现代风看到的「已完成」仍是上一套皮肤的紫罗兰（R68 抓到的正是
        #   这个：画面里残留别家强调色 #8b7cf8）。登记后换肤自动重画。
        _register_redraw(dot, _repaint_dot)
        lay.addWidget(dot)

        # 深色模式下原来的深色字在深底上完全看不清 —— 以前是手写两套 hex
        # （`"#e7eaf0" if _dark else "#1f2430"`），P1-4 收口改成走调色板：
        # 深浅两套值都住在 theme.PALETTE 里，这里只表达「要哪一档」。
        # ⚠⚠ R80 项11 修复：这三行原来是 `setStyleSheet(f"...color:{c_name}")`
        #   —— **颜色在建行那一刻被烤死**。而 L2 增量刷新（_refresh_task_list）
        #   按任务指纹决定重不重建行，换皮肤不改指纹 ⇒ 从浅色皮肤切到暗夜风时，
        #   旧行拿着浅色调色板的深字坐在暗底上（实测对比度 1.4，「所有列表
        #   文字看不清」的真凶）。文件头那句「TaskRow 每次 refresh() 都重建」
        #   自 L2 起就不再成立。
        #   修法：改走 theme.style 登记式行内样式（_tstyle）—— 换肤时
        #   `_restyle_registered` 会把这三份模板按新皮肤重渲一遍，
        #   与同行那三个按钮（b_run/b_stop/b_edit 本来就用 _tstyle）一致。
        #   原生皮肤（classic）下 css() 返 "" ⇒ 行内样式整体失效、回系统
        #   调色板，这正是 NATIVE_SKIN_ORDER §3.3 定下的行为。
        #   （_dark / c_name / c_sub / c_st 三个局部量随之删除 —— 留着就是
        #    死代码，后来人会以为颜色还是从那里来的。）

        mid = QVBoxLayout()
        mid.setSpacing(3)
        name = _ElideLabel(t.name)
        _tstyle(name, "font-weight:600; font-size:14px; color:{text};")
        mid.addWidget(name)
        rep = win._queue.reports.get(t.id) if win._queue else None
        if t.status.value == "running":
            sub = "正在抓取…"   # 状态由左侧绿点表示，不再另放进度条/转圈
        elif t.last_error:
            # 需求 24：失败原因写简短清晰的中文（缺 cookie / 站点连不上…），
            # 不再把原始英文异常直接甩给用户。完整异常仍在日志和诊断包里。
            from ..stats import short_reason
            sub = short_reason(t.last_error)
        else:
            sub = (f"上次: 下载{rep['downloaded']} · 页面{rep['pages']}"
                   if rep and rep.get("pages") else
                   f"{len(t.entry_urls)} 个起始网址 · 深度{t.max_depth}")
        sched = getattr(t, "schedule_at", "") or ""
        if sched:
            kind, _, _ = win._parse_schedule(sched)
            label = "每天" if kind == "daily" else "一次性"
            sub = f"⏰{label} {sched} · {sub}"
        # 限速 / 允许时段（#22）：设了才显示，默认不占地方
        extra = []
        if int(getattr(t, "rate_limit", 0) or 0) > 0:
            extra.append(f"限{int(t.rate_limit)}张/分")
        if (getattr(t, "allowed_hours", "") or "").strip():
            extra.append(f"时段{t.allowed_hours.strip()}")
        if extra:
            sub = " · ".join(extra) + " · " + sub
        # R154-F9：页面级断点续采账（有抓过页面才显示，没抓过的任务
        # 不占这一行的宝贵宽度）。与 Web 详情模态同一口径。
        _pb = _pages_brief(getattr(win, "db", None), t.id)
        if _pb:
            sub = f"{sub} · {_pb}"
        sub_lbl = _ElideLabel(sub)
        _tstyle(sub_lbl, "color:{text_dim}; font-size:11px;")
        mid.addWidget(sub_lbl)
        lay.addLayout(mid, 1)

        st = QLabel(STATUS_TXT.get(t.status.value, t.status.value))
        st.setFixedWidth(52)
        st.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(st, "color:{text_faint}; font-size:12px;")
        lay.addWidget(st)

        b_run = QPushButton()
        icons.set_themed_icon(b_run, "play", "{text}", 15)
        b_run.setFixedSize(34, 30)
        b_run.setToolTip("运行")
        b_run.clicked.connect(lambda: win._run_by_id(t.id))
        b_stop = QPushButton()
        icons.set_themed_icon(b_stop, "stop", "{text}", 15)
        b_stop.setFixedSize(34, 30)
        b_stop.setToolTip("停止")
        b_stop.clicked.connect(lambda: win._stop_by_id(t.id))
        b_edit = QPushButton()
        icons.set_themed_icon(b_edit, "edit", "{text}", 15)
        b_edit.setFixedSize(34, 30)
        b_edit.setToolTip("编辑")
        b_edit.clicked.connect(lambda: win._open_task_form(
            get_task(win.db, t.id)))
        for b in (b_run, b_stop, b_edit):
            b.setIconSize(QSize(15, 15))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            # 显式配色：浅灰底+深灰字（全局 QPushButton 白字样式会让
            # 浅底白字"隐形"，必须在这里覆盖 color）
            _tstyle(b,
                    "QPushButton{background:{surface_alt};border:none;"
                    "border-radius:7px;color:{text};font-size:13px;font-weight:600;}"
                    "QPushButton:hover{background:{accent};color:{on_accent};}"
                    "QPushButton:pressed{background:{accent_hover};"
                    "color:{on_accent};}")
            lay.addWidget(b)


class DragArea(QWidget):
    """无边框窗口的拖拽区域（按住移动窗口）。"""

    def __init__(self, win, parent=None):
        super().__init__(parent or win)
        self._win = win
        self._off = None

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._off = (e.globalPosition().toPoint()
                         - self._win.frameGeometry().topLeft())

    def mouseMoveEvent(self, e):
        if self._off is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self._win.move(e.globalPosition().toPoint() - self._off)

    def mouseReleaseEvent(self, e):
        self._off = None

    def mouseDoubleClickEvent(self, e):
        """双击标题栏 = 最大化 / 还原（标准窗口行为，此前漏了所以双击没反应）。

        复用主窗口的 toggle_maximize()，最大化按钮图标/提示会一起同步。
        """
        if e.button() == Qt.MouseButton.LeftButton:
            self._off = None
            try:
                self._win.toggle_maximize()
            except Exception:
                pass
            e.accept()
            return
        super().mouseDoubleClickEvent(e)


class _LearnWorker(QThread):
    """后台跑 learn_folder：用独立 DB 连接避免与主线程 SQLite 连接跨线程冲突。"""
    progress = Signal(int, int, str)
    finished = Signal(object)
    errored = Signal(str)

    def __init__(self, cfg, folder: str, copy_to_library: bool = False,
                 do_tag: bool = True):
        super().__init__()
        self.cfg = cfg
        self.folder = folder
        self.copy_to_library = copy_to_library
        self.do_tag = do_tag

    def run(self):
        from ..db import Database
        from ..learn_folder import learn_folder
        stats = {"added": 0, "skipped_dup": 0, "blacklisted": 0,
                 "errors": 0, "learned": {}}
        db = None
        try:
            db = Database(self.cfg.db_path)
            stats = learn_folder(
                db, self.cfg, self.folder,
                copy_to_library=self.copy_to_library,
                do_tag=self.do_tag,
                progress=lambda d, t, n: self.progress.emit(d, t, n))
        except Exception as e:
            # V0.0.6：以前异常直接穿出 QThread（stats 还是未定义的局部
            # 变量），进度框永远停在「正在学习」，用户连一句错话都看不到。
            stats = {"added": 0, "skipped_dup": 0, "blacklisted": 0,
                     "errors": 1, "learned": {},
                     "ai_note": f"导入失败：{type(e).__name__}: {e}"}
            try:
                self.errored.emit(stats["ai_note"])
            except Exception:
                pass
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
        self.finished.emit(stats)


class _BackfillWorker(QThread):
    """后台回填：独立 DB 连接，边扫边报进度，可被取消，绝不阻塞 GUI。

    回填核心是"把磁盘上已有、但没进库的图片补链接"，默认不做逐图开图 / CLIP
    分类，所以在大目录上也很快；只有用户勾选"重新跑 AI 主题过滤"时才启用重分类。
    """
    progress = Signal(int, int, str)   # scanned, added, current_path
    finished = Signal(int)             # 新增张数
    errored = Signal(str)

    def __init__(self, db_path: Path, re_filter_ai: bool = False,
                 task_id: int | None = None):
        super().__init__()
        self.db_path = Path(db_path)
        self.re_filter_ai = re_filter_ai
        self.task_id = task_id
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def run(self):
        from ..db import Database
        from ..engine import backfill_local_files
        db = Database(self.db_path)
        try:
            n = backfill_local_files(
                db, self.task_id,
                on_progress=lambda s, a, c: self.progress.emit(s, a, c),
                stop=self._stop,
                re_filter_ai=self.re_filter_ai,
            )
            self.finished.emit(n)
        except Exception as e:
            import traceback as _tb
            self.errored.emit(f"{type(e).__name__}: {e}\n{_tb.format_exc()}")
        finally:
            try:
                db.close()
            except Exception:
                pass


class _FillDimWorker(QThread):
    """#86 后台补齐图片分辨率：读每张图真实宽高写进 images 表，修复『无法显示分辨率』，
    并让方向/分辨率筛选生效。只读文件、绝不动磁盘。可被取消。"""
    progress = Signal(int, int, str)   # scanned, updated, current_path
    finished = Signal(int)             # 补齐张数
    errored = Signal(str)

    def __init__(self, db_path: Path):
        super().__init__()
        self.db_path = Path(db_path)
        self._stop = threading.Event()

    def cancel(self):
        self._stop.set()

    def run(self):
        from ..db import Database
        try:
            from PIL import Image
        except Exception:
            self.errored.emit("无法加载 PIL（图像处理库），『补充尺寸』不可用")
            return
        db = Database(self.db_path)
        try:
            rows = db.query(
                "SELECT id, path FROM images WHERE path IS NOT NULL AND path<>'' "
                "AND (width IS NULL OR height IS NULL OR width=0 OR height=0)")
            total = len(rows)
            updated = 0
            for i, r in enumerate(rows):
                if self._stop.is_set():
                    break
                p = Path(r["path"])
                w = h = None
                try:
                    if p.exists():
                        with Image.open(p) as im:
                            w, h = im.size
                except Exception:
                    pass
                if w and h:
                    db.execute(
                        "UPDATE images SET width=?, height=? WHERE id=?",
                        (int(w), int(h), r["id"]))
                    updated += 1
                if i % 50 == 0:
                    self.progress.emit(i, updated, str(p))
            self.finished.emit(updated)
        except Exception as e:
            import traceback as _tb
            self.errored.emit(f"{type(e).__name__}: {e}\n{_tb.format_exc()}")
        finally:
            try:
                db.close()
            except Exception:
                pass


class _CookieWorker(QThread):
    """后台驱动真实浏览器抓取站点 Cookie（CF 挑战/登录态）。

    用 Playwright channel="chrome" 复用系统 Chrome，不下载 Chromium；
    失败（无 playwright/Chrome）由 _on_cookie_failed 回退到手动粘贴。
    """
    done = Signal(str)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            from ..browser_cookie import harvest_cookie
            self.status.emit("浏览器已打开，请在窗口中通过该站验证/登录…")
            header = harvest_cookie(self.url, timeout=180)
            self.done.emit(header)
        except Exception as e:  # noqa: BLE001 - 任何失败都转交 UI 回退
            self.failed.emit(str(e)[:300])


class _GalleryList(QListWidget):
    """图库网格/列表：自定义滚轮步长。

    历史：最早用 Qt 默认步长（太小，像没滚动）→ 改成无脑 ×2 → 触控板的
    pixelDelta 本身就有几百像素，×2 后一下就冲到底（用户反馈）。现在改为：
      * pixelDelta（触控板/高精度滚轮）本身就是像素，按像素走、不再放大；
      * angleDelta 是 1/8 度（一格滚轮=120），换算成"几行"；
      * 单次最多滚 3 行，兜住惯性/大幅滚动，绝不会一下冲到底。
    """

    def wheelEvent(self, e):
        sbv = self.verticalScrollBar()
        sbh = self.horizontalScrollBar()
        pd = e.pixelDelta()
        ad = e.angleDelta()
        row = max(24, self.gridSize().height() or 120)

        def _step(px, deg):
            if px:
                # 触控板/高精度滚轮：本身是像素，单次钳到 3 行防惯性冲底
                limit = row * 3
                return max(-limit, min(limit, int(px)))
            # 普通滚轮一格 = 滚 1 行。之前 ×2 / 3 行都太大，列表模式一滚就到底。
            return int(row * (deg / 120.0))

        if pd.y() or ad.y():
            sbv.setValue(sbv.value() - _step(pd.y(), ad.y()))
        elif pd.x() or ad.x():
            sbh.setValue(sbh.value() - _step(pd.x(), ad.x()))
        e.accept()

    def mousePressEvent(self, e):
        # 右键只弹菜单、绝不动 selection：否则原生处理会把多选折叠成单张，
        # 导致「全选后右键→不喜欢」只作用到 1 张。左键照常（选择/拖拽）。
        if e.button() == Qt.MouseButton.RightButton:
            e.accept()
            self.customContextMenuRequested.emit(e.pos())
            return
        super().mousePressEvent(e)


# ---------------------------------------------------------------------------
# R158-Bug4：隔离区管理的「网格视图」数据结构
# ---------------------------------------------------------------------------
QUAR_GRID_ICON = 128          # 网格缩略图边长（与图库/审核同一档视觉密度）
# 一次最多同步生成多少张缩略图。超出部分先出文件名占位 —— 隔离区可能攒了
# 几千条，全部同步解码会把「打开隔离区」变成几十秒的假死（比没有网格更糟）。
QUAR_GRID_ICON_CAP = 240


def make_quarantine_grid():
    """R158-Bug4：隔离区的网格视图（**模块级** ⇒ 不 exec 对话框也能单测）。

    ⚠ 为什么抽成模块级：隔离区对话框最后 ``dlg.exec()`` 会阻塞，测试**没法**
    真开这个窗口（R131 同型坑）⇒ 断言只能落在可独立调用的东西上。
    ⚠ 必须 ``ExtendedSelection``：QListWidget 默认 SingleSelection，
    「一次勾 50 张批量还原」在这个视图里就又失效了 —— 与表格那边同一个病。
    ⚠ 复用 ``apply_grid_layout``：缩略图尺寸/间距/禁拖拽与图库、审核统一，
    不再出现"同一个软件里三种网格"。
    """
    g = _GalleryList()
    g.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
    apply_grid_layout(g, QUAR_GRID_ICON)
    return g


def quarantine_grid_items(rows, cache_dir=None, icon_size: int = QUAR_GRID_ICON,
                          cap: int = QUAR_GRID_ICON_CAP):
    """隔离区行 → 网格 item 列表（**纯数据进出**，可单测、不需要窗口）。

    ⚠ id 存在 ``Qt.ItemDataRole.UserRole`` —— 与 ``table_view.grid_selected_ids``
    的读数口径一致，批量还原/删除不用关心当前是表格还是网格。

    ⚠ 缩略图走 ``square_thumb_pixmap(..., crop=False)``（等比完整、带磁盘
    缓存），与图库/审核同一口径：裁剪成方块会看不出构图，而隔离区恰恰是
    「要不要留」的场景。
    ⚠ ``cap`` 之外不解码：先占位给文件名，避免首屏假死。磁盘上没有文件的
    记录（隔离区常见）明确留空图标，不假装成功。
    """
    items = []
    size = max(16, int(icon_size or QUAR_GRID_ICON))
    for r in rows or []:
        row = dict(r) if not isinstance(r, dict) else r
        try:
            iid = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        p = Path(row.get("path") or "") if row.get("path") else None
        name = p.name if p else f"#{iid}"
        it = QListWidgetItem(name)
        it.setData(Qt.ItemDataRole.UserRole, iid)
        it.setToolTip(f"#{iid}\n{row.get('source_url') or name}")
        shown = False
        if p is not None and len(items) < int(cap) and p.exists():
            try:
                pm = square_thumb_pixmap(str(p), size, cache_dir=cache_dir,
                                         key=str(p), crop=False)
                if pm is not None and not pm.isNull():
                    it.setIcon(QIcon(pm))
                    shown = True
            except Exception:
                shown = False
        if not shown:
            # 磁盘已缺失 / 超出 cap / 解码失败：文字仍然可读可勾选，
            # 批量清理「磁盘已缺失」时这些就是目标。
            # ⚠ 三种"没图"必须**分开标注**：无路径是数据问题、文件缺失是
            # 磁盘问题（正是「清理磁盘已缺失」要找的目标）、解码失败是格式
            # 问题 —— 混成一句「无图」，用户不知道该点哪个按钮补救。
            if p is None:
                it.setText(f"{name}（无路径）")
            elif not p.exists():
                it.setText(f"{name}（文件缺失）")
        items.append(it)
    return items


class _ThumbSignaler(QObject):
    """原图解码结果回传主线程（跨线程必须是 Queued 连接）。"""
    ready = Signal(object, object)    # token, PNG bytes（解码失败给 None）


class _ThumbDecodeRunnable(QRunnable):
    """后台把**原图**解成缩略图 PNG 字节（图库几百张未缓存图不卡主线程的关键）。

    r37 #141：avif / webp 原图 Pillow 单张 30~80ms，200 张就是十几秒。放worker
    线程里解，主线程只做 QPixmap.loadFromData + setIcon（~1ms），主线程永远
    不被拖住。解码成功后顺手把缓存小图写盘，下次翻同一页直接读缓存。

    【R117 P2-7】``crop`` 默认由 True 改成 **False**（等比缩放、不裁剪）。
    ★ 这正是用户报的「网格缩略图全是方块」的根因：
      同一件事有**两条解码路径**——
        · 同步路径 ``table_view.square_thumb_pixmap(..., crop=False)``：等比完整；
        · 异步路径（**本类**，首次加载/翻页绝大多数图都走这里）：``ImageOps.fit``
          居中裁成正方形。
      结果是「先看到方块、再刷一次变回完整图」，用户看到的绝大多数就是方块。
      现在两条路径口径一致（crop 由调用方显式传，默认不裁）。
      ⚠ ``Image.thumbnail`` 与 ``ImageOps.fit`` 都**原地改** im，
        且 thumbnail 只在**缩小**时生效（原图比 size 还小则原样返回）——
        这正是我们要的「完整显示、不放大糊」。
    """

    def __init__(self, signaler, path: str, token, out_png: str, size: int,
                 crop: bool = False):
        super().__init__()
        self._sig = signaler
        self._path = path
        self._token = token
        self._out_png = out_png
        self._size = int(size)
        self._crop = bool(crop)

    def run(self):
        data = None
        try:
            from PIL import Image, ImageOps
            with Image.open(self._path) as im:
                try:
                    im = ImageOps.exif_transpose(im)
                except Exception:
                    pass
                try:
                    im.seek(0)
                except Exception:
                    pass
                if im.mode in ("P", "LA", "La", "I;16", "I;16B", "I;16L"):
                    im = (im.convert("RGBA") if "A" in (im.mode or "")
                          else im.convert("RGB"))
                elif im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                if self._crop:
                    im = ImageOps.fit(im, (self._size, self._size),
                                      Image.LANCZOS)
                else:
                    # 【R117 P2-7】等比缩到最长边 = size，**不裁剪、不拉伸**
                    im.thumbnail((self._size, self._size), Image.LANCZOS)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                data = buf.getvalue()
            # 顺手写缓存小图（失败无所谓，下次还会再走一次这条路）
            try:
                d = os.path.dirname(self._out_png)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(self._out_png, "wb") as f:
                    f.write(data)
            except Exception:
                pass
        except Exception:
            data = None
        try:
            self._sig.ready.emit(self._token, data)
        except Exception:
            pass


#: R178：顶栏「同时运行任务数」的上限（用户要求能设到 50）。
#: 抽成模块级常量：护栏测试直接读它，避免以后有人改了 UI 却忘了改判据
#: （MEMORY 铁律：数字不许散落在代码里）。
MAX_PARALLEL_TASKS = 50


def format_start_all_message(enqueued: int, limit: int,
                             running_now: int = 0) -> str:
    """R180：「全部开始」的提示文案 —— **如实**反映并发上限。

    原来的文案是「已入队 22 个任务（全部开始）」，而顶栏「同时运行任务数」
    最多 20 ⇒ 用户看到「22 个都开始了」与实际行为矛盾（订单原话）。
    现在把「并发上限 / 立即运行 / 排队」三个数都亮出来。

    ⚠ 写成模块级纯函数（同 `_tray_can_reside` 的理由）：GUI 替身测不到
    方法，且文案这种东西本来就该能脱离窗口单测。
    """
    try:
        e = max(0, int(enqueued or 0))
    except Exception:
        e = 0
    try:
        lim = max(1, int(limit or 1))
    except Exception:
        lim = 1
    try:
        run = max(0, int(running_now or 0))
    except Exception:
        run = 0
    if e <= 0:
        return "没有可运行的任务（可能都在运行中，或列表为空）"
    slots = max(0, lim - run)
    will_run = min(e, slots) if slots else 0
    waiting = e - will_run
    base = f"已加入队列 {e} 个，并发上限 {lim}，当前运行 {run + will_run}"
    if waiting <= 0:
        return base + "（全部立即开始）"
    return base + f"，排队 {waiting}（受「同时运行任务数」限制，前序任务结束后自动进入）"


def _tray_can_reside(obj) -> bool:
    """R138：能不能「缩到托盘常驻」（托盘在 + 没被要求真退）。

    写成**模块级函数**而不是 MainWindow 的方法：R117 的关闭测试用的是纯
    Python 替身（不跑 __init__），方法在替身上不存在会 AttributeError。
    对任何对象都安全（缺属性就当没有托盘）⇒ 老测试的收尾路径不受影响。
    """
    try:
        if getattr(obj, "_explicit_quit", False):
            return False
        return getattr(obj, "tray", None) is not None
    except Exception:
        return False


def _tray_hide_on_close(obj, event=None) -> bool:
    """R138：接管一次「关闭」——能缩托盘就缩，不真退。

    返回 True = 这次关闭已被接管（closeEvent 直接返回，不做收尾）；
    返回 False = 走原来的四层安全收尾。

    同样是模块级（理由同 `_tray_can_reside`）：QMainWindow 子类替身 /
    纯 Python 替身身上没有 MainWindow 的方法，直接 `self.xxx()` 会
    AttributeError，被 except 吞掉后行为就变成「既没缩托盘也没收尾」。
    """
    if not _tray_can_reside(obj):
        return False
    try:
        if event is not None:
            event.ignore()          # 不接受这次关闭 ⇒ 窗口不会被销毁
    except Exception:
        pass
    try:
        obj._hide_to_tray()
    except Exception:
        try:
            obj.hide()
        except Exception:
            pass
    return True


class MainWindow(QMainWindow):
    log_signal = Signal(str)          # safe to emit from worker threads
    popup_signal = Signal(str, str)   # title, body (from worker threads)
    # r39：后台（线程池）任务的结果回主线程再动 Qt 用。回调里要建对话框/刷表格，
    # 直接在工作线程里跑会被 Qt 拒（"Cannot create a QWidget outside the GUI
    # thread"）—— 以前 find_similar 的 SSIM 就是同步堵死主线程 295s，改成后台
    # 之后必须再通过信号 ev 回来，不能让回调继续待在工作线程。
    bg_result = Signal(object)        # payload = (cb, result, error)
    # queue events are emitted from the worker thread but the SLOT runs on the
    # GUI thread (Qt signal cross-thread delivery) -> safe to touch widgets.
    queue_event_signal = Signal(str, object, object)
    # R154-F20：Web 扫码登录的授权请求（token, ip）。由 HTTP 线程 emit，
    # 槽在主线程弹确认框（跨线程安全，不阻塞 HTTP 服务）。
    qr_login_requested = Signal(str, str)
    # r39f：refresh_stats 被每个队列事件驱动，不节流的话一次抓取能把主线程
    # 反复拽进 library_stats。这里是最小重算间隔（秒）。
    STATS_MIN_INTERVAL = 3.0

    #: R167：「隔离区」导航项。V0.0.9：学习看板升级成首页（stack 索引 0），
    #: 原任务/审核/图库/隔离区整体后移一位 → 任务1/审核2/图库3/隔离区4。
    #: R181：隔离区**已是 stack 里的一页**（主窗口内嵌面板，与图库、
    #: 审核同级）—— R167 时代的「动作项/模态弹窗」语义作废。
    #: ⚠ 单独抽成常量而不是散落字面量：平铺导航、分组导航、_nav_go 三处
    #:   必须指同一个索引，写散了一改就漏。
    _NAV_QUARANTINE_IX = 4
    #: 导航项索引 → `_nav_badge_counts()` 的键（顺序必须对齐）。
    #: 索引 0「首页」是 V0.0.9 的学习看板页，不挂数量角标（见 _refresh_nav_badges）。
    _NAV_KEY_BY_IX = {0: "home", 1: "tasks", 2: "review", 3: "gallery",
                     4: "quarantine"}

    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        # R149-B：用 cfg 校准全局「学习」开关（默认开；老配置无键=兼容开）。
        from ..classify.preference import set_learning_enabled
        set_learning_enabled(bool(getattr(self.cfg, "learn_enabled", True)))
        # R149-A：用 cfg 校准下载代理（空=走系统/环境变量代理或直连）。
        from ..models_lib import set_download_proxy
        set_download_proxy(getattr(self.cfg, "proxy", None))
        # 日志：写入文件 + 推送到界面「日志」面板（节流在 logutil 内处理）
        setup_logging(self.cfg.log_dir,
                      sink=lambda m: self.log_signal.emit(m),
                      sanitize=bool(getattr(self.cfg, "log_sanitize", True)))
        # R161：登记生效配置；加密开启且内存无主密码 -> 启动界面先要主密码
        from ..config import (set_active_config, set_db_master_password,
                              get_db_master_password)
        set_active_config(self.cfg)
        if getattr(self.cfg, "encryption_enabled", False) and not get_db_master_password():
            _pw = self._prompt_db_master_password()
            if _pw is None:
                self.cfg.encryption_enabled = False  # 用户取消 -> 降级明文，免得起不来
            else:
                set_db_master_password(_pw)
        self.db = Database(self.cfg.db_path)
        self.pool = ProxyPool(self.db)
        # R169 第 3 条：学习数据维护（碎片词清扫 + 时间衰减，每天一次）。
        # 全程兜异常 —— 学习数据坏了是小事，软件起不来是大事。
        try:
            from ..repo import run_learning_maintenance
            run_learning_maintenance(self.db)
        except Exception:
            pass
        self._dot_cache = {}
        self.log_signal.connect(self.log_append)
        self.popup_signal.connect(self._show_popup)
        self.queue_event_signal.connect(self._on_queue_event)
        # r39：后台任务结果回主线程。
        # **必须显式 QueuedConnection**：PySide6 把信号连到 Python callable 时
        # 默认是直连（它分不清 receiver 属于哪个线程），从工作线程 emit 会直接
        # 在工作线程里跑回调 —— 那回调里建对话框/刷表格就会
        # "Cannot create a QWidget outside the GUI thread"。
        self.bg_result.connect(self._on_bg_result,
                               Qt.ConnectionType.QueuedConnection)
        from .. import VERSION as _VER
        self.setWindowTitle(f"Sweet采集AI版 · V{_VER}")
        # 去掉系统标题栏（最小化/关闭那行），用自绘拖拽条（右上角 最小化 和 关闭 按钮）
        # 无边框自绘标题栏，但必须带上 最小化/最大化/关闭 系统标志——
        # 否则 Windows 把窗口当成“无边框工具窗口”，showMinimized / showNormal
        # 会失效（启动最大化后既无法最小化、也无法还原成默认大小）。
        # 改回 Windows 原生标题栏：系统自带 左上角应用图标 / 右键标题栏的
        # 系统菜单 / 右上角 最小化·最大化·关闭 / 四边四角拖拽缩放，以及
        # 真正满屏的最大化。不再用无边框自绘——那套要自己搓 WM_NCHITTEST 做
        # 缩放，而且无边框窗口的 showMaximized() 在 Windows 上经常不真正满屏
        # （就是「窗口比原来窄一截」那个真回归）。
        self.setWindowFlags(Qt.WindowType.Window)
        ic = icon_path()
        if ic.exists():
            self.setWindowIcon(QIcon(str(ic)))
        self._queue = None
        # R138：托盘对象（launch() 里挂上）。为 None 时「关闭」= 真退出，
        # 有托盘时「关闭」默认缩到托盘常驻。
        self.tray = None
        # R138：只有用户**明确**选了退出（托盘「退出」/ 设置菜单「完全退出」）
        # 才置 True，此时 closeEvent 走原来的四层安全收尾并真退。
        # R155-A：窗口**最小化**不再置任何藏托盘行为——最小化=缩任务栏。
        self._explicit_quit = False
        # r37 #141：图库缩略图后台解码线程池。
        # 离屏真机取证（916 张真图）：原图 avif 单张解码 30~80ms，200 张就是
        # 十几秒全压在主线程 → 「900 张卡的不行」。现在解原来是纯 CPU 活，丢
        # 进这 4 个 worker，主线程只做 loadFromData+setIcon。
        try:
            self._thumb_sig = _ThumbSignaler()
            self._thumb_sig.ready.connect(self._on_thumb_ready)
            # r39c：**别把 QThreadPool 挂在主窗口下**（原来写 QThreadPool(self)）。
            # 主窗口一销毁就会连池子一起销毁，而池里的解码线程此刻还在解图
            # （avif 单张 30~80ms），线程往一个已经没了归属的池里回结果 →
            # 退出码 0xC0000409、Python 层一个字都不留。改成不带 parent 的裸池，
            # 由 closeEvent 里的 waitForDone 负责收干净。
            self._thumb_pool = QThreadPool()
            self._thumb_pool.setMaxThreadCount(self._THUMB_WORKER_N)
            self._thumb_slots = {}          # token -> (item, mkey, path, sha)
            self._thumb_tokens = iter(range(10 ** 9))
            self._thumb_retry = set()       # 已重试过一次的坏图 item id（防死循环）
        except Exception:
            self._thumb_sig = None
            self._thumb_pool = None
            self._thumb_slots = None
            self._thumb_tokens = iter(range(10 ** 9))
        self._build_ui()
        self._build_queue()
        self.refresh()
        self._daily_backup()
        self.check_missing_on_start()

    def log_append(self, text: str):
        if text == "__ENABLE_RUN__":
            try:
                self.act_run.setEnabled(True)
            except RuntimeError:
                pass
            return
        # r40：日志控件会随「日志统计」页被释放（需求 4/5 把两页合并成
        # QTabWidget 里的一页，sweep 或切页时整棵子树会重建）。日志槽还挂在
        # 信号上，被删之后再 append 就是
        #   RuntimeError: libshiboken: ... QTextEdit already deleted
        # 真机第 3 轮扫描实测刷了 216 条。日志只是显示，死了就跳过，
        # 完整记录仍在日志文件里，绝不能因为它掀翻调用方。
        import shiboken6
        lv = getattr(self, "log_view", None)
        if lv is None or not shiboken6.isValid(lv):
            return
        try:
            lv.append(text)
            # 限制行数，避免长时间运行把日志面板撑爆（完整记录仍在日志文件里）
            doc = lv.document()
            if doc.blockCount() > 2000:
                cur = lv.textCursor()
                cur.movePosition(QTextCursor.MoveOperation.Start)
                cur.movePosition(QTextCursor.MoveOperation.Down,
                                 QTextCursor.MoveMode.KeepAnchor,
                                 doc.blockCount() - 1500)
                cur.removeSelectedText()
            # 自动滚到最新，避免还要手动拖滚动条
            sb = lv.verticalScrollBar()
            sb.setValue(sb.maximum())
        except RuntimeError:
            pass

    def log(self, msg: str):
        """写一行日志（r39）。

        **属性遮蔽坑**：日志面板控件原来起名就叫 ``self.log``，于是
        ``self.log("…")`` 这个调用在运行时是「拿 QTextEdit 实例当函数调用」→
        ``TypeError: 'QTextEdit' object is not callable``（真机扫描在
        「键盘审核时弹选词框」和「删除常用词」两个动作上抓到）。
        现在控件改名 ``self.log_view``，这里补上真正的 ``log()`` 方法。
        """
        try:
            self.log_signal.emit(str(msg))
        except Exception:
            pass

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        # ---------------- MENU BAR (所有动作都在菜单里，不再占一排工具栏) ----
        self.act_new = QAction("新增任务", self)
        icons.set_themed_icon(self.act_new, "plus", "{text_dim}", 16)
        self.act_new.triggered.connect(self.add_task)
        # 【修复】「保存修改」这个动作当年被拿掉了（会拿旧值覆盖用户改完的内容，有害），
        # 但对象还留在代码里没清理 → 死对象。真正的保存只有任务表单自己的「保存」按钮。
        # 此处不再创建 act_save。
        # 「编辑任务」：右键任务行也要能改（用户要求），和工具栏的「编辑」同源
        self.act_edit = QAction("编辑任务", self)
        icons.set_themed_icon(self.act_edit, "edit", "{text_dim}", 16)
        self.act_edit.setToolTip("打开选中任务的编辑窗口（双击任务行同样可以）")
        self.act_edit.triggered.connect(self._edit_selected)
        self.act_run = QAction("运行", self)
        icons.set_themed_icon(self.act_run, "play", "{text_dim}", 16)
        self.act_run.setToolTip("运行选中的任务 (双击任务也可)")
        self.act_run.triggered.connect(self.run_selected)
        self.act_stop = QAction("停止", self)
        icons.set_themed_icon(self.act_stop, "stop", "{text_dim}", 16)
        self.act_stop.setToolTip("停止选中的正在运行的任务（暂停只挡新任务，停止才是掐掉正在跑的）")
        self.act_stop.triggered.connect(self.stop_selected)
        self.act_pause = QAction("暂停/继续", self)
        icons.set_themed_icon(self.act_pause, "pause", "{text_dim}", 16)
        self.act_pause.setToolTip("暂停/继续队列（不影响正在运行的任务）")
        self.act_pause.triggered.connect(self.toggle_pause)
        self.act_refresh = QAction("刷新", self)
        icons.set_themed_icon(self.act_refresh, "refresh", "{text_dim}", 16)
        self.act_refresh.triggered.connect(self.refresh)
        self._refresh_autostart_label()
        # 2026-10-27（编号 13）：配置里开着监视文件夹就自启（无路径时静默跳过，
        # 不弹框打扰启动流程 —— 用户可能只是上次配了路径后来把盘拔了）。
        try:
            if bool(getattr(self.cfg, "watch_enabled", False)) and \
                    str(getattr(self.cfg, "watch_dir", "") or "").strip():
                self._start_watch_folder()
        except Exception:
            pass

        # ---- #R117-P1-1 「全部开始 / 全部停止」QAction 在此**提前**创建 ----
        # 为什么必须提前：R117 P1-1 要求把这两个入口补进「任务」菜单，而
        # 「任务」菜单（m_task）在下面先构建、「运行」菜单（m_run）后构建。
        # 原本它们是在 m_run 段里 new 的 —— 顺序上晚于 m_task，m_task 里就
        # 引用不到。同一时刻把它们挂进两个菜单是 Qt 允许的（QAction 可属于
        # 多个 QMenu），所以这里只负责**建 + 连槽**，两个菜单各自 addAction。
        self.act_run_all = QAction("全部开始", self)
        icons.set_themed_icon(self.act_run_all, "play", "{text_dim}", 16)
        self.act_run_all.setToolTip(
            "把列表里所有未运行的任务一次性入队（同时运行数量受顶栏"
            "「同时运行任务数」限制）")
        self.act_run_all.triggered.connect(self.run_all)
        self.act_stop_all = QAction("全部停止", self)
        icons.set_themed_icon(self.act_stop_all, "stop", "{text_dim}", 16)
        self.act_stop_all.setToolTip("停止当前所有正在运行的任务")
        self.act_stop_all.triggered.connect(self.stop_all)

        mb = self.menuBar()
        # 【V0.0.19 推倒重排】用户拍板：8 个顶级菜单收成
        # 任务 / 工具 / 视图 / 设置 / 帮助 5 个；页面类入口走左侧导航。
        # 「新手/老鸟」双模式控制顶级菜单显隐：新手藏「工具」「视图」。
        # ⚠ 顶级菜单一律不带图标（PySide6 6.11 fusion 带图标只画图标不画字）。

        # ---------- 1) 任务：建 / 改 / 删 / 批量 / 启停 ----------
        m_task = mb.addMenu("任务")
        m_task.addSection("任务管理")
        m_task.addAction(self.act_new)
        m_task.addAction(self.act_edit)
        self.act_del = QAction("删除选中任务", self)
        icons.set_themed_icon(self.act_del, "trash", "{text_dim}", 16)
        self.act_del.triggered.connect(self.delete_selected)
        m_task.addAction(self.act_del)
        self.act_batch = QAction("批量导入任务…", self)
        icons.set_themed_icon(self.act_batch, "plus", "{text_dim}", 16)
        self.act_batch.setToolTip(
            "每行粘贴一个网址，一次建多个任务（其余用默认配置）")
        self.act_batch.triggered.connect(self.batch_import_tasks)
        m_task.addAction(self.act_batch)
        self.act_batch_export = QAction("批量导出任务…", self)
        self.act_batch_export.setToolTip(
            "把任务配置 + 进度统计（已下载 N / 已丢弃 N / 状态）导出成一个 JSON，"
            "换机器或备份时用；配合「工具 → 日志统计 → 导入任务…」可以还原。")
        self.act_batch_export.triggered.connect(self.batch_export_tasks)
        m_task.addAction(self.act_batch_export)
        # 2026-10-27（编号 10）：快速添加 —— 从剪贴板取网址直接建任务。
        self.act_quick_add = QAction("快速添加（剪贴板网址）", self)
        self.act_quick_add.setToolTip(
            "从剪贴板读取一行或多行网址，用默认配置直接建任务。\n"
            "Shift 点击 = 建完立即开始。")
        self.act_quick_add.triggered.connect(
            lambda: self.quick_add(clipboard=True))
        m_task.addAction(self.act_quick_add)
        m_task.addSeparator()
        m_task.addSection("执行")
        # act_run/act_stop 只作用于选中行；act_run_all/act_stop_all 全局
        # （R117 P1-1：两个 QAction 在菜单构建之前创建，此处 addAction）。
        m_task.addAction(self.act_run)
        m_task.addAction(self.act_stop)
        m_task.addAction(self.act_run_all)
        m_task.addAction(self.act_stop_all)
        m_task.addAction(self.act_pause)
        m_task.addAction(self.act_refresh)
        self.act_retry = QAction("重试失败任务", self)
        icons.set_themed_icon(self.act_retry, "refresh", "{text_dim}", 16)
        self.act_retry.setToolTip("将上次失败或中断的任务重新加入队列并重新执行")
        self.act_retry.triggered.connect(self.retry_failed)
        m_task.addAction(self.act_retry)
        m_task.addSeparator()
        m_task.addSection("网址与规则")
        self.act_mgr = QAction("网址与适配器管理器…", self)
        self.act_mgr.triggered.connect(self.url_adapter_manager)
        m_task.addAction(self.act_mgr)

        # ---------- 2) 工具（老鸟专属）：站点 / AI / 图库 / 数据 / 诊断 ----------
        m_tools = mb.addMenu("工具")

        # ---------- 工具 → 采集与站点 ----------
        m_site = m_tools.addMenu("采集与站点")
        m_site.addSection("站点与规则")
        m_site.addAction("站点订阅管理…", self.show_subs_dialog)
        m_site.addAction("站点 Cookie 管理…", self.show_cookie_dialog)
        m_site.addAction("使用在线 AI 生成站点规则…", self.open_ai_adapter_dialog)
        m_site.addSeparator()
        m_site.addSection("网络与代理")
        m_site.addAction("浏览器插件（取 Cookie/Token）…",
                         self.open_extension_folder)
        m_site.addAction("代理健康检查", self.proxy_health_check)
        m_site.addAction("代理管理…", self.proxy_dialog)
        m_site.addAction("手机扫码登录…", self.show_qrlogin)

        # ---------- 工具 → AI 与学习 ----------
        m_ai = m_tools.addMenu("AI 与学习")
        self.act_blur = QAction("敏感图入库前模糊", self)
        self.act_blur.setCheckable(True)
        self.act_blur.setChecked(bool(getattr(self.cfg, "blur_sensitive", False)))
        self.act_blur.setToolTip(
            "开启后，命中敏感词（人脸 / 身份证 / 水印 等）的图会先在本地做\n"
            "马赛克处理，图库只显示模糊版；原图保留在磁盘但不在图库出现。\n"
            "全程本地处理，不上传任何图片。")
        self.act_blur.triggered.connect(self.toggle_blur)
        m_ai.addAction(self.act_blur)
        m_ai.addAction("敏感词设置…", self.edit_sensitive_words)
        self.act_ai = QAction("AI 模型设置…", self)
        self.act_ai.triggered.connect(self.ai_model_dialog)
        m_ai.addAction(self.act_ai)
        m_ai.addAction("AI 推理线程数…", self.edit_ai_threads)
        self.act_ai_gpu = QAction("AI 用 GPU 加速（无显卡时自动回退 CPU）", self)
        self.act_ai_gpu.setCheckable(True)
        self.act_ai_gpu.setChecked(bool(getattr(self.cfg, "ai_gpu", True)))
        self.act_ai_gpu.setToolTip(
            "开着：建推理会话时优先 CUDA（N 卡）/ DirectML（其它显卡），"
            "建不成自动回退 CPU，AI 打分快很多。\n"
            "关掉：强制纯 CPU——显卡驱动抽风、显存被别的程序占满、"
            "或一开 GPU 推理界面就卡时用（下次运行任务生效）。")
        self.act_ai_gpu.triggered.connect(self.toggle_ai_gpu)
        m_ai.addAction(self.act_ai_gpu)
        sub_dev = m_ai.addMenu("推理设备")
        self._dev_actions = {}
        for _key, _label, _tip in (
                ("auto", "自动（有显卡就用显卡）", "默认：有 CUDA/DirectML 就走显卡"),
                ("gpu", "强制用显卡", "CPU 要腾出来干别的时选它；没有显卡后端会明确提示"),
                ("npu", "强制用 NPU（高通 QNN / AMD Ryzen AI / 英特尔 OpenVINO）",
                 "适用于高通 QNN、AMD Ryzen AI、英特尔 OpenVINO 等 NPU 后端；"
                 "运行时缺失时会明确提示"),
                ("cpu", "强制用 CPU", "显卡驱动异常 / 显存被占 / 想让显卡干别的时选它")):
            a = sub_dev.addAction(_label)
            a.setCheckable(True)
            a.setChecked(str(getattr(self.cfg, "ai_device", "auto") or "auto") == _key)
            a.setToolTip(_tip)
            a.triggered.connect(lambda _=False, k=_key: self.set_ai_device(k))
            self._dev_actions[_key] = a
        m_ai.addAction("学习看板…", self.show_learning_dashboard)
        m_ai.addAction("从文件夹学习…（导入本地图库）", self.learn_from_folder)
        a_learn = QAction("学习（AI 偏好）", self, checkable=True)
        a_learn.setChecked(bool(getattr(self.cfg, "learn_enabled", True)))
        a_learn.setToolTip("关闭后：逐张「喜欢 / 不喜欢」不再记录偏好权重，"
                           "审核、删除、隔离不受影响。\n"
                           "导入本地图库自动学到的偏好词、以及判退图片学到的回避词不受该开关影响。")
        a_learn.toggled.connect(self._on_learn_toggled)
        m_ai.addAction(a_learn)
        self._act_learn = a_learn

        # ---------- 工具 → 图库与隔离 ----------
        m_lib = m_tools.addMenu("图库与隔离")
        m_lib.addAction("批量重命名（加前缀）…", self.batch_rename_selected)
        m_lib.addAction("按模板重命名…", self.gallery_rename_by_template)
        m_lib.addAction("查找类似图片…", self.gallery_find_similar_images)
        self.act_recycle = QAction("删除时先进隔离区", self)
        self.act_recycle.setCheckable(True)
        self.act_recycle.setChecked(bool(getattr(self.cfg, "use_recycle", True)))
        self.act_recycle.setToolTip(
            "勾选：删除的图进隔离区（软件内，可还原）\n"
            "取消：直接从磁盘永久删除，不可恢复")
        self.act_recycle.triggered.connect(self.toggle_recycle)
        m_lib.addAction(self.act_recycle)
        self.act_qmgr = QAction("隔离区管理…", self)
        self.act_qmgr.setToolTip("查看被 AI / 黑名单隔离的图片：清理磁盘已缺失的记录、"
                                 "还原到图库、或彻底清空")
        self.act_qmgr.triggered.connect(self.quarantine_manager)
        m_lib.addAction(self.act_qmgr)
        # 【破坏性】下面真删文件，跟上面的查看/开关用分隔线隔开
        m_lib.addSeparator()
        m_lib.addSection("危险操作（真删文件）")
        self.act_prune = QAction("清空隔离区…", self)
        self.act_prune.setToolTip(
            "物理删除**软件隔离区**里全部隔离图（无收藏/未拉黑/未待审核的），"
            "释放磁盘。与 Windows 系统回收站无关。\n"
            "不限时间，可随时手工执行；软件不会自动删除任何隔离图片")
        self.act_prune.triggered.connect(self.prune_recycle)
        m_lib.addAction(self.act_prune)
        self.act_full_cleanup = QAction("全面清理（重置到新装状态）", self)
        self.act_full_cleanup.setToolTip(
            "按勾选清单清空：默认只清任务 / 已落库图 / 待审核 / 隔离区 / 审核记录 / "
            "缩略图 / 备份 / 日志；适配器、AI 模型、学习记录、设置、登录 Cookie 默认不勾，"
            "想连它们一起清就在窗口里勾上（标「真删硬盘」的勾上＝文件直接从硬盘抹掉）。")
        self.act_full_cleanup.triggered.connect(self.full_cleanup)
        m_lib.addAction(self.act_full_cleanup)

        # ---------- 工具 → 数据与备份 ----------
        m_data = m_tools.addMenu("数据与备份")
        m_data.addSection("备份与迁移")
        self.act_backup = QAction("每日自动备份数据库", self)
        self.act_backup.setCheckable(True)
        self.act_backup.setChecked(bool(getattr(self.cfg, "daily_backup", True)))
        self.act_backup.setToolTip("每天首次启动备份一次，只保留最近 7 份")
        self.act_backup.triggered.connect(self.toggle_backup)
        m_data.addAction(self.act_backup)
        self.act_backfill = QAction("回填本地图片到图库", self)
        icons.set_themed_icon(self.act_backfill, "refresh", "{text_dim}", 16)
        self.act_backfill.setToolTip("扫描各任务的保存目录，把磁盘上已有、但没进数据库的"
                                     "图片补写进图库（恢复「磁盘有图、界面为空」的情况）")
        self.act_backfill.triggered.connect(self.backfill_local)
        m_data.addAction(self.act_backfill)
        m_data.addAction("导出偏好（换电脑用）", self.export_preferences)
        m_data.addAction("导入偏好", self.import_preferences)
        m_data.addAction("导出完整备份（配置+库+规则+缩略图）…", self.export_backup)
        m_data.addAction("从完整备份恢复…", self.import_backup)
        m_data.addAction("备份同步到目录（现在）", self.backup_sync_now)
        m_data.addAction("设置备份同步目录…", self.edit_backup_sync_dir)
        m_data.addAction("完整备份管理…", self.show_fullbackup_manager)
        m_data.addSeparator()
        m_data.addSection("导出与交付")
        m_data.addAction("导出图库索引（加密）…", self.export_gallery_index)
        m_data.addAction("合格线自动校准…", self.show_threshold_tune)
        m_data.addAction("导出训练素材集…", self.export_training_kit)
        m_data.addAction("数据存放位置…", self.show_data_location)

        # ---------- 工具 → 统计与看板 ----------
        m_stat = m_tools.addMenu("统计与看板")
        m_stat.addAction("站点采集统计…", self.show_site_stats)
        m_stat.addAction("通知中心…", self.show_notifications)
        self.act_report = QAction("采集周报 / 月报…", self)
        self.act_report.setToolTip("按周或按月统计采集张数、通过率、不喜欢 Top 词与任务排行，可导出 Markdown / CSV")
        self.act_report.triggered.connect(self.show_collection_report)
        m_stat.addAction(self.act_report)
        self.act_diskspace = QAction("磁盘空间看板…", self)
        self.act_diskspace.setToolTip(
            "看图库/待审核/隔离区/缩略图/模型各占多少、近 7 日增长趋势；"
            "剩余空间低于「磁盘剩余空间预警」值时中文提醒")
        self.act_diskspace.triggered.connect(self.show_disk_space)
        m_stat.addAction(self.act_diskspace)

        # ---------- 工具 → 检查与修复 ----------
        m_fix = m_tools.addMenu("检查与修复")
        # 【顺序纪律】先检查后动手：只读的「检查与预警」必须排在
        # 会改文件的「修复与整理」之前（verify_r11 3c 钉着这条）。
        m_fix.addSection("检查与预警（只读）")
        m_fix.addAction("文件完整性检查…", self.integrity_dialog)
        m_fix.addAction("本机配置…", self.show_sysinfo)
        m_fix.addAction("磁盘剩余空间预警…", self.edit_disk_threshold)
        m_fix.addAction("导出诊断包…", self.export_diagnostic_bundle)
        m_fix.addAction("查看日志文件夹", self.open_log_folder)
        # 【只读 → 破坏性】分隔线隔开下面会动文件的操作
        m_fix.addSeparator()
        m_fix.addSection("修复与整理（会改动文件）")
        self.act_repair = QAction("修复图库（恢复误隔离的图）", self)
        self.act_repair.setToolTip(
            "重新用当前模型给被隔离的真图打分，达标的恢复进图库；"
            "删除无文件的垃圾记录；清理磁盘 0kb/.part 残留。")
        self.act_repair.triggered.connect(self.repair_gallery)
        m_fix.addAction(self.act_repair)
        self.act_clean0 = QAction("清理 0kb/残留文件", self)
        self.act_clean0.setToolTip("删除图片目录里的 0 字节图片与 .part 孤儿文件")
        self.act_clean0.triggered.connect(self.clean_orphan_files)
        m_fix.addAction(self.act_clean0)
        # r37：老图转 JPG（设置里那个开关只管新图，历史库得单独跑一次）
        self.act_convert_old = QAction("把已有图片转成 JPG…", self)
        self.act_convert_old.setToolTip(
            "把图库里已有的 webp / avif / png 等转成 .jpg。\n"
            "转换前先自动备份被替换的文件到 thumbs/converted_backup/，出问题能还原。\n"
            "只转当前图库记录指向的文件，磁盘上多出来的孤儿文件不动。")
        self.act_convert_old.triggered.connect(self.convert_existing_to_jpg)
        m_fix.addAction(self.act_convert_old)

        # ---------- 工具 → 日志统计（test_r45 钉：必须是工具的直接子菜单，
        # 且内部动作严格为 清空日志/导出日志/刷新统计/导出报告/导入任务 五项）----------
        m_log = m_tools.addMenu("日志统计")
        self.act_clear_log = QAction("清空日志", self)
        self.act_clear_log.setToolTip("清空界面上的日志面板（不动磁盘日志文件）")
        # 走方法而不是直接连 self.log_view.clear：菜单比日志页先建，
        # 建菜单那一刻 self.log_view 还不存在，直接取属性会 AttributeError。
        self.act_clear_log.triggered.connect(self._clear_log_view)
        m_log.addAction(self.act_clear_log)
        self.act_export_log = QAction("导出日志…", self)
        self.act_export_log.triggered.connect(self.export_log)
        m_log.addAction(self.act_export_log)
        self.act_refresh_stats = QAction("刷新统计", self)
        self.act_refresh_stats.setToolTip("立刻重算一遍统计（不等节流窗口）")
        # force=True 不能少：refresh_stats 有节流，漏了 force 点菜单会被挡掉、看不到新数
        self.act_refresh_stats.triggered.connect(
            lambda: self.refresh_stats(force=True))
        m_log.addAction(self.act_refresh_stats)
        self.act_export_report = QAction("导出报告…", self)
        self.act_export_report.triggered.connect(self.export_report)
        m_log.addAction(self.act_export_report)
        m_log.addSeparator()
        self.act_import_tasks = QAction("导入任务…", self)
        self.act_import_tasks.setToolTip("从之前导出的任务文件把任务批量导回来")
        self.act_import_tasks.triggered.connect(self.import_tasks)
        m_log.addAction(self.act_import_tasks)

        # ---------- 3) 视图（老鸟专属）：页面跳转 / 检索 / 模式 / 视觉 ----------
        m_view = mb.addMenu("视图")
        m_view.addSection("页面")
        self._view_page_actions = []
        for _ix, _label in ((0, "首页"), (1, "任务"), (2, "审核"),
                            (3, "图库"), (4, "隔离区")):
            _a = QAction(_label, self)
            _a.setCheckable(True)
            _a.triggered.connect(lambda _c=False, x=_ix: self._nav_go(x))
            m_view.addAction(_a)
            self._view_page_actions.append(_a)
        m_view.addSeparator()
        m_view.addSection("检索")
        m_view.addAction("全局搜索…", self.show_search_all)
        m_view.addAction("学习看板…", self.show_learning_dashboard)
        m_view.addSeparator()
        m_view.addSection("界面模式")
        # V0.0.19 双模式：新手/老鸟。持久化 cfg.ui_mode，切换即时生效。
        m_uimode = m_view.addMenu("界面模式")
        self._uimode_actions = {}
        for _mk, _ml, _mtip in (
                ("newbie", "新手模式（精简）",
                 "只保留最常用的入口：任务、设置、帮助；"
                 "新建任务只显示网址、保存目录、代理三项。"),
                ("pro", "老鸟模式（全部）",
                 "全部菜单、全部设置项与高级选项展开。")):
            _a = QAction(_ml, self)
            _a.setCheckable(True)
            _a.setToolTip(_mtip)
            _a.triggered.connect(lambda _c=False, k=_mk: self.set_ui_mode(k))
            m_uimode.addAction(_a)
            self._uimode_actions[_mk] = _a
        self._refresh_uimode_checks()
        m_view.addSeparator()
        m_view.addSection("现代化视觉")
        # V0.0.19 毛玻璃：Win11 系统级 Acrylic/Mica 背板（gui/acrylic.py）。
        self.act_glass = QAction("毛玻璃效果（跟随系统）", self)
        self.act_glass.setCheckable(True)
        self.act_glass.setChecked(bool(getattr(self.cfg, "glass_enabled", False)))
        self.act_glass.setToolTip(
            "启用窗口毛玻璃（Windows 11 为 Acrylic/Mica 系统背板，\n"
            "Windows 10 为半透明近似；远程桌面或老显卡驱动可能不支持，\n"
            "此时自动保持普通不透明外观，不影响使用）。")
        self.act_glass.triggered.connect(self.toggle_glass)
        m_view.addAction(self.act_glass)

        # ---------- 4) 设置：系统集成 + 通用偏好 ----------
        m_set = mb.addMenu("设置")
        self.act_settings_center = QAction("设置中心…", self)
        self.act_settings_center.setToolTip(
            "按八个分区集中查看与修改常用设置，支持搜索设置项；"
            "原有的独立对话框也可从这里逐个打开。")
        self.act_settings_center.triggered.connect(self.show_settings_center)
        m_set.addAction(self.act_settings_center)
        m_set.addSection("外观")
        from . import theme
        m_skin = m_set.addMenu("皮肤")
        self._skin_actions = {}
        for _pk in theme.PRESETS:
            _a = QAction(theme.preset_label(_pk), self)
            _a.setCheckable(True)
            _a.triggered.connect(lambda _c, k=_pk: self.set_skin(k))
            m_skin.addAction(_a)
            self._skin_actions[_pk] = _a
        _customs = theme.list_custom_skins()
        if _customs:
            m_skin.addSeparator()
            for _ck, _cl in _customs:
                _a = QAction(theme.preset_label(_ck), self)
                _a.setCheckable(True)
                _a.setToolTip("自定义皮肤：叠加在当前内建皮肤之上的样式表")
                _a.triggered.connect(lambda _c, k=_ck: self.set_skin(k))
                m_skin.addAction(_a)
                self._skin_actions[_ck] = _a
        m_skin.addSeparator()
        self.act_open_skins = QAction("打开皮肤目录…", self)
        self.act_open_skins.setToolTip(
            "把 .qss 文件放进这个目录，重启后就会出现在上面的皮肤列表里。\n"
            "文件里写一行 /* mrm-base: night */ 可指定它叠在哪个皮肤上。")
        self.act_open_skins.triggered.connect(self._open_skins_dir)
        m_skin.addAction(self.act_open_skins)
        self._refresh_skin_checks()
        m_set.addSeparator()
        m_set.addSection("启动与集成")
        self.act_onboarding = QAction("新手引导（重新运行向导）…", self)
        self.act_onboarding.setToolTip(
            "3 步向导：① 粘贴网址自动匹配适配器 ② 设 AI 与 Web 密码 "
            "③ 创建第一个任务。\n"
            "全新安装的首次运行会自动弹出一次，之后随时可以在这里重跑。")
        self.act_onboarding.triggered.connect(self.open_onboarding_wizard)
        m_set.addAction(self.act_onboarding)
        self.act_db_enc = QAction("数据库加密…", self)
        self.act_db_enc.setToolTip(
            "开启后用主密码把本地数据库加密落盘，防止本机其它用户/"
            "物理拿到硬盘直接读库。默认关；开启时设主密码。")
        self.act_db_enc.triggered.connect(self.db_encryption_dialog)
        m_set.addAction(self.act_db_enc)
        self.act_rate_plan = QAction("下载限速计划…", self)
        self.act_rate_plan.setToolTip(
            "按一天里的时段给采集带宽设上限（夜里全速、白天收着点）。\n"
            "默认关闭；打开后只对**之后新开始**的下载生效，"
            "正在跑的任务按它开始时那一刻的时段走到底。")
        self.act_rate_plan.triggered.connect(self.open_rate_plan_dialog)
        m_set.addAction(self.act_rate_plan)
        self.act_hotkeys = QAction("快捷键…", self)
        self.act_hotkeys.setToolTip(
            "自定义审核页、灯箱、任务启停的键位；冲突会给中文提示。\n"
            "保存后立即生效（含界面上的键位文案）。")
        self.act_hotkeys.triggered.connect(self.open_hotkey_dialog)
        m_set.addAction(self.act_hotkeys)
        self.act_autostart = QAction("开机自启: 关", self)
        self.act_autostart.triggered.connect(self.toggle_autostart)
        m_set.addAction(self.act_autostart)
        self.act_tray_start = QAction("启动后最小化到托盘", self)
        self.act_tray_start.setCheckable(True)
        self.act_tray_start.setChecked(
            bool(getattr(self.cfg, "tray_start_minimized", False)))
        self.act_tray_start.setToolTip(
            "勾上后，下次启动不弹主窗口，直接缩在任务栏通知区域。\n"
            "点托盘图标（或再双击程序图标）即可唤回窗口；\n"
            "退出请走托盘右键「退出」，别在任务管理器里杀。")
        self.act_tray_start.triggered.connect(self.toggle_tray_start)
        m_set.addAction(self.act_tray_start)
        self.act_shortcut = QAction("创建桌面快捷方式", self)
        self.act_shortcut.triggered.connect(self.create_shortcut)
        m_set.addAction(self.act_shortcut)
        # 2026-10-27（编号 13）：监视文件夹开关（路径在设置里配）。
        self.act_watch_dir = QAction("监视文件夹", self)
        self.act_watch_dir.setCheckable(True)
        self.act_watch_dir.setChecked(bool(getattr(self.cfg, "watch_enabled", False)))
        self.act_watch_dir.setToolTip(
            "把「一行一个网址」的 .txt 丢进监视目录，自动建任务。\n"
            "处理过的清单会移到「已处理/」，不会删除。")
        self.act_watch_dir.triggered.connect(self.toggle_watch_folder)
        m_set.addAction(self.act_watch_dir)
        m_set.addSeparator()
        m_set.addSection("网络与常用行为")
        self.act_sanitize = QAction("日志脱敏（URL 只留域名）", self)
        self.act_sanitize.setCheckable(True)
        self.act_sanitize.setChecked(bool(getattr(self.cfg, "log_sanitize", True)))
        self.act_sanitize.setToolTip(
            "开启后写入日志文件和界面日志面板的内容会自动打码：\n"
            "网页链接只保留域名、本机目录只保留文件名。\n"
            "排障时若需要看完整地址，可临时关闭。")
        self.act_sanitize.triggered.connect(self.toggle_sanitize)
        m_set.addAction(self.act_sanitize)
        # ---- 需求 28：Web 功能开关（仿 qBittorrent）----
        self.act_web = QAction("启用 Web 功能（可跨设备访问）", self)
        self.act_web.setCheckable(True)
        # R231：启动时按 cfg.web_enabled 还原勾选态，必须 blockSignals——
        # 否则 setChecked(True) 触发 toggled→toggle_web(True) 同步弹配置框。
        self.act_web.blockSignals(True)
        self.act_web.setChecked(bool(getattr(self.cfg, "web_enabled", False)))
        self.act_web.blockSignals(False)
        self.act_web.setToolTip(
            "开启后用浏览器（电脑/手机/平板）访问本软件，看任务/图库/待审核，"
            "并从网页启停任务。\n"
            "· 默认只监听本机（127.0.0.1）；要让别的设备访问把监听地址改成 0.0.0.0\n"
            "· 密码为选填项：留空表示免密（建议仅在监听本机时使用）\n"
            "· 桌面 GUI 不受影响，两边共用同一份数据")
        self.act_web.toggled.connect(self.toggle_web)
        m_set.addAction(self.act_web)
        # R231：web_enabled=true 时，启动后静默恢复 Web 服务（不弹配置框）。
        QTimer.singleShot(0, self._restore_web_on_start)
        self._web_server = None
        # R154-F20：扫码登录授权队列（HTTP 线程 emit 进来，主线程弹框）。
        self._qr_login_pending: list = []
        self._qr_login_box = None
        self.qr_login_requested.connect(self._prompt_qr_login)
        self.act_keyask = QAction("键盘审核时弹选词框", self)
        self.act_keyask.setCheckable(True)
        self.act_keyask.setChecked(bool(getattr(self.cfg, "review_key_ask", False)))
        self.act_keyask.setToolTip(
            "默认关闭：用键盘 ↑/↓ 快速判定时不弹「未命中主题词」选词框。\n"
            "打开后，键盘判定也会弹出选词框（鼠标点右键/按钮始终会弹）。")
        self.act_keyask.triggered.connect(self.toggle_key_ask)
        m_set.addAction(self.act_keyask)
        self.act_convert_jpg = QAction("保存时转成 JPG（通用预览）", self)
        self.act_convert_jpg.setCheckable(True)
        self.act_convert_jpg.setChecked(
            bool(getattr(self.cfg, "convert_to_jpg", False)))
        self.act_convert_jpg.setToolTip(
            "开启后新采集的图片一律存成 .jpg（webp/avif/png/gif 都会转），"
            "灯箱、缩略图、图库通用预览都不会再有看不了的格式。\n"
            "只影响**以后新落的图**，老图去「工具 → 把已有图片转成 JPG…」。")
        self.act_convert_jpg.triggered.connect(self.toggle_convert_jpg)
        m_set.addAction(self.act_convert_jpg)
        self.act_goto_gallery = QAction("任务结束后自动跳到图库", self)
        self.act_goto_gallery.setCheckable(True)
        self.act_goto_gallery.setChecked(
            bool(getattr(self.cfg, "task_done_goto_gallery", False)))
        self.act_goto_gallery.setToolTip(
            "关闭（默认）：任务完成/报错后都停在当前页面，只在日志和通知里告知。\n"
            "开启：任务成功完成后才自动跳到图库页看结果（报错不跳）。")
        self.act_goto_gallery.triggered.connect(self.toggle_goto_gallery)
        m_set.addAction(self.act_goto_gallery)
        m_set.addSeparator()
        m_set.addSection("退出")
        a_quit = QAction("完全退出", self)
        icons.set_themed_icon(a_quit, "log-out", "{text_dim}", 16)
        a_quit.setToolTip(
            "真正退出程序（先安全收尾：停任务→等线程→关库）。\n"
            "关闭窗口只是缩到托盘，要退出请用这里或托盘右键的退出。")
        a_quit.triggered.connect(self.request_quit)
        m_set.addAction(a_quit)

        # ---------- 5) 帮助：说明书 / 快捷键 / 更新 ----------
        m_help = mb.addMenu("帮助")
        m_help.addAction("快捷键说明", self.show_shortcuts)
        m_help.addAction("新手引导（重新运行向导）…", self.open_onboarding_wizard)
        # 2026-10-27（编号 7）：检查程序更新。
        self.act_check_update = QAction("检查程序更新…", self)
        self.act_check_update.setToolTip(
            "显示当前版本与构建时间；\n"
            "若在设置里填了「更新清单地址」，会比对是否有新版本。")
        self.act_check_update.triggered.connect(self.check_update)
        m_help.addAction(self.act_check_update)

        # ---------- V0.0.19 双模式：按 cfg.ui_mode 控制顶级菜单显隐 ----------
        # 新手模式藏「工具」「视图」；恢复时按配置还原。
        self._m_tools, self._m_view = m_tools, m_view
        self._apply_ui_mode_menus()

        try:
            # ⚠ QShortcut 在 PySide6 里位于 **QtGui**（6.11 起 QtWidgets 里
            # 已导入不到）；以前这里从 QtWidgets 导，ImportError 被 except
            # 吞掉 ⇒ F1 快捷键静默失效（菜单项还能用，所以一直没被发现）。
            from PySide6.QtGui import QKeySequence, QShortcut
            sc = QShortcut(QKeySequence("F1"), self)
            sc.activated.connect(self.show_shortcuts)
        except Exception:
            pass

        # ---------- F19：任务启停快捷键（可自定义，见 hotkeys.py）----------
        self._hotkey_shortcuts: list = []
        self._rebind_hotkeys()

        # ---------- 【R117 P1-2】任务表 Ctrl+A 全选 ----------
        # 只挂在任务表自己的焦点范围（WidgetShortcut）：QLineEdit 里
        # Ctrl+A 是"移到行首"，不能被窗口级快捷键抢走。
        try:
            from PySide6.QtGui import QKeySequence, QShortcut
            from PySide6.QtCore import Qt as _Qt
            _tt = getattr(self, "task_table", None)
            if _tt is not None:
                _sc_sel = QShortcut(QKeySequence.SelectAll, _tt)
                _sc_sel.setContext(_Qt.ShortcutContext.WidgetShortcut)
                _sc_sel.activated.connect(
                    lambda: _tt.select_all())
                _tt._r117_select_all_shortcut = _sc_sel
        except Exception:
            pass

        # ---------------- 通用框架：左导航 | 右内容页 ----------------
        # #qB皮肤：同一套代码按 theme.preset_layout 显隐/重排控件，不另写 UI。
        # 主窗口**始终把两套控件都建出来**（顶栏工具条、平铺导航、分组导航、
        # 状态栏、任务页底部面板），由 _apply_layout_visibility() 按当前预设切换；
        # 这样切皮肤**不用重建窗口**，三页状态也不丢。
        # 顶栏工具条（非 qB 皮肤建出来但隐藏）
        # r66 全局顶栏（TOP_PARALLEL_ORDER）：同时运行任务数。
        # **必须在 _build_toolbar 之前调用** —— QMainWindow 同一区域的工具条
        # 按加入顺序叠放，先加的在上，这样 _topstrip 才是界面最顶那一行。
        # 原 qB 动作工具条（self._tb）本单已取消：顶部菜单栏 + 页内工具条
        # 已覆盖全部动作，且它与页内 4 按钮在 qB 皮肤里是重复项。
        self._build_top_strip()

        self._split_h = QSplitter()

        # LEFT: 侧栏容器（平铺导航 + 分组导航，按布局显隐其一）
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        slv = QVBoxLayout(self.sidebar)
        slv.setContentsMargins(0, 0, 0, 0)
        slv.setSpacing(0)
        # 需求 1：取消左上角的 logo（那个 Sweet 图），改为在右下角
        # 日志/统计框里做水印（见 gui/watermark.py，透明度 10%）。
        # 这里只留一条可拖动的空白标题条，保留「拖这里能挪窗口」的手感。
        hdr = DragArea(self)               # 标题条仍可拖动窗口
        hdr.setFixedHeight(44)
        hh = QHBoxLayout(hdr)
        hh.setContentsMargins(10, 4, 8, 2)
        hh.setSpacing(7)
        # V0.0.19：logo 从侧栏左下角水印迁到侧栏顶部（用户指令：
        # 「设置中心占了下角，logo 找个合适的地方」）。图标取窗口图标
        # 同一来源（icon_path），取不到就整块退回纯文字，不留空位。
        try:
            _lp = icon_path()
            if _lp.exists():
                _li = QLabel(hdr)
                _pm = QPixmap(str(_lp))
                if not _pm.isNull():
                    _li.setPixmap(_pm.scaled(
                        22, 22,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                    hh.addWidget(_li)
        except Exception:
            pass
        _lt = QLabel("Sweet采集", hdr)
        _tstyle(_lt, "color:{text};font-size:12px;font-weight:500;")
        hh.addWidget(_lt, 1)
        slv.addWidget(hdr)
        self._flat_nav = self._build_flat_nav()
        self._grouped_nav = self._build_grouped_nav()
        slv.addWidget(self._flat_nav)
        slv.addWidget(self._grouped_nav)

        # CENTER: 堆叠页（0=下载管理 1=图库 2=审核）
        self.stack = QStackedWidget()
        self.stack.currentChanged.connect(self._on_page_changed)
        self.stack.currentChanged.connect(lambda _=None: self._sync_nav_checks())

        # ---- 页0：迅雷式下载管理 ----
        page0 = QWidget()
        p0 = QVBoxLayout(page0)
        p0.setContentsMargins(12, 10, 12, 8)
        p0.setSpacing(8)

        # 主单项3：任务页工具条的 新建任务/全部开始/全部停止/编辑 4 个按钮
        # **已删除**（顶部菜单栏 + 右键菜单已全覆盖）。撤按钮前逐条核对过
        # 替代入口，缺一不可：
        #   · 新建任务   → 任务菜单「新增任务」/ 右键（卡片双击同样能建）
        #   · 编辑       → 任务菜单「编辑任务」/ 右键 / 双击任务行
        #   · 全部开始   → 运行菜单「全部开始」（R80 新增，见 _build_ui）
        #   · 全部停止   → 运行菜单「全部停止」（R80 新增，见 _build_ui）
        # ⚠ R66 曾因 qB 皮肤顶栏工具条被撤而把这 4 个按钮改成「常显」；
        #   本单是**连按钮带工具条一起撤**，所以 _apply_layout_visibility 里
        #   那段按 _page0_actions 显隐的代码也一并删除（见该方法）。

        # 任务行列表（每行：名称+进度+统计+状态+运行/停止按钮）
        self.task_list = QListWidget()
        self.task_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self._task_menu)
        self.task_list.currentRowChanged.connect(self._on_select)
        self.task_list.itemDoubleClicked.connect(
            lambda it: self._open_task_form_by_item(it))
        self.task_list.setObjectName("task_list")
        # 任务不多时本就不需要滚动条；溢出时才出现，平时保持干净（用户要求去掉滚动条）
        self.task_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.task_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 用 #task_list ID 选择器，特异性高于全局主题里的 QListWidget::item:selected，
        # 这样选中行既不会被全局紫色整行刷白、也不会漏出白字。同时显式关掉
        # focus 边框（PE_FrameFocusRect），窗口有焦点/无焦点都只显示同一套淡紫选中态。
        _tstyle(self.task_list,
                "#task_list{background:transparent;border:none;outline:none;}"
                "#task_list::item{margin-bottom:6px;border-radius:10px;outline:none;}"
                "#task_list::item:hover{background:rgba(139,124,248,28);}"
                "#task_list::item:selected{background:rgba(139,124,248,58);"
                "color:{text};outline:none;border:1px solid {accent_soft_hover};}"
                "#task_list::item:focus{outline:none;border:1px solid {accent_soft_hover};}"
                "#task_list::item:selected:focus{outline:none;"
                "border:1px solid {accent_soft_hover};background:rgba(139,124,248,64);}")
        p0.addWidget(self.task_list, 1)

        # qB 式原生高密度表格（NATIVE_SKIN_ORDER §2 / gui/task_table.py）。
        # ⚠⚠ **与 task_list 并排共存，绝不删 TaskRow** —— 由
        # theme.LAYOUTS[key]["task_view"] 决定哪套可见（table/cards）。
        # 两套都建出来、靠显隐切换，跟 toolbar/sidebar 同一套路：
        # 重建窗口会丢三页状态，风险高得多。
        from .task_table import TaskTableView
        self.task_table = TaskTableView(self)
        # V0.0.17：列宽与列顺序持久化（与网页端列宽记忆对称）。
        self._restore_task_table_columns()
        # ⚠ R74：「操作」列删除 ⇒ table 的run_requested / stop_requested
        #   两个信号已不存在，这两条接线必须同步撤掉（否则 AttributeError
        #   在建窗那一刻就炸，窗口根本起不来）。
        #   启停能力**没有丢**：改由 `_task_menu`（表格右键菜单）里的
        #   「运行 / 停止」承担，见 `_task_table_menu`。
        self.task_table.edit_requested.connect(self._edit_by_id)
        self.task_table.menu_requested.connect(self._task_table_menu)
        self.task_table.selectionModel().currentChanged.connect(
            lambda _cur, _prev: self._on_select(0))
        # 「被跳过刷新的那套」变脏标记：见 _refresh_task_table 的注释。
        self._task_table_dirty = False
        self._task_cards_dirty = False

        # 底部：日志/统计 + 实时抓图
        self._vsplit = QSplitter(Qt.Orientation.Vertical)
        bottom = QWidget()
        # 水印穿透链上的一环：这层容器透明，水印才透得到右下角「实时抓到」面板。
        # 只点名这一个容器，日志框（在 bottom_tabs 的 opaque pane 里）仍不透，
        # 所以不会出现"整页透明 → 满屏重影"。
        bottom.setObjectName("bottombar")
        bh = QHBoxLayout(bottom)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.setSpacing(6)
        # 需求 4/5：日志、统计、任务、审核、图库都改成标签页 —— 一个区域
        # 一排标签，切换即看，比「日志/统计塞在左边、任务/图库/审核塞在右边」
        # 两套导航好记。这里底部只留一个「日志统计」页。
        self.bottom_tabs = QTabWidget()
        self._stats_tab = self._build_log_stats_tab()
        # **这个 addTab 不能漏**：漏了的话 bottom_tabs 是个「0 页签的空壳」，
        # 而 _stats_tab 只被 new 出来没人要 —— 于是日志/统计/水印全画在一个
        # 孤儿页面里，界面上底部就是一片空白（用户反馈「还是没有水印」的真正
        # 根因：不是透明度低，是压根没上屏）。注释里说「底部只留一个日志统计页」
        # 指的就是这一个 tab。
        self.bottom_tabs.addTab(self._stats_tab, "日志统计")
        # ---- 需求 9：删掉「日志统计」这个导航项 ----
        # 底部只有一页，那排页签就是个**只占地方、点了也不换页**的导航项；
        # 更糟的是它跟页内标题「运行信息」说的是同一样东西 —— 用户看到
        # 两个名字，以为是两块内容。合并口径按作业单建议：**保留「运行信息」**。
        # 这里**只撤页签条、不撤页面**：_stats_tab 照旧是 bottom_tabs 的第 0 页，
        # 之前踩过的坑（漏 addTab ⇒ 日志画在孤儿页上、界面一片空白）不会复现。
        # 省下的那条页签高度自动还给内容区，任务框因此顶上去（"顶住空缺"）。
        # 注意：顶部菜单「维护 → 日志统计」子菜单**保留不动** ——
        # verify_r11 把它的名字钉在基线里，改了会直接破 18/18。
        self.bottom_tabs.tabBar().setVisible(False)
        self.bottom_tabs.setMinimumHeight(120)
        self.bottom_tabs.setMaximumHeight(280)
        bh.addWidget(self.bottom_tabs, 1)
        # #qB皮肤：底部「日志统计」面板由布局字典控制显隐（v1 全预设都留着）
        self._bottom_panel = bottom

        # 【C7 移除实时抓图墙】原来这里有个 190px 宽的 livepanel（live_grid 图墙 +
        # 「实时抓到」标题 + 空提示）。用户 10-07 反馈：取消这个部件 ——
        # 它挤占底部日志区宽度，而且和图库/审核页的缩略图功能重复。
        # 现在底部只留 bottom_tabs（日志统计），日志/统计自动拉长占满。
        # 连带撤掉的还有：图库菜单里的开关项 act_live、toggle_live_wall 方法、
        # refresh_live 的查库重绘、以及每 3 秒一次的 _live_seq 定时检查。
        # refresh_live 保留成**空实现**（四处调用点不用改），写清为什么留。

        self._vsplit.addWidget(self.task_list)
        self._vsplit.addWidget(self.task_table)
        self._vsplit.addWidget(bottom)
        # ⚠⚠⚠ **stretch / sizes 是按下标定位的，加孩子必须同步改这里。**
        #   原本只有 2 个孩子（task_list + bottom），写的是
        #   `setStretchFactor(0,1) / setStretchFactor(1,0) / setSizes([400,300])`。
        #   变成 3 个孩子后，**下标 1 已经变成 task_table，不再是 bottom**：
        #     · stretch(1)=0 落到了表格上，而 bottom 拿默认 stretch 0
        #       ⇒ 三者都不拉伸，Qt 把**全部**高度给了当时可见的那个，
        #     · 于是底部「日志统计」被挤成 **height=0** ——
        #       用户侧现象是「日志统计面板不见了」（而任务列表占满整页）。
        #   ⇒ 现在：**两张任务表都是"主区"**（同一时刻只有一张可见，
        #     都给 stretch=1 不会互相抢），**bottom 固定次要高度**。
        self._vsplit.setStretchFactor(0, 1)      # task_list（cards 时的主区）
        self._vsplit.setStretchFactor(1, 1)      # task_table（table 时的主区）
        self._vsplit.setStretchFactor(2, 0)      # bottom 日志统计：次要
        self._vsplit.setSizes([400, 400, 300])
        p0.addWidget(self._vsplit, 1)

        # ---- 页1/页2 ----
        self._gallery_tab = self._build_gallery_tab()
        self._review_tab = ReviewTab(self.db, self.cfg.isolate_dir,
                                     lambda s: self.log_signal.emit(s), self.cfg)
        # r39：把主窗口挂给审核页，审核网格的缩略图才能复用图库那条线程池
        #（见 review_tab._thumb_icon_async）。没有这个引用，审核页就只能退回在
        # Qt 主线程里同步解原图 —— 见 refresh() 里那一段注释。
        try:
            self._review_tab._win = self
        except Exception:
            pass
        # V0.0.9：学习看板升级成**首页**（用户初衷：打开软件第一眼就该看见
        # 「正按哪些词找、躲哪些词、从图库学到了什么」）。插到 stack 最前，
        # 原页序整体后移一位 → 任务1/审核2/图库3/隔离区4（懒建）。
        # 启动即建（不是懒建）：它就是默认展示页，懒建等于没省。
        # ⚠ _task_page 引用必须存：V0.0.9 页序后移后，凡"切到任务页才做 X"
        #   的判断不许再钉字面量索引（_on_page_changed 的补同步就是例子，
        #   钉 0 会错打在首页上，任务行宽永远补不了同步）。
        self._task_page = page0
        from .learning_board import LearningDashboard
        self._board_tab = LearningDashboard(self.db, embed=True)
        # V0.0.19：首页按当前界面模式初始化（看板自己默认老鸟，
        # 这里显式对齐主窗口 cfg，避免活动配置未登记时首屏显示错档）。
        try:
            self._board_tab.apply_ui_mode(
                str(getattr(self.cfg, "ui_mode", "pro") or "pro"))
        except Exception:
            pass
        self.stack.addWidget(self._board_tab)    # index 0 = 首页
        self.stack.addWidget(page0)
        self.stack.addWidget(self._review_tab)
        self.stack.addWidget(self._gallery_tab)

        # ---- 需求 1/23/26：右下角 Sweet 水印（挂主内容区，切页也跟着在） ----
        # 挂在 self.stack 而不是某一页：这样切「任务 / 待审核 / 图库」哪一页，
        # 水印都固定在这块内容区的右下角 —— 挂在日志统计页的话，一切页它就
        # 跟着那页一起没了（用户明确要求过）。
        # 「最底层」靠 lower()：水印铺满 stack 并沉到所有页之下，文字/缩略图
        # 都画在它上面；页背景透明的地方就能看见它。
        # attach 里同时装了父容器的 resize 转发，切页导致 stack 尺寸变化时
        # 它会自己重新贴合。
        from .watermark import WatermarkOverlay, ALPHA as _WM_ALPHA
        # 主浮层挂 stack（跨页常驻）；审核页/图库页各挂一个独立浮层，
        # 避免这两页不透明内容把 stack 上的主浮层盖住导致看不到水印。
        #
        # ⚠ R48-A2：三处**必须传同一个 anchor=stack**。不传的话每页各算各的
        #   `_content_bottom()`，实测同一时刻三页基线是 848/812/791 三个不同值
        #   → 水印在页与页之间错位（用户反馈"三页位置不一样"）。
        # ⚠ R48-A3：三处 alpha 一律取同一个值（配置里的 watermark_alpha，
        #   缺省才用 watermark.ALPHA）。之前三处都吃构造默认值，而滑条的
        #   setValue 发生在 valueChanged.connect **之前** → 信号还没连上，
        #   配置里的透明度**从来没同步到控件**，用户拖滑条只改当次。
        _wm_a = int(getattr(self.cfg, "watermark_alpha", _WM_ALPHA) or _WM_ALPHA)
        self._watermarks = []
        # R233：水印改挂「左侧导航栏 self.sidebar」那一列、贴它左下角 = 整个
        # 软件最左列（任务/审核/图库/隔离区按钮的下方，用户 A 方案）。sidebar
        # 跨页固定，一处即可；不再需要 stack/review/gallery 三处。
        _wm = WatermarkOverlay(alpha=_wm_a, parent=self.sidebar,
                               anchor=self.sidebar)
        _wm.attach(self.sidebar)
        self._watermarks.append(_wm)
        self._watermark = _wm  # 兼容旧引用
        # 三页同一个 alpha：建完立刻统一刷一次，别指望滑条信号（它还没连上）。
        self._apply_watermark_alpha(_wm_a)
        # R50：水印总开关。⚠ 必须在**建完之后**统一刷 —— 三处浮层都已
        # 建好才谈得上"一起关"。设 False 时用 setVisible(False) 而不是
        # 不建浮层：Web 端可能在软件运行中改这个开关，不建就再也开不回来。
        self._apply_watermark_enabled(
            bool(getattr(self.cfg, "watermark_enabled", True)))

        self._split_h.addWidget(self.sidebar)
        self._split_h.addWidget(self.stack)
        self._split_h.setStretchFactor(0, 0)
        self._split_h.setStretchFactor(1, 1)
        self._split_h.setSizes([96, 1164])

        # 全宽底部状态栏：队列状态 | 统计（替代窄侧栏底部）
        # #qB皮肤：改成**真正的 QStatusBar**（qB 观感由全局 QSS 决定）；
        # 其它皮肤把它刷成透明无边框，看起来就跟原来的自定义状态条一样。
        # ⚠ 为什么只做一份 QLabel 而不是两套：lbl_queue/lbl_stats 被多处
        #   刷新逻辑直接改文本，一个 QLabel 只能挂在一个父控件上，
        #   做两套就等于「两个状态栏各更新一半」，必然对不上。
        self._statusbar = QStatusBar(self)
        self._statusbar.setObjectName("statusbar_qb")
        self.lbl_queue = QLabel("队列: 空闲")
        _tstyle(self.lbl_queue, "color:{text_dim}; font-size:12px;")
        self.lbl_stats = QLabel("已扫描 0 · 丢弃 0 · 保存 0")
        _tstyle(self.lbl_stats, "color:{text_dim}; font-size:12px;")
        self._statusbar.addWidget(self.lbl_queue, 1)
        self._statusbar.addPermanentWidget(self.lbl_stats)
        # V0.0.17：状态栏右侧常驻设置入口（分组导航皮肤下同样可达）。
        self._status_settings_btn = QPushButton("设置中心")
        self._status_settings_btn.setObjectName("navbtn")
        self._status_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._status_settings_btn.setToolTip("打开按八个分区整理的设置中心，可搜索设置项")
        self._status_settings_btn.clicked.connect(
            lambda _=False: self.show_settings_center())
        self._statusbar.addPermanentWidget(self._status_settings_btn)
        self.setStatusBar(self._statusbar)

        shell = QWidget()
        sh = QVBoxLayout(shell)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.setSpacing(0)
        sh.addWidget(self._split_h, 1)
        self.setCentralWidget(shell)
        # 自绘右上角三键 / 窗口圆角已随「改回 Windows 原生标题栏」一起撤掉

        # 按当前皮肤预设显隐/重排控件（qB=全开，其它=原样）
        self._apply_layout_visibility()
        # 分组侧栏数量角标：初始拉一次 + 每 2 秒刷新
        self._refresh_nav_badges()
        self._badge_timer = QTimer(self)
        self._badge_timer.setInterval(2000)
        self._badge_timer.timeout.connect(self._refresh_nav_badges)
        self._badge_timer.start()

        self.resize(1280, 760)
        self._restore_ui_state()
        self._lock_layout()
        # C8：启动时自动指向默认模型（只在用户没配过时生效）
        self._autopoint_default_model()


    def _watermark_below_widget(self):
        """R176：水印锚「左侧导航（任务/审核/图库/隔离区）」正下方。

        两套导航按皮肤二选一，这里**挑当前可见、且在 stack 坐标里最低的
        那一个**导航按钮（= 隔离区）。都不可见（比如构造期布局还没跑）
        就返回 None —— 水印退回贴左下角，不至于飘到窗口外。

        返回 callable 的**绑定方法**给水印：换皮肤/改窗口后每次 _fit
        都会重新解析，不会锚死在换布局前那一套上。
        """
        cands = list(getattr(self, "_nav_btns", None) or [])
        for item in (getattr(self, "_nav_qb", None) or []):
            if isinstance(item, (tuple, list)) and item:
                cands.append(item[0])
            elif item is not None:
                cands.append(item)
        best, best_y = None, -1
        for b in cands:
            try:
                if b is None or not b.isVisible() or b.height() <= 0:
                    continue
                y = b.mapTo(self.stack, QPoint(0, b.height())).y()
            except (RuntimeError, TypeError, AttributeError):
                continue
            if y > best_y:
                best, best_y = b, y
        return best

    def _quar_reload(self):
        """重新载入隔离区列表（隔离区页构建时把具体实现挂到 _quar_reload_impl）。

        2026-10-27：原来直接 `self._quar_reload = reload_rows` 动态赋值，
        静态守卫（test_l1_self_method_calls_all_exist）看不到、IDE 也标红。
        改成**真方法 + 内部委托**，两边都满意，行为不变。
        """
        fn = getattr(self, "_quar_reload_impl", None)
        if callable(fn):
            fn()

    def _nav_go(self, ix: int):
        """左侧图标导航切换页面。

        R181：索引 3 = 「隔离区」—— 它**已是 stack 里的一页**（主窗口内嵌
        面板，与图库/审核同级）。R167 时代的特殊分支（拦在 setCurrentIndex
        之前开模态对话框、关后补同步选中态）已作废。
        但隔离区页是 **build-once 懒建**：启动不预付几千条隔离记录的取数
        + 缩略图成本（与旧弹窗一样，只在打开时付）⇒ 首调时页可能还不在
        stack 里，直接 setCurrentIndex(3) 会切到越界页。所以这里仍拦一层，
        转交 `quarantine_manager`（首调 build+切页，次调切页+重载）；
        菜单「隔离区 → 隔离区管理…」与导航共用同一入口。
        """
        if ix == self._NAV_QUARANTINE_IX:
            try:
                self.quarantine_manager()
            except Exception as e:
                try:
                    self.log_signal.emit(f"打开隔离区失败：{e}")
                except Exception:
                    pass
            return
        self.stack.setCurrentIndex(ix)
        # V0.0.9：切回「首页（学习看板）」就重刷一次数据——用户可能在审核页
        # 判退了几张图，回到首页要立刻看到新的词表，不该停在开机那一眼。
        if ix == 0:
            bt = getattr(self, "_board_tab", None)
            if bt is not None:
                try:
                    bt.reload()
                except Exception:
                    pass
        # #qB皮肤：平铺/分组两套导航的选中态一起同步（分组侧栏才有角标那套）
        self._sync_nav_checks()
        # 每次切到「待审核」都重刷一次：标题行的总数量才不会停在旧值上
        # （用户反馈"待审核区缺少总数量"，一半原因是切过去后还是上次那个数）。
        rt = getattr(self, "_review_tab", None)
        if rt is not None and ix == self.stack.indexOf(self._review_tab):
            try:
                rt.reload()
            except Exception:
                pass
        # r36：切到「图库」页时自己刷一次。以前图库只在 refresh() 里加载
        # （refresh 会无条件调 load_gallery），图库页被切走后没人管 —— 现在
        # refresh 不再无条件重建图库了（见 refresh 里的说明），加载就交给切页。
        gt = getattr(self, "_gallery_tab", None)
        if gt is not None and ix == self.stack.indexOf(gt):
            try:
                self.load_gallery()
            except Exception:
                pass

    # ───────────────────────────────────────────────────────────────────
    # #qB皮肤：框架按 theme.preset_layout 显隐/重排（同一套代码，不另写 UI）
    #
    # 主窗口**始终**把两套控件都建出来（顶栏工具条 / 平铺导航 / 分组导航 /
    # 状态栏 / 任务页底部面板），切皮肤时只调一次 _apply_layout_visibility()
    # 显隐+调宽，绝不重建整个窗口（重建会丢三页状态、风险高）。
    # ───────────────────────────────────────────────────────────────────
    def _build_top_strip(self):
        """全局顶栏：同时运行任务数（右对齐，**全部 5 个皮肤都显示**）。

        r66 / TOP_PARALLEL_ORDER：以前这个数字框只长在任务页的页内工具条上，
        于是「不停下载」的用户必须先停在下载页才改得到它。挪到顶栏之后，
        在图库页/审核页/任何皮肤下都够得着。

        ⚠ 为什么用 theme.style（登记式）而不是 setStyleSheet（硬编码）：
          行内样式一旦写死 hex，换皮肤时这个控件不会跟着重渲（MEMORY 铁律
          「色值不许做成模块级常量」的同一个道理）。theme.style 走弱引用
          登记表，切皮肤时统一重渲一遍。
        ⚠ 原生皮肤（classic）下 css() 返 ""，等于清掉行内样式 → 顶栏控件
          回到系统原生观感，不会残留自绘紫色。这正是作业单 §2 验收第4 条。
        """
        ts = QToolBar("状态条", self)
        ts.setObjectName("topstrip")
        ts.setMovable(False)
        ts.setFloatable(False)
        # 左占位把右侧控件顶到右端
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        ts.addWidget(spacer)
        lbl = QLabel("同时运行任务数:")
        _tstyle(lbl, "color:{text_dim}; font-size:12px;")
        self.spin_parallel = QSpinBox()
        # r37：以前写死上限 6，用户根本选不到「20 个任务同时跑」。
        # R178（2026-10-22 用户要求）：20 也不够，他要能设到 **50**。
        # 上限就给 50 —— 队列本身是 `asyncio.Semaphore(max_concurrent)`，
        # 想设多少都不会把线程池撑爆（并发数只决定「同时跑几个」，
        # 不是「开几个线程」），再高的实际收益由机器资源决定，不是UI该卡的。
        self.spin_parallel.setRange(1, MAX_PARALLEL_TASKS)
        # ⚠ setValue 必须**排在 connect 之前**：反过来的话这一句会立刻发一次
        # valueChanged → _set_parallel → save_config + 摸 self._queue，
        # 而 __init__ 此刻队列还没建起来。
        self.spin_parallel.setValue(int(getattr(self.cfg, "parallel_tasks", 3)))
        self.spin_parallel.setFixedWidth(92)
        self.spin_parallel.setToolTip("几个任务可以同时下载（改了立即生效）")
        self.spin_parallel.valueChanged.connect(self._set_parallel)
        ts.addWidget(lbl)
        ts.addWidget(self.spin_parallel)
        self._topstrip = ts
        self.addToolBar(ts)   # 先加 → 置顶；本单之后它是唯一顶栏

    def _build_toolbar(self):
        """顶栏动作工具条（#qB皮肤）—— **r66 起不再创建**。

        保留方法体不删，是为了旧配置/存档之外没有任何调用方时也不至于
        AttributeError；但 __init__ 已不再调用它，`_apply_layout_visibility`
        里那行 setVisible 也一并撤了（见该方法）。它上面的六个动作在顶部
        菜单栏（新建/编辑/运行/停止/AI 模型设置）与页内工具条都有入口，
        唯一没有菜单入口的「代理」已补进「维护」菜单。
        """
        from . import icons
        tb = QToolBar("主工具条", self)
        tb.setObjectName("maintb")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        def _add(text, iname, slot, tip=""):
            a = QAction(text, self)
            icons.set_themed_icon(a, iname, "{text_dim}", 18)
            a.setToolTip(tip)
            a.triggered.connect(slot)
            tb.addAction(a)
            return a

        _add("新建任务", "plus", lambda: self._open_task_form(None), "新建一个采集任务")
        _add("全部开始", "play", self.run_all, "把列表里所有未运行的任务入队")
        _add("全部停止", "stop", self.stop_all, "停止当前所有正在运行的任务")
        _add("编辑", "edit", self._edit_selected, "编辑当前选中的任务")
        tb.addSeparator()
        _add("代理", "globe", self.proxy_dialog, "管理 SOCKS/HTTP 代理")
        _add("设置", "settings", self.ai_model_dialog, "打开设置（AI 模型 / 通用偏好）")
        self._tb = tb
        self.addToolBar(tb)

    def _build_flat_nav(self):
        """左侧平铺图标导航（首页/任务/审核/图库/隔离区），非 qB 皮肤用。

        R167：新增「隔离区」。V0.0.9：学习看板升级成首页后**插在最前**，
        其余项与 stack 页序整体后移一位（任务1/审核2/图库3/隔离区4）——
        页序、导航、常量三处同步改，护栏测试钉着不许走偏。
        """
        from . import icons
        w = QWidget()
        w.setObjectName("flatnav")
        lv = QVBoxLayout(w)
        lv.setContentsMargins(8, 4, 8, 10)
        lv.setSpacing(6)
        self._nav_btns = []
        for i, (iname, label) in enumerate(
                (("home", "首页"), ("download", "任务"), ("checklist", "审核"),
                 ("image", "图库"), ("trash", "隔离区"))):
            b = QPushButton(label)
            b.setObjectName("navbtn")
            b.setCheckable(True)
            b.setFixedHeight(56)
            icons.set_themed_icon(b, iname, "{text_dim}", 26)
            b.setIconSize(QSize(26, 26))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, ix=i: self._nav_go(ix))
            lv.addWidget(b)
            self._nav_btns.append(b)
        self._nav_btns[0].setChecked(True)
        # V0.0.17：侧栏底部常驻一个设置中心入口，不必每次翻菜单。
        # 刻意不写进 _nav_btns —— 那五个按钮与 stack 页序一一对应，护栏钉着。
        self._nav_settings_btn = QPushButton("设置中心")
        self._nav_settings_btn.setObjectName("navbtn")
        self._nav_settings_btn.setFixedHeight(40)
        icons.set_themed_icon(self._nav_settings_btn, "settings", "{text_dim}", 20)
        self._nav_settings_btn.setIconSize(QSize(20, 20))
        self._nav_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._nav_settings_btn.setToolTip("打开按八个分区整理的设置中心，可搜索设置项")
        self._nav_settings_btn.clicked.connect(
            lambda _=False: self.show_settings_center())
        lv.addWidget(self._nav_settings_btn)
        lv.addStretch()
        return w

    def _build_grouped_nav(self):
        """qB 式分组侧栏：分组标题 + 带数量角标的导航项。

        R167：媒体库组末尾加「隔离区」。V0.0.9：置顶「总览」组 = 首页
        （学习看板），其余索引整体后移一位。
        """
        from . import icons
        w = QWidget()
        w.setObjectName("sidebar_qb")
        lv = QVBoxLayout(w)
        lv.setContentsMargins(8, 4, 8, 8)
        lv.setSpacing(2)

        self._nav_qb = []
        # 仿 qBittorrent 的分组：总览 / 任务管理 / 媒体库。导航项带实时数量角标。
        groups = [
            ("总览",     [("home", "首页", 0)]),
            ("任务管理", [("download", "下载", 1)]),
            ("媒体库",   [("checklist", "待审核", 2), ("image", "图库", 3),
                          ("trash", "隔离区", 4)]),
        ]
        for gtitle, items in groups:
            gh = QLabel(gtitle)
            _tstyle(gh, "color:{text_dim}; font-size:11px; font-weight:700; "
                         "padding:8px 6px 2px;")
            lv.addWidget(gh)
            for iname, label, ix in items:
                row = QWidget()
                row.setObjectName("navqb_row")
                rh = QHBoxLayout(row)
                rh.setContentsMargins(4, 2, 4, 2)
                rh.setSpacing(8)
                btn = QPushButton(label)
                btn.setObjectName("navqb_btn")
                btn.setCheckable(True)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                icons.set_themed_icon(btn, iname, "{text_dim}", 18)
                btn.setIconSize(QSize(18, 18))
                _tstyle(btn,
                        "QPushButton#navqb_btn{background:transparent;color:{text};"
                        "border:none;border-radius:8px;padding:6px 10px;text-align:left;"
                        "font-size:13px;}"
                        "QPushButton#navqb_btn:hover{background:{accent_soft};}"
                        "QPushButton#navqb_btn:checked{background:{accent};"
                        "color:{on_accent};font-weight:600;}")
                badge = QLabel("0")
                badge.setObjectName("navbadge")
                _tstyle(badge,
                        "QLabel#navbadge{color:{on_accent};background:{accent};"
                        "border-radius:9px;font-size:11px;padding:1px 6px;}")
                rh.addWidget(btn)
                rh.addStretch()
                rh.addWidget(badge)
                btn.clicked.connect(lambda _=False, i=ix: self._nav_go(i))
                lv.addWidget(row)
                self._nav_qb.append((btn, badge, ix))
        lv.addStretch()
        if self._nav_qb:
            self._nav_qb[0][0].setChecked(True)
        return w

    def _nav_badge_counts(self):
        """导航角标计数（R167：四项，返回 dict 而不是元组）。

        口径（订单 R167 需求 2，两端一致）：
          · tasks     = 任务总数（`is_template=0` —— 模板不算任务）
          · review    = 待审核张数（`needs_review=1`）
          · gallery   = 图库内总张数（**图库视图同款 WHERE**）

        ⚠ 四条都是 `COUNT(*)` + 既有 WHERE 口径（webui 的 `_api_view` 视图
        where 映射 / 隔离区管理的 `_QUAR_WHERE`），**不许加 JOIN 或子查询** ——
        这个函数挂在 2 秒定时器上，慢了会拖 UI。
        ⚠ 返回 dict 而不是老的三元组：老写法 `(t,r,g)[ix]` 在加第 4 项时
        会 IndexError（或者静默给 0），dict 缺键能一眼看出来。
        """
        out = {"tasks": 0, "review": 0, "gallery": 0, "quarantine": 0}
        q = (
            ("tasks", "SELECT COUNT(*) c FROM tasks WHERE is_template=0"),
            ("review", "SELECT COUNT(*) c FROM images WHERE needs_review=1"),
            # 图库口径**必须与网页端 `_api_view` 的 gallery 视图逐字一致**
            # （in_quarantine=0 AND blacklisted=0 AND path IS NOT NULL）：
            # 角标数字与点进去看到的张数不一样，是最容易被用户立刻发现的那类错。
            ("gallery", "SELECT COUNT(*) c FROM images "
                        "WHERE in_quarantine=0 AND blacklisted=0 "
                        "AND path IS NOT NULL"),
            # R143/R167：隔离区口径同样与隔离区管理/Web 视图逐字一致。
            ("quarantine", "SELECT COUNT(*) c FROM images "
                           "WHERE in_quarantine=1 AND needs_review=0"),
        )
        for key, sql in q:
            try:
                out[key] = int(self.db.query(sql)[0]["c"] or 0)
            except Exception:
                pass
        return out

    def _refresh_nav_badges(self):
        """刷新分组侧栏的数量角标（R167：数据页挂数字；V0.0.9：首页不挂）。"""
        try:
            counts = self._nav_badge_counts()
        except Exception:
            return
        for btn, badge, ix in getattr(self, "_nav_qb", []):
            try:
                if ix == 0:
                    # 首页是学习看板，没有「多少个」的口径可挂——
                    # 挂 0 会被当成"有 0 个什么"，宁可不出角标。
                    badge.setVisible(False)
                    continue
                badge.setText(str(counts.get(self._NAV_KEY_BY_IX.get(ix), 0)))
                badge.setVisible(True)
            except Exception:
                pass

    def _sync_nav_checks(self):
        """让平铺/分组两套导航的选中态都跟当前页对齐。

        R181：隔离区（索引 `_NAV_QUARANTINE_IX`）**已是 stack 里的一页**
        （内嵌面板），选中态与其它页完全一致。R167 时代「永不选中」的
        动作项语义作废 —— 那两处 `and i != self._NAV_QUARANTINE_IX`
        排除已撤掉：留着会让用户停在隔离区页时四个导航一个都不选中，
        看着像坏了一样。
        """
        ix = self.stack.currentIndex()
        for btn, badge, i in getattr(self, "_nav_qb", []):
            try:
                btn.setChecked(i == ix)
            except Exception:
                pass
        for j, b in enumerate(getattr(self, "_nav_btns", [])):
            try:
                b.setChecked(j == ix)
            except Exception:
                pass

    def _apply_layout_visibility(self):
        """按当前皮肤预设显隐/重排框架控件（#qB皮肤）。

        关键：只切显隐与宽度，**不重建**窗口，三页状态得以保留。
        """
        try:
            from . import theme
        except Exception:
            return
        lay = theme.preset_layout(getattr(self.cfg, "ui_preset", None))
        # r66：顶栏对**全部皮肤**常显（不按 layout 的toolbar 标志 ——
        # 那个标志本来管的是已撤销的 qB 动作工具条，现在没有任何消费者）。
        try:
            self._topstrip.setVisible(True)
        except Exception:
            pass
        grp = bool(lay.get("grouped"))
        try:
            self._flat_nav.setVisible(not grp)
            self._grouped_nav.setVisible(grp)
            # 2026-10-26：平铺导航 96→118 —— 96px 下「隔离区」被裁成「隔离」
            self.sidebar.setFixedWidth(210 if grp else 118)
            self._split_h.setSizes([210 if grp else 118, 1050])
        except Exception:
            pass
        try:
            if lay.get("statusbar"):
                # qB 皮肤：用原生 QStatusBar 观感（由全局 QSS 决定）。
                self._statusbar.setStyleSheet("")
            else:
                # 其它皮肤：状态条与内容底融为一体（透明、无边框）。
                self._statusbar.setStyleSheet(
                    "QStatusBar{background:transparent;border:none;}"
                    "QStatusBar::item{border:none;}")
        except Exception:
            pass
        try:
            self._bottom_panel.setVisible(bool(lay.get("bottom_panel", True)))
        except Exception:
            pass
        # §2：任务列表两套并存，按 task_view 切显隐。
        # ⚠⚠ **切过去的那一套必须先补刷一次**：refresh() 只刷可见的那套
        # （省开销，见 _refresh_task_table），所以隐藏期间它一直是旧的。
        # 忘了这一步的症状是「切皮肤后表格内容是上上一个皮肤时期的」，
        # 而且因为 refresh 高频触发、很难稳定复现 ⇒ 极难查。
        try:
            want_table = lay.get("task_view") == "table"
            self.task_table.setVisible(want_table)
            self.task_list.setVisible(not want_table)
            if want_table and self._task_table_dirty:
                self._refresh_task_table(list_tasks(self.db))
            elif not want_table and self._task_cards_dirty:
                self._refresh_task_list(list_tasks(self.db))
            # ⚠⚠ 切到卡片时必须补一次行宽同步 —— 与 #123「切回任务页要补同步」
            #   **完全同型**：控件从隐藏变可见，隐藏期间不参与布局，
            #   它的行控件保留着上一次的旧宽度 ⇒ 行尾「运行/停止/编辑」
            #   停在屏幕中间不靠右。
            #   以前不会出这个问题，是因为卡片**永远可见**，切皮肤不影响它；
            #   有了 table/cards 两套之后，「切皮肤」也成了让它变可见的入口。
            #   不补这一句的症状是「换了皮肤之后按钮位置怪怪的」，
            #   而它只在「先表格后卡片」这个顺序下出现 ⇒ 极难复现。
            if not want_table:
                try:
                    self._sync_task_rows_width()
                except Exception:
                    pass
        except Exception:
            pass
        # 主单项3：任务页工具条连 4 个按钮一起撤了（R80），原来那段
        # 「按 _page0_actions 逐个 setVisible(True)」的补偿代码随之删除。
        self._sync_nav_checks()

    def _set_parallel(self, n: int):
        """同时运行的任务数（改了立即生效）。

        r37：保存配置仍然要做（关窗口后再生效），只有队列那一步可能因为
        事件循环已关闭而抛错 —— 以前这个异常直接冒到 Qt 槽外面，写进
        gui-crash.log 三连，用户看到的就是「软件不能正常使用」。
        """
        try:
            self.cfg.parallel_tasks = int(n)
        except Exception:
            pass
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        q = getattr(self, "_queue", None)
        if q is not None:
            try:
                q.set_concurrency(int(n))
            except Exception:
                pass

    def _sync_cfg_field(self, field: str, value):
        """把刚改的设置**立刻**灌进已经在跑的 engine / queue。

        这两个对象各自持有一份 cfg 引用（不是 main_window 那一份），只
        `save_config()` 的话，正在跑的任务要等重开软件才认这个开关。
        """
        for obj in (getattr(self, "_engine", None),
                    getattr(self, "_queue", None),
                    getattr(self, "_fetcher", None)):
            cfg = getattr(obj, "cfg", None)
            if cfg is not None and hasattr(cfg, field):
                try:
                    setattr(cfg, field, value)
                except Exception:
                    pass

    def toggle_convert_jpg(self, checked: bool):
        """设置 → 「保存时转成 JPG」。"""
        self.cfg.convert_to_jpg = bool(checked)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self._sync_cfg_field("convert_to_jpg", bool(checked))
        self.log_signal.emit(
            "设置: " + ("新采集的图片将统一转成 JPG 保存"
                        if checked else "不再转换，按站点原格式保存"))

    def toggle_goto_gallery(self, checked: bool):
        """设置 → 「任务结束后自动跳到图库」（2026-10-26 新增，默认关）。"""
        self.cfg.task_done_goto_gallery = bool(checked)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self._sync_cfg_field("task_done_goto_gallery", bool(checked))
        self.log_signal.emit(
            "设置: " + ("任务成功完成后将自动跳到图库页"
                        if checked else "任务结束后不再自动跳图库（只通知）"))

    def convert_existing_to_jpg(self):
        """维护 → 「把已有图片转成 JPG」（只救历史库，新图靠设置开关）。"""
        from PySide6.QtWidgets import QMessageBox, QProgressDialog
        from ..convert_img import convert_file_to_jpg
        try:
            rows = self.db.query(
                "SELECT id, path, bytes FROM images "
                "WHERE in_quarantine=0 AND blacklisted=0 "
                "AND path IS NOT NULL AND path<>''")
        except Exception as e:
            QMessageBox.critical(self, "出错了", f"读取图库失败：{e}")
            return
        targets, skipped = [], 0
        for r in rows:
            p = Path(r["path"])
            if not p.exists():
                skipped += 1
                continue
            if p.suffix.lower() in (".jpg", ".jpeg"):
                skipped += 1
                continue
            targets.append((r["id"], p))
        if not targets:
            QMessageBox.information(
                self, "提示",
                "图库里已经没有需要转换的图片了"
                + (f"（跳过 {skipped} 张已是 JPG / 文件缺失的）" if skipped else ""))
            return
        ok = QMessageBox.question(
            self, "把已有图片转成 JPG",
            f"共 {len(targets)} 张图片待转换（webp / avif / png 等 → jpg）。\n\n"
            f"· 转换前会把原文件备份到 thumbs/converted_backup/，可随时还原\n"
            f"· 只转图库记录指向的文件，磁盘上多出来的孤儿文件不动\n"
            f"· 图库记录的文件路径会同步更新，缩略图缓存会重建\n\n"
            f"继续吗？")
        if ok != QMessageBox.StandardButton.Yes:
            return
        backup = self._thumb_dir() / "converted_backup"
        try:
            backup.mkdir(parents=True, exist_ok=True)
        except Exception:
            backup = None
        dlg = QProgressDialog("正在转换…", "中止", 0, len(targets), self)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.show()
        n = fail = 0
        for i, (iid, p) in enumerate(targets):
            dlg.setValue(i)
            dlg.labelText = f"{p.name} ({i+1}/{len(targets)})"
            try:
                from PySide6.QtWidgets import QApplication
                QApplication.processEvents()
            except Exception:
                pass
            if dlg.wasCanceled():
                break
            try:
                if backup is not None:
                    try:
                        bp = backup / p.name
                        n_b = 0
                        while bp.exists():
                            n_b += 1
                            bp = backup / f"{p.stem}_{n_b}{p.suffix}"
                        import shutil as _sh
                        _sh.copy2(str(p), str(bp))
                    except Exception:
                        pass
                _np, conv = convert_file_to_jpg(p)
                if conv:
                    try:
                        self.db.execute(
                            "UPDATE images SET path=?, bytes=?, fmt='JPEG' "
                            "WHERE id=?", (str(_np), _np.stat().st_size, iid))
                    except Exception:
                        pass
                    n += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
        try:
            dlg.close()
        except Exception:
            pass
        try:
            self._gal_sig = None
            self._gal_base_sig = None
            if self.stack.currentIndex() == self.stack.indexOf(self._gallery_tab):
                self.load_gallery()
        except Exception:
            pass
        QMessageBox.information(
            self, "转换完成",
            f"转换成功 {n} 张"
            + (f"，失败 {fail} 张（Pillow 读不了的 exotic 格式）" if fail else "")
            + (f"；已中止，剩余未处理" if n + fail < len(targets) else "")
            + f"。\n原文件备份在：{backup or '（无，备份失败）'}")






    def toggle_maximize(self):
        """最大化 / 还原（统一出口）。

        窗口已是 Windows 原生标题栏：系统按钮、双击标题栏切换、四边拖拽缩放
        都由系统负责，不再需要「无边框模拟最大化」那套自己记账的土办法
        （无边框窗口的 showMaximized() 在 Windows 上经常不真正满屏）。
        这里保留方法，供以后加自定义入口 / 快捷键复用。
        """
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()


    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 窗口大小变化后，图库网格按“固定列数”自适应每列宽度，保持整齐
        g = getattr(self, "g_grid", None)
        if g is not None:
            try:
                self._apply_grid()
            except Exception:
                pass
        # 审核页缩略图同样跟随窗口放大/还原
        rt = getattr(self, "_review_tab", None)
        if rt is not None:
            try:
                rt.apply_grid_size(self.isMaximized())
            except Exception:
                pass
        # 任务行宽度必须跟着视口走：最大化时行被拉宽，还原后若行控件几何没回缩，
        # 右侧的 状态/运行/停止/编辑 会被挤出可视区（表现为"按钮又没了"）。
        self._sync_task_rows_width()

    def _on_page_changed(self, ix: int):
        """切页后补一次任务行宽同步（只在切回「任务」页时）。

        任务列表在审核/图库页是隐藏的，隐藏期间窗口被拉宽/缩小不会更新它的
        viewport，回来时行的旧宽度会让行尾按钮掉到屏幕中间。这里等这一帧布局
        落定（singleShot 0）再量视口同步，保证宽度对得上。

        V0.0.9：首页变成学习看板后任务页索引是 1 —— 条件改用动态查索引，
        不许钉字面量（钉 0 会错把"补同步"挂在首页上，任务行宽永远补不了）。
        """
        if self.stack.indexOf(getattr(self, "_task_page", None)) == ix:
            QTimer.singleShot(0, self._sync_task_rows_width)

    def _sync_task_rows_width(self):
        """把每个任务行的宽度钳到视口宽度，保证行尾按钮永远可见、靠右。

        只有当任务列表**当前可见**时才同步。页面切到审核 / 图库时列表是隐藏的，
        而隐藏控件不参与布局、`viewport().width()` 返回的是上一次的旧值；拿这个
        旧值去 setMaximumWidth + resize 会把行永久钳在旧宽度上 —— 真回归现象：
        「在审核/图库页把窗口拉宽，再切回任务页，行尾的 运行/停止/编辑 跑到屏幕
        中间不靠右」（实测：视口已 1376、行还停在 900）。所以隐藏时直接跳过，
        等切回来由 `_on_page_changed` 补一次同步。
        """
        tl = getattr(self, "task_list", None)
        if tl is None or not tl.isVisible():
            return
        try:
            vw = tl.viewport().width()
            if vw <= 0:
                return
            for i in range(tl.count()):
                it = tl.item(i)
                w = tl.itemWidget(it)
                if w is None:
                    continue
                # 先解锁尺寸约束：哪怕上一轮因为任何原因钳过 max/min，
                # 这里不清掉 resize() 也会被 clamp 回旧宽度（症状同上）。
                w.setMinimumWidth(0)
                w.setMaximumWidth(_WIDGET_MAX_SIZE)
                # 行内是 名称(stretch 吃余量) + 状态 + 三按钮，宽度=视口宽
                # 时按钮必然贴右；行窄于视口就会掉到屏幕中间去。
                w.resize(vw, max(w.height(), 1))
        except Exception:
            pass

    def _ui_state_file(self) -> Path:
        from ..config import user_data_dir
        return user_data_dir() / "ui.json"

    def _save_ui_state(self):
        """记住窗口位置 / 大小 / 是否最大化，下次启动还原。"""
        try:
            # 注意：最大化状态下 self.geometry() 返回的是“满屏矩形”，若直接存它，
            # 下次还原时 showNormal() 会回到满屏、看起来像“无法还原默认大小”。
            # 必须用 normalGeometry()（Qt 记录的“还原后正常尺寸”）来存。
            ng = self.normalGeometry()
            data = {
                "maximized": bool(self.isMaximized()),
                "geometry": [int(ng.x()), int(ng.y()),
                             int(ng.width()), int(ng.height())],
            }
            self._ui_state_file().write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _is_dirty_max_geometry(self, w: int, h: int, scr) -> bool:
        """判断「maximized=true 时记下来的 normalGeometry」是不是脏记录。

        两种脏法，统一当作「没记住」，还原时用默认普通尺寸兜底：
          ① 存的是满屏矩形（旧版误存），还原后仍是满屏、选择「还原」也回不去；
          ② 存的是窄窗——最大化之前用户先把窗口拖到接近 minimumSize（例如
             920 宽），normalGeometry 就记下了这个窄值。旧代码只认①，②会被
             原样还原，表现正是「初始化窗口比原来小一大截」。

        注意：屏幕本身比最小尺寸还小（小屏笔记本 / offscreen 默认 800x800）
        时，正常几何本来就被屏幕压着，此时启用②会把用户正常尺寸误判成脏记录，
        所以要先确认屏幕装得下最小尺寸才启用②。
        """
        min_w, min_h = self.minimumWidth(), self.minimumHeight()
        full = (w >= scr.width() * 0.95 and h >= scr.height() * 0.95)
        # 宽或高任一被拖到接近最小值就算脏：真回归那次记录是 920x760，
        # 920 正好撞在宽度下限上，而 760 离高度下限 640 并不算近 ——
        # 只判高会漏掉这种最典型的窄窗（用户看到的就是「窗口变窄」）。
        too_small = (scr.width() > min_w and scr.height() > min_h
                     and (w <= min_w * 1.1 or h <= min_h * 1.1))
        return full or too_small

    def _restore_ui_state(self):
        """还原上次记住的窗口状态（换显示器 / 分辨率变化会兜底修正）。"""
        try:
            f = self._ui_state_file()
            if not f.exists():
                return
            data = json.loads(f.read_text(encoding="utf-8"))
            from PySide6.QtWidgets import QApplication
            scr = QApplication.primaryScreen().availableGeometry()
            g = data.get("geometry")
            if isinstance(g, list) and len(g) == 4:
                x, y, w, h = (int(v) for v in g)
                w = max(self.minimumWidth(), min(w, scr.width()))
                h = max(self.minimumHeight(), min(h, scr.height()))
                x = min(max(x, scr.x()), scr.x() + max(0, scr.width() - w))
                y = min(max(y, scr.y()), scr.y() + max(0, scr.height() - h))
            else:
                w, h = min(1280, scr.width()), min(760, scr.height())
                x = scr.x() + max(0, (scr.width() - w) // 2)
                y = scr.y() + max(0, (scr.height() - h) // 2)
            self.setGeometry(x, y, w, h)
            if data.get("maximized"):
                # 构造期调用 showMaximized 无效，必须等窗口显示出来。
                # 旧版曾把“最大化矩形”误存为普通 geometry，导致还原时仍满屏、
                # 点“还原”也回不到正常大小。若保存的 geometry 接近满屏，判定为
                # 误存的满屏矩形，改用默认普通尺寸作为还原目标。
                if self._is_dirty_max_geometry(w, h, scr):
                    w = min(1280, scr.width())
                    h = min(760, scr.height())
                    x = scr.x() + max(0, (scr.width() - w) // 2)
                    y = scr.y() + max(0, (scr.height() - h) // 2)
                    self.setGeometry(x, y, w, h)
                # 构造期直接 showMaximized 无效，等窗口显示出来再最大化
                QTimer.singleShot(0, self.showMaximized)
        except Exception:
            pass

    def _lock_layout(self):
        """允许最大化/缩放（用户要“看到的图片多一点、大一点”），但分隔条
        仍锁定，保持左右/上下面板比例固定，不会被拖乱。

        窗口是 Windows 原生标题栏：缩放、最大化、系统三键、双击标题栏切换
        全部由系统负责（沿用无边框时自己搓 WM_NCHITTEST 的那套已删除）；
        这里只锁分隔条，保持左右/上下面板比例固定不被拖乱。
        """
        # 设一个最小尺寸，避免窗口被缩得过小导致面板挤崩
        # 最小宽度不能再放到 920：窗口能被拖到那么窄，一旦「拖窄→最大化→
        # 关闭」，记住的 normalGeometry 就是 920，下次启动看起来又窄又小。
        self.setMinimumSize(1024, 640)
        # 锁定分隔条，禁止拖动改变面板大小（布局比例固定）
        for sp in (self._split_h, self._vsplit):
            for i in range(sp.count()):
                h = sp.handle(i)
                if h is not None:
                    h.setFixedWidth(2)
                    h.setEnabled(False)

    def _task_menu(self, pos, tid=None, global_pos=None):
        """Right-click = TASK actions only (app settings live in 设置 menu).

        ⚠ `tid` / `global_pos` 是给 **qB 式表格**（§2）用的：表格来的右键
        坐标在 `task_table.viewport()` 里，而本方法按`task_list` 的坐标
        解释它（`itemAt` / `mapToGlobal`）。硬把表格坐标换算成列表坐标是
        **错的** —— 表格可见时卡片是隐藏的，它的 viewport 尺寸/位置根本
        不代表屏幕上的任何东西，换算出来的 pos 会让菜单弹在屏幕另一处，
        甚至 `itemAt` 命中一个完全不相干的任务。
        ⇒ 所以让调用方**直接给 id 与全局坐标**，而不是硬做坐标转换。
        `tid=None` 时保持老行为（从 pos 反查选中行）。
        """
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        # 右键点哪行就先选中哪行：菜单里的动作都是针对"当前选中"的，
        # 否则右键弹出的菜单会作用在之前选中的任务上（跟鼠标位置不符）。
        if tid is None:
            it = self.task_list.itemAt(pos)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) is not None:
                self.task_list.setCurrentItem(it)
            tid = self.selected_id()
        else:
            tid = int(tid)
            if tid < 0:                # 表格里右键点了空白处
                tid = None
            else:
                self._select_by_id(tid)
        if tid is None:
            menu.addAction("（先选中一个任务）").setEnabled(False)
            menu.addSeparator()
        else:
            # 顺序按用户要求：运行 → 停止 → 编辑任务。
            # 「保存修改」已移除：表单改成对话框后，主窗口没有可保存的状态，
            # 这项会拿上一次对话框里的旧值覆盖任务（有害无益）。
            menu.addAction(self.act_run)
            menu.addAction(self.act_stop)
            menu.addAction(self.act_edit)
            menu.addSeparator()
            # 【R170】「克隆任务」已按用户拍板**移除** —— 克隆后容易忘改配置，
            # 两个相同任务同时跑=重复采集（用户明确担忧）。模板功能保留：
            # 模板本身不自动跑，且套用后是新任务。
            menu.addAction("存为模板…", lambda: self._save_task_as_template(int(tid)))
            menu.addSeparator()
            # 2026-10-24：出错任务的「查看错误详情…」。表格原来只显示英文
            # "error" 两个字、没有任何原因入口，用户问"出错去哪儿看报错"
            # 说的就是它。中文简短原因 + 完整原文（detailedText 可复制）。
            _err = ""
            try:
                _row = self.db.query(
                    "SELECT last_error FROM tasks WHERE id=?", (int(tid),))
                _err = (_row[0]["last_error"] or "") if _row else ""
            except Exception:
                _err = ""
            if _err:
                menu.addAction("查看错误详情…",
                               lambda: self._show_task_error(int(tid), _err))
                menu.addSeparator()
            # 【F7/R154】失败网址清单：只重跑下载失败的单条 URL，
            # 不重新爬页面（与「重试失败任务」整任务重跑是两条路）。
            menu.addAction("失败网址清单…",
                           lambda: self.show_failed_urls(int(tid)))
            menu.addSeparator()
            menu.addAction(self.act_del)
        menu.addSeparator()
        menu.addAction(self.act_refresh)
        menu.exec(self.task_list.mapToGlobal(pos) if global_pos is None
                  else global_pos)

    def _show_task_error(self, tid: int, err: str):
        """2026-10-24：任务错误详情弹窗（中文原因 + 完整原文可复制）。

        表格原来只有英文 "error"、卡片只有一行简短中文，都没有完整信息
        入口。这里两样都给：正文是中文简短原因，detailedText 是完整原文
        （用户截图反馈"出错去哪儿看报错、有没有中文"）。
        """
        from PySide6.QtWidgets import QMessageBox
        try:
            # 2026-10-27：原来少一个点（`gui` 包里没有 stats），
            # ImportError 被 except 吞掉 ⇒ 界面直接显示英文原文，
            # 用户看到的就是「英文报错」。stats 在包的上一层。
            from ..stats import short_reason
            cn = short_reason(err)
        except Exception:
            cn = (err or "")[:80]
        box = QMessageBox(self)
        box.setWindowTitle(f"任务 #{tid} 错误详情")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"失败原因（中文）：{cn}")
        box.setInformativeText(
            "完整错误信息见下方「显示详细信息…」；也可以去\n"
            "「维护 → 查看日志文件夹」看运行日志，或「导出诊断包」发给我排查。")
        box.setDetailedText(err or "（无详细错误信息）")
        box.exec()

    def toggle_key_ask(self, checked: bool):
        """键盘（↑不喜欢/↓喜欢）判定时是否弹「未命中主题词」选词框。"""
        self.cfg.review_key_ask = bool(checked)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log(
            f"键盘审核弹选词框：{'开' if self.cfg.review_key_ask else '关'}")

    def _autostart_on(self) -> bool:
        try:
            from ..autostart import status
            return status()[0]
        except Exception:
            return False

    def _refresh_task_terms_label(self):
        """把「这回按什么词去找图」实时显示到建任务表单最上面。"""
        lbl = getattr(self, "_lbl_task_terms", None)
        if lbl is None:
            return
        try:
            from .. import find_terms as _ft
            g = _ft.global_terms(self.db, self.cfg)

            def words(attr):
                f = getattr(self, attr, None)
                if f is None or not hasattr(f, "text"):
                    return []
                return [x.strip() for x in
                        re.split(r"[\s,，、;；]+", str(f.text() or ""))
                        if x.strip()]

            typed: list[str] = []
            for a in ("f_ai_labels", "f_ai_l2", "f_ai_l3", "f_keywords"):
                for x in words(a):
                    if x.lower() not in {y.lower() for y in typed}:
                        typed.append(x)
            learned = [w for w in g.want
                       if w.lower() not in {x.lower() for x in typed}]
            avoid: list[str] = []
            for x in words("f_ai_excl") + list(g.avoid):
                if x.lower() not in {y.lower() for y in avoid}:
                    avoid.append(x)
            want_all = [x for x in typed + learned
                        if x.lower() not in {y.lower() for y in avoid}]
            fmt = lambda xs: "、".join(xs[:12]) + ("…" if len(xs) > 12 else "") \
                if xs else "（无）"
            # V0.0.7：判掉过一次的图教出来的词还在观察期，只送审核、
            # 不硬拦。建任务时也说清楚，免得用户以为「拉黑了怎么还在下」。
            soft = [x for x in getattr(g, "soft_avoid", [])
                    if x.lower() not in {y.lower() for y in avoid}]
            lbl.setText(
                "这回目标词：" + fmt(want_all)
                + ("　［学到的：" + fmt(learned) + "］" if learned else "")
                + "\n排除词：" + fmt(avoid)
                + ("\n观察期（判退 1 次，仅进入待审核）：" + fmt(soft)
                   if soft else ""))
        except Exception as e:
            lbl.setText(f"词表读取失败：{type(e).__name__}: {e}")

    def _build_task_form_panel(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        # V0.0.6（界面三块改版第一块）：词表才是建任务的主角，放最上面。
        # 新建/编辑任务时一眼能看见「这回软件会按哪些词去站里找图」，
        # 学来的词单独标出来；改上面的词，这行跟着变。
        self._lbl_task_terms = QLabel("本轮目标词：（尚未填写）")
        self._lbl_task_terms.setWordWrap(True)
        self._lbl_task_terms.setToolTip(
            "「目标词」＝手工填写的主题词 + 从导入的原有图库中学习到的词；\n"
            "「排除词」＝填写的排除项 + 判退学习到的词。采集时下载前即按此表筛选。")
        _tstyle(self._lbl_task_terms,
                "color:{text_dim};font-size:11px;padding:4px 6px;")
        v.addWidget(self._lbl_task_terms)
        LAB_W = 92   # 统一标签宽度：所有编辑框同一起点，精确对齐

        # 【V0.0.19-B 表单分层】按标签把行分三类：
        #   always = 新手/老鸟都常驻（网址/保存到/代理）
        #   pro    = 老鸟直接显示（学习链路核心词表）
        #   adv    = 收进「高级选项」折叠区（低频调参）
        _ALWAYS = {"网址", "保存到", "代理"}
        _PRO = {"本地AI 主题词(一级)", "本地AI 排除词"}

        def _cell_class(label: str) -> str:
            if label in _ALWAYS:
                return "always"
            if label in _PRO:
                return "pro"
            return "adv"

        def half_cell(label, wd, extra=None):
            """半宽单元格：[标签 92px][编辑框填满] —— 右边界正好是行中线。"""
            c = QWidget()
            c.setProperty("mrm_row_class", _cell_class(label))
            h = QHBoxLayout(c)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            lab = QLabel(label)
            lab.setFixedWidth(LAB_W)
            h.addWidget(lab)
            h.addWidget(wd, 1)
            if extra is not None:
                h.addWidget(extra)
            return c

        def full_cell(label, wd, extra=None):
            """整行单元格：AI 主题词/排除词专用。"""
            c = QWidget()
            c.setProperty("mrm_row_class", _cell_class(label))
            h = QHBoxLayout(c)
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            lab = QLabel(label)
            lab.setFixedWidth(LAB_W)
            h.addWidget(lab)
            h.addWidget(wd, 1)
            if extra is not None:
                h.addWidget(extra)
            return c

        # 两个后段容器：老鸟直显区 / 高级折叠区（挂载点在「代理」行后）。
        self._pro_host = QWidget()
        self._pro_lay = QVBoxLayout(self._pro_host)
        self._pro_lay.setContentsMargins(0, 0, 0, 0)
        self._pro_lay.setSpacing(6)
        self._adv_host = QWidget()
        self._adv_lay = QVBoxLayout(self._adv_host)
        self._adv_lay.setContentsMargins(0, 0, 0, 0)
        self._adv_lay.setSpacing(6)
        # 「代理」是常驻区最后一行：它一入布局就立刻把两个后段容器接上，
        # 后续 pro/adv 行自然落进各自容器（不依赖 return 的位置）。
        self._form_tail_mounted = False

        def _mount_form_tail():
            if self._form_tail_mounted:
                return
            self._form_tail_mounted = True
            v.addWidget(self._pro_host)
            self._adv_toggle = QToolButton()
            self._adv_toggle.setText(
                "高级选项（调度 · 限速 · Cookie · 过滤 · AI 词级 · 子目录）")
            self._adv_toggle.setCheckable(True)
            self._adv_toggle.setArrowType(Qt.ArrowType.RightArrow)
            self._adv_toggle.setToolTip(
                "低频调参项收在这里；老鸟模式默认收起，按需展开。")
            self._adv_toggle.toggled.connect(self._adv_body_toggle)
            v.addWidget(self._adv_toggle)
            self._adv_body = self._adv_host
            self._adv_host.setVisible(False)
            v.addWidget(self._adv_host)

        def _row_class(cells):
            rank = {"always": 0, "pro": 1, "adv": 2}
            cls = "always"
            seen = False
            for c in cells:
                k = c.property("mrm_row_class")
                if not k:
                    continue
                seen = True
                if rank.get(str(k), 2) > rank[cls]:
                    cls = str(k)
            return "adv" if (not seen and cls == "always"
                             and not any(c.property("mrm_spacer")
                                         for c in cells)) else cls

        def add_row(*cells):
            """一行放 N 个等宽单元格（2 个=各占一半，1 个=整行）。

            V0.0.19-B：按标签分类路由到常驻/老鸟/高级三个容器；
            路由出常驻行时剔除无标签占位块（半行补齐在高级区里没意义）。
            """
            cls = _row_class(cells)
            if cls != "always":
                # 剔除占位块，剩下的真控件排一行
                keep = [c for c in cells if not c.property("mrm_spacer")]
                if not keep:
                    return
                cells = keep if len(keep) < len(cells) else cells
            h = QHBoxLayout()
            h.setContentsMargins(0, 0, 0, 0)
            h.setSpacing(6)
            for c in cells:
                h.addWidget(c, 1)
            if cls == "adv":
                _adv_lay = self._adv_lay
                _adv_lay.addLayout(h)
            elif cls == "pro":
                self._pro_lay.addLayout(h)
            else:
                v.addLayout(h)
                # 「代理」是常驻区收尾行，它一进来就把后段容器接上
                for c in cells:
                    labs = c.findChildren(QLabel)
                    if labs and labs[0].text() == "代理":
                        _mount_form_tail()

        self.f_entry = QLineEdit()
        self.f_entry.setPlaceholderText("粘贴网址 (https://...)")

        out_row = QWidget()
        # ⚠ R47/B3：这一行原来是**纯横向**布局（f_out + 浏览…），
        # 而 P1-2 的「沿用上次目录」勾选框 + 提示文字被 `_open_task_form`
        # 直接 `addWidget` 塞进**这个横向布局**里 —— 于是每次选择「新建任务」
        # 都往同一行里再塞两个控件，控件越叠越多、互相压盖，
        # 真机表现就是「保存到这一行错乱/重叠」。
        # ⚠ R48/C：R47 那版拆成上下两层（第一层输入框+浏览、第二层沿用），
        #   用户还是不顺手 —— 作业单要求**三个控件同一行**、沿用排在后面。
        #   现在一层搞定；ov 只剩这一行（保留 ov 以免改挂载代码）。
        # ⚠⚠ ov 必须挂在**最终那个** out_row 上。这里 out_row 在下面被
        #   重绑成 reuse_row（三控件同一行），所以布局要等 reuse_row 建好再挂 ——
        #   先挂旧的会让 reuse_row 变成没有布局的裸控件，行内控件全挤在 (0,0)。
        # （f_out / 浏览… 已并入下面那一行，见 R48/C）
        # ⚠ R48/C：作业单要求「保存目录编辑框 + 浏览按钮」与「沿用上次目录」
        #   **同一行**，且排在它后面；并**去掉独立的「上次目录」显示**——
        #   上次目录直接显示在编辑框里（勾选沿用时把上次目录填进编辑框）。
        #   原来是两层：ov 第一层 = f_out+浏览，第二层 = 勾选 + _lbl_last_out。
        #   现在合并成一层，且 _lbl_last_out 保留成**不可见**的兼容对象 ——
        #   `_open_task_form` 仍在 setText 它，删掉属性会 AttributeError。
        reuse_row = QWidget()
        rr = QHBoxLayout(reuse_row)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(6)
        self.f_out = QLineEdit()
        self.f_out.setReadOnly(True)     # 有浏览按钮就不该手打，防误改
        self.f_out.setPlaceholderText("留空=默认图库目录")
        b_out = QPushButton("浏览…")
        b_out.clicked.connect(self.pick_output_dir)
        # R52：本行只留 [编辑框][浏览]，与「代理」行结构一致 →
        # 「保存到」编辑框宽度 == 代理下拉框宽度（用户要求）。沿用勾选移到下一行。
        rr.addWidget(self.f_out, 1)
        rr.addWidget(b_out)
        self._out_row = reuse_row
        out_row = reuse_row
        ov = QVBoxLayout(out_row)      # 挂到最终容器（见上面 ⚠⚠）
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(4)
        ov.addWidget(reuse_row)
        # 沿用上次目录：独立一行（R52，避免挤占「保存到」编辑框宽度）。
        # 仍排在「保存到」后面、且只此一个勾选，不破坏 R48/C「别搞成两层混乱」的本意。
        self._cb_reuse_last = QCheckBox("沿用上次目录（默认不勾）")
        self._cb_reuse_last.setChecked(False)
        self._cb_reuse_last.toggled.connect(self._on_reuse_last_toggled)
        self._cb_reuse_last.setToolTip(
            "勾上才会把上次用的输出目录**填进上面的编辑框**；"
            "不勾则「保存到」留空（留空=存到默认图库目录）。\n"
            "新建任务默认不勾，避免误存到旧目录。")
        # C：上次目录**不再单独显示一行文字**，直接体现在编辑框内容里。
        # 这个标签保留但永久隐藏，只为 `_open_task_form` 的 setText 不炸。
        self._lbl_last_out = QLabel("")
        self._lbl_last_out.setVisible(False)
        self._lbl_last_out.setWordWrap(True)
        _tstyle(self._lbl_last_out, "color:{warn_text};font-size:11px;")
        # 【修复】「自动运行」此前被硬屏蔽成 f_auto=None、写库时强制 auto_run=False，
        # 但后台无头模式(serve)又是照着 auto_run 批量拉任务的 —— GUI 永远开不了它。
        # 恢复成真勾选框。
        self.f_auto = QCheckBox("自动运行（被定时任务 / 后台模式自动拉起）")
        self.f_auto.setToolTip(
            "启用后，该任务将由定时任务或后台常驻模式自动启动，无需手工单击「运行」。\n"
            "常规使用手工单击「全部开始 / 行内运行」即可，无需启用此项。")

        # ---- 控件创建 ----
        self.f_adapter = QComboBox()
        self.f_adapter.setEditable(True)
        self._reload_adapters()
        self.f_adapter.setToolTip(
            "「(自动 - gallery-dl)」表示交由内置下载器处理，多数站点无需修改。\n"
            "只有它不认识的网站才需要选一个适配器规则。粘贴网址后会自动匹配。")

        self.f_proxy = QComboBox()
        self.f_proxy.setEditable(False)      # 有"管理"对话框就够了，不需要手填
        self._reload_proxies()
        b_pm = QPushButton("管理…")
        b_pm.clicked.connect(self.proxy_dialog)
        prow = QWidget(); ph = QHBoxLayout(prow); ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(self.f_proxy, 1)
        ph.addWidget(b_pm)

        self.f_ai_labels = QLineEdit()
        self.f_ai_labels.setPlaceholderText(
            "一级：白丝 sunset（多个词以空格分隔）")
        self.f_ai_labels.setToolTip(
            "多个词须以【空格】分隔（不支持逗号、分号、顿号），否则将被合并为一个词。\n"
            "【本地AI 主题词 · 一级 / 留空即不启用】\n"
            "描述需要保留的图片内容，由本地 AI 为每张图片计算匹配分（完全离线）。\n"
            "支持中英文：填写中文自动补充英文、填写英文自动补充中文（内置词典）。\n"
            "留空表示不按主题筛选。\n"
            "一级为必要条件：未命中时按既有规则隔离或送审；命中后再判定下级。")
        # r29 主题词分级：二级 / 三级细分词 + 严格模式开关
        self.f_ai_l2 = QLineEdit()
        self.f_ai_l2.setPlaceholderText("二级细分：jk 制服（多个词以空格分隔）")
        self.f_ai_l2.setToolTip(
            "多个词须以【空格】分隔（不支持逗号、分号、顿号），否则将被合并为一个词。\n"
            "【AI 二级细分词 / 留空 = 不细分】\n"
            "仅在一级命中后参与判定，用于细化分类：\n"
            "• 二级（或三级）有词达到阈值：图片带细分标签入库，\n"
            "  最深命中的词作为「按主题词分目录」的子目录名；\n"
            "• 均未命中：图片进入【待审核】，可在审核区右键补充标签\n"
            "  （默认行为）。\n"
            "支持中英文。")
        self.f_ai_l3 = QLineEdit()
        self.f_ai_l3.setPlaceholderText("三级细分：黑长直 数码 御姐（多个词以空格分隔）")
        self.f_ai_l3.setToolTip(
            "多个词须以【空格】分隔（不支持逗号、分号、顿号），否则将被合并为一个词。\n"
            "【AI 三级细分词 / 留空表示不细分】较二级更细的标签，"
            "规则相同：需一级命中后参与，未命中则送人工标注。")
        # 2026-10-28：主题词改成**无限分级**（1..N 层），而且**每一级能单独设严格**。
        #   二级 / 三级各配一个「严格」勾选；第 4 级起是动态行（见 _deep_box）。
        self.f_strict2 = QCheckBox("严格")
        self.f_strict2.setToolTip(
            "勾选：二级必须命中，否则整张图判定为不命中（结果更少，无需人工复核）。\n"
            "不勾选：二级未命中时停留在上一级，图片保留（默认，避免误判丢弃）。")
        self.f_strict3 = QCheckBox("严格")
        self.f_strict3.setToolTip(
            "勾选：三级必须命中，否则整张图判定为不命中。\n"
            "注意：二级命中而三级未命中时，勾选此项将丢弃该图片。")
        # V0.0.11：旧的「全局严格模式」勾选撤销。2026-10-28 起每一级都有
        # 独立严格开关（f_strict2 / f_strict3 / 深层动态行），全局勾既冗余
        # 又与单级勾互相矛盾（历史上还得双向同步）。数据层兼容不变：
        # label_levels.apply_levels 仍按「任一级严格」回写 ai_label_strict，
        # 老任务读取时 stricts_of 照样展开。
        self.f_ai_excl = QLineEdit()
        self.f_ai_excl.setPlaceholderText(
            "需要排除的词，多个词以【空格】分隔（支持中英文）")
        self.f_ai_excl.setToolTip(
            "多个词须以【空格】分隔（不支持逗号、分号、顿号），否则将被合并为一个词。\n"
            "【AI 排除词（黑名单）/ 留空表示不启用】\n"
            "图片内容命中这些词（达到阈值）时，文件将被【直接删除】，\n"
            "不在硬盘保留（不进入隔离区）。\n"
            "支持中英文，自动互译；需先填写上方主题词以启用本地 AI。")
        # 词一改，最上面那行「这回要找什么」跟着变，不用等保存再猜
        for _f in (self.f_ai_labels, self.f_ai_l2, self.f_ai_l3,
                   self.f_ai_excl):
            _f.textChanged.connect(lambda *_: self._refresh_task_terms_label())
        self.f_ai_thr = QDoubleSpinBox()
        self.f_ai_thr.setRange(0.0, 1.0)
        self.f_ai_thr.setSingleStep(0.05)
        self.f_ai_thr.setValue(0.35)
        self.f_ai_thr.setFixedWidth(96)
        self.f_ai_thr.setToolTip(
            "【阈值是什么】\n"
            "AI 给图片和主题词的匹配打 0~1 的分数，达到阈值才保留。\n"
            "0.35 = 默认值（三成相似即入库）。调低→收得多但杂；调高→收得少但准。\n"
            "不达标的图进隔离区，随时可恢复。")
        self.f_ai_mode = QComboBox()
        self.f_ai_mode.addItem("命中一个就保留 (OR)", "any")
        self.f_ai_mode.addItem("全部命中才保留 (AND)", "all")
        self.f_ai_mode.setToolTip(
            "【主题词(白名单)匹配方式】\n"
            "• 命中一个就保留：图片内容匹配到任意一个主题词即入库（默认，收得多）。\n"
            "• 全部命中才保留：必须所有主题词都匹配才入库（更严，收得少但更准）。")
        self.f_ai_excl_mode = QComboBox()
        self.f_ai_excl_mode.addItem("命中一个就删除 (OR)", "any")
        self.f_ai_excl_mode.addItem("全部命中才删除 (AND)", "all")
        self.f_ai_excl_mode.setToolTip(
            "【排除词(黑名单)匹配方式】\n"
            "• 命中一个就删除：图片内容匹配到任意一个排除词就直接删除（默认，拦得狠）。\n"
            "• 全部命中才删除：必须所有排除词都匹配才删除（更宽松，少误删）。")

        # ---- 表单：适配器紧跟网址；除 AI 两项外一律半长 ----
        # ---- 表单：所有编辑框同一起点(92px)、半长框右边界=行中线，AI 两项整行 ----
        blank = QWidget()            # 空白占位：保证半长框右边界对齐到行中线
        blank.setProperty("mrm_spacer", True)
        # ---- R154-F16：模板行（下拉 + 套用 + 存为模板） ----
        # 套用 = 把模板配置填进表单并切到「新建」态（保存即建新任务）；
        # 存为模板 = 把当前表单内容（含未保存修改）收成模板，不关对话框。
        self.f_tpl = QComboBox()
        self.f_tpl.setToolTip(
            "常用配置保存为模板后，在此选择并点击「套用」即可填入。\n"
            "「存为模板」将当前表单内容保存为模板（不关闭对话框、不创建任务）。")
        b_tpl_apply = QPushButton("套用")
        b_tpl_apply.setToolTip("将所选模板的配置填入表单（不影响已有任务）")
        b_tpl_apply.clicked.connect(self._apply_template)
        b_tpl_save = QPushButton("存为模板")
        b_tpl_save.setToolTip("把当前表单内容存成模板")
        b_tpl_save.clicked.connect(self._save_form_as_template)
        # V0.0.17：与网页端模板管理对称，补「删除模板」。
        b_tpl_del = QPushButton("删除模板")
        b_tpl_del.setToolTip("删除下拉列表中选中的模板；已有任务不受影响")
        b_tpl_del.clicked.connect(self._delete_selected_template)
        tpl_row = QWidget()
        th = QHBoxLayout(tpl_row)
        th.setContentsMargins(0, 0, 0, 0)
        th.addWidget(self.f_tpl, 1)
        th.addWidget(b_tpl_apply)
        th.addWidget(b_tpl_save)
        th.addWidget(b_tpl_del)
        add_row(half_cell("模板", tpl_row), QWidget())
        add_row(half_cell("网址", self.f_entry))
        add_row(half_cell("适配器", self.f_adapter), QWidget())
        add_row(half_cell("保存到", out_row), QWidget())
        add_row(half_cell("沿用上次目录", self._cb_reuse_last), QWidget())
        add_row(half_cell("代理", prow), QWidget())
        add_row(half_cell("调度", self.f_auto), QWidget())

        # ---- #52 站点 Cookie / 登录态：需要登录才能看的图 ----
        self.f_cookie = QLineEdit()
        self.f_cookie.setPlaceholderText(
            "留空表示不发送（需登录或通过 Cloudflare 校验的站点可使用「取站点 Cookie」自动获取）")
        self.f_cookie.setToolTip(
            "【站点 Cookie / 登录态】\n"
            "部分网站（Cloudflare 挑战、需登录图库）不带上 Cookie 就抽不到图。\n"
            "点击右侧「取站点 Cookie」将通过浏览器打开该站点，完成验证或登录后\n"
            "自动获取 Cookie 并按域名保存，同域名任务后续可自动复用，无需重复填写。\n"
            "仅本机配置文件保存，不会上传。")
        cookie_row = QWidget()
        ch = QHBoxLayout(cookie_row)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.addWidget(self.f_cookie, 1)
        self.b_fetch_cookie = QPushButton("取站点 Cookie")
        self.b_fetch_cookie.setToolTip(
            "通过浏览器打开该站点，完成验证或登录后自动获取并保存 Cookie")
        self.b_fetch_cookie.clicked.connect(self._on_fetch_cookie)
        ch.addWidget(self.b_fetch_cookie)
        self.b_clear_cookie = QPushButton("清除")
        self.b_clear_cookie.setToolTip("删除该域名已保存的 Cookie")
        self.b_clear_cookie.clicked.connect(self._on_clear_cookie)
        ch.addWidget(self.b_clear_cookie)
        add_row(full_cell("站点 Cookie", cookie_row))

        # ---- R173 任务级 User-Agent（留空=用全局）----
        #   为什么必须有：实测 xxknit 是「app UA 403 / 浏览器 UA 200」，而
        #   danbooru 恰好**反过来**（app UA 200 / 浏览器 UA 403）⇒ 全局只有一个
        #   UA 永远救不了两边，必须能逐任务覆盖。cf_clearance 又与 UA+IP 绑定，
        #   所以它和上面的 Cookie 要同一任务一起配。
        self.f_ua = QLineEdit()
        self.f_ua.setPlaceholderText(
            "留空则使用「设置」中的全局 User-Agent（多数站点无需填写）")
        self.f_ua.setToolTip(
            "【任务级 User-Agent / 留空即用全局】\n"
            "个别站点会按 UA 放行或拦截，且方向相反（有的只认浏览器 UA，\n"
            "有的只认程序 UA）。这里填了只影响本任务，不改全局设置。\n"
            "• 撞到 403 又确认不是 IP 问题时，可试浏览器 UA：\n"
            "  Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36\n"
            "• 配 Cloudflare 站的 cf_clearance Cookie 时，两者必须与实际\n"
            "  浏览器一致，缺一个都过不去。")
        self.b_ua_clear = QPushButton("清除")
        self.b_ua_clear.setFixedWidth(64)
        self.b_ua_clear.setToolTip("清空本任务的 User-Agent（恢复用全局）")
        self.b_ua_clear.clicked.connect(
            lambda: self.f_ua.setText(""))
        ua_row = QWidget()
        uh = QHBoxLayout(ua_row)
        uh.setContentsMargins(0, 0, 0, 0)
        uh.addWidget(self.f_ua, 1)
        uh.addWidget(self.b_ua_clear)
        add_row(full_cell("User-Agent", ua_row))

        # ---- #28 按主题词分子目录：开启后站内图片按命中的 AI 主题词分文件夹 ----
        self.f_kwsubdir = QCheckBox("按主题词分子目录（{site}/{主题词}/）")
        self.f_kwsubdir.setToolTip(
            "开启后，图片按命中的 AI 主题词放进对应子目录；\n"
            "都未命中则平铺在站点目录下。需要先在上面填 AI 主题词才生效。")
        vbox_kw = QWidget(); kv = QVBoxLayout(vbox_kw)
        kv.setContentsMargins(0, 0, 0, 0)
        kv.addWidget(self.f_kwsubdir)
        add_row(full_cell("关键词子目录", vbox_kw))

        # ---- 定时运行（可选；留空=不定时）----
        self.f_sched = QLineEdit()
        self.f_sched.setPlaceholderText("留空=不定时（例如 08:30 或 2026-10-01 08:30）")
        self.f_sched.setToolTip(
            "【定时自动运行 / 留空即不启用】\n"
            "• 每天定时：填 HH:MM，例如 08:30 = 每天上午 8:30 自动跑一次。\n"
            "• 一次性定时：填 日期 HH:MM，例如 2026-10-01 08:30 = 那天跑一次。\n"
            "• 错过的时间点会在软件下次开着时自动补跑一次（不会漏跑，也不会重复跑）。\n"
            "到达设定时间后自动入队，无需人工守候。")
        sched_inner = QWidget(); sh = QHBoxLayout(sched_inner)
        sh.setContentsMargins(0, 0, 0, 0); sh.setSpacing(6)
        sh.addWidget(self.f_sched, 1)
        self.b_sched_clear = QPushButton("清除")
        self.b_sched_clear.setFixedWidth(64)
        self.b_sched_clear.setToolTip("清空定时设置")
        self.b_sched_clear.clicked.connect(
            lambda: self.f_sched.setText(""))
        sh.addWidget(self.b_sched_clear)
        add_row(full_cell("定时运行", sched_inner))

        # ---- 下载限速 / 允许时段（#22，留空即不启用，防封）----
        self.f_rate = QSpinBox(); self.f_rate.setRange(0, 600); self.f_rate.setValue(0)
        self.f_rate.setFixedWidth(96)
        self.f_rate.setToolTip("每分钟最多下载几张。0=不限。抓大站时调慢可显著降低被封概率。")
        self.f_hours = QLineEdit()
        self.f_hours.setPlaceholderText("留空=全天；22-6 = 只在晚上10点到次日6点抓")
        self.f_hours.setToolTip(
            "【允许时段 / 留空即全天】\n"
            "填 开始-结束 的小时数（0-23），例如 22-6 = 晚上 10 点到次日早上 6 点才抓。\n"
            "白天挂着任务也不影响站点；不在时段内的图会跳过，下次运行时再抓。\n"
            "留空、或首尾相同（如 0-0）= 全天不限。")
        rate_inner = QWidget(); rh2 = QHBoxLayout(rate_inner)
        rh2.setContentsMargins(0, 0, 0, 0); rh2.setSpacing(6)
        rh2.addWidget(QLabel("限速(张/分)")); rh2.addWidget(self.f_rate)
        rh2.addSpacing(12)
        rh2.addWidget(QLabel("允许时段")); rh2.addWidget(self.f_hours, 1)
        add_row(full_cell("下载限速", rate_inner))

        # ---- 图片过滤（分辨率 + 文件大小）现在「按任务保存」，放进新建任务表单 ----
        # 每个任务可以不一样；新建任务用全局配置当默认值，编辑任务用任务自己的值。
        self.f_minw = QSpinBox(); self.f_minw.setRange(0, 99999); self.f_minw.setFixedWidth(96)
        self.f_minh = QSpinBox(); self.f_minh.setRange(0, 99999); self.f_minh.setFixedWidth(96)
        self.f_min_kb = QSpinBox(); self.f_min_kb.setRange(0, 999999); self.f_min_kb.setFixedWidth(96)
        self.f_max_kb = QSpinBox(); self.f_max_kb.setRange(0, 999999); self.f_max_kb.setFixedWidth(96)
        self.f_minw.setValue(int(getattr(self.cfg, "min_width", 0) or 0))
        self.f_minh.setValue(int(getattr(self.cfg, "min_height", 0) or 0))
        self.f_min_kb.setValue(int(getattr(self.cfg, "min_bytes", 0) or 0) // 1024)
        self.f_max_kb.setValue(int(getattr(self.cfg, "max_bytes", 0) or 0) // 1024)
        for sp, tip in ((self.f_minw, "小于此宽度的图直接跳过（0=不限）"),
                       (self.f_minh, "小于此高度的图直接跳过（0=不限）"),
                       (self.f_min_kb, "小于此大小(KB)的图跳过，可滤掉占位图/图标（0=不限）"),
                       (self.f_max_kb, "大于此大小(KB)的图跳过（0=不限）")):
            sp.setToolTip(tip)
        filt_inner = QWidget(); fh = QHBoxLayout(filt_inner)
        fh.setContentsMargins(0, 0, 0, 0); fh.setSpacing(6)
        fh.addWidget(QLabel("宽≥")); fh.addWidget(self.f_minw)
        fh.addWidget(QLabel("高≥")); fh.addWidget(self.f_minh)
        fh.addWidget(QLabel("大小")); fh.addWidget(self.f_min_kb)
        fh.addWidget(QLabel("~")); fh.addWidget(self.f_max_kb)
        fh.addWidget(QLabel("KB"))
        fh.addStretch()
        add_row(full_cell("图片过滤", filt_inner))
        # ---- R53b：曾用词「全部填满」改成**每个级别各一个**，放在该级别
        #   「曾用词」三个字的**左边**（与曾用词同一行，用户最终确认版）。不再用
        #   R51 全局按钮；曾用词本身恢复成 chip 样子。严格模式移到「保存」键左边。
        # ---- 主单项6：这一行**整体顶到对话框最左**（不再套 full_cell 的 92px
        #   标签占位）—— 用户原话「左移到最左、顶住左边」。占位标签撤掉后，
        #   「全部填满」按钮与对话框左边缘齐平，四行位置一致。
        add_row(full_cell("本地AI 主题词(一级)", self.f_ai_labels))
        self.b_hist_labels = self._mk_history_button(
            "ai_label_history", self.f_ai_labels)
        v.addWidget(self.b_hist_labels)
        # R53b：一级「全部填满」已并入上方曾用词行（按钮在「曾用词」左边）
        # r29 二三级细分词：一级命中后才参与，命中→入库带标签，没命中→送审核
        # r31：二三级也各自挂一排「曾用词」（与一级分开记，别把细分词串进一级）
        add_row(full_cell("AI 细分词(二级)", self.f_ai_l2, self.f_strict2))
        self.b_hist_l2 = self._mk_history_button(
            "ai_label_history_l2", self.f_ai_l2)
        v.addWidget(self.b_hist_l2)
        # R53b：二级「全部填满」已并入上方曾用词行
        add_row(full_cell("AI 细分词(三级)", self.f_ai_l3, self.f_strict3))
        self.b_hist_l3 = self._mk_history_button(
            "ai_label_history_l3", self.f_ai_l3)
        v.addWidget(self.b_hist_l3)
        # ---- 2026-10-28：第 4 级起可无限加（每一级都能单独设严格）----
        #   前三级保持原有独立输入框（老用户习惯 + 曾用词按钮都在），
        #   再往下的层级用动态行：每行 [第N级] [输入框] [严格] [−]。
        self._deep_rows = []          # [(QWidget 行, QLineEdit, QCheckBox)]
        self._deep_box = QWidget()
        self._deep_lay = QVBoxLayout(self._deep_box)
        self._deep_lay.setContentsMargins(0, 0, 0, 0)
        self._deep_lay.setSpacing(4)
        add_row(full_cell("AI 细分词(第4级起)", self._deep_box))
        self.b_add_level = QPushButton("+ 加一级")
        self.b_add_level.setObjectName("btnAddLevel")
        self.b_add_level.setToolTip(
            "再往下加一级细分词。层数不限（最多 20 级），每一级都可以勾「严格」。")
        self.b_add_level.clicked.connect(lambda: self._add_deep_level())
        v.addWidget(self.b_add_level)
        # R53b：三级「全部填满」已并入上方曾用词行
        # 严格模式已移出本表单，放到对话框「保存」键左边（见 _open_task_form）
        add_row(full_cell("本地AI 排除词", self.f_ai_excl))
        self.b_hist_excl = self._mk_history_button(
            "ai_excl_history", self.f_ai_excl)
        v.addWidget(self.b_hist_excl)
        # R53b：黑名单「全部填满」已并入上方曾用词行
        add_row(half_cell("主题词匹配", self.f_ai_mode),
                half_cell("排除词匹配", self.f_ai_excl_mode))
        v.setSpacing(4)

        self.f_depth = QSpinBox(); self.f_depth.setMaximum(10); self.f_depth.setValue(3)
        self.f_depth.setToolTip("从网址往里点几层。2-3 层适合大多数相册站。")
        # 最大抓取页数（r29）：从原来全局唯一的一项改成每个任务各自设，
        # 放在「新建任务」表单里；0 = 不限（默认值，用户明确要求不设上限）。
        self.f_pages = QSpinBox(); self.f_pages.setRange(0, 100000)
        self.f_pages.setSingleStep(50)
        self.f_pages.setFixedWidth(96)
        self.f_pages.setValue(0)
        self.f_pages.setToolTip(
            "这个任务最多抓多少【页面】（不是图片）。\n"
            "0 = 不限（默认，抓到没有新图为止）。\n"
            "每个任务可以不一样，改一个任务不影响别的任务。\n"
            "个别站点分页极多（首页达数千页）时，可为该任务单独设置上限，"
            "比如 500；这种收紧会盖过下面「新建任务默认页数」。")
        self.f_conc = QSpinBox(); self.f_conc.setMaximum(16); self.f_conc.setValue(4)
        self.f_conc.setToolTip(
            "同时下载几张。太大容易被网站封，也会拖慢机器（本机是纯 CPU，\n"
            "AI 打分比下载更耗资源），建议 2~4，不宜超过 6。")
        self.f_delay = QDoubleSpinBox()
        self.f_delay.setRange(0.0, 30.0)
        self.f_delay.setDecimals(2)
        self.f_delay.setSingleStep(0.1)
        self.f_delay.setValue(1.0)
        self.f_delay.setToolTip(
            "每次请求的间隔秒数。数值过大会降低速度，过小易触发站点限制。\n"
            "支持小数（如 0.5 / 0.8 秒），可通过上下箭头以 0.1 步进调整。")
        self.f_priority = QSpinBox(); self.f_priority.setRange(1, 9); self.f_priority.setValue(5)
        self.f_priority.setToolTip("多个任务排队时，数值越小越先执行。")
        self.f_depth.setFixedWidth(88)
        self.f_conc.setFixedWidth(88)
        self.f_delay.setFixedWidth(96)
        self.f_priority.setFixedWidth(88)

        # ---- 底部操作行：抓取参数一行（运行交给主界面「全部开始」或每行的运行按钮）----
        bottom = QWidget(); bh = QHBoxLayout(bottom)
        bh.setContentsMargins(0, 2, 0, 0)
        bh.setSpacing(8)
        bh.addWidget(QLabel("抓取深度")); bh.addWidget(self.f_depth)
        bh.addWidget(QLabel("最大页数")); bh.addWidget(self.f_pages)
        bh.addWidget(QLabel("并发")); bh.addWidget(self.f_conc)
        bh.addWidget(QLabel("延迟(秒)")); bh.addWidget(self.f_delay)
        bh.addWidget(QLabel("优先级")); bh.addWidget(self.f_priority)
        bh.addWidget(QLabel("AI阈值")); bh.addWidget(self.f_ai_thr)
        self.b_calib = QPushButton("自动校准")
        self.b_calib.setToolTip(
            "用本任务已下载图片的 AI 分数分布，给出一个不会「全隔离」的阈值建议。"
            "先跑一次任务（哪怕结果是 0 张入库、全进隔离区）也能校准——"
            "隔离区图片同样带有评分。新建任务尚无图片时不可用。")
        self.b_calib.clicked.connect(self._calibrate_threshold)
        if getattr(self, "_editing_tid", None) is None:
            self.b_calib.setEnabled(False)
            self.b_calib.setToolTip("新建任务尚无图片，请保存并执行一次任务后再进行校准。")
        bh.addWidget(self.b_calib)
        bh.addStretch()
        v.addWidget(bottom)

        v.addStretch()

        v.addStretch()
        # 粘贴网址后自动匹配适配器（4）
        self.f_entry.textChanged.connect(lambda *_: self._auto_pick_adapter())
        return w

    def _build_gallery_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        # ---- 第一排：筛选 / 搜索 / 既有操作 ----
        row = QWidget(); rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0)
        # 按任务筛选：每个任务对应自己的图库/审核，看得清
        self.g_task_combo = QComboBox()
        self.g_task_combo.addItem("全部任务", None)
        self.g_task_combo.currentIndexChanged.connect(lambda *_: self.load_gallery())
        self.g_task_combo.setToolTip("选择要看哪个任务的图片")
        self.g_search = QLineEdit()
        self.g_search.setPlaceholderText(
            "搜索：裸词匹配标签/来源；高级语法 tag:白丝 site:pictoa "
            "score>0.5 w>=800 fav quar black -黑丝")
        self.g_search.returnPressed.connect(self.load_gallery)
        b_s = QPushButton("搜索"); b_s.clicked.connect(self.load_gallery)
        # F17：刷新同时重铺标签下拉（刚加/删过标签，新标签要能立刻选到）
        b_r = QPushButton("刷新"); b_r.clicked.connect(self.gallery_refresh)
        b_o = QPushButton("打开所在文件夹"); b_o.clicked.connect(self.open_selected_file)
        b_f = QPushButton("收藏/取消"); b_f.clicked.connect(self.toggle_favorite)
        b_ssim = QPushButton("查找相似(SSIM)"); b_ssim.clicked.connect(self.find_similar)
        # V0.0.5：「相似图聚类去重」按钮已撤 —— 重复图片功能专项取消（用户拍板：
        # 有专业软件做，不重复造轮子）。菜单与右键入口同批撤掉。
        self.g_filter = QComboBox()
        self.g_filter.addItem("全部", "all")
        self.g_filter.addItem("只看收藏", "fav")
        self.g_filter.addItem("只看未看", "unseen")
        self.g_filter.addItem("只看缺失", "missing")
        self.g_filter.setToolTip("按状态筛选图库")
        self.g_filter.currentIndexChanged.connect(lambda *_: self.load_gallery())
        # ---- F17：标签筛选（点击标签即筛）----
        # 与搜索框 tag:白丝 语法**互通**：这里选中一个标签，只是在
        # load_gallery 里往查询串末尾拼一个 ` tag:xxx`，走结构化搜索分支
        # 统一处理 —— 不另写一套筛选项，筛选栏与搜索语法永远同一口径。
        # ⚠ 含空格的标签不进下拉：tag: 语法按空格切 token，带空格的标签
        #   拼进去会被切成两个词（筛出错的东西），要筛它请直接在搜索框敲。
        self.g_tag_filter = QComboBox()
        self.g_tag_filter.setToolTip(
            "按标签筛选：选一个标签即只看带该标签的图"
            "（与搜索框 tag:白丝 语法互通；含空格的标签请用搜索框）")
        self.g_tag_filter.currentIndexChanged.connect(
            lambda *_: self.load_gallery())
        self._gal_tag_combo_filled = False
        # 需求 5：排序改成**按钮组**，重复点同一个就切倒序。
        # 原来用 QComboBox 下拉，只能选方向不能翻方向 —— 要看「最新的」和
        # 「最老的」得打开下拉重新选同一个项，等于没有反向排序。
        # 按钮组天生可重复点：同一个按钮再点一次就在 正序↔倒序 之间切，
        # 按钮文字上带箭头，当前方向一眼可见。
        # D8：排序入口换成「表头点击接管取数」+「分辨率下拉兼当排序」（D7）。
        # 原先这四个排序按钮（最新/站点/AI分/分辨率）整组撤掉，见
        # `_on_gallery_header_sort`：点表头会回写下面的 key/desc 并重新取数，
        # 翻页、换筛选都不再回弹。
        self._gal_sort_mode = "new"
        self._gal_sort_desc = True
        # ---- 方向筛选（横图/竖图/正方形）----
        self.g_orient = QComboBox()
        self.g_orient.addItem("全部方向", "all")
        self.g_orient.addItem("横图（宽>高）", "land")
        self.g_orient.addItem("竖图（高>宽）", "port")
        self.g_orient.addItem("正方形", "square")
        self.g_orient.setToolTip("按图片方向筛选：横图/竖图/正方形（需先『补充尺寸』才有数据）")
        self.g_orient.currentIndexChanged.connect(lambda *_: self.load_gallery())
        # ---- 分辨率筛选 + 排序入口（D7：一个下拉两段，分隔线隔开，不加分辨率列）----
        # 上段是「按长边分辨率筛选」，下段是排序。以前这两件事各自占地方：
        # 顶部一排最新/站点/AI分/分辨率排序按钮 + 一个分辨率筛选下拉。现在把排序
        # 收进同一个下拉，按钮组整组撤掉（见 _on_gallery_header_sort）。
        # userData 用 ("f", 档位) 表示筛选、("s", "key:方向") 表示排序。
        self.g_res = QComboBox()
        self.g_res.addItem("全部分辨率", ("f", "all"))
        self.g_res.addItem("小图 (<1280)", ("f", "s"))
        self.g_res.addItem("中图 (1280-1920)", ("f", "m"))
        self.g_res.addItem("大图 (1920-2560)", ("f", "l"))
        self.g_res.addItem("超大图 (≥2560)", ("f", "xl"))
        self.g_res.insertSeparator(self.g_res.count())
        for label, key, desc in (("最新", "new", True), ("AI分", "score", True),
                                 ("站点", "site", False), ("分辨率", "res", True)):
            self.g_res.addItem(f"排序：{label} {'↓' if desc else '↑'}",
                               ("s", f"{key}:{'desc' if desc else 'asc'}"))
        self.g_res.setToolTip(
            "上段：按长边分辨率筛选（需先『补充尺寸』才有数据）；"
            "下段：选排序键与方向（『分辨率』是排序，表格不加这一列）")
        self.g_res.currentIndexChanged.connect(
            lambda *_: self._on_gallery_res_box())
        rh.addWidget(self.g_task_combo)
        rh.addWidget(self.g_orient)
        rh.addWidget(self.g_res)
        rh.addWidget(self.g_filter)
        rh.addWidget(self.g_tag_filter)
        rh.addWidget(self.g_search); rh.addWidget(b_s); rh.addWidget(b_r)
        v.addWidget(row)

        # ---- 缺失健康检查横幅（启动发现库↔磁盘不一致时提示）----
        self.g_banner = QWidget(); gb = QHBoxLayout(self.g_banner)
        gb.setContentsMargins(10, 8, 10, 8)
        _tstyle(self.g_banner,
                "QWidget{background:{warn_bg};border:1px solid {warn_border};"
                "border-radius:8px;} QLabel{color:{warn_text};}")
        gb.setSpacing(10)
        self.g_banner_label = QLabel("")
        gb.addWidget(self.g_banner_label, 1)
        b_b_clean = QPushButton("清理缺失")
        _tstyle(b_b_clean,
                "QPushButton{background:{warn_solid};border:none;border-radius:6px;"
                "color:{on_accent};font-weight:600;padding:4px 10px;}")
        b_b_clean.clicked.connect(self.cleanup_missing)
        b_b_reimp = QPushButton("从磁盘恢复")
        _tstyle(b_b_reimp,
                "QPushButton{background:{accent_soft_hover};border:none;"
                "border-radius:6px;color:{accent_deep};font-weight:600;"
                "padding:4px 10px;}")
        b_b_reimp.clicked.connect(self.backfill_local)
        b_b_hide = QPushButton("忽略")
        _tstyle(b_b_hide,
                "QPushButton{background:transparent;border:none;color:{warn_text};}")
        b_b_hide.clicked.connect(lambda: self.g_banner.hide())
        gb.addWidget(b_b_clean); gb.addWidget(b_b_reimp); gb.addWidget(b_b_hide)
        self.g_banner.setVisible(False)
        v.addWidget(self.g_banner)

        # ---- 第二排：喜欢/不喜欢 · 视图(网格/列表) · 预览尺寸 · 缺失清理/恢复 ----
        row2 = QWidget(); rh2 = QHBoxLayout(row2); rh2.setContentsMargins(0, 0, 0, 0)
        rh2.setSpacing(6)
        b_dis = QPushButton("不喜欢")
        icons.set_themed_icon(b_dis, "thumbs-down", "{danger}", 15)
        b_dis.setIconSize(QSize(15, 15))
        b_dis.clicked.connect(self.gallery_dislike)
        self.g_mode = QPushButton("列表模式")
        icons.set_themed_icon(self.g_mode, "list", "{text_dim}", 15)
        self.g_mode.setIconSize(QSize(15, 15))
        self.g_mode.setCheckable(True)
        self.g_mode.setChecked(False)
        self.g_mode.setToolTip("切换网格缩略图 / 列表（左缩略图、右信息：分辨率/来源/打中的词）")
        self.g_mode.clicked.connect(self.toggle_gallery_mode)
        rh2.addWidget(b_dis); rh2.addWidget(self.g_mode)
        rh2.addSpacing(10)
        # r31：删掉「每行几张」（用户实测和预览一起用有 BUG）→ 列数完全由
        # 缩略图尺寸自适应，这里只留「预览」一个尺寸控件，拖多少就是多大。
        b_all = QPushButton("全选本页")
        b_all.setToolTip("选中本页全部图片（亦可使用 Ctrl / Shift 手工多选）")
        b_all.clicked.connect(self.gallery_select_all)
        b_bdis = QPushButton("批量不喜欢")
        b_bdis.clicked.connect(self.gallery_batch_dislike)
        b_del = QPushButton("批量删除")
        icons.set_themed_icon(b_del, "trash", "{danger}", 15)
        b_del.setIconSize(QSize(15, 15))
        b_del.setToolTip("删除选中的图片（先选择「全选本页」或 Ctrl / Shift 多选）；"
                         "按设置决定移入回收站还是直接永久删除")
        b_del.clicked.connect(self.gallery_batch_delete)
        rh2.addWidget(b_all)
        rh2.addWidget(b_del)
        rh2.addSpacing(10)
        rh2.addSpacing(10)
        rh2.addWidget(QLabel("预览"))
        self.s_thumb = QSlider(Qt.Orientation.Horizontal)
        self.s_thumb.setRange(64, 360)
        self.s_thumb.setValue(int(getattr(self.cfg, "gallery_thumb_size", 128)))
        self.s_thumb.setFixedWidth(120)
        self.s_thumb.setToolTip("图库缩略图尺寸，**唯一**的尺寸控件：拖到多少就是多大，"
                                 "一行放几张由软件自己算（最大化窗口会自动放大）。"
                                 "图库「列表模式」下拖它=改右侧预览图大小")
        self.s_thumb.valueChanged.connect(self.set_gallery_thumb_size)
        rh2.addWidget(self.s_thumb)
        self.l_thumb = QLabel(f"{self.s_thumb.value()}px")
        self.l_thumb.setFixedWidth(40)
        self.s_thumb.valueChanged.connect(
            lambda v: self.l_thumb.setText(f"{v}px"))
        rh2.addWidget(self.l_thumb)
        # ---- #42：水印不透明度临时候选滑条 + 百分比显示 ----
        # 用户试色 demo 定的默认是 178（＝70%，见 watermark.ALPHA）。滑条只是
        # 「临时」的：它不新建设置页，就地改 watermark 的 alpha 并写进
        # config.json 的 watermark_alpha，重开软件还在。
        from .watermark import ALPHA as _WM_DEFAULT
        self.s_wm = QSlider(Qt.Orientation.Horizontal)
        self.s_wm.setRange(0, 255)
        self.s_wm.setValue(int(getattr(self.cfg, "watermark_alpha", _WM_DEFAULT)
                               or _WM_DEFAULT))
        # ⚠ R48-A3：`setValue` 必须在 connect **之后**再刷一次水印，否则
        # 「配置里的透明度」与「控件实际 alpha」不一致（用户反馈审核/图库
        # 透明度有问题）。控件在别处已按同一值建好，这里只是保证二者同步。
        self._apply_watermark_alpha(self.s_wm.value())
        self.s_wm.setFixedWidth(120)
        self.s_wm.setToolTip(
            f"水印不透明度（默认 {_WM_DEFAULT}，即 70%）："
            "拖动调整可见程度，0 表示完全不可见；"
            "该值保存于 config.json 的 watermark_alpha，重启后仍生效"
            "（如需修改默认值，请调整 watermark.ALPHA）。")
        self.l_wm = QLabel(self._wm_pct(self.s_wm.value()))
        self.l_wm.setFixedWidth(46)
        self.s_wm.valueChanged.connect(
            lambda v: self.l_wm.setText(self._wm_pct(v)))
        self.s_wm.valueChanged.connect(self._apply_watermark_alpha)
        self.s_wm.sliderReleased.connect(self._save_watermark_alpha)
        # 2026-10-27 用户要求：**界面上不要再出现水印透明度滑条**
        # （水印以后会整体取消）。控件对象保留（别的代码可能引用），
        # 但不再塞进工具栏 —— 用户看不到就等于没有。
        # rh2.addWidget(QLabel("水印"))
        # rh2.addWidget(self.s_wm)
        # rh2.addWidget(self.l_wm)
        b_clean = QPushButton("清理缺失")
        icons.set_themed_icon(b_clean, "trash", "{danger}", 15)
        b_clean.setIconSize(QSize(15, 15))
        b_clean.setToolTip("删除图库里指向『磁盘已删除文件』的残留记录（操作前自动备份数据库）")
        b_clean.clicked.connect(self.cleanup_missing)
        b_reimp = QPushButton("从磁盘恢复图库")
        icons.set_themed_icon(b_reimp, "refresh", "{text_dim}", 15)
        b_reimp.setToolTip("扫描各任务保存目录，把磁盘上已有但没进图库的图片补回来（文件已删则无效）")
        b_reimp.clicked.connect(self.backfill_local)
        b_learn = QPushButton("从文件夹学习…")
        icons.set_themed_icon(b_learn, "image", "{accent}", 15)
        b_learn.setIconSize(QSize(15, 15))
        b_learn.setToolTip("选择本地图片文件夹，使用本地 CLIP 批量标注并入库（纯离线，不消耗额度）。"
                           "适用于将自有图库导入学习：导入后在图库中浏览、标记喜欢或不喜欢即可训练偏好")
        _tstyle(b_learn,
                "QPushButton{background:{accent_soft};border:none;border-radius:7px;"
                "color:{accent_deep};font-size:13px;font-weight:600;}"
                "QPushButton:hover{background:{accent_soft_hover};}")
        b_learn.clicked.connect(self.learn_from_folder)
        rh2.addStretch()
        v.addWidget(row2)

        self.g_grid = _GalleryList()
        # R134：图库网格改用与审核页一致的 apply_grid_layout（统一 gridSize /
        # spacing / 禁拖拽），修掉 crop=False 变高缩略图在 IconMode 下缺 gridSize
        # 导致的格子错位 / 点击落点偏移（"图库网格不能点击"）。
        # apply_grid_layout 内部已设 view mode / resize / movement / iconSize /
        # gridSize / spacing / 禁拖 + 选中委托，无需再单独挂。
        apply_grid_layout(self.g_grid, 128)
        # 多选：Ctrl / Shift 多选后可批量喜欢 / 不喜欢
        self.g_grid.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        # #46：图库双击**开灯箱**（Web 与桌面端行为统一为灯箱）。
        # 这推翻了 #26 的「图库双击走系统看图器」—— 用户新决定是灯箱。
        # ⚠ 灯箱是 on_decide=None 的纯浏览模式：四向都只翻页，
        #   ← → 不会被吃成「喜欢/不喜欢」（那是审核页灯箱才有的语义）。
        self.g_grid.itemDoubleClicked.connect(self.open_lightbox)
        self.g_grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.g_grid.customContextMenuRequested.connect(self._gallery_menu)
        v.addWidget(self.g_grid)
        # ---- 列表模式（与旧版隔离区同款：表格 + 右侧预览）----
        from .table_view import make_table
        self.g_table = make_table(
            on_like=self.gallery_batch_like, on_dislike=self.gallery_batch_dislike)
        # D8：点表头不再只做内存重排，交回主窗口重新取数（见下方同名方法）
        self.g_table._header_sort_hook = self._on_gallery_header_sort
        self.g_table.itemDoubleClicked = None  # QTableWidget 用 cellDoubleClicked
        self.g_table.cellDoubleClicked.connect(
            lambda r, c: self.open_lightbox())
        self.g_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.g_table.customContextMenuRequested.connect(self._gallery_menu)
        prev_box = QWidget()
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(8, 0, 0, 0)
        pv.setSpacing(6)
        pv.addWidget(QLabel("<b>预览</b>"))
        # r31：与审核页同一个自适应预览控件（以前写死 240×240 → 拖大只留白）
        from .table_view import PreviewLabel
        self.g_prev_img = PreviewLabel("（选中一行看预览）")
        self.g_prev_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(self.g_prev_img,
                "QLabel{background:{surface_alt};border:1px solid {border};"
                "border-radius:8px;color:{text_dim};}")
        pv.addWidget(self.g_prev_img, 1)
        # R148：列表模式预览图同样支持旋转（与灯箱同逻辑；只改显示不写原文件）
        _prev_btn_row = QHBoxLayout()
        _b_prev_rot = QPushButton("旋转 90° (R)")
        _b_prev_rot.setToolTip("顺时针旋转预览图 90°；只改显示、不写原文件")
        _b_prev_rot.clicked.connect(lambda: self.g_prev_img.rotate_cw())
        _prev_btn_row.addStretch(1)
        _prev_btn_row.addWidget(_b_prev_rot)
        pv.addLayout(_prev_btn_row)
        self.g_prev_txt = QLabel("")
        self.g_prev_txt.setWordWrap(True)
        _tstyle(self.g_prev_txt, "QLabel{color:{text_dim};font-size:12px;}")
        pv.addWidget(self.g_prev_txt)
        self.g_split = QSplitter()
        self.g_split.addWidget(self.g_table)
        self.g_split.addWidget(prev_box)
        self.g_split.setStretchFactor(0, 1)
        self.g_split.setStretchFactor(1, 0)
        self.g_split.setVisible(False)
        self.g_table.itemSelectionChanged.connect(self._gallery_table_preview)
        v.addWidget(self.g_split, 1)
        # ---- 分页：大库（几千张）不必一次性加载全部缩略图 ----
        self._gal_page = 0
        self._gal_base_sig = None
        # #39：分页条是网格/列表共用的公共块，以前只是局部变量，切到列表模式
        # 还杵在那儿冒出「上一页/下一页」——存成属性，好按当前视图模式显隐。
        self.g_pager = QWidget(); pg = QHBoxLayout(self.g_pager)
        pg.setContentsMargins(0, 4, 0, 0)
        self.g_prev = QPushButton("← 上一页")
        self.g_prev.clicked.connect(lambda: self._gal_page_move(-1))
        self.g_next = QPushButton("下一页 →")
        self.g_next.clicked.connect(lambda: self._gal_page_move(1))
        self.g_pageno = QLabel("第 1 页")
        pg.addWidget(self.g_prev)
        pg.addWidget(self.g_pageno)
        pg.addWidget(self.g_next)
        pg.addStretch()
        v.addWidget(self.g_pager)
        self.g_info = QLabel("共 0 张")
        v.addWidget(self.g_info)
        self.g_list_mode = False
        self._apply_grid()
        return w

    def _stats_schedule(self, delay_s: float):
        """排一个尾部补算；已经排过就不再重复排。"""
        if getattr(self, "_stats_defer_timer", None) is not None:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)

        def _fire():
            self._stats_defer_timer = None
            self.refresh_stats(force=True)

        timer.timeout.connect(_fire)
        self._stats_defer_timer = timer
        timer.start(int(max(0.0, delay_s * 1000)))

    def _set_stats_text(self, html: str):
        """写统计文本；控件已销毁就跳过。

        r40 第 4 轮扫描实测：日志与统计合并成一页后，统计页的 QLabel 也会
        随整棵子树被释放，refresh_stats 仍被队列事件驱动着调 → 撞
        `QLabel already deleted`。统计只是显示，死了不能掀翻调用方。
        """
        import shiboken6
        lbl = getattr(self, "stats_text", None)
        if lbl is None:
            return
        try:
            if not shiboken6.isValid(lbl):
                return
            lbl.setText(html)
        except RuntimeError:
            pass

    def refresh_stats(self, force: bool = False):
        # R152-A7：stats_text 已按需求 21 从界面撤掉（setHidden(True) 不进
        # 布局）—— 看不见还要每 3 秒（STATS_MIN_INTERVAL）全表 SELECT +
        # 逐目录 scandir 算一遍，大库上是可感知的周期性卡顿，纯浪费。
        # 控件隐藏即直接 return；导出报告（export_report）自己现算
        # library_stats，不受影响。哪天统计挂回界面，isHidden() 自会放行。
        lbl = getattr(self, "stats_text", None)
        if lbl is not None:
            try:
                if lbl.isHidden():
                    return
            except Exception:
                pass
        if not force:
            now = time.monotonic()
            # 首帧必须真算：初值不能用 0.0 —— time.monotonic() 是从进程启动
            # 起算的，测试/冷启动那几秒内 0.0 会让第一次刷新被自己节流掉。
            last = getattr(self, "_stats_at", None)
            if last is not None and now - last < self.STATS_MIN_INTERVAL:
                self._stats_schedule(self.STATS_MIN_INTERVAL - (now - last))
                return
            self._stats_at = now
        try:
            s = library_stats(self.db, self.cfg.library_dir)
        except Exception as e:
            self._set_stats_text(f"统计失败: {e}")
            return
        ext = "、".join(f"{k}×{v}" for k, v in list(s["by_ext"].items())[:8]) or "-"
        last = ""
        if self._queue:
            reps = self._queue.snapshot()["reports"]
            if reps:
                r = list(reps.values())[-1]
                speed = r.get("elapsed") and (r.get("downloaded", 0) / r["elapsed"])
                last = (f"<br><b>上次运行</b>: 任务#{r['task_id']} 下载"
                        f"{r.get('downloaded',0)}张 / {r.get('elapsed',0):.1f}s"
                        + (f"（{speed:.1f} 张/秒）" if speed else ""))
        fails = self._queue.snapshot()["failed"] if self._queue else {}
        fail_txt = "、".join(f"#{k}:{v[:40]}" for k, v in fails.items()) or "无"
        # 需求 21：把「按任务的重复率 + 平均AI分」分布加进来。
        # 原来只有「任务分布 #20:1647」这种张数，看不出哪个站点值得继续抓。
        # 现在每个任务一行：张数 / 重复率 / 平均AI分 / 隔离数，
        # 重复率高或均分低的一眼可见 —— 这才是选任务时真正要看的。
        q = s.get("per_task_quality") or {}
        names = {}
        try:
            for r in self.db.query("SELECT id, name FROM tasks"):
                names[int(r["id"])] = (r["name"] or "").strip()
        except Exception:
            pass
        rows_html = []
        for tid, d in q.items():
            nm = names.get(int(tid), "")
            label = f"#{tid}" + (f" {nm[:14]}" if nm else "")
            # 均分低 → 标红；重复率高 → 标橙。颜色与数字同排，扫一眼就有结论。
            # P1-4：三档色改从调色板取（原先写死绿/橙/红）。这三档表达的是
            # **好坏**，所以刻意不跟随皮肤（grade_* 语义键，见 theme.PALETTE）——
            # 跟皮肤走的话换肤后「合格」可能和强调色撞在一起，结论就读不出来了。
            _dark = bool(getattr(getattr(self, "cfg", None), "dark_mode", False))
            avg = float(d.get("avg_score") or 0)
            dr = float(d.get("dup_rate") or 0)
            avg_c = (_tc(_dark, "grade_ok", "#22c55e") if avg >= 0.5
                     else (_tc(_dark, "grade_warn", "#f59e0b") if avg >= 0.35
                           else _tc(_dark, "grade_bad", "#ef4444")))
            dup_c = (_tc(_dark, "grade_ok", "#22c55e") if dr < 1
                     else (_tc(_dark, "grade_warn", "#f59e0b") if dr < 5
                           else _tc(_dark, "grade_bad", "#ef4444")))
            rows_html.append(
                f"{label}: {d.get('n', 0)}张 · "
                f"重复 <span style='color:{dup_c}'>{dr:.1f}%</span> · "
                f"均分 <span style='color:{avg_c}'>{avg:.2f}</span> · "
                f"隔离 {d.get('quar', 0)}")
        dist = "<br>".join(rows_html) or "（暂无已入库的任务）"

        self._set_stats_text(
            f"<b>图库</b> {s['total_records']} 张（磁盘 {s['on_disk']}） · "
            f"占用 <b>{s['bytes_human']}</b> · 格式 {ext}<br>"
            f"隔离 {s['quarantined']} · 黑名单 {s['blacklisted']} · "
            f"收藏 {s['favorites']} · 全局重复率 {s['duplicate_rate_pct']}% · "
            f"平均AI分 {s['avg_score']:.2f}{last}<br>"
            f"<b>任务分布（重复率 / 平均AI分）</b><br>{dist}<br>"
            f"<b>失败任务</b>: {fail_txt}")

    def _bg_submit(self, fn, cb, label=""):
        """把重活丢到自持线程池，结果回主线程回调（r39 通用小工具）。

        - 沿用 r36 铁律：**不用 `asyncio.to_thread`**（解释器默认 executor 在退出
          阶段已 shutdown，会抛 ``cannot schedule new futures after shutdown``）。
        - 沿用 r35 铁律：后台线程三道闸（窗口已关 / 库已关 / 回调自己抛错），
          只记日志，绝不把主线程带崩。
        - 目前用于「查找相似(SSIM)」这类几十秒的 CPU/IO 重活，其它 UI 里的
          同步重活（以后发现）都该走这里。
        """
        if getattr(self, "_closing", False):
            return
        try:
            if self.db is not None and self.db.is_closed():
                return
        except Exception:
            return
        pool = getattr(self, "_bg_pool", None)
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = ThreadPoolExecutor(max_workers=4,
                                      thread_name_prefix="mrm-bg")
            self._bg_pool = pool

        def _run():
            # r39c：窗口已经在关了就别再干活 —— 跑完再碰数据库/日志，
            # 就是「解释器收尾时后台线程把整个进程带走」那条 0xC0000409。
            if getattr(self, "_closing", False):
                return None, None
            try:
                return fn(), None
            except Exception as e:
                return None, e

        def _done(fut):
            try:
                res, err = fut.result()
            except Exception as e:
                res, err = None, e
            # 结果 ev 回主线程：Qt 对象（对话框/表格）只能在 GUI 线程碰
            try:
                self.bg_result.emit((cb, res, err))
            except RuntimeError:
                pass   # 窗口正在销毁， receiver 没了，忽略

        try:
            pool.submit(_run).add_done_callback(_done)
        except Exception as e:
            self.log_signal.emit(
                f"{label or '后台任务'}提交失败: {type(e).__name__}: {e}")

    def _on_bg_result(self, payload):
        """后台任务结果（主线程）——真正调回调、动 UI 的地方。"""
        cb, res, err = payload
        if getattr(self, "_closing", False):
            return
        try:
            if self.db is not None and self.db.is_closed():
                return
        except Exception:
            return
        if err is not None:
            self.log_signal.emit(
                f"{getattr(self, '_bg_label', '后台任务') or '后台任务'}失败: "
                f"{type(err).__name__}: {err}")
            return
        try:
            cb(res)
        except Exception as e:
            self.log_signal.emit(
                f"后台任务结果处理失败: {type(e).__name__}: {e}")

    def find_similar(self):
        """查找相似（SSIM）。

        **r39**：聚类是 O(n²) 的 ``cv2.imread``（150 张 ≈ 上万次解码）。以前在
        槽函数里同步跑，真机全按钮扫描实测把主线程堵了 **35s+**，看门狗直接判
        STALL。现在丢到自持线程池里算，主线程只收结果、建对话框（「保留哪张」
        也已在后台一次算好），界面全程不卡。
        """
        if getattr(self, "_ssim_busy", False):
            return
        self._ssim_busy = True

        def _calc():
            return find_similar_groups(self.db)

        def _show(groups):
            self._ssim_busy = False
            if not groups:
                QMessageBox.information(self, "查找相似", "没有发现相似图片组")
                return
            dlg = SimilarDialog(groups, self.cfg.isolate_dir,
                                on_done=self.load_gallery, parent=self,
                                db=self.db)      # R152-A5：回写 images 表
            dlg.exec()
            self.refresh_stats()

        self.log_signal.emit("正在比对相似图片（大图几十秒，界面不卡）…")
        self._bg_submit(_calc, _show)

    def toggle_live_wall(self, checked: bool):
        """【C7】实时抓图墙已移除，这里只把配置记下来就返回。

        **故意保留这个方法**：老配置里可能有 ``live_wall`` 字段，设置页/
        外部脚本也可能还调它。直接删方法会让那些调用方 AttributeError，
        而它们要的只是「别再显示抓图墙」—— 空实现正好满足，且不用改调用点。
        """
        self.cfg.live_wall = False
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass

    def set_gallery_refresh(self, seconds: int):
        """图库自动刷新间隔（0=关闭）。定时器靠 load_gallery 的签名比对，
        内容没变化时零重绘，所以即使 5 秒一次也不费资源。"""
        self.cfg.gallery_refresh_sec = int(seconds)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        if int(seconds) > 0:
            self._gal_timer.start(int(seconds) * 1000)
        else:
            self._gal_timer.stop()
        self.log_signal.emit(
            "图库自动刷新: " + ("关闭" if seconds <= 0 else f"{seconds} 秒"))

    def _models_base_dir(self) -> Path:
        """AI 模型根目录 = 数据目录/models。

        放数据目录（而不是 exe 旁边）有两个理由：便携模式下 software/data 会
        跟着软件目录走，配置里已有 ai_model_path 可覆盖具体用哪个模型，
        根目录只是「下载落到哪」——统一走 user_data_dir()，别自己拼路径。
        """
        from ..config import user_data_dir
        try:
            override = str(getattr(self.cfg, "models_dir", "") or "").strip()
            if override:
                return Path(override)
        except Exception:
            pass
        return user_data_dir() / "models"

    def _autopoint_default_model(self) -> Optional[str]:
        """C8：首次启动自动指向默认模型，用户不必手动配路径。

        用户反馈「内置 clip 是空的」—— 打开设置看见空目录 = 功能不可用。
        这里在启动时找一次可用模型（捆绑 → 已下载 → 任意就绪项），
        找到就填进 ``cfg.ai_model_path`` 并落盘，下次启动直接可用。

        **只填空位，不覆盖用户的选择**：``ai_model_path`` 或 ``ai_backend``
        已经被用户改过（local/api）就一律不动 —— 软件不该替用户做决定。
        找不到模型也**安静返回**（离线/未捆绑是正常情况，不是错误）。
        """
        try:
            from ..models_lib import resolve_default_model_dir
            # 用户已有选择 → 不动
            if str(getattr(self.cfg, "ai_model_path", "") or "").strip():
                return None
            backend = str(getattr(self.cfg, "ai_backend", "builtin") or "builtin")
            if backend not in ("builtin", "", None):
                return None
            d = resolve_default_model_dir(self._models_base_dir())
            if d is None:
                return None
            self.cfg.ai_model_path = str(d)
            self._save_cfg_quiet()
            self.log_signal.emit(
                f"已自动指向默认 AI 模型：{Path(d).name}")
            return str(d)
        except Exception:
            # 模型解析失败绝不该影响启动
            return None

    def _build_model_manager(self, dlg, path_in):
        """需求 9 / R172：AI 模型库面板（卡片行 + 状态徽章 + 行内进度）。

        原来只有一个「本地模型目录」输入框，想换模型得自己去网上找、
        下完再手填路径。这里列出多个可选模型，选择「下载」自动拉取，
        下完能一键设为当前使用的模型。下载走后台线程（几百 MB）。

        R172 重排（用户：「太反人类，重新设计，更简洁」）：
          · 每个模型一**卡片行**：左边名称 + 一句话优点，右边状态徽章 + 主按钮；
          · 硬件门槛（最低 CPU/GPU/显存/输入尺寸）收进「详情」折叠 —— 原来
            一整行 `最低 CPU…最低 GPU…显存…` 堆在卡片里，用户说满屏术语；
          · 「使用中」那一行高亮、按钮变「✓ 当前」且禁用；
          · 下载进度只在**正在下载的那一行**内嵌显示，不占全局（原来是一个
            全局进度条挂在列表上方，看的人不知道在给谁下载）；
          · 文案去术语化。
        """
        from ..models_lib import (CATALOG, is_ready, model_dir,
                                  disk_usage, DownloadWorker, remove_model,
                                  sorted_catalog, hardware_line,
                                  bundled_ready, gpu_switch_text)
        from .theme import c as _c   # P1-4：富文本不是 QSS，直接取调色板
        from PySide6.QtWidgets import (QVBoxLayout, QLabel, QProgressBar,
                                       QPushButton as _BP, QFrame as _QF,
                                       QToolButton)
        base = self._models_base_dir()
        base.mkdir(parents=True, exist_ok=True)

        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 4, 0, 0)
        v.setSpacing(4)
        t = QLabel("选择「下载」自动获取，下完选择「设为当前」启用。")
        _tstyle(t, "color:{text_dim};")
        v.addWidget(t)

        # R172：进度条**按行**挂（每个未下载的模型各有一条，默认隐藏）。
        # 旧版是全局一条挂在列表顶部 —— 用户看不出在给谁下载。
        _dl_bars: dict = {}
        # R172：全局那条只留「下载中/失败」的汇总文本（不再显示百分比）。
        status = QLabel("")
        status.setWordWrap(True)
        _tstyle(status, "color:{text_dim};font-size:11px;")
        v.addWidget(status)

        # R149-A：失败时可一键复制错误信息（含已试来源），方便排查。
        b_copy_err = _BP("复制错误信息")
        b_copy_err.setVisible(False)
        v.addWidget(b_copy_err)

        # 自持引用：worker 跑完前对话框可能被关掉，属性挂到窗口上防 GC
        self._model_dl_worker = None

        _use_btns: dict = {}
        _use_rows: dict = {}
        # R172：自建（不在清单里）的那几行按**显式目录**判定「当前」——
        # 拿目录名去 `is_ready` 问永远问不出结果，行高亮/按钮态就永远是灰的。
        _row_dirs: dict = {}

        def _current_path():
            try:
                cur = str(path_in.text() or "").strip()
                return Path(cur).resolve() if cur else None
            except Exception:
                return None

        def _refresh_use_btns(msg=None):
            """按「当前是不是这一档」重挂徽章与按钮态（R172-3 的视觉闭环）。

            这是「设为当前没反馈」的根治点：以前 `_use_model` 只改文本框 +
            发一条日志，按钮/行样式一点没动，用户不知道选没选中。
            现在把这个刷新收成**一个函数**，点按钮与保存后都调它。

            `msg` 不为空时顺手把这句人话写到列表上方的状态行 —— 模态对话框
            盖住了主窗口状态栏，只发 log_signal 用户根本看不见。
            """
            if msg:
                try:
                    status.setText(str(msg))
                except RuntimeError:
                    pass
            want = _current_path()
            for mid, btn in _use_btns.items():
                try:
                    if mid in _row_dirs:
                        # 自建档：目录已知，只看文本框指不指向它。
                        usable = True
                        is_cur = bool(want is not None
                                      and _row_dirs[mid] == want)
                    else:
                        ready = is_ready(base, mid)
                        usable = ready or bundled_ready(mid)
                        is_cur = bool(want is not None and usable
                                      and _model_dir_resolved(mid) == want)
                    btn.setText("✓ 当前" if is_cur else "设为当前")
                    btn.setEnabled(usable and not is_cur)
                    row = _use_rows.get(mid)
                    if row is not None:
                        row.setProperty("mrmCurrent", "1" if is_cur else "0")
                        row.style().unpolish(row)
                        row.style().polish(row)
                except RuntimeError:
                    pass

        def _model_dir_resolved(mid):
            """这一档模型对应哪个目录（内置档指包内目录）。"""
            try:
                if is_ready(base, mid):
                    return Path(model_dir(base, mid)).resolve()
                if bundled_ready(mid):
                    from ..models_lib import bundled_models_dir
                    return (Path(bundled_models_dir()) / mid).resolve()
                return Path(model_dir(base, mid)).resolve()
            except Exception:
                return None

        def _human(n):
            for u in ("B", "KB", "MB", "GB"):
                if n < 1024:
                    return f"{n:.0f} {u}"
                n /= 1024
            return f"{n:.1f} TB"

        # R86 #51 + R172：4 档 CLIP 排前面。卡片行 = 名称 + 一句话优点 +
        # 状态徽章 + 主按钮；门槛数据收进「详情」折叠（不再满屏术语）。
        for m in sorted_catalog():
            row = _QF()
            row.setObjectName("modelCard")
            row.setProperty("mrmCurrent", "0")
            rh = QHBoxLayout(row)
            rh.setContentsMargins(8, 6, 8, 6)
            rh.setSpacing(6)
            mid = m["id"]
            ready = is_ready(base, mid)
            built_in = (not ready) and bundled_ready(mid)

            # 左列：名称 + 一句话优点（+ 状态徽章）
            left = QVBoxLayout()
            left.setContentsMargins(0, 0, 0, 0)
            left.setSpacing(1)
            name_row = QHBoxLayout()
            name_row.setContentsMargins(0, 0, 0, 0)
            name_row.setSpacing(6)
            nm = QLabel(f"<b>{m['name']}</b>")
            nm.setTextFormat(Qt.TextFormat.RichText)
            name_row.addWidget(nm)
            if ready or built_in:
                badge = QLabel("已就绪" if ready else "已内置")
                _tstyle(badge, "color:{ok};font-size:11px;font-weight:600;")
                name_row.addWidget(badge)
            name_row.addStretch(1)
            left.addLayout(name_row)
            pros = QLabel(f"{m.get('pros') or '—'}　·　{m['size_hint']}")
            _tstyle(pros, "color:{text_dim};font-size:11px;")
            pros.setWordWrap(True)
            left.addWidget(pros)
            # 「详情」折叠：门槛 + 输入尺寸 + 备注，默认收起
            det = QToolButton()
            det.setText("详情")
            det.setCheckable(True)
            det.setArrowType(Qt.ArrowType.RightArrow)
            det.setAutoRaise(True)
            _tstyle(det, "QToolButton{color:{text_dim};font-size:11px;"
                         "border:none;}")
            det_line = QLabel(
                f"{hardware_line(m)}"
                + (f"　输入 {m['input']}px" if m.get("input") else "")
                + (f"<br>{m['note']}" if m.get("note") else ""))
            det_line.setWordWrap(True)
            _tstyle(det_line, "color:{text_dim};font-size:11px;")
            det_line.setVisible(False)

            def _toggle_detail(checked, lbl=det_line, btn=det):
                lbl.setVisible(bool(checked))
                btn.setArrowType(Qt.ArrowType.DownArrow if checked
                                 else Qt.ArrowType.RightArrow)
            det.toggled.connect(_toggle_detail)
            left.addWidget(det)
            left.addWidget(det_line)
            # 行内下载进度（只在这一行显示，默认隐藏）
            pbar = QProgressBar()
            pbar.setRange(0, 100)
            pbar.setVisible(False)
            pbar.setTextVisible(True)
            left.addWidget(pbar)
            _dl_bars[mid] = pbar
            rh.addLayout(left, 1)

            b_use = _BP("设为当前")
            b_use.setEnabled(ready or built_in)
            b_use.setToolTip("把这个模型设为当前使用的本地模型（点完立刻变「✓ 当前」）")
            b_use.clicked.connect(
                lambda _=False, i=mid, nm2=m["name"]: self._use_model(
                    i, path_in, on_done=_refresh_use_btns, name=nm2))
            rh.addWidget(b_use)
            _use_btns[mid] = b_use
            _use_rows[mid] = row

            if ready:
                b_dl = _BP("已下载")
                b_dl.setEnabled(False)
            elif built_in:
                # 随软件一起装的那一档：默认包只带它，不需要也不该重复下载。
                b_dl = _BP("已内置")
                b_dl.setEnabled(False)
                b_dl.setToolTip("随软件内置，无需下载")
            else:
                b_dl = _BP("下载")
                b_dl.clicked.connect(
                    lambda _=False, i=mid, nm2=m["name"], pb=pbar: (
                        self._start_model_dl(
                            base, i, nm2, pb, status, _refresh_use_btns,
                            copy_btn=b_copy_err)))
            rh.addWidget(b_dl)

            b_del = _BP("删除")
            b_del.setEnabled(ready)
            b_del.clicked.connect(
                lambda _=False, i=mid: (remove_model(base, i),
                                        _refresh_use_btns(),
                                        status.setText(f"已删除 {i}")))
            rh.addWidget(b_del)
            v.addWidget(row)

        # 本地自建（不在清单里）也列出来
        from ..models_lib import local_dirs
        for mid, dpath, ready in local_dirs(base):
            if mid in {m["id"] for m in CATALOG}:
                continue
            row = _QF()
            row.setObjectName("modelCard")
            row.setProperty("mrmCurrent", "0")
            rh = QHBoxLayout(row)
            rh.setContentsMargins(8, 6, 8, 6)
            lbl = QLabel(f"<b>{dpath.name}</b>（自建）"
                         f"<br><span style='color:%s;font-size:11px'>"
                         % _c(False, "text_dim")
                         + f"{dpath}</span>")
            lbl.setWordWrap(True)
            rh.addWidget(lbl, 1)
            b_use = _BP("设为当前")
            b_use.setToolTip("把这个本地目录设为当前使用的模型（点完立刻变「✓ 当前」）")

            def _use_local(_=False, pp=dpath, nm=dpath.name):
                # R172-3：自建档与清单档走**同一套反馈**（行高亮 + 按钮态 +
                # 状态行），不然用户又遇到「点了没反应」。
                try:
                    path_in.setText(str(pp))
                    self.log_signal.emit(f"已设为当前：{nm}（保存后生效）")
                    _refresh_use_btns(f"已设为当前：{nm}（保存后生效）")
                except RuntimeError:
                    pass
            b_use.clicked.connect(_use_local)
            rh.addWidget(b_use)
            _use_btns[dpath.name] = b_use
            _use_rows[dpath.name] = row
            _row_dirs[dpath.name] = Path(dpath).resolve()
            v.addWidget(row)

        use_line = _QF()
        use_line.setFrameShape(_QF.Shape.HLine)
        v.addWidget(use_line)
        v.addWidget(QLabel(f"模型目录：{base}　当前占用："
                           f"{_human(disk_usage(base))}"))
        # R172：GPUC 开关的「实际状态」也要跟着刷新（开关本身在对话框顶部）。
        _refresh_use_btns()
        # 内部状态挂到 host 上：① 「设为当前」的回调要能刷这几张卡；
        # ② 真机排查与护栏（真跑控件、看按钮 text/disabled 与行 property）
        #    需要拿到这些对象 —— 只憋在闭包里就只能靠 grep 源码断言，
        #    而那种判据抓不到「按钮文案/禁用态整个失效」（R172 变异实测漏过）。
        host._mrm_refresh_use = _refresh_use_btns
        host._mrm_use_btns = _use_btns
        host._mrm_use_rows = _use_rows
        host._mrm_row_dirs = _row_dirs
        host._mrm_status = status
        return host

    def _use_model(self, model_id: str, path_in, on_done=None, name=None):
        """把下载好的模型设为当前使用的本地模型。

        R86 #51：没下载但**随软件内置**的那一档（CLIP ViT-B/32）也要能
        「设为当前」—— 以前只认已下载目录，内置档点了没反应。

        R172-3（「设为当前」无反馈）：改完路径立刻回调 `on_done` 去刷行高亮
        与按钮态。以前这里只 `setText` + 发日志，对话框里看不出任何变化，
        用户根本不知道选没选中 —— 这正是订单点名的真机 bug。
        """
        from ..models_lib import model_dir, is_ready, bundled_ready
        from ..models_lib import bundled_models_dir
        base = self._models_base_dir()
        if is_ready(base, model_id):
            p = model_dir(base, model_id)
        elif bundled_ready(model_id):
            p = bundled_models_dir() / model_id
        else:
            p = model_dir(base, model_id)
        shown = name or model_id
        try:
            path_in.setText(str(p))
            self.log_signal.emit(f"已设为当前：{shown}（保存后生效）")
            # 主窗口状态栏：对话框关掉/被盖住时，用户回到主界面也看得到。
            try:
                self.statusBar().showMessage(
                    f"已设为当前：{shown}（保存后生效）", 4000)
            except Exception:
                pass
            if callable(on_done):
                on_done(f"已设为当前：{shown}（保存后生效）")
        except RuntimeError:
            pass

    def _start_model_dl(self, base, model_id, name, bar, status, done_cb,
                        copy_btn=None):
        """启动后台下载。已有任务在跑就拒绝（别把带宽/磁盘搅乱）。"""
        from ..models_lib import DownloadWorker
        from PySide6.QtWidgets import QApplication
        w = getattr(self, "_model_dl_worker", None)
        if w is not None and w.is_alive():
            QMessageBox.information(self, "模型下载",
                                    "已有一个模型在下载，请等它完成。")
            return
        bar.setVisible(True)
        bar.setValue(0)
        status.setText(f"正在下载 {name} …")
        if copy_btn is not None:
            copy_btn.setVisible(False)
            copy_btn.setText("复制错误信息")
        tried: list[str] = []

        def _on_prog(rel, done, total, src=None):
            # 后台线程 → 主线程刷新进度条；显示「当前来源 + 进度」。
            if src and src not in tried:
                tried.append(src)
            cur = f"　来源：{src}" if src else ""
            tried_txt = f"　已试：{'、'.join(tried)}" if tried else ""
            # R152-A1：同样发三元组 (cb, res, err)（详见下面 _run 的注释）——
            # 以前是裸 lambda，进度条/来源显示全不刷新。
            # R168：**必须**接收并忽略位置参数 —— _on_bg_result 统一按
            # cb(res) 传 1 参，这里的 0 参 lambda 每收一次进度就 TypeError
            # （被 except 吞掉 ⇒ 进度条永不刷新）。
            self.bg_result.emit((lambda *_a, d=rel, dn=done, t=total, c=cur,
                                 tt=tried_txt: (
                bar.setValue(int(dn * 100 / t) if t else 0),
                status.setText(f"{name}：{d}  "
                               f"{dn/1048576:.1f}/{(t or 0)/1048576:.1f} MB"
                               f"{c}{tt}")), None, None))

        def _fin(ok, msg):
            bar.setVisible(False)
            status.setText(msg)
            if not ok and copy_btn is not None:
                # 失败：显示复制按钮，便于把含「已试来源」的错误信息发出去排查。
                copy_btn.setVisible(True)
                copy_btn.setText("复制错误信息")
                copy_btn.disconnect()
                copy_btn.clicked.connect(
                    lambda: (QApplication.clipboard().setText(msg),
                             copy_btn.setText("已复制 ✅")))
            else:
                if copy_btn is not None:
                    copy_btn.setVisible(False)
            try:
                done_cb()
            except Exception:
                pass
            if ok:
                QMessageBox.information(self, "模型下载", msg)
            else:
                QMessageBox.warning(self, "模型下载", msg)
            self.log_signal.emit(f"模型下载 {'完成' if ok else '失败'}：{msg}")

        wk = DownloadWorker(base, model_id, _on_prog)
        self._model_dl_worker = wk

        def _run():
            wk.run()
            # R152-A1：必须发标准三元组 (cb, res, err) —— _on_bg_result 按
            # 三元组解包；以前这里发裸 lambda，解包直接 TypeError，完成/失败
            # 弹窗、done_cb（「设为当前」复活）整条链路断掉。
            # R168：同上，0 参 lambda 收 1 参会 TypeError ⇒ _fin 永不执行
            # ⇒ 完成/失败弹窗、done_cb（「设为当前」复活）全链断。
            self.bg_result.emit((lambda *_a: _fin(*wk.result), None, None))
        threading.Thread(target=_run, daemon=True,
                         name=f"mrm-model-dlwrap").start()

    # ---------------- 需求 28：Web 功能开关 ----------------
    def _restore_web_on_start(self):
        """R231：启动时若 cfg.web_enabled=true，静默恢复 Web 服务（不弹配置框）。

        与「用户手动勾选 act_web」走 toggle_web（弹框问端口/密码）区分开：
        已启用过的配置直接起服务，浏览器按 webui._auth_ok 免密直进（空密码+
        绑本机放行）。起失败只记日志、不弹框打扰启动。
        """
        if not getattr(self.cfg, "web_enabled", False):
            return
        try:
            ok, msg = self._ensure_web_server()
            if ok:
                self.log_signal.emit(f"Web 服务已自动恢复：{msg}")
            else:
                self.log_signal.emit(f"Web 服务恢复失败：{msg}")
        except Exception as e:
            try:
                self.log_signal.emit(f"Web 服务恢复异常：{e}")
            except Exception:
                pass

    def toggle_web(self, on: bool):
        """Web 功能开关。关 = 立刻停服务；开 = 先要密码，再起服务。"""
        from PySide6.QtWidgets import (QDialog, QFormLayout, QLineEdit,
                                       QSpinBox, QDialogButtonBox)
        if not on:
            srv = getattr(self, "_web_server", None)
            if srv is not None:
                srv.stop()
                self.log_signal.emit("Web 功能已关闭")
            self.cfg.web_enabled = False
            self._save_cfg_quiet()
            return
        # 打开：先问端口/账号/密码
        dlg = QDialog(self)
        dlg.setWindowTitle("启用 Web 功能")
        f = QFormLayout(dlg)
        # R141：密码**选填**（用户当面裁决）。R48/F 早就拍板「不强制密码、
        # 空密码可访问」，后来这里被改成了「必须设密码、至少 4 位」，与
        # 之冲突 ⇒ 回归免密为默认形态，密码只是**可选**加固。
        # ⚠ 免密只在「本机 / 局域网 / IP 白名单」范围内生效
        # （webui._auth_ok 的 R52 规则）；公网客户端 + 空密码仍被拦，
        # 那条是 R58 的安全护栏，本单刻意不动。
        tip = QLabel(
            "开启后可以用浏览器（电脑/手机/平板）访问本软件，"
            "看任务、看图库、待审核，还能从网页启停任务。\n\n"
            "密码为选填项：留空表示免密访问（适用于单人使用且仅监听"
            "本机的情形）；需供他人访问时应设置密码。\n"
            "监听地址默认只许本机；要让别的设备访问请填 0.0.0.0，"
            "并确认所在网络可信。")
        tip.setWordWrap(True)
        _tstyle(tip, "color:{text_dim};")
        f.addRow(tip)
        cur_pw = str(getattr(self.cfg, "web_password", "") or "")
        if cur_pw:
            # 存量兼容：已设密码的人要能**转回免密** —— 密码框是回显的，
            # 清空它保存即等于清除密码（下面写入的就是空串）。
            lab = QLabel("已设置密码 —— 把下面的输入框清空即可转回免密")
            lab.setWordWrap(True)
            _tstyle(lab, "color:{text_dim};")
            f.addRow("当前", lab)
        pw = QLineEdit(cur_pw)
        pw.setEchoMode(QLineEdit.EchoMode.Password)
        pw.setPlaceholderText("留空=免密（建议仅本机监听时用）")
        port = QSpinBox()
        port.setRange(1024, 65535)
        port.setValue(int(getattr(self.cfg, "web_port", 8080) or 8080))
        bind = QLineEdit(str(getattr(self.cfg, "web_bind", "127.0.0.1")
                            or "127.0.0.1"))
        bind.setPlaceholderText("127.0.0.1=只有本机；0.0.0.0=允许局域网")
        f.addRow("密码", pw)
        f.addRow("端口", port)
        f.addRow("监听地址", bind)
        # 2026-10-28（编号 23）：HTTPS 开关。放这里而不是只放网页里 ——
        # 「要先开 Web 才能去网页上开 HTTPS」是个死循环，第一次总得在本地勾。
        https = QCheckBox("同时开启 HTTPS（自动生成本机自签证书）")
        https.setChecked(bool(getattr(self.cfg, "web_https", False)))
        https_port = QSpinBox()
        https_port.setRange(1024, 65535)
        https_port.setValue(int(getattr(self.cfg, "web_https_port", 8443) or 8443))
        _hint = QLabel("HTTPS 用独立端口；手机/别的电脑第一次访问会提示"
                       "「不安全」，选择「继续访问」即可。证书 10 年有效，"
                       "到期前会自动重签。")
        _hint.setWordWrap(True)
        _tstyle(_hint, "color:{text_dim};")
        f.addRow(https)
        f.addRow("HTTPS 端口", https_port)
        f.addRow(_hint)

        # ---- 与网页「设置 → Web 访问」逐字段对齐的安全设置项 ----
        # 字段名一律取自 config.AppConfig；保存仍走下面的 _save_cfg_quiet()，
        # 现有密码 / 端口 / 监听地址 / HTTPS / HTTPS 端口各项行为保持不变。
        sec = QGroupBox("Web 访问安全")
        sf = QFormLayout(sec)

        lan_open = QCheckBox("局域网免密")
        lan_open.setChecked(bool(getattr(self.cfg, "web_lan_open", False)))
        sf.addRow(lan_open)

        localhost_free = QCheckBox("仅本机免密")
        localhost_free.setChecked(bool(getattr(self.cfg, "web_localhost_free", False)))
        sf.addRow(localhost_free)

        wl_nopass = QCheckBox("白名单内免密")
        wl_nopass.setChecked(bool(getattr(self.cfg, "web_whitelist_nopass", False)))
        sf.addRow(wl_nopass)

        max_fail = QSpinBox()
        max_fail.setRange(0, 50)
        max_fail.setValue(int(getattr(self.cfg, "web_login_max_fail", 5) or 0))
        sf.addRow("登录失败锁定阈值", max_fail)

        ban_min = QSpinBox()
        ban_min.setRange(1, 1440)
        ban_min.setValue(int(getattr(self.cfg, "web_login_ban_min", 10) or 10))
        sf.addRow("锁定时长（分钟）", ban_min)

        session_hours = QSpinBox()
        session_hours.setRange(1, 720)
        session_hours.setValue(int(getattr(self.cfg, "web_session_hours", 12) or 12))
        sf.addRow("会话有效期（小时）", session_hours)

        frame_deny = QCheckBox("禁止被网页嵌入")
        frame_deny.setChecked(getattr(self.cfg, "web_frame_deny", True) is not False)
        sf.addRow(frame_deny)

        host_check = QCheckBox("校验 Host 头")
        host_check.setChecked(getattr(self.cfg, "web_host_check", True) is not False)
        sf.addRow(host_check)

        csrf = QCheckBox("校验请求来源（CSRF）")
        csrf.setChecked(getattr(self.cfg, "web_csrf", True) is not False)
        sf.addRow(csrf)

        extra_headers = QTextEdit()
        extra_headers.setPlainText(
            str(getattr(self.cfg, "web_extra_headers", "") or ""))
        extra_headers.setMaximumHeight(64)
        sf.addRow("自定义响应头", extra_headers)

        _wl = getattr(self.cfg, "web_ip_whitelist", []) or []
        ip_wl = QLineEdit(
            ", ".join(str(x) for x in _wl)
            if isinstance(_wl, (list, tuple)) else str(_wl))
        ip_wl.setPlaceholderText("逗号分隔，例如 192.168.1.100/24")
        sf.addRow("IP 白名单", ip_wl)

        trust = QCheckBox("信任反向代理")
        trust.setChecked(bool(getattr(self.cfg, "trust_proxy", False)))
        sf.addRow(trust)

        f.addRow(sec)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        zh_buttons(bb)          # C5：英文系统上别显示 "OK / Cancel"
        f.addRow(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            # 取消：把勾去掉，保持关闭
            try:
                self.act_web.blockSignals(True)
                self.act_web.setChecked(False)
                self.act_web.blockSignals(False)
            except RuntimeError:
                pass
            return
        # R141：★ 删掉了「密码至少 4 位」的强制校验（旧代码在这里直接 return，
        # 用户根本没法留空启用）。密码现在是选填项。
        self.cfg.web_password = pw.text()
        self.cfg.web_port = int(port.value())
        # 编号 23：HTTPS 两个值一起写（端口撞车由 WebServer 起服务时拦下并提示）
        self.cfg.web_https = bool(https.isChecked())
        self.cfg.web_https_port = int(https_port.value())
        bind_v = bind.text().strip() or "127.0.0.1"
        # 2026-11-05 安全修复（P0）：非回环绑定 + 空密码 = 同网段任意设备免密
        # 接管全部数据接口（图库 / 原图 / fs/list 全盘浏览 / 任务启停 / 整库
        # 备份下载）。原来只弹一次确认就放行 —— 用户点一下"是"就把整台机器
        # 暴露给局域网。现在**直接拒绝启用**：要对外网段开放必须先设一个非空
        # 密码；留空密码只能绑 127.0.0.1 / localhost / ::1（仅本机可达）。
        # 想完全免密共享请走 Web 设置页把具体 IP 加进 web_ip_whitelist。
        if not (self.cfg.web_password or "").strip() and \
                bind_v not in ("127.0.0.1", "localhost", "::1"):
            QMessageBox.warning(
                self, "必须设置密码",
                f"监听地址为 {bind_v}（对网络内所有设备开放），但**未设置密码**\n"
                "—— 同一网络内的任何设备都将免密访问图库、原图、全盘文件浏览\n"
                "和整库备份下载。为防止误启用，这种组合已被禁止。\n\n"
                "请先在上方「密码」框设一个非空密码，或把地址改回 127.0.0.1。")
            try:
                self.act_web.blockSignals(True)
                self.act_web.setChecked(False)
                self.act_web.blockSignals(False)
            except RuntimeError:
                pass
            return
        self.cfg.web_bind = bind_v
        self.cfg.web_lan_open = bool(lan_open.isChecked())
        self.cfg.web_localhost_free = bool(localhost_free.isChecked())
        self.cfg.web_whitelist_nopass = bool(wl_nopass.isChecked())
        self.cfg.web_login_max_fail = int(max_fail.value())
        self.cfg.web_login_ban_min = int(ban_min.value())
        self.cfg.web_session_hours = int(session_hours.value())
        self.cfg.web_frame_deny = bool(frame_deny.isChecked())
        self.cfg.web_host_check = bool(host_check.isChecked())
        self.cfg.web_csrf = bool(csrf.isChecked())
        self.cfg.web_extra_headers = extra_headers.toPlainText()
        self.cfg.web_ip_whitelist = [
            x.strip() for x in re.split(r"[\s,]+", ip_wl.text().strip())
            if x.strip()]
        self.cfg.trust_proxy = bool(trust.isChecked())
        self.cfg.web_enabled = True
        self._save_cfg_quiet()
        ok, msg = self._ensure_web_server()
        if ok:
            self.log_signal.emit(f"Web 功能已开启：{msg}")
            QMessageBox.information(
                self, "Web 功能已开启",
                f"浏览器打开：{msg}\n\n"
                "桌面 GUI 照常使用，两边数据是同一份。")
        else:
            QMessageBox.warning(self, "Web 功能", f"启动失败：{msg}")
            self.cfg.web_enabled = False
            self._save_cfg_quiet()
            try:
                self.act_web.blockSignals(True)
                self.act_web.setChecked(False)
                self.act_web.blockSignals(False)
            except RuntimeError:
                pass

    def _save_cfg_quiet(self):
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass

    def _ensure_web_server(self) -> tuple[bool, str]:
        """按配置起 Web 服务；已经起着就直接返回地址。"""
        from ..webui import WebServer
        srv = getattr(self, "_web_server", None)
        if srv is None:
            srv = WebServer(self.cfg, self.db,
                            getattr(self, "_queue", None))
            # R154-F20：注入扫码登录授权钩子（HTTP 线程 → 主线程信号）。
            srv.qr_hook = self.qr_login_requested.emit
            self._web_server = srv
        return srv.start()

    def _prompt_qr_login(self, token, ip):
        """主线程槽：扫码登录请求入队并弹确认框。
        非模态 + 用户必须主动点同意/拒绝 = 防恶意扫码（订单要求）。"""
        self._qr_login_pending.append((token, ip))
        self._show_next_qr_login()

    def _show_next_qr_login(self):
        """队列非空且当前没有开着的框 → 弹下一个。"""
        if self._qr_login_box is not None or not self._qr_login_pending:
            return
        token, ip = self._qr_login_pending.pop(0)
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(self)
        box.setWindowTitle("扫码登录请求")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"设备 {ip} 请求扫码登录 Web 端。")
        box.setInformativeText("是否同意该设备登录？")
        b_ok = box.addButton("同意", QMessageBox.ButtonRole.AcceptRole)
        b_no = box.addButton("拒绝", QMessageBox.ButtonRole.RejectRole)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._qr_login_box = box

        def _decide(ok):
            try:
                from ..webui import approve_qr_token, reject_qr_token
                if ok:
                    approve_qr_token(token)
                    self.log_signal.emit(f"已同意扫码登录（{ip}）")
                else:
                    reject_qr_token(token)
                    self.log_signal.emit(f"已拒绝扫码登录（{ip}）")
            except Exception as e:
                self.log_signal.emit(f"扫码登录确认失败：{e}")
            finally:
                self._qr_login_box = None
                self._show_next_qr_login()

        b_ok.clicked.connect(lambda: _decide(True))
        b_no.clicked.connect(lambda: _decide(False))
        box.show()

    def _prompt_db_master_password(self):
        """R161：启动时用模态框要主密码；返回密码字符串，取消返回 None。"""
        from PySide6.QtWidgets import (QDialog, QFormLayout, QLineEdit,
                                       QLabel, QPushButton, QVBoxLayout,
                                       QHBoxLayout, QWidget)
        dlg = QDialog(self)
        dlg.setWindowTitle("数据库已加密 - 输入主密码")
        root = QVBoxLayout(dlg)
        root.addWidget(QLabel("数据库已开启加密，请输入主密码以解锁："))
        form = QFormLayout()
        pw = QLineEdit()
        pw.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("主密码", pw)
        root.addLayout(form)
        bb = QWidget(); bh = QHBoxLayout(bb); bh.setContentsMargins(0, 0, 0, 0)
        bh.addStretch(1)
        b_ok = QPushButton("确定"); b_cancel = QPushButton("取消")
        bh.addWidget(b_ok); bh.addWidget(b_cancel); root.addWidget(bb)
        dlg.setMinimumWidth(360)
        res = {"ok": False}
        b_ok.clicked.connect(lambda: (res.__setitem__("ok", True),
                                      res.__setitem__("pw", pw.text()),
                                      dlg.accept()))
        b_cancel.clicked.connect(dlg.reject)
        if dlg.exec() == QDialog.DialogCode.Accepted and res.get("ok"):
            return res.get("pw") or ""
        return None

    def open_ai_adapter_dialog(self):
        """维护 → 使用在线 AI 生成站点规则…。

        网址输入、后台线程生成、规则预览、确认落盘与写回自检全部由
        ``gui/ai_adapter_dialog.py`` 内的对话框负责；本方法只负责打开，
        不在这里散落第二套流程。
        """
        try:
            from .ai_adapter_dialog import AIAdapterDialog
            dlg = AIAdapterDialog(self, self.cfg)
        except Exception:
            QMessageBox.warning(
                self, "无法打开 AI 生成站点规则",
                "打开对话框失败。请确认已在「AI 模型设置」中配置在线 API 地址与密钥，"
                "然后重新尝试；若仍无法打开，请查看日志文件了解详情。")
            return
        dlg.exec()

    def open_rate_plan_dialog(self):
        """F24：分时段下载限速（设置 → 下载限速计划…）。

        保存后立刻落盘 —— 否则用户点完直接关主窗口，改动会静默丢失
        （本仓这条教训在别处踩过：即时生效类设置必须自己持久化）。
        """
        from ..config import save_config
        from .rateplan_dialog import RatePlanDialog
        try:
            dlg = RatePlanDialog(self, self.cfg)
        except Exception as e:                                  # pragma: no cover
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "打不开限速设置", str(e))
            return
        if dlg.exec():
            dlg.apply_to(self.cfg)
            try:
                save_config(self.cfg)
            except Exception:
                pass
            self.statusBar().showMessage(
                "已保存下载限速计划：" + dlg.current_text(), 5000)

    def open_hotkey_dialog(self):
        """F19：快捷键自定义（设置 → 快捷键…）。

        保存后**三件事一起做**：落盘（否则直接关主窗口改动静默丢失——
        本仓这条教训在别处踩过：即时生效类设置必须自己持久化）、
        重建任务启停快捷键、重拼审核页键位文案（不同步就是
        「设置里写着 L、界面还印着 →」那类对不上缺陷）。
        """
        from ..config import save_config
        from .hotkey_dialog import HotkeyDialog
        try:
            dlg = HotkeyDialog(self, self.cfg)
        except Exception as e:                                  # pragma: no cover
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "打不开快捷键设置", str(e))
            return
        if dlg.exec():
            dlg.apply_to(self.cfg)
            try:
                save_config(self.cfg)
            except Exception:
                pass
            self._rebind_hotkeys()
            try:
                self._review_tab.sync_hotkey_hints()
            except Exception:
                pass
            n = len(dlg.warnings_text())
            self.statusBar().showMessage(
                "已保存快捷键设置"
                + (f"（{n} 处键位告警，见设置表红字）" if n else ""), 5000)

    def _rebind_hotkeys(self) -> None:
        """F19：按当前 cfg.hotkeys 重建「任务启停」两条窗口级快捷键。

        口径（与 hotkeys.warnings_for 的文案一致，别各写一套）：
        * 同一序列被两个任务动作绑了 → **只建排前面的**（Qt 对重复序列
          报 Ambiguous shortcut overload，可能两个都不触发 ⇒ 先到先得）；
        * QKeySequence 表达不了的键（RightCtrl）→ 跳过 + 记一行日志，
          不弹窗（用户可能是从 config.json 手改进来的，不该被弹窗拦）。
        """
        # ⚠ QShortcut 在 PySide6 里位于 **QtGui**（6.11 起 QtWidgets 里
        # 导入不到；本文件 F1 那块曾因此静默失效，见那边注释）。
        from PySide6.QtGui import QKeySequence, QShortcut
        from ..hotkeys import action_label, resolve as _hk
        from .hotkeys_qt import qt_sequence
        for sc in getattr(self, "_hotkey_shortcuts", None) or []:
            try:
                sc.setParent(None)
                sc.deleteLater()
            except RuntimeError:
                pass
        self._hotkey_shortcuts = []
        hk = getattr(self.cfg, "hotkeys", None) or {}
        seen = set()
        for aid, slot in (("task.start", self.run_selected),
                          ("task.stop", self.stop_selected)):
            seq = _hk(hk, aid)
            ks = qt_sequence(seq)
            if ks is None:
                try:
                    self.log(f"快捷键「{action_label(aid)}」的键位 {seq} "
                             f"建不出窗口快捷键，本轮跳过（换一个键即可）")
                except Exception:
                    pass
                continue
            token = ks.toString()
            if token in seen:
                continue
            seen.add(token)
            sc = QShortcut(ks, self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)
            self._hotkey_shortcuts.append(sc)

    def db_encryption_dialog(self):
        """R161：数据库加密开关。开启=设主密码并重启迁移；关闭=明文降级。"""
        from PySide6.QtWidgets import (QDialog, QFormLayout, QLineEdit,
                                       QCheckBox, QVBoxLayout, QPushButton,
                                       QLabel, QHBoxLayout, QWidget, QMessageBox)
        from ..config import (set_db_master_password, get_db_master_password,
                              save_config)
        dlg = QDialog(self)
        dlg.setWindowTitle("数据库加密")
        root = QVBoxLayout(dlg)
        cur = bool(getattr(self.cfg, "encryption_enabled", False))
        root.addWidget(QLabel(
            "当前状态：%s\n主密码只存在本次运行内存，不写磁盘；重启需重输。" %
            ("已开启（加密）" if cur else "未开启（明文）")))
        chk = QCheckBox("启用数据库加密（防止本机其它用户 / 拿到硬盘直接读库）")
        chk.setChecked(cur)
        root.addWidget(chk)
        form = QFormLayout()
        pw1 = QLineEdit(); pw1.setEchoMode(QLineEdit.EchoMode.Password)
        pw1.setPlaceholderText("主密码（≥4 位）")
        pw2 = QLineEdit(); pw2.setEchoMode(QLineEdit.EchoMode.Password)
        pw2.setPlaceholderText("再次输入确认")
        form.addRow("主密码", pw1); form.addRow("确认", pw2)
        root.addLayout(form)
        bb = QWidget(); bh = QHBoxLayout(bb); bh.setContentsMargins(0, 0, 0, 0)
        bh.addStretch(1)
        b_ok = QPushButton("保存"); b_cancel = QPushButton("关闭")
        bh.addWidget(b_ok); bh.addWidget(b_cancel); root.addWidget(bb)
        dlg.setMinimumWidth(420)

        def do_ok():
            want = chk.isChecked()
            if want == cur:
                dlg.accept(); return  # 无改动
            if want:
                p1, p2 = pw1.text(), pw2.text()
                if len(p1) < 4:
                    QMessageBox.warning(dlg, "主密码太短", "主密码至少 4 位。"); return
                if p1 != p2:
                    QMessageBox.warning(dlg, "不一致", "两次输入的主密码不一致。"); return
                # 启用：内存密码 + 标记 + 落盘；迁移在重启时由打开逻辑完成
                set_db_master_password(p1)
                self.cfg.encryption_enabled = True
                save_config(self.cfg)
                self._reopen_db_encrypted(True)
                self.statusBar().showMessage("数据库加密已开启，已就地迁移", 4000)
            else:
                # 关闭：二次确认后降级为明文（本进程内直接转换，不依赖重启）
                if QMessageBox.question(
                        dlg, "关闭加密",
                        "关闭后数据库将以明文存储，确定？") != QMessageBox.StandardButton.Yes:
                    return
                self._reopen_db_encrypted(False)
                set_db_master_password("")
                self.cfg.encryption_enabled = False
                save_config(self.cfg)
                self.statusBar().showMessage("数据库加密已关闭，数据转为明文", 4000)
            dlg.accept()

        b_ok.clicked.connect(do_ok)
        b_cancel.clicked.connect(dlg.reject)
        dlg.exec()

    def _reopen_db_encrypted(self, enable: bool) -> None:
        """R161：加密开关变更后，关闭旧库并以新状态重开（迁移/降级在此发生）。"""
        from ..db import Database, ProxyPool
        try:
            if getattr(self, "db", None) is not None and not self.db.is_closed():
                if not enable:
                    self.db._encrypted = False  # 降级：close 写明文，不回加密
                self.db.close()
        except Exception:
            pass
        try:
            self.db = Database(self.cfg.db_path)
            self.pool = ProxyPool(self.db)
        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "数据库重开失败", "加密切换后重开库失败：%s" % e)

    def _on_learn_toggled(self, checked: bool):
        """R149-B：「学习」开关勾选态持久化到 cfg.learn_enabled，并同步全局标志。"""
        self.cfg.learn_enabled = bool(checked)
        from ..classify.preference import set_learning_enabled, is_learning_enabled
        set_learning_enabled(bool(checked))
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        # 状态栏提示当前开关状态（开/关）。
        state = "开" if is_learning_enabled() else "关"
        try:
            self.statusBar().showMessage(f"学习（AI 偏好）：{state}", 3000)
        except Exception:
            pass

    def show_disk_space(self):
        """R154-F18「磁盘空间看板」：各目录占用 + 7 日增长 + 磁盘预警。

        与「学习看板 / 采集周报」的分工：那两个回答「AI 学到什么 /
        这一期采得值不值」，这个回答「硬盘被什么吃满了、还会不会继续涨」。
        阈值**不新建设置项** —— 直接读 cfg.min_free_mb（#54 磁盘剩余空间
        预警）：另设一个字段，用户在设置里改了却看不懂为什么两边不联动。

        只读 + 记一次快照：打开时若今天还没快照就扫一遍记上（「每日一
        快照」不要求用户记得点按钮）；同一天再打开不重复扫（大库扫一遍
        要好几秒）。扫目录与统计全在 ``diskspace`` 里，本方法不自己写。
        """
        from .diskspace_dialog import DiskSpaceDialog
        try:
            dlg = DiskSpaceDialog(self, self.db, self.cfg)
        except Exception as e:                                  # pragma: no cover
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "打不开磁盘空间看板", str(e))
            return None
        return dlg.exec()

    def show_collection_report(self):
        """R154-F25「采集周报 / 月报」：按周/月看这一期采得怎么样。

        与「学习看板」的分工：看板回答「AI 学到了什么」（权重、样本来源），
        报告回答「这一期采了多少、值不值」（采集量、通过率、任务排行）。
        两个问题混在一个窗口里两边都会被稀释，所以独立成框。

        只读：统计全在 ``reporting`` 里，本方法不写库、不改配置。
        """
        from .report_dialog import ReportDialog
        try:
            dlg = ReportDialog(self, self.db)
        except Exception as e:                                  # pragma: no cover
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "打不开采集报告", str(e))
            return None
        return dlg.exec()

    def show_learning_dashboard(self):
        """#63 / A6「学习看板」：概览 AI 从用户这里学到了什么。

        V0.0.9：看板升级成**首页**（打开软件第一眼就是它）后，菜单入口不再
        弹独立对话框——直接切到首页并重刷。返回看板部件（既有测试拿它断言，
        老调用方 `dlg = win.show_learning_dashboard()` 语义仍成立）。

        与审核页的「权重明细」是**互补而非重复**：
        权重明细是「逐条查/改/删」，看板是「一眼看全局」（正负向各Top-N、
        命中次数、14 天趋势、判定分布）。

        只读：看板里刻意不提供改权重入口，避免两个界面写同一张 preferences
        表。改权重仍统一走「权重明细」。
        """
        bt = getattr(self, "_board_tab", None)
        if bt is not None:
            self._nav_go(self.stack.indexOf(bt))
            return bt
        # 兜底（理论上走不到：首页在窗口构建时就挂进 stack）保持老弹窗形态，
        # 绝不让菜单点了没反应。
        from .learning_board import open_learning_dashboard
        return open_learning_dashboard(self, self.db)

    # ------------------------------------------------- R154-F22：新手引导 ----
    def maybe_show_onboarding(self):
        """首次运行（全新安装）弹 3 步新手向导；完成/跳过后写旗标不再弹。

        ⚠ 为什么**不**放在 __init__ 里：测试会直接建 MainWindow，
        弹模态框会把用例挂死。触发点 = launch()（真启动路径）+
        设置菜单「重新运行向导」（随时手动）。
        """
        if getattr(self.cfg, "onboarding_done", True):
            return
        try:
            self.open_onboarding_wizard()
        except Exception as e:                                  # pragma: no cover
            self.log_signal.emit(f"新手引导未显示: {type(e).__name__}: {e}")

    def open_onboarding_wizard(self):
        """打开新手向导（菜单入口 = 随时可重新运行）。"""
        from .onboarding import WizardDialog
        dlg = WizardDialog(
            self, self.cfg, self.db, self.cfg.rules_dir,
            on_open_manager=self.url_adapter_manager,
            on_create_task=self.create_first_task,
            on_start_task=self.start_first_task,
            on_finish=self.finish_onboarding)
        return dlg.exec()

    def finish_onboarding(self):
        """向导结束（完成或跳过）→ 写旗标 + 落盘，以后不再自动弹。"""
        self.cfg.onboarding_done = True
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass

    # ------------------------------------------------ 编号 7：检查更新 --
    def check_update(self):
        """显示当前版本；配了清单地址就比对远端版本（失败一律中文说明）。"""
        try:
            from ..update_check import check
            r = check(self.cfg)
        except Exception as ex:                       # noqa: BLE001
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "检查更新失败",
                                f"检查更新时出错：{type(ex).__name__}: {ex}")
            return
        from PySide6.QtWidgets import QMessageBox
        text = str(r.get("text") or "")
        if r.get("has_update") and r.get("url"):
            _ans = QMessageBox.question(
                self, "发现新版本",
                text + "\n\n要打开下载页吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if _ans == QMessageBox.StandardButton.Yes:
                try:
                    from PySide6.QtGui import QDesktopServices
                    from PySide6.QtCore import QUrl
                    QDesktopServices.openUrl(QUrl(str(r["url"])))
                except Exception:
                    pass
            return
        QMessageBox.information(self, "检查更新", text)

    # ------------------------------------------------ 编号 10/13：快速添加 --
    def quick_add(self, clipboard: bool = True, urls=None, name: str = ""):
        """用**默认配置**直接建任务（不弹表单）。

        `urls` 为空时从剪贴板取（菜单入口就是这条路）。
        返回新建的任务 id 列表（监视文件夹回调要靠这个计数）。
        """
        from ..models import TaskConfig
        from ..repo import save_task
        if urls is None:
            urls = []
            if clipboard:
                try:
                    from PySide6.QtWidgets import QApplication
                    from ..watchfolder import parse_urls
                    urls = parse_urls(QApplication.clipboard().text() or "")
                except Exception:
                    urls = []
        urls = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if not urls:
            self.log_signal.emit("快速添加：剪贴板里没有可用网址（需要 http/https 开头）")
            return []
        # 任务名：给的优先，否则用第一个网址的域名（用户一眼能认出是哪个站）
        if not name:
            try:
                from urllib.parse import urlsplit
                name = urlsplit(urls[0]).netloc or "快速任务"
            except Exception:
                name = "快速任务"
        tcfg = TaskConfig(
            id=None, name=str(name)[:80], adapter_name="", entry_urls=urls,
            max_depth=int(getattr(self.cfg, "max_depth", 3) or 3),
            concurrency=int(getattr(self.cfg, "concurrency", 4) or 4),
            delay=float(getattr(self.cfg, "delay", 0.5) or 0.5),
        )
        try:
            tid = save_task(self.db, tcfg)
        except Exception as ex:                       # noqa: BLE001
            self.log_signal.emit(f"快速添加失败：{type(ex).__name__}: {ex}")
            return []
        ids = [tid]
        self.log_signal.emit(f"快速添加：已建任务 #{tid}「{tcfg.name}」（{len(urls)} 个网址）")
        try:
            self.refresh()
        except Exception:
            pass
        # 静默添加的语义：如果用户开了「保存后立即运行」，就真的开始跑
        if bool(getattr(self.cfg, "auto_run", False)):
            try:
                self._queue.submit(int(tid))
            except Exception:
                pass
        return ids

    def _watch_create_tasks(self, urls, name: str) -> int:
        """监视文件夹回调（编号 13）：一次清单建一个任务，返回建了几个。"""
        try:
            return len(self.quick_add(clipboard=False, urls=urls, name=name))
        except Exception as ex:                       # noqa: BLE001
            self.log_signal.emit(f"监视文件夹建任务失败：{type(ex).__name__}: {ex}")
            return 0

    def toggle_watch_folder(self, on: bool | None = None):
        """开关监视文件夹（编号 13）。路径没配时直接提示，不静默失败。"""
        want = bool(self.act_watch_dir.isChecked()) if on is None else bool(on)
        path = str(getattr(self.cfg, "watch_dir", "") or "").strip()
        if want and not path:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "监视文件夹",
                "尚未设置监视目录。\n\n请前往「设置 → 采集与下载 → 监视文件夹路径」"
                "选一个目录，再回来打开这个开关。")
            self.act_watch_dir.setChecked(False)
            return
        self.cfg.watch_enabled = want
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        if want:
            self._start_watch_folder()
        else:
            self._stop_watch_folder()
            self.log_signal.emit("已停止监视文件夹")

    def _start_watch_folder(self):
        """启动（或重启）监视线程。重复调用是安全的。"""
        self._stop_watch_folder()
        path = str(getattr(self.cfg, "watch_dir", "") or "").strip()
        if not path:
            return
        try:
            from ..watchfolder import WatchFolder
            self._watcher = WatchFolder(
                path, self._watch_create_tasks,
                interval=float(getattr(self.cfg, "watch_interval_sec", 30) or 30),
                log=lambda m: self.log_signal.emit(m))
            self._watcher.start()
        except Exception as ex:                       # noqa: BLE001
            self._watcher = None
            self.log_signal.emit(f"监视文件夹启动失败：{type(ex).__name__}: {ex}")

    def _stop_watch_folder(self):
        w = getattr(self, "_watcher", None)
        if w is not None:
            try:
                w.stop()
            except Exception:
                pass
            self._watcher = None

    def create_first_task(self, url, adapter_name, name):
        """向导第 3 步：创建第一个任务（与任务表单保存同一套落库口径）。"""
        from ..models import TaskConfig
        from ..repo import save_task
        t = TaskConfig(id=None, name=name or "第一个任务",
                       adapter_name=adapter_name or "", entry_urls=[url])
        # 2026-10-27（编号 14）：改了输出目录 + 开关打开 → 问一次要不要搬旧文件。
        # 为什么"问"而不是"自动搬"：几千张图挪盘可能要几分钟，而且用户可能
        # 只是想把**以后**的图放新地方。宁可多问一句，也不能默默搬走。
        _old_dir = ""
        try:
            if editing_tid:
                _prev = get_task(self.db, editing_tid)
                _old_dir = str(getattr(_prev, "output_dir", "") or "") if _prev else ""
        except Exception:
            _old_dir = ""
        tid = save_task(self.db, t)
        try:
            _new_dir = str(getattr(t, "output_dir", "") or "")
            if (_old_dir and _new_dir and _old_dir != _new_dir
                    and bool(getattr(self.cfg, "move_on_path_change", False))):
                from pathlib import Path as _P
                _src = _P(_old_dir)
                if _src.exists() and any(_src.rglob("*")):
                    from PySide6.QtWidgets import QMessageBox
                    _ans = QMessageBox.question(
                        self, "移动已有文件",
                        f"这个任务的输出目录改了：\n\n旧：{_old_dir}\n新：{_new_dir}\n\n"
                        "要把旧目录里已经下好的图**搬过去**吗？\n"
                        "（同名文件会跳过，不会覆盖）",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No)
                    if _ans == QMessageBox.StandardButton.Yes:
                        from ..cleanup import move_dir_contents
                        st = move_dir_contents(_old_dir, _new_dir)
                        self.log_signal.emit(
                            f"已搬移 {st['moved']} 个文件（跳过 {st['skipped']}、"
                            f"失败 {st['failed']}）")
        except Exception as ex:                       # noqa: BLE001
            self.log_signal.emit(f"移动文件失败：{type(ex).__name__}: {ex}")
        self.refresh()
        return tid

    def start_first_task(self, tid):
        """向导第 3 步勾选「立即开始」→ 入队（与运行菜单同一条 submit 路径）。"""
        from ..repo import get_task
        t = get_task(self.db, tid)
        if t is None:
            return
        if self._queue is not None and tid not in self._queue.running:
            self._queue.submit_many([(t, None)])
            self.log_signal.emit("已入队新手任务，开始采集")

    def ai_model_dialog(self):
        """#44 AI 模型面板重做：**标签页** + 按模式切页（C9）；推理设备/线程数收进
    面板；全中文按钮（无 OK/Cancel）。

    模式：内置 CLIP / 未下载（模型库里点下载）/ 到本地 / 在线 API。

    【C9 用户反馈】原来四种模式的内容全挤在一个长表单里，一打开就**全摊开**：
    本地目录、API 地址/密钥/模型名、模型库下载面板一起出现，看着像填四张表，
    实际一次只会用一种。现在改成标签页：

        [通用] [到本地] [在线 API] [模型库]
          ↑模式=      ↑local    ↑api      ↑notdownloaded

    模式下拉仍是**总开关**：选哪个模式就跳到对应标签页，并把用不上的标签
    disable 掉（而不是 setVisible 藏起来 —— 藏起来的页在标签栏留个空位更难懂）。
    #44 的 `_reveal_*` 契约保留（测试按它断言），只是底层换成enable/disable。
    """
        from PySide6.QtWidgets import (QDialog, QFormLayout, QLineEdit,
                                       QComboBox, QFileDialog, QHBoxLayout,
                                       QVBoxLayout, QWidget, QPushButton,
                                       QLabel, QSpinBox, QTabWidget, QCheckBox)
        dlg = QDialog(self)
        dlg.setWindowTitle("AI 模型设置")
        root = QVBoxLayout(dlg)

        # ---- 模式总开关：选模式 = 切标签页（C9）----
        mode = QComboBox()
        # 2026-10-27 用户要求：**删掉「到本地（自定义本地模型目录）」**。
        # 理由：它和下面模型库里的「未下载/已就绪」完全是同一件事的两个入口，
        # 而且第三方模型大家更习惯走在线 API。留两档本地来源只会让人选错。
        MODES = [
            ("内置 CLIP（默认，离线）", "builtin"),
            ("未下载（在下方模型库里点下载获取）", "notdownloaded"),
            ("在线 API（OpenAI 兼容接口）", "api"),
        ]
        for text, data in MODES:
            mode.addItem(text, data)
        cur = getattr(self.cfg, "ai_backend", "builtin")
        if cur not in ("builtin", "notdownloaded", "api"):
            cur = "builtin"
        # 兼容老配置：AI 来源=local（自定义目录）这一档已取消。
        # 有路径（= 用户当年从模型库选过某个已下载模型）→ 显示「未下载」页，
        # 那里就是模型库本体；没路径 → 内置。
        if cur == "local":
            cur = ("notdownloaded" if getattr(self.cfg, "ai_model_path", "")
                   else "builtin")
        mode.setCurrentIndex(mode.findData(cur))
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("模式"))
        mode_row.addWidget(mode, 1)
        root.addLayout(mode_row)

        # ---- R172-1：GPU/CPU 开关搬到**这里**（原来只在「AI 与过滤」菜单里，
        #      用户找不到，以为功能没做）。与菜单项读/写**同一份 cfg.ai_gpu**，
        #      两边互相同步勾选态；下面那行「实际状态」把「开了但没显卡」这种
        #      静默降级说清楚 —— 这正是用户抱怨的「不知道生没生效」。----
        try:
            from ..models_lib import gpu_switch_text as _gpu_text
        except Exception:                                  # pragma: no cover
            _gpu_text = None
        gpu_row = QHBoxLayout()
        gpu_chk = QCheckBox("用显卡加速（GPU）")
        #: R172-1 双向同步用的两个小容器，提前定义（下拉在下面「通用」页才建）。
        _dev_holder: dict = {}
        _syncing: dict = {}
        gpu_chk.setChecked(bool(getattr(self.cfg, "ai_gpu", True)))
        gpu_chk.setToolTip("开启后优先用显卡推理，显存不够或无显卡时自动回退 CPU。\n"
                           "与「AI 与过滤」菜单里那一项是同一个开关。")
        gpu_row.addWidget(gpu_chk)
        gpu_state = QLabel("")
        gpu_state.setWordWrap(True)
        _tstyle(gpu_state, "color:{text_dim};font-size:11px;")
        gpu_row.addWidget(gpu_state, 1)
        root.addLayout(gpu_row)

        def _paint_gpu_state(*_a):
            """把「配置想要的」+「本机实际能给的」渲染成一行状态文字。

            ⚠ 探测后端要 import onnxruntime（几百 ms），只在开关被点、
            或对话框刚打开时算 —— 不放进任何定时刷新路径。
            """
            if _gpu_text is None:
                gpu_state.setText("")
                return
            try:
                txt = _gpu_text(bool(gpu_chk.isChecked()))
            except Exception as e:                         # pragma: no cover
                txt = f"读不到本机推理后端：{e}"
            gpu_state.setText(txt)

        def _gpu_toggled(on):
            """开关一动手就把状态写进 cfg + 落盘，并**同步菜单勾选态**。

            以前对话框里没有这个开关，改设备要「保存」才落盘；现在它是
            即时生效的设置（与菜单项行为一致），所以立即持久化 —— 否则
            用户点完直接关掉对话框（不点保存），改动会静默丢失。
            """
            self.cfg.ai_gpu = bool(on)
            # 2026-10-27：有了三档之后，复选框只是"用不用显卡"的快捷方式：
            # 勾上 = 自动（有显卡就用），取消 = 强制 CPU。
            # 不写 ai_device 的话，取消勾选后 auto 档仍会去挑显卡 → 开关白点。
            self.cfg.ai_device = "auto" if on else "cpu"
            act = getattr(self, "act_ai_gpu", None)
            if act is not None:
                try:
                    act.setChecked(bool(on))
                except RuntimeError:
                    pass
            # 同步「通用」页那个下拉（它是后建的，所以通过 holder 拿）。
            # 不同步的话：用户勾了复选框直接选择「保存」，do_save 会按下拉的
            # **旧值**把 just-set 的 ai_gpu 覆盖回去 —— 改动静默回滚。
            dv = _dev_holder.get("w")
            if dv is not None and not _syncing.get("on"):
                try:
                    _syncing["on"] = True
                    # V0.0.16：推理设备新增 NPU 档后，写死索引会把「CPU」指到
                    # 第 3 项（NPU），复选框关掉却显示成 NPU —— 按 data 查找，
                    # 以后再增删档位也不会错位。
                    _di = dv.findData("auto" if on else "cpu")
                    if _di >= 0:
                        dv.setCurrentIndex(_di)
                except RuntimeError:
                    pass
                finally:
                    _syncing["on"] = False
            try:
                from ..config import save_config
                save_config(self.cfg)
            except Exception:
                pass
            _paint_gpu_state()

        gpu_chk.toggled.connect(_gpu_toggled)
        # 反向同步：菜单里先改了、再打开对话框，复选框要显示菜单里那份状态。
        act0 = getattr(self, "act_ai_gpu", None)
        if act0 is not None:
            try:
                if bool(act0.isChecked()) != bool(gpu_chk.isChecked()):
                    gpu_chk.setChecked(bool(act0.isChecked()))
            except RuntimeError:
                pass
        _paint_gpu_state()
        dlg._gpu_chk = gpu_chk
        self._dlg_gpu_chk = gpu_chk     # 供 set_ai_device 同步
        dlg._gpu_state = gpu_state
        dlg._gpu_toggled = _gpu_toggled

        # ---- 说明行：一句话讲清「模型/设备怎么选」，不再满屏术语（R172-2）----
        tip = QLabel("选模型只需两步：在「模型库」点下载，再选择「设为当前」。"
                     "模型越大越准，也越慢、越吃显存。")
        tip.setWordWrap(True)
        _tstyle(tip, "color:{text_dim};font-size:11px;")
        root.addWidget(tip)

        # ---- 标签页容器 ----
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        # ---- Tab「通用」：推理设备 + 线程数（对所有模式都适用）----
        tab_gen = QWidget()
        genf = QFormLayout(tab_gen)

        # ---- 到本地：本地模型目录（默认隐藏）----
        local_container = QWidget()
        lh = QHBoxLayout(local_container)
        lh.setContentsMargins(0, 0, 0, 0)
        path_in = QLineEdit(str(getattr(self.cfg, "ai_model_path", "") or ""))
        path_in.setPlaceholderText("留空=软件自带模型")
        lh.addWidget(path_in, 1)

        def pick():
            _pk = QFileDialog(dlg, "选择模型目录", path_in.text())
            _pk.setOption(QFileDialog.DontUseNativeDialog, True)
            _pk.setFileMode(QFileDialog.FileMode.Directory)
            _pk.setOption(QFileDialog.Option.ShowDirsOnly)
            if _pk.exec() == QDialog.DialogCode.Accepted and _pk.selectedFiles():
                path_in.setText(_pk.selectedFiles()[0])
        b_pick = QPushButton("浏览…")
        b_pick.clicked.connect(pick)
        lh.addWidget(b_pick)
        tab_local = QWidget()
        tl = QVBoxLayout(tab_local)
        tl.addWidget(QLabel("指向本机已有的 CLIP 模型目录（比「模型库」自由）。"))
        tl.addWidget(local_container)
        tl.addStretch()

        # ---- 在线 API：地址 / 密钥 / 模型名（默认隐藏）----
        api_container = QWidget()
        av = QVBoxLayout(api_container)
        av.setContentsMargins(0, 0, 0, 0)
        av.setSpacing(4)
        url_in = QLineEdit(str(getattr(self.cfg, "ai_api_url", "") or ""))
        url_in.setPlaceholderText("如 https://api.openai.com/v1")
        key_in = QLineEdit(str(getattr(self.cfg, "ai_api_key", "") or ""))
        key_in.setPlaceholderText("sk-...（只保存在本机配置里）")
        model_in = QLineEdit(str(getattr(self.cfg, "ai_api_model", "") or ""))
        model_in.setPlaceholderText("留空=接口默认（如 gpt-4o-mini）")
        av.addWidget(QLabel("API 地址"))
        av.addWidget(url_in)
        av.addWidget(QLabel("API 密钥"))
        av.addWidget(key_in)
        av.addWidget(QLabel("在线模型名"))
        # V0.0.17：与网页端「从接口拉取模型列表」对称，后台线程请求，不卡界面。
        model_row = QWidget()
        mr = QHBoxLayout(model_row)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.addWidget(model_in, 1)
        b_fetch_models = QPushButton("从接口拉取模型列表")
        b_fetch_models.setToolTip("读取在线接口提供的模型清单，选取后填入「在线模型名」")
        mr.addWidget(b_fetch_models)
        av.addWidget(model_row)
        model_list = QComboBox()
        model_list.setVisible(False)
        model_list.setToolTip("接口返回的模型清单，选取后填入上方的模型名")
        av.addWidget(model_list)

        class _ModelsWorker(QThread):
            finished_ok = Signal(object)

            def __init__(self, url, key, parent=None):
                super().__init__(parent)
                self.url, self.key = url, key

            def run(self):
                from ..online_models import fetch_models
                self.finished_ok.emit(fetch_models(self.url, self.key))

        _models_holder = {"worker": None}

        def _on_models(res):
            b_fetch_models.setEnabled(True)
            _models_holder["worker"] = None
            try:
                if not self._alive(b_fetch_models):
                    return
            except Exception:
                return
            if not isinstance(res, dict) or not res.get("ok"):
                msg = (res or {}).get("error") if isinstance(res, dict) else ""
                QMessageBox.information(
                    dlg, "在线模型列表",
                    str(msg or "未能读取在线模型列表，请检查接口地址与密钥。"))
                return
            names = [str(x) for x in (res.get("models") or []) if str(x).strip()]
            if not names:
                QMessageBox.information(dlg, "在线模型列表", "接口未返回可用模型。")
                return
            model_list.blockSignals(True)
            model_list.clear()
            model_list.addItems(names)
            model_list.blockSignals(False)
            model_list.setVisible(True)

        def _apply_model(name):
            if name:
                model_in.setText(str(name))

        model_list.currentTextChanged.connect(_apply_model)

        def _fetch_clicked():
            if _models_holder["worker"] is not None:
                return
            b_fetch_models.setEnabled(False)
            w = _ModelsWorker(url_in.text().strip(), key_in.text().strip(), dlg)
            _models_holder["worker"] = w
            w.finished_ok.connect(_on_models)
            w.finished.connect(
                lambda: _models_holder.__setitem__("worker", None))
            w.start()

        b_fetch_models.clicked.connect(_fetch_clicked)
        # 持有引用，避免对话框关闭时后台线程被回收
        dlg._models_worker_holder = _models_holder

        tab_api = QWidget()
        ta = QVBoxLayout(tab_api)
        ta.addWidget(QLabel("OpenAI 兼容接口。密钥只写在本机配置文件里，不外发。"))
        ta.addWidget(api_container)
        ta.addStretch()

        # ---- 未下载：模型库下载面板（默认隐藏）----
        dl_container = QWidget()
        dv = QVBoxLayout(dl_container)
        dv.setContentsMargins(0, 0, 0, 0)
        # R172：把模型面板（`_build_model_manager` 返回的 host）挂到对话框上
        # —— 「设为当前」的即时反馈要靠它刷新行态，真机排查与护栏也要拿到
        # 里面的按钮/行对象（只憋在闭包里就只能 grep 源码，抓不住行为失效）。
        _mrm_panel = self._build_model_manager(dlg, path_in)
        dlg._mrm_panel = _mrm_panel
        dv.addWidget(_mrm_panel)
        tab_dl = QWidget()
        td = QVBoxLayout(tab_dl)
        td.addWidget(QLabel("尚未下载模型？请在下方单击下载，完成后单击「设为当前」。"))
        td.addWidget(dl_container)
        td.addStretch()

        # ---- 推理设备 + 线程数（#44：收进本面板）----
        # R172-1：与顶部那个 GPU 复选框是**同一份 cfg.ai_gpu**，双向同步。
        # 保留这个下拉是为了不动 #44 的既有契约与老用户习惯；两处必须始终
        # 一致 —— 「顶部勾了 GPU、通用页显示纯 CPU」比只有一个入口更让人懵。
        dev = QComboBox()
        # 2026-10-27 用户要求：四档（自动 / 强制显卡 / 强制 NPU / 强制 CPU）——
        # CPU 有时要跑别的重负载任务，需要主动把推理挪到显卡或 NPU 上。
        dev.addItem("自动（有显卡就用显卡）", "auto")
        dev.addItem("强制显卡（CPU 腾出来干别的）", "gpu")
        dev.addItem("强制用 NPU（高通 QNN / AMD Ryzen AI / 英特尔 OpenVINO）", "npu")
        dev.addItem("强制 CPU（显卡异常/显存被占）", "cpu")
        from ..config import ai_device_effective
        _cur_dev = ai_device_effective(self.cfg)
        # ⚠ 初始化时**必须屏蔽信号**：setCurrentIndex 会触发 _dev_changed
        # → set_ai_device → 反过来改写 cfg，把用户配置改成"刚建出来的那一刻"的值
        # （实测：cfg.ai_gpu=False 打开对话框后 ai_device 变成 gpu）。
        _blk = dev.blockSignals(True)
        # 按 data 查找：老配置里出现未知设备值时也不该 KeyError 打不开面板。
        _dev_ix = dev.findData(_cur_dev)
        dev.setCurrentIndex(_dev_ix if _dev_ix >= 0 else 0)
        dev.blockSignals(_blk)
        genf.addRow("推理设备", dev)

        def _dev_changed(*_a):
            """下拉变了 → 统一走 set_ai_device（它负责落盘 + 同步菜单/复选框）。"""
            if _syncing.get("on"):
                return          # 复选框推过来的，别回推成环
            self.set_ai_device(str(dev.currentData() or "auto"))
            _paint_gpu_state()
        dev.currentIndexChanged.connect(_dev_changed)
        _dev_holder["w"] = dev
        dlg._dev_changed = _dev_changed

        threads = QSpinBox()
        threads.setRange(0, 4)
        threads.setSpecialValueText("自动（≤核心数一半）")
        threads.setValue(int(getattr(self.cfg, "ai_threads", 0) or 0))
        genf.addRow("推理线程数", threads)
        genf.addRow(QLabel("内置 CLIP 离线可用；选其它模式请到对应标签页填参数。"))

        # R149-B：学习开关同步显示在 AI 设置页（与「AI与过滤」菜单同一份状态）。
        learn_chk = QCheckBox("启用学习（记录 AI 偏好权重）")
        learn_chk.setChecked(bool(getattr(self.cfg, "learn_enabled", True)))
        learn_chk.setToolTip("关闭后：逐张「喜欢 / 不喜欢」不再记录偏好权重，"
                             "审核、删除、隔离不受影响。\n"
                             "导入本地图库自动学到的偏好词、以及判退图片学到的回避词不受该开关影响。")
        genf.addRow(learn_chk)
        dlg._learn_chk = learn_chk
        tabs.addTab(tab_gen, "通用")
        tabs.addTab(tab_local, "到本地")
        tabs.addTab(tab_api, "在线 API")
        tabs.addTab(tab_dl, "模型库")

        # ---- 按模式切页/ 禁用（#44 契约 + C9 标签页）----
        # 2026-10-27：local 档已取消（见 MODES 注释），标签索引跟着重排。
        _TAB_OF = {"builtin": 0, "api": 2, "notdownloaded": 3}

        def apply_mode():
            m = mode.currentData()
            dlg._mode = m
            dlg._reveal_local = (m == "local")
            dlg._reveal_api = (m == "api")
            dlg._reveal_dl = (m == "notdownloaded")
            # 用不到的标签 disable（不是隐藏）：标签栏始终看得到全部四个入口，
            # 但当前模式以外的点不动，避免"标签栏少了一个页"那种看不懂的界面。
            for ix in range(tabs.count()):
                tabs.setTabEnabled(ix, ix == _TAB_OF.get(m, 0))
            tabs.setCurrentIndex(_TAB_OF.get(m, 0))
            local_container.setVisible(dlg._reveal_local)
            api_container.setVisible(dlg._reveal_api)
            dl_container.setVisible(dlg._reveal_dl)
        dlg._mode_combo = mode
        dlg._apply_mode = apply_mode
        dlg._tabs = tabs
        dlg._local_container = local_container
        dlg._api_container = api_container
        dlg._dl_container = dl_container
        dlg._device_combo = dev
        dlg._threads_spin = threads
        mode.currentIndexChanged.connect(lambda *_: apply_mode())
        apply_mode()

        # ---- 全中文按钮（#44：禁 OK/Cancel）----
        bb = QWidget()
        bh = QHBoxLayout(bb)
        bh.setContentsMargins(0, 0, 0, 0)
        bh.addStretch(1)
        b_save = QPushButton("保存")
        b_close = QPushButton("关闭")
        bh.addWidget(b_save)
        bh.addWidget(b_close)
        root.addWidget(bb)      # C9：现在是标签页布局，按钮挂在根布局

        def do_save():
            m = mode.currentData()
            act_ai_gpu_obj = getattr(self, "act_ai_gpu", None)
            # 「未下载」= 本地模型（从模型库下载/选用），所以仍写 local；
            # 但界面上不再有「自定义目录」这一档（2026-10-27 用户要求删除）。
            self.cfg.ai_backend = {
                "builtin": "builtin",
                "notdownloaded": "local",
                "api": "api",
            }[m]
            if m == "api":
                self.cfg.ai_model_path = ""   # API 模式不用本地路径
                self.cfg.ai_api_url = url_in.text().strip()
                self.cfg.ai_api_key = key_in.text().strip()
                self.cfg.ai_api_model = model_in.text().strip()
            else:  # builtin / notdownloaded：本地路径交给模型库「设为当前」
                self.cfg.ai_model_path = path_in.text().strip()
            # R172-1：设备以**顶部那个复选框**为准（它有即时落盘 + 菜单同步），
            # 下拉只是同一份状态的另一视图。两处已双向同步，这里再按复选框
            # 兜一次底：即使某条路径漏了同步，也不会让「保存」把开关覆盖回去。
            self.cfg.ai_gpu = bool(gpu_chk.isChecked())
            if act_ai_gpu_obj is not None:
                try:
                    act_ai_gpu_obj.setChecked(self.cfg.ai_gpu)
                except RuntimeError:
                    pass
            self.cfg.ai_threads = threads.value()
            # R149-B：学习开关状态随 AI 设置页一起保存，并同步全局标志 + 菜单勾选。
            self.cfg.learn_enabled = bool(learn_chk.isChecked())
            try:
                from ..classify.preference import set_learning_enabled
                set_learning_enabled(self.cfg.learn_enabled)
                if getattr(self, "_act_learn", None) is not None:
                    self._act_learn.setChecked(self.cfg.learn_enabled)
            except Exception:
                pass
            try:
                from ..config import save_config
                save_config(self.cfg)
            except Exception:
                pass
            self.log_signal.emit(
                f"AI 模型设置已保存（模式={m}，"
                f"设备={'GPU优先' if self.cfg.ai_gpu else 'CPU'}，"
                f"线程={self.cfg.ai_threads or '自动'}）")
            dlg.accept()
        dlg._do_save = do_save
        b_save.clicked.connect(do_save)
        b_close.clicked.connect(dlg.reject)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

    def set_skin(self, key):
        """切换皮肤预设 —— 用户在选择器里点的**唯一**外观入口。

        一个预设同时决定强调色与明暗，所以这里要写三处：
          · `cfg.ui_preset`  用户的真实选择（落盘这个）；
          · `cfg.skin`       强调色（取色引擎层的老字段仍被自绘控件读）；
          · `cfg.dark_mode`  派生的明暗标志（自绘控件有十几处读它）。
        ⚠ 后两个是**派生**值，必须与 ui_preset 保持一致 —— 不一致的话
        QSS 走预设、自绘走 dark_mode，同一屏两套观感。
        """
        from . import theme
        from ..config import save_config
        # R160：自定义键也是合法选择，归一走 theme.resolve_preset_key
        # （各处的「不是内建就回落默认」判断已收在那一个函数里）。
        key = theme.resolve_preset_key(key)
        self.cfg.ui_preset = key
        self.cfg.skin = theme.preset_accent(key)
        self.cfg.dark_mode = theme.preset_dark(key)
        try:
            save_config(self.cfg)
        except Exception:
            pass
        theme.apply_preset(QApplication.instance(), key)
        self._refresh_skin_checks()
        # #qB皮肤：皮肤带「框架布局」，换皮肤可能要换控件位置/显隐
        # （qB=顶栏工具条 + 分组侧栏 + 状态栏；其它=原布局）。
        # 这里只切显隐与宽度，**不重建窗口** —— 三页状态、任务选择全部保留。
        try:
            self._apply_layout_visibility()
            self._refresh_nav_badges()
        except Exception:
            pass
        try:
            self.log_signal.emit(f"已切换皮肤：{theme.preset_label(key)}")
        except Exception:
            pass

    def _refresh_skin_checks(self):
        cur = getattr(self.cfg, "ui_preset", None)
        for _n, _a in getattr(self, "_skin_actions", {}).items():
            try:
                _a.setChecked(_n == cur)
            except Exception:
                pass

    def _open_skins_dir(self):
        """在文件管理器里打开自定义皮肤目录（R160 #85）。

        ⚠ 目录**不存在时也要建出来**：给用户一个空目录比弹「找不到路径」
          有用得多 —— 他要的就是「把文件放进去」这一个动作。
        ⚠ 打开失败静默：这只是在「设置」里的一个便利入口，不该因为
          系统没有关联程序就把整个操作打断。
        """
        try:
            from . import theme
            d = theme.custom_skins_dir()
            d.mkdir(parents=True, exist_ok=True)
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))
            self.log_signal.emit(f"已打开皮肤目录：{d.name}")
        except Exception:
            pass

    def toggle_recycle(self):
        """开关：删除时是否先经过回收站（隔离区，可还原）。"""
        self.cfg.use_recycle = self.act_recycle.isChecked()
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log_signal.emit(
            "删除方式已切换："
            + ("先进回收站（可还原）" if self.cfg.use_recycle else "直接永久删除"))

    def toggle_backup(self):
        """开关：每天首次启动是否自动备份数据库。"""
        self.cfg.daily_backup = self.act_backup.isChecked()
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log_signal.emit(
            "每日自动备份：" + ("已开启" if self.cfg.daily_backup else "已关闭"))

    def toggle_sanitize(self):
        """开关：日志是否脱敏（URL 只留域名、本机路径只留文件名）。"""
        self.cfg.log_sanitize = bool(self.act_sanitize.isChecked())
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        try:
            setup_logging(self.cfg.log_dir,
                          sink=lambda m: self.log_signal.emit(m),
                          sanitize=self.cfg.log_sanitize)
        except Exception:
            pass
        self.log_signal.emit(
            "日志脱敏：" + ("已开启（链接只留域名、路径只留文件名）"
                            if self.cfg.log_sanitize else "已关闭（记录完整地址）"))

    def toggle_blur(self):
        """开关：敏感图是否在入库前做本地模糊（#35）。"""
        self.cfg.blur_sensitive = bool(self.act_blur.isChecked())
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log_signal.emit(
            "敏感图入库前模糊：" + ("已开启" if self.cfg.blur_sensitive else "已关闭")
            + f"（当前敏感词 {len(getattr(self.cfg, 'sensitive_words', []) or [])} 个）")

    def set_ai_device(self, device: str):
        """设置推理设备：auto / gpu / npu / cpu（2026-10-27，npu 于本单补齐）。

        · auto：有显卡后端就用显卡；
        · gpu ：强制显卡（没有就明确提示，不偷偷降级）；
        · npu ：强制 NPU（高通 QNN / AMD Ryzen AI / 英特尔 OpenVINO）；
        · cpu ：强制 CPU。
        同步老字段 ai_gpu（auto/gpu/npu → True，cpu → False），旧配置读取不受影响；
        npu 视为非 CPU，故 ai_gpu 保持 True。
        """
        dev = str(device or "auto").lower()
        if dev not in ("auto", "gpu", "npu", "cpu"):
            dev = "auto"
        self.cfg.ai_device = dev
        self.cfg.ai_gpu = dev != "cpu"
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        # 同步**当前打开的** AI 对话框里的复选框（否则下拉切了 GPU，
        # 对话框顶部还显示"没勾"，两处打架 —— r172 那条就是抓这个）。
        _dlg_chk = getattr(self, "_dlg_gpu_chk", None)
        if _dlg_chk is not None:
            try:
                _dlg_chk.setChecked(dev != "cpu")
            except RuntimeError:
                pass
        for k, a in getattr(self, "_dev_actions", {}).items():
            try:
                a.setChecked(k == dev)
            except Exception:
                pass
        try:
            self.act_ai_gpu.setChecked(dev != "cpu")
        except Exception:
            pass
        label = {"auto": "自动", "gpu": "强制显卡", "npu": "强制用 NPU",
                 "cpu": "强制 CPU"}.get(dev, dev)
        try:
            self.log(f"推理设备已设为：{label}（下一张图生效）")
        except Exception:
            pass
        # 立刻重建会话，让用户下次看图就走新设备
        try:
            self._clf = None
        except Exception:
            pass

    def toggle_ai_gpu(self, on=None):
        """CPU / GPU 加速切换：只改配置，下次运行任务时重建会话才生效。

        关掉 = 强制 CPUExecutionProvider（不再去挑 CUDA/DirectML）；
        开着 = 优先显卡后端，建不成再自动回退 CPU。
        """
        want = self.act_ai_gpu.isChecked() if on is None else bool(on)
        self.act_ai_gpu.setChecked(want)
        self.cfg.ai_gpu = bool(want)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        # 顺手报一下本机到底有哪些后端，省得开关了不知道有没有生效
        avail = []
        try:
            import onnxruntime as ort
            avail = list(ort.get_available_providers())
        except Exception:
            pass
        tail = f"（本机可用后端：{'、'.join(avail) or '未知'}）" if avail else ""
        self.log_signal.emit(
            "AI 推理设备：" + ("GPU 加速（优先显卡，失败自动回退 CPU）"
                              if want else "强制 CPU（不使用显卡）") + tail
            + "（下次运行任务生效）")

    def edit_ai_threads(self):
        """AI 推理线程数：卡机器时调小，机器很闲想快点可以调大。"""
        from PySide6.QtWidgets import QInputDialog
        import os
        cpu = os.cpu_count() or 4
        cur = int(getattr(self.cfg, "ai_threads", 0) or 0)
        val, ok = QInputDialog.getInt(
            self, "AI 推理线程数",
            f"每张图打分时用几个 CPU 线程？（本机 {cpu} 核）\n"
            "0 = 自动（最多 4，且不超过核心数一半，推荐）\n"
            "机器卡就把这个值调小（1~2）；想更快就调到 4~8。",
            cur, 0, 16, 1)
        if not ok:
            return
        self.cfg.ai_threads = int(val)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log_signal.emit(
            "AI 推理线程数：" + (f"{val}（下次任务生效）" if val
                                 else "自动（最多 4）"))

    def edit_max_pages(self):
        """「新建任务」时预填多少最大抓取页数（0=不限）。

        注意：这里只是**默认值**，不再Runtime限制任何任务。真正管用的是
        每个任务表单里的「最大页数」（任务级 > 规则级 > 这里）。
        """
        from PySide6.QtWidgets import QInputDialog
        cur = int(getattr(self.cfg, "max_pages", 0) or 0)
        val, ok = QInputDialog.getInt(
            self, "新建任务默认抓取页数",
            "以后【新建任务】时，预填『最大抓取页数』为多少？\n"
            "0 = 不限（默认，抓到没新图为止）\n"
            "这里只影响以后新建的任务，不会改动已有任务；\n"
            "单个任务想设上限，去「编辑任务 → 抓取深度旁边 → 最大页数」。\n"
            "抓取较慢或站点分页极多时，可考虑设为 100~500。",
            cur, 0, 100000, 50)
        if not ok:
            return
        self.cfg.max_pages = int(val)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log_signal.emit(
            "新建任务默认抓取页数：" + (f"{val} 页" if val else "不限（每个任务自己设）"))

    def edit_disk_threshold(self):
        """#54 设置磁盘剩余空间预警阈值（MB，0=不检查）。"""
        from PySide6.QtWidgets import QInputDialog
        cur = int(getattr(self.cfg, "min_free_mb", 200) or 0)
        val, ok = QInputDialog.getInt(
            self, "磁盘剩余空间预警",
            "保存盘剩余空间低于多少 MB 时自动暂停所有任务？\n"
            "（0 = 不检查；建议 200 ~ 1024）",
            cur, 0, 102400, 50)
        if not ok:
            return
        self.cfg.min_free_mb = int(val)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log_signal.emit(
            "磁盘空间预警阈值：" + (f"{val} MB" if val else "已关闭（不检查）"))

    def integrity_dialog(self):
        """#55 文件完整性校验：缺失 / 0 字节 / 过小 / 无法解码。

        只提示、只统计，**绝不自动删除**——清不清理由用户勾选后自己点。
        """
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QTableWidget, QTableWidgetItem, QAbstractItemView, QCheckBox,
            QHeaderView)

        dlg = QDialog(self)
        dlg.setWindowTitle("文件完整性检查")
        dlg.resize(880, 560)
        v = QVBoxLayout(dlg)

        chk_deep = QCheckBox("深度检查：逐张解码验证（图多时较慢）")
        chk_deep.setToolTip("默认只查「文件缺失 / 0 字节 / 过小」；\n"
                            "勾选后还会用 Pillow 打开每张图确认能正常解码。")
        v.addWidget(chk_deep)

        tbl = QTableWidget(0, 4, dlg)
        tbl.setHorizontalHeaderLabels(["ID", "文件名", "问题", "大小"])
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.verticalHeader().setVisible(False)
        try:
            tbl.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Stretch)
        except Exception:
            pass
        v.addWidget(tbl, 1)

        lbl = QLabel("准备扫描…")
        v.addWidget(lbl)

        self._integrity_rows: list = []

        def run_scan():
            lbl.setText("正在扫描（后台线程）…")
            tbl.setRowCount(0)
            deep = chk_deep.isChecked()
            susp = int(getattr(self.cfg, "suspicious_bytes", 1024) or 0)
            db_path = self.cfg.db_path

            class _Scan(QThread):
                done = Signal(object)

                def run(self_):
                    db = Database(db_path)
                    try:
                        rows = db.query(
                            "SELECT id, path FROM images "
                            "WHERE path IS NOT NULL AND path<>''") or []
                    finally:
                        db.close()
                    bad = []
                    for r in rows:
                        p = Path(r["path"])
                        try:
                            if not p.exists():
                                bad.append((int(r["id"]), p.name,
                                            "文件缺失", 0)); continue
                            sz = p.stat().st_size
                        except Exception:
                            continue
                        if sz == 0:
                            bad.append((int(r["id"]), p.name, "0 字节", sz))
                            continue
                        if susp and sz < susp:
                            bad.append((int(r["id"]), p.name,
                                        f"过小(<{susp}B)", sz)); continue
                        if deep:
                            try:
                                from PIL import Image
                                with Image.open(p) as im:
                                    im.verify()
                            except Exception:
                                bad.append((int(r["id"]), p.name,
                                            "无法解码", sz))
                    self_.done.emit(bad)

            def _fill(bad):
                self._integrity_rows = list(bad)
                tbl.setRowCount(0)
                for iid, name, why, sz in bad:
                    i = tbl.rowCount()
                    tbl.insertRow(i)
                    for c, val in enumerate(
                            (str(iid), name, why,
                             (f"{sz} B" if sz else "-"))):
                        tbl.setItem(i, c, QTableWidgetItem(val))
                kinds: dict = {}
                for _, _, why, _ in bad:
                    kinds[why] = kinds.get(why, 0) + 1
                detail = "、".join(f"{k} {n}" for k, n in kinds.items())
                lbl.setText(f"共发现 {len(bad)} 条异常"
                            + (f"（{detail}）" if detail else "——全部正常"))

            scan = _Scan()
            scan.done.connect(_fill)
            scan.finished.connect(scan.deleteLater)
            self._integrity_scan = scan      # 持有引用，避免被回收
            scan.start()

        def clean_selected():
            idxs = tbl.selectionModel().selectedRows()
            if not idxs:
                QMessageBox.information(dlg, "清理", "先选中要清理的行")
                return
            ids = [int(tbl.item(x.row(), 0).text()) for x in idxs]
            if QMessageBox.question(
                    dlg, "清理异常记录",
                    f"删除选中的 {len(ids)} 条数据库记录？\n"
                    "（仅删除数据库记录；磁盘上的异常文件需手工删除，不影响正常图片）"
            ) != QMessageBox.StandardButton.Yes:
                return
            for i in ids:
                # R221/C10：FK 开启后子表先清（reviews 引用 images）
                self.db.execute("DELETE FROM reviews WHERE image_id=?", (i,))
                self.db.execute("DELETE FROM images WHERE id=?", (i,))
            self.db.audit("integrity", "clean", str(len(ids)))
            self.log_signal.emit(f"完整性检查：清理异常记录 {len(ids)} 条")
            run_scan()

        bar = QHBoxLayout()
        b_scan = QPushButton("开始扫描")
        b_scan.clicked.connect(run_scan)
        b_clean = QPushButton("清理选中记录")
        _tstyle(b_clean, "QPushButton{color:{danger};font-weight:600}")
        b_clean.clicked.connect(clean_selected)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(dlg.accept)
        bar.addWidget(b_scan)
        bar.addWidget(b_clean)
        bar.addWidget(b_close)
        v.addLayout(bar)
        dlg.exec()
        self.load_gallery()

    def edit_sensitive_words(self):
        """设置触发模糊的敏感词（逗号/空格/换行分隔）。"""
        from PySide6.QtWidgets import QInputDialog
        cur = " ".join(getattr(self.cfg, "sensitive_words", []) or [])
        text, ok = QInputDialog.getMultiLineText(
            self, "敏感词设置",
            "命中这些词的图片会被本地模糊后入库（空格 / 逗号 / 换行分隔）：\n"
            "例如：人脸 身份证 护照 水印 nude watermark",
            cur)
        if not ok:
            return
        words = [w.strip() for w in re.split(r"[\s,，;；]+", text or "") if w.strip()]
        self.cfg.sensitive_words = words
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        self.log_signal.emit(f"敏感词已更新（{len(words)} 个）: " + ", ".join(words))

    # ---------------- 需求 30：查找类似图片（三路加权，后台算） ----------------
    def gallery_find_similar_images(self, *_):
        """对当前选中的图，找出「同一类画面、道具可以不同」的相似图。

        取代原来的「查找相似(SSIM)」：SSIM 只能找近乎一样的近重复图，
        而用户要的是语义同类（小女孩抱洋娃娃 → 抱狗 / 抱猫那一类）。
        算法在 media_resource_manager/simsearch.py：
        主题词 Jaccard 0.42 + 整图三重色调 0.33 + pHash 0.25。

        提特征要逐张解码，实测 400 张 ≈ 15s —— 必须后台线程，
        主线程只负责弹框和铺结果。重复点直接忽略，避免堆一串后台任务。
        """
        if getattr(self, "_simsearch_busy", False):
            return
        iid = self._gallery_selected()
        if iid is None:
            QMessageBox.information(self, "提示", "先选中一张图片再操作")
            return
        self._simsearch_busy = True

        def _work():
            """后台：读种子 + 候选，提取特征，排序。全程不碰 Qt。"""
            from ..simsearch import features_for_image, rank_similar
            rows = self.db.query(
                "SELECT id, path, tags, phash FROM images "
                "WHERE path IS NOT NULL AND in_quarantine=0 "
                "AND tags IS NOT NULL AND tags NOT IN ('', '[]') "
                "ORDER BY id DESC LIMIT 600")
            cands = []
            for r in rows:
                f = features_for_image(r["path"])
                if f is None:
                    continue
                cands.append({"id": r["id"], "path": r["path"],
                              "tags": r["tags"], "phash": r["phash"],
                              "color": f})
            seed_row = next((c for c in cands if c["id"] == iid), None)
            if seed_row is None:
                # 种子自己没提到特征（文件没了/太小）→ 退化成只按标签和哈希
                sr = self.db.query(
                    "SELECT id, path, tags, phash FROM images WHERE id=?",
                    (iid,))
                if not sr:
                    return None, None, 0
                s = sr[0]
                seed_row = {"id": s["id"], "path": s["path"],
                            "tags": s["tags"], "phash": s["phash"],
                            "color": None}
            hit = rank_similar(seed_row, cands, limit=60)
            return seed_row, hit, len(cands)

        def _done(res):
            self._simsearch_busy = False
            seed, hits, n_cand = res
            if seed is None:
                QMessageBox.information(self, "查找类似图片", "这张图已不在库里了。")
                return
            if not hits:
                QMessageBox.information(
                    self, "查找类似图片",
                    f"在 {n_cand} 张候选图里没找到评分够高的。\n\n"
                    "提示：带 AI 标签的图才推荐得准；库里标签越全越像。")
                return
            self.log_signal.emit(
                f"查找类似图片：{seed['id']} → {len(hits)} 条"
                f"（候选 {n_cand} 张，主题词/色调/结构三路加权）")
            try:
                from .simsearch_dialog import SimilarImagesDialog
            except Exception as e:
                QMessageBox.warning(self, "查找类似图片", f"结果窗口打不开：{e}")
                return
            dlg = SimilarImagesDialog(seed, hits, self)
            # 自持引用：裸局部变量会被 GC 回收，对话框当场消失（r39b 铁律）
            self._simsearch_dlg = dlg
            dlg.destroyed.connect(
                lambda *a, **k: setattr(self, "_simsearch_dlg", None))
            dlg.show()

        self.log_signal.emit("查找类似图片：正在提取特征，几秒到十几秒…")
        self._bg_submit(_work, _done, "查找类似图片")

    def gallery_blur(self):
        """图库右键：把选中图另存为模糊版（原图保留，图库改显示模糊版）。"""
        from ..security.blur import blur_file
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(self, "提示",
                                    "先选中图片（可 Ctrl / Shift 多选）")
            return
        if QMessageBox.question(
                self, "模糊化处理",
                f"把选中的 {len(ids)} 张另存为马赛克版本？\n"
                "原图保留在磁盘（不在图库显示），可随时改回。") \
                != QMessageBox.StandardButton.Yes:
            return
        n = 0
        for iid in ids:
            row = self.db.query("SELECT path FROM images WHERE id=?", (iid,))
            if not row or not row[0]["path"]:
                continue
            p = Path(row[0]["path"])
            if not p.exists():
                continue
            dst = blur_file(p)
            if dst is None:
                continue
            self.db.execute("UPDATE images SET path=?, width=NULL WHERE id=?",
                            (str(dst), iid))
            self.db.audit("gallery", "blur", str(iid))
            n += 1
        self.log_signal.emit(f"图库: 已生成 {n} 张模糊版")
        self.load_gallery()

    def quarantine_manager(self):
        """隔离区管理：查看 / 清理缺失 / 还原 / 彻底删除 / 清空。

        隔离区（回收站）里的图是「不喜欢」或被 AI / 黑名单拦下的，文件已移到
        %APPDATA%/.../quarantine。这里给出统一入口做磁盘一致性清理。

        R181：从独立 QDialog 弹窗改为**主窗口内嵌页**（stack 索引 3，
        与图库/审核同级；复用图库/审核同一套表格/网格组件，不另写第二套
        界面）。构建是 **build-once**：首次调用时建页并挂进 stack，
        之后每次调用（导航点击 / 菜单「隔离区 → 隔离区管理…」）都只是
        「切到该页 + 重载数据 + 同步角标」。

        ⚠ 口径契约（R143，桌面端与 Web 端必须逐字一致）：下面的
        ``_QUAR_WHERE`` 与 webui.py ``_api_view`` 的 "quarantine" 视图
        where 是同一份语义 —— 两边任一侧改口径都要同步改另一侧，
        否则用户会看到「网页隔离区和桌面隔离区数量对不上」。
        """
        page = getattr(self, "_quar_tab", None)
        if page is not None:
            # 已构建（常态路径）：切页 + 重载 + 同步选中态/角标。
            try:
                self.stack.setCurrentIndex(self.stack.indexOf(page))
                self._sync_nav_checks()
                self._quar_reload()
                self._refresh_nav_badges()
            except Exception as e:
                try:
                    self.log_signal.emit(f"打开隔离区失败：{e}")
                except Exception:
                    pass
            return

        # ---- 以下只跑一次（build-once）：构建内嵌页 ----
        # R181：原 QDialog 弹窗整体搬迁成内嵌页；父控件最终由
        # stack.addWidget 重新认领，这里先以 self 为父建控件。
        from PySide6.QtWidgets import (
            QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
            QTableWidget, QTableWidgetItem, QAbstractItemView, QHeaderView,
            QSplitter)
        from PySide6.QtGui import QPixmap
        from ..repo import get_task
        from ..utils.url import domain_of
        from .table_view import selected_ids as tbl_selected, grid_selected_ids

        page = QWidget(self)
        v = QVBoxLayout(page)

        # R143：隔离区口径常量。**桌面端与 Web 端 `webui._api_view` 的 where
        # 映射必须逐字一致** —— 两边任一侧改口径都要同步改另一侧。
        _QUAR_WHERE = "in_quarantine=1 AND needs_review=0"
        # 分批大小。原来写死 LIMIT 1000 且没有翻页/加载更多 ⇒ 超过 1000 条
        # 时「后面的全看不到」（用户实测反馈），且标题把本批条数当总数。
        _QUAR_PAGE = 500

        def count_all() -> int:
            """隔离区**真总数**（单独 COUNT，与分页无关）。

            R143 第 4 条：标题必须显示真总数，不能把本批条数当总数 ——
            那会让人误以为看到的就是全部。
            """
            try:
                return int(self.db.query(
                    "SELECT COUNT(*) c FROM images WHERE " + _QUAR_WHERE
                )[0]["c"] or 0)
            except Exception:
                return 0

        def all_quarantine_rows():
            """**全量**隔离记录（不受分页限制）。

            清理缺失 / 清空隔离区必须覆盖全部，否则只处理了最新一批、
            剩下的记录还在，用户却以为清干净了。
            """
            return [dict(r) for r in (self.db.query(
                "SELECT id, path FROM images WHERE " + _QUAR_WHERE) or [])]

        def load_rows(limit=_QUAR_PAGE, offset=0):
            # 隔离区只列「用户主动操作进来的回收站」（needs_review=0）；
            # 审核队列里的待定图(needs_review=1)走审核页，不在这里混。
            # r32：加 collected_at —— 「入库时间」列 + 点表头能按它排序
            # R143：LIMIT ? OFFSET ? 取代写死的 LIMIT 1000，配「加载更多」。
            return [dict(r) for r in (self.db.query(
                "SELECT id, path, task_id, score, bytes, width, height, "
                "source_url, blacklisted, favorite, collected_at FROM images "
                "WHERE " + _QUAR_WHERE + " "
                "ORDER BY id DESC LIMIT ? OFFSET ?", (int(limit), int(offset))
            ) or [])]

        # 与图库/审核列表同一套表格模块（旧版隔离区同款样式，7 列含网站）
        from .table_view import make_table, fill_table
        # R158-Bug4：multi=True 开 Ctrl/Shift 多选（原来只能一张张点）。
        tbl = make_table(page, on_like=lambda: restore(),
                         on_dislike=lambda: purge(), multi=True)

        # R158-Bug4：网格视图。复用图库同一只 ``_GalleryList``（滚轮步长、
        # 「右键不动选择」都是踩过的坑），缩略图口径也与图库一致。
        grid = make_quarantine_grid()
        grid.itemSelectionChanged.connect(lambda: show_preview())
        # 两个视图叠在一起，切视图不动数据（st["rows"] 只有一份）。
        views = QStackedWidget()
        views.addWidget(tbl)
        views.addWidget(grid)

        def current_ids():
            """当前视图（表格 or 网格）选中的 id。

            ⚠ 少了这一层，切到网格后「还原选中」会去读**表格**的选择 ——
            表格此时一个没选 ⇒ 弹「先选中行」，用户以为功能坏了。
            """
            return (grid_selected_ids(grid)
                    if views.currentWidget() is grid else tbl_selected(tbl))

        lbl = QLabel("")
        v.addWidget(lbl)
        btn_more = QPushButton("加载更多")
        btn_more.setVisible(False)

        # 已载入的行与偏移量放容器里：嵌套函数改外层名字要 nonlocal，
        # 而这里 reload_rows 是在 for 循环之前定义的，用容器最稳。
        st = {"rows": [], "off": 0}

        def reload_rows(more=False):
            """刷**对话框自己**的表格（项14：四个操作回调都必须调它）。

            ⚠ 名字不能叫 refresh：R39 的 AST 护栏判「类内裸调用同类方法名」，
            而 MainWindow.refresh 是同类方法 —— 裸调 refresh() 会被判成
            「把 self.refresh 当全局函数」那种 bug。reload_rows 不重名。

            R143：more=True = 追加下一批；False = 从头重来（刷新/任何改动后）。
            """
            if more:
                batch = load_rows(_QUAR_PAGE, st["off"])
                st["rows"] = st["rows"] + batch
                st["off"] += len(batch)
            else:
                st["rows"] = load_rows(_QUAR_PAGE, 0)
                st["off"] = len(st["rows"])
            total = count_all()
            # R147：隔离区删除/还原/清空后 reload 选中"下一张"（追加更多=more=True 不改选中）
            _q_anchor = None if more else (
                tbl.currentRow() if tbl.currentRow() >= 0 else None)
            shown, missing = fill_table(tbl, st["rows"], self.db,
                                        get_task_fn=get_task,
                                        domain_fn=domain_of,
                                        select_row=_q_anchor)
            # R158-Bug4：网格与表格共用同一份已载入数据，切视图不重新取数。
            fill_grid()
            lbl.setText(f"隔离区共 {total} 条（当前已载入 {shown} 条）："
                        f"在盘 {shown - missing}、磁盘已缺失 {missing}")
            rest = total - st["off"]
            btn_more.setVisible(rest > 0)
            if rest > 0:
                btn_more.setText(f"加载更多（还有 {rest} 条）")

        def load_more():
            reload_rows(more=True)

        btn_more.clicked.connect(load_more)

        selected_ids = lambda: tbl_selected(tbl)

        def fill_grid():
            """把已载入的行灌进网格视图（每条 ℹ 同步解码，受 cap 保护）。

            ⚠ 只画**当前已载入**的批次：隔离区可能有几千条，一次性全解码
            会把「打开隔离区」变成几十秒假死。要看更多先选择「加载更多」。
            """
            grid.clear()
            cache = None
            try:
                cache = str(self._thumb_dir())
            except Exception:
                cache = None
            for it in quarantine_grid_items(st["rows"], cache_dir=cache):
                grid.addItem(it)

        def clean_missing():
            # R143：用**全量**隔离记录，不再只扫最新一批（分页后 load_rows()
            # 只返回当前页，照旧写会漏掉绝大部分缺失记录）。
            rows = all_quarantine_rows()
            ids = [int(r["id"]) for r in rows
                   if not (r["path"] and Path(r["path"]).exists())]
            if not ids:
                QMessageBox.information(page, "隔离区", "没有磁盘已缺失的记录")
                return
            if QMessageBox.question(
                    page, "清理缺失",
                    f"删除 {len(ids)} 条「磁盘已缺失」的隔离记录？（只删数据库记录，不动磁盘）"
            ) != QMessageBox.StandardButton.Yes:
                return
            for i in ids:
                # R221/C10：FK 开启后子表先清（reviews 引用 images）
                self.db.execute("DELETE FROM reviews WHERE image_id=?", (i,))
                self.db.execute("DELETE FROM images WHERE id=?", (i,))
            self.db.audit("quarantine", "clean-missing", str(len(ids)))
            self.log_signal.emit(f"隔离区：清理缺失记录 {len(ids)} 条")
            reload_rows()      # 项14：先刷对话框自己的表格（旧代码只刷主窗口）
            self.refresh()   # 再同步主窗口（图库计数/统计）

        def restore():
            ids = current_ids()
            if not ids:
                QMessageBox.information(page, "还原", "先在表格里选中要还原的行")
                return
            if QMessageBox.question(
                    page, "还原到图库",
                    f"把选中的 {len(ids)} 张还原回图库？（文件会搬回图库文件夹，不再隔离）"
            ) != QMessageBox.StandardButton.Yes:
                return
            from ..repo import get_image
            from ..review.queue import ReviewQueue
            rq = ReviewQueue(self.db, self.cfg.isolate_dir, self.cfg)
            n = 0
            for i in ids:
                rec = get_image(self.db, i)
                if not rec:
                    continue
                # R166：还原回图库 = 正样本（用户把它从隔离区捞回来，
                # 明确表示"这张我其实想要"）。这是 `restore` 与其他正向
                # 动作分开的来源，看板上能一眼看出"有多少是从隔离区救回来的"。
                # ⚠ 学在**改标志位之前**：`rec.tags` 是模型里的现成值，
                #   还原动作不改标签，但先学能保证「这次判断"针对的是
                #   当时那张图」的语义（与真删除的"先学再删"同一原则）。
                try:
                    from ..classify.preference import PreferenceLearner
                    PreferenceLearner(self.db, 0).learn_action(
                        "restore", list(getattr(rec, "tags", None) or []),
                        source="quarantine", image_id=i,
                        sample_task_id=getattr(rec, "task_id", None))
                except Exception:
                    pass
                # 三文件夹分离：还原时文件必须真正搬回图库文件夹（按域名子目录）
                newp = rq._release_to_gallery(rec)
                self.db.execute(
                    "UPDATE images SET in_quarantine=0, needs_review=0, "
                    "blacklisted=0, path=? WHERE id=?", (newp, i))
                n += 1
            self.db.audit("quarantine", "restore", str(n))
            self.log_signal.emit(f"隔离区：还原 {n} 张到图库")
            reload_rows()      # 项14：先刷对话框自己的表格（旧代码只刷主窗口）
            self.refresh()   # 再同步主窗口（图库计数/统计）

        def purge():
            ids = current_ids()
            if not ids:
                QMessageBox.information(page, "彻底删除", "先在表格里选中要删除的行")
                return
            # 不再弹确认框（用户要求直接删）。遵循「删除时是否进回收站」开关：
            # 开 → 文件移入 隔离区/_purged（可找回）；关 → 从磁盘永久删除。
            recycle = bool(getattr(self.cfg, "use_recycle", True))
            # V0.0.3：所有删除统一走安全入口，绝不删到用户原有图库（F 盘）
            from ..safe_delete import delete_image_file
            n = 0
            for i in ids:
                row = self.db.query(
                    "SELECT path, task_id, tags FROM images WHERE id=?", (i,))
                # R166：彻底删除 = **强负样本**（比普通"不喜欢"更重，且不可
                # 还原 —— 用户最确定的一次"不要"）。必须**先学再删**：
                # 下面会 DELETE 掉图片行，行没了就再也取不到 tags 与 task_id。
                if row:
                    try:
                        from ..classify.preference import PreferenceLearner
                        PreferenceLearner(self.db, 0).learn_action(
                            "purge", self._tags_of_row(row[0]),
                            source="quarantine", image_id=i,
                            sample_task_id=row[0]["task_id"])
                    except Exception:
                        pass
                # V0.0.3：外部图库文件只清登记记录，不动磁盘；搬运失败不降级真删
                outcome = delete_image_file(
                    row[0]["path"] if row else None, self.cfg,
                    recycle=recycle)
                if outcome == "failed":
                    self.log_signal.emit(
                        f"删除失败，原文件已保留：{row[0]['path'] if row else ''}")
                # R221/C10：FK 开启后子表先清（reviews 引用 images）
                self.db.execute("DELETE FROM reviews WHERE image_id=?", (i,))
                self.db.execute("DELETE FROM images WHERE id=?", (i,))
                n += 1
            self.db.audit("quarantine", "purge", str(n))
            self.log_signal.emit(
                f"隔离区：删除 {n} 张登记记录"
                + ("（软件目录内的文件已进回收站；原有图库文件未动）"
                   if recycle else "（软件目录内的文件已永久删除；原有图库文件未动）"))
            reload_rows()      # 项14：先刷对话框自己的表格（旧代码只刷主窗口）
            self.refresh()   # 再同步主窗口（图库计数/统计）

        def purge_all():
            # R143：同理，清空必须覆盖**全部**隔离记录，不是当前页。
            # V0.0.3：清空也必须遵守「删除时先进回收站」开关，
            # 且绝不删用户原有图库的文件（老代码这里无条件真删）。
            recycle = bool(getattr(self.cfg, "use_recycle", True))
            from ..safe_delete import delete_image_file
            rows = all_quarantine_rows()
            if not rows:
                QMessageBox.information(page, "清空", "隔离区已经是空的")
                return
            if QMessageBox.question(
                    page, "清空隔离区",
                    f"清空全部 {len(rows)} 条隔离记录？\n"
                    "软件自己目录里的文件会按开关进回收站或删除；"
                    "原有图库（如 F 盘）的文件不会被改动。"
            ) != QMessageBox.StandardButton.Yes:
                return
            n = 0
            for r in rows:
                # R166：清空 = 逐张「彻底删除」，样本极性同为强负样本。
                # ⚠ 与 purge() 同一条铁律：**先学再删**（下面 DELETE 掉行后
                #   就取不到 tags/task_id 了）。这里按 id 反查一次 ——
                #   ``all_quarantine_rows`` 只取了 id/path 两列。
                try:
                    _r = self.db.query(
                        "SELECT task_id, tags FROM images WHERE id=?",
                        (int(r["id"]),))
                    if _r:
                        from ..classify.preference import PreferenceLearner
                        PreferenceLearner(self.db, 0).learn_action(
                            "purge", self._tags_of_row(_r[0]),
                            source="quarantine", image_id=int(r["id"]),
                            sample_task_id=_r[0]["task_id"])
                except Exception:
                    pass
                outcome = delete_image_file(r["path"], self.cfg,
                                            recycle=recycle)
                if outcome == "failed":
                    self.log_signal.emit(f"删除失败，原文件已保留：{r['path']}")
                # R221/C10：FK 开启后子表先清（reviews 引用 images）
                self.db.execute(
                    "DELETE FROM reviews WHERE image_id=?", (int(r["id"]),))
                self.db.execute("DELETE FROM images WHERE id=?", (int(r["id"]),))
                n += 1
            self.db.audit("quarantine", "purge-all", str(n))
            self.log_signal.emit(
                f"隔离区：已清空 {n} 条登记记录（原有图库文件未动）")
            reload_rows()      # 项14：先刷对话框自己的表格（旧代码只刷主窗口）
            self.refresh()   # 再同步主窗口（图库计数/统计）

        # ---- 右侧缩略图预览 ----
        # 用户反馈：只有文件名时根本不知道要还原哪张，必须能看图。
        prev_box = QWidget()
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(8, 0, 0, 0)
        pv.setSpacing(6)
        pv.addWidget(QLabel("<b>预览</b>"))
        prev_img = QLabel("（选中一行看预览）")
        prev_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        prev_img.setMinimumSize(240, 240)
        _tstyle(prev_img,
                "QLabel{background:{surface_alt};border:1px solid {border};"
                "border-radius:8px;color:{text_dim};}")
        pv.addWidget(prev_img, 1)
        prev_txt = QLabel("")
        prev_txt.setWordWrap(True)
        _tstyle(prev_txt, "QLabel{color:{text_dim};font-size:12px;}")
        pv.addWidget(prev_txt)

        def show_preview():
            """选中行变化时更新右侧缩略图（文件缺失时明确提示）。"""
            ids = current_ids()
            if not ids:
                prev_img.setPixmap(QPixmap())
                prev_img.setText("（选中一行看预览）")
                prev_txt.setText("")
                return
            row = self.db.query("SELECT id, path, source_url, score, width, "
                                "height, bytes FROM images WHERE id=?", (ids[0],))
            if not row:
                return
            r = dict(row[0])      # sqlite3.Row 没有 .get()，转 dict
            p = Path(r["path"]) if r["path"] else None
            if p and p.exists():
                pm = QPixmap(str(p))
                if not pm.isNull():
                    prev_img.setPixmap(pm.scaled(
                        240, 240, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation))
                    prev_img.setText("")
                else:
                    prev_img.setPixmap(QPixmap())
                    prev_img.setText("（这个文件不是有效图片）")
            else:
                prev_img.setPixmap(QPixmap())
                prev_img.setText("（磁盘上已无此文件）")
            size_b = int(r.get("bytes") or 0)
            size_txt = (f"{size_b / (1024 * 1024):.1f} MB" if size_b >= 1024 * 1024
                        else f"{size_b // 1024} KB" if size_b else "-")
            prev_txt.setText(
                f"#{r['id']}  {r.get('width') or '?'}x{r.get('height') or '?'}"
                f"  {size_txt}  AI分 {r.get('score') or 0:.3f}\n"
                f"{p.name if p else '(无路径)'}\n"
                f"{r.get('source_url') or ''}")

        tbl.itemSelectionChanged.connect(show_preview)

        split = QSplitter()
        split.addWidget(views)      # R158-Bug4：表格/网格叠在一起，不再是裸表格
        split.addWidget(prev_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        v.addWidget(split, 1)
        tip = QLabel("↑↓ / Ctrl+点击 / Shift+点击 多选 ·  → 喜欢（还原回图库） ·  "
                     "← 不喜欢（删除，遵循回收站设置）")
        _tstyle(tip, "QLabel{color:{text_dim};font-size:12px;}")
        v.addWidget(tip)
        bar = QHBoxLayout()
        bar.addWidget(btn_more)      # R143：分页入口（还有更多时才显示）

        def toggle_view():
            """表格 ⇄ 网格。

            ⚠ 切过去**不清选择**：用户可能刚勾好 20 张想换个看法，
            一按切换全丢等于逼他重勾一遍（隔着窗人称累了会被误会成 bug）。
            """
            views.setCurrentWidget(grid if views.currentWidget() is tbl
                                   else tbl)
            btn_view.setText("网格视图" if views.currentWidget() is tbl
                             else "列表视图")

        btn_view = QPushButton("网格视图")
        btn_view.clicked.connect(toggle_view)
        bar.addWidget(btn_view)
        for text, fn, danger in (
                ("刷新", reload_rows, False),
                ("清理磁盘已缺失", clean_missing, False),
                ("还原选中到图库", restore, False),
                ("彻底删除选中", purge, True),
                ("清空隔离区", purge_all, True),
                ("_purged 快照…", self.open_purged_snapshot, False)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            if danger:
                _tstyle(b, "QPushButton{color:{danger};font-weight:600}")
            bar.addWidget(b)
        v.addLayout(bar)
        # R181：登记 + 挂进 stack（索引 3）+ 切过去 + 载入第一批。
        # ⚠ reload_rows 必须真调（R143：打开即载入，别让用户先选择「刷新」
        #    才看得到内容 —— 那也是"exe 里看到的数量不对"的一环）。
        # ⚠ 不再有 dlg.exec() / self.load_gallery()：内嵌页没有"关闭"
        #    时刻，图库刷新交给切页时的 _nav_go（与旧行为等价）。
        self._quar_tab = page
        self._quar_reload_impl = reload_rows
        self.stack.addWidget(page)
        self.refresh()
        reload_rows()
        self.stack.setCurrentIndex(self.stack.indexOf(page))
        self._sync_nav_checks()
        self._refresh_nav_badges()
    def open_purged_snapshot(self):
        """浏览 隔离区/_purged（去重输家/彻底删除的删前快照）。

        还原 = 移回 图库/restored 并按文件直接入库（sha256 去重由
        UNIQUE(path) 与回填同款逻辑兜底）；彻底删除 = 真正从磁盘抹掉。
        """
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton)
        from ..repo import upsert_image
        from ..models import ImageRecord
        from ..dedup.hash import sha256_file, image_dimensions
        trash = self.cfg.isolate_dir / "_purged"

        dlg = QDialog(self)
        dlg.setWindowTitle("_purged 快照（删前备份，可找回）")
        dlg.resize(720, 480)
        v = QVBoxLayout(dlg)
        lbl = QLabel("")
        v.addWidget(lbl)
        lst = QListWidget()
        lst.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        v.addWidget(lst, 1)

        def refresh():
            lst.clear()
            files = sorted(trash.glob("*")) if trash.exists() else []
            for p in files:
                if p.is_file():
                    it = QListWidgetItem(
                        f"{p.name}  ({max(1, p.stat().st_size // 1024)} KB)")
                    it.setData(Qt.ItemDataRole.UserRole, str(p))
                    lst.addItem(it)
            lbl.setText(f"快照目录：{trash}（共 {lst.count()} 个文件）")

        def _selected_paths():
            return [it.data(Qt.ItemDataRole.UserRole)
                    for it in lst.selectedItems()]

        def restore_selected():
            sel = _selected_paths()
            if not sel:
                QMessageBox.information(dlg, "还原", "先选中要还原的文件")
                return
            dest_dir = self.cfg.library_dir / "restored"
            n = 0
            for s in sel:
                p = Path(s)
                if not p.exists():
                    continue
                dest_dir.mkdir(parents=True, exist_ok=True)
                dst = dest_dir / p.name
                k = 1
                while dst.exists():
                    dst = dest_dir / f"{p.stem}({k}){p.suffix}"
                    k += 1
                try:
                    shutil.move(str(p), str(dst))
                except Exception:
                    continue
                sha = None
                try:
                    sha = sha256_file(dst)
                except Exception:
                    pass
                w, h = image_dimensions(dst)
                upsert_image(self.db, ImageRecord(
                    id=None, source_url=f"restored::{dst.name}", task_id=0,
                    sha256=sha, width=w, height=h,
                    bytes=dst.stat().st_size, path=str(dst)))
                n += 1
            self.db.audit("purged", "restore", str(n))
            self.log_signal.emit(
                f"_purged 快照：还原 {n} 个文件到 图库/restored 并入库")
            self.refresh()

        def delete_selected():
            sel = _selected_paths()
            if not sel:
                QMessageBox.information(dlg, "删除", "先选中要删除的文件")
                return
            if QMessageBox.question(
                    dlg, "彻底删除",
                    f"从磁盘永久删除选中的 {len(sel)} 个快照文件？不可恢复！"
            ) != QMessageBox.StandardButton.Yes:
                return
            n = 0
            for s in sel:
                try:
                    Path(s).unlink(missing_ok=True)
                    n += 1
                except Exception:
                    pass
            self.db.audit("purged", "delete", str(n))
            self.log_signal.emit(f"_purged 快照：彻底删除 {n} 个文件")
            self.refresh()

        bar = QHBoxLayout()
        for text, fn, danger in (("刷新", lambda: self.refresh(), False),
                                 ("还原选中到图库", restore_selected, False),
                                 ("彻底删除选中", delete_selected, True)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            if danger:
                _tstyle(b, "QPushButton{color:{danger};font-weight:600}")
            bar.addWidget(b)
        v.addLayout(bar)
        self.refresh()
        dlg.exec()

    def _daily_backup(self):
        """每天首次启动自动备份一次数据库，只保留最近 N 份。"""
        if not bool(getattr(self.cfg, "daily_backup", True)):
            return
        try:
            from ..config import user_data_dir
            d = user_data_dir() / "backups"
            d.mkdir(parents=True, exist_ok=True)
            dest = d / f"mrm_db_backup_{datetime.now():%Y%m%d}.db"
            if not dest.exists():
                try:
                    self.db.backup(dest)
                    self.log_signal.emit(f"已自动备份数据库 → {dest.name}")
                except Exception as e:      # noqa: BLE001
                    # 2026-11-05：备份失败不能静默 —— 盘满 / 目录不可写 /
                    # 库被锁时，用户以为每日在自动备份，实际一份都没留下，
                    # 等真要恢复才发现全是空的。留明确日志，别再 except-pass。
                    try:
                        self.log_signal.emit(
                            f"⚠ 自动备份失败（数据库未备份）：{e}")
                    except Exception:
                        pass
            keep = int(getattr(self.cfg, "backup_keep", 7) or 7)
            if keep > 0:
                for old in sorted(d.glob("mrm_db_backup_*.db"))[:-keep]:
                    try:
                        old.unlink()
                    except Exception:
                        pass
        except Exception as e:              # noqa: BLE001
            # 2026-11-05：外层同样不能静默 —— mkdir / 目录计算失败也要看得见。
            try:
                self.log_signal.emit(f"⚠ 自动备份执行异常：{e}")
            except Exception:
                pass
        # #79 外部备份同步：设了同步目录就在启动时静默同步一次
        sync_dir = getattr(self.cfg, "backup_sync_dir", "") or ""
        if sync_dir:
            try:
                ok, msg = self._backup_sync_internal(sync_dir)
                if ok:
                    self.log_signal.emit(f"启动备份同步完成 → {sync_dir}")
            except Exception:
                pass

    def _file_pick(self, caption, start="", filter_="", save=False, dir_only=False):
        """选一个文件或目录路径，返回字符串；用户取消返回 ""（r39e）。

        **为什么必须 `DontUseNativeDialog`（这道坑啃了三轮，实测矩阵）：**

        Qt 在 Windows 上默认走**原生 Win32 文件对话框后端**，这个后端有个
        致命脾气 —— 只要对话框是用 `open()` 弹出去的（qt 侧自己管的那个
        「模态窗口」），再**从 Qt 侧主动**把它 `reject / accept / close /
        hide / done` 关掉，进程立刻 `SIGSEGV`（退出码 139、faulthandler 抓到
        崩溃线程、gui-crash.log 空）。实测 6 种关闭姿势 **3/3 全崩**：
            原生 + open() + reject  → 139（accept/close/hide/done0/hide+del 同样）
            原生 + exec() + reject  → 安全
            非原生 + open()/exec()  → 安全
        也就是：**雷不在「嵌套事件循环」，在原生后端 + open() 被关**。
        上一版把阻塞式 `getSaveFileName` 换成非阻塞 `open()`，等于把雷
        从「循环里点按钮」挪到「外部关对话框」，换了地方没换掉。

        关掉原生后端后外观仍是标准 QFileDialog，但关闭路径全在 Qt 自己
        手里，谁去关（用户点取消、别的线程、收尾逻辑）都安全。
        所以**全项目所有文件选择一律走这个 helper，不许直接调静态函数**。

        ⚠ R47/B10：用户要**原生文件夹选择框**（真机：「输出目录浏览现在用的
        是自绘对话框，用户要的是系统原生文件夹选择框」）。
        按上面那张实测矩阵，「原生 + exec()」这一格本来就写着**安全**
        —— 真正会 139 的是「原生 + open() 被 Qt 侧主动关」。本函数用的正是
        `exec()`（阻塞、Qt 自己管关闭），所以对**目录选择**恢复原生后端是
        安全的，不需要退回自绘。

        为什么只给 `dir_only` 恢复原生、文件选择仍走非原生：
        目录选择在 Windows 上会拉起那个熟悉的「选择文件夹」小窗；
        而保存/打开文件对话框带过滤器，原生后端在不同 Windows 版本上
        行为差异更大、历史上出问题更多，收益小风险大 —— 按「改动面小
        的那种」原则先只放开用户明确点名的那一个。

        另外这里**只读** `d.exec()` 的返回值，绝不从外部 `close/reject/
        hide/deleteLater` 它 —— 那正是 139 的触发条件。
        """
        from PySide6.QtWidgets import QFileDialog, QDialog
        d = QFileDialog(self, caption, start or "", filter_ or "")
        if dir_only:
            # 原生文件夹选择框（B10）。用 exec() 而非 open()，从根上避开
            # 「原生后端被 Qt 侧主动关闭 → SIGSEGV」那个雷。
            d.setFileMode(QFileDialog.FileMode.Directory)
            d.setOption(QFileDialog.Option.ShowDirsOnly)
            if start:
                d.setDirectory(start)
        else:
            d.setOption(QFileDialog.DontUseNativeDialog, True)
            d.setAcceptMode(QFileDialog.AcceptMode.AcceptSave if save
                            else QFileDialog.AcceptMode.AcceptOpen)
            if save:
                d.selectFile(start or "")
            elif start:
                d.setDirectory(start)
        try:
            if d.exec() == QDialog.DialogCode.Accepted:
                files = d.selectedFiles()
                return files[0] if files else ""
        except Exception:
            return ""
        return ""

    def _dir_pick(self, caption, start=""):
        """选一个目录（同样是禁用原生后端的版本）。"""
        return self._file_pick(caption, start=start, dir_only=True)

    def export_preferences(self):
        """导出学到的偏好，换电脑/重装可以带走，不用从零学。

        学习记录是全局的（task_id 恒为 0），导出时一并记下来，导入也归到全局。
        """
        path = self._file_pick("导出偏好", start="mrm_preferences.json",
                               filter_="JSON 文件 (*.json)", save=True)
        if not path:
            return
        try:
            rows = self.db.query(
                "SELECT task_id, label, weight, seen FROM preferences")
            data = {"version": 1,
                    "exported_at": datetime.now().isoformat(timespec="seconds"),
                    "preferences": [dict(r) for r in rows]}
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        QMessageBox.information(self, "已导出",
                                f"共 {len(rows)} 条偏好 →\n{path}")

    def import_preferences(self):
        """导入偏好：合并进全局学习记录（task_id 恒为 0），已存在的覆盖权重。"""
        path = self._file_pick("导入偏好", filter_="JSON 文件 (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            items = data.get("preferences") or []
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        n = 0
        for it in items:
            try:
                self.db.execute(
                    "INSERT INTO preferences(task_id, label, weight, seen) "
                    "VALUES (?,?,?,?) "
                    "ON CONFLICT(task_id, label) DO UPDATE SET "
                    "weight=excluded.weight, seen=excluded.seen",
                    (0, str(it["label"]),
                     float(it.get("weight", 0.0)), int(it.get("seen", 0))))
                n += 1
            except Exception:
                continue
        QMessageBox.information(self, "已导入", f"已合并 {n} 条偏好。")

    # --------------------------- #61 完整备份 / #79 外部备份同步 ----
    def export_backup(self):
        """导出配置 + 数据库 + 规则 + 缩略图为一个 zip（换电脑/留档用）。"""
        from PySide6.QtWidgets import QFileDialog
        import zipfile
        path = self._file_pick("导出完整备份", start="mrm_backup.zip",
                               filter_="ZIP 文件 (*.zip)", save=True)
        if not path:
            return
        base = user_data_dir()
        srcs = []
        for s in (base / "config.json", self.cfg.db_path,
                  self.cfg.rules_dir, self._thumb_dir()):
            if s.exists():
                srcs.append(s)
        # R152-A6：数据库必须走 sqlite 在线备份 API 拿**一致快照**。
        # 原来是直接 zip self.cfg.db_path 文件本身 —— WAL 模式下还没并回
        # 主库的事务全在 -wal 里，zip 出去的是旧数据，恢复即丢最近改动。
        import tempfile
        snap_dir = Path(tempfile.mkdtemp(prefix="mrm_bk_"))
        snap = snap_dir / "mrm.db"
        try:
            self.db.backup(snap)
        except Exception as e:
            shutil.rmtree(snap_dir, ignore_errors=True)
            QMessageBox.warning(self, "导出失败", f"数据库快照失败：{e}")
            return
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                for s in srcs:
                    if s == self.cfg.db_path:
                        # 快照按原相对路径入包，旧备份/导入逻辑零改动
                        z.write(snap, str(s.relative_to(base)))
                        continue
                    if s.is_dir():
                        for f in s.rglob("*"):
                            if f.is_file():
                                z.write(f, str(f.relative_to(base)))
                    else:
                        z.write(s, str(s.relative_to(base)))
            QMessageBox.information(
                self, "已导出", f"完整备份 → {path}\n含配置、数据库、规则、缩略图")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
        finally:
            shutil.rmtree(snap_dir, ignore_errors=True)

    def import_backup(self, path: str = ""):
        """从完整备份 zip 恢复（覆盖当前配置/库/规则/缩略图）。

        path 由「完整备份管理」直接传入时跳过文件选择框；菜单入口不传参数，
        仍走原有选择流程。
        """
        from PySide6.QtWidgets import QFileDialog
        import zipfile
        if not path:
            path = self._file_pick("从完整备份恢复", filter_="ZIP 文件 (*.zip)")
        if not path:
            return
        if QMessageBox.question(
                self, "恢复备份",
                "这会覆盖当前配置、数据库、规则、缩略图。\n确定继续？"
                "（建议先「导出完整备份」留一份当前状态）"
        ) != QMessageBox.StandardButton.Yes:
            return
        base = user_data_dir()
        # R152-A6：恢复前**真的停**——暂停队列 + 有界等待运行中的任务收尾
        # + 关库，再覆盖。原来是「只 pause 不关库就 extractall」：旧连接还
        # 开着，-wal 边车还在，覆盖主库后旧 WAL 往新库上 replay = 数据错乱
        # （WAL replay 风险）。引擎每轮自查 is_closed，等待期间不会写已闭的库。
        try:
            self._queue.pause(reason="恢复备份")
        except Exception:
            pass
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                if not getattr(self._queue, "running", None):
                    break
            except Exception:
                break
            time.sleep(0.2)
        try:
            self.db.close()
        except Exception:
            pass
        # 清掉旧连接可能留下的 WAL/SHM 边车（派生数据，留着会被 replay 到新库上）
        for side in ("-wal", "-shm"):
            sp = Path(str(self.cfg.db_path) + side)
            try:
                if sp.exists():
                    sp.unlink()
            except Exception:
                pass
        try:
            with zipfile.ZipFile(path) as z:
                z.extractall(base)
            # 重开 + 完整性自检（恢复完不能让界面停在「无库」状态）
            from ..db import Database as _DB
            self.db = _DB(self.cfg.db_path)
            rows = self.db.query("PRAGMA integrity_check")
            bad = [str(r[0]) for r in (rows or [])]
            if bad and bad[0].lower() != "ok":
                QMessageBox.warning(
                    self, "恢复完成，但库自检异常",
                    "备份已恢复，但完整性自检未通过：\n"
                    + "\n".join(bad[:5])
                    + "\n建议换一份备份重试，或联系支持。")
            else:
                QMessageBox.information(
                    self, "已恢复",
                    "备份已恢复（数据库已重开并通过完整性自检），界面已刷新。")
            for fn in (self.refresh, self.load_gallery, self.refresh_stats):
                try:
                    fn()
                except Exception:
                    pass
            try:
                self._queue.resume()
            except Exception:
                pass
        except Exception as e:
            # 恢复失败也要把库恢复回来，别把界面留在无库状态
            try:
                from ..db import Database as _DB
                self.db = _DB(self.cfg.db_path)
            except Exception:
                pass
            try:
                self._queue.resume()
            except Exception:
                pass
            QMessageBox.warning(self, "恢复失败", str(e))

    def _backup_sync_internal(self, dst: str) -> tuple[bool, str]:
        """#79 把数据库/规则/缩略图复制到外部同步目录。返回 (成功, 信息)。"""
        import shutil
        d = Path(dst)
        try:
            d.mkdir(parents=True, exist_ok=True)
            if self.cfg.db_path.exists():
                # R152-A6：与导出备份同口径——走在线备份 API 拿一致快照，
                # 不直接 copy2 主库文件（WAL 里未并回的事务会漏）。
                import tempfile
                _snap_dir = Path(tempfile.mkdtemp(prefix="mrm_sync_"))
                try:
                    snap = _snap_dir / "mrm.db"
                    self.db.backup(snap)
                    shutil.copy2(snap, d / "mrm.db")
                finally:
                    shutil.rmtree(_snap_dir, ignore_errors=True)
            if self.cfg.rules_dir.exists():
                shutil.copytree(self.cfg.rules_dir, d / "rules",
                                dirs_exist_ok=True)
            if self._thumb_dir().exists():
                shutil.copytree(self._thumb_dir(), d / "thumbs",
                                dirs_exist_ok=True)
            cfgf = self.cfg.db_path.parent / "config.json"
            if cfgf.exists():
                # 需求 19：config.json 里有 API key、代理、偏好设置 ——
                # 同步到网盘/移动盘就是明文外泄，必须加密成 .mrmx。
                from .. import crypt as _crypt
                (d / "config.json.mrmx").write_bytes(_crypt.encrypt_bytes(
                    cfgf.read_bytes(), self._export_passphrase()))
            # 偏好模型（用户学到的喜欢/不喜欢）单独加密一份，便于只同步学习成果
            try:
                from .. import crypt as _crypt
                prefs = self.db.query("SELECT * FROM preferences")
                (d / "preferences.json.mrmx").write_bytes(_crypt.encrypt_bytes(
                    json.dumps([dict(r) for r in prefs],
                               ensure_ascii=False).encode("utf-8"),
                    self._export_passphrase()))
            except Exception as e:
                (d / "preferences_error.txt").write_text(str(e),
                                                        encoding="utf-8")
            return True, str(d)
        except Exception as e:
            return False, str(e)

    def backup_sync_now(self):
        """#79 把数据库/规则/缩略图复制到外部同步目录（可指向网盘/移动盘）。"""
        dst = getattr(self.cfg, "backup_sync_dir", "") or ""
        if not dst:
            QMessageBox.information(
                self, "备份同步",
                "尚未设置同步目录。请先在「设置 → 设置备份同步目录」中选择目录"
                "（可以指向 OneDrive / 群晖 / 移动硬盘）。")
            return
        ok, msg = self._backup_sync_internal(dst)
        if ok:
            self.log_signal.emit(f"备份同步完成 → {dst}")
            QMessageBox.information(self, "备份同步", f"已同步到 {dst}")
        else:
            QMessageBox.warning(self, "同步失败", msg)

    def edit_backup_sync_dir(self):
        from PySide6.QtWidgets import QFileDialog
        cur = getattr(self.cfg, "backup_sync_dir", "") or ""
        d = self._dir_pick("选择备份同步目录", start=cur)
        if not d:
            return
        self.cfg.backup_sync_dir = d
        save_config(self.cfg)
        self.log_signal.emit(f"备份同步目录已设为 {d}")

    def open_data_dir(self):
        """打开「软件目录/data」——任务、图库、模型、日志都在这一层往下。

        r39e：以前这里是 `QFileDialog.getExistingDirectory(...)` 然后**把
        返回值整个丢掉** —— 弹了个目录选择器，用户挑完什么都不会发生，
        动作等于白点。现在改成：让用户挑一个目录，挑中就在资源管理器里
        打开它（挑 / 取消都不动数据）。
        """
        from ..config import user_data_dir
        d = self._dir_pick("数据目录（软件目录/data）", start=str(user_data_dir()))
        if not d:
            return
        try:
            os.startfile(d)                     # Windows Explorer
        except Exception:
            try:
                subprocess.Popen(["explorer", "/select,", d])
            except Exception as e:
                QMessageBox.warning(self, "打不开", str(e))

    def show_data_location(self):
        from PySide6.QtWidgets import QMessageBox
        from ..config import user_data_dir, _legacy_user_data_dir, migrate_from_legacy
        new = user_data_dir()
        old = _legacy_user_data_dir()
        moved = migrate_from_legacy()      # 幂等：已搬过就返回空列表
        if moved:
            QMessageBox.information(
                self, "数据已搬到软件目录",
                "检测到旧版本数据目录，原任务、图片与模型已迁移至软件目录：\n\n"
                f"新位置：{new}\n"
                f"旧位置：{old}（保留未删，可随时搬回来）\n\n"
                f"已迁移：{', '.join(moved)}")
            self.refresh()
            return
        QMessageBox.information(
            self, "数据存放位置",
            "现在所有数据都放在「软件自己的目录」里，\n"
            "把整个软件文件夹拷走/拷回来就能带着任务、图片、模型继续用。\n\n"
            f"数据目录：{new}\n"
            f"里面：mrm.db（任务和图库记录） / rules（站点规则） /\n"
            f"models（本地 AI 模型） / quarantine（隔离区） /\n"
            f"review（待审核） / thumbs（缩略图） / logs（日志）\n\n"
            "旧的 %APPDATA\\... 目录还保留着，没删任何东西。")

    def edit_recycle_days(self):
        from PySide6.QtWidgets import QInputDialog
        v, ok = QInputDialog.getInt(
            self, "回收站保留天数", "超过这些天的隔离图可被「清理过期回收站」删除：",
            int(getattr(self.cfg, "recycle_keep_days", 30)), 0, 3650)
        if not ok:
            return
        self.cfg.recycle_keep_days = v
        save_config(self.cfg)
        # 同步更新菜单 tooltip
        if getattr(self, "act_prune", None) is not None:
            self.act_prune.setToolTip(
                f"物理删除回收站里超过 {v} 天、且无收藏/未拉黑/未待审核的图")

    def edit_thumbnail_size(self):
        from PySide6.QtWidgets import QInputDialog
        v, ok = QInputDialog.getInt(
            self, "缩略图尺寸", "缓存缩略图边长（像素，越大越清晰越占缓存）：",
            int(getattr(self.cfg, "thumbnail_size", 256)), 64, 1024, 32)
        if not ok:
            return
        self.cfg.thumbnail_size = v
        save_config(self.cfg)
        self.load_gallery()

    def show_shortcuts(self):
        # F19：审核页 / 灯箱 / 任务三段按**当前生效键位**现场拼（默认键位时
        # 与改前的固定文本一致）；列表模式表格与通用段不参与自定义，保持静态。
        # ⚠ 下面各段的标题字面量被既有测试（test_r153 / test_r26）当锚点用，
        # 改段标题时连那些测试一起看。
        from ..hotkeys import (display as _d, resolve as _hk,
                               SHELL_FIXED_NOTES, SHELL_MODE_NOTE)
        _h = getattr(self.cfg, "hotkeys", None) or {}

        def _k(aid):
            return _d(_hk(_h, aid))

        QMessageBox.information(self, "快捷键", (
            "审核页（焦点在缩略图列表上）：\n"
            f"  {_k('review.up')} / {_k('review.down')} = 上/下移动选中（不判定，会环绕）\n"
            f"  {_k('review.like')} = 喜欢（通过审核，搬回图库）\n"
            f"  {_k('review.dislike')} = 不喜欢（搬进隔离区，并自动学习主题词）\n"
            f"  {_k('review.reason')} = 为什么不喜欢（键盘填原因：勾主题词只调低权重、\n"
            "      填的排除词才写进任务排除词，下次运行自动跳过）\n"
            f"  {_k('review.skip')} = 待定/跳过（保留，不收藏不学习）\n"
            "  Ctrl+Z = 撤销上一步审核（固定，不在此表）\n"
            "  （Windows 能区分左右 Ctrl：只有右 Ctrl 才算待定；\n"
            "    Linux / ARM 上左右 Ctrl 都算）\n"
            "  审核进度断点续审：关掉软件下次回来，光标自动落到上次审完的\n"
            "  该图之后继续，无需从头翻页\n\n"
            "灯箱（图库 / 审核页 双击缩略图都能打开）：\n"
            f"  {_k('rlight.prev')} / ↑ = 上一张（翻页，不判定）\n"
            f"  {_k('rlight.like')} = 喜欢（判完自动下一张）\n"
            f"  {_k('rlight.next')} = 下一张\n"
            f"  {_k('glight.prev')} / {_k('glight.next')} = 图库灯箱里 上一张 / 下一张\n"
            "  （灯箱只保留「喜欢」；判「不喜欢」请用审核页列表/网格，\n"
            "    键盘 ← 在列表上仍是不喜欢）\n"
            f"  {_k('light.slideshow')} / P = 幻灯片自动播放 开/停（P=暂停/继续）\n"
            f"  {_k('light.zoom_in')} / {_k('light.zoom_out')} / "
            f"{_k('light.zoom_reset')} = 放大 / 缩小 / 缩放复位\n"
            f"  {_k('light.fullscreen')} = 全屏 / 退出全屏\n"
            f"  {_k('light.rotate')} = 旋转（只改显示，不写原文件）\n"
            "  Esc = 全屏时先退出全屏，再按才关闭\n"
            "  ↑↓ = 灯箱固定别名，始终翻页（不参与自定义）\n\n"
            "任务（焦点在主窗口时）：\n"
            f"  {_k('task.start')} / {_k('task.stop')} = 开始 / 停止选中的任务\n\n"
            "三种【列表模式】表格（图库 / 审核 / 隔离区）：\n"
            "  →          喜欢    审核=通过入库，图库=批量喜欢\n"
            "  ←          不喜欢  审核=移入隔离，图库=批量不喜欢\n"
            "  隔离区的表格里 → = 还原回图库，← = 删除\n\n"
            "通用：\n"
            "  双击缩略图   图库 / 审核页 = 灯箱（实时墙=默认看图软件）\n"
            "  双击标题栏   最大化 / 还原\n"
            "  双击托盘图标 显示主窗口\n"
            "  F1          打开本说明\n\n"
            "改键：设置 → 快捷键…（审核页 / 灯箱 / 任务启停可自定义；\n"
            "↑↓、P、Esc 为固定别名，不参与自定义）\n\n"
            + "内置窗口固定键位：\n"
            + SHELL_MODE_NOTE + "\n"
            + "\n".join(f"  {x['key']}    {x['label']}"
                        for x in SHELL_FIXED_NOTES)))
        self._shortcuts_shown = True
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass

    # ---------------- 需求 19：导出一律加密 ----------------
    def _export_passphrase(self, ask: bool = True) -> str:
        """导出口令。默认用内置口令（用户定的「内置 salt」方案），
        这里保留 ask=True 的入口：将来想让用户自己设口令时接上即可。"""
        return str(getattr(self.cfg, "export_passphrase", "") or "")

    def _write_encrypted(self, data: bytes, base_name: str,
                         what: str) -> Path:
        """把导出内容写成 .mrmx 加密文件，返回落盘路径。"""
        from .. import crypt
        d = self.cfg.log_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{base_name}.mrmx"
        try:
            blob = crypt.encrypt_bytes(data, self._export_passphrase())
        except Exception as e:
            QMessageBox.warning(self, "导出失败",
                                f"{what} 加密出错：{type(e).__name__}: {e}")
            return p
        p.write_bytes(blob)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        return p

    def export_report(self):
        """统计报告导出。需求 19：报告含图库构成等敏感信息 → 加密成 .mrmx。"""
        import json
        from ..stats import human_bytes
        s = library_stats(self.db, self.cfg.library_dir)
        s["bytes_human"] = human_bytes(s["bytes_total"])
        blob = json.dumps(s, ensure_ascii=False, indent=2).encode("utf-8")
        p = self._write_encrypted(blob, "mrm-report", "统计报告")
        QMessageBox.information(self, "已导出（已加密）", str(p))

    def export_gallery_index(self):
        """导出图库索引（图片→标签映射）。需求 19 明确点名要加密这一项。"""
        import json
        rows = self.db.query(
            "SELECT id, path, tags, score, phash, source_url FROM images "
            "ORDER BY id")
        data = {
            "version": 1,
            "items": [dict(r) for r in rows],
        }
        blob = json.dumps(data, ensure_ascii=False).encode("utf-8")
        p = self._write_encrypted(blob, "mrm-gallery-index", "图库索引")
        QMessageBox.information(
            self, "已导出（已加密）",
            f"{str(p)}\n\n共 {len(rows)} 条「图片→标签」映射。"
            "\n（图片文件本身不加密，只有这张映射表加密）")

    # ---- V0.0.17 · 界面重排与桌面补齐：统一入口 / 备份 / 本机信息 / 扫码 ----
    def show_settings_center(self):
        """按八个分区集中管理设置；原有独立对话框由本窗口注入。"""
        from .settings_center import SettingsCenterDialog

        acts = {
            "ai_model": self.ai_model_dialog,
            "web": lambda: self.toggle_web(True),
            "hotkeys": self.open_hotkey_dialog,
            "rate_plan": self.open_rate_plan_dialog,
            "db_encryption": self.db_encryption_dialog,
            "onboarding": self.open_onboarding_wizard,
            "skins_dir": self._open_skins_dir,
            "proxy": self.proxy_dialog,
            "cookie": self.show_cookie_dialog,
            "subs": self.show_subs_dialog,
            "diskspace": self.show_disk_space,
            "backup": self.show_fullbackup_manager,
            "full_cleanup": self.full_cleanup,
            "diagnostic": self.export_diagnostic_bundle,
            "shortcuts_help": self.show_shortcuts,
            "sync_menu_states": self.sync_settings_menu_states,
            "pick_dir": lambda: self._dir_pick("选择目录", ""),
            "pick_file": lambda: self._file_pick("选择文件", ""),
        }
        try:
            SettingsCenterDialog(self.db, self.cfg, acts, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "设置中心",
                                f"无法打开设置中心：{e}")

    def _adv_body_toggle(self, on: bool):
        """高级选项折叠开合（箭头方向跟着走）。"""
        try:
            self._adv_body.setVisible(bool(on))
            self._adv_toggle.setArrowType(
                Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        except Exception:
            pass

    # -------------------------------------------------- V0.0.19 界面双模式 --
    def showEvent(self, e):
        super().showEvent(e)
        # V0.0.19：毛玻璃需要有效窗口句柄，首次 show 后延迟一帧再应用。
        if not getattr(self, "_glass_applied", False):
            self._glass_applied = True
            QTimer.singleShot(120, self._apply_glass_on_start)

    def set_ui_mode(self, mode: str):
        """切换新手/老鸟界面模式并持久化；即时生效，无需重启。"""
        mode = "newbie" if str(mode) == "newbie" else "pro"
        self.cfg.ui_mode = mode
        try:
            save_config(self.cfg)
        except Exception as e:
            QMessageBox.warning(self, "界面模式",
                                f"模式已切换，但保存设置失败：{e}")
        self._apply_ui_mode_menus()
        self._refresh_uimode_checks()
        try:
            self._refresh_task_form_mode()
        except Exception:
            pass
        # V0.0.19：首页看板同步收起/展开明细区，双模式一处切换全局生效。
        board = getattr(self, "_board_tab", None)
        if board is not None and hasattr(board, "apply_ui_mode"):
            try:
                board.apply_ui_mode(mode)
            except Exception:
                pass

    def _ui_is_newbie(self) -> bool:
        return str(getattr(self.cfg, "ui_mode", "pro") or "pro") == "newbie"

    def _apply_ui_mode_menus(self):
        """新手模式隐藏「工具」「视图」两个顶级菜单。"""
        for attr in ("_m_tools", "_m_view"):
            m = getattr(self, attr, None)
            if m is None:
                continue
            act = m.menuAction()
            try:
                act.setVisible(not self._ui_is_newbie())
            except Exception:
                pass

    def _refresh_uimode_checks(self):
        cur = "newbie" if self._ui_is_newbie() else "pro"
        for k, a in (getattr(self, "_uimode_actions", None) or {}).items():
            try:
                a.blockSignals(True)
                a.setChecked(k == cur)
                a.blockSignals(False)
            except Exception:
                pass

    def _refresh_task_form_mode(self):
        """按界面模式收起/保持任务表单高级选项区（控件都在，只调显隐）。"""
        tog = getattr(self, "_adv_toggle", None)
        body = getattr(self, "_adv_body", None)
        if tog is None or body is None:
            return
        try:
            if self._ui_is_newbie():
                body.setVisible(False)
                tog.setChecked(False)
                tog.setVisible(False)
            else:
                tog.setVisible(True)
        except Exception:
            pass

    # --------------------------------------------------- V0.0.19 毛玻璃 --
    def toggle_glass(self, checked: bool = None):
        """开/关主窗口毛玻璃背板并持久化；不支持就明说并保持关闭。"""
        from . import acrylic
        on = (self.act_glass.isChecked() if checked is None
              else bool(checked))
        if on:
            wh = self.windowHandle()
            res = acrylic.apply_glass(int(wh.winId()) if wh else 0)
            if not res.get("ok"):
                QMessageBox.information(
                    self, "毛玻璃效果",
                    res.get("error") or "当前系统不支持毛玻璃效果，"
                    "已保持常规外观。")
                try:
                    self.act_glass.blockSignals(True)
                    self.act_glass.setChecked(False)
                    self.act_glass.blockSignals(False)
                except Exception:
                    pass
                on = False
        else:
            try:
                wh = self.windowHandle()
                if wh is not None:
                    acrylic.remove_glass(int(wh.winId()))
            except Exception:
                pass
        self.cfg.glass_enabled = bool(on)
        self._set_glass_look(bool(on))
        try:
            save_config(self.cfg)
        except Exception:
            pass

    def _set_glass_look(self, on: bool):
        """现代化观感与系统背板同开同关：背板没生效就不改控件外观。"""
        try:
            from . import modern
            modern.apply_look(self, bool(on))
        except Exception:
            pass

    def _apply_glass_on_start(self):
        if bool(getattr(self.cfg, "glass_enabled", False)):
            try:
                from . import acrylic
                wh = self.windowHandle()
                if wh is not None:
                    res = acrylic.apply_glass(int(wh.winId()))
                    if res.get("ok"):
                        self._set_glass_look(True)
            except Exception:
                pass

    def sync_settings_menu_states(self):
        """设置中心改完内联项后，把菜单里同一批开关的勾选态对齐。

        一律 blockSignals，避免 setChecked 反向触发 toggled 又弹配置框或改值。
        """
        pairs = (
            ("act_tray_start", "tray_start_minimized", False),
            ("act_goto_gallery", "task_done_goto_gallery", False),
            ("act_convert_jpg", "convert_to_jpg", False),
            ("act_keyask", "review_key_ask", False),
            ("act_sanitize", "log_sanitize", True),
            ("act_blur", "blur_sensitive", False),
            ("act_recycle", "use_recycle", True),
            ("act_backup", "daily_backup", True),
            ("act_watch_dir", "watch_enabled", False),
            ("act_ai_gpu", "ai_gpu", True),
            ("_act_learn", "learn_enabled", True),
            ("act_web", "web_enabled", False),
        )
        for attr, key, default in pairs:
            act = getattr(self, attr, None)
            if act is None:
                continue
            try:
                blk = act.blockSignals(True)
                act.setChecked(bool(getattr(self.cfg, key, default)))
                act.blockSignals(blk)
            except Exception:
                pass
        try:
            from ..config import ai_device_effective
            cur = ai_device_effective(self.cfg)
            for k, act in (getattr(self, "_dev_actions", {}) or {}).items():
                blk = act.blockSignals(True)
                act.setChecked(k == cur)
                act.blockSignals(blk)
        except Exception:
            pass

    def show_fullbackup_manager(self):
        """完整备份管理：列举、上传、另存与自动清理（网页端对称）。"""
        from .fullbackup_dialog import FullBackupDialog

        try:
            FullBackupDialog(self.db, self.cfg, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "完整备份管理",
                                f"无法打开完整备份管理：{e}")

    def restore_full_backup(self, path: str):
        """供完整备份管理调用：跳过文件选择框，直接恢复指定备份包。"""
        try:
            self.import_backup(str(path))
        except Exception as e:
            QMessageBox.warning(self, "恢复备份",
                                f"无法恢复所选备份包：{e}")

    def show_sysinfo(self):
        """本机配置整合看板（网页端同一套采集口径）。"""
        from .sysinfo_dialog import SysInfoDialog

        try:
            SysInfoDialog(self.cfg, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "本机配置",
                                f"无法打开本机配置：{e}")

    def show_search_all(self):
        """全局搜索：一个关键词同时搜任务 / 图库 / 操作日志。

        V0.0.18 与网页顶栏搜索框对称，两端共用 ``searchall.search_all``。
        """
        from .searchall_dialog import SearchAllDialog

        try:
            SearchAllDialog(self.db, self, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "全局搜索",
                                f"无法打开全局搜索：{e}")

    def show_site_stats(self):
        """站点采集统计看板（成功率/速率/失败 Top/Cookie 提醒/导出历史）。

        V0.0.18；口径与网页报表页共用 ``site_stats`` 纯函数。
        """
        from .site_stats_dialog import SiteStatsDialog

        try:
            SiteStatsDialog(self.db, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "站点采集统计",
                                f"无法打开站点采集统计：{e}")

    def show_notifications(self):
        """通知中心（与网页端铃铛共用 notifications 表，口径只在 notify.py）。"""
        from .notify_dialog import NotifyDialog

        try:
            NotifyDialog(self.db, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "通知中心",
                                f"无法打开通知中心：{e}")

    def show_qrlogin(self):
        """生成供手机扫码登录的二维码（与网页端共用令牌表）。"""
        from .qrlogin_dialog import QRLoginDialog

        try:
            QRLoginDialog(self.cfg, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "手机扫码登录",
                                f"无法打开手机扫码登录：{e}")

    def _delete_selected_template(self):
        """删除任务表单里当前选中的模板；真任务一律不受影响。"""
        combo = getattr(self, "f_tpl", None)
        if combo is None or not self._alive(combo):
            return
        tid = combo.currentData()
        if not tid:
            QMessageBox.information(
                self, "删除模板", "尚未选择模板，请先从下拉列表中选取。")
            return
        name = combo.currentText()
        if QMessageBox.question(
                self, "删除模板",
                f"确定删除模板「{name}」？已有任务不受影响。"
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            from .. import taskio
            ok = taskio.delete_template(self.db, int(tid))
        except Exception as e:
            QMessageBox.warning(self, "删除模板", f"删除模板失败：{e}")
            return
        self._reload_templates()
        if not ok:
            QMessageBox.information(self, "删除模板",
                                    "所选条目不是模板，未作改动。")

    def _task_col_settings_obj(self):
        from PySide6.QtCore import QSettings
        return QSettings("SweetMRM", "MainWindow")

    def _restore_task_table_columns(self):
        """恢复上次的任务表列宽，并挂上持久化与双击复位。"""
        tt = getattr(self, "task_table", None)
        if tt is None:
            return
        try:
            st = self._task_col_settings_obj()
            saved = str(st.value("task_table/col_widths", "") or "")
            if saved:
                widths = [int(x) for x in saved.split(",")
                          if x.strip().isdigit()]
                for i, w in enumerate(widths[:tt.model().columnCount()]):
                    if w > 0:
                        tt.setColumnWidth(i, w)
            hh = tt.horizontalHeader()
            hh.sectionResized.connect(self._on_task_col_resized)
            hh.sectionMoved.connect(
                lambda *_a: self._persist_task_table_columns())
            hh.sectionDoubleClicked.connect(self._reset_one_task_column)
            hh.setToolTip("拖动列边界可调整宽度；双击列边界复位该列宽度")
        except Exception:
            pass

    def _on_task_col_resized(self, *_a):
        self._persist_task_table_columns()

    def _persist_task_table_columns(self):
        tt = getattr(self, "task_table", None)
        if tt is None:
            return
        try:
            st = self._task_col_settings_obj()
            widths = [str(tt.columnWidth(i))
                      for i in range(tt.model().columnCount())]
            st.setValue("task_table/col_widths", ",".join(widths))
        except Exception:
            pass

    def _reset_one_task_column(self, idx):
        """双击表头复位单列宽度，取任务表列定义里的默认宽度。"""
        tt = getattr(self, "task_table", None)
        if tt is None:
            return
        try:
            from .task_table import COLUMNS
            if 0 <= idx < len(COLUMNS):
                tt.setColumnWidth(idx, int(COLUMNS[idx][2]))
        except Exception:
            pass

    # ---- V0.0.14 · 桌面补齐订阅 / Cookie 面板，以及素材交付链路 ----
    def show_subs_dialog(self):
        """站点订阅管理（网页端 Subs.vue 的桌面对称）。"""
        from .subs_dialog import SubsDialog

        try:
            dlg = SubsDialog(self.db, self.cfg, self)
            # 订阅「比对并创建任务」要能自动开始，就注入主窗口的调度队列。
            if hasattr(dlg, "set_queue"):
                dlg.set_queue(getattr(self, "queue", None))
            dlg.exec()
        except Exception as e:
            QMessageBox.warning(self, "站点订阅管理",
                                f"无法打开站点订阅管理：{e}")
            return
        # 检查订阅可能建了新任务，关窗后刷新一次任务列表（方法名不同版本
        # 有差异，取不到就静默跳过，不影响订阅本身）。
        try:
            self.refresh_tasks()
        except Exception:
            pass

    def show_cookie_dialog(self):
        """站点 Cookie 管理（网页端 CookieMgr.vue 的桌面对称）。"""
        from .cookie_dialog import CookieDialog

        try:
            CookieDialog(self.cfg, self).exec()
        except Exception as e:
            QMessageBox.warning(self, "站点 Cookie 管理",
                                f"无法打开站点 Cookie 管理：{e}")

    def show_threshold_tune(self):
        """按已收藏 / 已判退图片的评分分布，反推一条合格线。"""
        from .. import threshold_tune as tt
        from ..config import save_config

        try:
            cur = float(getattr(self.cfg, "ai_threshold", 0.35) or 0.35)
            r = tt.suggest(self.db, cur)
        except Exception as e:
            QMessageBox.warning(self, "合格线自动校准",
                                f"计算合格线失败：{e}")
            return
        if not r.get("ok"):
            QMessageBox.information(
                self, "合格线自动校准",
                r.get("note") or "暂无可用建议，请先继续判定图片。")
            return
        ok = QMessageBox.question(
            self, "合格线自动校准",
            str(r.get("note") or "") +
            "\n\n是否将该数值设为全局合格线？任务中单独填写过合格线的，"
            "仍以任务设置为准。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ok != QMessageBox.StandardButton.Yes:
            return
        old = getattr(self.cfg, "ai_threshold", 0.35)
        self.cfg.ai_threshold = r["suggested"]
        try:
            save_config(self.cfg)
        except Exception as e:
            self.cfg.ai_threshold = old
            QMessageBox.warning(self, "合格线自动校准",
                                f"保存合格线失败：{e}")
            return
        QMessageBox.information(self, "合格线自动校准",
                                f"全局合格线已设为 {r['suggested']}。")

    def export_training_kit(self):
        """把合格图复制成一份带同名标注的素材集，供 AI 学习后生成更多图片。"""
        from PySide6.QtWidgets import QDialog

        from .. import export_kit as ek
        from .training_kit_dialog import TrainingKitDialog

        dlg = TrainingKitDialog(self.cfg, self, db=self.db)
        if dlg.exec() != QDialog.Accepted:
            return
        # ⚠ 一律走 _dir_pick（禁用原生后端的版本）。直接调静态
        # QFileDialog.getExistingDirectory 会在对话框被关闭时触发崩溃，
        # test_r39e / test_r60c 两条护栏专门钉这条。
        dest = self._dir_pick("选择训练素材集导出目录")
        if not dest:
            return
        opts = dlg.options()
        picked = dlg.selected_ids()
        if picked:
            opts["ids"] = picked
        try:
            res = ek.export_training_kit(self.db, self.cfg, dest, opts)
        except Exception as e:
            QMessageBox.warning(self, "导出训练素材集", f"导出失败：{e}")
            return
        if not res.get("ok"):
            QMessageBox.information(
                self, "导出训练素材集",
                res.get("error") or "导出未完成，请调整筛选条件后重试。")
            return
        QMessageBox.information(self, "已导出",
                                res.get("note") or "训练素材集已导出。")

    def batch_export_tasks(self):
        """C11 批量导出任务：勾选要导的任务 → 导出 JSON（含进度统计）。

        和「批量导入任务…」对称。导出内容 = 每条任务的全部配置 + 一个
        ``progress`` 段（已下载 N / 已丢弃 N / 待审核 N / 状态）。
        进度**只作展示**，导入时不还原 —— 软件没有"从第 N 张续抓"的机制，
        假装能还原比不给更糟（这一点在 taskio.export_tasks 里有详细说明）。

        弹窗里除了让用户勾，还直接把每条任务的进度写在勾选项后面 ——
        省得导出完再去翻数据库。
        """
        from PySide6.QtWidgets import (QDialog, QListWidget, QListWidgetItem,
                                       QDialogButtonBox, QVBoxLayout, QLabel,
                                       QPushButton, QHBoxLayout)
        from ..repo import list_tasks
        from .. import taskio

        tasks = list_tasks(self.db)
        if not tasks:
            QMessageBox.information(
                self, "批量导出任务", "暂无可导出的任务。\n"
                "先在「任务」页新建一个再回来。")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("批量导出任务")
        dlg.resize(620, 480)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "勾选要导出的任务。导出的是一个 JSON 文件，含任务全部配置 + "
            "进度统计（已下载/ 已丢弃 / 状态）；换机器或备份时用。"))
        lst = QListWidget()
        _prog = {}
        for t in tasks:
            p = taskio.task_progress(self.db, t.id)
            _prog[t.id] = p
            label = (f"{t.name or '(未命名)'}"
                     f"｜已下载 {p['downloaded']}"
                     f"｜已丢弃 {p['discarded']}"
                     f"｜待审核 {p['pending_review']}"
                     f"｜{getattr(t.status, 'value', t.status)}")
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, t.id)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)   # 默认全选
            lst.addItem(it)
        v.addWidget(lst, 1)

        r = QHBoxLayout()
        b_all = QPushButton("全选")
        b_none = QPushButton("全不选")
        r.addWidget(b_all)
        r.addWidget(b_none)
        r.addStretch()
        b_exp = QPushButton("导出")
        b_close = QPushButton("关闭")
        r.addWidget(b_exp)
        r.addWidget(b_close)
        v.addLayout(r)

        def _set_all(checked: bool):
            st = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for i in range(lst.count()):
                lst.item(i).setCheckState(st)

        b_all.clicked.connect(lambda: _set_all(True))
        b_none.clicked.connect(lambda: _set_all(False))
        b_close.clicked.connect(dlg.reject)

        def _do_export():
            ids = [lst.item(i).data(Qt.ItemDataRole.UserRole)
                   for i in range(lst.count())
                   if lst.item(i).checkState() == Qt.CheckState.Checked]
            if not ids:
                QMessageBox.warning(self, "批量导出任务", "一条都没勾。\n"
                                    "至少勾一个任务再导出。")
                return
            default = Path(self.cfg.library_dir or ".").parent / (
                f"tasks_export_{datetime.now():%Y%m%d_%H%M%S}.json")
            f = self._file_pick("保存任务导出文件", start=str(default),
                                filter_="JSON (*.json)", save=True)
            if not f:
                return
            try:
                n = taskio.export_tasks(self.db, Path(f), ids=ids,
                                        with_progress=True)
            except Exception as e:
                QMessageBox.warning(self, "导出失败",
                                    f"{type(e).__name__}: {e}"[:300])
                return
            # 读回校验：写完必须确认真落盘了。导出完没文件 = 白忙一场，
            # 而用户往往要到下次换机器才发现。
            try:
                back = json.loads(Path(f).read_text(encoding="utf-8"))
            except Exception as e:
                QMessageBox.warning(self, "导出异常",
                                    f"写入 {f} 后读回失败：{type(e).__name__}: {e}"[:300])
                return
            if not isinstance(back, list) or len(back) != n:
                QMessageBox.warning(
                    self, "导出异常",
                    f"已写入 {f}，但读回校验不通过"
                    f"（期望 {n} 条，实际 "
                    f"{len(back) if isinstance(back, list) else '不是列表'}）。")
                return
            summary = taskio.summarize_progress(back)
            self.log_signal.emit(f"已导出 {n} 个任务（含进度统计）→ {f}")
            dlg.accept()
            QMessageBox.information(
                self, "导出完成",
                f"已导出 {n} 个任务：\n{f}\n\n（含进度统计）\n\n{summary}")

        b_exp.clicked.connect(_do_export)
        dlg.exec()

    def import_tasks(self):
        f = self._file_pick("选择任务导出文件", filter_="JSON (*.json)")
        if not f:
            return
        try:
            from ..taskio import import_tasks_file
            n = import_tasks_file(self.db, Path(f))
            self.log_signal.emit(f"已导入 {n} 个任务")
            self.refresh()
            QMessageBox.information(
                self, "导入完成",
                f"已从导出文件导入 {n} 个任务。\n"
                "网址重复的任务会自动跳过。\n\n"
                "注意：导出文件里的进度统计只作展示，不会还原抓取进度"
                "（软件没有「从第 N 张续抓」的机制）。")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e)[:300])

    # ---------------- 需求 4/5/21/23：日志统计合并页 ----------------
    def _build_log_stats_tab(self) -> QWidget:
        """底部唯一的「日志统计」页：上日志、下统计，中间一条横线隔开。

        需求 4/5：原来这里是「日志」「统计」两个标签页，用户觉得跟右边的
        任务/审核/图库不是一套导航，干脆合成一页 —— 跟上面的标签页保持一致。
        需求 23：页内画一条横线，上半「运行信息」、下半「统计」，一眼分得开。
        需求 21：统计里加「各任务重复率 + 平均 AI 分」分布，图库统计功能取消
        （这个分布就是原来「统计」页存在的意义，现在放到底部任务栏那一层）。
        需求 1/23：右下角 Sweet 图水印，压在文字底下（透明度由 watermark.ALPHA
        管，用户已定死 255 = 不透明）。

        **r40 尾部调整（用户：「底部的统计信息怎么还在？之前不是说去掉，
        保留一部分信息，到已扫描这一行的最底部」）**：页面里那一整块「统计」
        （标题 + stats_text + 近 60 秒速率图 + 统计类按钮）撤出界面，底栏只留
        「运行信息 / 日志」，底部最下面一行是状态栏的「已扫描 N · 丢弃 N ·
        保存 N」。功能与代码全保留（见 4032-4056 那段的注释）。

        **#45（用户：「日志页别摆按钮」）：底部那 5 个按钮也撤了** ——
        清空日志 / 导出日志… / 刷新统计 / 导出报告… / 导入任务… 全部搬进
        顶部菜单「维护 → 日志统计」，页面只剩日志正文。
        """
        w = QWidget()
        w.setObjectName("logstats")
        v = QVBoxLayout(w)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(6)

        # ---- 上半：运行信息 ----
        t1 = QLabel("运行信息")
        _tstyle(t1, "font-weight:700;font-size:13px;color:{title};")
        v.addWidget(t1)
        # 需求 23：横线，把「运行信息」和「统计」两块分开
        hr = QFrame()
        hr.setFrameShape(QFrame.Shape.HLine)
        _tstyle(hr, "color:{border};border:0;max-height:1px;"
                    "background:{border};")
        v.addWidget(hr)

        # r39：控件必须叫 log_view —— 叫 self.log 会遮蔽 log() 方法。
        self.log_view = QTextEdit()
        self.log_view.setObjectName("logview")     # 透明，给水印让位
        self.log_view.setReadOnly(True)
        self.log_view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.log_view.setMinimumHeight(72)
        v.addWidget(self.log_view, 1)

        # ---- 下半：统计（用户拍板「去掉，保留一部分信息，到已扫描那一行」）----
        # 原来这里是一整块「统计」：标题「统计」 + stats_text（图库/隔离/黑名单/
        # 重复率/AI分/任务分布/失败任务一大段） + 近 60 秒下载速率迷你折线 +
        # 一排按钮。现在**整块从界面上撤掉**，底栏只留「运行信息 / 日志」，
        # 最底部是状态栏那一行「已扫描 N · 丢弃 N · 保存 N · 内存 · CPU」。
        #
        # 注意：撤的是**显示**，不是功能 —— 下面这些控件照样 new 出来、照样
        # 挂在 self 上，只是 setHidden(True) 不进布局。refresh_stats /
        # _draw_chart / export_report / import_tasks 方法一个没删，哪天想把
        # 统计挂回菜单（或挂到右键），挂回来就行，不用重写。
        # 顺带省掉一件事：refresh_stats 的节流照跑，但内容进了看不见的
        # QLabel，所以这儿也不再让 log_view 给它们腾地方。
        self.stats_text = QLabel("选择「刷新统计」查看")
        self.stats_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.stats_text.setWordWrap(True)
        self.stats_text.setTextFormat(Qt.TextFormat.RichText)
        self.stats_text.setHidden(True)

        # 实时下载速率迷你折线（近 60 秒）——从旧「统计」页搬过来的，
        # 需求 21 取消独立统计页后，这里是它唯一的落脚点，别一起删掉，
        # 只是不再上屏。
        self._dl_hist: list = []
        self._last_scanned = 0
        self.chart_label = QLabel()
        self.chart_label.setFixedSize(300, 56)
        # P1-4：底色/描边改走 `_tstyle` 模板（原先写死 #f4f5fa/#d8d8e8，
        # 深色模式下是一块刺眼的浅色矩形）。走 _tstyle 才能登记进重渲表，
        # 切皮肤时自动跟着变。
        _tstyle(self.chart_label,
                "border:1px solid {border};border-radius:6px;"
                "background:{surface_alt};")
        self.chart_label.setHidden(True)
        self.chart_caption = QLabel("近 60 秒下载速率（每秒新增图片数）")
        _tstyle(self.chart_caption, "color:{text_dim};font-size:12px;")
        self.chart_caption.setHidden(True)

        # ---- 按钮行：#45 已整体撤出页面 ----
        # 用户要求「日志页别摆按钮」，这 5 个动作（清空日志/导出日志…/刷新统计/
        # 导出报告…/导入任务…）现在都在顶部菜单「维护 → 日志统计」里，槽也接在
        # 那边的 QAction 上（见 _build_ui 里 #45 那段）。这里原来 new 出来的
        # 5 个 QPushButton 连同它们所在的 row 一起删掉 —— 留着就等于按钮还在，
        # 只是 setHidden，纯属死代码。
        #注意：**水印不在这里挂** —— 它现在挂在 self.stack（主内容区）上，
        # 见 _build_ui 里addWidget 之后那一段。挂在这一页的话，切到审核/图库
        # 就跟着这页一起消失了（用户 10-04 明确要求「切到审核和图库，右下角
        # 水印仍然在」）。
        return w

    def _clear_log_view(self):
        """清空界面日志面板（#45 起由菜单「维护 → 日志统计 → 清空日志」调用）。

        单独抽一个方法而不是让菜单直连 self.log_view.clear：菜单在日志页之前
        建（_build_ui 先建菜单、1107 行才建日志页），连接时 self.log_view 还不
        存在。延迟到点击时再取属性，顺序就无所谓了。
        """
        lv = getattr(self, "log_view", None)
        if lv is not None:
            lv.clear()


    # -------------------------------------------------------------- queue --
    def _build_queue(self):
        self._queue = TaskQueue(self.cfg, max_concurrent=int(getattr(self.cfg, 'parallel_tasks', 3)),
                                on_event=self._queue_event_emit)
        # r38b（推翻 r38 前半案）：事件循环回到子线程 `run_forever()`，
        # Qt 侧**不再**每帧 pump —— loop 必须真的处于 running 状态，
        # engine/queue 全线在 `asyncio.get_running_loop()` 上干活：
        #   queue._worker → create_task(self._run_one(...))
        #   engine._to_thread → loop.run_in_executor（下载主力全走这条）
        #   engine.bfs 里 3 处 create_task
        # 「主线程 pump」这种 loop 永不 running 的写法会当场
        #   RuntimeError: no running event loop，真机 soak 实测
        #   16 站全入队却 60s 零下载（比 0x8001010d 还致命：不崩，只是不干活）。
        # 0x8001010d 由 queue.make_loop() 用纯 select.select 的 SelectSelector
        # 解决（IOCP 与 WSAEventSelect 都会走 CoWaitForMultipleHandles，
        # 只有 select.select 不会 —— 见 queue.make_loop 的取证注释）。
        if not os.environ.get("MRM_NO_QUEUE_LOOP"):
            self._queue.start()
            # 2026-10-28（编号 21/22）：站点订阅的定时检查线程。
            # 与 Web/headless 共用同一个全局单例，三处都调也只会有一个线程。
            try:
                from ..subscribe import start_global_watcher
                start_global_watcher(self.cfg, self.cfg.db_path, self._queue)
            except Exception as _ex:                     # noqa: BLE001
                try:
                    from ..logutil import get_logger
                    get_logger().warning("订阅监控启动失败（不影响采集）: %s", _ex)
                except Exception:
                    pass
        # 2026-10-26：后台预热 CLIP 模型会话（153MB ONNX，加载约 6.8s）。
        # 不预热的话第一个任务的启动会把这 6.8s 卡在事件循环里，后面每个
        # 任务虽然走缓存很快，但首次仍然明显。放 daemon 线程里做，不挡启动。
        try:
            import threading as _th
            # 2026-10-27：原来少一个点，异常被吞 ⇒ 预热从未真正执行过
            # （首次推理慢一大截，且日志里一句提示都没有）。
            from ..classify.clip_onnx import get_clip_onnx

            def _warm():
                try:
                    from ..models_lib import resolve_default_model_dir
                    md = Path(getattr(self.cfg, "ai_model_path", "") or "")
                    if not (md / "model_quantized.onnx").exists():
                        md = resolve_default_model_dir(user_data_dir())
                    if not md:
                        return
                    mp = md / "model_quantized.onnx"
                    if not mp.exists():
                        mp = md / "model.onnx"
                    tk = md / "tokenizer.json"
                    if mp.exists() and tk.exists():
                        get_clip_onnx(mp, tk,
                                      threads=int(getattr(self.cfg, "ai_threads", 0) or 0),
                                      gpu=bool(getattr(self.cfg, "ai_gpu", True)))
                except Exception:
                    pass

            _th.Thread(target=_warm, daemon=True,
                       name="mrm-clip-warmup").start()
        except Exception:
            pass
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        # 需求 10：图库自动刷新定时器**不启动**。对象留着（set_gallery_refresh
        # 和 _bg_pump 之类还会引用它），但永远不 start —— 靠 F5 / 菜单手动刷。
        self._gal_timer = QTimer(self)
        self._gal_timer.timeout.connect(self.load_gallery)
        # 定时运行调度器：每 30 秒检查一次到点任务，启动后立刻扫一遍
        self._sched_timer = QTimer(self)
        self._sched_timer.timeout.connect(self._check_schedule)
        self._sched_timer.start(30000)
        self._check_schedule()
        # 需求 18：取消「启动时自动自检」。原来这里挂 3 个 singleShot：
        # 完整性检查 / 崩溃兜底 / 代理自检 —— 每次开软件都自动跑一遍，
        # 既拖慢启动、又会自己弹代理提示框，用户明确要求去掉。
        # 手动入口一个不少，都还在「维护」菜单里（完整性自检 / 重新检测 /
        # 「设置 → 代理」）。要临时跑一次就去维护菜单点。
        #
        # 注意：这也顺带消掉了 r40 扫描里「启动首帧连续 STALL」的诱因之一 ——
        # 启动即跑完整性检查要遍历全库文件，2000+ 行时能把主线程堵住几十秒。
        pass

    def _schedule_list_refresh(self):
        """r39：队列事件很密（一个任务 running/queued 就来一个），每个都触发一次
        全量列表刷新 → 连着把审核页重建几十次。合并到 500ms 最多一次。

        顺带把「列表轻刷新」和「审核页重活」隔开：这里只刷任务行，审核页
        重活由 refresh() 里的可见性判断兜住。
        """
        if getattr(self, "_closing", False):
            return
        if getattr(self, "_list_refresh_pending", False):
            return
        try:
            self._list_refresh_pending = True
            t = getattr(self, "_list_refresh_timer", None)
            if t is None:
                from PySide6.QtCore import QTimer as _QTimer
                t = _QTimer(self)
                t.setSingleShot(True)
                t.setInterval(500)
                # 必须走这个中间槽：**到点要先把 pending 清掉再刷**。
                # 直接接 refresh_list 的话，pending 永远是 True，刷过一次之后
                # 所有队列事件都被吞掉，UI 状态就此不再更新（r39 测试逼出来的真 bug）
                t.timeout.connect(self._flush_list_refresh)
                self._list_refresh_timer = t
            t.start()
        except Exception:
            self._list_refresh_pending = False
            try:
                self.refresh_list()
            except Exception:
                pass

    def _flush_list_refresh(self):
        """节流窗口到点：清标志 + 真刷一次（异常不该判掉整轮刷新）。"""
        self._list_refresh_pending = False
        try:
            self.refresh_list()
        except Exception:
            pass

    def _queue_event_emit(self, kind, task_id=None, payload=None):
        """Bridge the queue's worker-thread callbacks to the GUI thread."""
        self.queue_event_signal.emit(kind, task_id, payload)

    def _show_popup(self, title: str, body: str):
        QMessageBox.information(self, title, body)

    def _on_queue_event(self, kind, task_id=None, payload=None):
        # 关闭中不再处理队列事件（否则 db 已关还会查询 →「closed database」报错）
        if getattr(self, "_closing", False):
            return
        # 状态变化实时反映到左侧列表（stopped 之前漏了 → 行卡在"正在抓取"）
        if kind in ("queued", "running", "finished", "error", "stopped"):
            self._schedule_list_refresh()
        if kind == "running":
            self.log_signal.emit(f"开始任务 #{task_id}")
        elif kind == "finished":
            r = payload or {}
            self.log_signal.emit(
                f"任务 #{task_id} 完成: 下载{r.get('downloaded', 0)} "
                f"去重{r.get('dedup', 0)} 页面{r.get('pages', 0)} "
                f"AI过滤{r.get('ai_filtered', 0)}"
                + (f" 失败{r.get('failed', 0)}" if r.get('failed') else "")
                # F9（R154）：断点续采账 —— 跳过多少已抓页（0 时不占地方）
                + (f" 跳过已抓{r.get('pages_resumed', 0)}页"
                   if r.get('pages_resumed') else ""))
            self._notify(f"任务 #{task_id} 完成",
                         f"下载 {r.get('downloaded', 0)} · 去重 "
                         f"{r.get('dedup', 0)} · AI过滤 "
                         f"{r.get('ai_filtered', 0)}"
                         + (f" · 失败 {r.get('failed', 0)}"
                            if r.get('failed') else ""))
            # 2026-10-26：自动跳图库改为开关（默认关），且仅"真完成"才跳。
            # ⚠ queue 对报错/0图/部分失败也会发 finished（error 照发、finished
            # 也照发，queue.py:643），所以这里查一次任务状态，非 done 不跳。
            # refresh_live（左下角统计，轻）保持 unconditional；重的
            # load_gallery + _nav_go 才受开关门管——否则 22 个任务同完
            # 会重绘图库 N 次（同族卡顿，见任务 #203 附带项）。
            if bool(getattr(self.cfg, "task_done_goto_gallery", False)):
                _jump = True
                try:
                    _row = self.db.query(
                        "SELECT status FROM tasks WHERE id=?", (task_id,))
                    _jump = bool(_row) and _row[0]["status"] == "done"
                except Exception:
                    _jump = True      # 查不到状态时不拦跳，保持老行为
                if _jump:
                    # 跑完自动带用户去看结果（V0.0.9：页序整体后移一位，
                    # 这里改用动态查索引，别再钉字面量）
                    self._nav_go(self.stack.indexOf(self._gallery_tab))
                    self.load_gallery()
            self.refresh_live()
            # r32：任务跑完顺手清一次垃圾（0 字节/.part 孤儿、图库残留记录、
            # 隔离区 _purged）。只清软件自己产生的，**不碰系统回收站**。
            # 延迟 1.5s：先让结果渲染出来，再 quietly 收拾，不跟刷新抢资源。
            if bool(getattr(self.cfg, "tidy_after_task", True)):
                QTimer.singleShot(1500,
                                  lambda: self._tidy_after_task(task_id))
        elif kind == "error":
            self.log_signal.emit(f"任务 #{task_id} 失败: {payload}")
            # R186：不再因 compliance/授权报错自动弹授权窗（入口已删）。
        elif kind == "stopped":
            self.log_signal.emit(f"任务 #{task_id} 已停止")
        elif kind == "retry-urls":
            # F7：按 URL 重试失败网址跑完（payload = engine 返回的摘要）。
            # 有成功下载就去图库把新图刷出来——用户此刻多半正看着这个对话框。
            r = payload or {}
            n_ok = len(r.get("succeeded") or [])
            self.log_signal.emit(
                f"任务 #{task_id} 失败网址重试完成：成功 {n_ok} 条，"
                f"仍失败 {len(r.get('still_failed') or [])} 条"
                + (f"，过滤跳过 {len(r.get('filtered') or [])} 条"
                   if r.get("filtered") else ""))
            if n_ok:
                # 与 _nav_go 切到图库页同款：直接 load_gallery()（图库页
                # 不可见时 reload 也没人看，切过去时 _nav_go 自己会再刷）。
                try:
                    self.load_gallery()
                except Exception:
                    pass
        elif kind == "queued":
            self.log_signal.emit(f"任务 #{task_id} 入队 (优先级{payload})")
        elif kind == "auto_throttle":
            # #66 队列自动降速：失败率过高时自动暂停队列，避免被站点封禁
            self.log_signal.emit(
                f"队列自动降速：{payload}（失败率过高，已暂停队列避免被封）")
            self._notify("已自动降速", str(payload))
        elif kind == "low_disk":
            # #54 磁盘空间预警：队列已自动暂停，明确告诉用户为什么停了、
            # 现在剩多少、去哪里改阈值
            self.log_signal.emit(f"磁盘空间不足，队列已暂停：{payload}")
            self._notify("磁盘空间不足，已暂停采集", str(payload))
            self.popup_signal.emit(
                "磁盘空间不足，已自动暂停",
                f"{payload}\n\n"
                "所有任务已暂停，不会再继续下载（不会把磁盘写满）。\n"
                "清理出空间后选择「暂停 / 继续」即可恢复。\n"
                "阈值可在 设置 → 「磁盘剩余空间预警」里调整。")
            self.refresh_list()

    def _notify(self, title: str, body: str):
        """Tray balloon notification (silently skipped if no tray)."""
        tray = getattr(self, "tray", None)
        if tray is not None:
            try:
                from PySide6.QtWidgets import QSystemTrayIcon
                tray.showMessage(title, body,
                                 QSystemTrayIcon.MessageIcon.Information,
                                 5000)
            except Exception:
                pass

    # V0.0.5：gallery_cluster_dedupe（相似图聚类去重）整个方法已删除 ——
    # 重复图片功能专项取消，不留「以后开工」的口子。

    # ---------------------------------------------------- #53 代理健康检查 ---
    def proxy_health_check(self):
        """逐个测试代理节点连通性，汇总健康状态（网络操作放后台线程）。"""
        proxies = self.pool.list()
        if not proxies:
            QMessageBox.information(
                self, "代理健康检查",
                "当前没有配置任何代理节点。\n可在「设置 → 代理」里添加，"
                "或在任务的「使用代理」选项中指定一个节点。")
            return

        class _Probe(QThread):
            done = Signal(list)

            def run(self_):
                out = []
                for e in self.pool.list():
                    try:
                        ok, lat, err = self.pool.check(e)
                    except Exception as ex:
                        ok, lat, err = False, 0, str(ex)
                    out.append((e.name, ok, lat, err))
                self_.done.emit(out)

        def _show(rows):
            lines = []
            ok_n = 0
            for name, ok, lat, err in rows:
                if ok:
                    ok_n += 1
                    lines.append(f"✓ {name}  {lat} ms")
                else:
                    lines.append(f"✗ {name}  不可用：{err}")
            QMessageBox.information(
                self, "代理健康检查",
                f"共 {len(rows)} 个节点，可用 {ok_n} 个：\n\n"
                + "\n".join(lines))

        w = _Probe()
        w.pool = self.pool
        w.done.connect(_show)
        w.finished.connect(w.deleteLater)
        self._proxy_worker = w
        w.start()

    # ---------------------------------------------------- #59 清理过期回收站 ---
    def prune_recycle(self):
        """清空回收站：物理删除全部隔离图（需求 8 取消保留天数后的唯一出口）。

        原来是「删除超过 keep_days 天的」，现在没有天数概念了：
        隔离区的图永远不会自动消失，要释放磁盘只能由用户主动点这里，
        弹确认框报清数量再删。收藏 / 拉黑 / 待审核的图照旧不动。
        """
        # 取消保留天数后，这个动作从「顺手清过期」变成「一键清空」，
        # 误点代价从「删几张旧的」变成「删全部隔离图」→ 必须先确认并报数量。
        try:
            cnt = self.db.query(
                "SELECT COUNT(*) c FROM images WHERE in_quarantine=1 "
                "AND needs_review=0 AND favorite=0 AND blacklisted=0")[0]["c"]
        except Exception:
            cnt = 0
        if not cnt:
            QMessageBox.information(self, "清空隔离区", "隔离区已经是空的。")
            return
        ans = QMessageBox.question(
            self, "清空隔离区",
            f"将**物理删除** {cnt} 张隔离图对应的磁盘文件，不可恢复。\n\n"
            "（已收藏 / 已拉黑 / 待审核的图不受影响）\n\n"
            "注意：清的是软件内隔离区，与 Windows 系统回收站无关。\n\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        # keep=0 会被 quarantine_prune 理解为「永不清」，所以这里直接
        # 传一个极大值 = 「所有早于创世纪」的记录，即全部。
        n = self.db.quarantine_prune(10 ** 6)
        self.log_signal.emit(f"清空隔离区：删除 {n} 张隔离图")
        if n:
            self._notify("隔离区已清空", f"已删除 {n} 张隔离图，释放磁盘空间")
        else:
            QMessageBox.information(self, "清空隔离区", "隔离区已经是空的。")
        self.load_gallery()

    # ---------------- 图库修复 / 0kb 清理（后台线程，避免卡 UI） ----------------
    def repair_gallery(self):
        """#全隔离根因修复：重新打分被隔离的真图并恢复达标项、删垃圾、清 0kb。"""
        btn = getattr(self, "act_repair", None)
        if btn is not None:
            btn.setEnabled(False)
        self.log_signal.emit("开始修复图库（重新打分被隔离的图、清理垃圾/0kb）…")

        class _Repair(QThread):
            done = Signal(str)

            def __init__(self_, *a, **k):
                super().__init__(*a, **k)
                self_.db = self.db          # noqa: F821 —— 外层 MainWindow
                self_.cfg = self.cfg

            def run(self_):
                # R249：原来这里 `from tools.repair_gallery import main` ——
                # 而 tools/ **不在打包清单里**，安装版点这个菜单必然
                # ModuleNotFoundError。逻辑已搬进包内 maintenance.repair，
                # 与网页端同一份实现（阈值/死链都改成显式选项）。
                try:
                    from .. import maintenance as mt
                    res = mt.repair(self_.db, self_.cfg,
                                    fix_threshold=True,
                                    restore_quarantine=True,
                                    drop_ghosts=True)
                    self_.done.emit(
                        "ok:%d:%d:%d" % (res.get("restored", 0),
                                         res.get("ghosts", 0),
                                         res.get("threshold_changed", 0)))
                except Exception as e:  # noqa
                    self_.done.emit(f"err:{type(e).__name__}: {e}")

        def _on(d):
            if btn is not None:
                btn.setEnabled(True)
            if d.startswith("ok"):
                parts = (d.split(":") + ["0", "0", "0"])[1:4]
                restored, ghosts, thr = (int(x) for x in parts)
                self.log_signal.emit(
                    f"图库修复完成：恢复 {restored} 张、清理死链 {ghosts} 条")
                self.load_gallery()
                self.refresh_live()
                QMessageBox.information(
                    self, "修复完成",
                    f"恢复进图库 {restored} 张\n"
                    f"清理无文件死链 {ghosts} 条\n"
                    f"调整任务门槛 {thr} 个（已降到安全值）\n\n"
                    "0 字节与 .part 残留请用「维护 → 清理 0kb 孤儿文件」。")
            else:
                QMessageBox.warning(self, "修复出错", d)

        w = _Repair()
        w.done.connect(_on)
        w.finished.connect(w.deleteLater)
        self._repair_worker = w
        w.start()

    def clean_orphan_files(self):
        """#85 清理图片目录里的 0 字节图片与 .part 孤儿文件（移到 trash），
        并删除图库里指向『文件已消失 / 0 字节』的残留记录（含关联审核），
        避免留下『缺失 · path』幽灵项。操作前自动备份数据库。"""
        import datetime
        from pathlib import Path as _P
        # R249：同上 —— 这个 import 在安装版里必崩（tools/ 没打包），
        # 而「维护 → 清理 0kb」是常用功能，等于一直是坏的。
        from ..maintenance import move_orphan_files as _move_orphans

        def _clean(dirs, trash):
            return int(_move_orphans(list(dirs), trash).get("moved") or 0)

        # R152-B4：清理目录收窄成白名单（软件自己的三个目录 + 下载类任务的
        # output_dir 子目录）。原来是「每个任务的 output_dir + 每张图的
        # 父目录」原样丢进去 —— 用户相册被设成 output_dir（或从文件夹学习
        # 建过任务）时，0 字节文件会被当垃圾收走 = 越界清用户文件。
        # 详见 tidy.clean_scan_dirs 的注释。
        from ..tidy import clean_scan_dirs
        dirs = clean_scan_dirs(self.cfg, self.db)
        trash = _P(self.cfg.log_dir) / "orphan_trash"

        def _work():
            # 1) 移走磁盘上的 0kb / .part 孤儿文件
            n_file = _clean(dirs, trash)
            # 1.5) 删记录前先自动备份（写这里，别占主线程）
            try:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                self.db.backup(self.cfg.db_path.parent / f"mrm_db_backup_{ts}.db")
            except Exception:
                pass
            # 2) 删除图库里指向『文件已消失 / 0 字节』的残留记录（含关联审核）
            #    R152-B3：与 tidy.py 同口径——去掉 in_quarantine=0 过滤，
            #    隔离区/待审核的幽灵行（文件已不在盘上）也要能清掉；
            #    存活文件有 gone 判据挡着，不会被误删。
            n_db = 0
            rows = self.db.query(
                "SELECT id, path FROM images WHERE path IS NOT NULL AND path<>'' "
                "AND blacklisted=0")
            for r in rows:
                p = _P(r["path"])
                gone = (not p.exists()) or (p.exists() and p.stat().st_size == 0)
                if gone:
                    self.db.execute(
                        "DELETE FROM reviews WHERE image_id=?", (r["id"],))
                    self.db.execute("DELETE FROM images WHERE id=?", (r["id"],))
                    n_db += 1
            return n_file, n_db

        def _show(res):
            n_file, n_db = res
            try:
                self.db.audit("gallery", "clean-orphan",
                              "", f"files={n_file} db_rows={n_db}")
            except Exception:
                pass
            self.log_signal.emit(
                f"清理 0kb/.part 孤儿文件 = {n_file}（已移到 {trash}），"
                f"删除残留图库记录 = {n_db}")
            QMessageBox.information(
                self, "清理完成",
                f"已清理 {n_file} 个 0 字节/.part 残留文件，"
                f"并移除 {n_db} 条指向消失文件的残留记录。")

        self.log_signal.emit("正在清理 0kb/.part 孤儿文件（扫盘，界面不卡）…")
        self._bg_submit(_work, _show, label="清理 0kb 孤儿")

    # --------------------------- #86 补充尺寸（修复『无法显示分辨率』）----
    def gallery_fill_dimensions(self):
        """后台读取每张图真实宽高写进数据库，修复『无法显示分辨率』，
        并让『方向/分辨率』筛选生效（只读文件、不动磁盘）。"""
        n_missing = self.db.query(
            "SELECT COUNT(*) c FROM images WHERE path IS NOT NULL AND path<>'' "
            "AND (width IS NULL OR height IS NULL OR width=0 OR height=0)")[0]["c"]
        if n_missing == 0:
            QMessageBox.information(
                self, "补充尺寸", "所有图片都已有分辨率信息，无需补充。")
            return
        prog = QProgressDialog(
            f"正在读取 {n_missing} 张图的真实尺寸…", "取消", 0, 0, self)
        prog.setWindowTitle("补充尺寸中…")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setValue(0)
        worker = _FillDimWorker(self.cfg.db_path)
        self._fill_dim_worker = worker  # 防 GC

        def on_progress(scanned, updated, current):
            prog.setLabelText(
                f"已扫描 {scanned} 张，补齐 {updated} 张…\n"
                f"{current[-80:] if current else ''}")
            prog.setValue(prog.value() + 1)

        def on_finished(updated):
            try:
                prog.reset()
                prog.close()
            except Exception:
                pass
            QMessageBox.information(
                self, "补充尺寸完成",
                f"已为 {updated} 张图片补齐分辨率信息。\n"
                f"现在图库能显示分辨率，『方向/分辨率』筛选也开始生效。")
            self.load_gallery()

        def on_errored(msg):
            try:
                prog.reset()
                prog.close()
            except Exception:
                pass
            QMessageBox.warning(self, "补充尺寸失败", msg)

        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.errored.connect(on_errored)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    # --------------------------- #38 后台生成全部缩略图 / #73 批量操作 ----
    def generate_thumbs_background(self):
        """#38 后台线程预生成全部缩略图，避免首次浏览时逐个解码卡顿。"""
        mw = self
        rows = self.db.query(
            "SELECT path FROM images WHERE path IS NOT NULL "
            "AND blacklisted=0 AND in_quarantine=0")
        paths = [Path(r["path"]) for r in rows if r.get("path")]
        paths = [p for p in paths if p.exists()]
        if not paths:
            QMessageBox.information(self, "提示", "没有可生成缩略图的图片")
            return
        self.log_signal.emit(f"后台生成缩略图：开始处理 {len(paths)} 张…")

        class _Gen(QThread):
            done = Signal(int, int)

            def run(self_):
                thumbs = mw._thumb_dir()
                sz = int(getattr(mw.cfg, "thumbnail_size", 256))
                n = 0
                for p in self_.paths:
                    try:
                        mw._thumb_pixmap(p, "", sz, thumbs)
                        n += 1
                    except Exception:
                        pass
                self_.done.emit(n, len(self_.paths))

        def _on(n, total):
            self.log_signal.emit(f"后台生成缩略图完成：{n}/{total}")
            self.load_gallery()

        w = _Gen()
        w.paths = paths
        w.done.connect(_on)
        w.finished.connect(w.deleteLater)
        self._gen_thumb_worker = w
        w.start()

    @staticmethod
    def _bump_name(dst: Path) -> Path:
        """同 Downloader._unique_dest：已存在则顺延成 name(1).ext。"""
        if not dst.exists():
            return dst
        stem, dot, ext = dst.name.rpartition(".")
        if not dot:
            stem, ext = dst.name, ""
        for i in range(1, 1000):
            cand = (dst.with_name(f"{stem}({i}).{ext}") if ext
                    else dst.with_name(f"{stem}({i})"))
            if not cand.exists():
                return cand
        return dst

    def batch_export_selected(self):
        """#73 把选中的图复制到指定目录（不动原文件与数据库）。"""
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(self, "提示",
                                    "先选中图片（可 Ctrl / Shift 多选）")
            return
        from PySide6.QtWidgets import QFileDialog
        d = self._dir_pick("导出选中图片到…")
        if not d:
            return
        dst = Path(d)
        rows = _query_images_by_ids(self.db, ids)
        import shutil
        n = 0
        for r in rows:
            src = Path(r["path"])
            if not src.exists():
                continue
            tgt = self._bump_name(dst / src.name)
            try:
                shutil.copy2(src, tgt)
                n += 1
            except Exception:
                pass
        QMessageBox.information(self, "批量导出", f"已导出 {n} 张到 {dst}")

    def batch_rename_selected(self):
        """#73 给选中文件名加前缀，并重命名磁盘文件 + 更新数据库 path。

        V0.0.12：逻辑抽到 rename_ops（网页批量重命名走**同一份**）——
        两端各写一份早晚分叉（红线判断、跳过计数、改名与 UPDATE 的顺序）。
        """
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(self, "提示",
                                    "先选中图片（可 Ctrl / Shift 多选）")
            return
        from PySide6.QtWidgets import QInputDialog
        pref, ok = QInputDialog.getText(
            self, "批量重命名", "给选中的文件名加前缀：", text="")
        if not ok or not pref.strip():
            return
        from ..rename_ops import rename_by_prefix
        r = rename_by_prefix(self.db, self.cfg, ids, pref.strip())
        extra = (f"，跳过原有图库 {r['skipped_external']} 张（文件不改动）"
                 if r["skipped_external"] else "")
        self.log_signal.emit(f"批量重命名 {r['renamed']} 张（前缀「{pref.strip()}」）{extra}")
        self.load_gallery()

    def gallery_rename_by_template(self):
        """#35 按模板批量重命名选中的图片（磁盘文件 + 数据库 path 同步改）。

        支持占位符：{n}=三位序号(按选择顺序) {orig}=原文件名(去扩展名)
        {date}=今天 YYYYMMDD {site}=来源页域名 {task}=所属任务名。
        非法字符自动替换成 _；重名自动 (1)(2) 编号（沿用 _bump_name）。

        V0.0.12：逻辑抽到 rename_ops.rename_by_template，网页端共用同一份
        （红线跳过、序号不占位、占位符校验都只有一处实现）。
        """
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(
                self, "提示", "先选中图片（可 Ctrl / Shift 多选）")
            return
        from PySide6.QtWidgets import QInputDialog
        tpl, ok = QInputDialog.getText(
            self, "按模板重命名",
            "模板（支持 {n} {orig} {date} {site} {task}）：",
            text="{date}_{site}_{n}")
        if not ok or not tpl.strip():
            return
        from ..rename_ops import rename_by_template
        r = rename_by_template(self.db, self.cfg, ids, tpl.strip())
        if r.get("error"):
            QMessageBox.information(self, "按模板重命名", r["error"])
            return
        self.log_signal.emit(
            f"按模板重命名 {r['renamed']} 张（模板「{tpl.strip()}」）"
            + (f"，跳过原有图库 {r['skipped_external']} 张（文件不改动）"
               if r["skipped_external"] else ""))
        self.load_gallery()

    def refresh_live(self):
        """【C7】实时抓图墙已移除 —— 空实现，四处调用点保持原样。

        以前这里每次都跑两条 SQL（COUNT+MAX 签名比对、最新 1 张图），
        由 :meth:`_tick` 每 3 秒触发一次。部件删了以后这些查询纯属白花钱，
        所以整个方法体清空、只留一个 return。**保留方法名**是为了不改
        四个调用点（批量导入后/重命名后/修复图库后/_tick）；
        它们本来也只是"顺手刷新一下"，现在无操作可做，安全。
        """
        return

    def _tick(self):
        if getattr(self, "_closing", False):
            return
        if not self._queue:
            return
        # 任务状态签名兜底：即使队列事件遗漏（或状态被别处直接改写），
        # 状态一变也会重建任务行，行里的"正在抓取…"/忙碌条不会卡死。
        try:
            sig = tuple((r["id"], r["status"]) for r in
                        self.db.query("SELECT id, status FROM tasks "
                                      "WHERE is_template=0 ORDER BY id"))
        except Exception:
            sig = None
        if sig is not None and sig != getattr(self, "_task_sig", None):
            self._task_sig = sig
            self.refresh()
        s = self._queue.snapshot()
        pend = len(self._queue.queued())
        self.lbl_queue.setText(
            f"队列: 运行{len(s['running'])} 待{pend} 失败{len(s['failed'])}"
            + (" [暂停]" if s["paused"] else ""))
        # 2026-10-24：运行中每秒把实时进度（已保存/速度）喂给任务表格。
        # reports 只在跑完才写，不补这一路的话那两列运行中永远空白。
        if s["running"]:
            try:
                live = self._sample_live_progress(s["running"])
                self._refresh_task_table(list_tasks(self.db), live)
            except Exception:
                pass
        # 左下角统计：已扫描=全部记录；丢弃=被AI筛掉/隔离/黑名单；保存=进图库的
        try:
            scanned = self.db.query("SELECT COUNT(*) AS n FROM images")[0]["n"]
            dropped = self.db.query(
                "SELECT COUNT(*) AS n FROM images WHERE path IS NULL "
                "OR in_quarantine=1 OR blacklisted=1")[0]["n"]
            # 内存占用（#40）：让大图库时的内存是否失控一眼可见
            mb = self._mem_mb()
            mem = f" · 内存 {mb:.0f} MB" if mb > 0 else ""
            # CPU 占用（#39）：系统级占用率（GetSystemTimes 差分），一眼看负载
            cpu = self._cpu_percent()
            cpu_s = f" · CPU {cpu:.0f}%" if cpu > 0 else ""
            self.lbl_stats.setText(
                f"已扫描 {scanned} · 丢弃 {dropped} · 保存 {scanned - dropped}"
                f"{mem}{cpu_s}")
            # 实时速率图：记录每秒新增图片数（= 扫描总数增量），近 60 秒
            hist = getattr(self, "_dl_hist", None)
            if hist is not None:
                delta = max(0, scanned - getattr(self, "_last_scanned", 0))
                self._last_scanned = scanned
                hist.append(delta)
                if len(hist) > 60:
                    del hist[:len(hist) - 60]
                self._draw_chart()
        except Exception:
            pass
        # 【C7】原来这里每 3 秒调一次 refresh_live()（内部再查两条 SQL）。
        # 抓图墙部件已删，这个定时调用整段去掉 —— _tick 本来就是 1 秒一跳的
        # 心跳，留着一个必然空转的调用纯属浪费。

    def open_log_folder(self):
        """#15 打开日志文件夹（gui-crash.log / gui-native.log / 运行日志）。"""
        from ..config import user_data_dir
        d = user_data_dir() / "logs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def _startup_crash_check(self):
        """#15 启动告警：上次会话若新增了崩溃/异常记录（gui-crash.log），
        弹窗提示可查看。已看过的内容不重复弹（记录已确认大小到 .crash_seen）；
        offscreen 探针环境只写日志不弹窗，避免模态框挂死自检。"""
        try:
            from ..config import user_data_dir
            logs = user_data_dir() / "logs"
            log = logs / "gui-crash.log"
            if not log.exists() or log.stat().st_size == 0:
                return
            size = log.stat().st_size
            try:
                seen = int((logs / ".crash_seen").read_text(
                    encoding="utf-8").strip() or "0")
            except Exception:
                seen = 0
            if size <= seen:
                return
            if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
                self.log_signal.emit(f"检测到崩溃记录: {log}（offscreen 不弹窗）")
            else:
                ret = QMessageBox.question(
                    self, "检测到异常记录",
                    f"上次运行留有崩溃/异常记录：\n{log}\n\n现在查看吗？",
                    QMessageBox.StandardButton.Yes |
                    QMessageBox.StandardButton.No)
                if ret == QMessageBox.StandardButton.Yes:
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(log)))
            try:
                (logs / ".crash_seen").write_text(str(size), encoding="utf-8")
            except Exception:
                pass
        except Exception:
            pass

    # V0.0.5：open_adapter_checkup（适配器巡检）已彻底删除 ——
    # 决定清单拍板：功能冗余（规则灵不灵抓一张图便知）、且旧版一关窗口
    # 会把整个软件带退出。gui/adapter_checkup.py 模块同批移除。
    # 网页「适配器」页的规则体检不依赖本模块（webui 自带同口径判据）。

    def _startup_proxy_probe(self):
        """#20 启动主动探测代理：配置了代理就后台探一次，结果只写日志区，
        不弹窗不打断。失败时提示去 维护→代理健康检查 复查。"""
        proxy = (getattr(self.cfg, "proxy", None)
                 or os.environ.get("HTTPS_PROXY")
                 or os.environ.get("HTTP_PROXY"))
        if not proxy:
            return

        def work():
            try:
                import httpx
                r = httpx.get("https://api.ip.sb/ip", proxy=proxy, timeout=8,
                              headers={"User-Agent": "Mozilla/5.0"})
                self.log_signal.emit(
                    f"代理 {proxy} 可用，出口 IP {r.text.strip()}")
            except Exception as e:
                self.log_signal.emit(
                    f"代理 {proxy} 探测失败：{type(e).__name__}"
                    "（可到 维护→代理健康检查 复查）")

        threading.Thread(target=work, daemon=True).start()

    def _startup_integrity_check(self):
        """#39 崩溃恢复：启动后后台校验库里记录的文件是否还在、是否完整。

        上次异常退出可能留下半截下载（0 字节）或已删除文件仍被库记录。
        这里只统计 + 提示，绝不自动删除（破坏性动作交给用户点『清理缺失』，
        避免把用户的好图误删）。
        """
        def work():
            try:
                rows = self.db.query(
                    "SELECT id, path FROM images WHERE path IS NOT NULL")
            except Exception:
                return
            missing = empty = 0
            for r in rows:
                p = Path(r["path"] or "")
                try:
                    if not p.exists():
                        missing += 1
                    elif p.stat().st_size == 0:
                        empty += 1
                except Exception:
                    missing += 1
            if missing or empty:
                self.log_signal.emit(
                    f"启动自检: 库里有 {missing} 条记录的文件已不在磁盘、"
                    f"{empty} 个是 0 字节空文件（多半是上次异常退出留下的半截下载）。"
                    "可在图库执行『清理缺失』处理。")
            else:
                self.log_signal.emit("启动自检: 库↔磁盘一致，未发现缺失或空文件")
        threading.Thread(target=work, daemon=True).start()

    def _cpu_percent(self) -> float:
        """系统 CPU 占用率(%)。纯标准库：Windows kernel32.GetSystemTimes 差分，
        Linux 读 /proc/stat 差分。采样间隔至少 2 秒（差分太短会抖动），
        间隔内复用上次读数；首次调用返回 0（无差分基线）；异常返回 0。"""
        try:
            import time as _time
            now = _time.monotonic()
            last_t = getattr(self, "_cpu_last_t", 0.0)
            if now - last_t < 2.0:
                return getattr(self, "_cpu_last_val", 0.0)
            if sys.platform.startswith("win"):
                import ctypes

                class _FT(ctypes.Structure):
                    _fields_ = [("lo", ctypes.c_ulong),
                                ("hi", ctypes.c_ulong)]

                def _u64(ft: "_FT") -> int:
                    return ft.lo | (ft.hi << 32)

                k = ctypes.windll.kernel32
                idle, ker, usr = _FT(), _FT(), _FT()
                if not k.GetSystemTimes(ctypes.byref(idle), ctypes.byref(ker),
                                        ctypes.byref(usr)):
                    return 0.0
                cur = (_u64(idle), _u64(ker) + _u64(usr))
            else:
                with open("/proc/stat", "r", encoding="ascii") as f:
                    parts = f.readline().split()[1:8]
                vals = [int(x) for x in parts]
                cur = (vals[3] + vals[4], sum(vals))  # (idle+iowait, total)
            last = getattr(self, "_cpu_last", None)
            self._cpu_last = cur
            self._cpu_last_t = now
            if not last:
                return 0.0
            d_total = cur[1] - last[1]
            d_idle = cur[0] - last[0]
            if d_total <= 0:
                return 0.0
            val = max(0.0, min(100.0, (d_total - d_idle) / d_total * 100.0))
            self._cpu_last_val = val
            return val
        except Exception:
            return 0.0

    def _mem_mb(self) -> float:
        """当前进程内存占用(MB)。纯标准库：Windows 用 psapi，Linux 读 /proc。

        万张图库时用来确认内存没失控（配合图库分页已把单页限制在 200 张）。
        """
        try:
            if sys.platform.startswith("win"):
                import ctypes
                from ctypes import wintypes

                class _PMC(ctypes.Structure):
                    _fields_ = [
                        ("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t),
                    ]
                pmc = _PMC()
                pmc.cb = ctypes.sizeof(_PMC)
                ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                    ctypes.windll.kernel32.GetCurrentProcess(),
                    ctypes.byref(pmc), pmc.cb)
                if ok:
                    return pmc.WorkingSetSize / (1024.0 * 1024.0)
                return 0.0
            with open("/proc/self/statm") as f:
                return int(f.read().split()[1]) * 4096 / (1024.0 * 1024.0)
        except Exception:
            return 0.0

    def _draw_chart(self):
        """把近 60 秒的每秒下载增量画成迷你折线图（紫罗兰风格，随主题走）。"""
        lbl = getattr(self, "chart_label", None)
        if lbl is None:
            return
        from PySide6.QtGui import QPainter, QPen, QColor
        hist = getattr(self, "_dl_hist", []) or []
        w, h = self.chart_label.width(), self.chart_label.height()
        dark = bool(getattr(self.cfg, "dark_mode", False))
        # P1-4：底色与线色改从调色板取。原先写死 #8b7cf8（=violet 皮肤），
        # 于是**换别的皮肤时这条折线图不变色** —— 自绘不跟随皮肤的那一半。
        pm = QPixmap(w, h)
        pm.fill(QColor(_tc(dark, "surface_alt", "#f4f5fa")))
        p = QPainter(pm)
        pen = QPen(QColor(_tc(dark, "accent", "#8b7cf8")))
        pen.setWidth(2)
        p.setPen(pen)
        if len(hist) >= 2:
            mx = max(hist) or 1
            n = len(hist)
            step = w / max(1, 59)
            for i in range(1, n):
                x0 = (i - 1) * step
                y0 = h - 6 - (hist[i - 1] / mx) * (h - 14)
                x1 = i * step
                y1 = h - 6 - (hist[i] / mx) * (h - 14)
                p.drawLine(int(x0), int(y0), int(x1), int(y1))
        p.end()
        lbl.setPixmap(pm)

    # --------------------------------------------------------------- tasks --
    def _fill_form(self, t):
        """把任务数据填进编辑对话框的表单。"""
        if t.adapter_name:
            self.f_adapter.setCurrentText(t.adapter_name)
        else:
            self.f_adapter.setCurrentIndex(0)
        self.f_entry.setText(" ".join(t.entry_urls))
        self.f_depth.setValue(t.max_depth)
        self.f_conc.setValue(t.concurrency)
        self.f_delay.setValue(t.delay)
        self.f_priority.setValue(t.priority)
        self.f_out.setText(t.output_dir or "")
        self.f_ai_labels.setText(" ".join(t.ai_labels))
        self.f_ai_excl.setText(" ".join(t.ai_blacklist))
        self.f_ai_thr.setValue(t.ai_threshold)
        mi = self.f_ai_mode.findData(t.ai_match_mode or "any")
        self.f_ai_mode.setCurrentIndex(mi if mi >= 0 else 0)
        me = self.f_ai_excl_mode.findData(t.ai_exclude_match_mode or "any")
        self.f_ai_excl_mode.setCurrentIndex(me if me >= 0 else 0)
        idx = self.f_proxy.findData(t.proxy_id)
        self.f_proxy.setCurrentIndex(idx if idx >= 0 else 0)
        self.f_sched.setText(getattr(t, "schedule_at", "") or "")
        self.f_cookie.setText(getattr(t, "cookie", "") or "")
        # R173：任务级 UA 回填（留空即用全局，与新建态一致）
        self.f_ua.setText(getattr(t, "user_agent", "") or "")
        self.f_kwsubdir.setChecked(bool(getattr(t, "keyword_subdir", False)))
        # r29 主题词分级回填（老任务没有这些字段 → 留空，行为不变）
        # 2026-10-28：改成「无限分级」—— 第 1~3 级用固定框，第 4 级起动态行，
        #   每级严格勾选也一并回填（老任务的全局 ai_label_strict 已被
        #   label_levels.stricts_of 展开成「第 2 层起全部严格」）。
        try:
            from ..label_levels import levels_of as _lv, stricts_of as _st
            _levels = _lv(t)
            _stricts = _st(t)
        except Exception:
            _levels = [list(getattr(t, "ai_labels", []) or []),
                       list(getattr(t, "ai_labels_l2", []) or []),
                       list(getattr(t, "ai_labels_l3", []) or [])]
            _one = bool(getattr(t, "ai_label_strict", False))
            _stricts = [False, _one, _one]
        self._clear_deep_levels()
        self.f_ai_l2.setText(" ".join(_levels[1] if len(_levels) > 1 else []))
        self.f_ai_l3.setText(" ".join(_levels[2] if len(_levels) > 2 else []))
        self.f_strict2.setChecked(bool(_stricts[1]) if len(_stricts) > 1 else False)
        self.f_strict3.setChecked(bool(_stricts[2]) if len(_stricts) > 2 else False)
        for _i in range(3, len(_levels)):
            self._add_deep_level(" ".join(_levels[_i]),
                                 bool(_stricts[_i]) if len(_stricts) > _i else False)
        self.f_rate.setValue(int(getattr(t, "rate_limit", 0) or 0))
        self.f_hours.setText(getattr(t, "allowed_hours", "") or "")
        if hasattr(self, "f_auto") and self.f_auto is not None:
            self.f_auto.setChecked(bool(getattr(t, "auto_run", False)))
        # 图片过滤（分辨率 + 文件大小）按任务走：编辑任务用任务自己的值，
        # 新建任务用全局配置当默认值。
        if getattr(t, "id", None) is not None:
            self.f_minw.setValue(int(getattr(t, "min_width", 0) or 0))
            self.f_minh.setValue(int(getattr(t, "min_height", 0) or 0))
            self.f_min_kb.setValue(int(getattr(t, "min_bytes", 0) or 0) // 1024)
            self.f_max_kb.setValue(int(getattr(t, "max_bytes", 0) or 0) // 1024)
        else:
            self.f_minw.setValue(int(getattr(self.cfg, "min_width", 0) or 0))
            self.f_minh.setValue(int(getattr(self.cfg, "min_height", 0) or 0))
            self.f_min_kb.setValue(int(getattr(self.cfg, "min_bytes", 0) or 0) // 1024)
            self.f_max_kb.setValue(int(getattr(self.cfg, "max_bytes", 0) or 0) // 1024)
        # 最大抓取页数按任务走：编辑任务用任务自己的值，新建任务用全局默认值。
        if getattr(t, "id", None) is not None:
            self.f_pages.setValue(int(getattr(t, "max_pages", 0) or 0))
        else:
            self.f_pages.setValue(int(getattr(self.cfg, "max_pages", 0) or 0))

    def _calibrate_threshold(self):
        """用本任务已下载/隔离图片的 AI 分数分布，给出阈值建议（离线、零风险）。

        直接针对「阈值设太高 → 全部判未命中 → 全进隔离区 → 库空」这一坑：
        隔离区里的图同样带着 AI 分数，所以哪怕上次跑出来 0 张入库也能校准。
        """
        from PySide6.QtWidgets import QMessageBox
        tid = getattr(self, "_editing_tid", None)
        if tid is None:
            QMessageBox.information(
                self, "还不能校准",
                "新建任务尚无图片。请先保存并执行一次任务（完成抽图与打分，"
                "哪怕结果全进隔离区），再来这里选择「自动校准」。")
            return
        rows = self.db.query(
            "SELECT score FROM images WHERE task_id=? AND score>0", (tid,))
        scores = sorted(float(r["score"]) for r in rows if r["score"] > 0)
        if len(scores) < 3:
            QMessageBox.information(
                self, "样本不足",
                f"这个任务只有 {len(scores)} 张带分数的图，样本太少没法校准。\n"
                "先跑一次任务，再来校准。")
            return

        def pct(p):
            k = max(0, min(len(scores) - 1,
                           int(round((p / 100.0) * (len(scores) - 1)))))
            return scores[k]
        lo, p10, med, p90, hi = pct(0), pct(10), pct(50), pct(90), pct(100)
        # 建议阈值：保留约 90% 样本 -> 取 10 分位，并夹在 [0.3, 0.6] 安全区间
        # （本机内置 CLIP 对正确匹配最高约 0.5~0.65，≥0.65 必全隔离）
        sugg = max(0.3, min(0.6, round(p10 / 0.05) * 0.05))
        msg = (f"本任务共 {len(scores)} 张带 AI 分数的图。\n\n"
               f"分数分布：最低 {lo:.2f} / 10% {p10:.2f} / 中位 {med:.2f} "
               f"/ 90% {p90:.2f} / 最高 {hi:.2f}\n\n"
               f"建议阈值 ≈ {sugg:.2f}（保留约 90% 样本，不会把全部图判「未命中→隔离」）。\n"
               f"当前阈值 {self.f_ai_thr.value():.2f}。\n\n"
               f"是否应用建议阈值 {sugg:.2f}？")
        box = QMessageBox(self)
        box.setWindowTitle("AI 阈值校准")
        box.setText(msg)
        box.setStandardButtons(QMessageBox.StandardButton.Apply |
                               QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Apply).setText("应用建议")
        if box.exec() == QMessageBox.StandardButton.Apply:
            self.f_ai_thr.setValue(sugg)
            QMessageBox.information(self, "已应用",
                f"阈值已设为 {sugg:.2f}。保存任务后下次运行即按此阈值筛选。")

    def _open_task_form(self, t=None):
        """新建/编辑任务：迅雷式布局下表单放在对话框里。

        r39：面板 + 对话框**复用同一套**（`_ensure_task_form()` 建常驻面板挂在主
        窗口下，对话框只建一次）。以前每次开对话框都 `_build_task_form_panel()`
        新建一套控件：`self.f_*` 被改指向新控件，旧面板没 parent → 立刻被销毁，
        旧控件一销毁，任何还在读 `self.f_entry` / `_reload_adapters` 的地方就
        `RuntimeError: Internal C++ object already deleted`。复用后只剩一套控件，
        也顺带省掉每次开对话框都重建上百个子控件的开销。
        """
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QDialogButtonBox, \
            QMessageBox
        self._ensure_task_form()
        panel = getattr(self, "_form_panel", None)
        if panel is None or not self._alive(panel):
            return
        # R154-F16：模板下拉每次开对话框都重拉（模板可能在别处刚增删）
        self._reload_templates()
        self._editing_tid = t.id if t is not None else None
        title = "新建任务" if t is None else f"编辑任务：{t.name}"
        # ---- r40 修复：对话框空白（用户截屏 编辑任务：ososedki.com 只有「保存/取消」）----
        # 成因：面板被 addWidget 挂到 dlg 后，dlg 一销毁（close/deleteLater）面板就跟着
        # 死；_ensure_task_form 随后重建面板并挂回主窗口，但 _form_dlg 若还“活着”，
        # 旧分支只 setWindowTitle 就 exec() —— 对话框里装的是**已经死的旧面板** → 空白。
        # 修法：复用对话框时，先确认「当前面板真的在 dlg 布局里」，不在就重新装配。
        dlg = getattr(self, "_form_dlg", None)
        reused = False
        if self._alive(dlg):
            _lay = dlg.layout()
            if _lay is not None and (panel.parent() is not dlg
                                     or _lay.indexOf(panel) < 0):
                # 重新挂上成功 → 就复用这个对话框；面板已死返回 False → 走新建分支
                if self._attach_panel_to_dlg(panel, dlg):
                    reused = True
            else:
                reused = True
        if not reused:
            dlg = QDialog(self)
            v = QVBoxLayout(dlg)
            v.setContentsMargins(6, 6, 6, 6)
            v.addWidget(panel, 1)
            try:
                panel.show()          # 面板平时是 hide 的（主窗口里不占地方），装进框就要显示
            except Exception:
                pass
            bb = QDialogButtonBox()
            b_save = bb.addButton("保存", QDialogButtonBox.ButtonRole.AcceptRole)
            b_cancel = bb.addButton(
                "取消", QDialogButtonBox.ButtonRole.RejectRole)
            # V0.0.11：原 R52「全局严格模式放保存键左边」的底部栏随该勾
            # 撤销一起删除——每一级已有独立严格开关，左下角不再有控件。
            v.addWidget(bb)

            def save_and_close():
                try:
                    if not self._alive(self.f_entry):
                        return
                    if not self.f_entry.text().strip():
                        QMessageBox.warning(dlg, "缺少入口", "请填写入口/主题 URL")
                        return
                    self._save_form(self._editing_tid)
                except Exception as e:      # 保存失败别把对话框一起带走
                    self.log_signal.emit(
                        f"保存任务失败: {type(e).__name__}: {e}")
                dlg.accept()

            b_save.clicked.connect(save_and_close)
            b_cancel.clicked.connect(dlg.reject)
            dlg.setWindowTitle(title)
            dlg.resize(860, 620)
            self._form_dlg = dlg
        else:
            dlg.setWindowTitle(title)

        # ---- 填值 / 历史标签 / 新建默认值（复用面板只覆盖文本，不会重造控件）----
        # ⚠ r41 修复：这一段原先被错缩进进 `_attach_panel_to_dlg` 的 `return True`
        # 之后，成了永远执行不到的死代码 —— 真机上对话框压根没 exec()，表现为
        # 「点了新建/编辑任务没反应 / 白框」。这里搬回 `_open_task_form` 本体，
        # 并补上必须执行的 `dlg.exec()`。
        try:
            if t is not None:
                self._fill_form(t)
            self._refresh_hist_chips()     # 曾用词标签每次打开都重建（历史可能刚更新过）
        except Exception:
            pass
        if t is None:
            # ── P1-2 + R47/B9：新建任务要「**全空白**」 ──
            # 旧实现只把输出目录换成了「勾选框 + 提示」，其余字段照旧留着
            # 上一次的值（面板是**复用**的，r39 起 `_open_task_form` 不再重建
            # 控件）→ 用户选择「新建任务」看到的是上次的任务，只是不小心。
            # 现在：先把**所有**字段清成默认值（新建态的默认值，不是 0/空），
            # 再由用户在需要时主动勾「沿用上次目录」。
            self._reset_form_for_new()
            last = str(self.cfg.last_output_dir or "")
            # 勾选框/提示都是建面板时就存在的，这里只更新文字与状态，
            # **绝不 addWidget**（B3：动态往布局里塞控件会越叠越乱）
            lbl = getattr(self, "_lbl_last_out", None)
            if lbl is not None:
                # ⚠ R48/C：上次目录**不再单独显示一行文字**（作业单要求去掉）。
                #   这里仍然 setText 保持对象有值（别处可能读），但**永远保持隐藏**
                #   —— 原来这里 `setVisible(bool(last))` 会把 C 段隐藏掉的标签
                #   又显示出来，等于把「去掉独立显示」这需求退回去了。
                lbl.setText(f"上次用的目录：{last}" if last else "")
                lbl.setVisible(False)
            cb = getattr(self, "_cb_reuse_last", None)
            if cb is not None:
                try:
                    cb.blockSignals(True)
                except Exception:
                    pass
                cb.setChecked(False)
                try:
                    cb.blockSignals(False)
                except Exception:
                    pass
                cb.setVisible(bool(last))
            # 合规模板：严格/标准基线给新建任务预填默认排除词
            #（在 _reset_form_for_new 之后，预填不算「残留」；R186 起
            #  compliance_level 默认 "off"，default_exclusions 返回 []，
            #  这里等于不再预填，仅兼容仍设 loose/standard/strict 的旧配置）
            try:
                _ex = default_exclusions(
                    str(getattr(self.cfg, "compliance_level", "") or "").lower())
                if _ex:
                    self.f_ai_excl.setText(" ".join(_ex))
            except Exception:
                pass
        # 同域名已存 Cookie 自动带入（不覆盖用户已手填的值）
        try:
            if not self.f_cookie.text().strip():
                urls = self.f_entry.text().strip().split()
                if urls:
                    dom = domain_of(urls[0])
                    if dom:
                        stored = get_cookie(dom)
                        if stored:
                            self.f_cookie.setText(stored)
        except Exception:
            pass
        self._form_dlg = dlg
        # 2026-10-26：模态期间停掉 1s 周期刷新。
        # 真机三次崩溃（01:40 / 13:23 / 19:30，pyside6.abi3.dll 同偏移
        # 0xc0000005@0x1c91e）都发生在「弹窗打开 + 后台多任务在跑」时：
        # _tick 检测到任务状态变化就 refresh()，而 refresh 会重建任务卡片、
        # 并 _reload_adapters/_reload_proxies 把**弹窗里正在用的**下拉框
        # clear() 重填 —— Qt 侧对象生命周期被打断 → 原生崩溃。
        _was = bool(getattr(self, "_timer", None) and self._timer.isActive())
        if _was:
            self._timer.stop()
        # 字段都填好了再刷新词表提示，用户看到的是这次真实会用的词
        self._refresh_task_terms_label()
        try:
            dlg.exec()                      # 必须真的把对话框弹出来
        finally:
            if _was:
                self._timer.start(1000)

    def _reset_form_for_new(self):
        """R47/B9：把任务表单重置成「新建态」的默认值。

        为什么必须显式重置：r39 起表单面板是**复用**的（`_ensure_task_form`
        只建一次，之后 `self.f_*` 一直指着同一批控件）。于是「编辑过任务 A
        → 点新建任务」时，屏幕上还是 A 的内容 —— 用户最容易在没注意的情况下
        把 A 的配置存成一个新任务。

        ⚠ 这里写的是**新建态该有的默认值**，不是一律清零：
        `f_ai_thr` 该留 0.35、`f_conc`/`f_depth` 该留默认并发/深度、
        `f_adapter` 该回到第 0 项。一律 `setValue(0)` 会把「阈值 0」当成
        「什么都不要」，那是另一个 bug（更隐蔽、更难发现）。
        """
        from PySide6.QtWidgets import QComboBox, QLineEdit
        # 文本框：清空
        for name in ("f_entry", "f_out", "f_sched", "f_cookie", "f_ua",
                     "f_ai_labels", "f_ai_l2", "f_ai_l3", "f_ai_excl",
                     "f_hours"):
            w = getattr(self, name, None)
            if isinstance(w, QLineEdit):
                w.setText("")
        # 数值：回新建默认值（与 _build_task_form_panel 里设的一致）
        defaults = {
            "f_depth": 1, "f_conc": 2, "f_delay": 0, "f_priority": 0,
            "f_ai_thr": 0.35, "f_rate": 0,
            "f_minw": 0, "f_minh": 0, "f_min_kb": 0,
        }
        for name, val in defaults.items():
            w = getattr(self, name, None)
            if w is None:
                continue
            try:
                w.setValue(val)
            except Exception:
                pass
        # 下拉：回到第 0 项（适配器 / 匹配方式 / 代理）
        for name in ("f_adapter", "f_ai_mode", "f_ai_excl_mode", "f_proxy"):
            w = getattr(self, name, None)
            if isinstance(w, QComboBox):
                try:
                    w.setCurrentIndex(0)
                except Exception:
                    pass
        # 勾选框：全部取消
        for name in ("f_auto", "f_kwsubdir"):
            w = getattr(self, name, None)
            if w is not None and hasattr(w, "setChecked"):
                try:
                    w.setChecked(False)
                except Exception:
                    pass

    def _on_reuse_last_toggled(self, on: bool):
        """R47/B9：「沿用上次目录」勾选 → 回填/清空输出目录。

        勾上才回填上次目录；取消勾立刻清空（用户能明确看到"没勾=不用旧目录"）。
        """
        try:
            last = str(self.cfg.last_output_dir or "")
        except Exception:
            last = ""
        try:
            self.f_out.setText(last if on else "")
        except Exception:
            pass

    def _attach_panel_to_dlg(self, panel, dlg):
        """把面板重新装进已有对话框（面板被销毁重建 / 被 deleteLater 带走时用）。"""
        lay = dlg.layout()
        if lay is None:
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(6, 6, 6, 6)
        try:
            lay.removeWidget(panel)
        except Exception:
            pass
        try:
            panel.setParent(dlg)      # 先回到 dlg 树，再插布局，避免布局持悬空指针
            lay.insertWidget(0, panel)
            panel.show()
        except RuntimeError:          # 面板已被 C++ 侧销毁
            return False
        return True

    def _save_form(self, editing_tid):
        # 新建任务：editing_tid 为 None，必须用齐 TaskConfig 的必填字段
        # （name / adapter_name，没有默认值），否则 TaskConfig(id=None) 会抛
        # TypeError，导致「保存」和「开始任务」两个按钮都崩溃。
        t = (get_task(self.db, editing_tid)
             if editing_tid else TaskConfig(id=None, name="", adapter_name=""))
        t = self._collect(t)
        # R170：新建前防呆 —— 同网址任务已存在且停止时提示一次。
        if not self._warn_dup_url_on_save(
                editing_tid, list(getattr(t, "entry_urls", None) or []),
                getattr(t, "name", "")):
            return None                      # 用户取消：不落库，也不刷新
        tid = save_task(self.db, t)
        self.refresh()
        return tid

    # ------------------------------------------- R154-F16：克隆 / 模板 ----
    def _reload_templates(self):
        """刷新任务表单里的模板下拉（模板增删后都要调用；开着对话框也能刷）。"""
        combo = getattr(self, "f_tpl", None)
        if combo is None or not self._alive(combo):
            return
        try:
            from .. import taskio
            tpls = taskio.list_templates(self.db)
        except Exception:
            tpls = []
        try:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（不使用模板）", None)
            for t in tpls:
                combo.addItem(t.name or "(未命名)", t.id)
        finally:
            try:
                combo.blockSignals(False)
            except Exception:
                pass

    def _apply_template(self):
        """R154-F16：把选中模板的配置填进表单，并切到「新建任务」态。

        ⚠ 套用后 `_editing_tid=None`：保存时必须**建新任务**，
        绝不能把模板自己 UPDATE 掉（模板也是 tasks 行）。
        """
        combo = getattr(self, "f_tpl", None)
        if combo is None or not self._alive(combo):
            return
        tid = combo.currentData()
        if tid is None:
            QMessageBox.information(
                self, "套用模板", "先在「模板」下拉里选一个模板。")
            return
        from .. import taskio
        t = get_task(self.db, int(tid))
        if t is None or not getattr(t, "is_template", False):
            QMessageBox.warning(
                self, "套用模板", "该模板已不存在，列表已刷新。")
            self._reload_templates()
            return
        self._fill_form(t)
        self._editing_tid = None
        dlg = getattr(self, "_form_dlg", None)
        if self._alive(dlg):
            dlg.setWindowTitle(f"新建任务（模板：{t.name}）")
        QMessageBox.information(
            self, "已套用模板",
            f"已填入模板「{t.name}」的配置。\n"
            "改好网址和名字，选择「保存」就会建成一个新任务。")

    def _save_form_as_template(self):
        """R154-F16：把**当前表单内容**（含未保存修改）存为模板。

        与任务菜单的「存为模板…」的区别：那个从已存盘的任务复制；
        这个从表单现取 —— 用户改到一半发现这配置常用，不必先保存。
        """
        try:
            t = self._collect(TaskConfig(id=None, name="", adapter_name=""))
        except Exception as e:
            QMessageBox.warning(self, "存为模板",
                                f"表单读取失败：{type(e).__name__}: {e}"[:300])
            return
        name, ok = QInputDialog.getText(
            self, "存为模板", "模板名字（新建任务时按名字选）：",
            text=(t.name or ""))
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.warning(self, "存为模板", "模板名字不能为空。")
            return
        from .. import taskio
        t.name = name
        try:
            taskio.save_template_config(self.db, t)
        except Exception as e:
            QMessageBox.warning(self, "存为模板",
                                f"保存失败：{type(e).__name__}: {e}"[:300])
            return
        self._reload_templates()
        self.log_signal.emit(f"已存为模板「{name}」")
        QMessageBox.information(
            self, "已存为模板",
            f"配置已收进模板「{name}」。\n"
            "下次新建任务时在「模板」下拉选它，选择「套用」即可。")

    # 【R170】原 _clone_task 已移除（用户拍板删除克隆功能）。
    # 库层 `taskio.clone_task` 仍保留 —— 它是「用模板新建」的底层实现，
    # 删了会把模板功能一起打死（R173 有 4 条护栏钉着它）。

    def _warn_dup_url_on_save(self, editing_tid, urls, name):
        """R170 防呆：新建任务时若同网址任务已存在**且处于停止态**，提示一次。

        订单要求：套用模板建任务时若同网址任务已存在且停止，弹一次提示。
        这里把判据扩到**所有新建路径**（套模板 / 手填）：
        用户真正怕的是「两个相同任务同时跑」，而入口不止模板一个。
        ``editing_tid`` 非空（编辑已有任务）时不提示 —— 改自己的网址不是新建。

        返回 True=继续保存；False=用户取消。
        """
        if editing_tid is not None:
            return True
        try:
            want = {str(u).strip().rstrip("/").lower()
                    for u in (urls or []) if str(u).strip()}
        except Exception:
            return True
        if not want:
            return True
        try:
            rows = self.db.query(
                "SELECT id, name, status, entry_urls FROM tasks "
                "WHERE is_template=0 OR is_template IS NULL")
        except Exception:
            return True
        hits = []
        for r in rows:
            if editing_tid is not None and int(r["id"]) == int(editing_tid):
                continue
            st = str(r["status"] or "").lower()
            if st not in ("stopped", "idle", "error"):
                continue                     # 正在跑的任务不重复提示
            try:
                have = {str(u).strip().rstrip("/").lower()
                        for u in (r["entry_urls"] or "").split()
                        if str(u).strip()}
            except Exception:
                have = set()
            if have & want:
                hits.append((int(r["id"]), r["name"] or ""))
        if not hits:
            return True
        tid0, nm0 = hits[0]
        box = QMessageBox(self)
        box.setWindowTitle("已存在同网址任务")
        box.setText(f"已存在同网址任务「{nm0}」（#{tid0}）"
                    + (f"，另有 {len(hits) - 1} 个" if len(hits) > 1 else "")
                    + "。\n\n确定仍要新建吗？")
        box.setInformativeText("两个同网址任务同时运行会重复采集。")
        box.setStandardButtons(QMessageBox.StandardButton.Yes |
                               QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("仍要新建")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes
    def _save_task_as_template(self, tid):
        """R154-F16：把已存盘的任务存为模板（任务菜单入口）。"""
        from .. import taskio
        t = get_task(self.db, int(tid))
        if t is None:
            QMessageBox.warning(self, "存为模板",
                                "任务不存在，可能已被删除，列表已刷新。")
            self.refresh()
            return
        name, ok = QInputDialog.getText(
            self, "存为模板", "模板名字（新建任务时按名字选）：",
            text=(t.name or "") + " 模板")
        if not ok:
            return
        name = (name or "").strip()
        if not name:
            QMessageBox.warning(self, "存为模板", "模板名字不能为空。")
            return
        try:
            taskio.save_as_template(self.db, int(tid), name)
        except Exception as e:
            QMessageBox.warning(self, "存为模板",
                                f"保存失败：{type(e).__name__}: {e}"[:300])
            return
        self._reload_templates()
        self.log_signal.emit(f"已把任务「{t.name}」存为模板「{name}」")
        QMessageBox.information(
            self, "已存为模板",
            f"已收进模板「{name}」。\n新建任务时在「模板」下拉选它，选择「套用」。")

    # ----------------------------------------------------- 站点 Cookie 抓取 --
    def _current_task_domain(self):
        """从「网址」里取第一个 URL 的注册域名（用于按域名存/取 Cookie）。"""
        urls = self.f_entry.text().strip().split()
        if not urls:
            return None, None
        return domain_of(urls[0]), urls[0]

    def _on_fetch_cookie(self):
        domain, url = self._current_task_domain()
        if not url:
            QMessageBox.warning(self, "需要网址",
                "请先在「网址」里填一个该站入口 URL，再取 Cookie。")
            return
        if getattr(self, "_cookie_busy", False):
            return
        self._cookie_busy = True
        self.b_fetch_cookie.setEnabled(False)
        self.f_cookie.setPlaceholderText(
            "正在打开浏览器，请在弹窗中通过该站验证/登录…")
        self._cookie_worker = _CookieWorker(url)
        self._cookie_worker.done.connect(self._on_cookie_done)
        self._cookie_worker.failed.connect(self._on_cookie_failed)
        self._cookie_worker.start()

    def _on_cookie_done(self, header):
        # 迟到信号防御：任务对话框可能已被用户关闭销毁（f_cookie 是它的子控件）
        try:
            domain, _ = self._current_task_domain()
            if domain:
                set_cookie(domain, header)
            self.f_cookie.setText(header)
            self.b_fetch_cookie.setEnabled(True)
            self.f_cookie.setPlaceholderText(
                "留空表示不发送（需登录或通过 Cloudflare 校验的站点可使用「取站点 Cookie」自动获取）")
        except RuntimeError:
            return   # 对话框已销毁；Cookie 仍已按域名入库
        self._cookie_busy = False
        QMessageBox.information(
            self, "已保存",
            f"已保存 {domain or '该站'} 的 Cookie（同域名任务自动复用）。")

    def _on_cookie_failed(self, msg):
        try:
            self.b_fetch_cookie.setEnabled(True)
            self.f_cookie.setPlaceholderText(
                "留空表示不发送（需登录或通过 Cloudflare 校验的站点可使用「取站点 Cookie」自动获取）")
            domain, url = self._current_task_domain()
        except RuntimeError:
            return   # 对话框已销毁
        self._cookie_busy = False
        # 自动回退：打开默认浏览器让用户过 CF，再手动粘贴 Cookie
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
        text, ok = QInputDialog.getText(
            self, "手工填写 Cookie",
            f"自动获取不可用：{msg}\n\n请在该站通过验证后，按 F12 → Console 输入 "
            f"document.cookie 复制整串，粘贴到下面：")
        if ok and text.strip():
            if domain:
                set_cookie(domain, text.strip())
            try:
                self.f_cookie.setText(text.strip())
            except RuntimeError:
                pass

    def _on_clear_cookie(self):
        domain, _ = self._current_task_domain()
        if not domain:
            QMessageBox.information(
                self, "提示",
                "请先在「网址」里填该站入口 URL，才能清除对应 Cookie。")
            return
        del_cookie(domain)
        QMessageBox.information(
            self, "已清除",
            f"已删除 {domain} 的已保存 Cookie（本任务框内若手填的值不变）。")

    # ------------------------------------------------- 浏览器插件 CRX --
    def build_extension_crx(self, *_):
        """需求 7：把插件目录打包成 CRX3（可上架应用市场）。

        打包在后台线程跑（要算 RSA 签名，openssl/cryptography 都在写文件），
        免得点一下界面卡住。默认不带私钥 → 生成临时自签密钥，只能本机安装；
        真要上架得把自己的 RSA 私钥放到数据目录下的 extension_key.pem，
        之后每次打包都用它，签名稳定不变（换了密钥商店会拒收）。
        """
        from PySide6.QtWidgets import QMessageBox as _MB
        src = self._extension_dir()
        if not (src / "manifest.json").exists():
            _MB.warning(self, "打包 CRX",
                        f"没找到插件目录里的 manifest.json：\n{src}")
            return
        from ..config import user_data_dir
        key = user_data_dir() / "extension_key.pem"
        out_dir = user_data_dir() / "exports"
        out = out_dir / f"{src.name}.crx"
        self.log_signal.emit(f"正在打包插件 CRX：{src.name} …")

        def _work():
            # R249：实现已搬进包内（tools/ 不参与打包，安装版 import 不到）
            from ..crx import build_crx, verify_crx
            build_crx(src, out, key if key.exists() else None)
            return verify_crx(out)

        def _done(res):
            ok, msg = res
            if ok:
                self.log_signal.emit(f"CRX 已生成：{out}（{msg}）")
                _MB.information(
                    self, "CRX 已生成",
                    f"{out}\n\n{msg}\n\n"
                    + (f"使用保存在 {key.name} 的私钥，可用于上架。\n"
                       if key.exists() else
                       "⚠ 本次是临时自签密钥，只能本机拖进浏览器安装。\n"
                       "要上架请把固定的 RSA 私钥放到：\n"
                       f"{key}\n"))
            else:
                self.log_signal.emit(f"CRX 打包失败：{msg}")
                _MB.warning(self, "打包 CRX", f"失败：{msg}")

        self._bg_submit(_work, _done, "打包CRX")

    # ------------------------------------------------- 浏览器插件（取 Cookie）--
    def _extension_dir(self) -> Path:
        """浏览器插件所在目录；冻结态缺失时从打包数据自愈复制到 exe 旁。"""
        name = "Sweet采集助手"
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).parent / "extension"
            target = root / name
            bundled = Path(getattr(sys, "_MEIPASS", "")) / "extension" / name
            if not target.exists() and bundled.exists():
                try:
                    shutil.copytree(bundled, target)
                except Exception:
                    pass
            return target
        return Path(__file__).resolve().parents[2] / "extension" / name

    def open_extension_folder(self):
        d = self._extension_dir()
        if not d.exists():
            QMessageBox.warning(self, "未找到插件",
                                f"插件目录不存在：\n{d}")
            return
        QMessageBox.information(
            self, "浏览器插件 · 安装 30 秒",
            "① 浏览器地址栏打开 chrome://extensions（360极速同款）\n"
            "② 打开右上角「开发者模式」\n"
            "③ 选择「加载已解压的扩展程序」，选择马上弹出的目录里的\n"
            "    「Sweet采集助手」文件夹\n\n"
            "之后：打开目标网站过验证/登录 → 点插件「复制整串 Cookie」→\n"
            "粘贴到任务的「站点 Cookie」框。详情见目录内「安装说明.txt」。")
        try:
            os.startfile(str(d))     # Windows Explorer
        except Exception:
            QMessageBox.information(self, "插件目录", str(d))

    def _open_task_form_by_item(self, it):
        """双击任务行 → 编辑（R152-A9：任务没了要明说，不许退化成新建）。"""
        tid = self._id_of_item(it)
        t = get_task(self.db, tid) if tid is not None else None
        if t is None:
            QMessageBox.information(
                self, "任务不存在",
                "该任务已不存在（可能已被删除）。\n任务列表已刷新。")
            self.refresh()
            return
        self._open_task_form(t)

    def _edit_by_id(self, tid):
        """表格 edit_requested → 编辑（R152-A9 同 _open_task_form_by_item）。"""
        t = get_task(self.db, tid) if tid is not None else None
        if t is None:
            QMessageBox.information(
                self, "任务不存在",
                f"任务 #{tid} 已不存在（可能已被删除）。\n任务列表已刷新。")
            self.refresh()
            return
        self._open_task_form(t)

    def _edit_selected(self):
        tid = self.selected_id()
        if tid is None:
            QMessageBox.information(self, "提示", "先选中一个任务")
            return
        t = get_task(self.db, tid)
        if t is None:
            # R152-A9：原来是 _open_task_form(None) —— 任务已被删掉时
            # 弹「新建任务」空表单，用户以为在编辑、存出来却是个新任务。
            QMessageBox.information(
                self, "任务不存在",
                f"任务 #{tid} 已经不存在（可能已被删除）。\n"
                "任务列表已刷新，请重新选中后再编辑。")
            self.refresh()
            return
        self._open_task_form(t)

    def _run_by_id(self, tid: int):
        if tid not in self._queue.running:
            self._queue.submit(tid)

    def _stop_by_id(self, tid: int):
        self._queue.stop_task(tid)

    def _reload_adapters(self):
        # r39：面板已被销毁时（用户关掉对话框后）原逻辑会拿悬空控件 .clear() →
        # 每 0.5s 的 refresh 抛一条 libshiboken。现在面板复用，这里再兜一道。
        if not self._alive(getattr(self, "f_adapter", None)):
            return
        # 2026-10-26：后台刷新不许吃掉正在编辑的值。
        # 真机事故：编辑对话框开着时 refresh 每 tick 都走这里 clear() 重填，
        # 把用户选好的适配器重置成「(自动 - gallery-dl)」；用户一保存就
        # 写回 adapter_name="" ⇒ 任务下次运行变成"(自动)"，站内适配器全丢
        #（#1 pictoa / #20 spankbank 就是这么坏的）。先记下当前文本，重填后恢复。
        _cur = self.f_adapter.currentText().strip()
        self.f_adapter.clear()
        # default: no adapter needed - gallery-dl handles supported sites
        self.f_adapter.addItem("(自动 - gallery-dl)")
        d = self.cfg.rules_dir
        if d.exists():
            for p in sorted(d.glob("*")):
                if p.suffix.lower() in (".yaml", ".yml", ".json"):
                    self.f_adapter.addItem(p.stem)
        self.f_adapter.addItem("自定义 / 管理适配器…")
        # 2026-10-26：恢复重填前的选择/手输文本（见上方事故注释）。
        # setCurrentText 对可编辑下拉框：命中项则选中，未命中则作为文本保留。
        if _cur:
            self.f_adapter.setCurrentText(_cur)
        # r39：refresh 每 tick 都走这里，直接 connect 会**每 0.5s 多攒一条连接**
        # （PySide6 允许同 callable 重复 connect），攒到几百条后用户选一次适配器
        # 就被回调几十次。先摘干净再接。
        try:
            # 第一次进来本来就没连过，disconnect 会打一条 libpyside
            # RuntimeWarning —— 用 catch_warnings 咽掉，别污染日志。
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                self.f_adapter.activated.disconnect(self._on_adapter_activated)
        except Exception:
            pass
        self.f_adapter.activated.connect(self._on_adapter_activated)

    def _reload_proxies(self):
        # 2026-10-26：同 f_adapter——后台刷新不许吃掉正在编辑的代理选择
        #（#1 的代理就是这么丢的：编辑保存后 use_proxy=0）。
        _cur = self.f_proxy.currentData()
        self.f_proxy.clear()
        self.f_proxy.addItem("(不使用代理)", None)
        try:
            for e in self.pool.list(only_enabled=False):
                self.f_proxy.addItem(f"{e.name} ({e.mask()})", e.id)
        except Exception:
            pass
        if _cur is not None:
            idx = self.f_proxy.findData(_cur)
            if idx >= 0:
                self.f_proxy.setCurrentIndex(idx)

    # ------------------------------------- 网址与适配器管理器 / 自动匹配 ---
    def _rule_files(self) -> list:
        files = list(self.cfg.rules_dir.glob("*.yaml"))
        files += list(self.cfg.rules_dir.glob("*.yml"))
        files += list(self.cfg.rules_dir.glob("*.json"))
        return sorted(files)

    def _rule_path(self, cur) -> Optional[Path]:
        """选中项 → 规则文件路径。

        名字（不带扩展名）存在列表文本里给人看，完整文件名存在 Qt.UserRole 里，
        取路径一律看 UserRole —— 早先拿 item.text() 去拼，永远拼成 x.yaml.yaml。
        """
        if cur is None:
            return None
        try:
            fname = cur.data(Qt.UserRole)
        except Exception:
            fname = None
        if not fname:
            try:
                fname = cur.text()
            except Exception:
                fname = None
        if not fname:
            return None
        return self.cfg.rules_dir / str(fname)

    def _auto_pick_adapter(self):
        """粘贴网址后按域名自动选中匹配的适配器规则。"""
        url = self.f_entry.text().strip()
        if not url.startswith(("http://", "https://")):
            return
        host = domain_of(url)
        if not host:
            return
        from ..config import load_rule_file
        # 优先级：专属适配器先于 generic-gallery 兜底。generic-gallery 的
        # allowed_domains 覆盖了大批站点，若先匹配它，专属适配器永远没机会被自动选中。
        rule_files = sorted(
            self._rule_files(),
            key=lambda f: (f.stem.lower() == "generic-gallery", f.stem.lower()))
        for f in rule_files:
            try:
                raw = load_rule_file(f)
            except Exception:
                continue
            domains = [str(raw.get("base_url", ""))]
            domains += [str(d) for d in raw.get("allowed_domains", [])]
            for d in domains:
                d = (d.lower().replace("https://", "")
                     .replace("http://", "").strip(" /"))
                if not d or "." not in d:
                    continue
                if host == d or host.endswith("." + d) or d.endswith("." + host):
                    if self.f_adapter.currentText() != raw.get("name", f.stem):
                        self.f_adapter.setCurrentText(str(raw.get("name", f.stem)))
                        self.log_signal.emit(f"已自动匹配适配器: {raw.get('name', f.stem)}")
                    return

    def url_adapter_manager(self):
        """网址与适配器管理器：适配器规则增删改 + 常用网址收藏。"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QDialog, QTabWidget, QListWidget,
                                       QListWidgetItem, QDialogButtonBox)
        dlg = QDialog(self)
        dlg.setWindowTitle("网址与适配器管理器")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)
        tabs = QTabWidget()

        # ---- Tab 1: 适配器 ----
        w1 = QWidget(); v1 = QVBoxLayout(w1)
        lst_adp = QListWidget()
        lst_adp.setToolTip("适配器规则保存在 " + str(self.cfg.rules_dir))
        v1.addWidget(lst_adp)
        r1 = QHBoxLayout()
        def _reload_adp():
            """r40 修复：列表只显示「不带扩展名的规则名」，完整文件名存在 item 的
            UserRole 里。以前直接 addItem(f.name)（带 .yaml），而「打开编辑/删除」
            又拿列表文本去 rules_dir / 文本 拼路径 → 永远拼成 x.yaml.yaml，文件
            不存在 → 点开打不开、点删删不掉，看起来就像「适配器在管理器里没显示」。"""
            lst_adp.clear()
            for f in self._rule_files():
                it = QListWidgetItem(f.stem)
                it.setData(Qt.UserRole, f.name)
                lst_adp.addItem(it)
        b_new = QPushButton("新建规则")
        def _new_rule():
            from PySide6.QtWidgets import QInputDialog
            name, ok = QInputDialog.getText(dlg, "新建适配器", "规则名（英文）:")
            if not ok or not name.strip():
                return
            name = "".join(c for c in name.strip() if c.isalnum() or c in "-_")
            tpl = (self.cfg.rules_dir / f"{name}.yaml")
            tpl.write_text(
                f"name: {name}\nbase_url: https://example.com\n"
                f"allowed_domains: [example.com]\n"
                f"image_link_selectors:\n  - kind: css\n    expr: \"img\"\n"
                f"    attr: src\nmax_depth: 2\n", encoding="utf-8")
            _reload_adp()
        b_edit = QPushButton("打开编辑")
        # 不在这里再包一层同名 _rule_path()：AST 静态防线会把裸调的同名类方法
        # 当「把类方法当全局函数裸调用」报出来。直接调 MainWindow._rule_path。
        def _edit_rule():
            f = self._rule_path(lst_adp.currentItem())
            if f is None:
                return
            if not f.exists():
                # 缩进踩过的坑：原来 startfile 那几行缩在 return 后面、
                # 跟 if 同级 → 文件不存在时警告框弹完还是会尝试打开，
                # 直接抛 OSError。现在明确区分两条路。
                QMessageBox.warning(dlg, "规则不存在", f"未找到规则文件：\n{f}")
                return
            if sys.platform.startswith("win"):
                os.startfile(str(f))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(f)])
            else:
                subprocess.Popen(["xdg-open", str(f)])
        b_del = QPushButton("删除")
        def _del_rule():
            cur = lst_adp.currentItem()
            if not cur:
                return
            f = self._rule_path(cur)
            if f and f.exists() and QMessageBox.question(
                    dlg, "删除适配器", f"删除 {cur.text()} ?") == \
                    QMessageBox.StandardButton.Yes:
                f.unlink()
                _reload_adp()
        b_new.clicked.connect(_new_rule)
        b_edit.clicked.connect(_edit_rule)
        b_del.clicked.connect(_del_rule)
        # ⑧ 自动编写适配器：分析网址 -> 预览候选规则 -> 确认才落盘（不自动入库）。
        b_auto = QPushButton("分析这个网址 / 自动生成规则")
        def _auto_gen():
            from media_resource_manager.crawler.adapter_proposer import open_auto_gen
            open_auto_gen(dlg, self.cfg.rules_dir, on_saved=_reload_adp)
        b_auto.clicked.connect(_auto_gen)
        # 【C1 修】开对话框就先铺一次列表。以前 _reload_adp 只挂在「新建/删除」之后，
        # 打开管理器时 lst_adp 是空的 —— 用户看到的就是「一个适配器都没有」，
        # 哪怕 data/rules/ 下明明有 16 份规则。这不是路径错，是**压根没调用**。
        _reload_adp()
        for b in (b_new, b_edit, b_del, b_auto):
            r1.addWidget(b)
        r1.addStretch()
        v1.addLayout(r1)
        tabs.addTab(w1, "适配器")

        # ---- Tab 2: 常用网址 ----
        w2 = QWidget(); v2 = QVBoxLayout(w2)
        lst_url = QListWidget()
        for u in getattr(self.cfg, "saved_urls", []):
            lst_url.addItem(u)
        v2.addWidget(lst_url)
        r2 = QHBoxLayout()
        url_in = QLineEdit()
        url_in.setPlaceholderText("https://...")
        r2.addWidget(url_in, 1)
        def _add_url():
            u = url_in.text().strip()
            if u.startswith("http"):
                lst_url.addItem(u); url_in.clear()
        b_add = QPushButton("添加")
        b_add.clicked.connect(_add_url)
        url_in.returnPressed.connect(_add_url)
        b_use = QPushButton("填到任务")
        def _use_url():
            cur = lst_url.currentItem()
            if cur:
                self.f_entry.setText(cur.text())
        b_use.clicked.connect(_use_url)
        b_udel = QPushButton("删除")
        def _del_url():
            cur = lst_url.currentItem()
            if cur:
                lst_url.takeItem(lst_url.row(cur))
        # R152-A8：原来这里把 b_add.clicked 又连了一遍 _add_url、b_use 又连了
        # 一遍 _use_url（b_use2=b_use 同一个按钮）⇒ 点一次「添加」加两条、
        # 点一次「填到任务」填两遍（还会持久化两遍）。去重，各只连一次。
        b_udel.clicked.connect(_del_url)
        for b in (b_add, b_use, b_udel):
            r2.addWidget(b)
        v2.addLayout(r2)
        tabs.addTab(w2, "常用网址")

        v.addWidget(tabs)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        bb.clicked.connect(dlg.accept)
        zh_buttons(bb)          # C5：英文系统上别显示 "Close"
        v.addWidget(bb)

        def _save_urls():
            self.cfg.saved_urls = [lst_url.item(i).text()
                                   for i in range(lst_url.count())]
            try:
                from ..config import save_config
                save_config(self.cfg)
            except Exception:
                pass
        b_add.clicked.connect(_save_urls)
        b_udel.clicked.connect(_save_urls)
        b_use.clicked.connect(lambda: dlg.accept)
        dlg.exec()
        try:
            self._reload_adapters()
        except Exception as e:
            self.log(f"刷新适配器列表失败: {e}")

    def _save_filters(self):
        """图片过滤现在是「按任务保存」，这里不再写全局配置。

        全局配置只当作新建任务时的默认值；真正生效的过滤条件随任务一起存在
        tasks 表里，每个任务可以不一样（保存任务时由 _collect 读取）。
        """
        return

    def _on_adapter_activated(self, idx):
        """适配器下拉最后一项 = 自定义/管理适配器入口。"""
        if self.f_adapter.itemText(idx).startswith("自定义"):
            self.url_adapter_manager()

    # R186：授权管理器（auth_manager）整段移除 —— 合规表不再有写入路径。

    def refresh(self):
        # 关闭窗口过程中（_closing=True）队列事件仍可能触发刷新，此时 DB 已关闭，
        # 直接跳过，避免「Cannot operate on a closed database」（#83 健壮性）。
        if getattr(self, "_closing", False):
            return
        # 2026-10-26：任何模态弹窗（任务表单/AI 设置…）可见期间不重建界面 ——
        # 重建会清空弹窗里的下拉框（同 f_adapter 被清空的根因），也是原生
        # 崩溃的触发条件。弹窗关掉后由下一次 tick 补刷。
        try:
            from PySide6.QtWidgets import QApplication
            if QApplication.activeModalWidget() is not None:
                return
            _dlg = getattr(self, "_form_dlg", None)
            if _dlg is not None and _dlg.isVisible():
                return
        except Exception:
            pass
        tasks = list_tasks(self.db)
        # L2：以前这里是「无条件销毁全部行控件 → clear() → 重建 N 个 TaskRow」。
        # N 个自定义 QWidget 的构造 + setItemWidget 是重量级操作，任务多了
        # 一次刷新就能把主线程堵死几百毫秒到数秒（每个队列事件都触发一次）。
        # 现在先 diff：只有「新增 / 状态变了 / 消失了」的行才动。
        self._refresh_task_list(tasks)
        # §2：qB 式原生表格也吃同一份 tasks（**两套都要喂**，作业单明写）。
        # ⚠⚠ **只能跳过「表格」，绝不能跳过「卡片」** —— 默认预设就是
        # classic（= table 可见），若在表格可见时跳过卡片刷新，
        # tests/test_l2_incremental_refresh.py 与 test_task_row_page_switch.py
        # 里读 `win.task_list.itemWidget(...)` 的断言会全拿到 None 而变红，
        # 而那症状（"行控件不见了"）跟真回归**长得一模一样**，极难自证清白。
        # 作业单原话是「隐藏的那套**可以**跳过」——「可以」不是「必须」，
        # 这里选改动面小的那一边：卡片照刷（它自己已有 L2 指纹增量），
        # 表格隐藏时才跳过。
        self._refresh_task_table(tasks)
        # 行宽钳到视口：否则最大化后还原，行控件残留旧宽度把行尾按钮挤出屏幕
        self._sync_task_rows_width()
        self._sync_task_combos()
        # 刷新尾巴（曾被错误吞进 _make_hint_label 的 return 之后，导致删任务/
        # 停止后图库、审核、统计永远不刷新——2026-09-30 修复）。这些控件在
        # __init__ 早期还不存在，按存在性守卫调用。
        if self._alive(getattr(self, "f_adapter", None)):
            self._reload_adapters()
        if self._alive(getattr(self, "f_proxy", None)):
            self._reload_proxies()
        if hasattr(self, "g_search"):
            # r36：图库页**不可见就别重建整张图库**。
            # 以前这里无条件 load_gallery()，等于每次刷新任务列表（点复制任务 /
            # 停止 / 任务状态变化）都把上百~上千行图库重画一遍。实测图库记录一
            # 多（当前库 1882 行），刷新直接把进程打成 SIGSEGV —— C 层段错误，
            # gui-crash.log 一片空白（段错误不写 Python 异常），用户侧表现就是
            #「点一下复制任务，软件悄悄没了」。上次那个吃 1.4 GB 还不退的僵尸
            # 进程也是它一层层堆出来的。
            # 加载时机改成：① 切到图库页（_nav_go）；② 自己点搜索/刷新按钮。
            # 图库页不可见时跳过，任务列表刷新也就不再拖着整张图库一起重建。
            _gal = getattr(self, "_gallery_tab", None)
            if _gal is not None and self.stack.currentIndex() == \
                    self.stack.indexOf(_gal):
                self.load_gallery()
        try:
            # r39：审核页**不可见就别重建**（r36 对图库就是这么处理的）。
            # 以前每个队列事件都把待审核列表整页重画一次，里面的缩略图是在
            # Qt 主线程里一张张用 Pillow 解原图的（avif/webp 单张 30~80ms，
            # 175 张就是十几秒到几分钟），Qt 事件循环被整个堵死 —— 界面假死、
            # 所有按钮点不动（r39 看门狗从线程里扒出来的主线程栈：
            # PIL/WebPImagePlugin.load ← table_view.square_thumb_pixmap
            # ← review_tab.reload ← main_window.refresh ← _on_queue_event）。
            # 现在：① 异步缩略图（见 _thumb_icon_async / thumb_request_async）；
            # ② 不可见时不重建，切到这一页时 _nav_go 自己 reload()。
            _rt = getattr(self, "_review_tab", None)
            if _rt is not None and _rt.isVisible():
                _rt.reload()
            self.refresh_stats()
        except Exception:
            pass

    def _task_row_signature(self, t) -> tuple:
        """任务行「显示内容」的指纹 —— 只有它变了才需要重建那个行控件。

        只看**会显示出来**的字段。刻意不把 id 放进去也不放 created_at：
        前者恒定（判不了变化），后者不显示（变了不该导致整行重建）。
        漏掉某个会显示的字段 → 那一列不更新（症状是「状态还是老的」）；
        多放一个不显示的字段 → 白重建（只是慢，不影响正确性）。
        后者安全，所以宁可多放。
        """
        return (
            t.status.value,
            t.name,
            t.last_error or "",
            t.last_run_at or "",
            getattr(t, "schedule_at", "") or "",
            int(getattr(t, "rate_limit", 0) or 0),
            (getattr(t, "allowed_hours", "") or "").strip(),
            # entry_urls 的长度会显示成「N 个起始网址」
            len(t.entry_urls),
            int(getattr(t, "max_depth", 0) or 0),
            bool(getattr(t, "is_template", False)),
        )

    def _refresh_task_list(self, tasks):
        """L2 增量刷新任务列表：只动真正变化的行。

        三条路（按代价从低到高）：
          ① 指纹全同 → 什么都不做（绝大多数队列事件走这条）；
          ② 少量行变 → 只重建那些行的控件，其余原样留着；
          ③ 大面积变/ 顺序变了 → 整体重建（diff 反而更慢）。

        ⚠ **销毁旧行控件的那段前处理不能省**：``clear()`` 不会删
        ``setItemWidget`` 塞进去的 widget，残留的旧 TaskRow 会带着忙碌条当
        「孤儿」继续悬浮在列表上；r36 补过一次仍会 PySide6 悬空指针段错误，
        所以换掉控件前必须先 removeItemWidget + setParent(None) + deleteLater
        再 ``sendPostedEvents(DeferredDelete)`` 冲刷。增量路径**逐行**做同一套，
        不能因为「只动一行」就省略冲刷。
        """
        from .incr_refresh import plan_incremental
        want_ids = [t.id for t in tasks]
        # 当前列表里的 id（跳过那条「还没有任务」的提示项，它没有 UserRole）
        old_ids = []
        for i in range(self.task_list.count()):
            it = self.task_list.item(i)
            try:
                old_ids.append(int(it.data(Qt.ItemDataRole.UserRole)))
            except (TypeError, ValueError):
                continue        # 提示项/异常项：不参与 diff
        by_id = {t.id: t for t in tasks}
        sigs = getattr(self, "_task_row_sigs", None) or {}

        def changed(tid: int) -> bool:
            t = by_id.get(tid)
            return t is None or sigs.get(tid) != self._task_row_signature(t)

        use_incr, d = plan_incremental(old_ids, want_ids, changed)
        # 空列表 → 走「还没有任务」提示那条路（顺便清掉旧行）
        if not tasks:
            self._rebuild_task_list(tasks)
            self._task_row_sigs = {}
            return
        if not use_incr or d.order_changed or not old_ids:
            self._rebuild_task_list(tasks)
            self._task_row_sigs = {t.id: self._task_row_signature(t)
                                   for t in tasks}
            return
        # ---- 增量：先摘掉消失的行，再补新增的，最后换掉内容变了的 ----
        for tid in d.removed:
            self._drop_task_row(tid)
        for tid in d.updated:
            # 同一位置原地换控件：位置由「当前 id 序列」决定，不重新排序
            self._replace_task_row(tid, by_id[tid])
        for tid in d.added:
            self._insert_task_row(tid, by_id[tid])
        self._task_row_sigs = {t.id: self._task_row_signature(t)
                               for t in tasks}

    def _refresh_task_table(self, tasks, live=None):
        """把 tasks 喂给 qB 式原生表格（§2）。

        ⚠ **只刷可见的那套**：卡片版每行一个 QWidget，200 个任务时
        `setItemWidget` 一次能吃掉几百毫秒，而 refresh() 是被队列事件
        高频触发的 —— 表格可见却还去刷隐藏的卡片，等于白付这份开销。
        代价是隐藏那套会变旧，所以置脏标记，切过去时由
        `_apply_layout_visibility` 补刷（那里有完整说明）。

        ⚠ `reports` 取的是队列的权威口径，两套列表必须说同一件事
        （不一致时用户会以为其中一套坏了）。

        `live`（2026-10-24）：运行中每秒采样的实时进度
        （task_id → {"saved", "rate"}）。reports 只在跑完才写，
        没有 live 的话「已保存/速度」两列运行中也全是空的。
        """
        tt = getattr(self, "task_table", None)
        if tt is None:
            return
        want_table = False
        try:
            from . import theme
            lay = theme.preset_layout(getattr(self.cfg, "ui_preset", None))
            want_table = lay.get("task_view") == "table"
        except Exception:
            want_table = False
        if not want_table:
            self._task_table_dirty = True
            return
        q = getattr(self, "_queue", None)
        reports = getattr(q, "reports", None) if q else None
        try:
            tt.set_tasks(tasks, reports, live)
        except Exception:
            return
        self._task_table_dirty = False

    def _sample_live_progress(self, running: dict) -> dict:
        """2026-10-24：运行中任务的实时「已保存/速度」采样。

        reports 只在任务跑完时才写（queue._run_one 末尾），所以运行中
        「已保存/速度」两列历史上永远是空的（用户原话："从来没见过
        这两个里面有数据"）。这里每秒用 images 表实时计数补上：
          saved = COUNT(*) WHERE task_id
          rate  = 与上一次采样的差值 / 时间差（首采样没有速率）
        一次 GROUP BY 查完所有任务，21 个任务的库成本可忽略。
        """
        import time as _time
        if not running:
            self._live_probe = {}
            return {}
        try:
            rows = self.db.query(
                "SELECT task_id, COUNT(*) AS n FROM images GROUP BY task_id")
            counts = {int(r["task_id"]): int(r["n"]) for r in rows}
        except Exception:
            return {}
        now = _time.monotonic()
        prev = getattr(self, "_live_probe", None)
        if prev is None:
            prev = {}
        out = {}
        for tid in running:
            n = counts.get(int(tid), 0)
            rate = None
            if int(tid) in prev:
                pn, pt = prev[int(tid)]
                dt = now - pt
                if dt >= 0.5:
                    d = n - pn
                    if d > 0:
                        rate = d / dt
            out[int(tid)] = {"saved": n, "rate": rate}
            prev[int(tid)] = (n, now)
        # 跑完的任务从探针里清掉，避免下一轮同 id 任务拿到陈旧基线
        for tid in list(prev):
            if tid not in out:
                prev.pop(tid, None)
        self._live_probe = prev
        return out

    def _task_table_menu(self, pos, tid):
        """表格右键 → 复用 `_task_menu` 的动作集，**直接传 id 与全局坐标**。

        ⚠ 不另写一份菜单：两份菜单必然漂移（加了新动作只改一处 ⇒
        用户在某种皮肤下找不到那个功能，且极难发现）。
        ⚠ 也不做「表格坐标 → 卡片坐标」的换算，理由见 `_task_menu` 头注。
        """
        gp = None
        try:
            gp = self.task_table.viewport().mapToGlobal(QPoint(int(pos.x()),
                                                             int(pos.y()))) \
                if pos is not None else None
        except Exception:
            gp = None
        self._task_menu(None, tid=tid, global_pos=gp)

    def _find_task_row_index(self, tid: int) -> int:
        for i in range(self.task_list.count()):
            it = self.task_list.item(i)
            try:
                if int(it.data(Qt.ItemDataRole.UserRole)) == int(tid):
                    return i
            except (TypeError, ValueError):
                continue
        return -1

    def _drop_task_row(self, tid: int):
        """删掉一行并真正销毁它的行控件（见 refresh 里关于孤儿的注释）。"""
        i = self._find_task_row_index(tid)
        if i < 0:
            return
        it = self.task_list.item(i)
        w = self.task_list.itemWidget(it)
        if w is not None:
            self.task_list.removeItemWidget(it)
            try:
                w.setParent(None)
            except Exception:
                pass
            w.deleteLater()
        self.task_list.takeItem(i)
        try:
            self._task_row_sigs = {k: v for k, v in
                                   (getattr(self, "_task_row_sigs", None)
                                    or {}).items() if k != tid}
        except Exception:
            pass

    def _replace_task_row(self, tid: int, t):
        """原地换掉某一行的行控件（内容变了但位置不变）。"""
        i = self._find_task_row_index(tid)
        if i < 0:
            self._insert_task_row(tid, t)
            return
        old = self.task_list.itemWidget(self.task_list.item(i))
        if old is not None:
            self.task_list.removeItemWidget(self.task_list.item(i))
            try:
                old.setParent(None)
            except Exception:
                pass
            old.deleteLater()
        try:
            self.task_list.setItemWidget(self.task_list.item(i), TaskRow(self, t))
        except Exception:
            self._insert_task_row(tid, t)

    def _insert_task_row(self, tid: int, t):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, t.id)
        item.setSizeHint(QSize(0, 78))
        self.task_list.addItem(item)
        self.task_list.setItemWidget(item, TaskRow(self, t))

    def _rebuild_task_list(self, tasks):
        """全量重建（首屏 / 大面积变动 / 顺序变化时走这条）。

        保留 r36/r38 那套「先摘干净再 clear」的顺序 —— 顺序反了会 PySide6
        悬空指针段错误（gui-crash.log 空白、无Python 异常）。

        ⚠⚠ **r69：重建前必须记住「选中的是哪个任务 id」，填完行再复原。**
        `task_list.clear()` 会把 `currentRow` 归 -1，而走到这条路的条件很常见
        （`plan_incremental` 判「改动比例 > 50%」、顺序变了、首屏那次 ——
        首屏时 `_task_row_sigs` 为空 ⇒ 每行都算「变了」⇒ 必然全量重建）。
        ⇒ 用户选中一个正在跑的任务，状态一变（`_tick` 每秒兜底刷新）就整表重建
        ⇒ 选中态没了 ⇒ 此刻点卡片上的「停止」，`selected_id()` 返回 None，
        **停不掉那个任务**（`selected_id` 的 docstring 警告过「停错了任务」，
        这是它的另一面：什么都停不了）。
        作业单 §2.1 也明确要求「切完不重建窗口，任务选中必须还在」。
        ⇒ 复原用 `_select_by_id`（它两套都设，正是为此存在，见其 docstring）。
        """
        keep_tid = None
        try:
            _cur = self.task_list.currentItem()
            if _cur is not None:
                keep_tid = self._id_of_item(_cur)
        except Exception:
            keep_tid = None
        for i in range(self.task_list.count()):
            _it = self.task_list.item(i)
            w = self.task_list.itemWidget(_it)
            if w is not None:
                self.task_list.removeItemWidget(_it)
                try:
                    w.setParent(None)
                except Exception:
                    pass
                w.deleteLater()
        # 立即冲刷延迟删除事件：确保被停止任务的旧行（含忙碌条）当帧就被
        # 真正销毁，避免停止后旧行的进度条仍以"孤儿"形式悬浮在列表上继续滚动
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.task_list.clear()
        if not tasks:
            hint = QListWidgetItem("暂无任务 —— 单击右上角「+ 新建任务」开始")
            hint.setFlags(Qt.ItemFlag.ItemIsEnabled)
            hint.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.task_list.addItem(hint)
            self.task_list.setItemWidget(hint, self._make_hint_label())
        # ── R84（第 49 项性能）：两遍式插入 ──────────────────────────────
        # 原来是「每行 addItem 后立刻 setItemWidget」—— 交错执行时
        # QListWidget 每挂一行就要为**已在列表里的每一行**重排一次，
        # 于是每行的成本随 N 一起涨（实测 tools/r84_ab_twopass.py，offscreen）：
        #     150 行 7.77ms/行 → 300 行 8.83 → 600 行 11.30  ← 超线性
        # 改成「先铺满全部 item，再逐行挂控件」后重排次数从平方级降到线性：
        #     150 行 6.57ms/行 → 300 行 6.89 → 600 行 7.15    ← 回到线性
        # 600 行总耗时 6.78s → 4.29s（-37%）。
        # 微基准（tools/r84_micro_row.py）定位到成本归属：
        #   addItem 单独 0.02ms/行、setItemWidget 单独 0.17ms/行、
        #   TaskRow 构造 2.17ms/行 ⇒ 三项相加 2.36ms/行，而交错写法 8.8ms/行
        #   ⇒ 多出来的部分全是「反复重排」，不是行控件本身贵。
        # ⚠ 结果等价性已由探针逐条校验：items 数、TaskRow 数、每行 UserRole
        #   id 顺序与 tasks 完全一致（ids_ok=True）⇒ 本改动不改变任何行为。
        # ⚠ 不动 `_insert_task_row`（增量路径一次只插一行，两遍对它没有意义）。
        items = []
        for t in tasks:
            it = QListWidgetItem()
            it.setData(Qt.ItemDataRole.UserRole, t.id)
            it.setSizeHint(QSize(0, 78))
            self.task_list.addItem(it)
            items.append(it)
        for _it, t in zip(items, tasks):
            self.task_list.setItemWidget(_it, TaskRow(self, t))
        # r69：复原重建前的选中态（任务已不在列表里时 `_select_by_id` 是空操作，
        # 自然退化成「什么都没选」，不需要额外判断）。
        if keep_tid is not None:
            try:
                self._select_by_id(keep_tid)
            except Exception:
                pass

    def _make_hint_label(self):
        from PySide6.QtWidgets import QLabel
        lab = QLabel("暂无任务 —— 单击右上角「+ 新建任务」开始")
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(lab, "color:{text_dim}; font-size:14px;")
        return lab

    def refresh_list(self):
        """队列事件驱动的轻量刷新：直接重建迅雷式任务行。"""
        self.refresh()
    def _dot_icon(self, status: str) -> QIcon:
        # P1-4：取色走 status_color()（跟着当前皮肤/深浅现取）。
        # 缓存以**颜色**为键，所以换肤后颜色变了自然落到新条目，
        # 不存在「拿旧皮肤的点图继续用」的问题。
        color = status_color(status)
        pm = self._dot_cache.get(color)
        if pm is None:
            pm = QPixmap(12, 12)
            pm.fill(Qt.GlobalColor.transparent)
            pt = QPainter(pm)
            pt.setRenderHint(QPainter.RenderHint.Antialiasing)
            pt.setBrush(QColor(color))
            pt.setPen(Qt.PenStyle.NoPen)
            pt.drawEllipse(1, 1, 10, 10)
            pt.end()
            self._dot_cache[color] = pm
        return QIcon(pm)

    def selected_id(self):
        """当前选中的任务 id。**卡片与表格两套都要看**（§2）。

        ⚠⚠ 漏掉一套的后果不是「少一个功能」而是**操作作用到错误的行**：
        表格可见时用户选中的是表格第 3 行，`_task_menu` 却去读
        `task_list.currentItem()`（还停在上次卡片时代的选中行）⇒
        「停止」会停掉另一个任务。这种 bug 不崩溃、不报错，
        只是**停错了任务** —— 用户会以为软件有灵异bug。
        所以这里按「哪套可见」问哪套，而不是两套都问（都问会歧义）。
        """
        tt = getattr(self, "task_table", None)
        if tt is not None and tt.isVisible():
            return tt.selected_id()
        item = self.task_list.currentItem()
        if not item:
            return None
        return self._id_of_item(item)

    def _id_of_item(self, item) -> Optional[int]:
        """取任务 id：优先用 UserRole（列表已不显示 #编号）。"""
        try:
            v = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(v, int):
                return v
        except Exception:
            pass
        try:
            for part in item.text().replace("\n", " ").split():
                if part.startswith("#") and part[1:].isdigit():
                    return int(part[1:])
        except Exception:
            pass
        return None

    def _select_by_id(self, tid: int):
        """按 id 选中任务。**两套都要设**（§2）。

        ⚠ 卡片和表格的选中是**各自独立**的（两个不同控件），只设一套的话
        切皮肤后「选中」看起来丢了 —— 用户会以为状态没保存。
        """
        tt = getattr(self, "task_table", None)
        if tt is not None:
            try:
                tt.select_id(tid)
            except Exception:
                pass
        for i in range(self.task_list.count()):
            item = self.task_list.item(i)
            if item and self._id_of_item(item) == tid:
                self.task_list.setCurrentRow(i)
                return

    def _on_select(self, row):
        """行选中：迅雷式布局下表单在编辑对话框里，这里只做轻量记录。"""
        self._selected_tid = self.selected_id()

    def _collect(self, t: TaskConfig) -> TaskConfig:
        # 需求 5：取消独立的「任务名称」输入框 —— 任务名直接取「网址」
        # （首条入口 URL），不另让用户填。空网址时回退占位 "task"。
        entry = self.f_entry.text().strip().split()
        t.name = entry[0] if entry else "task"
        adapter = self.f_adapter.currentText().strip()
        t.adapter_name = "" if adapter.startswith("(自动") else adapter
        t.entry_urls = self.f_entry.text().split()
        # V0.0.4：桌面表单里没有这两个输入框（改用「AI 排除词」），但网页端、
        # 批量导入任务、「不喜欢→加黑名单」都会往这两个字段写值。以前保存
        # 任务时无条件清空，等于把用户填的黑名单抹掉，下一轮照样把不要的图
        # 下回来。现在一律保留原值，只有专门的入口才能改它们。
        # t.keywords / t.blacklist 保持不动
        t.max_depth = self.f_depth.value()
        # 每任务最大抓取页数（r29）：任务级独立，0=不限
        t.max_pages = int(self.f_pages.value() or 0)
        t.concurrency = self.f_conc.value()
        t.delay = self.f_delay.value()
        t.priority = self.f_priority.value()
        t.output_dir = self.f_out.text().strip() or None
        # 记住上次保存地址，下次新建任务默认填充（用户要求「默认读取上次配置」）
        if t.output_dir:
            try:
                if t.output_dir != str(self.cfg.last_output_dir or ""):
                    self.cfg.last_output_dir = Path(t.output_dir)
                    from ..config import save_config
                    save_config(self.cfg)
            except Exception:
                pass
        # 留空即不启用：主题词/细分层/排除词任一非空才开本地 AI
        _deep_words_list, _deep_strict_list = self._deep_level_values()
        t.ai_enabled = bool(self.f_ai_labels.text().strip()
                            or self.f_ai_excl.text().strip()
                            or self.f_ai_l2.text().strip()
                            or self.f_ai_l3.text().strip()
                            or any(w.strip() for w in _deep_words_list))
        # r29 主题词分级：一级 = 入门票；二级起只做细分（打标签/分子目录）
        # 2026-10-28：层数不限 + 每级独立严格。这里统一交给 label_levels 写，
        #   它会同时更新 ai_labels / l2 / l3 三个老字段（老读者/老测试都还能用）。
        from ..label_levels import apply_levels
        _lv_all = [self.f_ai_labels.text().split(),
                   self.f_ai_l2.text().split(),
                   self.f_ai_l3.text().split()]
        _st_all = [False,
                   bool(self.f_strict2.isChecked()),
                   bool(self.f_strict3.isChecked())]
        for _w, _s in zip(_deep_words_list, _deep_strict_list):
            _lv_all.append(_w.split())
            _st_all.append(bool(_s))
        apply_levels(t, _lv_all, _st_all)
        t.ai_blacklist = self.f_ai_excl.text().split()
        t.ai_threshold = self.f_ai_thr.value()
        t.ai_match_mode = self.f_ai_mode.currentData() or "any"
        t.ai_exclude_match_mode = self.f_ai_excl_mode.currentData() or "any"
        t.auto_run = bool(self.f_auto.isChecked())
        # 代理：下拉选择即可（管理对话框负责增删改测），不再手填
        pid = self.f_proxy.currentData()
        t.proxy_id = pid if pid else None
        t.proxy = None
        t.use_proxy = bool(t.proxy_id)
        # #52 站点 Cookie / 登录态：原样随请求下发（留空=不发送）
        # R173：与 Web 端走**同一个清洗函数**（去换行防请求头注入 + 限长 8KB +
        #   支持 cookies.txt 路径）。两端口径必须一致，别在此另写一套。
        from ..task_headers import resolve_task_headers
        t.user_agent, t.cookie = resolve_task_headers(
            self.f_ua.text(), self.f_cookie.text())
        # #28 按主题词分子目录
        t.keyword_subdir = self.f_kwsubdir.isChecked()
        # 图片过滤（分辨率 + 文件大小）按任务保存，每个任务各自独立
        t.min_width = int(self.f_minw.value())
        t.min_height = int(self.f_minh.value())
        t.min_bytes = int(self.f_min_kb.value()) * 1024
        t.max_bytes = int(self.f_max_kb.value()) * 1024
        # 定时运行：留空即不定时。R152-A4 起语义 = 「当天未到点不触发、
        # 错过不补跑」——下午新建的「08:30」任务不会一启动就补跑，下一个
        # 发生周期（明天 08:30）才跑；触发判据见 _check_schedule。
        # （last_run_at 不在这里清：它记录的是上次真实运行时刻。）
        sched = self.f_sched.text().strip()
        if sched:
            kind, _, err = self._parse_schedule(sched)
            if err:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    None, "定时格式不对",
                    f"「定时运行」填的不是有效时间：{sched}\n"
                    "请填 HH:MM（每天）或 日期 HH:MM（一次性），例如 08:30。\n"
                    "本次按「不定时」保存。")
                sched = ""
        t.schedule_at = sched
        # 限速 / 允许时段（#22）：0 / 留空 = 不限，行为与之前完全一致
        t.rate_limit = int(self.f_rate.value())
        t.allowed_hours = self.f_hours.text().strip()
        # 「曾用词」历史：保存任务时记住，下次点一下就能填进去。
        # r31：二三级各自记一份（细分词跟一级主题词混在一起会填错层）
        self._remember_words(t.ai_labels, "ai_label_history")
        self._remember_words(t.ai_labels_l2, "ai_label_history_l2")
        self._remember_words(t.ai_labels_l3, "ai_label_history_l3")
        self._remember_words(t.ai_blacklist, "ai_excl_history")
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        return t

    # ----------------------------------------------- 定时运行调度器 --
    # R152-A4：错过不补跑的宽限度（秒）。调度 tick 是 30s；计划时刻过去超过
    # 这个宽限度还没跑上（例如软件当时关着/睡眠），视为「错过」，不补跑，
    # 等下一个发生周期。设 10 分钟：远大于 tick，又能容忍几分钟的卡顿。
    _SCHED_GRACE_S = 600

    def _parse_schedule(self, s: str):
        """解析「定时运行」字段 —— V0.0.12 起转调共享实现 schedule.parse_schedule。

        桌面与网页表单必须用**同一个解析**：口径漂移会导致同一份文本
        桌面存得进、网页存不进（或反过来），且调度器判据（R152-A4 四条）
        只对一种格式成立。行为逐字不变。
        """
        from ..schedule import parse_schedule
        return parse_schedule(s)

    @staticmethod
    def _parse_dt(s):
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _created_local(s):
        """tasks.created_at 由 sqlite ``datetime('now')`` 写入 = **UTC**；
        last_run_at 是本地时间。比较前先把 created_at 换成本地 naive 时间。"""
        dt = MainWindow._parse_dt(s)
        if dt is None:
            return None
        try:
            off = datetime.now() - datetime.utcnow()
            return dt + off
        except Exception:
            return dt

    def _check_schedule(self):
        """每 30 秒扫一次：到点且没跑过本轮的定时任务自动入队。

        R152-A4 触发判据（四条同时满足才入队）：
          ① ``when <= now``——当天未到点不触发（daily 只返回今天时刻）；
          ② ``last is None or last < when``——本发生周期没跑过；
          ③ ``when >= created``——**计划时刻不得早于任务创建时刻**：
             下午新建的「08:30」任务不会一启动就补跑今天早上那一发；
          ④ ``now - when <= _SCHED_GRACE_S``——错过不补跑：软件当时没开、
             睡过头了，就等下一个周期（daily=明天，once=不跑）。
        """
        if not self._queue:
            return
        try:
            rows = self.db.query(
                "SELECT id, schedule_at, last_run_at, created_at FROM tasks "
                # R154-F16：模板永不定时运行（它只是配置副本）
                "WHERE schedule_at IS NOT NULL AND schedule_at <> '' "
                "AND is_template=0")
        except Exception:
            return
        now = datetime.now()
        for r in rows:
            sched = (r["schedule_at"] or "").strip()
            kind, when, err = self._parse_schedule(sched)
            if err or kind is None or when is None:
                continue
            tid = int(r["id"])
            if tid in self._queue.running or tid in self._queue.queued():
                continue
            last = self._parse_dt(r["last_run_at"])
            if last is not None and last >= when:
                continue                      # ② 本周期已跑过
            created = self._created_local(r["created_at"])
            if created is not None and when < created.replace(
                    second=0, microsecond=0):
                continue                      # ③ 计划时刻早于任务创建
            if when > now:
                continue                      # ① 当天未到点
            if (now - when).total_seconds() > self._SCHED_GRACE_S:
                continue                      # ④ 错过不补跑
            self.log_signal.emit(f"⏰ 定时任务到点，自动运行 #{tid}（{sched}）")
            try:
                self._queue.submit(tid)
            except Exception as e:
                self.log_signal.emit(f"定时运行 #{tid} 入队失败: {e}")
                continue
            self.db.execute(
                "UPDATE tasks SET last_run_at=? WHERE id=?",
                (now.strftime("%Y-%m-%d %H:%M:%S"), tid))

    # ------------------------------------------- 主题词无限分级（2026-10-28）--
    def _add_deep_level(self, words: str = "", strict: bool = False):
        """加一行「第 4 级起」的细分词：``[N 级][输入框][严格][−]``。

        前三级有独立输入框（老用户习惯 + 曾用词按钮），第 4 级起走这里，
        层数不限（上限由 label_levels.MAX_LEVELS 兜底）。
        """
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        lab = QLabel(f"{len(self._deep_rows) + 4} 级")
        lab.setFixedWidth(56)
        edit = QLineEdit()
        edit.setPlaceholderText("这一级的细分词（留空 = 这级不设条件）")
        edit.setText(words or "")
        chk = QCheckBox("严格")
        chk.setChecked(bool(strict))
        chk.setToolTip(
            "勾上 = 这一级必须命中，否则整张图判「不命中」。\n"
            "不勾 = 这一级没命中就停在上一级，图照样保留。")
        btn = QPushButton("−")
        btn.setFixedWidth(26)
        btn.setToolTip("删掉这一级（后面的级会顺次上移）")
        btn.clicked.connect(lambda _=False, r=row: self._remove_deep_level(r))
        h.addWidget(lab)
        h.addWidget(edit, 1)
        h.addWidget(chk)
        h.addWidget(btn)
        self._deep_lay.addWidget(row)
        self._deep_rows.append((row, edit, chk))
        return edit

    def _remove_deep_level(self, row):
        for i, (r, _e, _c) in enumerate(self._deep_rows):
            if r is row:
                self._deep_rows.pop(i)
                self._deep_lay.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
                self._renumber_deep_levels()
                return

    def _renumber_deep_levels(self):
        """删掉中间某级后，把「N 级」的编号重新排一遍（否则会出现 4、6 跳号）。"""
        for i, (r, _e, _c) in enumerate(self._deep_rows):
            lab = r.findChild(QLabel)
            if lab is not None:
                lab.setText(f"{i + 4} 级")

    def _deep_level_values(self):
        """第 4 级起的 (词文本列表, 严格列表)。"""
        return ([e.text() for (_r, e, _c) in self._deep_rows],
                [bool(c.isChecked()) for (_r, _e, c) in self._deep_rows])

    def _clear_deep_levels(self):
        for (r, _e, _c) in list(getattr(self, "_deep_rows", [])):
            self._deep_lay.removeWidget(r)
            r.setParent(None)
            r.deleteLater()
        self._deep_rows = []

    # ------------------------------------------- AI 词「曾用词」快捷点选 --
    def _mk_history_button(self, cfg_key: str, target):
        """「曾用词」直接铺开显示成一行小标签：点一下就填进上面的输入框。

        用户明确要求：不要再藏进下拉框里点开才看到。
        """
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        tip = QLabel("曾用词")
        _tstyle(tip, "color:{text_dim};font-size:12px;")
        # R53b：曾用词行 = [全部填满按钮][曾用词][chip区]，按钮在「曾用词」左边
        b_fill = self._mk_fill_button(cfg_key, target)
        lay.addWidget(b_fill)
        lay.addWidget(tip)
        box = QWidget()
        # ⚠ R47/B11：可换行的 FlowLayout —— 原来是不换行的 QHBoxLayout，
        # 词一多就撑出容器、把相邻控件压住（真机：「常用词列表盖住旁边内容」）。
        bl = _FlowLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        # R179：容器宽度随窗口变化时自动重算高度（不装这个，变窄后
        # 多出来的行会被单行高度裁掉）。
        box._refit = _FlowRefit(box, bl)
        lay.addWidget(box, 1)
        w._box = box
        w._key = cfg_key
        w._target = target
        setattr(self, f"_hist_chips_{cfg_key}", w)
        return w

    # ---- R48/B：曾用词「全部填满」 ----
    # 「白名单/黑名单」在本项目里的实际字段（别被名字误导，代码是唯一事实源）：
    #   白名单 = 本地AI 主题词，一/二/三级 → f_ai_labels / f_ai_l2 / f_ai_l3
    #   黑名单 = 本地AI 排除词               → f_ai_excl
    # 四条历史键：ai_label_history / ai_label_history_l2 / ai_label_history_l3
    #            / ai_excl_history
    _FILL_ALL_MAP = (
        ("ai_label_history", "f_ai_labels"),
        ("ai_label_history_l2", "f_ai_l2"),
        ("ai_label_history_l3", "f_ai_l3"),
        ("ai_excl_history", "f_ai_excl"),
    )

    def _fill_all_history(self) -> int:
        """把曾用词一次性填进全部四个输入框（一/二/三级白名单 + 黑名单）。

        语义要点（都是能测的，别靠肉眼）：
        - **追加不覆盖**：用户已经填过的词必须留着（`_append_word` 的语义），
          凭空覆盖会丢用户输入 —— 那是破坏而不是便捷。
        - **不跨级串词**：一级词不串进二级桶（r31 的规矩，违反会污染匹配）。
        - **主题词永不进排除词**：这是全局铁律，跨桶填充绝不能破。
        - 没有历史的层级**保持原样**（不写空串、不清空已有内容）。

        返回实际新增的词数（便于测试断言与日志）。
        """
        total = 0
        touched = 0
        for cfg_key, field in self._FILL_ALL_MAP:
            target = getattr(self, field, None)
            if target is None:
                continue
            words = list(getattr(self.cfg, cfg_key, []) or [])
            if not words:
                continue
            before = len(target.text().split())
            for wd in words:
                self._append_word(target, str(wd))
            after = len(target.text().split())
            total += max(0, after - before)
            touched += 1
        try:
            self._refresh_hist_chips()
        except Exception:
            pass
        self.log(f"「全部填满」：已把曾用词填入 {touched} 个输入框，新增 {total} 个词")
        return total

    def _mk_fill_button(self, cfg_key: str, field, label: str = "全部填满"):
        """曾用词「全部填满」：只填**当前这一级**的输入框（不碰其它级）。

        R52：取代 R51 的全局「全部填满」——每个级别（一/二/三级白名单 + 黑名单）
        在各自曾用词下方各放一个，点哪个就只填满那一级，符合用户「分级全部填满」诉求。
        """
        b = QPushButton(label)
        b.setObjectName("btnFillOne_" + cfg_key)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        _lvl = {
            "ai_label_history": "一级主题词（白名单）",
            "ai_label_history_l2": "二级细分词",
            "ai_label_history_l3": "三级细分词",
            "ai_excl_history": "排除词（黑名单）",
        }.get(cfg_key, "曾用词")
        b.setToolTip(
            f"将「{_lvl}」的曾用词一次性填入上方输入框，无需逐条添加。\n"
            f"仅追加，不覆盖已填写内容；重复的词不会重复添加。")
        _tstyle(b,
                "QPushButton{background:{accent};border:none;border-radius:9px;"
                "color:{on_accent};font-size:12px;padding:2px 10px;}"
                "QPushButton:hover{background:{accent_deep};}")
        b.clicked.connect(
            lambda _=False, ck=cfg_key, fld=field: self._fill_one_history(ck, fld))
        return b

    def _fill_one_history(self, cfg_key: str, field) -> int:
        """把单条曾用词历史填进对应输入框（追加、去重、不覆盖已有内容）。

        语义同 _fill_all_history，但只处理传入的那一级 —— 不跨级串词、
        主题词永不进排除词（全局铁律）。返回实际新增词数（供日志/测试）。
        """
        if not self._alive(field):
            return 0
        try:
            hist = list(getattr(self.cfg, cfg_key, []) or [])
        except Exception:
            hist = []
        if not hist:
            self.log(f"「{cfg_key}」暂无曾用项")
            return 0
        cur = field.text().split()
        have = set(cur)
        add = []
        for w in hist:
            w = (w or "").strip()
            if not w or w in have:
                continue
            add.append(w)
            have.add(w)
        if not add:
            self.log("曾用词都已填过，无新增")
            return 0
        field.setText(" ".join(cur + add))
        try:
            self._refresh_hist_chips()
        except Exception:
            pass
        self.log(f"「全部填满」：已把 {len(add)} 个曾用词填入 {cfg_key}")
        return len(add)


    def _refresh_hist_chips(self):
        """把历史词重建成一排可点的标签（打开任务对话框时调用）。

        ⚠ R47/B11：原来这些 chip 直接塞进一个**不换行**的 QHBoxLayout，
        最多 12 个 + 一个折叠入口全挤在一行上。词一多（或者窗口窄、
        或者开了字体缩放），这一行就撑出容器、把**相邻控件压住/盖住**
        ——真机反馈「常用词列表盖住旁边内容，很难看」。
        R179（用户原话「词多就横排铺过去、自动换行、不要更多」）：
        全部词条无条件平铺 + `_FlowLayout` 自动换行，容器高度由
        `fit_flow_height` 显式撑到装得下 —— 不再有折叠入口、不再截断。
        """
        from PySide6.QtWidgets import QWidget
        for cfg_key in ("ai_label_history", "ai_label_history_l2",
                        "ai_label_history_l3", "ai_excl_history"):
            w = getattr(self, f"_hist_chips_{cfg_key}", None)
            if w is None:
                continue
            box = w._box
            target = w._target
            lay = box.layout()
            while lay.count():
                it = lay.takeAt(0)
                if it.widget():
                    # ⚠ R47-c：必须**先 hide 再 deleteLater**——deleteLater 是
                    # 延迟投递（嵌套 processEvents 里因 loopLevel 不派发，
                    # 实测 6 次都不清），旧 chip 在这期间**仍然可见**且带着
                    # 上一次布局的几何，既会被 findChildren 抓到假越界，
                    # 真机上也会肉眼看到旧词挂一瞬。hide() 同步生效。
                    it.widget().hide()
                    it.widget().deleteLater()
            words = list(getattr(self.cfg, cfg_key, []) or [])
            if not words:
                lbl = QLabel("（保存任务后自动记录）")
                _tstyle(lbl, "color:{text_dim};font-size:12px;")
                lay.addWidget(lbl)
                # ⚠ 必须是 `continue` 不是 `return`：这个循环要遍历**四个**
                # 历史键（一级/二级/三级/排除词），只有当前这个没词。
                # 写成 return 会让后面几个键的 chip 一个都不生成
                # （test_view_size.test_chips_render_every_level 实测抓到的
                #   真回归：一级为空 → 二级/三级的常用词全都不显示）。
                continue
            # R179（用户原话「词多就横排铺过去、自动换行、不要更多」）：
            # 全部词条**无条件**建 chip，按容器宽度自动换行；容器高度由
            # `fit_flow_height` 显式撑到装得下（R189 实测父布局按单行给死，
            # 不撑高第二行必被裁）。不再有按宽度额度截断、不再有折叠入口。
            # 省略宽度随容器收窄，保证窄容器里单个 chip 也放得下
            _elide_w = max(40, min(140, int(box.width() or 320) - 22))
            # r40：界面显示走 i18n（标签默认中文化，后期接语言包只改 i18n）；
            # 存入配置 / 比对 / 回填输入框的仍然是原始英文 key（tag_text）。
            for wd in words:
                b = QPushButton(label_text(str(wd)))
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip("左键：填进上面的输入框；右键：从常用词里删除")
                # ⚠ B11：词本身可能很长，超长时**省略**而不是把 chip 撑成
                # 一整行（chip 越宽越容易把整行顶爆）。
                b.setMaximumWidth(150)
                b.setText(_elide(b.fontMetrics(), label_text(str(wd)),
                                 _elide_w))
                _tstyle(b,
                        "QPushButton{background:{accent_soft};border:none;"
                        "border-radius:9px;color:{accent_deep};font-size:12px;"
                        "padding:2px 9px;}"
                        "QPushButton:hover{background:{accent_soft_hover};}")
                b.clicked.connect(
                    lambda _=False, x=str(wd), t=target: self._append_word(t, x))
                b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                b.customContextMenuRequested.connect(
                    lambda _pos, k=cfg_key, w=str(wd): self._delete_hist_word(k, w))
                lay.addWidget(b)
                # ⚠ R47-c：addWidget 后必须**立即 b.show()**——Qt 把「show 子
                # 控件」推迟到布局激活，而 box 可见时新加的按钮在那之前
                # isHidden()=True ⇒ QWidgetItem.sizeHint() 返回 (0,0)
                # （mini 实测：btn.sizeHint() 正常、item.sizeHint()=0x0）。
                # 紧随其后的 fit_flow_height 全按 0 高算 ⇒ 容器高度被低估
                # 成 1~2 行，chip 全溢出（B11 复发根因，pytest 打点钉死）。
                b.show()
            # 重建完立刻把容器撑到当前宽度需要的高度（多行不被裁）
            fit_flow_height(box, lay)

    def _show_word_history(self, cfg_key: str, target):
        from PySide6.QtWidgets import QMenu, QWidgetAction
        words = list(getattr(self.cfg, cfg_key, []) or [])
        menu = QMenu(self)
        menu.setToolTip("左键点词=填入输入框；右键点词=从常用词删除")
        if not words:
            menu.addAction("暂无曾用词（保存任务后自动记录）").setEnabled(False)
        else:
            for w in words:
                # 显示中文，w 本身仍是原始英文 key（填入/删除都按它走）
                sub = QMenu(label_text(str(w)), menu)
                a_fill = sub.addAction("填入输入框")
                a_fill.triggered.connect(
                    lambda _=False, word=w: self._append_word(target, word))
                a_del = sub.addAction("从常用词删除")
                a_del.triggered.connect(
                    lambda _=False, k=cfg_key, word=w: self._delete_hist_word(k, word))
                menu.addMenu(sub)
        menu.exec(target.mapToGlobal(target.rect().bottomLeft()))

    def _delete_hist_word(self, cfg_key: str, word: str):
        """右键删除一条「曾用词」历史（主题词 / 排除词各自的常用词列表）。"""
        hist = list(getattr(self.cfg, cfg_key, []) or [])
        if word not in hist:
            return
        hist.remove(word)
        setattr(self.cfg, cfg_key, hist)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        try:
            self._refresh_hist_chips()
        except Exception:
            pass
        self.log(f"已删除常用词「{word}」（{cfg_key}）")

    def _append_word(self, target, word: str):
        """把一个词追加到输入框（空格分隔，已有则不重复加）。"""
        cur = target.text().split()
        if word in cur:
            return
        cur.append(word)
        target.setText(" ".join(cur))

    def _remember_words(self, words, cfg_key: str):
        """保存任务时把用过的词记进历史：新的排最前，上限 30 条。"""
        hist = list(getattr(self.cfg, cfg_key, []) or [])
        for w in words or []:
            w = str(w).strip()
            if not w:
                continue
            if w in hist:
                hist.remove(w)
            hist.insert(0, w)
        setattr(self.cfg, cfg_key, hist[:30])

    def _apply_blacklist_retro(self, t):
        """保存任务后，把黑名单追应用到该任务已下载进图库的图片。"""
        if t.id is None or not t.blacklist:
            return
        try:
            from ..engine import apply_blacklist_existing
            moved = apply_blacklist_existing(self.db, t, self.cfg.isolate_dir)
            if moved:
                self.log_signal.emit(
                    f"黑名单追溯清理: 任务 #{t.id} 有 {moved} 张已移入隔离区")
        except Exception as e:
            self.log_signal.emit(f"黑名单清理失败: {type(e).__name__}: {e}")

    @staticmethod
    def _alive(o):
        """控件是否还活着（r39）。

        任务表单面板被 GC/销毁后，`self.f_*` 里存的只是悬空的 shiboken 包装，
        任何 `.text()` / `.clear()` 都会抛
        ``RuntimeError: Internal C++ object already deleted`` —— 上次是
        `_on_cookie_done` 撞到，补了个 `except RuntimeError` 就过去了；结果
        r39 真机全按钮扫描一开就撞在 `add_task` 和 `_tick→refresh` 上，
        每 0.5s 刷一条 traceback。这里收成统一判活，别再到处兜异常。
        """
        if o is None:
            return False
        try:
            from shiboken6 import isValid
            return bool(isValid(o))
        except Exception:
            return True

    def _ensure_task_form(self):
        """任务表单面板（f_* 那批输入框）是**懒构建**的：只有开过「新建/编辑任务」
        对话框才建过。首次就直接选择「任务 → 新增任务」时 `self.f_entry` 根本不存在，
        `add_task()` 第一行就 `AttributeError`（r38 真机 soak 抓到的现场）。

        **r39 修复（真机扫描抓到）**：`_build_task_form_panel()` 造出来的面板原本
        是「裸 QWidget」——没有 Qt 父节点，函数一返回 Python 侧就再没人引用它，
        PySide6 立刻把整棵面板连同 `f_entry / f_adapter / f_out …` 一起销毁，
        而 `self.f_entry` 还指着那个悬空包装 → 下一次访问就炸 libshiboken。
        修法：**面板必须 `setParent(self)` 挂到主窗口**，生命周期跟着主窗口走，
        控件永不悬空；`_open_task_form` 再复用这同一套面板，不再每次重建。

        这里补上「没有就先建一个」，点新增任务依然走正常提示（面板是空的 → 弹
        「请填写入口」），不会再崩。
        """
        panel = getattr(self, "_form_panel", None)
        if self._alive(panel):
            return
        try:
            panel = self._build_task_form_panel()
            panel.setParent(self)          # 关键：挂到主窗口，别让面板被 GC 掉
            try:
                panel.hide()
            except Exception:
                pass
            self._form_panel = panel
            # r40：面板重建说明旧面板已被销毁，而旧对话框里挂的正是那个死面板
            # → 必须让对话框下次重新装配，否则只会 setWindowTitle 就 exec() 出空白框。
            try:
                self._form_dlg = None
            except Exception:
                pass
        except Exception:
            pass

    def add_task(self):
        """打开空白的「新建任务」对话框（不再偷偷沿用上次填的表单直接存盘）。"""
        self._ensure_task_form()
        self._fill_form(TaskConfig(id=None, name="", adapter_name=""))
        self._open_task_form(None)

    def save_selected(self):
        tid = self.selected_id()
        if tid is None:
            return
        t = get_task(self.db, tid)
        if not t:
            return
        save_task(self.db, self._collect(t))
        self._apply_blacklist_retro(t)
        self.log_signal.emit(f"已保存任务 #{tid}")
        self.refresh()

    def backfill_local(self):
        """回填：后台线程把磁盘上已有、但没进库的图片补写进图库，界面不卡死。"""
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout,
                                      QCheckBox, QPushButton, QLabel,
                                      QProgressDialog)

        # 1) 选项：是否重新跑 AI 主题过滤（开销大，默认只补链接）
        dlg = QDialog(self)
        dlg.setWindowTitle("回填本地图片到图库")
        dlg.resize(440, 170)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "扫描各任务的保存目录，将磁盘上已存在但尚未入库的图片补充登记。\n"
            "已在库里的会自动跳过，不会重复。大目录在后台慢慢扫，界面不卡。"))
        cb = QCheckBox("重新跑 AI 主题词过滤（命中黑名单 / 主题不符的进审核，较慢）")
        v.addWidget(cb)
        row = QHBoxLayout()
        b_ok = QPushButton("开始回填")
        b_ok.setDefault(True)
        b_cancel = QPushButton("取消")
        row.addStretch(1)
        row.addWidget(b_ok)
        row.addWidget(b_cancel)
        v.addLayout(row)
        b_ok.clicked.connect(dlg.accept)
        b_cancel.clicked.connect(dlg.reject)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        re_filter_ai = cb.isChecked()

        # 2) 进度对话框（后台线程跑，可取消）
        prog = QProgressDialog("正在回填：扫描磁盘图片…", "取消", 0, 0, self)
        prog.setWindowTitle("回填中…")
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(0)
        prog.setAutoClose(False)
        prog.setValue(0)

        worker = _BackfillWorker(self.cfg.db_path, re_filter_ai=re_filter_ai)
        self._bf_worker = worker   # 防止被 GC

        def on_progress(scanned, added, current):
            tail = current[-90:] if current else ""
            prog.setLabelText(
                f"已扫描 {scanned} 个文件，新补写 {added} 张进图库…\n{tail}")
            # 维持事件循环（busy 进度条），并让"取消"可点
            prog.setValue(prog.value() + 1)

        def on_finished(added):
            try:
                prog.reset()
                prog.close()
            except Exception:
                pass
            QMessageBox.information(
                self, "回填完成",
                f"已把磁盘上 {added} 张图片补写进图库。\n"
                f"（之前已经在库里的会自动跳过，不会重复。）")
            self.refresh()

        def on_canceled():
            worker.cancel()

        def on_error(msg):
            try:
                prog.close()
            except Exception:
                pass
            QMessageBox.critical(self, "回填失败", msg)

        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.errored.connect(on_error)
        prog.canceled.connect(on_canceled)
        worker.start()

    def batch_import_tasks(self):
        """批量导入任务：每行一个网址，一次建多个任务（其余用默认配置）。"""
        from PySide6.QtWidgets import (QDialog, QPlainTextEdit, QDialogButtonBox,
                                       QMessageBox, QVBoxLayout, QLabel)
        import json
        dlg = QDialog(self)
        dlg.setWindowTitle("批量导入任务（每行一个网址）")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "每行粘贴一个网址，可一次建多个任务（其余用默认配置）。\n"
            "空行忽略；同一网址已存在则跳过，不会重复建。"))
        te = QPlainTextEdit()
        te.setPlaceholderText("https://site1/album\nhttps://site2/album")
        v.addWidget(te, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                               QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("导入")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        zh_buttons(bb)          # C5：只补 Cancel，"导入" 是业务词不会被改
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        urls = [u.strip() for u in te.toPlainText().splitlines() if u.strip()]
        if not urls:
            return
        have = set()
        for r in self.db.query("SELECT entry_urls FROM tasks"):
            try:
                for uu in json.loads(r["entry_urls"] or "[]"):
                    have.add(uu)
            except Exception:
                pass
        added = skipped = 0
        for u in urls:
            if u in have:
                skipped += 1
                continue
            t = TaskConfig(id=None, name="", adapter_name="", entry_urls=[u])
            t.output_dir = str(self.cfg.last_output_dir or self.cfg.library_dir)
            save_task(self.db, t)
            added += 1
        self.refresh()
        QMessageBox.information(
            self, "批量导入完成",
            f"新建 {added} 个任务，跳过重复 {skipped} 个。\n"
            "在「任务」页可逐个调整配置，或选择「全部开始」一起跑。")

    def delete_selected(self):
        tid = self.selected_id()
        if tid is None:
            return
        if QMessageBox.question(
                self, "确认", f"删除任务 #{tid}？") != QMessageBox.StandardButton.Yes:
            return
        # R221/C10：外键开启后必须先清子表再删父表 —— failed_urls /
        # task_pages 都 REFERENCES tasks(id)。R154-F9 的页面账本清理因此
        # 从「删后」提到「删前」（顺序反了会被 FK 拦下）。
        for _clr in (self.db.clear_failed_urls, self.db.clear_task_pages):
            try:
                _clr(tid)
            except Exception:
                pass
        self.db.execute("DELETE FROM tasks WHERE id=?", (tid,))
        self.db.audit("gui", "delete_task", str(tid))
        self.refresh()

    # --------------------------------------------------------------- run ----
    def _site_of(self, tid: int) -> str:
        t = get_task(self.db, tid)
        return domain_of(t.entry_urls[0]) if (t and t.entry_urls) else ""

    def _authorized(self, site: str) -> bool:
        if not site:
            return False
        return bool(self.db.query(
            "SELECT 1 FROM compliance WHERE site=? AND robots_ok=1 LIMIT 1",
            (site,)))

    def run_selected(self):
        # 【R117 P1-2】单选 → 多选：作用于**所有选中行**。
        tids = self._selected_task_ids()
        if not tids:
            QMessageBox.information(self, "提示", "先在列表里选中一个或多个任务")
            return
        # 已在运行的跳过（别重复入队），其余一次性 submit_many。
        items = [(t, None) for t in tids
                 if self._queue and t not in self._queue.running]
        if not items:
            QMessageBox.information(self, "提示",
                                    "选中的任务当前都已在运行中")
            return
        self._queue.submit_many(items)
        self.log_signal.emit(f"已入队 {len(items)} 个选中任务")
        self.refresh_list()

    def _selected_task_ids(self) -> list:
        """【R117 P1-2】当前选中的任务 id 列表（任务表多选 → 集合）。

        卡片版（task_list）保持单选语义：只取 current 项。
        """
        tt = getattr(self, "task_table", None)
        if tt is not None:
            try:
                return list(tt.selected_ids())
            except Exception:
                pass
        tid = self.selected_id()
        return [tid] if tid is not None else []

    # R186：授权流程（authorize_selected / _authorize_flow / _do_authorize）
    # 整段移除 —— 授权门禁已随合规层一起删除。

    def run_auto(self):
        if self._queue:
            n = enqueue_auto_tasks(self._queue, self.cfg)
            self.log_signal.emit(f"已入队 {n} 个自动任务")

    def stop_selected(self):
        # 【R117 P1-2】同上：作用于所有选中行。
        tids = self._selected_task_ids()
        if not tids:
            QMessageBox.information(self, "提示",
                                    "先在左侧选中要停止的一个或多个任务")
            return
        stopped = 0
        for tid in tids:
            if self._queue and self._queue.stop_task(tid):
                stopped += 1
        if stopped:
            self.log_signal.emit(f"正在停止 {stopped} 个任务…")
        else:
            QMessageBox.information(self, "提示",
                                    "选中的任务当前都没有在运行")
        self.refresh_list()

    def _all_task_ids(self) -> list[int]:
        """任务列表里当前展示的所有任务 id（与数据库一致）。"""
        ids: list[int] = []
        for i in range(self.task_list.count()):
            item = self.task_list.item(i)
            tid = self._id_of_item(item)
            if tid is not None:
                ids.append(tid)
        return ids

    def run_all(self):
        """「全部开始」：把列表里所有未运行的任务一次性入队。

        已运行的任务不会重复入队；同时运行数量仍受「同时运行任务」限制。
        单个任务的运行/停止仍可由每行的按钮或右键菜单控制。
        """
        if not self._queue:
            return
        running_now = len(getattr(self._queue, "running", ()) or ())
        items = [(tid, None) for tid in self._all_task_ids()
                 if tid not in self._queue.running]
        if not items:
            QMessageBox.information(
                self, "提示",
                "没有可运行的任务（可能都在运行中，或列表为空）")
            return
        self._queue.submit_many(items)
        # R180：如实说清「入队多少 / 上限多少 / 立刻跑几个 / 排队几个」，
        # 不再出现「22 个都开始」这种与并发上限矛盾的文案。
        self.log_signal.emit(format_start_all_message(
            len(items), int(getattr(self._queue, "max_concurrent", 1) or 1),
            running_now))
        # R177：22 个任务的 queued 事件马上就会各来一次刷新，这里再同步
        # 重建一次列表＝「点一下卡一下」的主因。改成挂号刷新（500ms 合并）。
        self._schedule_list_refresh()

    def stop_all(self):
        """「全部停止」：停止当前所有正在运行的任务。

        R177：这里**绝不同步重建任务列表**。cancel 22 个任务会连着甩回
        22 个 stopped 事件，每个都走 `_schedule_list_refresh()`（500ms
        合并一次）；再同步 refresh_list() 一次等于在最忙的那一刻把主线程
        按在列表重建里 —— 用户感受到的「点了全部停止窗口拖不动」就是它。
        """
        if not self._queue:
            return
        self._queue.stop_all()
        self.log_signal.emit("正在停止全部运行中的任务…")
        self._schedule_list_refresh()

    def run_all_quiet(self):
        """托盘版「全部开始」：窗口可能隐藏，一律不弹窗，只写日志。"""
        if not self._queue:
            return
        running_now = len(getattr(self._queue, "running", ()) or ())
        ids = [t for t in self._all_task_ids() if t not in self._queue.running]
        if not ids:
            self.log_signal.emit("托盘「全部开始」：没有可运行的任务")
            return
        self._queue.submit_many([(tid, None) for tid in ids])
        # R180：托盘版同样如实（窗口可能藏着，只能靠日志看懂）。
        self.log_signal.emit("托盘「全部开始」：" + format_start_all_message(
            len(ids), int(getattr(self._queue, "max_concurrent", 1) or 1),
            running_now))
        self.refresh_list()

    def stop_all_quiet(self):
        """托盘版「全部停止」：不弹窗，只写日志。"""
        if not self._queue:
            return
        self._queue.stop_all()
        self.log_signal.emit("托盘「全部停止」：正在停止全部运行中的任务…")
        self._schedule_list_refresh()

    def _cleanup_plan_dialog(self):
        """#37：把「一句笼统二次确认」摊成复选项清单。

        返回勾选的 key 集合（见 cleanup.CLEAN_ITEMS）；点了取消 / 把窗关掉 = None。
        ⚠ 这里**不再逐项问回收站**（D9）：「是否进回收站」是设置里那一个全局开关
        （cfg.use_recycle，菜单「隔离区 → 删除时先进回收站」），日常删除路径统一
        读它；而「全面清理」是用户显式勾的破坏性动作，勾了就是真删硬盘，不受它管。
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget,
                                       QListWidgetItem, QPushButton, QVBoxLayout)
        from ..cleanup import CLEAN_ITEMS, DEFAULT_PLAN

        d = QDialog(self)
        d.setWindowTitle("全面清理 · 勾选要清掉的东西")
        d.setMinimumWidth(560)
        lay = QVBoxLayout(d)

        tip = QLabel("勾完选择「确认」。标「(真删硬盘)」的那些＝文件直接从硬盘抹掉，"
                     "不进回收站、不可恢复；只清列表／只清缓存的勾上没所谓。")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        lst = QListWidget()
        rows = []
        for key, name, danger, desc in CLEAN_ITEMS:
            item = QListWidgetItem(name + ("（真删硬盘）" if danger == "risky" else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if key in DEFAULT_PLAN
                               else Qt.CheckState.Unchecked)
            item.setToolTip(desc)
            item.setData(Qt.ItemDataRole.UserRole, key)
            lst.addItem(item)
            rows.append(item)
        lay.addWidget(lst, 1)

        def _check(keys):
            for it in rows:
                it.setCheckState(Qt.CheckState.Checked
                                 if it.data(Qt.ItemDataRole.UserRole) in keys
                                 else Qt.CheckState.Unchecked)

        row = QHBoxLayout()
        b_all = QPushButton("全选")
        b_def = QPushButton("默认")
        b_cancel = QPushButton("取消")
        b_ok = QPushButton("确认")
        b_all.setToolTip("全勾（连 AI 模型、站点适配器也一起清）")
        b_def.setToolTip("回到默认：只清业务数据，学习记录 / 适配器 / 模型都留着")
        b_all.clicked.connect(lambda: _check({k for k, *_ in CLEAN_ITEMS}))
        b_def.clicked.connect(lambda: _check(set(DEFAULT_PLAN)))
        for b in (b_all, b_def):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_cancel)
        row.addWidget(b_ok)
        lay.addLayout(row)

        rc = bool(getattr(self.cfg, "use_recycle", True))
        hint = QLabel(
            "常规删除（「不喜欢」/ 批量删）现在："
            + ("先进回收站、可还原。" if rc else "直接永久删除。")
            + "「全面清理」不受它影响 —— 勾了就是真删。要改这个开关去「隔离区 → 删除时先进回收站」。")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        b_cancel.clicked.connect(d.reject)
        b_ok.clicked.connect(d.accept)
        if d.exec() != QDialog.DialogCode.Accepted:
            return None
        return {CLEAN_ITEMS[i][0] for i in range(lst.count())
                if lst.item(i).checkState() == Qt.CheckState.Checked}

    def full_cleanup(self):
        """全面清理：按勾选清单清（#37）。

        默认只清业务数据；站点适配器 / AI 模型 / 学习记录 / 设置 / Cookie 默认不勾。
        """
        from PySide6.QtWidgets import QMessageBox
        # 先停掉所有运行中的任务，避免清理过程中 DB 被写
        if self._queue:
            self._queue.stop_all()
        plan = self._cleanup_plan_dialog()
        if not plan:
            return
        # #37：学习记录不再由代码替用户拍 —— 勾了清单里「学习记录」这一项才真删
        drop_prefs = "prefs" in plan
        try:
            from ..cleanup import perform_full_cleanup
            # 核心清理逻辑（保留适配器/模型/学习记录/设置/Cookie）抽到 cleanup 模块，
            # 便于单元测试；GUI 只负责停任务 + 二次确认 + 刷新。
            perform_full_cleanup(self.db, self.cfg, user_data_dir(),
                                 drop_prefs=drop_prefs, plan=plan)
            # 5) 刷新界面
            self.refresh_list()
            try:
                self.load_gallery()
            except Exception:
                pass
            try:
                self._review_tab.reload()
            except Exception:
                pass
            self.refresh_stats()
            from ..cleanup import CLEAN_ITEMS
            hit = [n for k, n, _dg, _d in CLEAN_ITEMS if k in plan]
            miss = [n for k, n, _dg, _d in CLEAN_ITEMS if k not in plan]
            self.log_signal.emit("全面清理完成：已清理 " + " / ".join(hit))
            QMessageBox.information(
                self, "完成",
                "本次勾了 " + str(len(plan)) + " 项：\n　" + "、".join(hit) + "\n"
                + ("学习记录已清空，AI 会重新学。\n\n" if drop_prefs else
                   "未勾的（" + "、".join(miss) + "）原样保留。\n\n")
                + "适配器 / 模型 / 设置 / Cookie 不在清单里，默认不动。")
        except Exception as e:
            QMessageBox.critical(self, "清理失败", str(e))

    def toggle_pause(self):
        if not self._queue:
            return
        if self._queue.snapshot()["paused"]:
            self._queue.resume(); self.log_signal.emit("队列已继续")
        else:
            self._queue.pause(); self.log_signal.emit("队列已暂停")

    def retry_failed(self):
        # #6：retry_failed 返回**真实入队条数**。0 条时必须明确提示，
        # 不许照旧弹「失败任务已重新入队」—— 那是骗人的（旧实现把失败标记
        # 清掉了，用户以为重试成功，任务其实永远不会重跑）。
        if not self._queue:
            return
        total = len(getattr(self._queue, "failed", {}) or {})
        try:
            n = self._queue.retry_failed()
        except Exception as e:
            self.log_signal.emit(f"重试失败: {type(e).__name__}: {e}"[:300])
            return
        if n <= 0:
            self.log_signal.emit(
                "没有可重试的失败任务" if total == 0
                else f"没能重新入队（{total} 条失败任务都入队失败）")
        elif n < total:
            self.log_signal.emit(f"部分任务已重新入队（{n}/{total}）")
        else:
            self.log_signal.emit(f"失败任务已重新入队（{n} 条）")

    # ---------------------------------------------------- F7：失败网址清单
    def show_failed_urls(self, tid: int):
        """R154-F7：某任务的失败网址清单对话框（勾选/单条/全部重试 + 清空）。

        与「重试失败任务」的分工：那个整任务重新入队（重新发现页面）；
        这个只跑清单里的单条 URL（engine.retry_failed_urls →
        _process_image 单 URL 下载+管线），不重新爬页面。
        重试在队列线程的事件循环上跑，这里用 QTimer 轮 Future **不卡界面**。
        """
        from PySide6.QtCore import Qt as _Qt

        dlg = QDialog(self)
        dlg.setWindowTitle(f"失败网址清单 · 任务 #{tid}")
        dlg.resize(680, 460)
        v = QVBoxLayout(dlg)
        hint = QLabel(
            "这里是这个任务里下载失败的网址（跨运行累积，重试成功会自动移除）。\n"
            "勾选后选择「重试勾选」只重跑这几条，用的是任务原有配置"
            "（适配器/代理/限速/AI 全不变），不会重新爬页面。")
        hint.setWordWrap(True)
        v.addWidget(hint)
        lst = QListWidget()
        v.addWidget(lst, 1)
        row = QHBoxLayout()
        b_retry = QPushButton("重试勾选")
        b_all = QPushButton("全部重试")
        b_clear = QPushButton("清空清单")
        b_close = QPushButton("关闭")
        row.addWidget(b_retry)
        row.addWidget(b_all)
        row.addWidget(b_clear)
        row.addStretch(1)
        row.addWidget(b_close)
        v.addLayout(row)

        def reload():
            lst.clear()
            try:
                rows = self.db.list_failed_urls(tid)
            except Exception as e:
                QMessageBox.warning(dlg, "读取失败", str(e))
                return
            if not rows:
                it = QListWidgetItem("（没有失败网址）")
                it.setFlags(_Qt.ItemFlag.NoItemFlags)
                lst.addItem(it)
                return
            for r in rows:
                txt = r["url"]
                err = (r["error"] or "").strip()
                if err:
                    txt += f"\n{err}"
                tries = int(r["tries"] or 1)
                txt += f" · 已试 {tries} 次 · 最近 {r['last_tried'] or ''}"
                it = QListWidgetItem(txt)
                it.setFlags(it.flags() | _Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(_Qt.CheckState.Checked)
                it.setData(_Qt.ItemDataRole.UserRole, r["url"])
                lst.addItem(it)

        def checked_urls():
            out = []
            for i in range(lst.count()):
                it = lst.item(i)
                u = it.data(_Qt.ItemDataRole.UserRole)
                if u and it.checkState() == _Qt.CheckState.Checked:
                    out.append(u)
            return out

        def do_retry(only_checked: bool):
            if not self._queue:
                QMessageBox.information(dlg, "重试", "队列未就绪")
                return
            urls = checked_urls() if only_checked else None
            if only_checked and not urls:
                QMessageBox.information(dlg, "重试", "没有勾选任何网址")
                return
            fut = None
            try:
                fut = self._queue.retry_failed_urls(tid, urls)
            except Exception as e:
                self.log_signal.emit(
                    f"重试失败网址出错: {type(e).__name__}: {e}"[:300])
                return
            if fut is None:
                QMessageBox.information(
                    dlg, "重试",
                    "这个任务可能正在运行（或队列没起来）。\n"
                    "先停止任务，再重试失败网址。")
                return
            n = len(urls) if urls is not None else len(
                [1 for i in range(lst.count())
                 if lst.item(i).data(_Qt.ItemDataRole.UserRole)])
            b_retry.setEnabled(False)
            b_all.setEnabled(False)
            self.log_signal.emit(f"开始重试失败网址（{n} 条）…")

            def poll():
                if not fut.done():
                    return
                timer.stop()
                b_retry.setEnabled(True)
                b_all.setEnabled(True)
                try:
                    res = fut.result() or {}
                except Exception as e:
                    self.log_signal.emit(
                        f"重试失败网址出错: {type(e).__name__}: {e}"[:300])
                    return
                if not res.get("ok", True):
                    self.log_signal.emit(
                        f"重试没跑成：{res.get('error') or '未知原因'}")
                    return
                self.log_signal.emit(
                    f"失败网址重试完成：成功 {len(res.get('succeeded') or [])} 条，"
                    f"仍失败 {len(res.get('still_failed') or [])} 条"
                    + (f"，过滤跳过 {len(res.get('filtered') or [])} 条"
                       if res.get("filtered") else ""))
                reload()

            timer = QTimer(dlg)
            timer.timeout.connect(poll)
            timer.start(400)

        def do_clear():
            try:
                n = self.db.clear_failed_urls(tid)
            except Exception as e:
                QMessageBox.warning(dlg, "清空失败", str(e))
                return
            self.log_signal.emit(f"已清空失败网址清单（{n} 条）")
            reload()

        b_retry.clicked.connect(lambda: do_retry(True))
        b_all.clicked.connect(lambda: do_retry(False))
        b_clear.clicked.connect(do_clear)
        b_close.clicked.connect(dlg.accept)
        reload()
        dlg.exec()

    # ----------------------------------------------------------- autostart --
    def _refresh_autostart_label(self):
        try:
            from ..autostart import status
            on, _ = status()
        except Exception:
            on = False
        act = getattr(self, "act_autostart", None)
        if act is not None:                      # menu bar is built later
            act.setText(f"开机自启: {'开' if on else '关'}")

    def toggle_autostart(self):
        from ..autostart import status, install, remove
        on, _ = status()
        if on:
            ok, out = remove()
            self.log_signal.emit("已关闭开机自启")
        else:
            ok, out = install()
            self.log_signal.emit("已开启开机自启(登录时启动后台调度)")
        if not ok:
            QMessageBox.warning(self, "操作失败", out[:300])
        self._refresh_autostart_label()

    # ------------------------------------------------- 启动进托盘（qB 对标）--
    def toggle_tray_start(self):
        """开关：下次启动直接进托盘，不弹主窗口。

        为什么要单独一项：开机自启用的是这台机器，人不在跟前时窗口弹出来
        既占屏也没人点。与「关闭 = 缩托盘」配套，才是 qBittorrent 那套
        「后台常驻」的完整用法。

        ⚠ 只影响**下一次启动**；当前这次窗口已经显示了，不做"立刻藏起来"
          （用户点的是设置项，不是"隐藏窗口"，当场消失反而像崩溃）。
        """
        on = bool(self.act_tray_start.isChecked())
        self.cfg.tray_start_minimized = on
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        tray = getattr(self, "tray", None)
        if on and tray is None:
            # 托盘不可用时打开这个开关 = 下次启动既没窗口也没图标，点不出来
            self.act_tray_start.setChecked(False)
            self.cfg.tray_start_minimized = False
            try:
                from ..config import save_config
                save_config(self.cfg)
            except Exception:
                pass
            QMessageBox.warning(
                self, "无法启用",
                "这台机器当前没有可用的系统托盘（任务栏通知区域），"
                "启动时藏起来就再也叫不回来了，所以没有启用。")
            return
        self.log_signal.emit(
            "已开启：下次启动直接进托盘（点托盘图标唤回）" if on
            else "已关闭：下次启动正常显示主窗口")

    def change_icon(self):
        """Pick any image file -> becomes the app icon (window/tray now,
        exe after the next rebuild)."""
        f = self._file_pick("选择图标图片（ico/png/jpg 都可以）",
                            filter_="图片 (*.ico *.png *.jpg *.jpeg *.webp)")
        if not f:
            return
        try:
            from ..resources import apply_custom_icon
            written = apply_custom_icon(f)
        except Exception as e:
            QMessageBox.warning(self, "换图标失败", f"{type(e).__name__}: {e}")
            return
        ic = icon_path()
        if ic.exists():
            self.setWindowIcon(QIcon(str(ic)))
        self.log_signal.emit(f"图标已更新: {written}")
        QMessageBox.information(
            self, "图标已更新",
            "窗口/托盘图标在下次启动程序时生效。\n"
            "exe 文件本身的图标需要重新打包一次（跟我说“重打包”即可）。")

    def create_shortcut(self):
        """Create (or remove) the desktop launcher from inside the app."""
        from .. import shortcut
        from ..resources import _base
        exist, _ = shortcut.launcher_status()
        if exist:
            if QMessageBox.question(
                    self, "桌面快捷方式",
                    "桌面上已有启动器，要删除它吗？") == QMessageBox.StandardButton.Yes:
                ok, out = shortcut.remove_desktop_launcher()
                self.log_signal.emit(f"桌面启动器已删除 ({out})")
            return
        # prefer the running exe; when running from source fall back to python
        import sys as _sys
        target = Path(getattr(_sys, "executable", "")) if getattr(
            _sys, "frozen", False) else None
        if target is None or not Path(target).exists():
            QMessageBox.information(
                self, "桌面快捷方式",
                "请先用打包好的 exe 运行本程序，再点此按钮创建桌面快捷方式。\n"
                "（从源码运行时无法确定要指向哪个 exe）")
            return
        ok, out = shortcut.create_desktop_launcher(target)
        if ok:
            self.log_signal.emit(f"桌面启动器已创建: {out}")
            QMessageBox.information(self, "完成", f"已创建桌面启动器：\n{out}")
        else:
            QMessageBox.warning(self, "失败", out[:300])

    # ------------------------------------------------------------- pickers --
    def pick_output_dir(self):
        start = self.f_out.text() or str(self.cfg.library_dir)
        d = self._dir_pick("选择输出目录", start=start)
        if d:
            self.f_out.setText(d)

    def proxy_dialog(self):
        from .proxy_dialog import ProxyDialog
        dlg = ProxyDialog(self.pool, self)
        if dlg.exec():
            self._reload_proxies()

    def test_proxy(self):
        pid = self.f_proxy.currentData()
        if pid in (None, -1):
            raw = self.f_proxy.currentText()
            if not raw.startswith(("http", "socks")):
                QMessageBox.information(self, "测试代理", "请先选择或填写代理")
                return
            u = urlsplit(raw)
            e = ProxyEntry(name="临时", scheme=u.scheme, host=u.hostname or "",
                           port=u.port or 0, username=u.username or "",
                           password=u.password or "")
        else:
            e = self.pool.get(pid)
        if not e:
            return
        ok, lat, err = self.pool.check(e)
        QMessageBox.information(self, "测试结果",
                                f"{'可用' if ok else '不可用'}\n延迟 {lat} ms\n{err}")

    # ------------------------------------------------------------ gallery ---
    def _gallery_task_id(self):
        """图库当前筛选的任务 id（下拉选择），None=全部任务。"""
        combo = getattr(self, "g_task_combo", None)
        return combo.currentData() if combo is not None else None

    def gallery_refresh(self):
        """图库「刷新」按钮：重取数据 + 重铺标签筛选下拉。

        F17：加/删标签后新标签要能立刻在下拉里选到，所以刷新不能只调
        load_gallery（那样下拉还是旧清单）。置脏后由 load_gallery 里的
        懒铺逻辑重铺。
        """
        self._gal_tag_combo_filled = False
        self.load_gallery()

    def _fill_gallery_tag_combo(self):
        """F17：按出现次数铺满标签筛选下拉（保留当前选中）。

        统计**全体** images 的 tags（隔离/拉黑的图也可能带标签，用户搜
        quar/black 时用得上）。取出现最多的前 40 个，次数相同按标签名排，
        顺序稳定不跳。含空格的标签跳过 —— tag: 语法按空格切 token，
        带空格的标签拼进查询会被切成两个词（筛错东西），要筛它请直接
        在搜索框敲（搜索框走的是 LIKE，不切 token）。
        """
        combo = getattr(self, "g_tag_filter", None)
        if combo is None:
            return
        cur = combo.currentData()
        try:
            rows = self.db.query(
                "SELECT tags FROM images "
                "WHERE tags IS NOT NULL AND tags NOT IN ('', '[]') "
                "LIMIT 5000")
        except Exception:
            rows = []
        # R228：统计口径下沉到 ``tags_util.count_tags``（Web 的标签筛选
        # 下拉走同一个函数），这里不再自己数一遍。
        from ..tags_util import count_tags
        top = count_tags(rows or (), 40)
        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("全部标签", None)
            for t, n in top:
                combo.addItem(f"{t} ({n})", t)
            # 保留原选中（clear 后 index 全变，按 userData 找回；
            # 找不到=该标签已被删完 → 回「全部标签」）
            if cur is not None:
                for i in range(combo.count()):
                    if combo.itemData(i) == cur:
                        combo.setCurrentIndex(i)
                        break
                else:
                    combo.setCurrentIndex(0)
        finally:
            combo.blockSignals(False)
        self._gal_tag_combo_filled = True

    def _is_structured_query(self, q: str) -> bool:
        """是否包含 #63 高级搜索语法（tag:/site:/score>/w>/h>/fav/quar/black/-排除）。

        纯裸词（无上述前缀）走原有的子串匹配，保持既有习惯。
        """
        from ..search import parse_query
        c = parse_query(q or "")
        return bool(
            c["tags_include"] or c["tags_exclude"] or c["site"]
            or c["score_min"] is not None or c["score_max"] is not None
            or c["w_min"] or c["h_min"] or c["fav"] or c["black"]
            or c["quar"] or c["review"])

    # 注意：这里**不能**有 @staticmethod —— 上一段的 @staticmethod 属于下面
    # 的 _gal_sort_key(r, order)。插入代码时若把装饰器留在原地，新方法会被
    # 变成静态方法、不再接收 self，调用直接 TypeError: missing 1 argument。
    def _toggle_gallery_sort(self, key: str):
        """需求 5：点排序按钮。点**同一个**按钮 = 切正序/倒序；点别的 = 换排序键。

        这是「重复选择时改为倒序」的标准做法：单选按钮组天生 can't 重复点击，
        所以用可 check 的 QPushButton 自己管状态，并把方向画在按钮文字上
        （↓ = 倒序，↑ = 正序），用户不用猜。
        """
        if getattr(self, "_gal_sort_mode", None) == key:
            self._gal_sort_desc = not bool(getattr(self, "_gal_sort_desc", True))
        else:
            self._gal_sort_mode = key
            # 换键时给一个符合直觉的默认方向：站点/分辨率升序更像人读，
            # AI 分和最新默认倒序（新的/高的在前）。
            self._gal_sort_desc = key in ("new", "score")
        self._gal_sig = None        # 强制重绘（签名里已含方向，但保险起见）
        self.load_gallery()

    # ---- D8：表头点击接管取数（r41）----------------------------------------
    # 表头逻辑列号 → 排序键。ID 列与「入库时间」列都落到 id 上
    # （`_gal_sort_key` 里 new 就是按 id），所以点哪一列都等于按入库时间排。
    _GAL_HEADER_SORT = {0: "new", 4: "score", 6: "site", 7: "new"}

    def _on_gallery_header_sort(self, col, direction):
        """图库表头被点：把列/方向写回主窗口排序状态，然后**重新取数**。

        以前点表头只是 make_table 内部 `_refill` 内存重排，一翻页或换筛选就回弹
        到默认顺序；现在统一由这里回写 `_gal_sort_mode/_gal_sort_desc`，
        `load_gallery` 跟着重新取数，排序能跨刷新保持。`direction` 为 None
        表示同一列点了第三次（回到默认排序）。
        """
        if direction is None:
            self._gal_sort_mode = "new"
            self._gal_sort_desc = True
        else:
            key = self._GAL_HEADER_SORT.get(col)
            if key is None:               # 这列不参与排序（文件名/状态/任务…）
                return
            self._gal_sort_mode = key
            self._gal_sort_desc = (direction == "desc")
        self._gal_sig = None
        self.load_gallery()

    def _on_gallery_res_box(self):
        """D7：下拉下段（排序项）被选中 —— 只改排序键/方向，筛选段照常走 load_gallery。"""
        data = self.g_res.currentData() if hasattr(self, "g_res") else None
        if not data or data[0] != "s":
            return
        _, spec = data
        key, _sep, d = spec.partition(":")
        self._gal_sort_mode = key
        self._gal_sort_desc = (d == "desc")
        self._gal_sig = None
        self.load_gallery()

    @staticmethod
    def _gal_sort_key(r: dict, order: str):
        """图库 Python 侧排序键（R152-A2：必须带 @staticmethod）。

        这里曾经缺装饰器：``self._gal_sort_key(r, srt)`` 把 self 当成第一个
        位置参数传进来，``r.get(...)`` 在 MainWindow 实例上 AttributeError，
        被 load_gallery 的 ``except`` 吞成「读取失败」—— 切排序键/高级搜索
        路径整个图库加载不出来。调用方在 :8926（结构化搜索排序）。
        """
        if order == "score":
            return r.get("score") or 0
        if order == "res":
            return (r.get("width") or 0) * (r.get("height") or 0)
        if order == "site":
            return r.get("source_url") or ""
        return r.get("id") or 0

    def _sync_task_combos(self):
        """左侧任务列表变化后，同步图库/审核的任务下拉框选项。"""
        try:
            tasks = list_tasks(self.db)
        except Exception:
            return
        for combo in (getattr(self, "g_task_combo", None),
                      getattr(self._review_tab, "task_combo", None)
                      if hasattr(self, "_review_tab") else None):
            if combo is None:
                continue
            cur = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("全部任务", None)
            for t in tasks:
                combo.addItem(f"{t.name}", t.id)
            if cur is not None:
                idx = combo.findData(cur)
                combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    # --------------------------- #86 图库方向/分辨率筛选辅助 ----
    def _gallery_orient_res_where(self, orient: str, res: str):
        """图库方向/分辨率筛选 → (sql_fragment, params)。

        2026-10-27：SQL 实现已挪到 ``search.orient_res_where``（Web 端
        也要用同一套口径）。这里只做转发 —— 两份 SQL 早晚会漂成
        「桌面端横图能筛出来、网页端筛不出来」。
        """
        from ..search import orient_res_where

        return orient_res_where(orient, res), []

    def _row_passes_orient_res(self, r, orient: str, res: str) -> bool:
        """Python 侧判断单行是否通过方向/分辨率筛选（用于高级搜索结果）。"""
        w = r.get("width") or 0
        h = r.get("height") or 0
        if w <= 0 or h <= 0:
            return False  # 无尺寸信息不参与具体筛选（只在『全部』下出现）
        if orient == "land" and not (w > h):
            return False
        if orient == "port" and not (h > w):
            return False
        if orient == "square" and not (w == h):
            return False
        m = max(w, h)
        if res == "s" and not (m < 1280):
            return False
        if res == "m" and not (1280 <= m < 1920):
            return False
        if res == "l" and not (1920 <= m < 2560):
            return False
        if res == "xl" and not (m >= 2560):
            return False
        return True

    def load_gallery(self):
        if getattr(self, "_closing", False):
            return
        q = self.g_search.text().strip()
        ql = q.lower()
        # F17：标签筛选下拉选中一项 = 往查询串拼一个 ` tag:xxx`，
        # 与用户手敲 tag:白丝 走**同一条**结构化搜索路径（互通，不双轨）。
        tagf = (self.g_tag_filter.currentData()
                if hasattr(self, "g_tag_filter") else None)
        q_eff = f"{q} tag:{tagf}" if tagf else q
        # F17：标签下拉懒铺（首次进图库 / 刷新时重铺，见 gallery_refresh）
        if hasattr(self, "g_tag_filter") and not getattr(
                self, "_gal_tag_combo_filled", False):
            self._fill_gallery_tag_combo()
        tid = self._gallery_task_id()
        flt = self.g_filter.currentData() if hasattr(self, "g_filter") else "all"
        # 需求 5：排序值来自按钮组（key + 方向），不再读下拉框
        srt = getattr(self, "_gal_sort_mode", "new")
        srt_desc = bool(getattr(self, "_gal_sort_desc", True))
        orient = self.g_orient.currentData() if hasattr(self, "g_orient") else "all"
        _rd = self.g_res.currentData() if hasattr(self, "g_res") else None
        # D7：下拉下段是排序项，这时它不能当分辨率筛选用（否则排序一选就没图了）
        res = _rd[1] if (_rd and _rd[0] == "f") else "all"
        # 性能：签名比对（数量+最新id+搜索词+任务+筛选+排序），没变化就不重绘；
        # 翻页时只改 page，base_sig 不变就不重置页码。
        PAGE = 200
        structured = self._is_structured_query(q_eff)
        try:
            if structured:
                # #63 高级搜索语法：tag:/site:/score>/w>/h>/fav/quar/black/-排除
                from ..search import search_images
                allrows = search_images(self.db, q_eff)
                if tid is not None:
                    allrows = [r for r in allrows if r.get("task_id") == tid]
                if flt == "fav":
                    allrows = [r for r in allrows if r.get("favorite")]
                if flt == "unseen":
                    allrows = [r for r in allrows if not r.get("viewed")]
                if flt != "missing" and (orient != "all" or res != "all"):
                    allrows = [r for r in allrows
                               if self._row_passes_orient_res(r, orient, res)]
                # 需求 5：方向由用户点的次数决定，不再写死「score/res 才倒序」。
                # 「最新」也允许倒序（=最老的在前），这才是完整的排序能力。
                allrows.sort(key=lambda r: self._gal_sort_key(r, srt),
                             reverse=srt_desc)
                total = len(allrows)
                mx = max((r.get("id") or 0) for r in allrows) if allrows else 0
                base_sig = (total, mx, q_eff, tid, flt, srt, srt_desc,
                             orient, res, 1)
            else:
                cond = ("WHERE path IS NOT NULL AND blacklisted=0 AND in_quarantine=0"
                        + (" AND task_id=?" if tid is not None else "")
                        + (" AND favorite=1" if flt == "fav" else "")
                        + (" AND viewed=0" if flt == "unseen" else ""))
                # 方向/分辨率筛选（『只看缺失』模式下不限制，缺失项本身没尺寸）
                if flt != "missing":
                    extra, _ = self._gallery_orient_res_where(orient, res)
                    cond += extra
                agg = self.db.query(
                    f"SELECT COUNT(*) AS c, MAX(id) AS m FROM images {cond}",
                    [tid] if tid is not None else [])
                total = agg[0]["c"]
                mx = agg[0]["m"] or 0
                base_sig = (total, mx, q_eff, tid, flt, srt, srt_desc,
                             orient, res, 0)
        except Exception as e:
            self.g_info.setText(f"读取失败: {e}")
            return
        if base_sig != getattr(self, "_gal_base_sig", None):
            self._gal_page = 0
            self._gal_base_sig = base_sig
        pages = max(1, (total + PAGE - 1) // PAGE)
        if self._gal_page >= pages:
            self._gal_page = pages - 1
        if self._gal_page < 0:
            self._gal_page = 0
        # #89 缓存签名必须带上 g_list_mode：否则切换「列表/网格」时签名不变 →
        # load_gallery 提前 return → 可见性永远不切（点了列表模式仍是缩略图网格）。
        sig = (base_sig, self._gal_page, self.g_list_mode)
        if sig == getattr(self, "_gal_sig", None):
            return
        self._gal_sig = sig
        # R147/R151：reload 前记住当前选中锚点（操作后选中"下一张"而非跳回开头）。
        # 列表模式记表格当前行；网格模式记当前 item 在网格里的序号。
        _gal_anchor_override = getattr(self, "_gal_anchor_override", None)
        _gal_anchor_list = None
        _gal_anchor_grid = None
        if _gal_anchor_override == "last":
            _gal_anchor_grid = -1        # 哨兵：本页删空回退上一页时选末张
        elif getattr(self, "g_list_mode", False):
            _r = self.g_table.currentRow()
            _gal_anchor_list = _r if _r >= 0 else None
        else:
            _gi = self.g_grid.currentItem()
            if _gi is not None:
                _gal_anchor_grid = self.g_grid.row(_gi)
        if structured:
            rows = allrows[self._gal_page * PAGE:(self._gal_page + 1) * PAGE]
        else:
            # 需求 5：每个排序键都给 ASC/DESC 两套，方向跟着按钮点击次数走。
            # L1：从「LIMIT .. OFFSET 页码*200」改成 keyset 游标 ——
            # OFFSET 要求 SQLite 先产出并丢弃前 m 行，耗时随页码线性增长；
            # 游标只从「上一页末行之后」开始取，第 1000 页与第 1 页同量级。
            try:
                rows, _nc, _more = self._gal_page_query(
                    PAGE, cursor=self._gal_cursor_for_page(
                        self._gal_page, srt=srt, desc=srt_desc, tid=tid,
                        flt=flt, orient=orient, res=res),
                    backward=False, sort=self._gal_keyset_sort(srt),
                    desc=srt_desc, tid=tid, flt=flt, orient=orient, res=res)
            except Exception as e:
                self.g_info.setText(f"读取失败: {e}")
                return
        self._stop_thumb_pump()
        # 批量重建网格前先掐掉更新：200 个 item 一个一个 addItem 会把 Qt 的
        # 布局/重绘触发 200 次（离屏实测 200 个格子要 800ms+），全批锁住再
        # 一次性刷新就只剩几十毫秒。异常一律吞——绝不能因为没刷新成功就不显示。
        _grid_updates = True
        try:
            self.g_grid.setUpdatesEnabled(False)
        except Exception:
            _grid_updates = False
        self.g_grid.clear()
        self._gal_rows = [dict(r) for r in rows]
        shown = 0
        missing_total = 0
        # ---- 列表模式：与旧版隔离区同款表格（ID/文件名/状态/大小/AI分/任务/网站）----
        if self.g_list_mode:
            self.g_grid.setVisible(False)
            self.g_split.setVisible(True)
            from .table_view import fill_table
            tbl_rows = []
            for r in self._gal_rows:
                blob = f"{r.get('tags')} {r.get('source_url')} {r.get('path')}".lower()
                if (not structured) and ql and ql not in blob:
                    continue
                if flt == "missing" and Path(r["path"]).exists():
                    continue
                tbl_rows.append(r)
            shown, missing_total = fill_table(
                self.g_table, tbl_rows, self.db,
                get_task_fn=lambda t_: get_task(self.db, t_) if t_ else None,
                domain_fn=domain_of,
                select_row=_gal_anchor_list)
            self.g_info.setText(
                f"第 {self._gal_page+1}/{pages} 页 · 共 {shown} 张"
                + (f"（其中 {missing_total} 张磁盘文件已缺失，点『清理缺失』清除残留记录）"
                   if missing_total else ""))
            self.g_pageno.setText(f"第 {self._gal_page+1}/{pages} 页（共 {total} 张）")
            self.g_prev.setEnabled(self._gal_page > 0)
            self.g_next.setEnabled(self._gal_page < pages - 1)
            if _grid_updates:
                try:
                    self.g_grid.setUpdatesEnabled(True)
                except Exception:
                    pass
            return
        self.g_split.setVisible(False)
        self.g_grid.setVisible(True)
        icon_sz = self.g_grid.iconSize().width()
        # 缩略图缓存分辨率取配置 thumbnail_size（#75）：比显示网格更大更清晰，
        # 放大查看/不同列数下都不糊；改配置后旧缓存按 key 不同自动失效。
        thumb_sz = int(getattr(self.cfg, "thumbnail_size", 256)) or 256
        thumbs = self._thumb_dir()
        # r37：缩略图改成「分帧异步补」。以前是这个 for 循环里同步一张张
        # 解码原图，而 thumbs 缓存是空的（缓存目录 0 个文件）—— 200 张
        # webp/avif 全靠 Pillow 现解，主线程一卡就是几十秒，翻页看起来就是
        # 「点了没反应」，900 张更是直接假死。现在先把 200 个格子画出来
        # （瞬间），缩略图交给 _pump_thumbs 每帧补一点，翻页永远立刻有反应。
        self._thumb_pending = []
        for r in self._gal_rows:
            blob = f"{r['tags']} {r['source_url']} {r['path']}".lower()
            if (not structured) and ql and ql not in blob:
                continue
            p = Path(r["path"])
            exists = p.exists()
            if flt == "missing" and exists:
                continue
            if not exists:
                # 文件已在磁盘删除/移动，但图库还记着 → 标灰显示，等用户清理
                missing_total += 1
                item = QListWidgetItem()
                item.setText("缺失 · " + (r["path"] or r["source_url"]))
                # P1-4：「缺失」标记色从调色板取（原来写死 #9aa0ab，
                # 深色下偏亮、浅色下偏暗，两边都不贴合）。
                item.setForeground(QColor(_tc(
                    bool(getattr(self.cfg, "dark_mode", False)),
                    "text_dim", "#9aa0ab")))
                item.setToolTip("磁盘上已无此文件，可执行『清理缺失』删除该记录")
                item.setData(Qt.ItemDataRole.UserRole, r["id"])
                item.setData(Qt.ItemDataRole.UserRole + 1, "missing")
                self.g_grid.addItem(item)
                shown += 1
                continue
            item = QListWidgetItem()
            sha = r["sha256"] if "sha256" in r.keys() else ""
            # r37（#141 收尾）：**就算磁盘缓存命中，也绝不在这一帧同步解码**。
            # 以前是这个分支里 `命中就当场 QPixmap(缓存小图)`，实测 200 张
            # 读盘+解码 984ms 全压在同一帧 —— 「点下一页就卡住」「900 张卡的不
            # 行」都是它。现在一律入队交给 _pump_thumbs 分帧补：翻页只剩建
            # 格子（离屏实测 ~45ms），图一张张冒出来；命中内存 LRU 的那批是
            # 微秒级，pump 第一帧就能捞完，看起来跟同步一样。
            self._thumb_pending.append((item, p, sha))
            exif = ""
            if r.get("fmt"):
                exif += f"格式 {r['fmt']}"
            if r.get("taken_at"):
                exif += (f" · 拍摄 {r['taken_at']}" if exif else f"拍摄 {r['taken_at']}")
            w_, h_ = r.get("width"), r.get("height")
            dim = (f"{w_}x{h_}" if (w_ and h_) else "尺寸未知")
            item.setText(f"{'★' if r['favorite'] else ''}{dim}"
                         + (f" · {r['fmt']}" if r.get("fmt") else ""))
            item.setToolTip(f"{p}\n{r['source_url']}\ntags={r['tags']}"
                            f"\nscore={r['score']}"
                            + (f"\n{exif}" if exif else ""))
            item.setData(Qt.ItemDataRole.UserRole, r["id"])
            self.g_grid.addItem(item)
            shown += 1
        if missing_total:
            self.g_info.setText(
                f"第 {self._gal_page+1}/{pages} 页 · 共 {shown} 张（其中 {missing_total} 张磁盘文件已缺失，"
                f"点『清理缺失』清除残留记录）")
        else:
            self.g_info.setText(f"第 {self._gal_page+1}/{pages} 页 · 共 {shown} 张")
        self.g_pageno.setText(f"第 {self._gal_page+1}/{pages} 页（共 {total} 张）")
        self.g_prev.setEnabled(self._gal_page > 0)
        self.g_next.setEnabled(self._gal_page < pages - 1)
        if _grid_updates:
            try:
                self.g_grid.setUpdatesEnabled(True)
            except Exception:
                pass
        # R151：网格模式 reload 后选中"下一张"（与列表模式同语义：删第 i 张→选第 i 张=下一张）
        if not getattr(self, "g_list_mode", False):
            _gn = self.g_grid.count()
            if _gn == 0 and self._gal_page > 0 and _gal_anchor_grid is not None:
                # 本页被删空且存在上一页 → 回退一页并选末张
                self._gal_page -= 1
                self._gal_anchor_override = "last"
                self._gal_sig = None
                self.load_gallery()
                self._gal_anchor_override = None
                return
            if _gal_anchor_grid is not None and _gn > 0:
                _ai = (_gn - 1) if _gal_anchor_grid == -1 \
                    else min(_gal_anchor_grid, _gn - 1)
                if _ai >= 0:
                    _it = self.g_grid.item(_ai)
                    if _it is not None:
                        self.g_grid.setCurrentItem(_it)
                        self.g_grid.scrollToItem(_it)
        # 缩略图回填（没缓存的那些）：分帧慢慢补，别阻塞当前这一帧
        if getattr(self, "_thumb_pending", None):
            self._start_thumb_pump()

    # ---------------- 缩略图分帧回填（r37，图库不卡的关键）----------------
    # 背景：thumbs 缓存目录一开始是空的，200 张图全靠 Pillow 现解（avif
    # 单张 30~80ms），同步循环 = 主线程冻结十几秒。改成每帧最多干 25ms，
    # 剩下的下一帧接着干 —— 选择「下一页」立刻出格子，图一张张冒出来。
    _THUMB_BUDGET_MS = 25
    _THUMB_WORKER_N = 4          # 后台解码线程数（本机 10 核，4 张不至于抢 UI）
    _THUMB_INFLIGHT_MAX = 600    # 最多允许 600 张在飞，再多就退回主线程同步解

    def _start_thumb_pump(self):
        # 先掐掉**上一个**定时器，但别把待办队列也清空了 —— 队列是本轮新
        # 攒出来的（以前这里一清，待办队列直接变空，分帧补图等于没做）。
        self._stop_thumb_pump(clear_pending=False)
        try:
            t = QTimer(self)
            t.setInterval(16)
            t.timeout.connect(self._pump_thumbs)
            self._thumb_timer = t
            t.start()
        except Exception:
            self._thumb_timer = None

    def _stop_thumb_pump(self, clear_pending: bool = True):
        t = getattr(self, "_thumb_timer", None)
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
            try:
                t.setParent(None)
            except Exception:
                pass
        self._thumb_timer = None
        if clear_pending:
            self._thumb_pending = []
            self._thumb_retry = set()
            # in-flight 的后台解码结果如果回来时页面已经换掉，token 查不到就
            # 自动丢（见 _on_thumb_ready），但槽位本身要腾掉，别一直涨。
            try:
                if getattr(self, "_thumb_slots", None) is not None:
                    self._thumb_slots = {}
            except Exception:
                pass

    def _pump_thumbs(self):
        """一帧最多干 `_THUMB_BUDGET_MS`，剩下的下一帧接着来。

        三级取图（顺序即优先级）：
          ① 内存 LRU → 微秒级，直接 setIcon；
          ② 磁盘缓存小图 → 5ms/张，还在本帧预算内就顺手同步做完；
          ③ 原图没缓存 → 丢给线程池（avif 单张 30~80ms，绝不能占主线程），
             结果由 `_on_thumb_ready` 回到主线程 setIcon。
        """
        pend = getattr(self, "_thumb_pending", None)
        if not pend or getattr(self, "_closing", False):
            # **不能 clear_pending**：这一支是「本帧没活干了」，此时后台往往还
            # 压着一批在飞的任务，槽位一清，结果回来就查不到 token → 那些格子
            # 永远空着（就是这个 bug 让后台解码在测试里「看起来没生效」）。
            self._stop_thumb_pump(clear_pending=False)
            return
        thumb_sz = int(getattr(self.cfg, "thumbnail_size", 256)) or 256
        thumbs = self._thumb_dir()
        t0 = time.perf_counter()
        while pend:
            item, p, sha = pend[0]
            pend.pop(0)
            digest = self._thumb_digest(sha, p)
            if not digest:
                try:
                    item.setIcon(QIcon())
                except Exception:
                    pass
                continue
            mkey = self._thumb_mem_path(digest, thumb_sz)
            pm = self._thumb_mem_get(mkey)
            if pm is None:
                try:
                    if self._thumb_cache_hit(p, sha, thumb_sz, thumbs):
                        pm = self._thumb_pixmap(p, sha, thumb_sz, thumbs)
                except Exception:
                    pm = None
            if pm is None:
                self._thumb_dispatch(p, digest, sha, thumb_sz, thumbs, item, mkey)
            else:
                try:
                    item.setIcon(QIcon(pm))
                except Exception:
                    pass
            if (time.perf_counter() - t0) * 1000.0 >= self._THUMB_BUDGET_MS:
                break
        if not pend:
            # 队列空了只停定时器，**别把在飞的槽位清掉**：这时后台线程往往还
            # 在解最后一批，槽位一清，结果回来就查不到 token（_on_thumb_ready
            # 直接丢）→ 这些格子永远空着。换页/关窗口那两处才该清（见
            # _stop_thumb_pump(clear_pending=True) 的调用点）。
            self._stop_thumb_pump(clear_pending=False)

    def _thumb_dispatch(self, p, digest, sha, size, thumbs, item, mkey):
        """原图没缓存 → 丢线程池解（本函数本身只花几十微秒）。"""
        try:
            # 【R117 P2-7】写 _fit（等比完整版），与同步路径
            # table_view.square_thumb_pixmap(crop=False) 同名规则。
            # R247：输出路径必须走分桶（否则异步解出来的又堆回 thumbs/ 根目录）
            from .. import thumbpath as _tp1
            _cf1 = _tp1.cache_path(thumbs, f"{digest}_fit{int(size)}.png")
            _cf1.parent.mkdir(parents=True, exist_ok=True)
            cf = str(_cf1)
            sig = getattr(self, "_thumb_sig", None)
            pool = getattr(self, "_thumb_pool", None)
            slots = getattr(self, "_thumb_slots", None)
            if sig is None or pool is None or slots is None:
                raise RuntimeError("线程池未就绪")
            if len(slots) >= self._THUMB_INFLIGHT_MAX:
                raise RuntimeError("排队任务过多，改同步解码")   # 退回下面同步解
            token = next(self._thumb_tokens)
            slots[token] = (item, mkey, str(p), sha)
            pool.start(_ThumbDecodeRunnable(sig, str(p), token, cf, size))
            return
        except Exception:
            pass
        # 线程池不可用 / 排队爆了 → 本帧同步解一张（有预算兜着，不会死循环）
        try:
            pm = self._thumb_pixmap(p, sha, size, thumbs)
            if pm is not None and not pm.isNull():
                self._thumb_mem_put(mkey, pm)
                item.setIcon(QIcon(pm))
        except Exception:
            pass

    def thumb_request_async(self, p, digest, size, thumbs, item, mkey, sha=""):
        """**通用**异步缩略图请求（图库 / 审核 / 隔离区共用一条线程池）。

        r39：审核网格原来是在 Qt 主线程里同步 `square_thumb_pixmap` 一张张解
        原图，几百张 avif/webp（单张 30~80ms，大的几秒）直接把事件循环堵死。
        这里是 `_thumb_dispatch` 的通用版，差别只在调用方自己给 token 对应的
        item（审核页的 QListWidgetItem），结果仍由 `_on_thumb_ready` 回主线程
        setIcon。解完顺手写盘，下次进这一页就是命中缓存（<1ms）。
        """
        try:
            sig = getattr(self, "_thumb_sig", None)
            pool = getattr(self, "_thumb_pool", None)
            slots = getattr(self, "_thumb_slots", None)
            if sig is None or pool is None or slots is None:
                raise RuntimeError("线程池未就绪")
            if len(slots) >= self._THUMB_INFLIGHT_MAX:
                raise RuntimeError("排队任务过多，改同步解码")
            token = next(self._thumb_tokens)
            slots[token] = (item, mkey, str(p), sha)
            from .. import thumbpath as _tp2
            _cf2 = _tp2.cache_path(thumbs, f"{digest}_fit{int(size)}.png")
            _cf2.parent.mkdir(parents=True, exist_ok=True)
            out_png = str(_cf2)
            pool.start(_ThumbDecodeRunnable(sig, str(p), token, out_png,
                                            int(size), crop=False))
            return True
        except Exception:
            pass
        # 线程池不可用 / 排队爆了 → 本帧同步解一张（有预算兜着，不会死循环）
        try:
            pm = self._thumb_pixmap(p, sha, int(size), thumbs)
            if pm is not None and not pm.isNull():
                try:
                    self._thumb_mem_put(mkey, pm)
                    item.setIcon(QIcon(pm))
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def thumb_slots_reset(self):
        """重建列表前丢掉在飞的槽位 —— 否则结果回来时 item 早被 clear() 掉了，
        setIcon 打到已释放的 C++ 对象上（PySide6 抛 Internal C++ object 已经
        deleted，但宁可先丢也别赌）。只丢「结果」，线程池与内存缓存都留着。
        """
        try:
            if getattr(self, "_thumb_slots", None) is not None:
                self._thumb_slots = {}
        except Exception:
            pass
        try:
            if getattr(self, "_thumb_pending", None) is not None:
                self._thumb_pending = []
        except Exception:
            pass

    def _on_thumb_ready(self, token, data):
        """后台解码回到主线程：装图 + 进内存 LRU。token 过期就丢弃。"""
        slots = getattr(self, "_thumb_slots", None)
        if not slots:
            return
        slot = slots.pop(token, None)
        if slot is None:
            return
        item, mkey, _p, sha = slot
        # r39c：退出阶段 item 可能已经销毁，setData 到一个悬空 QStandardItem
        # 会让整个进程变成 0xC0000409（现场就死在这）。先判活。
        if not self._alive(item):
            return
        try:
            pm = QPixmap()
            if data:
                pm.loadFromData(data)
            if pm.isNull():
                # 解码失败（文件坏/格式不支持）→ 放回待办重试一次。
                # **必须只重试一次**：同一张坏图每次失败都重新入队的话，
                # pump 会「失败→入队→失败」无限转，一帧一张永远退不出来。
                _rk = id(item)
                if _rk not in self._thumb_retry:
                    self._thumb_retry.add(_rk)
                    self._thumb_pending.append((item, Path(_p), sha))
                    self._start_thumb_pump()
                return
            self._thumb_mem_put(mkey, pm)
            item.setIcon(QIcon(pm))
        except Exception:
            pass

    @staticmethod
    def _gal_keyset_sort(srt: str) -> str:
        """排序模式 → keyset 排序键。白名单外的值一律当 id。

        排序键要拼进 SQL 的 ORDER BY 和游标比较里，**绝不能直接用原始输入**。

        ⚠ 名字别叫 ``_gal_sort_key``：那儿已经有一个同名的
        ``_gal_sort_key(r, order)``（结构化搜索的 Python 排序键，两参数），
        同名会**互相覆盖** —— 后定义的方法赢，结构化搜索会直接崩。
        """
        from ..repo import _SORT_EXPR  # noqa: PLC0415  白名单，避免 SQL 注入面
        return srt if srt in _SORT_EXPR else "id"

    def _gal_cursor_for_page(self, page: int, *, srt, desc, tid, flt, orient, res):
        """返回打开第 ``page`` 页该用的游标（首页是 None）。

        维护一个游标栈 ``_gal_cursors``：``[None, c1, c2, ...]``，
        下标 = 页码，值 = 打开该页的游标。**「上一页」直接读栈里已存的那个**，
        不需要反向 SQL —— 反向要额外一套比较运算符，两个方向两个出错点，
        收益只是省一次查栈，不划算（作业单允许的「缓存窗口」方案）。

        跳页（页码远超栈深）时逐页回填：每页一次「只取 limit+1 行」的轻查询，
        仍然**不随目标页码线性变慢**，因为每次都从游标处接着走。
        """
        key = (self._gal_keyset_sort(srt), bool(desc), tid, flt, orient, res)
        if getattr(self, "_gal_cursor_key", None) != key:
            # 换了筛选/排序 → 栈里存的是别的条件下的游标，全部作废。
            # 注意 tid/flt/orient/res 都在 key 里：只按排序键重置是不够的，
            # 换任务筛选后旧游标指向的行已经不是同一批数据了。
            self._gal_cursors = [None]
            self._gal_cursor_key = key
        stack = self._gal_cursors
        while len(stack) <= page:
            prev = stack[-1]
            try:
                _rows, nxt, _more = self._gal_page_query(
                    200, cursor=prev, backward=False,
                    sort=key[0], desc=key[1], tid=tid, flt=flt,
                    orient=orient, res=res)
            except Exception:
                nxt = None
            if nxt is None:
                # 到头了：把栈补齐到目标页（都填 None），让调用方 clamp 页码。
                while len(stack) <= page:
                    stack.append(None)
                break
            stack.append(nxt)
        return stack[page]

    def _gal_page_query(self, limit, *, cursor, backward, sort, desc,
                           tid, flt, orient, res):
        """L1：图库列表的单页 keyset 取数（WHERE 拼装集中在这里）。

        抽出来的原因：回填游标栈的调用**不需要行数据、只要 next_cursor**，
        而渲染用的调用两个都要。合成一处才能保证两边的 WHERE 完全一致 ——
        条件一旦分叉，症状是「翻页后某页的行数/过滤口径与首页不同」。
        """
        where = "path IS NOT NULL AND blacklisted=0 AND in_quarantine=0"
        params: list = []
        if tid is not None:
            where += " AND task_id=?"
            params.append(tid)
        if flt == "fav":
            where += " AND favorite=1"
        if flt == "unseen":
            where += " AND viewed=0"
        if flt != "missing":
            # 「只看缺失」模式下不限制方向/分辨率：缺失项本身没有尺寸数据。
            extra, _ = self._gallery_orient_res_where(orient, res)
            where += extra
        return list_images_page(
            self.db, where=where, params=params, sort=sort, desc=desc,
            cursor=cursor, limit=limit, backward=backward)

    def _gal_page_move(self, delta: int):
        self._gal_page = max(0, self._gal_page + delta)
        self.load_gallery()

    def _thumb_dir(self):
        """缩略图缓存目录（放用户数据目录，绝不进 output_dir，免得被回填扫到）。

        r31 顺手做一次「旧缓存清算」：改方形裁剪前，缓存文件名是
        ``{sha}_{size}.png``；改完是 ``{sha}_sq{size}.png``。旧的那一批永远
        不会被读到，却一直占着磁盘（一张几十 KB，几百张就是几十 MB）。所以第
        一次进图库时把不带 ``_sq`` 的老文件删掉，只做一次、失败就算了。
        【R117 P2-7】规则改为**带 `_fit` 或 `_sq` 的都算新缓存**：
        _sq 是方块版（内容已错，但同一批用户在升级前生成的要给它一次自动
        重解的机会，不能一上来就全删），真正要清的只剩更老的裸名文件。
        """
        from ..config import user_data_dir
        d = user_data_dir() / "thumbs"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            return d
        if not getattr(self, "_thumbs_legacy_swept", False):
            self._thumbs_legacy_swept = True
            try:
                n = 0
                for f in d.glob("*.png"):
                    # 【R117 P2-7】新缓存是 ``_fit``（等比完整版）；
                    # ``_sq`` 是 P2-7 之前异步路径写的**方块版**，内容已错，
                    # 留着只会被读到 → 当旧缓存清掉（用户下次进来自动重解）。
                    if "_fit" in f.name or "_sq" in f.name:   # 留着
                        continue
                    try:
                        f.unlink()
                    except Exception:
                        pass
                    n += 1
                    if n >= 2000:            # 上限，绝不无限删
                        break
            except Exception:
                pass
        return d

    # ---------------- 缩略图三级缓存（r37 #141 收尾）----------------
    # 背景（真机取证，200 张）：
    #   stat x200           3.5 ms   ← 不算慢
    #   read_bytes x200   201.1 ms   ← 小文件读盘 ~1ms/张（NTFS）
    #   QImage(file) x200  647.2 ms   ← 解码 ~3.2ms/张
    #   QPixmap(file) x200 984.2 ms   ← 读盘+解码总账
    # 也就是说「每次翻页都要把 200 张缓存小图重新读一遍」本身就是 1 秒级的
    # 主线程冻结。所以做成三级：
    #   ① 内存 LRU（命中 = 微秒级，来回翻同一页跟秒开一样）
    #   ② 磁盘缓存小图（命中 = 5ms/张，交给 _pump_thumbs 分帧摊掉）
    #   ③ 解码原图（webp/avif 单张 30~80ms，同样只进 pump）
    # 关键点还在于**判据必须和写缓存的人用同一套文件名**——以前这里算的是
    # sha1(sha1(sha))[:20]，而 square_thumb_pixmap 写的是 sha1(sha)[:20]，
    # 两级 hash 永远对不上，缓存命中从不生效，每次都退回解码原图（avif/webp
    # 一张几十毫秒，200 张十几秒，这就是「900 张卡的不行」的元凶）。
    _THUMB_MEM_MAX = 400

    # R117 第9项：列表模式右侧预览的**节奏**参数（不是契约，别钉死数值）。
    #   防抖窗口：连按箭头时只记最终行，停手后才画一次（连按几十次=1 次解码）
    #   解码上限：预览区最大 560px 宽（_resize_gallery_preview 的 clamp），
    #             按此给解码目标边长（2 倍余量留给高分屏），省掉整张原图解码
    _GAL_PREVIEW_DEBOUNCE_MS = 60
    _GAL_PREVIEW_DECODE_CAP = 1120

    # R117 第6项：点关闭后的**总**等待预算（秒）。
    # 原来各层各等固定的 5+2+5+8+8 = 最坏 28 秒；现在全程共用一个截止时刻。
    # 是「预算」不是「保证」：正常收尾会更快，超时也一定往下走（关库 → 退出）。
    _CLOSE_BUDGET_S = 6.0

    def _thumb_digest(self, sha: str, p) -> str:
        """缓存文件名里那个 20 位摘要。

        规则与 ``table_view.square_thumb_pixmap`` **必须一模一样**：
        ``sha1(sha256)[:20]``，sha256 为空时退化为 ``sha1(文件路径)[:20]``。
        """
        import hashlib
        key = (sha or "").strip()
        if not key:
            key = str(p)
        try:
            return hashlib.sha1(
                key.encode("utf-8", "replace")).hexdigest()[:20]
        except Exception:
            return ""

    @staticmethod
    def _thumb_mem_path(digest: str, size: int, tag: str = "sq") -> str:
        """内存缩略图缓存键。tag 区分裁剪版(sq)/完整版(fit) —— 需求 2 之后
        网格统一用 fit，但实时抓图墙之类还可能用 sq，共用一条会串图。"""
        return f"{digest}:{tag}{size}"

    def _thumb_mem_get(self, mkey: str):
        cache = getattr(self, "_thumb_mem", None)
        if not cache:
            return None
        try:
            pm = cache.get(mkey)
            if pm is None:
                return None
            cache.move_to_end(mkey)
        except Exception:
            return None
        if pm.isNull():
            try:
                cache.pop(mkey, None)
            except Exception:
                pass
            return None
        return pm

    def _thumb_mem_put(self, mkey: str, pm) -> None:
        cache = getattr(self, "_thumb_mem", None)
        if cache is None:
            try:
                from collections import OrderedDict
                cache = self._thumb_mem = OrderedDict()
            except Exception:
                return
        try:
            cache[mkey] = pm
            cache.move_to_end(mkey)
            # 上限保护：一张 256×256 的 QPixmap 约 256KB，400 张 ≈ 100MB。
            # 百万级图库时只留最近翻过的这几百张，绝不能无上限地涨。
            while len(cache) > self._THUMB_MEM_MAX:
                cache.popitem(last=False)
        except Exception:
            pass

    def _thumb_cache_hit(self, p: Path, sha: str, size: int, cache_dir: Path) -> bool:
        """缩略图缓存文件已存在吗？（只 stat，不解码）—— 分帧补图的判据。

        缓存文件名规则见 `_thumb_dir`/`_thumb_pixmap`：
        ``sha1(sha256)[:20]_sq{size}.png``；sha256 为空时退化为按路径取 hash。
        见类注释：这里以前算成了两级 sha1，**和写缓存的人对不上**，命中永远
        不生效。现在统一走 `_thumb_digest`。

        【R117 P2-7】写入端已统一到 ``_fit``（等比完整版），所以这里
        **先看 _fit、再看 _sq**：_sq 是 P2-7 之前异步路径写的方块版。
        ⚠ 保留读 _sq 只为「老缓存别全废」；一旦读到 _sq 就应当**顺手作废**，
        否则用户看到的还是方块（读命中却拿到裁剪图，最难排查）。
        """
        digest = self._thumb_digest(sha, p)
        if not digest:
            return False
        try:
            d = Path(cache_dir)
            # R247：判据必须与写入端走**同一套分桶规则**，否则缩略图进了
            # thumbs/xx/y/ 之后这里还在平铺路径上 stat，命中永远 False ——
            # 等于把 R117 修掉的「每次翻页重新解码原图」又还回去了。
            from .. import thumbpath as _tp
            if _tp.find(d, f"{digest}_fit{int(size)}.png").exists():
                return True
            sq = _tp.find(d, f"{digest}_sq{int(size)}.png")
            if sq.exists():
                # 老方块缓存：删掉让它重解成 fit 版（缓存文件，删了自动再生，
                # 不属于「删用户数据」）。删不掉就当没命中 → 重新解一张。
                try:
                    sq.unlink()
                except Exception:
                    pass
                return False
        except Exception:
            return False
        return False

    def _thumb_pixmap(self, p: Path, sha: str, size: int, cache_dir: Path):
        """缩略图（**方形居中裁剪**）：内存 LRU → 磁盘缓存小图 → 解码原图。

        r31：改方形是为了干掉「缩略图四周一圈白」——等比缩放的竖图塞进方格子
        会左右留白，图越大越明显。方形缓存铺满格子，图库/审核两处一样。
        文件名带 `_sq` 前缀，旧缓存不会被误读。
        """
        digest = self._thumb_digest(sha, p)
        if not digest:
            return None
        # 键里带 fit：需求 2 之后网格一律 crop=False，
        # 内存缓存必须与磁盘缓存同名规则（_fit<size>），否则切换前后
        # 会互相串图，看着像「改了没生效」。
        mkey = self._thumb_mem_path(digest, size, tag="fit")
        pm = self._thumb_mem_get(mkey)
        if pm is not None:
            return pm
        try:
            from .table_view import square_thumb_pixmap
            # 需求 2：图库网格不裁剪，完整显示（crop=False）
            pm = square_thumb_pixmap(p, size, cache_dir, key=(sha or "").strip(),
                                     digest=digest, crop=False)
            if pm is not None:
                self._thumb_mem_put(mkey, pm)
            return pm
        except Exception:
            return None

    def _gallery_selected_ids(self) -> list:
        """当前选中的全部图片 id（支持 Ctrl / Shift 多选；列表模式读表格）。"""
        out = []
        if getattr(self, "g_list_mode", False) and getattr(self, "g_table", None) is not None:
            from .table_view import selected_ids as _sel
            return _sel(self.g_table)
        for it in self.g_grid.selectedItems():
            iid = it.data(Qt.ItemDataRole.UserRole)
            if iid is not None:
                out.append(iid)
        return out

    def gallery_select_all(self):
        if getattr(self, "g_list_mode", False) and getattr(self, "g_table", None) is not None:
            self.g_table.selectAll()
            return
        self.g_grid.selectAll()

    def gallery_batch_like(self):
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(self, "提示",
                                    "先选中图片（可 Ctrl / Shift 多选）")
            return
        n = sum(1 for iid in ids if self._apply_like(iid))
        self.log_signal.emit(f"图库: 批量喜欢 {n} 张")
        self.load_gallery()

    def gallery_batch_delete(self):
        """批量删除选中的图片：按设置决定移入回收站或直接永久删除（不做偏好学习）。

        与「批量不喜欢」的区别：这里只是删图，不弹「哪个主题词未命中」、也不把
        标签写进本地偏好模型。适合「这一批图我就是不想要了」的纯清理场景。
        要批量选中可先选择「全选本页」，或 Ctrl / Shift 手动多选。
        """
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(
                self, "提示",
                "先选中要删除的图片（选择「全选本页」或 Ctrl / Shift 多选）")
            return
        recycle = bool(getattr(self.cfg, "use_recycle", True))
        tip = (f"确定删除这 {len(ids)} 张图片吗？\n（移入回收站，之后可以还原）"
               if recycle else
               f"确定删除这 {len(ids)} 张图片吗？\n（直接从磁盘永久删除，无法恢复）")
        if QMessageBox.question(self, "批量删除", tip) \
                != QMessageBox.StandardButton.Yes:
            return
        n = sum(1 for iid in ids if self._apply_dislike(iid, ask=False))
        self.log_signal.emit(f"图库: 批量删除 {n} 张")
        self.load_gallery()

    def gallery_batch_dislike(self, scope: str = "task"):
        """图库「不喜欢」：与审核页一致——弹「不喜欢原因」对话框，勾选命中的
        AI 关键词（加入排除词）/ 未命中的 AI 黑名单；不勾=单纯删除。支持多选
        （一次弹窗，按任务去重写排除词）。图库已取消「喜欢」，只保留不喜欢。"""
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(self, "提示",
                                    "先选中图片（可 Ctrl / Shift 多选）")
            return
        from ..repo import get_image
        recs = [get_image(self.db, i) for i in ids]
        recs = [r for r in recs if r is not None]
        if not recs:
            return
        labels_union = set()
        black_union = set()
        for r in recs:
            labels_union |= set(self._task_ai_labels(r.task_id))
            black_union |= set(self._task_blacklist(r.task_id))
            black_union |= set(self._task_ai_blacklist(r.task_id))
        recycle = bool(getattr(self.cfg, "use_recycle", True))
        unmatched, exclusions, mode = [], [], "pure"
        if labels_union or black_union:
            from .dislike_dialog import (ask_dislike_reason,
                                         consume_manual_words,
                                         consume_correct_labels)
            hint = ("删除后会"
                    + ("移入回收站（可还原）" if recycle
                       else "从磁盘永久删除（不可恢复）"))
            # 【R117 P1-3】合并多任务时层级会混，交给 dialog 按 tier 分段展示
            _tiers: dict = {}
            for _t in {r.task_id for r in recs}:
                for _k, _v in (self._task_ai_labels_by_tier(_t) or {}).items():
                    _tiers.setdefault(_k, [])
                    for _w in _v:
                        if _w not in _tiers[_k]:
                            _tiers[_k].append(_w)
            unmatched, exclusions, mode = ask_dislike_reason(
                self, sorted(labels_union), sorted(black_union), hint=hint,
                db=self.db, labels_by_tier=_tiers)
            if mode == "cancel":
                return
        # 【硬性规则】左栏「主题词」勾选 = 这张图没匹配上该主题，只做负向偏好
        # 学习；只有右栏「排除词」才允许写进任务 ai_blacklist。
        if exclusions:
            for tid in {r.task_id for r in recs}:
                self._add_exclusions(tid, exclusions)
        if unmatched:
            from ..classify.preference import PreferenceLearner
            for tid in {r.task_id for r in recs}:
                PreferenceLearner(self.db, tid).learn("dislike", unmatched)
        # #64：手填词桥接到词表标签再学（图库这条路同样要闭环）
        manual = consume_manual_words()
        if manual:
            from ..classify.label_bridge import LabelBridge
            from ..classify.preference import PreferenceLearner
            for tid in {r.task_id for r in recs}:
                hits = PreferenceLearner(self.db, tid).learn_manual(
                    "dislike", manual)
            for h in hits or []:
                self.log("词表桥接: " + LabelBridge.hint(h))
        # R174 §4：用户声明的「正确标签」→ **正向**学习信号。
        # ⚠ 走 learn_manual（桥接到词表标签再学）而不是 learn：
        #   这是**用户手写的词**，是本单里"用户意图"的最高证据等级 ——
        #   只有它配走 user_typed（也就是唯一可能进硬黑名单的来源）。
        #   而这里学的是**正向**（"like"），永远不会写黑名单，但来源记对了，
        #   以后用户想把它手工加进排除词时不会被当成模型自动词清掉。
        correct = consume_correct_labels()
        if correct:
            from ..classify.preference import PreferenceLearner
            for tid in {r.task_id for r in recs}:
                PreferenceLearner(self.db, tid).learn_manual("like", correct)
            self.log("误标纠正: 记为正向 " + " ".join(correct))
        n = sum(1 for iid in ids if self._apply_dislike(iid, ask=False, scope=scope))
        self.log_signal.emit(f"图库: 批量不喜欢 {n} 张（"
                             f"{'已加排除词' if exclusions else '单纯删除'}）")
        self.load_gallery()

    def _mark_viewed(self, iid):
        """标记图片为「已看」（图库「只看未看」筛选用）+ 写最后浏览时间。

        #82：时间戳统一由 `DB.mark_viewed` 写，避免「桌面端写了、别的入口没写」
        造成的「最近浏览」排序漏图。
        """
        if iid is None:
            return
        try:
            self.db.mark_viewed(iid)
        except Exception:
            pass

    def _gallery_selected(self):
        if getattr(self, "g_list_mode", False):
            ids = self._gallery_selected_ids()
            return ids[0] if ids else None
        item = self.g_grid.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _select_gallery_by_id(self, iid):
        """在图库里定位并选中指定 id 的图（需求 30：结果窗双击回图库）。

        网格模式走 currentItem，列表模式走表格选中行 —— 两边都要能用，
        否则「双击结果 → 跳回图库高亮它」在一种模式下是死的。
        """
        if iid is None:
            return False
        if getattr(self, "g_list_mode", False):
            tbl = getattr(self, "g_table", None)
            if tbl is None:
                return False
            from .table_view import row_of_id as _rid
            r = _rid(tbl, iid)
            if r is None:
                return False
            tbl.selectRow(r)
            return True
        for it in self.g_grid.items():
            if it.data(Qt.ItemDataRole.UserRole) == iid:
                self.g_grid.setCurrentItem(it)
                self.g_grid.scrollToItem(it)
                return True
        return False

    def _gallery_table_preview(self):
        """列表模式右侧预览：跟随表格选中行。

        R117 第9项（用户实测：图库列表模式按几下箭头就整个卡死变白屏，
        刷新/切网格/切审核全都不管用）—— 根因是**这里在主线程同步做重活**：
          ① `db.query(...)`：DB 被后台线程写锁住时，busy_timeout=10s，
             这一行就能把 GUI 线程堵死十秒（用户看到的就是「卡死」）；
          ② `QPixmap(str(p))`：**整张原图**解码（4000×3000 就是几十 MB），
             再 `scaled(...)` 又是一趟全图缩放。
        而它挂在 `itemSelectionChanged` 上 —— **每按一次箭头就跑一遍**。
        连按几十次 = 几十次「同步解码 + 同步查询」全堆在 GUI 线程，
        事件队列被塞满 → 主线程来不及重绘 → 白屏；此时连「刷新」都要排在
        队列后面，所以用户说怎么切都救不回来。

        对策（两条，都不动数据只动**节奏**）：
          ① **防抖 + 合并**：`_gal_prev_timer` 单 60ms，期间的新请求只更新
             「目标行」，不重活。停手后只画最终那一行 ⇒ 连按几十次 = 1 次解码。
             （跟 `_schedule_list_refresh` 同一个套路，队列事件密时合并。）
          ② **解码限尺寸**：预览区最多 560px 宽，没必要解整张原图；
             先用 QImageReader 按目标尺寸抽样解码，省掉绝大部分解码与缩放开销。
             ⚠ 只影响右侧预览的解码方式，显示尺寸/比例/内容不变（仍是
             KeepAspectRatio 等比 fit），灯箱和缩略图缓存路径不经过这里。

        铁律：防抖**不许丢选择**——停手后必须补画最终选中行；
        `_gal_prev_flush` 是给「马上要截图/要立刻看到」的旁路（如测试、单图）。
        """
        ids = self._gallery_selected_ids()
        img, txt = self.g_prev_img, self.g_prev_txt
        if not ids:
            self._gal_prev_pending = None
            try:
                if getattr(self, "_gal_prev_timer", None) is not None:
                    self._gal_prev_timer.stop()
            except Exception:
                pass
            img.clear_source()
            txt.setText("")
            return

        # 只记「最终要画哪一行」，重活交给防抖定时器
        self._gal_prev_pending = ids[0]
        tmr = getattr(self, "_gal_prev_timer", None)
        if tmr is None:
            try:
                tmr = QTimer(self)
                tmr.setSingleShot(True)
                tmr.timeout.connect(self._gallery_table_preview_now)
                self._gal_prev_timer = tmr
            except Exception as e:
                # 建不出定时器就退回同步（老行为），但**必须留痕**：早先这里
                # 静默 except，而各种配置/属性缺失也走这条路，于是防抖被
                # 悄悄摘掉、测试全绿而问题依旧（第一版测试就栽在这）。
                # 现在置一个可断言的标志 —— 测试能直接看到"防抖没生效"。
                self._gal_prev_degrade = f"{type(e).__name__}: {e}"
                self._gallery_table_preview_now()
                return
        self._gal_prev_degrade = None
        try:
            tmr.start(self._GAL_PREVIEW_DEBOUNCE_MS)
        except Exception:
            self._gallery_table_preview_now()

    def _gallery_table_preview_now(self):
        """真正画预览：**只画停手后的最终选中行**（防抖到期 / 旁路调用）。"""
        iid = getattr(self, "_gal_prev_pending", None)
        if iid is None:
            return
        self._gal_prev_pending = None
        img, txt = self.g_prev_img, self.g_prev_txt
        img.set_rotation(0)        # R148：切到另一张图时角度复位（重置回 0°）
        row = self.db.query("SELECT path, source_url, score, width, height, "
                            "bytes FROM images WHERE id=?", (iid,))
        if not row:
            return
        r = dict(row[0])
        p = Path(r["path"]) if r.get("path") else None
        pm = self._preview_pixmap_bounded(p) if p else None
        if pm is not None and not pm.isNull():
            # 原图存进预览框，按比例缩放到**当前**宽高；拖分割条 / 拖预览
            # 滑块都会重画（不再写死 240×240）。
            img.setSource(pm)
        elif p is not None and p.exists():
            img.clear_source("（这个文件不是有效图片）")
        else:
            img.clear_source("（磁盘上已无此文件）")
        b = int(r.get("bytes") or 0)
        size = (f"{b / (1024 * 1024):.1f} MB" if b >= 1024 * 1024
                else f"{b // 1024} KB" if b else "-")
        txt.setText(
            f"#{iid}  {r.get('width') or '?'}x{r.get('height') or '?'}"
            f"  {size}  AI分 {r.get('score') or 0:.3f}\n"
            f"{p.name if p else '(无路径)'}\n"
            f"{r.get('source_url') or ''}")

    def _preview_decode_cap(self) -> int:
        """预览解码的目标边长上限（px）。预览区最大 560 宽（见
        `_resize_gallery_preview` 的 clamp），留 2 倍余量给高分屏。"""
        return int(getattr(self, "_GAL_PREVIEW_DECODE_CAP", 1120))

    def _preview_pixmap_bounded(self, p):
        """按预览所需尺寸解码，超大图不再整张进内存。

        走 QImageReader.setScaledSize —— 解码器直接按目标尺寸抽样输出，
        比「整张解码 → 再 scaled」少一大截内存和 CPU。失败一律退回整张
        QPixmap（老行为），所以**任何情况下都能显示**，只是慢一点。
        """
        try:
            if not p or not p.exists():
                return None
            cap = self._preview_decode_cap()
            from PySide6.QtCore import QSize as _QSize
            from PySide6.QtGui import QImageReader, QPixmap
            rd = QImageReader(str(p))
            sz = rd.size()
            if sz.isValid() and cap > 0:
                w, h = sz.width(), sz.height()
                long_side = max(w, h)
                if long_side > cap:
                    k = cap / float(long_side)
                    # ⚠ 等比：绝不能各向独立缩放（那会把图拉变形，P2-7 红线）
                    rd.setScaledSize(_QSize(max(1, int(w * k)),
                                            max(1, int(h * k))))
            img = rd.read()
            if img is not None and not img.isNull():
                return QPixmap.fromImage(img)
        except Exception:
            pass
        try:
            from PySide6.QtGui import QPixmap
            return QPixmap(str(p)) if p and p.exists() else None
        except Exception:
            return None

    def open_lightbox(self, *_):
        """#46 图库双击：开**灯箱**看大图（← → 翻页、+/- 缩放、F 全屏、
        Esc 关闭、下载原图、打开源网页）。

        推翻 #26 的「图库双击走系统看图器」：用户新决定是 Web 与桌面端
        **统一为灯箱**（主单 46 项）。
        ⚠ 用 `self._gal_rows`（当前筛选后的全集）当翻页范围，而不是
        「只翻这一张」—— 否则灯箱里按 ← 到底就没反应了（用户会报
        「左右切换没用」）。`path` 为空的行必须滤掉：Lightbox 自己也会滤，
        但这里先滤能让 idx 对得上。
        """
        iid = self._gallery_selected()
        if iid is None:
            return
        self._mark_viewed(iid)
        rows, idx = [], 0
        for r in (getattr(self, "_gal_rows", None) or []):
            if not r.get("path"):
                continue
            rows.append(r)
            if r.get("id") == iid:
                idx = len(rows) - 1
        if not rows:
            QMessageBox.information(self, "提示", "没有可预览的图片")
            return
        from .lightbox import Lightbox
        # F19：灯箱键位读 cfg.hotkeys（默认键位时与改前逐字节一致）
        Lightbox(rows, idx, None,
                 hotkeys=getattr(self.cfg, "hotkeys", None)).exec()

    def open_image_default(self, *_):
        """双击：用系统默认看图软件打开图片（不是打开文件夹）。"""
        iid = self._gallery_selected()
        if iid is None:
            return
        self._mark_viewed(iid)
        row = self.db.query("SELECT path FROM images WHERE id=?", (iid,))
        if not row:
            return
        p = Path(row[0]["path"])
        if not p.exists():
            QMessageBox.information(self, "提示", "文件已不存在")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(p))            # 默认图片查看器
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def gallery_find_similar_web(self, *_):
        """#88 右键『在网站中查找相似』：对话框分两段——
        上段=同站（抓来源页及其上下层页面，勾选后保存到当前图所在文件夹）；
        下段=全网反查（Google Lens / Bing / Yandex / SauceNAO 一键打开）。
        保存位置不变：仍是这张图所在的文件夹。"""
        iid = self._gallery_selected()
        if iid is None:
            QMessageBox.information(self, "提示", "先选中一张图片再操作")
            return
        row = self.db.query(
            "SELECT source_url, path, task_id, score, tags FROM images WHERE id=?",
            (iid,))
        if not row:
            return
        url = (row[0]["source_url"] or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            # 本地来源（file:// 回填）：无网页可搜，退而打开所在文件夹
            p = Path(row[0]["path"]) if row[0]["path"] else None
            if p and p.exists():
                try:
                    if sys.platform.startswith("win"):
                        subprocess.Popen(["explorer", "/select,", str(p)])
                    else:
                        subprocess.Popen(["xdg-open", str(p.parent)])
                except Exception:
                    pass
                QMessageBox.information(
                    self, "提示",
                    "该图片为本地来源（非网页抓取），无法在网站查找相似内容；"
                    "已打开所在文件夹。")
            else:
                QMessageBox.information(self, "提示", "该图片没有可用的网页来源链接。")
            return
        # 用 ImageRecord 组装（对话框需要 source_url/task_id）
        from ..models import ImageRecord
        rec = ImageRecord(
            id=iid, task_id=row[0]["task_id"], source_url=url,
            path=row[0]["path"], score=row[0]["score"], tags=[],
            sha256=None, phash=None, width=None, height=None, bytes=None,
        )
        dest = Path(row[0]["path"]).parent if row[0]["path"] else None
        if dest is None or not dest.exists():
            QMessageBox.information(self, "提示", "未找到该图片所在文件夹，无法确定保存位置。")
            return
        from .similar_web import open_similar_web
        open_similar_web(self, self.db, self.cfg, rec, dest)
        self.log_signal.emit(f"在网站中查找相似：来源页 {url}")

    def _gallery_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        it = self.g_grid.itemAt(pos)
        if it is not None:
            if not it.isSelected():
                # 右键项未选中：折叠成单张（跟随鼠标，避免误伤第一项 / 上次选中）
                self.g_grid.clearSelection()
                it.setSelected(True)
                self.g_grid.setCurrentItem(it)
            else:
                # 已选中（如「全选后右键」）：保留整组多选，但把 current 设成右键项
                # 供单图操作（打开/网站找相似）使用。注意 ExtendedSelection 下
                # setCurrentItem 会把多选「折叠成单张」，所以先记下再恢复多选。
                saved = self.g_grid.selectedItems()
                self.g_grid.setCurrentItem(it)
                for s in saved:
                    s.setSelected(True)
        iid = self._gallery_selected()
        menu = QMenu(self)
        # #46：双击已是灯箱，右键也补一个灯箱项（用户不一定记得双击）。
        # ⚠「打开图片」**保留** —— 它走系统看图器，是另一条路（要看原图
        #   在文件管理器/其它软件里编辑时用），两者不是一回事，别顺手删。
        menu.addAction("大图查看（灯箱）", self.open_lightbox)
        menu.addAction("打开图片", self.open_image_default)
        menu.addAction("打开所在文件夹", self.open_selected_file)
        # 需求 12：右键「引用页」—— 默认浏览器打开这张图所在的源网页。
        # 字段 source_url（images 表），空地址时提示而非静默无效。
        menu.addAction("引用页", self.open_source_page_gallery)
        menu.addAction("在网站中查找相似", self.gallery_find_similar_web)
        menu.addAction("收藏/取消", self.toggle_favorite)
        menu.addAction("加标签…", self.gallery_add_tag)
        menu.addAction("删标签…", self.gallery_remove_tag)
        menu.addAction("编辑标签/评分…", self.gallery_edit_tags_score)
        menu.addAction("模糊化处理…", self.gallery_blur)
        menu.addSeparator()
        # 需求 30：取消「查找相似(SSIM)」，改成右键「查找类似图片…」。
        # 区别是根本性的 —— SSIM 找的是「几乎一模一样」的近重复图，
        # 用户要的是「同一类画面、道具可以不同」（小女孩抱洋娃娃 → 抱狗），
        # 所以换成 simsearch 的三路加权：主题词 0.42 / 三重色调 0.33 / pHash 0.25。
        menu.addAction("查找类似图片…", self.gallery_find_similar_images)
        # V0.0.5：右键「相似图聚类去重（全库）」已撤（重复图片功能取消）。
        # 需求 11：批量导出已从菜单取消（这里也不能有，否则两处不一致）。
        menu.addAction("批量重命名（加前缀）…", self.batch_rename_selected)
        menu.addAction("按模板重命名…", self.gallery_rename_by_template)
        # ---- 维护（原顶部按钮精简到此，全局动作无需选中图片也能用）----
        mnt = menu.addMenu("维护")
        mnt.addAction("补充尺寸（修复分辨率缺失）", self.gallery_fill_dimensions)
        mnt.addAction("清理 0kb 孤儿", self.clean_orphan_files)
        mnt.addAction("清理缺失记录", self.cleanup_missing)
        mnt.addAction("从磁盘恢复图库", self.backfill_local)
        mnt.addAction("生成缩略图(后台)", self.generate_thumbs_background)
        mnt.addAction("从文件夹学习…", self.learn_from_folder)
        if iid is not None:
            menu.addSeparator()
            # P1-4：色值走调色板（原先写死 #ef4444）。这里**故意不用**
            # set_themed_icon —— 右键菜单是每次弹出现场建的短命对象，
            # 登记进弱引用表只会留下立刻失效的条目；直接在建的那一刻
            # 取当前皮肤的颜色即可，反正下次弹出时会重新取。
            _dark = bool(getattr(getattr(self, "cfg", None),
                                "dark_mode", False))
            # 需求 16：「不喜欢」改**二级菜单**，先选作用范围再执行。
            # 直接给一个平铺项会让用户不知道自己这一下改的是单任务还是全局，
            # 而这两者的后果差别很大（全局会把这类图从以后所有任务里排除）。
            m_dis = menu.addMenu(icons.icon(
                "thumbs-down",
                color=_tc(_dark, "danger", "#ef4444"), size=15),
                "不喜欢（删除）")
            m_dis.addAction("仅当前任务",
                            lambda: self.gallery_dislike(scope="task"))
            m_dis.addAction("全局黑名单（所有任务）",
                            lambda: self.gallery_dislike(scope="global"))
        menu.exec(self.g_grid.mapToGlobal(pos))

    def open_selected_file(self, *_):
        iid = self._gallery_selected()
        if iid is None:
            return
        row = self.db.query("SELECT path FROM images WHERE id=?", (iid,))
        if not row:
            return
        p = Path(row[0]["path"])
        if not p.exists():
            QMessageBox.information(self, "提示", "文件已不存在")
            return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p.parent)])
        except Exception as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def open_source_page_gallery(self, *_):
        """需求 12：图库右键「引用页」入口 —— 取当前选中图的 source_url 打开。"""
        iid = self._gallery_selected()
        if iid is None:
            return
        self._open_source_page_for_image(iid)

    def _open_source_page_for_image(self, iid):
        """按图片 id 查 source_url 并用默认浏览器打开（空地址提示）。"""
        row = self.db.query(
            "SELECT source_url FROM images WHERE id=?", (iid,))
        url = row[0]["source_url"] if row else None
        if not url:
            QMessageBox.information(
                self, "引用页", "这张图没有可用的源网页地址。")
            return
        import webbrowser
        webbrowser.open(url)

    @staticmethod
    def _read_tags(raw) -> list:
        """读 images.tags 成标签列表（**保留原始大小写**）。

        ⚠ 与 ``simsearch.split_tags`` 的分工：那个给学习/相似度用（统一
        小写化），这个给**写回**用 —— 用户手敲的 "White Stockings" 不能
        被打成小写再落库（界面显示会变）。老库的逗号分隔格式也认（与
        split_tags 同款兼容），读出来顺手归一成 JSON 数组。

        ⚠ R228：**实现已下沉到 ``tags_util.read_tags``**（Web 端要用同一
        套口径）。这里保留同名静态方法只为不动既有调用点与护栏，
        转发过去、不另写一份 —— 两端各写一份迟早会「网页加的标签桌面
        端读不出来」。
        """
        from ..tags_util import read_tags
        return read_tags(raw)

    @staticmethod
    def _merge_tags(rows, tag: str) -> list:
        """F17 批量**加**标签的纯函数：返回 ``[(id, 新tags JSON), …]``。

        · 去重不覆盖：已有该标签的行**不返回**（不写库、不计数）；
        · 保留其余标签的原顺序与大小写，新标签追加在末尾；
        · tag 为空 / rows 为空 → 空列表（调用方据此提示）。

        ⚠ R228：实现下沉到 ``tags_util.merge_tags``（Web 同口径），
        这里只转发。
        """
        from ..tags_util import merge_tags
        return merge_tags(rows, tag)

    @staticmethod
    def _strip_tags(rows, remove) -> list:
        """F17 批量**删**标签的纯函数：返回 ``[(id, 新tags JSON), …]``。

        · 只给**确实少了一个标签**的行返回（没命中的行不写库）；
        · 保留其余标签的原顺序与大小写（老库逗号格式一并归一成 JSON）；
        · remove 为空 → 空列表。

        ⚠ R228：实现下沉到 ``tags_util.strip_tags``（Web 同口径），
        这里只转发。
        """
        from ..tags_util import strip_tags
        return strip_tags(rows, remove)

    def gallery_add_tag(self):
        """给选中的一张或多张图加一个自定义标签（支持 Ctrl/Shift 多选）。

        标签写入 images.tags（与 AI 主题词共用一个字段），图库搜索框按标签实时过滤，
        方便把喜欢的图分组（如「白丝」「风景」）。
        """
        from PySide6.QtWidgets import QInputDialog
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(
                self, "提示", "先选中一张或多张图片（Ctrl / Shift 多选）")
            return
        tag, ok = QInputDialog.getText(
            self, "加标签", f"给选中的 {len(ids)} 张图加一个标签：")
        tag = (tag or "").strip()
        if not ok or not tag:
            return
        rows = []
        for iid in ids:
            r = self.db.query("SELECT id, tags FROM images WHERE id=?", (iid,))
            if r:
                rows.append({"id": r[0]["id"], "tags": r[0]["tags"]})
        plan = self._merge_tags(rows, tag)
        for iid, js in plan:
            self.db.execute("UPDATE images SET tags=? WHERE id=?", (js, iid))
        n = len(plan)
        self.log_signal.emit(f"图库: 给 {n} 张图加标签「{tag}」（搜索框可过滤）")
        self._gal_tag_combo_filled = False   # 新标签要立刻进筛选下拉
        self.load_gallery()

    def gallery_remove_tag(self):
        """F17：给选中的一张或多张图**删标签**（与「加标签…」对称）。

        勾选清单 = 选中图片出现过的全部标签（并集，保序去重）；只删勾中的，
        没勾的标签原样留下。删的是 images.tags 里的用户标签（与 AI 主题词
        共用一个字段）—— 删错了可以用「加标签…」加回来。
        """
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox,
                                       QListWidget, QListWidgetItem)
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(
                self, "提示", "先选中一张或多张图片（Ctrl / Shift 多选）")
            return
        rows = []
        for iid in ids:
            r = self.db.query("SELECT id, tags FROM images WHERE id=?", (iid,))
            if r:
                rows.append({"id": r[0]["id"], "tags": r[0]["tags"]})
        if not rows:
            return
        union: list = []
        for r in rows:
            for t in self._read_tags(r["tags"]):
                t = t.strip()
                if t and t not in union:
                    union.append(t)
        if not union:
            QMessageBox.information(
                self, "删标签", "选中的图片都没有标签，无可删除。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("删标签")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"勾选要从这 {len(rows)} 张图里删掉的标签（没勾的原样保留）:"))
        lst = QListWidget()
        for t in union:
            it = QListWidgetItem(t)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Unchecked)
            lst.addItem(it)
        v.addWidget(lst)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        zh_buttons(bb)          # C5
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        rm = [lst.item(i).text() for i in range(lst.count())
              if lst.item(i).checkState() == Qt.CheckState.Checked]
        if not rm:
            return
        plan = self._strip_tags(rows, rm)
        for iid, js in plan:
            self.db.execute("UPDATE images SET tags=? WHERE id=?", (js, iid))
        self.log_signal.emit(
            f"图库: 从 {len(plan)} 张图删标签「{'、'.join(rm)}」")
        self._gal_tag_combo_filled = False   # 删掉的标签要立刻从下拉消失
        self.load_gallery()

    def gallery_edit_tags_score(self):
        """#36 整行编辑一张图的标签与用户评分：可改可删。

        「加标签…」是批量追加模式；这里是单图全量编辑——标签文本框直接展示
        当前全部标签（逗号分隔，中英文逗号都认），用户评分 0~5 步进 0.5，
        写入独立的 user_score 列（与 AI 打分 score 0~1 互不污染）。
        """
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox,
                                       QDoubleSpinBox)
        iid = self._gallery_selected_ids()
        if len(iid) != 1:
            QMessageBox.information(self, "编辑标签/评分", "先选中一张图片")
            return
        row = self.db.query("SELECT tags, user_score FROM images WHERE id=?",
                            (iid[0],))
        if not row:
            return
        try:
            cur_tags = ", ".join(json.loads(row[0]["tags"] or "[]"))
        except Exception:
            cur_tags = ""
        try:
            cur_score = float(row[0]["user_score"] or 0.0)
        except Exception:
            cur_score = 0.0
        dlg = QDialog(self)
        dlg.setWindowTitle("编辑标签 / 用户评分")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("标签（逗号分隔，可增可删）:"))
        ed = QTextEdit(cur_tags)
        ed.setFixedHeight(70)
        v.addWidget(ed)
        h = QHBoxLayout()
        h.addWidget(QLabel("用户评分 (0~5):"))
        sp = QDoubleSpinBox()
        sp.setRange(0.0, 5.0)
        sp.setSingleStep(0.5)
        sp.setValue(min(max(cur_score, 0.0), 5.0))
        h.addWidget(sp)
        h.addStretch()
        v.addLayout(h)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        zh_buttons(bb)          # C5
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tags = [t.strip() for t in
                ed.toPlainText().replace("，", ",").split(",") if t.strip()]
        self.db.execute(
            "UPDATE images SET tags=?, user_score=? WHERE id=?",
            (json.dumps(tags, ensure_ascii=False), float(sp.value()), iid[0]))
        self.log_signal.emit(
            f"图库: 已更新标签/用户评分（{len(tags)} 个标签，评分 {sp.value():g}）")
        self.load_gallery()

    def toggle_favorite(self):
        iid = self._gallery_selected()
        if iid is None:
            QMessageBox.information(self, "提示", "先在图库里选一张")
            return
        row = self.db.query(
            "SELECT favorite, task_id, tags FROM images WHERE id=?", (iid,))
        if row:
            new = 0 if row[0]["favorite"] else 1
            self.db.execute("UPDATE images SET favorite=? WHERE id=?", (new, iid))
            # R166：收藏 = 正样本（用户明确想留），进学习引擎。
            # ⚠ 只在**收**时学（0→1）：取消收藏只是收回一个主观标记，
            #   不是"这张不要"；学成负样本会把用户其实没意见的图打成负权重。
            # ⚠ 放在 UPDATE 之后：此行语义是"我把它标记成收藏了"，先写库再学，
            #   万一学失败（被 learn_action 内部吞掉）也不影响收藏本身生效。
            if new:
                try:
                    from ..classify.preference import PreferenceLearner
                    PreferenceLearner(self.db, 0).learn_action(
                        "favorite", self._tags_of_row(row[0]),
                        source="gallery", image_id=iid,
                        sample_task_id=row[0]["task_id"])
                except Exception:
                    pass
            # 只改当前项的文本（load_gallery 有签名跳过，整页重绘没必要）
            item = self.g_grid.currentItem()
            if item is not None:
                item.setText(f"{'★' if new else ''}#{iid} "
                             f"{item.text().split(' ', 1)[-1]}")

    @staticmethod
    def _tags_of_row(row) -> list:
        """从一行 images 记录取标签列表（R166，桌面端统一口径）。

        ⚠ 走 ``simsearch.split_tags`` 而不是裸 ``json.loads``：库里标签可能
        是「逗号分隔」的老格式，两种都要认，否则老图的判断学不进去。
        """
        try:
            raw = row["tags"] if row is not None else None
        except Exception:
            raw = None
        if not raw:
            return []
        try:
            from ..simsearch import split_tags
            return split_tags(raw)
        except Exception:
            return []

    # ----------------------------------------------------- 图库视图/喜欢不喜欢/缺失清理 ---
    def _apply_grid(self):
        """网格模式：缩略图 = 滑块值，列数交给 Qt；列表模式左表格右自适应预览。

        r30 起因：以前是「固定 n 列 + 列宽塞不进视口就把图缩小」，于是 6 列 +
        900px 视口下缩略图最大只有 ~143px，而审核页走 apply_grid_layout 不钳列宽
        （218px 直接生效）——同一个软件里两种网格不像一家，用户反馈"图库的图小"。

        r31：用户实测「每行几张」和「预览」两个控件一起用有 BUG，直接砍掉列数
        控件。现在只剩**一个**尺寸口径：gallery_thumb_size 就是缩略图边长，一行
        放几张由 Qt（Adjust resize mode）按 gridSize 自己算，绝不反压图的大小。
        """
        if self.g_list_mode:
            self.g_grid.setViewMode(QListWidget.ViewMode.ListMode)
            self.g_grid.setWrapping(False)
            icon = 100
            self.g_grid.setIconSize(QSize(icon, icon))
            self.g_grid.setSpacing(6)
            # 显式行宽=视口宽、行高=132：避免 ListMode 下多行信息把行高压塌、
            # 缩略图与文字堆在一起；每行列满整宽、左缩略图右信息，布局清爽。
            vw = self.g_grid.viewport().width() or 900
            self.g_grid.setGridSize(QSize(vw, 132))
            return
        from .table_view import apply_grid_layout
        self.g_grid.setViewMode(QListWidget.ViewMode.IconMode)
        # 列表模式里设过 setWrapping(False)，切回网格必须恢复 True，
        # 否则所有缩略图会挤在同一行、永远不换行。
        self.g_grid.setWrapping(True)
        # 预览尺寸：基础值取「预览」滑块；最大化时跟着窗口放大（×1.8）
        target = int(getattr(self.cfg, "gallery_thumb_size", 128)) or 128
        if self.isMaximized():
            target = int(target * 1.8)
        target = max(48, min(target, 420))
        # 与「审核网格」共用同一套几何（间距 8 / 单元格=图+28 / 禁误拖拽）：
        # 以前这里 spacing=12、缩略图 = cw-8，和审核页的 8 / 整宽差一截，
        # 同一个软件里两种网格不像一家。列数交给 Qt 自适应，不再手工钳。
        apply_grid_layout(self.g_grid, target)

    def toggle_gallery_mode(self):
        self.g_list_mode = self.g_mode.isChecked()
        self.g_mode.setText("网格模式" if self.g_list_mode else "列表模式")
        # P1-4：同 review_tab 的 b_rmode —— 改走 set_themed_icon 才会跟随皮肤。
        icons.set_themed_icon(self.g_mode, "grid" if self.g_list_mode else "list",
                              "{text_dim}", 15)
        # 立即切可见性（不等 load_gallery，避免缓存早退时点了没反应）
        self.g_grid.setVisible(not self.g_list_mode)
        self.g_split.setVisible(self.g_list_mode)
        # #39：翻页只给网格模式；列表模式一次看一整屏表格，翻页按钮只是干扰
        self.g_pager.setVisible(not self.g_list_mode)
        self._apply_grid()
        self.load_gallery()
        # R51：切列表/网格模式后重抬水印浮层，避免被新显示的内容盖在下面
        # （固定 alpha 在不同底色上看似「透明度不一致」）。
        self._refresh_watermarks()

    @staticmethod
    def _wm_pct(v: int) -> str:
        """#42：alpha 值 → 百分比文案（0/255 会显示成 0%，看着像坏了，所以 0 特殊处理）。"""
        if int(v) <= 0:
            return "隐藏"
        return f"{round(int(v) * 100 / 255)}%"

    def _refresh_watermarks(self):
        """R51：把全部水印浮层重算几何并置顶（切列表/网格模式后调用）。

        水印 eventFilter 只监听页面与 stack 自身的几何事件；切列表模式时
        t_split 由隐藏变显示并不向页面发 Show，浮层停在旧 z 序、被表格/网格
        内容盖住，看起来「透明度不一致」。这里统一重抬一次，保证三页水印
        永远在最上层、且同一 alpha（178）。"""
        for wm in getattr(self, "_watermarks", []):
            try:
                wm._fit()
            except Exception:
                pass

    def _apply_watermark_alpha(self, v: int):
        """拖动时只重画水印，不落盘（save_config 每次写 json，拖动会卡）。"""
        for wm in getattr(self, "_watermarks", []):
            try:
                wm.set_alpha(int(v))
            except Exception:
                pass

    def _save_watermark_alpha(self):
        """松手才落盘：写 cfg.watermark_alpha 并持久化。"""
        try:
            v = int(self.s_wm.value())
            self.cfg.watermark_alpha = v
            save_config(self.cfg)
        except Exception:
            pass

    def _apply_watermark_enabled(self, on: bool):
        """R50：水印总开关 —— **三处浮层一起**显示/隐藏。

        ⚠ 必须三处一起：只关主浮层的话，审核页与图库页各自那个
        独立浮层还在（它们当初存在的原因就是主浮层会被这两页盖住），
        于是用户看到"关了还有水印"，比没开关更糟。
        """
        for wm in getattr(self, "_watermarks", []):
            try:
                wm.setVisible(bool(on))
            except Exception:
                pass

    def set_gallery_thumb_size(self, v: int):
        """图库缩略图尺寸：拖动即时生效并持久化（#83）。**唯一**的尺寸控件。

        网格模式：只改单元格/图标尺寸（已有缩略图按新尺寸缩放，无需重建列表），
        所以拖动滑条很顺；列数交给 Qt 自适应，图片尺寸不会被顶小。
        列表模式：缩放图没有意义（表格行高固定），改成**联动右侧预览图**——
        把预览窗格宽到 v+40，图片按新宽重画，这样两种模式下拖滑块都有反应
        （以前列表模式拖滑块纹丝不动，用户报「预览大于 235px 就没效果」）。
        """
        self.cfg.gallery_thumb_size = int(v)
        try:
            save_config(self.cfg)
        except Exception:
            pass
        if self.g_list_mode:
            self._resize_gallery_preview(int(v))
            return
        self._apply_grid()

    def _resize_gallery_preview(self, v: int):
        """列表模式：把右侧预览窗格调成 v 大并按新尺寸重画预览图。"""
        pv = getattr(self, "g_prev_img", None)
        sp = getattr(self, "g_split", None)
        if pv is None or sp is None:
            return
        try:
            sizes = list(sp.sizes())
            if len(sizes) >= 2 and sizes[1] > 0:
                want = max(160, min(560, int(v) + 40))
                sizes[1] = want
                sizes[0] = max(200, sizes[0])
                sp.setSizes(sizes)
        except Exception:
            pass
        try:
            pv._redraw()
        except Exception:
            pass

    def gallery_like(self):
        ids = self._gallery_selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "先在图库里选一张")
            return
        for iid in ids:
            self._apply_like(iid)
        self.load_gallery()

    def _apply_like(self, iid) -> bool:
        """单张「喜欢」：收藏 + 记反馈 + 喂给本地偏好学习。"""
        row = self.db.query(
            "SELECT favorite, task_id, tags FROM images WHERE id=?", (iid,))
        if not row:
            return False
        self.db.execute("UPDATE images SET favorite=1 WHERE id=?", (iid,))
        self.db.execute("INSERT INTO reviews(image_id, decision, task_id) "
                        "VALUES (?,?,?)", (iid, "like", row[0]["task_id"]))
        self.db.audit("gallery", "like", str(iid))
        # R166：走统一入口（记权重 + 记来源留痕）。
        # ⚠ 原来这里是裸 ``PreferenceLearner(tid).learn("like", json.loads(...))``：
        #   ① 没有来源留痕，看板上分不出这次是"图库点的"还是"审核页点的"；
        #   ② ``json.loads`` 只认 JSON 数组，老库里「逗号分隔」的标签会抛异常
        #      被 except 吞掉 = 那些老图点喜欢永远学不到。改走 ``_tags_of_row``
        #      （内部统一 split_tags），两种格式都认。
        try:
            from ..classify.preference import PreferenceLearner
            PreferenceLearner(self.db, 0).learn_action(
                "like", self._tags_of_row(row[0]), source="gallery",
                image_id=iid, sample_task_id=row[0]["task_id"])
        except Exception:
            pass
        return True

    def gallery_dislike(self, scope: str = "task"):
        """图库「不喜欢」入口：直接走批量接口（内部已按选中图弹「原因」对话框）。

        需求 16：``scope`` = "task" 只影响当前任务 / "global" 进全局黑名单。
        """
        self.gallery_batch_dislike(scope=scope)

    def _task_ai_labels_by_tier(self, task_id) -> dict:
        """【R117 P1-3】AI 主题词**按层级**分组：``{1: [...], 2: [...], 3: [...]}``。

        为什么分组：用户要「层级标注清晰」。把所有词拍平成一个列表虽然都
        显示了，但用户看不出哪个是二级哪个是三级 —— 分组后左栏渲染成
        「一级主题词 / 二级 / 三级」三段小标题，一眼可辨。
        """
        out: dict = {1: [], 2: [], 3: []}
        try:
            from ..repo import get_task
            t = get_task(self.db, task_id)
            if not t:
                return out
            for tier, fld in ((1, "ai_labels"), (2, "ai_labels_l2"),
                              (3, "ai_labels_l3")):
                for v in (getattr(t, fld, None) or []):
                    v = (str(v or "")).strip()
                    # 跨层去重：同一个词若 L1/L3 都配了，只在**先出现的层**
                    # 显示一次（白名单铁律：两栏互不重复）
                    if not v or any(v in lst for lst in out.values()):
                        continue
                    out[tier].append(v)
        except Exception:
            return {1: [], 2: [], 3: []}
        return out

    def _task_ai_labels(self, task_id) -> list:
        """【R117 P1-3】取任务配置的 AI 主题词（白名单），L1+L2+L3 **都要**。

        原实现只 ``return list(t.ai_labels or [])`` —— **只读一级**，L2/L3
        （二级/三级细分词）压根没进来，所以「不喜欢」弹窗左栏只剩 1 级，
        用户报的「2级/3级又不见了」就是这么来的。

        现在三层合并返回。**不带层级前缀**（加前缀会让勾选后
        PreferenceLearner 学到带前缀的假词）；标注交给 dislike_dialog 按
        tier 分段渲染。
        """
        by = self._task_ai_labels_by_tier(task_id)
        out: list[str] = []
        for tier in (1, 2, 3):
            for v in by.get(tier) or []:
                if v not in out:
                    out.append(v)
        return out

    def _task_ai_blacklist(self, task_id) -> list:
        """取任务配置的「AI 主题排除词」（即用户填的 AI 主题排除词），用于
        「不喜欢」弹窗的黑名单一栏——让用户看到已排除的词、并可继续追加。"""
        try:
            from ..repo import get_task
            t = get_task(self.db, task_id)
            return list(t.ai_blacklist or []) if t else []
        except Exception:
            return []

    def _task_blacklist(self, task_id) -> list:
        """取任务配置的「普通黑名单」关键词（token-aware 命中即排除），用于
        「不喜欢」弹窗的黑名单一栏，与 AI 主题排除词合并展示。"""
        try:
            from ..repo import get_task
            t = get_task(self.db, task_id)
            return list(t.blacklist or []) if t else []
        except Exception:
            return []

    def _add_exclusions(self, task_id, labels: list[str],
                        scope: str = "task"):
        """把排除词落盘。

        需求 16：``scope`` 决定作用范围 ——
        - ``"task"``（默认）：只写当前任务的 AI 排除词（原有行为）；
        - ``"global"``：写进 AppConfig 的全局黑名单，**所有任务**都生效。

        【硬性规则】**主题词永不进排除词**：左栏勾的是"这张图没匹配上该主题"，
        只做负向学习。这里再兜一层 —— 凡是出现在任务 ai_labels（主题词）里的
        词一律丢弃并记录，防止上游改坏后把主题词写进黑名单（那就等于把用户
        要的东西永久排除了，且很难发现）。
        """
        if not labels:
            return
        try:
            from ..repo import get_task, save_task
            t = get_task(self.db, task_id)
            if not t:
                return
            if scope == "global":
                from ..config import add_global_blacklist
                theme = set(t.ai_labels or [])
                safe = [w for w in labels if w not in theme]
                added = add_global_blacklist(self.cfg, safe)
                if added:
                    self.log_signal.emit(
                        f"已把 {', '.join(added)} 加入**全局黑名单**"
                        f"（所有任务都会跳过这类图）")
                return
            merged = list(t.ai_blacklist or [])
            theme = set(t.ai_labels or [])
            for lab in labels:
                if lab in theme:
                    self.log_signal.emit(
                        f"「{lab}」是主题词，按规则不加入排除词")
                    continue
                if lab not in merged:
                    merged.append(lab)
            t.ai_blacklist = merged
            save_task(self.db, t)
            self.log_signal.emit(
                f"已把 {len(labels)} 个词条加入任务 #{task_id} 排除词: "
                f"{', '.join(labels)}（下次运行自动跳过这类图）")
        except Exception as e:
            self.log_signal.emit(f"写入排除词失败: {type(e).__name__}: {e}")

    def _apply_dislike(self, iid, ask: bool = True,
                       scope: str = "task") -> bool:
        """单张「不喜欢」：记反馈 + 学习，再按回收站开关删除/隔离。

        若这张图所属任务配了 AI 主题词（白名单），会先弹窗让用户选「哪个词条未命中」
        （加入任务排除词，下次不再自动保留这类图），或「单纯不喜欢」。取消则不动。
        """
        row = self.db.query(
            "SELECT path, task_id, tags FROM images WHERE id=?", (iid,))
        if not row:
            return False
        recycle = bool(getattr(self.cfg, "use_recycle", True))
        task_id = row[0]["task_id"]
        if ask:
            labels = self._task_ai_labels(task_id)
            blacks = sorted(set(self._task_blacklist(task_id))
                            | set(self._task_ai_blacklist(task_id)))
            if labels or blacks:
                from .dislike_dialog import (ask_dislike_reason,
                                             consume_manual_words,
                                             consume_correct_labels)
                hint = ("删除后会"
                        + ("移入回收站（可还原）" if recycle
                           else "从磁盘永久删除（不可恢复）"))
                # 【R117 P1-3】按 L1/L2/L3 分段显示
                unmatched, exclusions, mode = ask_dislike_reason(
                    self, labels, blacks, hint=hint, db=self.db,
                    labels_by_tier=self._task_ai_labels_by_tier(task_id))
                if mode == "cancel":
                    return False
                # 【硬性规则】左栏主题词 = 图没匹配上该主题，只负向学习，不进排除词
                if exclusions:
                    self._add_exclusions(task_id, exclusions,
                                         scope=scope)
                if unmatched:
                    from ..classify.preference import PreferenceLearner
                    PreferenceLearner(self.db, task_id).learn("dislike",
                                                             unmatched)
                # #64：手填词桥接到词表标签再学
                manual = consume_manual_words()
                if manual:
                    from ..classify.label_bridge import LabelBridge
                    from ..classify.preference import PreferenceLearner
                    hits = PreferenceLearner(self.db, task_id).learn_manual(
                        "dislike", manual)
                    for h in hits or []:
                        self.log("词表桥接: " + LabelBridge.hint(h))
                # R174 §4：用户声明的「正确标签」→ 正向学习（详见批量那条的注释）
                correct = consume_correct_labels()
                if correct:
                    from ..classify.preference import PreferenceLearner
                    PreferenceLearner(self.db, task_id).learn_manual(
                        "like", correct)
                    self.log("误标纠正: 记为正向 " + " ".join(correct))
            else:
                tip = ("确定删除这张图片吗？\n（移入回收站，之后可以还原）"
                       if recycle else
                       "确定删除这张图片吗？\n（直接从磁盘永久删除，无法恢复）")
                if QMessageBox.question(self, "不喜欢", tip) \
                        != QMessageBox.StandardButton.Yes:
                    return False
        # 先记反馈 + 学习（删前），再删文件 + 删记录
        self.db.execute("INSERT INTO reviews(image_id, decision, task_id) "
                        "VALUES (?,?,?)", (iid, "dislike", row[0]["task_id"]))
        self.db.audit("gallery", "dislike-delete", str(iid))
        # R166：走统一入口。⚠ 必须是「删前」学 —— use_recycle 关着时下面会
        # 真删图片行，行没了 tags 也就取不到了（原来放这里靠的就是这个顺序，
        # 但裸 json.loads 会让老格式标签静默学不到，见 _apply_like 的注释）。
        try:
            from ..classify.preference import PreferenceLearner
            PreferenceLearner(self.db, 0).learn_action(
                "dislike", self._tags_of_row(row[0]), source="gallery",
                image_id=iid, sample_task_id=row[0]["task_id"])
        except Exception:
            pass
        p = Path(row[0]["path"]) if row[0]["path"] else None
        if recycle:
            # 经过回收站：文件移到隔离区 + 保留记录（之后可在回收站还原）
            # V0.0.3：用户原有图库（F 盘）的文件只登记、绝不搬走或删除，
            # 否则在软件里点一次「不喜欢」，F 盘原图就被移动了。
            from ..safe_delete import is_software_file
            protected = bool(p and p.exists()
                             and not is_software_file(p, self.cfg))
            dest = None
            try:
                self.cfg.isolate_dir.mkdir(parents=True, exist_ok=True)
                if p and p.exists() and not protected:
                    dest = self.cfg.isolate_dir / p.name
                    n = 1
                    while dest.exists():
                        dest = self.cfg.isolate_dir / f"{p.stem}({n}){p.suffix}"
                        n += 1
                    shutil.move(str(p), str(dest))
                if protected:
                    self.log("原有图库文件未改动，仅记入隔离区：" + str(p))
                # R143：进隔离区同时清 needs_review（四态机：待审核↔隔离区
                # 互斥）。桌面端与 Web 端 `webui._api_action` 必须同口径，
                # 否则同一张图在两端归属不同。
                self.db.execute(
                    "UPDATE images SET in_quarantine=1, needs_review=0, path=? "
                    "WHERE id=?",
                    (str(dest) if dest else row[0]["path"], iid))
            except Exception:
                try:
                    self.db.execute(
                        "UPDATE images SET in_quarantine=1, needs_review=0 "
                        "WHERE id=?", (iid,))
                except Exception:
                    pass
        else:
            # 不经过回收站：删除软件自己的文件 + 移除记录。
            # V0.0.3：原有图库（F 盘）的文件绝不删，只清登记记录。
            from ..safe_delete import delete_image_file
            delete_image_file(p, self.cfg, recycle=False)
            # R221/C10：FK 开启后子表先清（reviews 引用 images）——
            # 本路径上面刚 INSERT 了一条 dislike 审核行，不先删 reviews
            # 连图片行都删不掉。
            self.db.execute("DELETE FROM reviews WHERE image_id=?", (iid,))
            self.db.execute("DELETE FROM images WHERE id=?", (iid,))
        return True

    def _tidy_after_task(self, task_id=None):
        """任务跑完的轻量清理：只清软件自己产生的垃圾（不碰系统回收站）。

        r39：以前是在主线程直接跑 —— 它要扫一遍所有 output_dir 并逐个 stat，
        图一多就把界面卡住几十秒（真机扫描里和「清理 0kb」一起刷了两条
        `[!] blocked the qt loop for 60~73s`）。改成丢后台，结果只回一句日志。
        """
        def _work():
            from ..tidy import tidy_after_task
            # log 回调走 log_signal（主线程），别让后台线程碰 Qt 控件
            tidy_after_task(self.cfg, self.db,
                            log=lambda s: self.log_signal.emit(s))
            return None

        self._bg_submit(_work, lambda _r: None, label="任务后清理")

    def cleanup_missing(self):
        """删除图库里指向『磁盘已删除文件』的残留记录（清前自动备份数据库）。"""
        rows = self.db.query("SELECT id, path FROM images WHERE path IS NOT NULL")
        missing = [r for r in rows if not Path(r["path"]).exists()]
        n = len(missing)
        if n == 0:
            self.g_banner.setVisible(False)   # 没缺失时顺手把黄色横幅撤掉
            QMessageBox.information(self, "清理缺失", "没有缺失文件，图库与磁盘一致。")
            return
        if QMessageBox.question(
                self, "清理缺失图片",
                f"有 {n} 张图片的磁盘文件已删除/移动，但还留在图库记录里。\n"
                f"确定从图库删除这些残留记录吗？（操作前会自动备份数据库）") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.db.backup(self.cfg.db_path.parent / f"mrm_db_backup_{ts}.db")
        except Exception as e:
            QMessageBox.warning(self, "备份失败", f"数据库备份失败: {e}\n仍继续清理。")
        for r in missing:
            self.db.execute("DELETE FROM reviews WHERE image_id=?", (r["id"],))
            self.db.execute("DELETE FROM images WHERE id=?", (r["id"],))
        self.db.audit("gallery", "cleanup-missing", "", f"removed {n} stale rows")
        self.load_gallery()
        # 清完立刻重查一遍：缺失归零时把顶部黄色横幅撤掉（以前残留，用户要再点一次忽略）
        self.check_missing_on_start()
        QMessageBox.information(self, "完成", f"已清理 {n} 条缺失记录。")

    # ------------------------------------------- 从文件夹学习（本地 CLIP 打标入库）---
    def learn_from_folder(self):
        """GUI 入口：选一个本地图片文件夹，后台用本地 CLIP 批量打标+入库。
        适合把用户自己的图库（可能不在这台机器上）喂给软件学习。"""
        # V0.0.7：这里原来有一道「学习开关关着就直接 return」的门禁，
        # 等于把「导入自己的图库教软件」也一起拦掉了。那个开关管的只是
        # 「一张张点喜欢/不喜欢要不要记口味账」，跟主动把整个图库交给
        # 软件是两回事（learn_imported 的注释写得很清楚，网页端也从不拦）。
        # 后果：开关一关，桌面连选目录都不让选，F 盘图库一张导不进来。
        # 现在删掉门禁，两端行为一致；导入即学习本来就不受这个开关管。
        d = self._dir_pick(
            "选择要学习的图片文件夹",
            start=str(getattr(self.cfg, "last_learn_dir", "")
                      or getattr(self.cfg, "library_dir", "")))
        if not d:
            return
        # V0.0.5：导入确认小窗口 —— 「复制到软件图库」开关（默认关）。
        # 打开后：每张图复制一份进软件自己的图库目录，账本登记副本；
        # 之后所有删除 / 搬动只碰副本，原文件夹（如 F 盘）彻底隔离。
        # 关掉则维持老行为：只登记原地址，保护闸兜底不删外部文件。
        q = QDialog(self)
        q.setWindowTitle("导入本地图库")
        _ql = QVBoxLayout(q)
        _ql.addWidget(QLabel(
            f"即将导入并学习：\n{d}"))
        _cb_copy = QCheckBox("同时复制一份进软件图库（占双份磁盘，导入更慢；"
                             "但之后软件里的删除、移动只碰副本，不碰原图）")
        _cb_copy.setToolTip("默认不勾：只登记原图位置。原图受保护闸保护，"
                            "软件不会删除或搬动数据目录之外的文件。")
        _ql.addWidget(_cb_copy)
        # V0.0.7：网页端早就有这个勾，桌面端漏了 —— 决定清单明确要求
        # 「复制」和「只入库先不打标签」挨着放、两边同一个勾。
        _cb_index_only = QCheckBox("只入库、先不打标签（大批量时先建立索引，"
                                   "之后重新导入可补打标签并学习）")
        _cb_index_only.setToolTip(
            "未勾选：导入时自动识别图片并标注，同时从这批图片中学习偏好。\n"
            "勾选后：仅登记文件、不执行识别；之后重新导入同一目录并取消"
            "勾选，即可自动补打标签，不会重复入库、不会重复复制、不会修改原图。")
        _ql.addWidget(_cb_index_only)
        _bb = QHBoxLayout()
        _b_ok = QPushButton("开始导入"); _b_no = QPushButton("取消")
        _b_ok.clicked.connect(q.accept); _b_no.clicked.connect(q.reject)
        _bb.addStretch(1); _bb.addWidget(_b_ok); _bb.addWidget(_b_no)
        _ql.addLayout(_bb)
        q.setMinimumWidth(480)
        if q.exec() != QDialog.DialogCode.Accepted:
            return
        _copy = _cb_copy.isChecked()
        _do_tag = not _cb_index_only.isChecked()
        # V0.0.7：只记「上次选的源目录」，绝不改写「下载的新图存哪」。
        # 以前直接写 last_output_dir，导入一次 F 盘图库后，之后所有新建
        # 下载任务都把新图存进 F 盘原图库 —— 那片目录还被保护闸锁死，
        # 以后连删都删不掉。两件事必须用两个字段。
        self.cfg.last_learn_dir = Path(d)
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        dlg = QDialog(self)
        dlg.setWindowTitle("从文件夹学习…")
        dl = QVBoxLayout(dlg)
        info = QLabel(f"正在学习：{d}\n（本地 CLIP 推理，0 积分、纯离线）")
        dl.addWidget(info)
        bar = QProgressBar(); bar.setRange(0, 0); dl.addWidget(bar)
        msg = QLabel(""); dl.addWidget(msg)
        dlg.setMinimumWidth(460)
        dlg.show()

        # r39b：total 只在 on_progress 的参数里，on_finished 直接用会 NameError
        # （pyflakes 抓出来的：点了「从文件夹学习」跑完必炸）。
        state = {"total": 0}

        def on_progress(done, total, name):
            state["total"] = total
            if total > 0:
                bar.setRange(0, total); bar.setValue(done)
            msg.setText(f"{done}/{total}  {name}")

        def on_finished(stats):
            # V0.0.6：用户按 ESC 提前关掉进度框后，这些控件可能已被回收，
            # 回调里再 setText 会抛 RuntimeError 甚至带崩程序。都兜住。
            try:
                bar.setValue(state["total"] or bar.maximum())
                info.setText(
                    f"学习完成 ✅\n新增 {stats.get('added',0)} · "
                    f"跳过重复 {stats.get('skipped_dup',0)} · "
                    f"黑名单 {stats.get('blacklisted',0)} · 失败 {stats.get('errors',0)}")
                # R152-D2：AI 降级时如实说明（原来静默用 stub 打假标签）。
                note = str(stats.get("ai_note") or "")
                # V0.0.3：导入自己的图库 = 已经学过偏好，不再提示用户逐张打分
                learned = stats.get("learned") or {}
                if learned.get("written"):
                    msg.setText(f"已从这批图自动学到 {learned['written']} 个偏好词"
                                + (f"（{note}）" if note else ""))
                else:
                    msg.setText(note if note else
                                "已入库，并从这批图自动学习偏好。")
            except RuntimeError:
                pass
            self.load_gallery()
            self.check_missing_on_start()

        def on_errored(text):
            try:
                msg.setText(text)
            except RuntimeError:
                pass

        worker = _LearnWorker(self.cfg, d, copy_to_library=_copy,
                              do_tag=_do_tag)
        # 挂到 self 上防被回收：线程还在跑、QThread 对象却没了会直接 abort
        self._learn_worker = worker
        worker.progress.connect(on_progress)
        worker.finished.connect(on_finished)
        worker.errored.connect(on_errored)
        worker.start()
        dlg.finished.connect(lambda *_: None)
        dlg.exec()

    def check_missing_on_start(self):
        """启动与学习后做轻量库↔磁盘一致性检查，有缺失就弹黄色横幅提示。

        R152-A7：原来是主线程对全表逐条 ``Path(path).exists()``（几千张图
        能把启动首帧堵死几十秒——stats.py:30 那条 r40 实录同款坑）。
        现在复用 ``stats._existing_paths``（先 scandir 父目录再纯字符串
        比较）并丢到后台线程，结果经 ``_bg_submit`` 回主线程更新横幅。
        """
        from ..stats import _existing_paths

        def _work() -> int:
            try:
                rows = self.db.query(
                    "SELECT id, path FROM images WHERE path IS NOT NULL")
            except Exception:
                return 0
            paths = [r["path"] for r in rows]
            hit = _existing_paths(paths)
            return len(paths) - len(hit)

        def _done(n: int):
            try:
                if n and n > 0:
                    self.g_banner_label.setText(
                        f"⚠ 图库中有 {n} 张图片的磁盘文件已缺失（可能被清理过）。"
                        f"可清理残留记录，或从磁盘重新恢复。")
                    self.g_banner.setVisible(True)
                else:
                    self.g_banner.setVisible(False)
            except Exception:
                pass

        self._bg_submit(_work, _done, label="启动一致性检查")

    # ---------------------------------------------------------------- misc ---
    def export_log(self):
        d = self.cfg.log_dir
        d.mkdir(parents=True, exist_ok=True)
        p = d / "mrm-log.txt"
        p.write_text(self.log_view.toPlainText(), encoding="utf-8")
        QMessageBox.information(self, "已导出", str(p))

    def export_diagnostic_bundle(self):
        """打包一份排障材料：脱敏配置 + 库统计 + 日志 + 环境 + 数据库副本。

        不含任何图片字节；proxy / ai_api_key 等凭证只保留主机、密码打码。
        """
        from PySide6.QtWidgets import QFileDialog
        from datetime import datetime as _dt0
        default_name = f"mrm_diagnostic_{_dt0.now():%Y%m%d_%H%M%S}.zip"
        default_dir = str(self.cfg.db_path.parent / default_name)
        # r39d：这里**不能**用阻塞式 `QFileDialog.getSaveFileName()` —— 真机
        # 扫描稳定复现：点一次「导出诊断包…」进程就 0xC0000409 无声死亡
        # （退出码 127、gui-crash.log 空、无 Python 异常）。
        # r39e 又挖出真正的雷（见 _file_pick 的注释）：**原生 Win32 文件对话框
        # 一旦被 open() 弹出、又被 Qt 侧主动关闭（reject/accept/close/hide/done），
        # 6 种关法 3/3 全部 SIGSEGV**。所以本对话框两件事都做：走非阻塞
        # `dlg.open()` + accepted 信号（点完立刻返回，不卡 UI），并且强制
        # `DontUseNativeDialog`（关掉路径回到 Qt 自己手里，任谁关都安全）。
        self._diag_pending_path = default_dir
        # 上一轮对话框残留会一路堆下去（每点一次漏一个文件对话框），
        # 开新的之前先把旧的收掉。
        self._diag_dlg_close()
        dlg = QFileDialog(self, "导出诊断包", default_dir, "ZIP 文件 (*.zip)")
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)   # r39e：见 _file_pick
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dlg.selectFile(default_dir)
        dlg.accepted.connect(self._on_diag_bundle_picked)
        dlg.rejected.connect(self._on_diag_bundle_cancel)
        # 必须自持引用：局部变量建完 dialog 就被回收，PySide6 会把它提前删掉，
        # 后面 selectedFiles() 摸到的是悬空对象。
        self._diag_dlg = dlg
        dlg.open()          # 非阻塞；这里绝不进嵌套事件循环
        return

    def _diag_dlg_close(self):
        """收掉并释放上一轮遗留的保存对话框（r39e）。

        少这一句 = 每次导出都漏一个原生 QFileDialog：cancel 分支的
        `self._diag_dlg` 一直是 None（当初漏写赋值），accepted 分支又根本
        不碰它。真机扫描里这个动作会被点很多次，窗口一批批堆着不释放。
        """
        d = getattr(self, "_diag_dlg", None)
        if d is None:
            return
        self._diag_dlg = None
        # 先摘信号再关（r39e）：QDialog 的 close()/reject() 会把 rejected
        # 信号放出去，一放出去 `_on_diag_bundle_cancel` 就跟着跑 —— 它会把
        # 刚选好的 `_diag_pending_path` 清成 None，还递归调回本函数。
        # 真机上这就是「选了路径、打包却写去默认位置 / 什么都不写」的根因。
        for sig in ("accepted", "rejected"):
            try:
                getattr(d, sig).disconnect()
            except Exception:
                pass
        for meth in ("done", "reject", "close"):
            try:
                getattr(d, meth)(0) if meth == "done" else getattr(d, meth)()
            except Exception:
                pass
        try:
            d.setParent(None)
        except Exception:
            pass
        try:
            d.deleteLater()
        except Exception:
            pass

    def _on_diag_bundle_picked(self):
        """用户在保存对话框里选了路径 → 真正打包诊断包（r39d，非阻塞对话框）。"""
        d = getattr(self, "_diag_dlg", None)
        if d is not None:
            try:
                files = d.selectedFiles()
            except Exception:
                files = []
            if files:
                self._diag_pending_path = files[0]
        self._diag_dlg_close()      # r39e：接受了也要销毁，否则照样漏窗口
        self._build_diagnostic_bundle()

    def _on_diag_bundle_cancel(self):
        """用户取消：把路径清掉，并把对话框彻底释放。

        两个 r39d/r39e 的血泪点：① 必须走 _diag_dlg_close() 真正 destroy，
        只 `reject()` 不 `setParent(None)` + `deleteLater()` 的话，一个已
        reject 但仍挂在 self._diag_dlg 上的非模态 QFileDialog 会一直活着，
        真机扫描里它约 400s 后把进程带成 SIGSEGV（139、两个日志都空）。
        ② 取消后 _diag_pending_path 要置空，否则下一轮会被当成「已选路径」。
        """
        self._diag_pending_path = None
        self._diag_dlg_close()

    def _build_diagnostic_bundle(self):
        path = getattr(self, "_diag_pending_path", None)
        if not path:
            return
        path = Path(path)
        from PySide6.QtWidgets import QMessageBox
        import zipfile, json, re, platform as _plat, sys
        from datetime import datetime as _datetime
        _now = lambda: _datetime.now()

        # ---- 脱敏配置 ----
        cfg_d = self.cfg.to_dict()
        def mask_cred(s):
            if not isinstance(s, str) or not s:
                return s
            return re.sub(r"(://)([^/@]+):([^/@]+)@", r"\1***@", s)
        if cfg_d.get("ai_api_key"):
            cfg_d["ai_api_key"] = "***"
        for k in ("proxy", "ai_api_url"):
            if cfg_d.get(k):
                cfg_d[k] = mask_cred(cfg_d[k])

        # ---- 库统计（只读聚合，不碰图片）----
        stats: dict = {}
        try:
            q = lambda sql: self.db.query(sql)
            stats["tasks_total"] = q(
                "SELECT COUNT(*) c FROM tasks WHERE is_template=0")[0]["c"]
            stats["tasks_by_status"] = {
                r["status"]: r["c"] for r in
                q("SELECT status, COUNT(*) c FROM tasks "
                  "WHERE is_template=0 GROUP BY status")}
            stats["images_total"] = q("SELECT COUNT(*) c FROM images")[0]["c"]
            stats["images_in_library"] = q(
                "SELECT COUNT(*) c FROM images "
                "WHERE in_quarantine=0 AND blacklisted=0")[0]["c"]
            # R143：与隔离区列表 / Web 端同口径（in_quarantine=1 AND
            # needs_review=0）。原来少了 needs_review=0，统计面板的数字
            # 会比用户真正能在隔离区看到的多，正是"数量对不上"的一环。
            stats["images_quarantined"] = q(
                "SELECT COUNT(*) c FROM images "
                "WHERE in_quarantine=1 AND needs_review=0")[0]["c"]
            stats["images_favorite"] = q(
                "SELECT COUNT(*) c FROM images WHERE favorite=1")[0]["c"]
            stats["images_needs_review"] = q(
                "SELECT COUNT(*) c FROM images WHERE needs_review=1")[0]["c"]
            stats["preferences_total"] = q(
                "SELECT COUNT(*) c FROM preferences")[0]["c"]
            stats["reviews_total"] = q(
                "SELECT COUNT(*) c FROM reviews")[0]["c"]
            stats["per_task"] = [
                {"task_id": r["task_id"], "name": r["name"], "images": r["c"]}
                for r in q(
                    "SELECT t.id task_id, t.name name, COUNT(i.id) c "
                    "FROM tasks t LEFT JOIN images i ON i.task_id=t.id "
                    "WHERE t.is_template=0 "
                    "GROUP BY t.id ORDER BY c DESC LIMIT 20")]
        except Exception as e:
            stats["error"] = str(e)

        # ---- 环境 ----
        env = {
            "app": "Sweet采集AI版",
            "python": sys.version.split()[0],
            "platform": _plat.platform(),
            "machine": _plat.machine(),
            "bundled": bool(getattr(sys, "frozen", False)),
        }
        try:
            import onnxruntime as ort
            env["onnx_providers"] = ort.get_available_providers()
        except Exception:
            env["onnx_providers"] = "unavailable"

        # ---- 打包成 zip（先在内存里，需求 19：整个包要加密）----
        # 不能直接写 zip 到磁盘 —— 那样产出的还是明文 zip，等于没加密。
        # 正确顺序：内存里组好 zip → 整体 encrypt_bytes → 一次性写 .mrmx。
        try:
            import io as _io
            from .. import crypt as _crypt
            buf = _io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("config_masked.json",
                           json.dumps(cfg_d, indent=2, ensure_ascii=False))
                z.writestr("db_stats.json",
                           json.dumps(stats, indent=2, ensure_ascii=False))
                z.writestr("environment.json",
                           json.dumps(env, indent=2, ensure_ascii=False))
                log_text = ""
                try:
                    log_text = self.log_view.toPlainText()
                except Exception:
                    pass
                z.writestr("log.txt", log_text or "(no log)")
                # 数据库副本：只含元数据/统计，不含图片字节，便于远程排障
                try:
                    dest = path.with_name(path.stem + "_db.sqlite")
                    self.db.backup(dest)
                    z.write(dest, "mrm_db.sqlite")
                    dest.unlink()
                except Exception as e:
                    z.writestr("db_backup_error.txt", str(e))
            # ---- 加密后落盘（.mrmx）----
            enc = _crypt.encrypt_bytes(buf.getvalue(),
                                       self._export_passphrase())
            out = path if path.suffix.lower() == ".mrmx" else \
                path.with_name(path.name + ".mrmx")
            out.write_bytes(enc)
            QMessageBox.information(
                self, "已导出诊断包（已加密）",
                f"诊断包已保存到：\n{out}\n\n"
                "含：脱敏配置 / 库统计 / 环境信息 / 运行日志 / 数据库副本。\n"
                "不含任何图片文件，密钥已打码。\n"
                "整个包已加密（需求 19），只有本软件能打开。")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _join_threadpool(self, pool, timeout: float = 5.0):
        """有界地等一个 ThreadPoolExecutor 收工（r39d）。

        两个坑，踩过一次记一辈子：
        1. `_join_threadpool` 必须自己实现 —— `ThreadPoolExecutor.shutdown()`
           的签名是 `shutdown(wait=True, *, cancel_futures=False)`，**没有
           timeout 参数**。写 `shutdown(wait=True, timeout=5)` 会抛 TypeError，
           再被 `except Exception: pass` 一吞，池子就一直开着。上一版收尾代码
           正是这么写的，于是后台线程池从来没真正关掉过，进程退出时解释器
           finalization 还在 join 它里面跑着的 worker —— 0xC0000409，日志全空。
        2. 必须 `shutdown(wait=False)` 之后再逐个 `join()`：直接 `wait=True`
           会把调用方（这里是队列所在的 Qt 主线程）整个堵死，超时了也没法退。
        """
        threads = list(getattr(pool, "_threads", ()) or ())
        try:
            pool.shutdown(wait=False)
        except Exception:
            pass
        end = time.monotonic() + max(0.0, timeout or 0.0)
        for t in threads:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                t.join(timeout=left)
            except Exception:
                pass

    def _wait_bg_qthreads(self, budget: float = 8.0):
        """r39d：等所有后台 QThread 收工再关库。

        窗口上一共挂着 8 个后台 QThread（相似图去重 / 代理探测 / 修复图库 /
        待审核补图 / 生成缩略图 / 取 Cookie / 回填本地文件 …），它们的 run()
        里**直接读 self.db 和 self.cfg**，引用只靠 `self._xxx_worker = w` 这一行
        吊着。以前退出流程只等队列线程，不等这些 QThread：它们收工时查已关的库，
        或者进程收尾时 run() 还在跑（解释器正在清模块全局、库指针早空了），
        表现就是退出码 0xC0000409、gui-crash.log 一个字都没有 —— 实测复现 5 次。

        这里用 `vars(self)` 扫而不是写死属性名，以后新加 worker 自动覆盖。
        多个 QThread 串行 wait 会超时累加，所以统一一个 deadline。
        """
        deadline = time.monotonic() + budget
        for name, obj in list(vars(self).items()):
            if not isinstance(obj, QThread):
                continue
            try:
                if not obj.isRunning():
                    continue
                left = max(0.1, deadline - time.monotonic())
                obj.wait(int(left * 1000))
            except Exception:
                pass

    # ------------------------------------------------ R138 托盘常驻 / 唤回 ---
    def _can_reside_in_tray(self) -> bool:
        """有没有「缩到托盘」的条件（托盘在 + 没被要求真退）。"""
        return _tray_can_reside(self)

    def _hide_to_tray_on_close(self, event=None) -> bool:
        """R138 P0-A：接管一次「关闭」——能缩托盘就缩，不真退。

        返回 True = 这次关闭已被接管（closeEvent 直接返回，不做收尾）；
        返回 False = 走原来的四层安全收尾。

        ⚠ 判据用模块级 `_tray_can_reside()` 而不是 `self._can_reside_in_tray()`：
        R117 的关闭测试用的是**纯 Python 替身**（没跑 __init__），它身上没有
        这个方法；直接调会 AttributeError，替身又会把异常吞掉 ⇒ 老测试假绿/假红。
        """
        return _tray_hide_on_close(self, event)
        # R152-A10：return 之后原本留着 R138 重构前的旧实现（event.ignore +
        # _hide_to_tray + return True，永不可达），已删 —— 托盘判定统一走
        # 模块级 _tray_hide_on_close()（test_r202 用替身直接测模块级函数）。

    def _hide_to_tray(self):
        """R138：窗口藏进系统托盘，进程继续跑（下载/队列不停）。"""
        try:
            # 先清掉最小化态：否则下次 show() 出来还是「最小化」（看不见），
            # 用户会以为唤回没生效。
            if self.isMinimized():
                self.setWindowState(
                    self.windowState() & ~Qt.WindowState.WindowMinimized)
        except Exception:
            pass
        try:
            self.hide()
        except Exception:
            pass
        try:
            self._refresh_tray_tooltip()
        except Exception:
            pass
        try:
            from PySide6.QtWidgets import QSystemTrayIcon as _TrayIcon
            tray = getattr(self, "tray", None)
            if tray is not None:
                tray.showMessage(
                    "Sweet采集AI版",
                    "已缩到系统托盘，程序在后台继续运行。\n"
                    "单击或双击托盘图标可唤回窗口。",
                    _TrayIcon.MessageIcon.Information, 4000)
        except Exception:
            pass

    def show(self):
        """R138：从托盘唤回时要**真的**把窗口摆到屏幕上。

        `QWidget.show()` 不是虚函数，这个覆盖只影响 Python 侧的 `win.show()`
        调用（托盘「显示主窗口」走的就是它），不碰 Qt 内部的显隐派发。
        「最小化后隐藏」的窗口光 show() 还是最小化的 —— 用户看不见，
        所以这里顺手清掉最小化态并抬到最前。
        """
        try:
            st = self.windowState()
            if st & Qt.WindowState.WindowMinimized:
                self.setWindowState(st & ~Qt.WindowState.WindowMinimized)
        except Exception:
            pass
        try:
            # 用 QMainWindow.show(self) 而不是 super().show()：后者要求 self
            # 真的是 MainWindow 实例，替身/子类上会抛 TypeError（测试里出现过）。
            QMainWindow.show(self)
        except Exception:
            pass
        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            pass
        try:
            self._refresh_tray_tooltip()
        except Exception:
            pass

    # R155-A：changeEvent 的「窗口最小化→藏托盘」劫持已删除。
    # 用户原话：「最小化和关闭都是最小化到托盘，这个不对。最小化就是最小化到
    # 任务栏，关闭才是最小化到托盘。」⇒ 点 - = 标准行为（缩任务栏），
    # 只有 closeEvent（_hide_to_tray_on_close）才缩托盘。Qt 基类实现即可，
    # 不需要自己接 changeEvent。

    def _refresh_tray_tooltip(self):
        """R138 P1-C：托盘提示反映「几个任务在跑 / 是不是后台常驻」。"""
        tray = getattr(self, "tray", None)
        if tray is None:
            return
        try:
            from .tray import update_tray_tooltip
        except Exception:
            return
        n = 0
        try:
            q = getattr(self, "_queue", None)
            if q is not None:
                n = len(getattr(q, "running", ()) or ())
        except Exception:
            n = 0
        try:
            resident = not bool(self.isVisible())
        except Exception:
            resident = False
        update_tray_tooltip(tray, running=n, resident=resident)

    def _start_tray_status_timer(self, interval_ms: int = 3000):
        """R138：托盘提示的状态刷新定时器（托盘不在就什么都不做）。"""
        if getattr(self, "tray", None) is None:
            return
        try:
            t = QTimer(self)
            t.setInterval(int(interval_ms))
            t.timeout.connect(self._refresh_tray_tooltip)
            t.start()
            self._tray_tip_timer = t
        except Exception:
            pass

    def request_quit(self):
        """R138：明确退出。走**四层安全收尾**再退，不绕过 closeEvent。

        原来托盘「退出」直接 `app.quit()` —— QApplication.quit() **不会**派发
        closeEvent，四层收尾（发停止信号 / 停三路后台 / 等 QThread / 关库）
        整段被跳过，正是 R137 红线 5 禁止的形态。这里改成先 `close()`
        （closeEvent 收尾），再 quit 让事件循环返回。
        """
        # 2026-10-27（编号 5）：有任务在跑时先确认。
        # 为什么放 request_quit 而不是 closeEvent：closeEvent 平时是「缩到托盘」，
        # 只有明确退出才真的收尾 —— 确认框必须挂在**真退出**这条路上，
        # 否则用户每次关窗都被问一次会很烦。
        # ⚠ 必须 getattr(self, "cfg", None)：本方法会被**纯 Python 替身**调用
        # （test_r202 用 SimpleNamespace 测退出路径），替身上没有 cfg。
        if bool(getattr(getattr(self, "cfg", None), "confirm_exit_running", True)):
            try:
                _snap = self._queue.snapshot() if getattr(self, "_queue", None) else {}
                _running = list((_snap or {}).get("running") or {})
                _queued = 0
                try:
                    _queued = len(self._queue.queued())
                except Exception:
                    _queued = 0
                if _running or _queued:
                    from PySide6.QtWidgets import QMessageBox
                    _ans = QMessageBox.question(
                        self, "确认退出",
                        f"还有 {len(_running)} 个任务在跑、{_queued} 个排队。\n"
                        "现在退出会中断它们（已下载的图会保留）。\n\n确定退出吗？",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No)
                    if _ans != QMessageBox.StandardButton.Yes:
                        return
            except Exception:
                pass                      # 查询失败就别拦用户退出
        self._explicit_quit = True
        try:
            self.close()
        except Exception:
            pass
        try:
            app = QApplication.instance()
            if app is not None:
                app.quit()
        except Exception:
            pass
        # ---- R185：收尾之后**立即彻底退出** ----
        # 托盘「退出」与菜单「退出」共用这一个出口（订单第 2 条）：
        # 杀干净子进程（gallery-dl 黑窗/残留下载器）→ os._exit(0)。
        # 不交给解释器 finalization：它会在还有后台线程时把退出拖到未知时刻，
        # 甚至 0xC0000409 无声死亡（r39d 的老账）。
        try:
            from .lifecycle import force_quit
            force_quit(self)
        except Exception:
            # force_quit 正常路径不返回（hard_exit）；走到这里说明它自己
            # 炸了，那就退回老路径，至少别把用户卡在「点了退出没反应」。
            # R71：os._exit 只允许出现在 lifecycle.py —— 兜底也走 hard_exit。
            try:
                from .lifecycle import hard_exit
                hard_exit()
            except Exception:
                pass

    def closeEvent(self, event):
        """退出收尾（r39c）。

        真机 30 分钟按钮扫描死过一次：进程在 app.exec() 返回之后、
        Qt 对象销毁阶段以 0xC0000409（STATUS_STACK_BUFFER_OVERRUN）无声死亡，
        closeEvent 自己倒是完整跑完了（CE-6 打出来了）。所以崩的是「后台线程
        还在碰已经收摊的 UI」——三路后台（QThreadPool 缩略图解码、新加的
        _bg_pool 线程池、engine 自持线程池）在退出时都还没停干净。
        这里统一给它们收尾，并把「已经回来了的解码结果」丢掉。

        R117 第6项（有任务在跑时点关闭，卡很久）：
        原来这里是**一串无上限的串行等待** ——
        `_thumb_pool.waitForDone(5000)` + `QTTP.globalInstance().waitForDone(2000)`
        + `_join_threadpool(5.0)` + 队列 `th.join(8)` + `_wait_bg_qthreads(8.0)`
        ＝ 最坏 **28 秒**，而且**全程不告诉正在跑的任务「要退了」**，
        它们继续下载/写库，join 自然等不到 → 用户看到的就是「点 X 后界面僵住」。
        修法（保留四层安全收尾，只把「等」变成有界的）：
          ① **先发停止信号**：一进来就给运行中的任务 `stop_all()`，
             让它们自己收工 —— 这是「等得久」的真正解法（不打断下载、
             让对方走完当前的原子步骤，而不是被硬杀）。
          ② **共用一个截止时刻** `_CLOSE_BUDGET_S`：每层等待都按
             「还剩多少预算」传进去，而不是各等各的固定秒数。
             28s → 最坏 6s，且不会因为任何一层卡住而叠加。

        R138 P0-A：默认行为改成「点 X = 缩到托盘常驻」，
        只有 `_explicit_quit=True`（托盘「退出」/ 设置菜单「完全退出」）才走到
        下面这套四层收尾并真退。没有托盘时（冒烟/测试/无托盘环境）行为不变。
        R155-A：**最小化（-）不在此列**——它走 Qt 标准行为缩任务栏，
        changeEvent 里那段「最小化也藏托盘」的劫持已删除。
        """
        # ---- ⓪ 默认缩托盘：不收尾、不关库、进程继续跑 ----
        if _tray_hide_on_close(self, event):
            return
        self._closing = True
        # ---- ① 先让正在跑的任务知道「要退了」（R117 第6项）----
        # 放在最前面：拖得越久，下载/写库越可能撞上已关的库。
        try:
            if self._queue:
                self._queue.stop_all()
        except Exception:
            pass
        # 2026-10-27（编号 13）：先停监视线程 —— 它会在退出过程中继续建任务，
        # 而那些任务会去碰已经要关的库（同 R117「退出时后台还在动」的老坑）。
        try:
            self._stop_watch_folder()
        except Exception:
            pass
        # ---- ② 全程共用一个截止时刻，不再各等各的 ----
        import time as _time
        _deadline = _time.monotonic() + self._CLOSE_BUDGET_S

        def _left(default: float) -> int:
            """这一层还能等多久（ms）。用完就 0 —— 绝不因为上一层慢而叠加。"""
            try:
                return max(0, int((_deadline - _time.monotonic()) * 1000))
            except Exception:
                return int(default * 1000)

        try:
            # r37：停掉分帧补缩略图的定时器，别让它在关库后继续解图
            self._stop_thumb_pump()
        except Exception:
            pass
        try:
            self._save_ui_state()
        except Exception:
            pass
        # ---- 三路后台统统收工（每层只等「剩下的预算」）----
        try:
            pool = getattr(self, "_thumb_pool", None)
            if pool is not None:
                # 给在飞的 avif/webp 解码收工（waitForDone 会阻塞主线程，
                # 所以只在关窗口这一刻做；运行期靠 _pump_thumbs 的每帧预算兜着）
                pool.waitForDone(_left(5.0))
        except Exception:
            pass
        try:
            from PySide6.QtCore import QThreadPool as _QTTP
            _QTTP.globalInstance().waitForDone(_left(2.0))
        except Exception:
            pass
        try:
            # r39c：**必须 wait**，且只等预算内的份额。
            # 现场死因（看门狗扒到的主线程栈）：
            #   threading.join <- concurrent.futures.thread._python_exit
            #   <- threading._shutdown
            # 解释器收尾时会 join 线程池里还在跑的 worker，而那个 worker 收工
            # 时要碰数据库 / 写日志 —— 那时模块全局已经被清干净，它一碰就是
            # 一条没人接的异常把它和整个进程一起带走（退出码 0xC0000409，
            # 退出前 Python 层的日志一个字都没有）。
            # 所以必须在 Qt 还活着、主线程还能打日志的时候把它们收干净。
            bg = getattr(self, "_bg_pool", None)
            if bg is not None:
                # r39d：**必须真的等它收工**（原来只调 shutdown 是不等的，
                # 而且 shutdown(wait=True, timeout=5) 那个 timeout 根本不存在，
                # 见 _join_threadpool 的注释 —— 这就是 0xC0000409 拖五轮没根治的原因）
                self._join_threadpool(bg, timeout=max(0.0, _left(5.0) / 1000.0))
        except Exception:
            pass
        # 还在飞的解码结果一律丢弃（item 可能已经销毁，setData 到悬空
        # QStandardItem 就是上面那次 0xC0000409 的元凶形态）。
        # _stop_thumb_pump(clear_pending=True) 已经把 _thumb_slots 清空了，
        # 这里再兜一次，防止中间又塞进来。
        try:
            self._stop_thumb_pump(clear_pending=True)
        except Exception:
            pass
        try:
            if self._queue:
                self._queue.stop()
                # 等队列线程真正退出再关数据库，否则它收尾时还会查询已关的库
                # （现象：关窗口时日志报「Cannot operate on a closed database」）。
                # r35：引擎的 worker 协程是 queue 线程里 asyncio 的子任务，join
                # 超时说明它们还没退（可能卡在下载）—— 但① 已经先发过停止信号，
                # 它们会自己收工，这里只按预算等，超时就照样往下走（关库）。
                # worker 自己也查 `db.is_closed()` 提前收工（见 engine.py）。
                th = getattr(self._queue, "_thread", None)
                if th is not None and th.is_alive():
                    th.join(timeout=max(0.0, _left(8.0) / 1000.0))
        except Exception:
            pass
        # r39d：先等后台 QThread 收工，再关库（它们的 run() 里直接读 self.db）。
        try:
            self._wait_bg_qthreads(budget=max(0.0, _left(8.0) / 1000.0))
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        super().closeEvent(event)


def _ensure_single_instance(app) -> bool:
    """单实例：已有实例在跑就提示并退出（防双开）。

    2026-10-29：连上已有实例时把 socket 留在 ``app._single_sock`` 上，
    让 launch() 能给它发一句「把窗口叫出来」——
    否则「启动后最小化到托盘」的用户再双击图标，只会得到一句
    「已在运行」的弹框，而窗口还是不出来（比不弹更让人困惑）。
    """
    from PySide6.QtNetwork import QLocalSocket, QLocalServer
    key = "sweet-caiji-single-instance"
    sock = QLocalSocket()
    sock.connectToServer(key)
    if sock.waitForConnected(300):
        app._single_sock = sock              # 留着给 launch() 发唤醒指令
        return False
    QLocalServer.removeServer(key)      # 清理上次异常退出残留
    srv = QLocalServer()
    srv.listen(key)
    app._single_srv = srv               # 持引用防 GC
    return True


def _attach_single_instance_wakeup(app, win):
    """新实例连上来 ⇒ 把已有窗口叫到最前（qBittorrent 同款行为）。"""
    srv = getattr(app, "_single_srv", None)
    if srv is None or win is None:
        return

    def _on_new_connection():
        # 必须把连接收干净：不收的话 QLocalServer 会一直挂着它，
        # 第二次双击就连不上（表现：第一次能唤回，之后就永远"已在运行"）。
        try:
            while srv.hasPendingConnections():
                c = srv.nextPendingConnection()
                try:
                    c.readAll()
                except Exception:
                    pass
                try:
                    c.disconnectFromServer()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            from PySide6.QtCore import Qt as _Qt
            if win.isMinimized():
                win.setWindowState(win.windowState() & ~_Qt.WindowState.WindowMinimized)
            win.show()
            win.raise_()
            win.activateWindow()
        except Exception:
            pass

    try:
        srv.newConnection.connect(_on_new_connection)
        app._single_wake = _on_new_connection   # 持引用（Qt 侧连接不保活 Python 函数）
    except Exception:
        pass


def launch():
    app = QApplication.instance() or QApplication([])
    if not _ensure_single_instance(app):
        # 已有实例：让它把窗口叫出来，然后本进程安静退出
        sock = getattr(app, "_single_sock", None)
        if sock is not None:
            try:
                sock.write(b"show")
                sock.flush()
                sock.waitForBytesWritten(300)
                sock.disconnectFromServer()
            except Exception:
                pass
        else:                                   # pragma: no cover - 连接断开后的兜底
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                None, "已在运行",
                "Sweet采集AI版 已经在运行了（不能双开）。\n"
                "请使用已打开的那个窗口。")
        return
    from . import theme
    from .tray import build_tray
    _cfg = load_config()
    theme.apply_preset(app, getattr(_cfg, "ui_preset", None))
    # 2026-10-29（qBittorrent 对标）：启动后直接进托盘。
    # 只读一次配置：启动流程里下面还要判断闪屏，读两次万一中途被别的进程改了
    # 就会出现「没闪屏但也没显示窗口」这种半截状态。
    _start_min = bool(getattr(_cfg, "tray_start_minimized", False))
    # 2026-10-27（编号 9）：启动画面。主窗口构造要读库/建表格（几秒），
    # 这段时间原来是一块白屏 —— 用户会以为没启动成功又双击一次
    # （然后被单实例拦住，观感更差）。这里先亮一块带文字的图，构造完再收。
    _splash = None
    try:
        if (bool(getattr(_cfg, "splash_enabled", True))
                and not _start_min):        # 进托盘时不闪一下屏，闪完又消失像出错
            from PySide6.QtCore import Qt
            from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
            from PySide6.QtWidgets import QSplashScreen
            # ⚠ 自绘也不能写死色值：test_p14b 会扫全仓硬编码 hex
            # （硬编码的色在换皮肤时不会跟着变）。统一走 theme.c()。
            from . import theme as _th
            # 2026-10-29（用户第 16 条）：原来这里是 `c(True, "bg")` —— 传的是
            # **深色**调色板，所以不管用什么皮肤，启动画面都是黑底。
            # 用户的原话是「不应该是白底加 logo，底下写几个字正在启动吗」。
            # 现在：浅色底 + 品牌色圆角块（logo 本身是浅色线条，直接放白底上
            # 几乎看不见，垫一块品牌色才立得住）+ 下面一行小字。
            _pm = QPixmap(480, 300)
            _pm.fill(QColor(_th.c(False, "bg")))
            _p = QPainter(_pm)
            _p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            _tile = 116
            _tx = (_pm.width() - _tile) // 2
            _ty = 64
            _p.setPen(Qt.PenStyle.NoPen)
            _p.setBrush(QColor(_th.c(False, "accent")))
            _p.drawRoundedRect(_tx, _ty, _tile, _tile, 28, 28)
            # 2026-10-28（用户要求）：文字换成 logo。
            # ⚠ 取不到 logo 时**必须退回原来的文字**：空白闪屏比没闪屏更吓人。
            _drew_logo = False
            try:
                from ..resources import logo_path
                _lp = logo_path()
                if _lp is not None and _lp.exists():
                    _raw = QPixmap(str(_lp))
                    if not _raw.isNull():
                        side = 88
                        _logo = _raw.scaled(
                            side, side,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
                        _p.drawPixmap(
                            _tx + (_tile - _logo.width()) // 2,
                            _ty + (_tile - _logo.height()) // 2,
                            _logo)
                        _drew_logo = True
            except Exception:
                _drew_logo = False
            if not _drew_logo:
                _p.setPen(QColor(_th.c(False, "text")))
                _f = QFont()
                _f.setPointSize(22)
                _f.setBold(True)
                _p.setFont(_f)
                _p.drawText(_pm.rect().adjusted(0, -60, 0, 0),
                            int(Qt.AlignmentFlag.AlignCenter), "Sweet 采集AI版")
            # 底部一行「正在启动…」：用正文色的 60% 透明度，而不是写死一个灰值
            # （调色板里没有 muted 键，写死会在深色底上糊成一片）。
            _f2 = QFont()
            _f2.setPointSize(11)
            _p.setFont(_f2)
            _muted = QColor(_th.c(False, "text"))
            _muted.setAlpha(160)
            _p.setPen(_muted)
            _p.drawText(_pm.rect().adjusted(0, 148, 0, 0),
                        int(Qt.AlignmentFlag.AlignCenter), "正在启动…")
            _p.end()
            _splash = QSplashScreen(_pm)
            _splash.show()
            app.processEvents()
    except Exception:
        _splash = None
    win = MainWindow()

    def _show_main():
        """唤回主窗口（托盘单击/双击、第二个实例双击图标都走这里）。

        顺带补一次新手向导：勾了「启动后最小化到托盘」的首启用户，
        启动那段没窗口可挂向导，等他第一次打开窗口时再补，不然永远看不到。
        """
        try:
            win.show()
            win.raise_()
            win.activateWindow()
        except Exception:
            pass
        try:
            win.maybe_show_onboarding()
        except Exception:
            pass

    # R137 红线 5：退出项必须走四层收尾 ⇒ 挂 win.request_quit（内部 close()
    # 触发 closeEvent），不再直接挂 app.quit（它不派发 closeEvent）。
    tray = build_tray(app, _show_main, win.toggle_pause, win.request_quit,
                      on_start_all=win.run_all_quiet,
                      on_stop_all=win.stop_all_quiet,
                      on_hide=win._hide_to_tray)
    try:
        tray.show()
    except Exception:
        pass
    # R138：只有托盘**真的显示出来了**才允许「缩托盘常驻」并关掉
    # setQuitOnLastWindowClosed。否则窗口一藏、托盘又看不见，程序就成了
    # 既叫不回来也退不掉的幽灵（只能任务管理器杀）。这时保持旧行为：关=真退。
    _resident = False
    try:
        from PySide6.QtWidgets import QSystemTrayIcon as _TI
        _resident = (bool(_TI.isSystemTrayAvailable())
                     and bool(tray.isVisible()))
    except Exception:
        _resident = False
    win.tray = tray if _resident else None   # notifications come through here
    try:
        if _splash is not None:
            _splash.finish(win)              # 主窗口已 show，splash 让位
    except Exception:
        pass
    try:
        app.setQuitOnLastWindowClosed(not _resident)
    except Exception:
        pass
    # 2026-10-29：启动时直接藏进托盘。注意这**不是**「点最小化就缩托盘」
    # （R155 已否决：最小化按钮仍是标准行为，缩任务栏）。
    # ⚠ 只有托盘**确实可用**（_resident）才敢藏窗口；否则窗口一藏，
    #   既叫不回来也退不掉，只能任务管理器杀 —— 那就照常显示窗口。
    _started_hidden = bool(_start_min and _resident)
    if _started_hidden:
        try:
            from PySide6.QtWidgets import QSystemTrayIcon as _TI
            tray.showMessage(
                "Sweet采集AI版",
                "已在后台启动。点托盘图标可打开主窗口，右键有「退出」。",
                _TI.MessageIcon.Information, 5000)
        except Exception:
            pass
    else:
        _show_main()
    _attach_single_instance_wakeup(app, win)
    win._start_tray_status_timer()
    app.exec()
    # r39d：所有收尾（存 UI 状态 / 停三路后台 / 等 QThread 收工 / 关库）都已经在
    # closeEvent 里显式做完了。这里**照常返回** —— 硬退出（os._exit）交给
    # mrm_app.main() 去做：launch() 是公开入口，冒烟测试要靠它正常返回后
    # 再 print 一句 LAUNCH SMOKE OK，在这里 _exit 会把那行输出吞掉。
