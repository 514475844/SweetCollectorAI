"""F24 下载限速计划设置框（设置 → 下载限速计划…）。

用户诉求（R154-F24 原文）：设置页分时段限速（如 00:00-08:00 全速、
工作时间限 500KB/s）。

形态取舍：
* **一行一个时段**（开始 / 结束 / 限速），而不是「一张大表 + 一堆列」——
  用户只想填「几点到几点、多快」，表格控件在这个尺寸下远不如三格一行直观；
* 时间用 **QLineEdit（HH:MM）** 而不是 QTimeEdit：QTimeEdit 的滚轮/上下箭头
  会让人一不小心把 08:00 滚成 09:00，而且拿它的值要处理 QTime→str；
  输错了由 errors() 给中文提示，比控件偷偷纠正更容易懂；
* kbps 用 QSpinBox，范围 0~100000（0 = 不限速，与 rateplan.py 同口径）；
* 「当前实际限速」**实时算出来显示**（不是静态说明文字）—— 限速这类功能
  最容易出现的抱怨就是「我设了但不知道有没有生效」，所以对话框里就给答案。

⚠ 所有判定（时段解析 / 越界钳制 / 命中顺序）一律调 rateplan.py，
   这里**不重写第二份**：界面显示与实际限速对不上是这类功能的经典缺陷。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..rateplan import (
    MAX_KBPS, MIN_KBPS, PLAN_MAX_SLOTS, default_plan, describe_kbps,
    current_limit_text, normalize_plan, parse_hhmm, plan_summary,
)

#: 表头文案（与实际列顺序一致：开始 / 结束 / 限速）
_HEADERS = ("开始", "结束", "限速（KB/s，0=不限）")


class _PlanRow(QWidget):
    """一行时段：开始 / 结束 / 限速 + 删除。"""

    def __init__(self, parent=None, on_remove=None,
                 start: str = "00:00", end: str = "08:00", kbps: int = 0):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        self.ed_start = QLineEdit(start)
        self.ed_start.setPlaceholderText("HH:MM")
        self.ed_start.setMaximumWidth(80)
        self.ed_end = QLineEdit(end)
        self.ed_end.setPlaceholderText("HH:MM")
        self.ed_end.setMaximumWidth(80)
        self.sp_kbps = QSpinBox()
        self.sp_kbps.setRange(0, MAX_KBPS)
        self.sp_kbps.setSingleStep(50)
        self.sp_kbps.setValue(int(kbps or 0))
        self.sp_kbps.setSuffix(" KB/s")
        self.sp_kbps.setMaximumWidth(130)
        self.sp_kbps.setToolTip("0 = 这个时段不限速；最小 %d KB/s（更小会按 %d 算）"
                                % (MIN_KBPS, MIN_KBPS))
        lay.addWidget(self.ed_start)
        lay.addWidget(QLabel("–"))
        lay.addWidget(self.ed_end)
        lay.addWidget(self.sp_kbps)
        self.btn_del = QPushButton("删除")
        self.btn_del.setMaximumWidth(60)
        if on_remove is not None:
            self.btn_del.clicked.connect(on_remove)
        lay.addWidget(self.btn_del, 0, Qt.AlignmentFlag.AlignLeft)
        lay.addStretch(1)

    def value(self) -> dict:
        """表单原始值（**不**归一化：errors() 要靠原始值报中文错）。"""
        return {"start": self.ed_start.text().strip(),
                "end": self.ed_end.text().strip(),
                "kbps": int(self.sp_kbps.value())}


class RatePlanDialog(QDialog):
    """分时段下载限速设置框。用法：``dlg.exec()`` 为真 = 已写入 cfg 并落盘。"""

    def __init__(self, parent=None, cfg=None):
        super().__init__(parent)
        self.setWindowTitle("下载限速计划")
        self.resize(560, 420)
        self._cfg = cfg
        root = QVBoxLayout(self)

        self.chk_enable = QCheckBox("启用分时段限速（不勾 = 全天全速）")
        self.chk_enable.setChecked(
            bool(getattr(cfg, "rate_plan_enabled", False)))
        self.chk_enable.setToolTip(
            "只对**新开始的下载**生效（已在跑的任务按它开始时那一刻的时段走到底）。\n"
            "与 robots 的 crawl-delay 是两件事：那个管请求间隔，这个管字节速度，\n"
            "两者同时生效，实际速度取更严的那个。")
        root.addWidget(self.chk_enable)

        head = QHBoxLayout()
        for i, h in enumerate(_HEADERS):
            lab = QLabel(h)
            if i == 0:
                lab.setMinimumWidth(80)
            head.addWidget(lab)
            if i < 2:
                head.addWidget(QLabel(""))
        head.addStretch(1)
        root.addLayout(head)

        self._host = QVBoxLayout()
        root.addLayout(self._host)
        self._rows: list[_PlanRow] = []

        row_btn = QHBoxLayout()
        self.btn_add = QPushButton("添加时段")
        self.btn_add.clicked.connect(lambda: self.add_row())
        self.btn_reset = QPushButton("恢复默认")
        self.btn_reset.clicked.connect(self.reset_default)
        row_btn.addWidget(self.btn_add)
        row_btn.addWidget(self.btn_reset)
        row_btn.addStretch(1)
        root.addLayout(row_btn)

        # 「当前实际限速」：把「设了到底生不生效」这个问题当场回答掉。
        self.lbl_now = QLabel("")
        self.lbl_now.setWordWrap(True)
        root.addWidget(self.lbl_now)

        self.lbl_plan = QLabel("")
        self.lbl_plan.setWordWrap(True)
        self.lbl_plan.setToolTip("多条时段重叠时，取**第一条**命中的（按开始时间排序后）")
        root.addWidget(self.lbl_plan)

        self.box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        self.box.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self.box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.box.accepted.connect(self._on_save)
        self.box.rejected.connect(self.reject)
        root.addWidget(self.box)

        self.set_plan(getattr(cfg, "rate_plan", None) or default_plan())
        self.chk_enable.toggled.connect(self._refresh_state)
        self._refresh_state()

    # ---------------- 行管理 ----------------
    def add_row(self, start: str = "00:00", end: str = "08:00",
                kbps: int = 0) -> _PlanRow:
        row = _PlanRow(self, on_remove=None, start=start, end=end, kbps=kbps)

        def _rm():
            try:
                self._host.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
            except RuntimeError:
                pass
            if row in self._rows:
                self._rows.remove(row)
            self._refresh_state()

        row.btn_del.clicked.connect(_rm)
        self._rows.append(row)
        self._host.addWidget(row)
        self._refresh_state()
        return row

    def set_plan(self, plan) -> None:
        """用计划表重建所有行（会**丢弃**界面上未保存的改动）。"""
        for r in list(self._rows):
            try:
                self._host.removeWidget(r)
                r.setParent(None)
                r.deleteLater()
            except RuntimeError:
                pass
        self._rows = []
        slots = normalize_plan(plan)
        if not slots:
            # 计划表被洗空了（从没配过 / 全是废行）→ 给一份默认表，
            # 别让用户面对一张空表单猜该填什么。
            slots = default_plan()
        for sl in slots:
            self.add_row(sl["start"], sl["end"], int(sl["kbps"]))
        self._refresh_state()

    def reset_default(self) -> None:
        self.set_plan(default_plan())

    def collect_rows(self) -> list:
        return [r.value() for r in self._rows]

    def plan(self) -> list:
        """归一化后的计划表（非法行**不会**被丢掉——先过 errors()）。"""
        return normalize_plan(self.collect_rows())

    # ---------------- 校验 ----------------
    def errors(self) -> list:
        """返回中文错误列表；空列表 = 可以保存。"""
        errs: list = []
        rows = self.collect_rows()
        if not rows:
            errs.append("至少要有一个时段；不想限速就把上面「启用」的勾去掉")
            return errs
        if len(rows) > PLAN_MAX_SLOTS:
            errs.append(f"最多 {PLAN_MAX_SLOTS} 个时段，现在有 {len(rows)} 个")
        seen = set()
        for i, r in enumerate(rows, 1):
            s, e = parse_hhmm(r["start"]), parse_hhmm(r["end"])
            if s is None:
                errs.append(f"第 {i} 行的开始时间「{r['start']}」不对，"
                            f"要写成 HH:MM（如 08:30；24:00 表示到当天结束）")
            if e is None:
                errs.append(f"第 {i} 行的结束时间「{r['end']}」不对，"
                            f"要写成 HH:MM（如 18:00；24:00 表示到当天结束）")
            if s is not None and e is not None and s == e:
                errs.append(f"第 {i} 行开始和结束时间一样，这个时段是空的")
            kb = int(r["kbps"] or 0)
            if 0 < kb < MIN_KBPS:
                errs.append(f"第 {i} 行限速 {kb} KB/s 太小了："
                            f"最小 {MIN_KBPS} KB/s（想不限速就填 0）")
            key = (s, e)
            if key in seen and s is not None:
                errs.append(f"第 {i} 行与前面某行的时段重复了")
            seen.add(key)
        return errs

    # ---------------- 落盘 ----------------
    def apply_to(self, cfg) -> None:
        """把表单写进 cfg（**不**落盘：落盘由调用方决定，方便测试）。"""
        cfg.rate_plan_enabled = bool(self.chk_enable.isChecked())
        cfg.rate_plan = self.plan()

    def _on_save(self):
        errs = self.errors()
        if errs:
            QMessageBox.warning(self, "还改不了", "\n".join(errs[:8]))
            return
        if self._cfg is not None:
            self.apply_to(self._cfg)
        self.accept()

    # ---------------- 状态显示 ----------------
    def _refresh_state(self):
        try:
            on = bool(self.chk_enable.isChecked())
            self.lbl_now.setText("当前：" + current_limit_text(
                self.plan(), on))
            self.lbl_plan.setText("计划：" + plan_summary(self.plan()))
        except RuntimeError:
            pass

    def current_text(self) -> str:
        """「当前实际限速」一句话（测试/调用方用，与界面同一口径）。"""
        return current_limit_text(self.plan(),
                                  bool(self.chk_enable.isChecked()))


def kbps_label(kbps: int) -> str:
    """给别的界面复用的 KB/s 文案（与 rateplan.describe_kbps 同源）。"""
    return describe_kbps(kbps)
