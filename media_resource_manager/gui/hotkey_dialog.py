"""F19 快捷键设置框（设置 → 快捷键…）。

订单原文（R154 第二组 F19）：
  设置页「快捷键」表：列出主要动作当前键位 → 点击改键 → 冲突中文提示；
  至少覆盖审核流（喜欢/不喜欢/跳过/翻页）、灯箱、任务启停；
  持久化 cfg，恢复默认按钮。

形态取舍：
* **一行一个动作**（动作名 + 键位钮 + 告警），按上下文分组
  （审核页 / 审核灯箱 / 图库灯箱 / 灯箱通用 / 任务）—— 分组同时是
  冲突检测的作用域：审核页的 →（喜欢）和图库灯箱的 →（下一张）
  永远不同时在场，合成一张表报冲突全是误报；
* 键位用**自绘录键按钮**（点一下 → 直接按新键）而不是 QKeySequenceEdit：
  QKeySequenceEdit 在各平台 toString 形态不一（箭头可能给 "Right"），
  且拿不到「只按了修饰键」的中间态；自绘走 hotkeys_qt.seq_from_event，
  捕获口径与匹配口径同源；
* **冲突不挡保存**（订单要的是「中文提示」）：第三列红字写清会怎么叠，
  顶部汇总条计数，用户看清了自己决定；
* 「恢复默认」= 全部动作回注册表默认键（落盘时写空覆盖字典）。

⚠ 所有判定（解析 / 冲突 / 默认值）一律调 hotkeys.py，这里不重写第二份；
   录键只走 hotkeys_qt.seq_from_event（对话框认得、界面不认 = 经典缺陷）。
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from ..hotkeys import (
    CONTEXTS, DEFAULT_HOTKEYS, FIXED_ALIASES, context_actions, display,
    overrides_only, resolve, sanitize_overrides, warnings_for,
)
from .hotkeys_qt import seq_from_event


class _KeyButton(QPushButton):
    """点击进入录键态；下一次按键即为新键位（Esc 取消当次录入）。"""

    seq_changed = Signal(str)

    def __init__(self, seq: str, parent=None):
        super().__init__(parent)
        self._seq = seq
        self._listening = False
        self.setMinimumWidth(120)
        self.setToolTip("点击后按下新键位；含 Ctrl/Alt 的组合键需完整按下。\n"
                        "只按 Ctrl/Shift/Alt 不生效；录入中按 Esc 取消。")
        self.clicked.connect(self._start_listen)
        self._refresh_text()

    # ---------------- 状态 ----------------
    def seq(self) -> str:
        return self._seq

    def set_seq(self, seq: str) -> None:
        self._seq = seq
        self._refresh_text()
        self.seq_changed.emit(seq)

    def _refresh_text(self) -> None:
        self.setText(display(self._seq) if self._seq else "（未绑定）")

    # ---------------- 录键 ----------------
    def _start_listen(self) -> None:
        self._listening = True
        self.setText("请按下新键…")
        self.setFocus()
        try:
            self.grabKeyboard()
        except Exception:
            pass

    def _stop_listen(self) -> None:
        self._listening = False
        try:
            self.releaseKeyboard()
        except Exception:
            pass
        self._refresh_text()

    def _cancel_listen(self) -> None:
        self._stop_listen()

    def keyPressEvent(self, ev: QKeyEvent) -> None:  # noqa: N802
        if not self._listening:
            super().keyPressEvent(ev)
            return
        from PySide6.QtCore import Qt as _Qt
        if int(ev.key()) == int(_Qt.Key.Key_Escape):
            self._cancel_listen()
            ev.accept()
            return
        s = seq_from_event(ev)
        if s is None:
            # 只按了修饰键 / 认不出的键：继续等，不让录键态悄悄结束
            self.setText("请按一个实际键位的组合（不要只按 Ctrl/Shift/Alt）…")
            ev.accept()
            return
        self._stop_listen()
        self._seq = s
        self._refresh_text()
        self.seq_changed.emit(s)
        ev.accept()

    def focusOutEvent(self, ev) -> None:  # noqa: N802
        if self._listening:
            self._cancel_listen()
        super().focusOutEvent(ev)


class HotkeyDialog(QDialog):
    """快捷键设置表。用法：``dlg.exec()`` 为真 = 已写入 cfg 并落盘。"""

    def __init__(self, parent=None, cfg=None):
        super().__init__(parent)
        self.setWindowTitle("快捷键")
        self.resize(640, 620)
        self._cfg = cfg
        self._buttons: dict[str, _KeyButton] = {}
        self._warn_labels: dict[str, QLabel] = {}
        root = QVBoxLayout(self)

        root.addWidget(QLabel(
            "点击键位按钮后按下新键即可修改；含 Ctrl/Alt 的组合键需完整按下，"
            "录入中按 Esc 取消。\n"
            "保存后立即生效（审核页 / 灯箱 / 任务启停；Web 端键位不在此表）。"))

        alias_txt = "；".join(f"{display(k)} {d}" for k, d, _o in FIXED_ALIASES)
        root.addWidget(QLabel(
            f"以下键始终有效、不参与自定义：{alias_txt}。"))

        self.lbl_warn = QLabel("")
        self.lbl_warn.setWordWrap(True)
        root.addWidget(self.lbl_warn)

        for ctx in CONTEXTS:
            root.addWidget(_section(ctx))
            for act in context_actions(ctx):
                row = QHBoxLayout()
                lab = QLabel(act["label"])
                lab.setMinimumWidth(240)
                btn = _KeyButton(resolve(getattr(cfg, "hotkeys", None),
                                         act["id"]))
                btn.seq_changed.connect(lambda _s, a=act["id"]:
                                        self._on_row_changed(a))
                warn = QLabel("")
                warn.setWordWrap(True)
                row.addWidget(lab)
                row.addWidget(btn)
                row.addWidget(warn, 1)
                root.addLayout(row)
                self._buttons[act["id"]] = btn
                self._warn_labels[act["id"]] = warn

        row_btn = QHBoxLayout()
        self.btn_reset = QPushButton("恢复默认")
        self.btn_reset.clicked.connect(self.reset_default)
        row_btn.addWidget(self.btn_reset)
        row_btn.addStretch(1)
        root.addLayout(row_btn)

        self.box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel)
        self.box.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        self.box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        self.box.accepted.connect(self._on_save)
        self.box.rejected.connect(self.reject)
        root.addWidget(self.box)

        self._refresh_warnings()

    # ---------------- 收集 ----------------
    def current_map(self) -> dict:
        """界面上的完整生效键位（action_id → 规范序列串）。"""
        return {aid: btn.seq() for aid, btn in self._buttons.items()}

    def current_overrides(self) -> dict:
        """与默认不同的部分（落盘用；全默认 = {}）。"""
        return overrides_only(self.current_map())

    def reset_default(self) -> None:
        for aid, btn in self._buttons.items():
            btn.set_seq(DEFAULT_HOTKEYS[aid])
        self._refresh_warnings()

    # ---------------- 告警 ----------------
    def _on_row_changed(self, _aid: str) -> None:
        self._refresh_warnings()

    def _refresh_warnings(self) -> None:
        warns = warnings_for(self.current_map())
        for aid, lab in self._warn_labels.items():
            txt = warns.get(aid, "")
            lab.setText(("⚠ " + txt) if txt else "")
        if warns:
            self.lbl_warn.setText(
                f"有 {len(warns)} 处键位告警（见下行红字）：同一个键会触发两个动作。"
                f"确认效果无误后再保存。")
        else:
            self.lbl_warn.setText("")

    def warnings_text(self) -> dict:
        """当前告警（测试/调用方用，与界面同一口径）。"""
        return warnings_for(self.current_map())

    # ---------------- 落盘 ----------------
    def apply_to(self, cfg) -> None:
        """把表单写进 cfg（**不**落盘：落盘由调用方决定，方便测试）。"""
        cfg.hotkeys = sanitize_overrides(self.current_overrides())

    def _on_save(self):
        if self._cfg is not None:
            self.apply_to(self._cfg)
        self.accept()


def _section(title: str) -> QWidget:
    lab = QLabel(title)
    lab.setStyleSheet("font-weight:600;margin-top:6px;")
    return lab
