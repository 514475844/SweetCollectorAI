"""代理管理对话框（R47 / BUG_REPORT_ORDER B4+B5 重写）。

用户原话（2026-10-08 真机）：
- B4「类型」应该是**单选框**，现在是个下拉；
- B5 整个对话框**大幅简化**：只要一条代理，不要已保存列表、不要优先级、
  **不要任何测试按钮**（不做代理连通性测试）。

所以这个文件从「代理池管理器」缩成「一条代理的设置框」。上一版是
qBittorrent 式的池化管理（列表 + 优先级 + 全部测试/测试选中），那是
照搬别处的产品形态，不是用户要的形态 —— 这里按用户要求收窄。

**刻意保留的两点**（都是"别把功能改坏"）：
1. 类型用 `QRadioButton` + **同一个 parent**，Qt 才会自动互斥；
   自己维护 `checked` 变量而不用 radio 是本条最容易写错的地方。
2. 数据层仍写进 `ProxyPool`（`pool.save`），**一条覆盖写**而不是清表：
   用户说「只存一条当前代理」，但历史库里若已有旧数据，直接 `DELETE
   FROM proxies` 会把别的模块（如 `resolve()` 按 id 取的那条）一起弄坏；
   覆盖写一条固定 name 的记录，既满足"只有一条"，又不破坏既有引用。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QRadioButton, QVBoxLayout,
)

from ..proxy import ProxyEntry

#: 单一那条代理在库里的固定名字（覆盖写用）。
SINGLE_PROXY_NAME = "default"

#: (单选按钮上显示的文案, 存库的 scheme)。``None`` = 不用代理（直连）。
PROXY_TYPES = (
    ("不使用代理（直连）", ""),
    ("HTTP", "http"),
    ("HTTPS", "https"),
    ("SOCKS5", "socks5"),
)


class ProxyDialog(QDialog):
    """只编辑「当前这一条」代理的设置框。"""

    def __init__(self, pool, parent=None):
        super().__init__(parent)
        self.pool = pool
        self.setWindowTitle("代理设置")
        self.resize(440, 220)
        self._build()
        self.reload()

    # ------------------------------------------------------------------ UI
    def _build(self):
        v = QVBoxLayout(self)

        tip = QLabel("只保存一条代理，填好后所有任务都用它。")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # ---- 类型：QRadioButton 组（同一 parent ⇒ Qt 自动互斥）----
        v.addWidget(QLabel("类型"))
        self.rb_group = QButtonGroup(self)     # 同一 parent → 互斥
        self.rb_group.setExclusive(True)
        self.rb_by_scheme = {}
        for label, scheme in PROXY_TYPES:
            rb = QRadioButton(label, self)
            self.rb_group.addButton(rb)
            self.rb_by_scheme[scheme] = rb
            v.addWidget(rb)

        # ---- 地址：主机 + 端口 ----
        row = QHBoxLayout()
        self.e_host = QLineEdit()
        self.e_host.setPlaceholderText("127.0.0.1")
        from PySide6.QtWidgets import QSpinBox
        self.e_port = QSpinBox()
        self.e_port.setRange(0, 65535)        # 0 = 未填（直连时不必填）
        self.e_port.setValue(8080)
        row.addWidget(self.e_host, 1)
        row.addWidget(QLabel("端口"))
        row.addWidget(self.e_port)
        v.addLayout(row)

        # ---- 用户名 / 密码（多数代理需要，保留但不做掩码以外的花活）----
        row2 = QHBoxLayout()
        self.e_user = QLineEdit()
        self.e_user.setPlaceholderText("用户名（可留空）")
        self.e_pass = QLineEdit()
        self.e_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.e_pass.setPlaceholderText("密码（可留空）")
        row2.addWidget(self.e_user, 1)
        row2.addWidget(self.e_pass, 1)
        v.addLayout(row2)

        # ---- 只有「确定 / 关闭」两个按钮 ----
        # ⚠ 这里**故意没有**「全部测试 / 测试选中 / 全部取消」：用户明确
        #   要求不做任何代理连通性测试（B5）。真要做连通性探测会发起
        #   真实网络请求，既慢又会泄露内网地址，属于用户没要求的行为。
        bb = QDialogButtonBox()
        b_ok = bb.addButton("确定", QDialogButtonBox.ButtonRole.AcceptRole)
        b_cancel = bb.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        b_ok.clicked.connect(self.save_and_close)
        b_cancel.clicked.connect(self.reject)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # ------------------------------------------------------------------ data
    def _current(self) -> ProxyEntry | None:
        """库里那一条（不挑 enabled —— 直连也是一个合法状态）。"""
        try:
            items = self.pool.list(only_enabled=False)
        except TypeError:
            items = self.pool.list()
        return items[0] if items else None

    def reload(self):
        """把库里那条填进表单；库里没有就默认「直连」。"""
        e = self._current()
        if not e:
            self.rb_by_scheme[""].setChecked(True)
            self.e_host.clear()
            self.e_user.clear()
            self.e_pass.clear()
            return
        # 反查 scheme 对应哪个 radio；库里是空 scheme（直连）就点第一个
        rb = self.rb_by_scheme.get(e.scheme or "", self.rb_by_scheme[""])
        rb.setChecked(True)
        self.e_host.setText(e.host or "")
        self.e_port.setValue(int(e.port or 0))
        self.e_user.setText(e.username or "")
        self.e_pass.setText(e.password or "")

    def _selected_scheme(self) -> str:
        for scheme, rb in self.rb_by_scheme.items():
            if rb.isChecked():
                return scheme
        return ""

    def save_and_close(self):
        """覆盖写这一条，然后关窗。直连 → 删掉库里那条（或写一条空的）。"""
        scheme = self._selected_scheme()
        cur = self._current()
        e = ProxyEntry(
            id=cur.id if cur else None,
            name=SINGLE_PROXY_NAME,
            scheme=scheme or "http",   # ProxyEntry 的默认值，直连时不看它
            host=self.e_host.text().strip(),
            port=int(self.e_port.value() or 0),
            username=self.e_user.text().strip(),
            password=self.e_pass.text(),
            enabled=True,
            priority=0,               # B5：不再有优先级概念，恒为 0
        )
        if not scheme:
            # 直连 = 不走代理。库里那条留着但 enabled=False，resolve() 就不会
            # 选它；不 DELETE 是为了不破坏按 id 引用的其它代码路径。
            e.enabled = False
            e.host = e.host or ""
        else:
            err = e.validate()
            if err:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self, "无法保存", f"请检查地址和端口（{err}）")
                return
        try:
            self.pool.save(e)
        except Exception as ex:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "保存失败", f"{type(ex).__name__}: {ex}")
            return
        self.reload()
        self.accept()
