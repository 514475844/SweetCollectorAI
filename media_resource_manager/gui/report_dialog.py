"""R154-F25 ·「采集周报 / 月报」对话框（数据 → 采集周报 / 月报…）。

用户诉求（R154-F25 原文）：学习看板或独立页：按周/月统计（采集张数、通过率、
不喜欢 top 词、任务排行）；可导出 Markdown/CSV。

形态取舍：
* **独立对话框**而不是塞进学习看板：看板回答「AI 学到了什么」，报告回答
  「这一期采了多少、值不值」——两个问题混在一个窗口里，两边都会被稀释；
* 周期切换用**下拉 + 上一期/下一期**，不用日期选择器：用户要的是「这周」
  「上个月」，不是「2026-03-07 到 2026-03-11」；
* 「下一期」在**当期就禁用**（未来没有数据，点进去只会看到一张空表，
  用户会以为是软件坏了）—— 与 ``reporting.period_range`` 的钳制同口径；
* 导出按钮**自己弹保存框并落盘**，落完状态栏给一句「存到哪了」——
  点了没反应是这类功能最常见的抱怨。

⚠ 所有统计一律调 ``reporting``，**这里不写第二份 SQL**：两端（桌面/网页）
  各写一份口径，用户就会看到「桌面说 12、手机说 9」。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..reporting import (
    PERIOD_MONTH, PERIOD_WEEK, PERIOD_ZH, collect_period, headline,
    pass_rate_text, period_range, to_csv, to_markdown,
)

#: 任务排行表头（与 ``reporting.to_csv`` 的列顺序一致，别各写一份）
_HEADERS = ("任务", "采集", "喜欢", "不喜欢", "通过率")


class ReportDialog(QDialog):
    """采集周报 / 月报。``db`` 只读使用；本对话框不提供任何改库入口。"""

    def __init__(self, parent=None, db=None, kind: str = PERIOD_WEEK,
                 offset: int = 0):
        super().__init__(parent)
        self._db = db
        self._kind = kind if kind in PERIOD_ZH else PERIOD_WEEK
        self._offset = max(0, int(offset or 0))
        self._rep: dict = {}
        self.setWindowTitle("采集周报 / 月报")
        self.setMinimumSize(620, 460)

        lay = QVBoxLayout(self)

        # ---- 周期选择 ----
        top = QHBoxLayout()
        top.addWidget(QLabel("周期："))
        self.cmb_kind = QComboBox()
        self.cmb_kind.addItem(PERIOD_ZH[PERIOD_WEEK], PERIOD_WEEK)
        self.cmb_kind.addItem(PERIOD_ZH[PERIOD_MONTH], PERIOD_MONTH)
        self.cmb_kind.setCurrentIndex(
            0 if self._kind == PERIOD_WEEK else 1)
        self.cmb_kind.currentIndexChanged.connect(self._on_kind_changed)
        top.addWidget(self.cmb_kind)

        self.btn_prev = QPushButton("← 上一期")
        self.btn_prev.clicked.connect(self._on_prev)
        top.addWidget(self.btn_prev)
        self.btn_next = QPushButton("下一期 →")
        self.btn_next.clicked.connect(self._on_next)
        top.addWidget(self.btn_next)
        top.addStretch(1)
        lay.addLayout(top)

        self.lb_period = QLabel("")
        self.lb_period.setWordWrap(True)
        lay.addWidget(self.lb_period)

        self.lb_head = QLabel("")
        self.lb_head.setWordWrap(True)
        lay.addWidget(self.lb_head)

        # ---- 不喜欢 top 词 ----
        lay.addWidget(QLabel("不喜欢 Top 词（学习权重是全局的，非本期新增）："))
        self.lb_tops = QLabel("")
        self.lb_tops.setWordWrap(True)
        self.lb_tops.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lb_tops)

        # ---- 任务排行 ----
        lay.addWidget(QLabel("任务排行："))
        self.tbl = QTableWidget(0, len(_HEADERS))
        self.tbl.setHorizontalHeaderLabels(list(_HEADERS))
        self.tbl.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        lay.addWidget(self.tbl, 1)

        # ---- 按钮 ----
        btns = QHBoxLayout()
        self.btn_md = QPushButton("导出 Markdown…")
        self.btn_md.clicked.connect(self.export_markdown)
        btns.addWidget(self.btn_md)
        self.btn_csv = QPushButton("导出 CSV…")
        self.btn_csv.clicked.connect(self.export_csv)
        btns.addWidget(self.btn_csv)
        btns.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        btns.addWidget(box)
        lay.addLayout(btns)

        self.reload()

    # ---------------- 数据 ----------------

    def period(self) -> dict:
        return period_range(self._kind, None, self._offset)

    def reload(self) -> dict:
        """重算并刷新界面。返回本次的报告（测试与界面共用同一份）。"""
        p = self.period()
        self._rep = collect_period(self._db, p["start"], p["end"])
        self._rep["kind"] = p["kind"]
        self._rep["label"] = p["label"]
        self._paint()
        return self._rep

    def report(self) -> dict:
        return self._rep

    def markdown_text(self) -> str:
        return to_markdown(self._rep, self._kind)

    def csv_text(self) -> str:
        return to_csv(self._rep)

    # ---------------- 界面刷新 ----------------

    def _paint(self):
        r = self._rep
        self.lb_period.setText(
            f"{PERIOD_ZH.get(self._kind, '')}区间：{r.get('label', '')}")
        self.lb_head.setText(headline(r))
        tops = r.get("top_dislike") or []
        self.lb_tops.setText(
            "、".join(f"{t.get('text') or t.get('label')}({float(t.get('weight') or 0):.2f})"
                      for t in tops) if tops else "尚未学习到排除方向的偏好。")
        rank = r.get("task_rank") or []
        self.tbl.setRowCount(len(rank))
        for i, t in enumerate(rank):
            vals = [str(t.get("name", "")), str(int(t.get("images") or 0)),
                    str(int(t.get("liked") or 0)),
                    str(int(t.get("disliked") or 0)),
                    pass_rate_text(t.get("pass_rate"))]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                self.tbl.setItem(i, j, it)
        # 「下一期」在当期禁用：未来没有数据（period_range 同样把负 offset 钳回 0）
        self.btn_next.setEnabled(self._offset > 0)
        self.btn_md.setEnabled(bool(r.get("has_data")))
        self.btn_csv.setEnabled(bool(rank))

    # ---------------- 交互 ----------------

    def _on_kind_changed(self, _idx=0):
        self._kind = self.cmb_kind.currentData() or PERIOD_WEEK
        self.reload()

    def _on_prev(self):
        self._offset += 1
        self.reload()

    def _on_next(self):
        if self._offset <= 0:
            return
        self._offset -= 1
        self.reload()

    # ---------------- 导出 ----------------

    def _export(self, text: str, ext: str, filt: str, title: str):
        if not text:
            QMessageBox.information(self, title, "本期没有可导出的内容。")
            return ""
        p = self.period()
        default = f"采集{PERIOD_ZH.get(self._kind, '')}_{p['start']}_{p['end']}.{ext}"
        path, _ = QFileDialog.getSaveFileName(self, title, default, filt)
        if not path:
            return ""
        try:
            # ⚠ 必须显式 utf-8（CSV 带 BOM 也要按字节写）：
            # 用系统默认编码（Windows 上是 GBK）会把中文任务名写成乱码。
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return ""
        return path

    def export_markdown(self) -> str:
        return self._export(self.markdown_text(), "md",
                            "Markdown 文件 (*.md)", "导出周报 / 月报（Markdown）")

    def export_csv(self) -> str:
        return self._export(self.csv_text(), "csv",
                            "CSV 文件 (*.csv)", "导出任务排行（CSV）")


def open_report_dialog(parent, db) -> ReportDialog:
    """打开并展示报告框（与学习看板同款入口，供菜单调用）。"""
    dlg = ReportDialog(parent, db)
    dlg.exec()
    return dlg
