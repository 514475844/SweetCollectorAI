"""桌面端「完整备份管理」对话框（补齐网页端整库备份管理能力）。

数据口径一律复用 ``fullbackup.py`` 的纯函数：``is_full_name`` / ``inspect_zip``
/ ``read_manifest`` / ``full_name`` / ``prune_plan`` / ``prune_and_delete``，
备份目录沿用 ``data_backup.backup_dir(user_data_dir())``，大小显示沿用
``data_backup.human_size``，与 ``webui._api_full_list`` /
``webui._prune_full_backups`` 完全一致，不另写一套清单、命名或清理规则。

恢复为破坏性重操作，本对话框不自行编写恢复 SQL：二次确认后把本地备份包
按 ``webui._api_full_upload`` 的两段式口径导入备份目录，再把恢复委托给接线方
提供的入口，复用桌面端既有的「从完整备份恢复」能力。

文件选择一律走 ``parent._file_pick`` / ``parent._dir_pick``（禁用原生后端、
非阻塞），绝不使用静态 ``QFileDialog`` 函数；方法缺失时禁用相关按钮并给出
书面中文提示。
"""
from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

#: 破坏性动作按钮的警示色（不使用 emoji）。
_WARN_STYLE = "QPushButton { color: rgb(176, 0, 32); font-weight: 600; }"

_HEADERS = ("备份包名称", "大小", "生成时间", "可读性", "条目数")


def _zh_detail(text) -> str:
    """仅当说明基本由中文构成时返回，用于挡住底层英文异常细节。"""
    s = str(text or "").strip().rstrip("。")
    if not s:
        return ""
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    if cjk < 2 or cjk * 2 < len(s):
        return ""
    return s


def _full_dir() -> Path:
    """备份目录（与 webui._full_dir 同源，纯函数）。"""
    from ..config import user_data_dir
    from ..data_backup import backup_dir
    return backup_dir(user_data_dir())


class FullBackupDialog(QDialog):
    """完整备份包的列举、导入恢复、另存与自动清理。

    构造签名固定为 ``(db, cfg, parent=None)``，供主窗口接线调用。
    """

    def __init__(self, db, cfg, parent=None):
        super().__init__(parent)
        self._db = db
        self._cfg = cfg
        self._rows: list[dict] = []
        self.setWindowTitle("完整备份管理")
        self.setMinimumSize(780, 600)
        self._build()
        self._check_pickers()
        self.reload()

    # ---------------------------------------------------------------- 界面
    def _build(self):
        lay = QVBoxLayout(self)

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
        self.btn_refresh = QPushButton("刷新列表")
        self.btn_upload = QPushButton("上传备份包并恢复…")
        self.btn_save = QPushButton("另存所选备份包…")
        self.btn_prune = QPushButton("按此立即清理")
        self.btn_upload.setStyleSheet(_WARN_STYLE)
        self.btn_prune.setStyleSheet(_WARN_STYLE)
        self.btn_refresh.clicked.connect(self.reload)
        self.btn_upload.clicked.connect(self._upload_and_restore)
        self.btn_save.clicked.connect(self._save_selected)
        self.btn_prune.clicked.connect(self._do_prune)
        for b in (self.btn_refresh, self.btn_upload, self.btn_save,
                  self.btn_prune):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        box = QGroupBox("自动清理策略")
        form = QFormLayout(box)
        self.sp_keep = QSpinBox()
        self.sp_keep.setRange(0, 999)
        self.sp_keep.setValue(int(getattr(self._cfg, "full_backup_keep", 5) or 0))
        self.sp_days = QSpinBox()
        self.sp_days.setRange(0, 9999)
        self.sp_days.setValue(int(getattr(self._cfg, "full_backup_days", 30) or 0))
        self.sp_max_gb = QDoubleSpinBox()
        self.sp_max_gb.setRange(0.0, 9999.0)
        self.sp_max_gb.setDecimals(1)
        self.sp_max_gb.setSingleStep(0.5)
        self.sp_max_gb.setValue(
            float(getattr(self._cfg, "full_backup_max_gb", 8.0) or 0.0))
        form.addRow("保留最近份数", self.sp_keep)
        form.addRow("超过天数", self.sp_days)
        form.addRow("总量上限（GB）", self.sp_max_gb)
        hint = QLabel("数值为 0 时表示该条规则不生效，始终至少保留最新一份。")
        hint.setWordWrap(True)
        form.addRow("", hint)
        lay.addWidget(box)

        lay.addWidget(QLabel("操作结果："))
        self.lb_status = QLabel("")
        self.lb_status.setWordWrap(True)
        lay.addWidget(self.lb_status)

        bb = QDialogButtonBox()
        bb.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _check_pickers(self):
        """parent 未提供文件/目录选择入口时，禁用对应按钮并给出中文提示。"""
        parent = self.parent()
        self._pick_ok = callable(getattr(parent, "_file_pick", None))
        self._dir_ok = callable(getattr(parent, "_dir_pick", None))
        if not self._pick_ok:
            self.btn_upload.setEnabled(False)
        if not self._dir_ok:
            self.btn_save.setEnabled(False)
        if not (self._pick_ok and self._dir_ok):
            self._status("当前窗口未提供文件选择能力，上传与另存功能已禁用。")

    # ---------------------------------------------------------------- 数据
    def reload(self):
        """列举备份包：名称、大小、时间、可读性与条目数，复用 fullbackup 纯函数。"""
        from .. import fullbackup as fb
        from ..data_backup import human_size

        self._rows = []
        d = _full_dir()
        if d.is_dir():
            for p in sorted(d.glob("*.zip"), reverse=True):
                if not (p.is_file() and fb.is_full_name(p.name)):
                    continue
                st = p.stat()
                rec = {
                    "name": p.name,
                    "path": str(p),
                    "bytes": int(st.st_size),
                    "size_text": human_size(st.st_size),
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime(
                        "%Y-%m-%d %H:%M"),
                    "readable": True,
                    "entries": 0,
                    "error": "",
                }
                try:
                    man = fb.inspect_zip(p)
                    rec["entries"] = int(man.get("entries") or 0)
                    rec["created_at"] = man.get("created_at") or ""
                except Exception as e:
                    rec["readable"] = False
                    rec["error"] = str(e)
                self._rows.append(rec)

        self.tbl.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            cells = (
                r["name"], r["size_text"], r["mtime"],
                "可读取" if r["readable"] else "不可读",
                ("%d 条" % r["entries"]) if r["readable"] else "—",
            )
            for j, val in enumerate(cells):
                it = QTableWidgetItem(val)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.tbl.setItem(i, j, it)
        total = sum(r["bytes"] for r in self._rows)
        if self._rows:
            self.lb_summary.setText("当前共 %d 份完整备份，合计 %s。" % (
                len(self._rows), human_size(total)))
        else:
            self.lb_summary.setText("暂无完整备份包。")
        return self._rows

    def _current(self):
        row = self.tbl.currentRow()
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def _status(self, text: str):
        self.lb_status.setText(text)

    def _confirm_action(self, title: str, text: str) -> bool:
        """统一的二次确认：删除、恢复、覆盖前都必须经过此处。"""
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(text)
        btn_yes = box.addButton("确认", QMessageBox.ButtonRole.YesRole)
        btn_no = box.addButton("取消", QMessageBox.ButtonRole.NoRole)
        box.setDefaultButton(btn_no)
        box.exec()
        return box.clickedButton() is btn_yes

    # ---------------------------------------------------------------- 导入与恢复
    def _upload_and_restore(self):
        """选本地包 → 校验 → 二次确认 → 两段式导入 → 委托恢复。"""
        if not self._pick_ok:
            return
        path = self.parent()._file_pick(
            "选择完整备份包", filter_="ZIP 备份包 (*.zip)")
        if not path:
            return
        from .. import fullbackup as fb
        try:
            man = fb.inspect_zip(path)
        except Exception as e:
            detail = _zh_detail(e)
            self._status("所选文件未通过完整备份包校验，请确认文件来源。"
                         if not detail else
                         "所选文件未通过完整备份包校验：%s。" % detail)
            return
        name = Path(path).name
        entries = int(man.get("entries") or 0)
        if not self._confirm_action(
                "恢复完整备份",
                "确认导入备份包「%s」并执行恢复？\n该包含 %d 个条目。\n"
                "恢复将覆盖当前配置、数据库、规则与缩略图，请先确保已保留"
                "可用备份。" % (name, entries)):
            return
        dest = self._import_package(path)
        if not dest:
            return
        self.reload()
        restorer = getattr(self.parent(), "restore_full_backup", None)
        if callable(restorer):
            restorer(str(dest))
            self._status("已从备份包「%s」发起恢复。" % dest.name)
        else:
            self._status("备份包「%s」已导入，请在主窗口执行从完整备份恢复。"
                         % dest.name)

    def _import_package(self, path) -> Path | None:
        """两段式落盘（对齐 webui._api_full_upload）：.part → 校验 → 命名 → 替换。"""
        from .. import fullbackup as fb
        d = _full_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
            part = d / (fb.full_name() + ".part")
            shutil.copy2(path, part)
            fb.read_manifest(part)
            src = Path(path)
            want = None
            if fb.is_full_name(src.name) and not (d / src.name).exists():
                want = d / src.name
            else:
                now = datetime.now()
                for i in range(120):
                    cand = d / fb.full_name(now + timedelta(seconds=i))
                    if not cand.exists():
                        want = cand
                        break
            if want is None:
                part.unlink(missing_ok=True)
                self._status("备份目录内同名文件过多，请先清理旧备份。")
                return None
            part.replace(want)
            return want
        except Exception:
            try:
                part.unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            self._status("导入备份包失败，请检查文件后重试。")
            return None

    # ---------------------------------------------------------------- 另存
    def _save_selected(self):
        if not self._dir_ok:
            return
        r = self._current()
        if not r:
            self._status("请先选择一份备份包。")
            return
        dest_dir = self.parent()._dir_pick("选择保存目录")
        if not dest_dir:
            return
        src = Path(r["path"])
        dest = Path(dest_dir) / src.name
        if dest.exists() and not self._confirm_action(
                "覆盖已存在的文件",
                "目标目录已存在同名文件「%s」，确认覆盖？" % src.name):
            return
        try:
            shutil.copy2(src, dest)
        except Exception:
            self._status("另存备份包失败，请检查目标目录后重试。")
            return
        self._status("备份包已另存至：%s" % str(dest))

    # ---------------------------------------------------------------- 清理
    def _do_prune(self):
        """按界面策略清理：先列出将删除的包名并二次确认，再执行删除。"""
        from .. import fullbackup as fb
        from ..config import save_config
        from ..data_backup import human_size

        keep = int(self.sp_keep.value())
        days = int(self.sp_days.value())
        gb = float(self.sp_max_gb.value())
        self._cfg.full_backup_keep = keep
        self._cfg.full_backup_days = days
        self._cfg.full_backup_max_gb = gb
        try:
            save_config(self._cfg)
        except Exception:
            self._status("保存清理策略失败，本次仍按界面数值继续清理。")

        max_bytes = int(gb * 1024 ** 3) if gb > 0 else 0
        entries = [(r["name"], r["bytes"]) for r in self._rows]
        plan = fb.prune_plan(entries, keep=keep, days=days,
                             max_bytes=max_bytes)
        if not plan:
            self._status("按当前策略没有需要清理的备份。")
            return
        listing = "\n".join("  - %s" % n for n in plan)
        if not self._confirm_action(
                "清理旧备份",
                "确认按当前策略清理旧备份？将删除以下备份包：\n%s\n"
                "此操作不可撤销。" % listing):
            return
        res = fb.prune_and_delete(_full_dir(), keep=keep, days=days,
                                  max_bytes=max_bytes)
        gone = res.get("deleted") or []
        self.reload()
        self._status("已清理 %d 份旧备份，释放 %s。" % (
            len(gone), human_size(res.get("bytes") or 0)))
