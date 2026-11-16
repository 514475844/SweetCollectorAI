"""V0.0.14 · 训练素材集导出参数对话框（桌面端）。

只负责收集参数，**不执行导出**：真正的复制与标注生成统一在
``export_kit.export_training_kit``，网页端调用同一个函数，避免两端各写一套
筛选口径。对话框里出现的词一律按全软件术语：目标词 / 排除词。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QVBoxLayout,
)


class TrainingKitDialog(QDialog):
    """收集素材集筛选条件；``cfg`` 用于取当前全局合格线作为默认值。"""

    def __init__(self, cfg, parent=None, db=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("导出训练素材集")
        self.resize(460, 300)
        try:
            default_score = float(getattr(cfg, "ai_threshold", 0.35) or 0.35)
        except (TypeError, ValueError):
            default_score = 0.35

        v = QVBoxLayout(self)
        tip = QLabel(
            "按合格线挑选图片，复制到指定目录，并为每张图片生成同名标注文件。"
            "原图仅被复制，不会删除、移动或改名。")
        tip.setWordWrap(True)
        v.addWidget(tip)

        form = QFormLayout()
        self.f_score = QDoubleSpinBox()
        self.f_score.setRange(0.0, 1.0)
        self.f_score.setSingleStep(0.01)
        self.f_score.setDecimals(2)
        self.f_score.setValue(default_score)
        form.addRow("最低合格线", self.f_score)

        self.f_limit = QSpinBox()
        self.f_limit.setRange(1, 5000)
        self.f_limit.setValue(500)
        form.addRow("最多导出张数", self.f_limit)

        self.f_task = QLineEdit()
        self.f_task.setPlaceholderText("留空则不限任务")
        form.addRow("任务编号", self.f_task)

        self.f_tag = QLineEdit()
        self.f_tag.setPlaceholderText("留空则不限标签")
        form.addRow("标签包含", self.f_tag)

        self.f_terms = QLineEdit()
        self.f_terms.setPlaceholderText("多个目标词请以逗号分隔，将一并写入标注")
        form.addRow("附加目标词", self.f_terms)

        # V0.0.18：标注模板。默认「{terms}」= 目标词逗号分隔（老行为不变）；
        # 可写「触发词 {terms}」给文生图模型加前缀。留空回退默认。
        self.f_tpl = QLineEdit()
        self.f_tpl.setPlaceholderText(
            "默认「{terms}」即目标词逗号分隔；"
            "可写「mydog style {terms}」在标注前添加触发词")
        form.addRow("标注模板", self.f_tpl)

        self.f_fav = QCheckBox("仅导出收藏图片")
        form.addRow("", self.f_fav)

        self.f_group = QCheckBox("按任务分文件夹整理")
        self.f_group.setChecked(True)
        form.addRow("", self.f_group)
        v.addLayout(form)

        note = QLabel(
            "标注内容由目标词与图片标签组成；排除词不会写入标注。"
            "零字节图片与源文件已不存在的记录将被跳过。")
        note.setWordWrap(True)
        v.addWidget(note)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.button(QDialogButtonBox.Ok).setText("开始导出")
        box.button(QDialogButtonBox.Cancel).setText("取消")
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        v.addWidget(box)

        # V0.0.18：两步工作流 —— 先预览候选清单（不落盘），勾选后导出。
        # ``db`` 缺失（老调用方）时预览按钮禁用，直接导出，老行为不变。
        self._db = db
        self._previewed = False
        from PySide6.QtWidgets import QPushButton, QTableWidget

        self.btn_preview = QPushButton("预览候选图片")
        self.btn_preview.setToolTip(
            "按当前筛选条件列出候选图片，不复制任何文件；"
            "确认勾选后单击「开始导出」。")
        self.btn_preview.setEnabled(db is not None)
        v.insertWidget(v.count() - 1, self.btn_preview)
        self.btn_preview.clicked.connect(self._preview)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["文件名", "AI 评分", "任务", "预演标注", "状态"])
        self.table.horizontalHeader().setStretchLastSection(False)
        from PySide6.QtWidgets import QHeaderView
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.Stretch)
        for col, w in ((0, 200), (1, 70), (2, 120), (4, 110)):
            self.table.setColumnWidth(col, w)
        self.table.setVisible(False)
        v.addWidget(self.table, 1)
        self.table.itemChanged.connect(self._on_item_changed)
        self.resize(620, 480)

        self._picked: set[int] = set()

    def _preview(self):
        """调 export_kit.preview_training_kit 列候选；勾选状态存 _picked。

        预览**不落任何盘**。默认全部勾选（磁盘上真实存在且非零字节的），
        「源文件未找到 / 零字节」的行不勾选并标出原因——导出层本来也会
        跳过它们，预览只是提前让用户看见。
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMessageBox, QTableWidgetItem

        from .. import export_kit as ek

        try:
            res = ek.preview_training_kit(self._db, self.cfg, self.options())
        except Exception as e:
            QMessageBox.warning(self, "预览失败",
                                f"预览候选图片失败：{e}")
            return
        items = res.get("items") or []
        if not res.get("ok") or not items:
            QMessageBox.information(
                self, "预览",
                res.get("error") or "暂无符合条件的图片，"
                "请降低合格线或放宽筛选条件。")
            return
        self.table.blockSignals(True)   # 填充期间不触发 _on_item_changed
        self.table.clearContents()
        self.table.setRowCount(len(items))
        self._picked = set()
        ids: list[int] = []
        for row, it in enumerate(items):
            ok_item = bool(it.get("on_disk")) and not it.get("zero_bytes")
            iid = int(it["id"])
            ids.append(iid)
            if ok_item:
                self._picked.add(iid)
            status = "可导出" if ok_item else (
                "零字节，将被跳过" if it.get("zero_bytes")
                else "源文件未找到")
            cells = [str(it.get("name") or ""),
                     f"{float(it.get('score') or 0):.3f}",
                     str(it.get("task") or ""),
                     str(it.get("caption") or ""), status]
            for col, txt in enumerate(cells):
                wi = QTableWidgetItem(txt)
                if col == 0:
                    wi.setFlags(wi.flags() | Qt.ItemIsUserCheckable)
                    wi.setCheckState(Qt.Checked if ok_item else Qt.Unchecked)
                self.table.setItem(row, col, wi)
        self._ids_by_row = ids
        self.table.blockSignals(False)
        self.table.setVisible(True)
        self._previewed = True
        self._sync_count_label()

    def _sync_count_label(self):
        n = len(getattr(self, "_picked", ()))
        self.btn_preview.setText(f"预览候选图片（已勾选 {n} 张）")

    def _on_item_changed(self, item):
        """勾选变化 → 更新 _picked（行号→id 由 _ids_by_row 映射）。"""
        from PySide6.QtCore import Qt

        if item.column() != 0:
            return
        rows = getattr(self, "_ids_by_row", [])
        if item.row() >= len(rows):
            return
        iid = rows[item.row()]
        if item.checkState() == Qt.Checked:
            self._picked.add(iid)
        else:
            self._picked.discard(iid)
        self._sync_count_label()

    def options(self) -> dict:
        """返回可直接传给 ``export_kit.export_training_kit`` 的参数字典。"""
        task = self.f_task.text().strip()
        tag = self.f_tag.text().strip()
        out = {
            "min_score": float(self.f_score.value()),
            "limit": int(self.f_limit.value()),
            "only_favorite": bool(self.f_fav.isChecked()),
            "group_by": "task" if self.f_group.isChecked() else "flat",
            "extra_terms": self.f_terms.text().strip(),
        }
        tpl = self.f_tpl.text().strip()
        if tpl:
            out["caption_template"] = tpl
        if task:
            try:
                out["task_id"] = int(task)
            except ValueError:
                pass
        if tag:
            out["tag"] = tag
        return out

    def selected_ids(self) -> list[int]:
        """预览后用户勾选的图片 id；未预览返回空列表（=不启用白名单）。"""
        return sorted(self._picked) if self._previewed else []
