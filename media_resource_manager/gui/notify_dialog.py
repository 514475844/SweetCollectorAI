"""V0.0.18 · 通知中心（桌面端只读面板）。

与网页端顶栏铃铛共用 ``notify`` 模块与 ``notifications`` 表：批量任务操作、
素材集导出、备份同步、日志清空、全面清理这些动作完成后都会落一条通知。
桌面用户多数动作当场已有对话框反馈，本面板主要覆盖「软件后台发生的事」
与「网页端远程操作留下的痕迹」，因此只保留 刷新 / 全部标记已读 / 清空 三个动作。

口径只有一处：``notify.list_all / mark_read / clear``，本模块不写 SQL。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)


class NotifyDialog(QDialog):
    """构造签名固定 ``(db, parent=None)``。"""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("通知中心")
        self.resize(760, 460)

        v = QVBoxLayout(self)
        tip = QLabel("记录批量操作、导出、备份、清理等动作的完成通知。"
                     "未读条数与网页端铃铛为同一份数据。")
        tip.setWordWrap(True)
        v.addWidget(tip)

        bar = QHBoxLayout()
        self.summary = QLabel("")
        bar.addWidget(self.summary, 1)
        self.btn_reload = QPushButton("刷新")
        self.btn_reload.clicked.connect(self._reload)
        bar.addWidget(self.btn_reload)
        self.btn_read = QPushButton("全部标记已读")
        self.btn_read.clicked.connect(self._read_all)
        bar.addWidget(self.btn_read)
        self.btn_clear = QPushButton("清空通知")
        self.btn_clear.clicked.connect(self._clear)
        bar.addWidget(self.btn_clear)
        v.addLayout(bar)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时间", "标题", "内容", "状态"])
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        h.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 150)
        self.table.setColumnWidth(3, 70)
        v.addWidget(self.table, 1)

        self._reload()

    def _reload(self):
        from .. import notify

        res = notify.list_all(self.db, 200)
        rows = res.get("items", []) if res.get("ok") else []
        self.table.setRowCount(len(rows))
        unread = 0
        for i, r in enumerate(rows):
            read = bool(r.get("read"))
            if not read:
                unread += 1
            cells = [str(r.get("ts") or ""), str(r.get("title") or ""),
                     str(r.get("body") or ""), "已读" if read else "未读"]
            for c, t in enumerate(cells):
                self.table.setItem(i, c, QTableWidgetItem(t))
        self.summary.setText(f"共 {len(rows)} 条，其中未读 {unread} 条")

    def _read_all(self):
        from .. import notify

        notify.mark_read(self.db, None, all_read=True)
        self._reload()

    def _clear(self):
        from .. import notify

        # 二次确认：清空不可恢复
        if QMessageBox.question(
                self, "确认清空",
                "清空后全部通知记录不可恢复，是否继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return
        res = notify.clear(self.db)
        if not res.get("ok"):
            QMessageBox.warning(self, "清空失败",
                                res.get("error") or "通知未能清空。")
        self._reload()
