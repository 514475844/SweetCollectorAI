"""qBittorrent 式「传输列表」—— 原生高密度任务表（NATIVE_SKIN_ORDER §2）。

═══════════════════════════════════════════════════════════════════════
为什么不用「每行一个 QWidget」
═══════════════════════════════════════════════════════════════════════
卡片版（`main_window.TaskRow`）是每行一个 QWidget，里面还嵌3 个
QPushButton + 2 个 QLabel。200 个任务 = 200 个容器 + 1000 个子控件，
构造一次就把主线程堵死几百毫秒（这正是 L2 增量刷新要救的场景）。
qBittorrent 的传输列表是 **QTreeView + QStyledItemDelegate**：整张表只有
一套 view + 一套 delegate，行是**数据**不是控件，所以加行几乎不花钱。

本模块就是照这个做法做的，且**不删 TaskRow** —— 两套并存，由
`theme.LAYOUTS[key]["task_view"]` 决定哪套可见（"cards" / "table"）。

═══════════════════════════════════════════════════════════════════════
⚠ 五条设计铁律（改之前先读，改完请一并更新本段）
═══════════════════════════════════════════════════════════════════════
1. **进度条必须交给系统样式画**（`QStyleOptionProgressBar` +
   `style().drawControl(CE_ProgressBar, ...)`），不许自己拿 QPainter 画
   圆角矩形。理由：自己画出来的是「在 Windows 上假装像 Windows」，
   跟真原生的滚动条 / 复选框放在一起反而更刺眼；而且系统样式会跟着
   用户的主题、高对比度、缩放走，自己画不会。这是本单存在的**唯一理由**。
   （护栏 `test_progress_bar_is_drawn_by_the_system_style`钉死这条。）
2. **颜色一律走 QPalette**，不许写死 hex。本项目有 P1-4 静态护栏
   （`test_p14b_skin_convergence`）扫描全仓裸 hex，本模块一个都不写。
   ⚠ 注意别把QPalette 换成 setStyleSheet —— R60 已定：真原生模式下
   `theme.css()` 返回空串，行内样式必须**整体失效**；表格是原生件，
   本来就不该有任何行内 QSS。
3. **不许编数字**（R74 换了落点，但原则没变）。本项目的抓取是流式 BFS，
   运行中根本不知道总共会有多少张图—— 任何「还剩 3 分 20 秒」「已完成
   37%」都是编的，而用户会当真（这是进度类 UI 最伤信任的一种假）。
   ⚠ R74 按作业单删掉了「剩余(ETA)」列（理由正是「总量未知，ETA 无意义」）
   ⇒ 「编数字」这个风险**从 ETA 转移到了进度/速度两列**：进度算不出时
   必须是**不确定态**（busy + pct=None，画活动条），速度算不出时必须是
   **空串**而不是 "0"。护栏 `test_no_number_is_ever_fabricated` 钉死这一条
   ——它比原来那条更强：原来只判 ETA 一个格子，现在判**整行所有格子**。
4. **不许在单元格里塞真控件**（那等于又回到每行一个 widget）。
   ⚠ R74 按作业单删掉了「操作」列（操作改由**右键菜单**承担，见
   `main_window._task_menu`）⇒ 画按钮/命中判定那套（`ActionDelegate` +
   `action_rects`）随之整体删除。
   ⚠⚠ **这一条删除本身有陷阱，护栏 `test_start_stop_still_reachable_
   without_the_actions_column` 就是为它写的**：行内按钮曾是 table 视图
   （classic / qbittorrent 两个皮肤）**唯一**的启停入口，撤掉它若不同步
   确认右键菜单里还有「运行/停止」，这两个皮肤就会**静默丢失启停能力**
   —— 不崩溃、不报错，只是菜单项没了。这与 R66 撤 `self._tb` 丢掉
   qB 皮肤全部动作入口是**完全同型**的坑。
5. **不许写死毫秒数/字面阈值**（进度百分比、速度小数位、行高都是
   「量」不是「契约」）。要断言就断言**关系**，别把今天的取值钉死
   ——产品一改需求就集体假失败，且看起来像真回归。

═══════════════════════════════════════════════════════════════════════
与主窗口的接线（钩子在 main_window.py，这里只列名字）
═══════════════════════════════════════════════════════════════════════
· 建壳：`MainWindow.__init__` 里与 `task_list` **并排**建出本控件，
  两者都塞进同一个 `_vsplit`，靠显隐切换（与 toolbar/sidebar 同一套路：
  **绝不重建窗口**，重建会丢三页状态）。
· 刷新：`MainWindow._refresh_task_table()`。
· 显隐：`MainWindow._apply_layout_visibility()`（并处理「被跳过的那套
  变脏」的补偿，见 main_window 里 `_task_table_dirty` 的注释）。
"""
from __future__ import annotations

from typing import Optional

# ⚠ R74：QRect / QSize / QStyleOptionButton 三个 import 随「操作」列删除
#   —— 它们在本模块里只被 `action_rects` / `ActionDelegate` 用。
#   不删会变成静态检查噪音，也更容易让人以为还有代码在画按钮。
from PySide6.QtCore import (Qt, QAbstractTableModel, QModelIndex, QPoint,
                            Signal)
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QApplication, QAbstractItemView, QHeaderView,
                               QMenu, QStyle, QStyledItemDelegate,
                               QStyleOptionProgressBar,
                               QTableView)


# ══════════════════════════════════════════════════════════════════
# 列定义
# ══════════════════════════════════════════════════════════════════
# (key, 表头, 默认宽度, 对齐)
#
# ⚠ 顺序即默认显示顺序。
# ⚠⚠ **R74 按作业单删掉了两列**（TONIGHT_FINAL_ORDER 第 1、2 项）：
#   · 「剩余(ETA)」—— 抓取是流式 BFS，**运行时总量永远未知**，
#     绝大多数行会永远显示 "—"。用户看到的是一个恒为空却占 76px 的列，
#     而真想知道还剩多少，唯一靠谱的做法是看已保存张数。
#   · 「操作」—— 表内「开始/停止」两个按钮。作业单明写「所有模式/所有
#     皮肤」都删，启停改由**右键菜单**承担。
#     ⚠ 删它有陷阱：table 视图（classic / qbittorrent）当时**只有**这一条
#     启停路径，必须同步确认右键菜单里有等价项（护栏见规则 4）。
# 剩下一条工程约束：
#   · 「名称」是唯一 stretch 的列 —— 窗口拉宽时它吃掉全部余量，
#     数字列保持定宽（数字列被拉宽会看起来很怪，qB 也是定宽的）。
COLUMNS = [
    ("idx",      "编号",    44,  Qt.AlignmentFlag.AlignRight),
    ("name",     "名称",    260,  Qt.AlignmentFlag.AlignLeft),
    ("progress", "进度",    130,  Qt.AlignmentFlag.AlignCenter),
    ("status",   "状态",     72,  Qt.AlignmentFlag.AlignCenter),
    ("saved",    "已保存",   76,  Qt.AlignmentFlag.AlignRight),
    ("rate",     "速度",     80,  Qt.AlignmentFlag.AlignRight),
]
COL_IDX, COL_NAME, COL_PROGRESS, COL_STATUS = 0, 1, 2, 3
COL_SAVED, COL_RATE = 4, 5
NCOLS = len(COLUMNS)
COL_KEYS = [c[0] for c in COLUMNS]

# qB 观感的行高。作业单给的是 22~26px，取中间值 24：再扁系统字体就
# 切掉了，再高就不像传输列表了。
ROW_HEIGHT = 24

# 「名称」列不许隐藏 —— 没有它这表格就没有意义了。
# ⚠ R74：原先这里还有 "eta"（「操作」本来就不在隐藏列表里），随ETA 列一起删。
_HIDEABLE = ("idx", "progress", "status", "saved", "rate")


def _fmt_count(n: int) -> str:
    """大数字加千分位：12345 → "12,345"（qB 的传输列表也是这么写的）。"""
    return f"{int(n):,}"


def _fmt_rate(rate: Optional[float]) -> str:
    """速度（张/秒）。**算不出就返回空串**，不返回 "0" ——
    「0 张/秒」和「没数据」在 UI 上是两回事，混起来会误导用户。"""
    if rate is None:
        return ""
    if rate >= 10:
        return f"{rate:.0f}/s"
    if rate >= 1:
        return f"{rate:.1f}/s"
    return f"{rate:.2f}/s"


# ⚠ R74：`_fmt_dur`（时长格式化）随「剩余」列一起删除了。
#   它在本模块里**只有** ETA 列一个调用点，留着就是死代码；
#   而死代码比没有更糟 —— 下一个人会以为「时长显示」这个能力还在。
#   「算不出就显示 —」的原则仍在 `_fmt_rate`（空串）与 pct（不确定态）上。


# ══════════════════════════════════════════════════════════════════
# 纯数据层：任务 → 行数据（不碰 Qt，可单独测）
# ══════════════════════════════════════════════════════════════════
# 2026-10-24：状态列原来直接显示枚举英文（idle/error/running…），用户
# 明确要求全中文。逻辑判定仍用原始值，显示统一走这张表。
_STATUS_CN = {
    "idle": "空闲",
    "running": "运行中",
    "done": "已完成",
    "error": "出错",
    "paused": "已暂停",
    "stopped": "已停止",
    "queued": "排队中",
}


def build_rows(tasks, reports=None, live=None) -> list[dict]:
    """把任务列表 + 队列报告转成表格行。**纯函数，不碰 Qt。**

    `reports` 是 `queue.reports`（task_id → 上次运行的 report dict）。
    没有 report 的任务（从没跑过 / 正在跑）能拿到的信息就只有状态，
    那就只显示状态，进度/速度/ETA 一律空着 —— 见铁律 3。

    `live` 是 GUI 侧每秒采样的实时进度（task_id → {"saved": int,
    "rate": float|None}）：运行中 reports 是空的（队列只在跑完才写），
    没有 live 的话「已保存/速度」两列在运行中也永远是空的 —— 用户
    "从来没见过这两列有数据"说的就是它。跑完/空闲时以 reports 为准。

    ⚠ 「已保存」的口径：运行中=live.saved（images 表实时计数），
    其余=报告里的 downloaded（上次跑完真的存下来的张数）。
    """
    reports = reports or {}
    live = live or {}
    rows = []
    for i, t in enumerate(tasks, start=1):
        rep = reports.get(getattr(t, "id", None)) or {}
        status = getattr(getattr(t, "status", None), "value", "") or ""
        downloaded = int(rep.get("downloaded", 0) or 0)
        found = int(rep.get("images_found", 0) or 0)
        elapsed = float(rep.get("elapsed", 0) or 0)

        # 2026-10-24：「编号」列显示**真实任务 ID**（原来是行序 1..N）。
        # 任务删过后 ID 有断档（如 4/14/15 已删），行序和日志/通知里的
        # 「任务 #25」用的是两套编号 → 用户看到"列表 24 个、日志却说
        # #27"以为少了任务。统一成真实 ID 后三处（表格/日志/弹窗）一致。
        # 没有 id 的对象（测试手工造行）才退回行序。
        _tid = getattr(t, "id", None)

        # 运行中（没有 report）用实时采样补「已保存/速度」
        lv = live.get(getattr(t, "id", None)) or {}
        live_saved = lv.get("saved")
        live_rate = lv.get("rate")
        if status == "running" and live_saved is not None:
            saved = int(live_saved)
            rate = live_rate
        else:
            saved = downloaded
            rate = (downloaded / elapsed) if (elapsed > 0 and downloaded > 0) else None
        # ⚠ R74：这里原本还算 eta（剩余时间），随「剩余」列一起删了。
        #   理由不是「算不出来」—— found > downloaded 时它是真能算的，
        #   而是「绝大多数时候算不出来」：流式 BFS 运行中 found 恒为 0，
        #   于是一整列长期显示 "—"。留着它比删掉它更糟：用户会以为
        #   那一列坏了，或者更糟 —— 以为偶尔出现的一个数字是真的。
        #   「不许编数字」的原则没消失，转移到 pct（不确定态）与 rate
        #   （空串而非 "0"）上，见模块规则 3。

        # 进度百分比：done ⇒ 满；其余有 found 的按 已存/发现 算
        # （能反映失败率）；两者都没有 ⇒ None（画活动条，见 paint）。
        if status == "done":
            pct = 100
        elif found > 0:
            pct = int(round(downloaded * 100.0 / found))
        else:
            pct = None

        rows.append({
            "id": getattr(t, "id", None),
            "idx": _tid if _tid is not None else i,
            "name": getattr(t, "name", "") or "",
            "status": status,
            "status_cn": _STATUS_CN.get(status, status or "未知"),
            "saved": saved,
            "rate": rate,
            "pct": pct,
            # 出错原因（中文简短版）：卡片版一直有，表格版原来只有英文
            # "error" 两个字，用户问"出错去哪儿看原因"说的就是它。
            "err": (getattr(t, "last_error", "") or "") if status == "error" else "",
            # 「不确定态」= 正在跑但还不知道总量 —— 唯一该画活动条的情况
            "busy": status == "running" and pct is None,
        })
    return rows


# ══════════════════════════════════════════════════════════════════
# 模型
# ══════════════════════════════════════════════════════════════════
class TaskTableModel(QAbstractTableModel):
    """扁平表模型（无层级）—— qB 的传输列表本来就是平表。

    ⚠ 刷新走 `set_rows()`：内部比对行指纹，**没变就一个信号都不发**。
    这不是微优化 —— `refresh()` 被队列事件高频触发，每次都 reset 会连带
    让 view 重画全部行（用户看到的就是滚动时卡顿）。
    """

    # 自定义角色：让委托 / view 拿结构化数据，而不是去解析显示文本
    ROLE_ROW = Qt.ItemDataRole.UserRole + 1
    ROLE_PCT = Qt.ItemDataRole.UserRole + 2
    ROLE_BUSY = Qt.ItemDataRole.UserRole + 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._fp = ()

    # ---- 基本接口 ----
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else NCOLS

    def headerData(self, section, orientation,
                   role=Qt.ItemDataRole.DisplayRole):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < NCOLS:
                return COLUMNS[section][1]
            return None
        return str(section + 1)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == self.ROLE_ROW:
            return row
        if role == self.ROLE_PCT:
            return row["pct"]
        if role == self.ROLE_BUSY:
            return row["busy"]
        if role == Qt.ItemDataRole.UserRole:
            return row["id"] if col == COL_IDX else None
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_IDX:
                return str(row["idx"])
            if col == COL_NAME:
                return row["name"]
            if col == COL_PROGRESS:
                # 文字由委托画在进度条上，这里留空免得画两遍
                return ""
            if col == COL_STATUS:
                # .get() 防御手工造行（测试/旧调用方可能没有 status_cn 键）
                return row.get("status_cn") or row.get("status") or ""
            if col == COL_SAVED:
                return _fmt_count(row["saved"]) if row["saved"] else ""
            if col == COL_RATE:
                return _fmt_rate(row["rate"])
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(COLUMNS[col][3])
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(row)
        return None

    @staticmethod
    def _tooltip(row) -> str:
        """悬停提示：把「算不出」的那几个格子讲清楚，否则用户以为坏了。"""
        bits = [row["name"] or "（未命名）"]
        if row.get("err"):
            # 2026-10-24：出错任务把中文原因写进 tooltip —— 表格原来只有
            # 英文 "error"，用户根本不知道去哪儿看为什么失败。
            try:
                from ..stats import short_reason
                bits.append(f"失败原因：{short_reason(row['err'])}")
            except Exception:
                bits.append(f"失败原因：{row['err'][:60]}")
            bits.append("（右键 → 查看错误详情 可看完整信息）")
        if row["busy"]:
            bits.append("正在抓取（总量未知，完成后显示进度）")
        elif not row["pct"] and row["pct"] != 0:
            # ⚠ R74：原来这里还有一句「剩余时间：总量未知，无法估算」，
            #   那是在解释已删除的 ETA 列 —— 列都不在了，解释它只会
            #   让用户去找一个不存在的列。改成解释「进度为什么没有数」。
            bits.append("总量未知，暂时算不出进度")
        return "\n".join(bits)

    # ---- 刷新 ----
    @staticmethod
    def _fingerprint(rows) -> tuple:
        """行指纹。⚠ 只取**显示得出来的字段** —— 取多了会让「内容变了但
        屏幕上一样」的行也触发重画，取少了会让该重画的行不重画。"""
        # ⚠ status_cn / err 用 .get() 防御：测试与旧调用方可能手工造行
        # （不经过 build_rows），直接下标会 KeyError。
        return tuple((r["id"], r["idx"], r["name"], r["status"],
                      r.get("status_cn", r.get("status")), r["saved"],
                      r["pct"], r["busy"], bool(r.get("err")),
                      None if r["rate"] is None else round(r["rate"], 3))
                     for r in rows)

    def set_rows(self, rows) -> bool:
        """整体替换行数据；**内容没变则什么都不发信号**。"""
        fp = self._fingerprint(rows)
        if fp == self._fp:
            return False
        self.beginResetModel()
        self._rows = list(rows)
        self._fp = fp
        self.endResetModel()
        return True

    def rows(self) -> list:
        return self._rows

    def row_id(self, r: int):
        if 0 <= r < len(self._rows):
            return self._rows[r]["id"]
        return None

    def id_row(self, tid) -> int:
        for i, r in enumerate(self._rows):
            if r["id"] == tid:
                return i
        return -1

    # ---- 排序 ----
    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder):
        """按列排序。

        ⚠ 排序键**逐列显式写死**，不靠「显示文本字典序」：
        「已保存」按数字排（否则 "1,000" 排在 "999" 前面）、
        「进度」按百分比排（不是按条形宽度）。
        ⚠ R74：「ETA」空的沉底这条随 ETA 列一起删了。
        """
        if not self._rows:
            return
        rev = (order == Qt.SortOrder.DescendingOrder)

        def key(r):
            if column == COL_IDX:
                return r["idx"]
            if column == COL_NAME:
                return (r["name"] or "").lower()
            if column == COL_PROGRESS:
                # 未完成的（None）沉底，不因「没进度」被排到最前面
                return (r["pct"] is None, -(r["pct"] or 0))
            if column == COL_STATUS:
                return r["status"]
            if column == COL_SAVED:
                return r["saved"]
            if column == COL_RATE:
                return (r["rate"] is None, -(r["rate"] or 0.0))
            return r["idx"]

        self.layoutAboutToBeChanged.emit()
        self._rows = sorted(self._rows, key=key, reverse=rev)
        self.layoutChanged.emit()


# ══════════════════════════════════════════════════════════════════
# 委托
# ══════════════════════════════════════════════════════════════════
class ProgressDelegate(QStyledItemDelegate):
    """把进度列画成**系统风格**的进度条。

    全部实现都走 `QStyleOptionProgressBar` + `style().drawControl`，
    本类**不出现任何 QPainter 画矩形 / 圆角 / 渐变的代码**（铁律 1）。
    """

    def paint(self, painter, option, index):
        opt = QStyleOptionProgressBar()
        opt.rect = option.rect
        # 调色板整个交给 option：底 / 填充 / 文字都从系统调色板取，
        # 所以深浅色、高对比度、用户换系统主题全都自动跟随（铁律 2）。
        opt.palette = option.palette
        opt.state = option.state
        opt.textAlignment = Qt.AlignmentFlag.AlignCenter
        opt.text = self._text(index)

        if index.data(TaskTableModel.ROLE_BUSY):
            # minimum == maximum == 0 是 Qt 约定的「不确定态」：
            # 系统样式会画一条来回扫的动画条，表示「在动但不知道总量」。
            opt.minimum = 0
            opt.maximum = 0
            opt.progress = 0
        else:
            opt.minimum = 0
            opt.maximum = 100
            pct = index.data(TaskTableModel.ROLE_PCT)
            opt.progress = int(pct) if pct is not None else 0

        # widget 传对，样式才画得出 focus 框 / 禁用态
        QApplication.style().drawControl(
            QStyle.ControlElement.CE_ProgressBar, opt, painter, option.widget)

    @staticmethod
    def _text(index) -> str:
        pct = index.data(TaskTableModel.ROLE_PCT)
        if pct is None:
            return ""
        # 100% 就不再叠文字：满条 + "100%" 在 24px 行高里会糊成一团
        if pct >= 100:
            return ""
        return f"{int(pct)}%"


# ══════════════════════════════════════════════════════════════════
# 表格本体
# ══════════════════════════════════════════════════════════════════
class TaskTableView(QTableView):
    """原生高密度任务表。

    ⚠ **为什么是 QTableView 而不是 QTreeView**（作业单两者都写了）：
    PySide6 的 **QTreeView 根本没有 `verticalHeader()` / `setRowHeight()`**
    （实测 `hasattr` 均为 False —— 那是 QTableView 独有的 API），
    而「行高压扁到 22~26px」正是本单的核心观感要求，用 QTreeView 就得
    靠 `setStyleSheet` 或逐行 delegate 绕，既不原生又更慢。
    QTableView + QAbstractTableModel 是 `QTableWidget` 的正规现代写法
    （作业单允许 `QTableWidget`，但它内建模型、200 行会有额外开销）。
    顺带：QTreeView 那些树形功能（展开/折叠/层级）这里一条都用不上。

    信号：
      edit_requested(int)         双击行（与卡片版 itemDoubleClicked 同义）
      menu_requested(QPoint, int) 右键（pos 是**控件坐标**，id 是命中行；
                                   没命中行时 id = -1）
                                   ⚠ R74：「运行/停止」从这里搬走了。
                                     启停改由 **右键菜单**
                                     （`main_window._task_menu`）承担 ——
                                     两个信号 `run_requested` /
                                     `stop_requested` 随之删除。
                                     ⚠⚠ 删它们之前必须确认菜单里
                                     「运行」「停止」还在，否则
                                     classic / qbittorrent 两个皮肤
                                     会静默失去启停能力（R66 同型坑）。
    """

    edit_requested = Signal(int)
    menu_requested = Signal(QPoint, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("task_table")
        model = TaskTableModel(self)
        self.setModel(model)
        self.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self))
        # ⚠ R74：原先这里给「操作」列挂ActionDelegate（画两个系统风格
        #   小按钮）。列已删，委托与 `action_rects` 一并移除。
        # 24px 行高（qB 观感，见 ROW_HEIGHT）：默认行高按内容算，
        # 数字列与文字列会算出不同高度，密集列表必须**所有行一样高**。
        # ⚠ 只用 `verticalHeader().setDefaultSectionSize`：**不要**调
        # `setRowHeight` —— 它是 (row, height) 逐行的、且只对已存在的行生效，
        # 新增行不会继承（症状：新出现的行比别的行高一截）。实测
        # `setRowHeight(ROW_HEIGHT)` 单参调用直接 TypeError（要两个参数），
        # 属于「看着像设置默认值的 API 其实不是」的一类坑。
        self.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        # ⚠ 以下三个是 **QTreeView 独有**，QTableView 完全没有（实测
        #   hasattr 全 False，调用即 AttributeError）：
        #     setAllColumnsShowFocus / setUniformRowHeights /
        #     setRootIsDecorated
        #   等价效果分别由「选中整行」「固定行高」「本来就无树形层级」满足。
        #   同理 QTreeView 的 `header()` 在 QTableView 里叫
        #   **`horizontalHeader()`**，`setSectionHidden` 叫 `setColumnHidden`
        #   —— 抄 QTreeView 教程写表格必踩。
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setAlternatingRowColors(True)       # 斑马纹是原生表格的默认观感
        self.setSortingEnabled(True)
        vh = self.verticalHeader()
        # Fixed 模式 = 行高锁死，全部行等高（QTableView 里这就是
        # 「uniform row heights」的正解，不需要 QTreeView 那个 API）
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vh.setVisible(False)                     # 行号那列已经有一列了
        hh = self.horizontalHeader()             # ⚠ 不是 header()（那是树的）
        for i, (key, _label, width, _a) in enumerate(COLUMNS):
            if key == "name":
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
            else:
                hh.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
                self.setColumnWidth(i, width)
        hh.setSectionsMovable(True)              # 列可拖动（作业单要求）
        hh.setSectionsClickable(True)
        hh.setSortIndicatorShown(True)
        # 表头右键 = 勾选显示哪些列（作业单：列可隐藏 +
        #   GUI 铁律：表格必须配右键菜单）
        hh.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hh.customContextMenuRequested.connect(self._header_menu)
        # 表格本体右键 = 任务动作菜单（与卡片版同一个菜单）
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._body_menu)
        self.doubleClicked.connect(self._on_double_click)

    # ---- 便捷访问 ----
    @property
    def tmodel(self) -> TaskTableModel:
        m = self.model()
        return m if isinstance(m, TaskTableModel) else None

    def selected_id(self):
        idx = self.currentIndex()
        if not idx.isValid():
            return None
        row = idx.data(TaskTableModel.ROLE_ROW)
        return row["id"] if row else None

    def selected_ids(self) -> list:
        """【R117 P1-2】当前**全部**选中行的任务 id（Ctrl / Shift 多选）。

        返回顺序按行号从小到大（Qt 的 selectionModel 本身就是这个顺序）。
        一个都没选时退回 ``[selected_id()]`` —— 这样单选时代的调用方
        （点右键/点操作按钮时只选中了一行）行为不变；一个都没选中则返回 ``[]``。

        ⚠ 为什么要「一个都没选时退回当前行」而不是直接返回 []：
          鼠标点一下某行会让它成为 currentRow，Qt 在某些平台上 selectionModel
          里确实有它，但用户按住 Ctrl 又点空处取消选择后 currentRow 仍指向
          那一行 —— 语义上「没选中」。所以这里只在 **完全没选中** 时用
          currentRow 兜底（兼容单选），而不是反过来。
        """
        out: list = []
        try:
            sel = self.selectionModel()
            if sel is not None:
                for idx in sel.selectedRows(0):
                    row = idx.data(TaskTableModel.ROLE_ROW)
                    if row:
                        out.append(row["id"])
        except Exception:
            out = []
        if not out:
            one = self.selected_id()
            return [one] if one is not None else []
        # 去重（ExtendedSelection 下理论上不会重复，保险）
        seen = set()
        uniq = []
        for t in out:
            if t not in seen:
                seen.add(t)
                uniq.append(t)
        return uniq

    def select_all(self) -> int:
        """【R117 P1-2】全选（Ctrl+A / 表头菜单入口）。返回选中条数。

        ``selectAll()`` 是 QAbstractItemView 自带的，在 ExtendedSelection 下
        也是全选（不是「切换全选」）。这里只包一层 + 回读条数，
        **不手动 emit 信号**（selectAll() 自己会发，硬发会让下游槽跑两遍）。
        """
        self.selectAll()
        return len(self.selected_ids())

    def select_id(self, tid) -> bool:
        m = self.tmodel
        if m is None:
            return False
        r = m.id_row(tid)
        if r < 0:
            return False
        idx = m.index(r, 0)
        self.setCurrentIndex(idx)
        self.scrollTo(idx)
        return True

    # ---- 刷新 ----
    def set_tasks(self, tasks, reports=None, live=None) -> bool:
        m = self.tmodel
        if m is None:
            return False
        # ⚠⚠ **r69：换行数据前记住选中项，set_rows 之后复原。**
        # `set_rows` 走 `beginResetModel/endResetModel` ⇒ Qt 会把
        # selectionModel 的当前索引一并作废 ⇒ 表格选中态被清空。
        # 与 main_window._rebuild_task_list 里卡片那处**同型同因**：
        # 任务状态一变就整表重建 ⇒ 用户选中的行「莫名其妙没选中」⇒
        # 点操作列的「停止」作用不到（作业单 §2.1 要求选中态必须还在）。
        # ⚠ 内容没变时 `set_rows` 直接 return False 不发信号，
        #   所以复原只发生在真正换数据时，不会每次刷新都白动一次。
        keep_tid = None
        try:
            keep_tid = self.selected_id()
        except Exception:
            keep_tid = None
        changed = m.set_rows(build_rows(tasks, reports, live))
        if changed and keep_tid is not None:
            try:
                self.select_id(keep_tid)
            except Exception:
                pass
        return changed

    # ---- 命中判定 ----
    def _hit_row(self, pos: QPoint) -> int:
        idx = self.indexAt(pos)
        return idx.row() if idx.isValid() else -1

    # ⚠ R74：`_action_at` 与 `mousePressEvent` 覆写随之删除。
    #   为什么要连mousePressEvent 一起删：它**只**为操作列的命中判定而存在
    #   —— 判不到操作列时它就是 super().mousePressEvent(e)。留着一个
    #   永远走不到自己逻辑的覆写，只会让下一个人以为这里还有交互逻辑。
    #   ⇒ 删干净比留空壳好（这与「撤功能要改反向断言」是同一条原则）。

    def _on_double_click(self, index):
        # 卡片版双击 = 编辑。⚠ R74：原先这里要避开「操作」列
        # （双击在按钮上别弹编辑框），列已删 ⇒ 现在任何一格双击都是编辑。
        row = index.data(TaskTableModel.ROLE_ROW)
        if row and row["id"] is not None:
            self.edit_requested.emit(int(row["id"]))

    # ---- 菜单 ----
    def _header_menu(self, pos: QPoint):
        """表头右键：勾选式「显示哪些列」。

        ⚠ 与表格本体的任务菜单**刻意分开** —— 这里管「列」，
        那边管「任务」。混成一个菜单的话，用户右键表头会看到
        「停止所选任务」，非常费解（这正是 qB 也不这么干的原因）。
        """
        menu = QMenu(self)
        hh = self.horizontalHeader()
        for i, key in enumerate(COL_KEYS):
            if key not in _HIDEABLE:
                continue
            act = QAction(COLUMNS[i][1], menu)
            act.setCheckable(True)
            act.setChecked(not self.isColumnHidden(i))
            act.toggled.connect(
                lambda on, idx=i: self.setColumnHidden(idx, not on))
            menu.addAction(act)
        menu.exec(hh.mapToGlobal(pos))

    def _body_menu(self, pos: QPoint):
        r = self._hit_row(pos)
        tid = self.tmodel.row_id(r) if r >= 0 else None
        if tid is not None:
            self.select_id(tid)
        self.menu_requested.emit(pos, -1 if tid is None else int(tid))
