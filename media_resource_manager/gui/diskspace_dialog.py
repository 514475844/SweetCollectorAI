"""R154-F18 ·「磁盘空间看板」对话框（数据 → 磁盘空间看板…）。

用户诉求（R154-F18 原文）：各目录（library/quarantine/review/thumbs/models）
占用条形图 + 7 日增长趋势（每日一快照落库）；超阈值中文告警
（阈值走设置，联动现有磁盘预警若有）。

形态取舍：
* 阈值**不新建设置项**，直接读 ``cfg.min_free_mb``（#54「磁盘剩余空间
  预警」）：另设一个字段，用户在设置里改了却看不懂为什么两边不联动；
* 打开时若今天还没快照就扫一遍记上（「每日一快照」不要求用户记得点按钮），
  同一天再打开不重复扫（大库扫一遍要好几秒）—— 与网页端同一个
  ``diskspace.snapshot_today_if_missing``，两边记的是同一行；
* 条形图用 ``QProgressBar`` 而不是引 matplotlib：比例是「相对最大目录」，
  不需要额外依赖也能成比例显示；告警是**语义色**（红），不跟皮肤走
  （皮肤的职责是强调色，告警红在任何皮肤下都该是红的）；
* 趋势表**旧的在前**：从左往右读才是「增长」，今天在最后一行。

⚠ 统计一行都不在这里写：目录清单、扫描、快照、告警全在 ``diskspace``
（网页端同一个模块）。这里只负责把数字摆成界面。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .. import diskspace
from .theme import style as _tstyle   # P1-4：行内样式也要跟随皮肤

#: 趋势表头（与 ``diskspace.load_history`` 的字段一一对应，别各写一份）
_HEADERS = ("日期", "总占用", "较前一日")

#: 告警文案的样式模板。⚠ 色值**不许硬编码**（p14b 护栏 + 换肤铁律）：
#: 走 theme 的 ``{danger}`` 语义位 —— 告警红在任何皮肤下都该是红的，
#: 但具体色值由调色板回答（原生模式下整段失效，与全局 QSS 同进退）。
_ALERT_QSS = "QLabel{color:{danger};font-weight:bold}"


class DiskSpaceDialog(QDialog):
    """磁盘空间看板。``db`` 只读使用 + 每日一快照；不提供任何删除入口。"""

    def __init__(self, parent=None, db=None, cfg=None, root=None):
        super().__init__(parent)
        self._db = db
        self._cfg = cfg
        #: 数据根目录（None = user_data_dir()，与产品行为一致）；
        #: 测试传 tmp 目录，免得扫到真机的缩略图/模型。
        self._root = root
        self._usage: dict = {}
        self._bars: list[dict] = []
        self.setWindowTitle("磁盘空间看板")
        self.setMinimumSize(620, 480)

        lay = QVBoxLayout(self)

        # ---- 告警 + 概览 ----
        self.lb_alert = QLabel("")
        self.lb_alert.setWordWrap(True)
        self.lb_alert.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lb_alert)

        self.lb_head = QLabel("")
        self.lb_head.setWordWrap(True)
        lay.addWidget(self.lb_head)

        self.lb_free = QLabel("")
        self.lb_free.setWordWrap(True)
        lay.addWidget(self.lb_free)

        # ---- 目录占用条形图 ----
        lay.addWidget(QLabel("各目录占用（相对最大的目录）："))
        self._bar_box = QVBoxLayout()
        lay.addLayout(self._bar_box)

        # ---- 7 日增长趋势 ----
        lay.addWidget(QLabel("近 7 日增长趋势（每日首次打开时自动记录）："))
        self.tbl = QTableWidget(0, len(_HEADERS))
        self.tbl.setHorizontalHeaderLabels(list(_HEADERS))
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        lay.addWidget(self.tbl, 1)

        # ---- 按钮 ----
        btns = QHBoxLayout()
        self.btn_snap = QPushButton("立即记录快照")
        self.btn_snap.setToolTip("强制记录当日占用（同一天仅保留最后一条）")
        self.btn_snap.clicked.connect(self.record_now)
        btns.addWidget(self.btn_snap)
        btns.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        btns.addWidget(box)
        lay.addLayout(btns)

        self.reload()

    # ---------------- 数据 ----------------

    def usage(self) -> dict:
        return self._usage

    def reload(self) -> dict:
        """（按需）记快照 + 重扫并刷新界面。返回本次的用量。"""
        if self._db is not None:
            self._usage = diskspace.snapshot_today_if_missing(
                self._db, self._cfg, self._root)
        else:
            self._usage = diskspace.collect_usage(self._cfg, self._root)
        self._paint()
        return self._usage

    def record_now(self) -> dict:
        """「立即记录快照」：**先重扫**再记一笔（同日覆盖），并刷新界面。

        为什么要重扫：用户常常是「刚在资源管理器里清完隔离区」才来点这个
        按钮，记打开时的旧用量等于没记。
        """
        if self._db is None:
            return self._usage
        try:
            self._usage = diskspace.collect_usage(self._cfg, self._root)
            diskspace.record_snapshot(
                self._db, self._usage.get("dirs") or [],
                free_bytes=self._usage.get("free_bytes"))
        except Exception as e:                                  # pragma: no cover
            self.lb_alert.setText(f"快照记录失败：{e}")
            _tstyle(self.lb_alert, _ALERT_QSS)
            return self._usage
        self._paint()
        return self._usage

    # ---------------- 界面刷新 ----------------

    def _paint(self):
        u = self._usage
        self.lb_head.setText(diskspace.headline(u))

        free = u.get("free_bytes")
        thr = u.get("threshold_mb")
        free_text = diskspace.format_size(free) if free is not None else "未知"
        thr_text = (f"预警值 {thr} MB（设置 → 磁盘剩余空间预警）"
                    if thr else "未设预警值（设置 → 磁盘剩余空间预警）")
        self.lb_free.setText(f"保存盘剩余 {free_text}；{thr_text}")

        alert = u.get("alert") or ""
        self.lb_alert.setText(alert)
        # 有告警套语义色；无告警传空模板（连样式一起清掉，别留着上一回的红）
        _tstyle(self.lb_alert, _ALERT_QSS if alert else "")

        self._paint_bars(u.get("dirs") or [])
        self._paint_history()

    def _paint_bars(self, dirs):
        # 目录数固定 5 个，但清单是数据驱动的：重建一遍比猜索引可靠
        while len(self._bars) < len(dirs):
            row = QHBoxLayout()
            lb_name = QLabel("")
            lb_name.setMinimumWidth(80)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            lb_size = QLabel("")
            lb_size.setMinimumWidth(80)
            row.addWidget(lb_name)
            row.addWidget(bar, 1)
            row.addWidget(lb_size)
            self._bar_box.addLayout(row)
            self._bars.append({"name": lb_name, "bar": bar, "size": lb_size})
        for i, d in enumerate(dirs):
            w = self._bars[i]
            w["name"].setText(d["name"])
            w["bar"].setValue(int(round(float(d.get("fraction") or 0) * 100)))
            w["bar"].setFormat(d.get("text") or "0 B")
            w["size"].setText(d.get("text") or "0 B")
        # 多建的行藏掉（目录清单将来变长也不会留空行）
        for j in range(len(dirs), len(self._bars)):
            for w in (self._bars[j]["name"], self._bars[j]["bar"],
                      self._bars[j]["size"]):
                w.setVisible(False)

    def _paint_history(self):
        hist = diskspace.load_history(self._db) if self._db is not None else []
        self.tbl.setRowCount(len(hist))
        for i, h in enumerate(hist):
            vals = [h["date"], h["text"], h["delta_text"]]
            for j, v in enumerate(vals):
                self.tbl.setItem(i, j, QTableWidgetItem(str(v)))
        # 今天（最后一行）标出来：趋势的「今」在哪一行要一眼能看到
        if hist:
            last = self.tbl.item(len(hist) - 1, 0)
            if last is not None:
                last.setText(hist[-1]["date"] + "（今天）")


def open_diskspace_dialog(parent, db, cfg) -> DiskSpaceDialog:
    """打开并展示磁盘空间看板（与报告框同款入口，供菜单调用）。"""
    dlg = DiskSpaceDialog(parent, db, cfg)
    dlg.exec()
    return dlg
