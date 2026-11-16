"""V0.0.18 · 站点采集统计看板（桌面端）。

聚合展示每个任务（站点）的采集量、成功率、平均速率、磁盘占用与
失败原因 Top3，并在失败文本命中鉴权特征时给出「疑似登录失效 →
请到站点 Cookie 管理更新」提示。另附素材集导出历史清单。

口径**只有一处**：``site_stats.task_stats`` / ``site_stats.cookie_expiry_hints``
/ ``export_kit.export_history``，与网页端报表页共用同一批纯函数。
本模块只做展示，不写任何 SQL、不发起任何网络请求。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

_DAYS_LABELS = {"全部时间": 0, "最近 7 天": 7, "最近 30 天": 30}


def _fmt_bytes(n: int) -> str:
    x = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024.0
    return "0 B"


class SiteStatsDialog(QDialog):
    """构造签名固定 ``(db, parent=None)``，供主窗口接线调用。"""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("站点采集统计")
        self.resize(920, 600)

        v = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("统计范围"))
        self.range = QComboBox()
        self.range.addItems(list(_DAYS_LABELS.keys()))
        bar.addWidget(self.range)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._reload)
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        self.lbl_gen = QLabel("")
        bar.addWidget(self.lbl_gen)
        v.addLayout(bar)

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        # 页 1：任务/站点统计
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(
            ["任务", "站点", "图片", "收藏", "失败 URL", "成功率",
             "速率（图/时）", "磁盘占用"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c, w in ((2, 70), (3, 60), (4, 80), (5, 80), (6, 100), (7, 100)):
            self.table.setColumnWidth(c, w)
        self.tabs.addTab(self.table, "任务与站点")

        # 页 2：失败原因 Top
        self.err_table = QTableWidget(0, 3)
        self.err_table.setHorizontalHeaderLabels(["任务", "失败原因", "次数"])
        self.err_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self.err_table.setColumnWidth(0, 240)
        self.err_table.setColumnWidth(2, 80)
        self.tabs.addTab(self.err_table, "失败原因")

        # 页 3：Cookie 提醒
        cw = QWidget()
        cl = QVBoxLayout(cw)
        self.hint_lbl = QLabel(
            "根据失败记录的报错文本自动归因。命中「401/403/登录失效」"
            "等鉴权特征时在此提示；判定仅供参考，不代表站点一定需要登录。")
        self.hint_lbl.setWordWrap(True)
        cl.addWidget(self.hint_lbl)
        self.cookie_table = QTableWidget(0, 4)
        self.cookie_table.setHorizontalHeaderLabels(
            ["任务", "涉及域名（已存 Cookie）", "涉及域名（尚未存 Cookie）",
             "样例报错"])
        ch = self.cookie_table.horizontalHeader()
        ch.setSectionResizeMode(1, QHeaderView.Stretch)
        ch.setSectionResizeMode(2, QHeaderView.Stretch)
        ch.setSectionResizeMode(3, QHeaderView.Stretch)
        self.cookie_table.setColumnWidth(0, 200)
        cl.addWidget(self.cookie_table, 1)
        self.tabs.addTab(cw, "Cookie 提醒")

        # 页 4：导出历史
        self.hist_table = QTableWidget(0, 6)
        self.hist_table.setHorizontalHeaderLabels(
            ["时间", "导出目录", "张数", "标注数", "合格线", "筛选摘要"])
        hhh = self.hist_table.horizontalHeader()
        hhh.setSectionResizeMode(1, QHeaderView.Stretch)
        hhh.setSectionResizeMode(5, QHeaderView.Stretch)
        for c, w in ((0, 150), (2, 60), (3, 60), (4, 70)):
            self.hist_table.setColumnWidth(c, w)
        self.tabs.addTab(self.hist_table, "素材集导出历史")

        self._reload()

    # ------------------------------------------------------------------

    def _reload(self):
        from .. import export_kit as ek
        from .. import site_stats as ss

        days = _DAYS_LABELS.get(self.range.currentText(), 0)
        st = ss.task_stats(self.db, days)
        items = st.get("items", [])
        self.lbl_gen.setText(f"生成于 {st.get('generated_at', '')}")

        self.table.setRowCount(len(items))
        self.err_table.setRowCount(0)
        n_err = sum(len(it.get("top_errors", [])) for it in items)
        self.err_table.setRowCount(n_err)
        er = 0
        for row, it in enumerate(items):
            rate = it.get("success_rate")
            spd = it.get("images_per_hour")
            cells = [
                it["task"] or f"任务 #{it['task_id']}",
                ", ".join(it.get("hosts") or []) or "—",
                str(it["images"]), str(it["favorites"]),
                str(it["failed_urls"]),
                f"{rate * 100:.1f}%" if rate is not None else "—",
                f"{spd:.1f}" if spd is not None else "—",
                _fmt_bytes(it["disk_bytes"]),
            ]
            for col, txt in enumerate(cells):
                wi = QTableWidgetItem(txt)
                if col >= 2:
                    wi.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, col, wi)
            for e in it.get("top_errors", []):
                self.err_table.setItem(
                    er, 0, QTableWidgetItem(cells[0]))
                self.err_table.setItem(
                    er, 1, QTableWidgetItem(e["error"]))
                self.err_table.setItem(
                    er, 2, QTableWidgetItem(str(e["count"])))
                er += 1

        hints = ss.cookie_expiry_hints(self.db)
        self.cookie_table.setRowCount(len(hints))
        for row, h in enumerate(hints):
            cells = [h["task"] or f"任务 #{h['task_id']}",
                     ", ".join(h["hosts_with_cookie"]) or "—",
                     ", ".join(h["hosts_without_cookie"]) or "—",
                     " / ".join(h.get("sample_errors") or [])[:120] or "—"]
            for col, txt in enumerate(cells):
                self.cookie_table.setItem(row, col, QTableWidgetItem(txt))
        if not hints:
            self.hint_lbl.setText(
                "暂无提示：当前失败记录里没有命中鉴权特征的报错。"
                "若站点需要登录而尚未配置 Cookie，可在「维护 → 站点 Cookie 管理」添加。")
        else:
            self.hint_lbl.setText(
                f"发现 {len(hints)} 个任务的失败记录疑似登录失效，"
                "请前往「维护 → 站点 Cookie 管理」更新对应域名的 Cookie。")

        hist = ek.export_history(self.db, 100)
        rows = hist.get("items", [])
        self.hist_table.setRowCount(len(rows))
        for row, h in enumerate(rows):
            try:
                summary = ", ".join(
                    f"{k}:{v}" for k, v in (h.get("filters") or {}).items()
                    if v not in ("", None, 0, False))[:120]
            except Exception:
                summary = ""
            cells = [str(h.get("ts") or ""), str(h.get("dest") or ""),
                     str(h.get("copied", 0)), str(h.get("captions", 0)),
                     f"{float(h.get('min_score') or 0):.2f}",
                     summary or "默认"]
            for col, txt in enumerate(cells):
                self.hist_table.setItem(row, col, QTableWidgetItem(txt))
        if not rows:
            note = "尚无导出记录。完成一次「数据 → 导出训练素材集」后此处可见。"
            self.hist_table.setRowCount(1)
            for col in range(6):
                self.hist_table.setItem(
                    0, col, QTableWidgetItem(note if col == 0 else ""))
