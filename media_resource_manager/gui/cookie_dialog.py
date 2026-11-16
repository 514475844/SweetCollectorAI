"""桌面的「站点 Cookie 管理」对话框（与网页端 CookieMgr.vue 能力对齐）。

按域名保存站点登录态 Cookie：同一站点下的任务由 Fetcher 自动携带，无需
逐项配置。列表仅呈现 Cookie 名称与条数，值不进入表格，查看完整 Cookie 头
须通过「编辑」在输入框中载入。

⚠ 数据口径纪律：Cookie 的读取、写入、归一、导入与配对码一律只调用
   ``cookies.py``（本模块以 ``from .. import cookies as ck`` 引入），
   本文件不直接打开存储文件、不自行解析 Cookie 头、不另写一套读写逻辑。
   桌面端与网页端共用同一份口径，避免两处显示不一致。
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from .. import cookies as ck
from ..config import save_config


def _summary_text(it: dict) -> str:
    """把 ``summary()`` 的一项格式化为表格摘要（只含名称与条数，不含值）。"""
    names = [str(n) for n in (it.get("names") or [])]
    cnt = int(it.get("count") or 0)
    more = int(it.get("more") or 0)
    return f"{cnt} 条：{'、'.join(names)}" + ("…" if more else "")


class CookieDialog(QDialog):
    """站点 Cookie 管理；``cfg`` 为 ``AppConfig`` 实例。"""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("站点 Cookie 管理")
        self.setMinimumSize(660, 640)
        self._build()
        self._refresh_pair()
        self.reload()

    # ---------------------------------------------------------------- 界面
    def _build(self):
        lay = QVBoxLayout(self)

        tip = QLabel(
            "按域名保存站点 Cookie：同一站点下的所有任务将自动携带对应 Cookie，"
            "无需逐项重复配置。列表中仅显示 Cookie 名称，不显示其值"
            "（与密码同级保护）。")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        # ---- 配对码 ----
        lay.addWidget(QLabel("浏览器插件配对码"))
        prow = QHBoxLayout()
        self.lb_code = QLabel("")
        self.lb_code.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lb_code.setStyleSheet(
            "font-size: 15px; font-weight: 700; padding: 2px 10px;")
        prow.addWidget(self.lb_code)
        btn_rotate = QPushButton("更换配对码")
        btn_rotate.clicked.connect(self._on_rotate)
        prow.addWidget(btn_rotate)
        prow.addStretch(1)
        lay.addLayout(prow)

        pair_tip = QLabel(
            "请在浏览器插件中填写该配对码，以便插件向本软件推送站点 Cookie。")
        pair_tip.setWordWrap(True)
        lay.addWidget(pair_tip)

        # ---- 已存列表（只给名称与条数，不给值）----
        lay.addWidget(QLabel("已保存的 Cookie（按域名）："))
        self.tbl = QTableWidget(0, 2)
        self.tbl.setHorizontalHeaderLabels(["域名", "Cookie 名称（摘要）"])
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setStretchLastSection(True)
        self.tbl.setColumnWidth(0, 220)
        lay.addWidget(self.tbl, 1)
        self.lb_empty = QLabel("暂无已保存的 Cookie。")
        lay.addWidget(self.lb_empty)

        brow = QHBoxLayout()
        for text, slot in (("新增", self._on_new),
                           ("编辑", self._on_edit),
                           ("删除", self._on_del)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            brow.addWidget(b)
        brow.addStretch(1)
        lay.addLayout(brow)

        hint = QLabel(
            "单击「编辑」可在输入框中查看完整 Cookie 头；列表不显示 Cookie 值。")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # ---- 新增 / 编辑表单 ----
        drow = QHBoxLayout()
        drow.addWidget(QLabel("域名："))
        self.e_domain = QLineEdit()
        self.e_domain.setPlaceholderText("域名，例如 example.com")
        drow.addWidget(self.e_domain, 1)
        lay.addLayout(drow)

        lay.addWidget(QLabel("Cookie 头："))
        self.e_header = QPlainTextEdit()
        self.e_header.setPlaceholderText(
            "请填写 Cookie 头，例如 cf_clearance=xxxx; csrftoken=yyyy")
        self.e_header.setFixedHeight(60)
        lay.addWidget(self.e_header)

        srow = QHBoxLayout()
        btn_save = QPushButton("保存这条 Cookie")
        btn_save.clicked.connect(self._on_save)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._on_new)
        srow.addWidget(btn_save)
        srow.addWidget(btn_clear)
        srow.addStretch(1)
        lay.addLayout(srow)

        # ---- 从导出 JSON 导入 ----
        lay.addWidget(QLabel("从导出 JSON 导入："))
        self.te_import = QPlainTextEdit()
        self.te_import.setPlaceholderText(
            "可粘贴 Cookie 导出 JSON，或形如 {域名: k=v; k2=v2} 的对象")
        self.te_import.setFixedHeight(52)
        lay.addWidget(self.te_import)

        irow = QHBoxLayout()
        btn_file = QPushButton("从导出 JSON 导入…")
        btn_file.clicked.connect(self._on_import_file)
        btn_paste = QPushButton("解析并导入粘贴内容")
        btn_paste.clicked.connect(self._on_import_paste)
        irow.addWidget(btn_file)
        irow.addWidget(btn_paste)
        irow.addStretch(1)
        lay.addLayout(irow)

        # ---- 底部：插件目录用途 ----
        note = QLabel(
            "插件目录用于存放浏览器扩展：将 extension/Sweet采集助手 目录在"
            "浏览器的扩展管理页面以「加载已解压的扩展」方式载入，此后在当前"
            "站点单击推送按钮即可把 Cookie 发送到本软件。若插件推送不可用，"
            "可在上方区域手工填写域名与 Cookie 头。")
        note.setWordWrap(True)
        lay.addWidget(note)

        self.lb_status = QLabel("")
        self.lb_status.setWordWrap(True)
        lay.addWidget(self.lb_status)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    # ---------------------------------------------------------------- 数据
    def reload(self):
        items = ck.summary()
        self.tbl.setRowCount(len(items))
        for i, it in enumerate(items):
            dom = str(it.get("domain", ""))
            self.tbl.setItem(i, 0, QTableWidgetItem(dom))
            self.tbl.setItem(i, 1, QTableWidgetItem(_summary_text(it)))
        self.lb_empty.setVisible(not items)

    def _set_status(self, text: str):
        self.lb_status.setText(text)

    def _selected_domain(self) -> str:
        row = self.tbl.currentRow()
        if row < 0:
            return ""
        it = self.tbl.item(row, 0)
        return it.text().strip() if it else ""

    def _refresh_pair(self) -> str:
        cfg = self.cfg
        code = ck.pair_code(cfg, save=lambda: save_config(cfg))
        self.lb_code.setText(code)
        return code

    # ---------------------------------------------------------------- 交互
    def _on_new(self):
        self.tbl.clearSelection()
        self.e_domain.clear()
        self.e_header.clear()
        self._set_status("")

    def _on_edit(self):
        dom = self._selected_domain()
        if not dom:
            self._set_status("请先选择要编辑的记录。")
            return
        header = ck.get_cookie(dom) or ""
        if not header:
            self._set_status("该域名尚未保存 Cookie。")
            return
        self.e_domain.setText(dom)
        self.e_header.setPlainText(header)
        self._set_status(
            f"已载入域名 {dom} 的 Cookie，请在输入框中查看或编辑。")

    def _on_save(self):
        dom = ck.normalize_domain(self.e_domain.text())
        header = self.e_header.toPlainText().strip()
        if not dom:
            QMessageBox.warning(self, "保存未完成", "请填写有效的域名后再保存。")
            return
        if not header:
            QMessageBox.warning(self, "保存未完成", "请填写 Cookie 头内容。")
            return
        ck.set_cookie(dom, header)
        self.reload()
        self._set_status(f"已保存域名 {dom} 的 Cookie。")

    def _on_del(self):
        dom = self._selected_domain()
        if not dom:
            self._set_status("请先选择要删除的记录。")
            return
        ret = QMessageBox.question(
            self, "删除确认",
            f"请确认是否删除域名 {dom} 的 Cookie？删除后该站点的任务需要重新获取登录态。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        ck.del_cookie(dom)
        self.reload()
        self._set_status(f"已删除域名 {dom} 的 Cookie。")

    def _on_rotate(self):
        ret = QMessageBox.question(
            self, "更换配对码",
            "更换配对码后，浏览器插件中需要重新填写配对码。请确认是否更换？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        cfg = self.cfg
        code = ck.new_pair_code()
        try:
            cfg.cookie_pair_code = code
            save_config(cfg)
        except Exception:
            QMessageBox.warning(
                self, "更换未完成", "保存配置失败，请确认数据目录可写后重试。")
            return
        self.lb_code.setText(code)
        self._set_status("配对码已更换：请在浏览器插件中重新填写。")

    def _on_import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "请选择要导入的 JSON 文件", "",
            "JSON 文件 (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except Exception:
            QMessageBox.warning(
                self, "导入未完成",
                "读取所选文件失败，请确认文件可访问且为 UTF-8 编码。")
            return
        self._do_import(text)

    def _on_import_paste(self):
        text = self.te_import.toPlainText().strip()
        if not text:
            self._set_status("请先在输入框中粘贴导出 JSON 内容。")
            return
        self._do_import(text)

    def _do_import(self, text: str):
        try:
            payload = json.loads(text)
        except Exception:
            QMessageBox.warning(
                self, "导入未完成",
                "所选内容不是合法的 JSON，请提供 Cookie 导出文件的内容。")
            return
        res = ck.import_payload(payload, merge=True)
        doms = list((res.get("domains") or {}).keys())
        if not doms:
            self._set_status("未找到可导入的 Cookie，请检查内容是否包含域名与名称。")
            return
        self.reload()
        self._set_status(
            f"已导入 {int(res.get('imported') or 0)} 条 Cookie，"
            f"涉及域名：{'、'.join(str(d) for d in doms)}。")
