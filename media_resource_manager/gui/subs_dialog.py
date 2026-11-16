"""桌面端「站点订阅管理」对话框（补齐网页端 Subs.vue 的订阅管理能力）。

网页端 ``webapp/src/views/Subs.vue`` 已有完整订阅管理，桌面端此前缺位。
本对话框把同一套能力接到桌面：列表、新增 / 编辑、删除、试抓预览、
立即检查（仅比对 / 比对并创建任务）、以及订阅监控的全局开关。

纪律（务必遵守，避免桌面端与网页端口径分叉）：
* **数据口径只用 ``subscribe.py`` 一处**：列表 / 取单条 / 保存 / 删除 /
  历史快照条数 / 试抓预览 / 立即检查，一律调用 ``sub.list_subs``、
  ``sub.get_sub``、``sub.save_sub``、``sub.delete_sub``、``sub.snapshot_count``、
  ``sub.preview``、``sub.check_sub``。本文件不另写一套 SQL、不自行拼装
  订阅表查询，否则两端会出现同一条订阅显示不一致的情况。
* 全局开关写入 ``cfg.sub_enabled`` / ``cfg.sub_interval_min`` 后，统一调用
  ``config.save_config(cfg)`` 落盘，与网页端共用同一份配置。
* 所有可能联网的动作（试抓预览、立即检查）都放进 ``QThread`` 子类执行；
  执行期间禁用相关按钮，结束后恢复；对话框关闭时请求线程停止并等待收尾，
  不留下无人管理的后台线程。

界面文案一律使用专业书面中文；错误说明为完整中文句子，不把底层英文异常
直接呈现给用户。
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .. import subscribe as sub
from ..config import save_config

#: 列表列头，与「名称 / 地址 / 检查间隔 / 是否启用 / 是否自动开始 / 历史快照」对应。
_HEADERS = ("名称", "地址", "检查间隔", "是否启用", "是否自动开始",
            "历史快照条数")

#: 关闭对话框时仍在执行、暂由模块级集合托管引用的线程。
#: 保持强引用可避免 QThread 对象在线程运行期间被回收，finished 后自动移除。
_ORPHANS: set = set()


# ---------------------------------------------------------------- 纯函数
# 下面四个函数不依赖 Qt 与数据库，便于纯静态单测直接验证口径与中文文案。

def _zh_only(text: str) -> str:
    """仅当说明基本由中文构成时返回，用于挡住底层英文异常细节。

    ``subscribe.preview`` / ``check_sub`` 的错误多数已由 ``_zh_error`` 中文化，
    但个别兜底分支仍带英文异常名。这里做个保守筛选：中文字符不足一半就判为
    英文为主，交由调用方改用完整的中文整句。
    """
    s = str(text or "").strip().rstrip("。")
    if not s:
        return ""
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    if cjk < 2 or cjk * 2 < len(s):
        return ""
    return s


def _save_error_text(msg: str) -> str:
    """把 ``save_sub`` 的失败说明转成完整中文句子（不呈现正则引擎的英文细节）。"""
    m = str(msg or "")
    if "http" in m or "地址" in m:
        return ("保存订阅失败：订阅地址须以 http:// 或 https:// 开头，"
                "请检查后重试。")
    if "正则" in m or "规则" in m:
        return "保存订阅失败：链接规则不是合法的正则表达式，请检查后重试。"
    return "保存订阅失败，请检查填写内容后重试。"


def _preview_text(result: dict) -> tuple[str, str]:
    """把 ``preview`` 的结果整理为（状态句, 链接明细）。不创建任何任务。"""
    if not result.get("ok"):
        detail = _zh_only(result.get("error"))
        if detail:
            return "试抓预览未能完成：" + detail + "。", ""
        return "试抓预览未能完成，请检查网络与订阅地址后重试。", ""
    links = [str(u) for u in (result.get("links") or [])]
    count = int(result.get("count") or 0)
    if not links:
        return ("试抓完成，但未识别到任何条目。请调整链接规则或订阅地址后"
                "重新试抓。"), ""
    head = f"试抓完成：共识别到 {count} 条链接。"
    if result.get("truncated"):
        head += "以下仅显示前若干条。"
    return head, "\n".join(links)


def _check_text(result: dict, create_tasks: bool,
                queued: bool = False) -> tuple[str, str]:
    """把 ``check_sub`` 的结果整理为（状态句, 条目明细）。

    区分「仅比对」与「比对并创建任务」两种口径，说法必须与真实发生的行为一致：
    仅比对不写快照不建任务；创建任务分支据 ``task_id`` 与是否入队给出说明。
    """
    if not result.get("ok"):
        detail = _zh_only(result.get("error"))
        if detail:
            return "本次检查未能完成：" + detail + "。", ""
        return "本次检查未能完成，请检查网络与订阅地址后重试。", ""
    name = str(result.get("name") or "该订阅")
    found = int(result.get("found") or 0)
    new = int(result.get("new") or 0)
    head = f"「{name}」检查完成：页面上共 {found} 条链接，其中新条目 {new} 条。"
    body = "\n".join(str(u) for u in (result.get("items") or []) if u)
    if not create_tasks:
        head += "本次为仅比对，未写入历史快照，也未创建任务。"
        return head, body
    tid = int(result.get("task_id") or 0)
    if tid:
        head += f"已创建采集任务（编号 {tid}）。"
        head += ("任务已按订阅的自动开始设置加入下载队列。" if queued
                 else "请在任务列表中启动该任务。")
    elif new:
        head += "发现新条目，但未能创建任务，请稍后重试。"
    else:
        head += "本次未发现新条目，未创建任务。"
    return head, body


# ---------------------------------------------------------------- 后台线程
class _NetWorker(QThread):
    """在后台执行一次订阅网络动作，结果通过信号送回主线程。

    只跑一次调用即结束，不启动事件循环，因此用 ``QThread`` 自带的
    ``requestInterruption()`` / ``isInterruptionRequested()`` 做协作式停止。
    底层网络调用是阻塞的，无法被强制打断：关闭窗口时以 ``wait()`` 收尾，
    并保证 ``QThread`` 对象在线程真正结束前不被回收（见 ``_ORPHANS``）。
    """

    done = Signal(str, object)
    failed = Signal(str)

    def __init__(self, kind: str, action, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._action = action

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            result = self._action()
        except Exception:
            # 不把英文异常抛给用户，交由主线程显示完整中文句子。
            if not self.isInterruptionRequested():
                self.failed.emit(self._kind)
            return
        if not self.isInterruptionRequested():
            self.done.emit(self._kind, result)


# ---------------------------------------------------------------- 对话框
class SubsDialog(QDialog):
    """站点订阅管理。``db`` 为 ``Database`` 实例，``cfg`` 为 ``AppConfig`` 实例。

    构造签名固定为 ``(db, cfg, parent=None)``，供主窗口接线调用。
    若接线方持有调度队列，可在 ``exec()`` 前调用 ``set_queue(queue)``，
    使「比对并创建任务」按订阅的自动开始设置入队。
    """

    def __init__(self, db, cfg, parent=None):
        super().__init__(parent)
        self._db = db
        self._cfg = cfg
        self.queue = None
        self._rows: list[dict] = []
        self._editing_id = 0
        self._worker = None
        self._loading = False
        self._busy = False
        self.setWindowTitle("站点订阅管理")
        self.setMinimumSize(780, 600)
        self._build()
        self.reload()

    def set_queue(self, queue):
        """由接线方注入调度队列；未注入时创建的任务需在任务列表中启动。"""
        self.queue = queue

    # ---------------------------------------------------------------- 界面
    def _build(self):
        lay = QVBoxLayout(self)

        # ---- 全局设置 ----
        global_box = QGroupBox("订阅监控全局设置")
        gv = QVBoxLayout(global_box)
        row = QHBoxLayout()
        self.chk_global = QCheckBox("启用订阅监控")
        self.spin_default = QSpinBox()
        self.spin_default.setRange(sub.MIN_INTERVAL_MIN, 10080)
        row.addWidget(self.chk_global)
        row.addWidget(QLabel("默认检查间隔（分钟）"))
        row.addWidget(self.spin_default)
        row.addStretch(1)
        gv.addLayout(row)
        tip = QLabel("总开关关闭时，后台将不再定时检查；已有订阅与历史快照均保留。")
        tip.setWordWrap(True)
        gv.addWidget(tip)
        self.chk_global.toggled.connect(self._on_global_changed)
        self.spin_default.valueChanged.connect(self._on_global_changed)
        lay.addWidget(global_box)

        # ---- 列表 ----
        self.lb_summary = QLabel("")
        self.lb_summary.setWordWrap(True)
        lay.addWidget(self.lb_summary)

        self.tbl = QTableWidget(0, len(_HEADERS))
        self.tbl.setHorizontalHeaderLabels(list(_HEADERS))
        self.tbl.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tbl.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        lay.addWidget(self.tbl, 1)

        bar = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_new = QPushButton("新增订阅")
        self.btn_edit = QPushButton("编辑订阅")
        self.btn_delete = QPushButton("删除订阅")
        self.btn_check_dry = QPushButton("仅比对")
        self.btn_check_run = QPushButton("比对并创建任务")
        self.btn_refresh.clicked.connect(self.reload)
        self.btn_new.clicked.connect(self._open_new)
        self.btn_edit.clicked.connect(self._open_edit)
        self.btn_delete.clicked.connect(self._delete)
        self.btn_check_dry.clicked.connect(lambda: self._check(False))
        self.btn_check_run.clicked.connect(lambda: self._check(True))
        for b in (self.btn_refresh, self.btn_new, self.btn_edit,
                  self.btn_delete, self.btn_check_dry, self.btn_check_run):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        # ---- 新增 / 编辑表单 ----
        self.form_box = QGroupBox("订阅信息")
        fv = QVBoxLayout(self.form_box)
        form = QFormLayout()
        self.e_name = QLineEdit()
        self.e_name.setPlaceholderText("留空则使用域名作为名称")
        self.e_url = QLineEdit()
        self.e_url.setPlaceholderText("https://example.com/gallery")
        self.e_pattern = QLineEdit()
        self.e_pattern.setPlaceholderText("可留空；填写正则表达式，例如 /\\/post\\/\\d+/")
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(sub.MIN_INTERVAL_MIN, 10080)
        self.spin_max = QSpinBox()
        self.spin_max.setRange(1, 500)
        self.e_out = QLineEdit()
        self.e_out.setPlaceholderText("留空则使用全局图库目录")
        self.chk_enabled = QCheckBox("启用")
        self.chk_auto = QCheckBox("发现新条目后自动开始下载")
        form.addRow("名称", self.e_name)
        form.addRow("订阅地址", self.e_url)
        form.addRow("链接规则（正则表达式，可留空）", self.e_pattern)
        form.addRow("检查间隔（分钟）", self.spin_interval)
        form.addRow("最多条目", self.spin_max)
        form.addRow("输出目录", self.e_out)
        opts = QHBoxLayout()
        opts.addWidget(self.chk_enabled)
        opts.addWidget(self.chk_auto)
        opts.addStretch(1)
        form.addRow("选项", opts)
        fv.addLayout(form)
        hint = QLabel("建议先执行「试抓预览」，确认识别到的条目后再保存。"
                      "链接规则留空时按通用规则识别。试抓预览仅用于识别，不会创建任务。")
        hint.setWordWrap(True)
        fv.addWidget(hint)
        fbtns = QHBoxLayout()
        self.btn_save = QPushButton("保存")
        self.btn_preview = QPushButton("试抓预览")
        btn_cancel = QPushButton("取消")
        self.btn_save.clicked.connect(self._save_form)
        self.btn_preview.clicked.connect(self._do_preview)
        btn_cancel.clicked.connect(self._cancel_form)
        fbtns.addWidget(self.btn_save)
        fbtns.addWidget(self.btn_preview)
        fbtns.addWidget(btn_cancel)
        fbtns.addStretch(1)
        fv.addLayout(fbtns)
        self.form_box.setVisible(False)
        lay.addWidget(self.form_box)

        # ---- 结果 ----
        lay.addWidget(QLabel("操作结果："))
        self.lb_status = QLabel("")
        self.lb_status.setWordWrap(True)
        lay.addWidget(self.lb_status)
        self.txt_result = QPlainTextEdit()
        self.txt_result.setReadOnly(True)
        self.txt_result.setPlaceholderText("尚未产生结果。")
        self.txt_result.setFixedHeight(110)
        lay.addWidget(self.txt_result)

        bb = QDialogButtonBox()
        bb.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ---------------------------------------------------------------- 数据
    def reload(self):
        """刷新列表与全局控件。数据一律走 ``subscribe.py``，不另写 SQL。"""
        self._loading = True
        try:
            rows = sub.list_subs(self._db)
            self._rows = rows
            self.chk_global.setChecked(
                bool(getattr(self._cfg, "sub_enabled", True)))
            self.spin_default.setValue(
                int(getattr(self._cfg, "sub_interval_min", 60) or 60))
            self.tbl.setRowCount(len(rows))
            for i, s in enumerate(rows):
                seen = sub.snapshot_count(self._db, int(s["id"]))
                cells = (
                    str(s.get("name") or ""),
                    str(s.get("url") or ""),
                    f'{int(s.get("interval_min") or 60)} 分钟',
                    "已启用" if s.get("enabled") else "已停用",
                    "是" if s.get("auto_run") else "否",
                    f"{seen} 条",
                )
                for j, val in enumerate(cells):
                    it = QTableWidgetItem(val)
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    self.tbl.setItem(i, j, it)
            total = len(rows)
            enabled = sum(1 for s in rows if s.get("enabled"))
            self.lb_summary.setText(
                f"当前共 {total} 个订阅（启用 {enabled} 个）。"
                if total else "暂无订阅源，请单击「新增订阅」添加。")
        except Exception:
            self._status("读取订阅列表失败，请稍后重试。")
        finally:
            self._loading = False
        return self._rows

    def _current_sub(self):
        row = self.tbl.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _status(self, text: str, body: str = ""):
        self.lb_status.setText(text)
        self.txt_result.setPlainText(body)

    def _on_global_changed(self, *_args):
        if self._loading:
            return
        self._cfg.sub_enabled = self.chk_global.isChecked()
        self._cfg.sub_interval_min = int(self.spin_default.value())
        try:
            save_config(self._cfg)
        except Exception:
            self._status("保存订阅监控设置失败，请稍后重试。")
            return
        self._status("订阅监控设置已保存。")

    # ---------------------------------------------------------------- 表单
    def _open_new(self):
        self._editing_id = 0
        self.e_name.clear()
        self.e_url.clear()
        self.e_pattern.clear()
        self.spin_interval.setValue(
            int(getattr(self._cfg, "sub_interval_min", 60) or 60))
        self.spin_max.setValue(30)
        self.chk_enabled.setChecked(True)
        self.chk_auto.setChecked(True)
        self.e_out.clear()
        self.form_box.setTitle("新增订阅")
        self.form_box.setVisible(True)
        self._status("请填写订阅信息后保存。")

    def _open_edit(self):
        s = self._current_sub()
        if not s:
            self._status("请先选择一条订阅。")
            return
        self._editing_id = int(s["id"])
        self.e_name.setText(str(s.get("name") or ""))
        self.e_url.setText(str(s.get("url") or ""))
        self.e_pattern.setText(str(s.get("pattern") or ""))
        self.spin_interval.setValue(int(s.get("interval_min") or 60))
        self.spin_max.setValue(int(s.get("max_items") or 30))
        self.chk_enabled.setChecked(bool(s.get("enabled")))
        self.chk_auto.setChecked(bool(s.get("auto_run")))
        self.e_out.setText(str(s.get("output_dir") or ""))
        self.form_box.setTitle("编辑订阅")
        self.form_box.setVisible(True)
        self._status("请修改订阅信息后保存。")

    def _cancel_form(self):
        self.form_box.setVisible(False)
        self._editing_id = 0
        self._status("已取消编辑。")

    def _save_form(self):
        data = {
            "name": self.e_name.text().strip(),
            "url": self.e_url.text().strip(),
            "pattern": self.e_pattern.text().strip(),
            "interval_min": int(self.spin_interval.value()),
            "max_items": int(self.spin_max.value()),
            "enabled": self.chk_enabled.isChecked(),
            "auto_run": self.chk_auto.isChecked(),
            "output_dir": self.e_out.text().strip(),
        }
        if self._editing_id:
            data["id"] = self._editing_id
        if not data["url"]:
            self._status("请先填写订阅地址。")
            return
        try:
            ok, msg, _sid = sub.save_sub(self._db, data)
        except Exception:
            self._status("保存订阅失败，请稍后重试。")
            return
        if not ok:
            self._status(_save_error_text(msg))
            return
        note = "订阅已保存。" if self._editing_id else "已新建订阅。"
        self._editing_id = 0
        self.form_box.setVisible(False)
        self._status(note)
        self.reload()

    def _delete(self):
        s = self._current_sub()
        if not s:
            self._status("请先选择一条订阅。")
            return
        name = str(s.get("name") or "")
        if not self._confirm_delete(name):
            return
        try:
            n = sub.delete_sub(self._db, int(s["id"]))
        except Exception:
            self._status("删除订阅失败，请稍后重试。")
            return
        self._status("该订阅与历史快照已删除。"
                     if n else "未找到该订阅，可能已被删除。")
        self.reload()

    def _confirm_delete(self, name: str) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("删除订阅")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"确认删除订阅「{name}」？历史快照将一并删除；"
                    "重新订阅后，现有条目会被视为新条目。")
        btn_yes = box.addButton("确认删除", QMessageBox.ButtonRole.YesRole)
        btn_no = box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(btn_no)
        box.exec()
        return box.clickedButton() is btn_yes

    # ---------------------------------------------------------------- 后台动作
    def _do_preview(self):
        url = self.e_url.text().strip()
        if not url:
            self._status("请先填写订阅地址后再执行试抓预览。")
            return
        if not re.match(r"^https?://", url, re.I):
            self._status("订阅地址须以 http:// 或 https:// 开头，请检查后重试。")
            return
        pattern = self.e_pattern.text().strip()
        self._status("正在试抓预览，请稍候。")
        self._start_worker(
            "preview", lambda: sub.preview(self._cfg, url, pattern))

    def _check(self, create_tasks: bool):
        s = self._current_sub()
        if not s:
            self._status("请先选择一条订阅。")
            return
        full = sub.get_sub(self._db, int(s["id"]))
        if not full:
            self._status("未找到该订阅源，请刷新列表后重试。")
            return
        # 仅比对 dry_run=True 不建任务不写快照；创建任务分支按订阅设置入队。
        queue = self.queue if create_tasks else None
        dry_run = not create_tasks
        kind = "check_run" if create_tasks else "check_dry"

        def action():
            return sub.check_sub(self._db, self._cfg, full, queue=queue,
                                 dry_run=dry_run)

        self._status("正在检查订阅，请稍候。")
        self._start_worker(kind, action)

    def _start_worker(self, kind: str, action):
        if self._worker is not None and self._worker.isRunning():
            self._status("已有任务正在执行，请等待其完成后再继续。")
            return
        w = _NetWorker(kind, action)
        self._worker = w
        w.done.connect(self._on_done)
        w.failed.connect(self._on_failed)
        # finished 只维护模块级集合，不触碰界面，关闭窗口后触发也安全。
        w.finished.connect(lambda w=w: _ORPHANS.discard(w))
        self._set_busy(True)
        w.start()

    def _on_done(self, kind: str, result: dict):
        self._worker = None
        self._set_busy(False)
        if kind == "preview":
            text, body = _preview_text(result)
            self._status(text, body)
            return
        create_tasks = kind == "check_run"
        queued = bool(create_tasks and self.queue is not None)
        text, body = _check_text(result, create_tasks, queued)
        self._status(text, body)
        self.reload()

    def _on_failed(self, kind: str):
        self._worker = None
        self._set_busy(False)
        if kind == "preview":
            self._status("试抓预览未能完成，请检查网络与订阅地址后重试。")
        else:
            self._status("检查未能完成，请检查网络与订阅地址后重试。")

    def _set_busy(self, busy: bool):
        self._busy = busy
        for b in (self.btn_refresh, self.btn_new, self.btn_edit,
                  self.btn_delete, self.btn_check_dry, self.btn_check_run,
                  self.btn_preview, self.btn_save):
            b.setEnabled(not busy)

    # ---------------------------------------------------------------- 关闭收尾
    def _stop_worker(self):
        """关闭时协作式停止后台线程：请求中断、断开界面回调、等待收尾。

        网络调用无法被强制打断，若有限等待后仍未结束，则把线程交给
        ``_ORPHANS`` 托管强引用，待其自然结束后由 finished 回调移除，
        避免 QThread 对象在线程运行期间被回收，也避免界面回调落到已关闭窗口。
        """
        w = self._worker
        if w is None:
            return
        for sig, slot in ((w.done, self._on_done),
                          (w.failed, self._on_failed)):
            try:
                sig.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        w.requestInterruption()
        if w.isRunning():
            w.wait(3000)
        if w.isRunning():
            _ORPHANS.add(w)
        self._worker = None
        self._set_busy(False)

    def reject(self):
        self._stop_worker()
        super().reject()

    def done(self, result):
        # V0.0.17 冒烟同型修复：程序化关闭（done/accept）不走 reject/closeEvent，
        # 后台线程还在跑时对话框被回收 = QThread 带活线程销毁 = 直接崩溃。
        # done 是 reject/accept 的共同底层出口，堵这里即全路径覆盖。
        self._stop_worker()
        super().done(result)

    def closeEvent(self, event):
        self._stop_worker()
        super().closeEvent(event)
