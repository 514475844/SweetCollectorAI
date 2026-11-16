"""V0.0.18 · 全局搜索对话框（桌面端）。

一个输入框同时搜任务 / 图库 / 审计日志。口径**只有一处**：
``searchall.search_all``，与网页端 ``/api/search/all`` 调同一个函数，
两端结果必须一致。本模块只做展示与跳转，不写任何 SQL。

跳转纪律：
  · 任务：可跳到任务页（选中该行）或直接进入编辑；
  · 图片：双击开灯箱（只含本条记录，← → 不回翻全库）；
  · 日志：只读展示，不提供跳转。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

_KIND_ROLE = Qt.UserRole + 1   # 存 "task" / "image" / "log"
_ID_ROLE = Qt.UserRole + 2     # 存数据库 id


class SearchAllDialog(QDialog):
    """全局搜索。构造签名固定 ``(db, main_window, parent=None)``。

    ``main_window`` 仅用于跳转（切页/开灯箱），不向其写任何状态。
    """

    def __init__(self, db, win, parent=None):
        super().__init__(parent)
        self.db = db
        self.win = win
        self.setWindowTitle("全局搜索")
        self.resize(760, 520)

        v = QVBoxLayout(self)
        tip = QLabel("一个关键词同时搜索任务、图库与操作日志。"
                     "双击任务行可进入编辑，双击图片行查看大图。")
        tip.setWordWrap(True)
        v.addWidget(tip)

        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("输入关键词，回车或单击「搜索」…")
        self.edit.setClearButtonEnabled(True)
        row.addWidget(self.edit, 1)
        self.btn = QPushButton("搜索")
        self.btn.clicked.connect(self._do_search)
        row.addWidget(self.btn)
        v.addLayout(row)
        # 停顿 400ms 自动搜索（与网页端一致）；回车立即搜索。
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(400)
        self._debounce.timeout.connect(self._do_search)
        self.edit.textChanged.connect(lambda _="": self._debounce.start())
        self.edit.returnPressed.connect(self._do_search)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["结果", "说明", "编号"])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 340)
        self.tree.setColumnWidth(1, 300)
        self.tree.itemDoubleClicked.connect(self._activate)
        v.addWidget(self.tree, 1)

        self.status = QLabel("尚未搜索。")
        self.status.setObjectName("search_all_status")
        v.addWidget(self.status)

        # 焦点给输入框：从菜单打开后可直接输入。
        self.edit.setFocus()
        if hasattr(self.edit, "selectAll"):
            self.edit.selectAll()

    # ------------------------------------------------------------------

    def _do_search(self):
        from .. import searchall

        kw = self.edit.text().strip()
        if not kw:
            self.tree.clear()
            self.status.setText("请输入搜索关键词。")
            return
        res = searchall.search_all(self.db, kw)
        self.tree.clear()
        n_task = n_img = n_log = 0
        head = QTreeWidgetItem([f"—— 任务（{len(res['tasks'])}）——", "", ""])
        head.setDisabled(True)
        self.tree.addTopLevelItem(head)
        for t in res["tasks"]:
            it = QTreeWidgetItem([t["name"] or f"任务 #{t['id']}",
                                  f"状态：{t['status'] or '未知'}",
                                  str(t["id"])])
            it.setData(0, _KIND_ROLE, "task")
            it.setData(0, _ID_ROLE, t["id"])
            self.tree.addTopLevelItem(it)
            n_task += 1
        head = QTreeWidgetItem([f"—— 图库（{len(res['images'])}）——", "", ""])
        head.setDisabled(True)
        self.tree.addTopLevelItem(head)
        for im in res["images"]:
            sc = im.get("score")
            it = QTreeWidgetItem([im["name"] or f"图片 #{im['id']}",
                                  f"AI 评分 {sc:.3f}" if sc is not None else "",
                                  str(im["id"])])
            it.setData(0, _KIND_ROLE, "image")
            it.setData(0, _ID_ROLE, im["id"])
            self.tree.addTopLevelItem(it)
            n_img += 1
        head = QTreeWidgetItem([f"—— 操作日志（{len(res['logs'])}）——", "", ""])
        head.setDisabled(True)
        self.tree.addTopLevelItem(head)
        for lg in res["logs"]:
            desc = " ".join(x for x in (lg["action"], lg["target"]) if x)
            it = QTreeWidgetItem([desc[:80] or f"日志 #{lg['id']}",
                                  str(lg["ts"] or ""), str(lg["id"])])
            it.setData(0, _KIND_ROLE, "log")
            it.setData(0, _ID_ROLE, lg["id"])
            self.tree.addTopLevelItem(it)
            n_log += 1
        if not (n_task or n_img or n_log):
            self.status.setText(f"未找到与「{kw}」匹配的内容。")
        else:
            self.status.setText(
                f"关键词「{kw}」：任务 {n_task} 条 · 图片 {n_img} 张 · "
                f"日志 {n_log} 条")

    def _activate(self, item, _col):
        kind = item.data(0, _KIND_ROLE)
        iid = item.data(0, _ID_ROLE)
        if kind == "task" and iid is not None:
            self._goto_task(int(iid))
        elif kind == "image" and iid is not None:
            self._open_image(int(iid))

    def _goto_task(self, task_id: int):
        """跳到任务页并选中该任务；能取到编辑入口时直接进入编辑。"""
        try:
            self.win._nav_go(1)
            table = getattr(self.win, "task_table", None)
            if table is not None:
                table.select_id(task_id)
            if hasattr(self.win, "_edit_by_id"):
                self.win._edit_by_id(task_id)
            self.accept()
        except Exception as e:
            self.status.setText(f"跳转任务失败：{e}")

    def _open_image(self, image_id: int):
        """用灯箱打开单张图片（只含本条记录）。

        取数走 ``searchall.image_row_for_lightbox``，对话框内不写 SQL。
        """
        from .. import searchall
        from .lightbox import Lightbox

        row = searchall.image_row_for_lightbox(self.db, image_id)
        if row is None or not str(row.get("path") or ""):
            self.status.setText("未找到该图片的记录或路径为空。")
            return
        Lightbox([row], 0, None,
                 hotkeys=getattr(self.win.cfg, "hotkeys", None)).exec()
