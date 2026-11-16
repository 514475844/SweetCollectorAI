"""桌面端「手机扫码登录」对话框（补齐网页端扫码登录能力）。

协议完全复用 ``webui.py`` 的 qr_login 口径，不另写一套：令牌用
``secrets.token_urlsafe(24)``，登录链接格式为 ``http://{host}/qr-login?token=…``，
状态表为 ``webui._QR_TOKENS``（pending / approved / rejected / used），有效期用
``webui._QR_TOKEN_TTL``，清理用 ``webui._qr_gc``，作废旧令牌用
``webui.reject_qr_token``。状态变为 approved 表示桌面端已通过
``webui.approve_qr_token`` 授权，变为 used 表示确认页已完成登录。

二维码用惰性导入的 ``segno`` 生成并渲染成 QPixmap；组件缺失时给出中文提示
而不是让对话框崩溃。轮询确认状态放在 QThread，关闭时协作式停止并等待收尾。
"""
from __future__ import annotations

import io
import secrets
import time

from PySide6.QtCore import QThread, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout,
)

#: 关闭对话框时仍在执行、暂由模块级集合托管引用的线程。
_ORPHANS: set = set()


def _make_status_fn(webui_mod):
    """构造只读状态查询闭包，供后台线程使用（不触碰任何 Qt 控件）。

    ``approved`` 由 ``webui.approve_qr_token`` 写入，``used`` 由确认页写入；
    本函数只读 ``webui._QR_TOKENS`` 且在其锁内访问，绝不修改协议表。
    """
    def status_of(tok: str) -> str:
        webui_mod._qr_gc()
        ttl = float(webui_mod._QR_TOKEN_TTL)
        with webui_mod._QR_LOCK:
            rec = webui_mod._QR_TOKENS.get(tok)
            if rec is None:
                return "expired"
            if time.time() - rec["created_at"] > ttl:
                return "expired"
            return str(rec.get("status") or "pending")
    return status_of


class _PollWorker(QThread):
    """后台轮询登录确认状态；只发信号，绝不操作界面控件。"""

    confirmed = Signal()
    state = Signal(str)

    def __init__(self, token: str, status_fn, parent=None):
        super().__init__(parent)
        self._token = token
        self._status_fn = status_fn

    def run(self):
        try:
            while not self.isInterruptionRequested():
                st = self._status_fn(self._token)
                if st in ("approved", "used"):
                    if not self.isInterruptionRequested():
                        self.confirmed.emit()
                    return
                if st in ("rejected", "expired"):
                    if not self.isInterruptionRequested():
                        self.state.emit(st)
                    return
                for _ in range(8):
                    if self.isInterruptionRequested():
                        return
                    self.msleep(100)
        except Exception:                       # noqa: BLE001 —— 线程必须兜住
            if not self.isInterruptionRequested():
                self.state.emit("error")


class QRLoginDialog(QDialog):
    """生成一次性登录二维码，轮询确认状态，成功后显示「登录已确认」并关闭。

    构造签名固定为 ``(cfg, parent=None)``，供主窗口接线调用。
    """

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._token = ""
        self._url = ""
        self._webui = None
        self._worker = None
        self.setWindowTitle("手机扫码登录")
        self.setMinimumSize(380, 480)
        self._build()
        self._regenerate()

    # ---------------------------------------------------------------- 界面
    def _build(self):
        lay = QVBoxLayout(self)
        tip = QLabel("请使用手机扫描二维码，并在手机上完成登录确认。")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.lb_qr = QLabel("")
        self.lb_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lb_qr.setMinimumHeight(240)
        lay.addWidget(self.lb_qr, 1)

        self.lb_link = QLabel("")
        self.lb_link.setWordWrap(True)
        self.lb_link.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lb_link)

        self.lb_status = QLabel("正在等待手机端确认…")
        self.lb_status.setWordWrap(True)
        lay.addWidget(self.lb_status)

        row = QHBoxLayout()
        self.btn_regen = QPushButton("重新生成二维码")
        self.btn_copy = QPushButton("复制登录链接")
        self.btn_regen.clicked.connect(self._regenerate)
        self.btn_copy.clicked.connect(self._copy_link)
        row.addWidget(self.btn_regen)
        row.addWidget(self.btn_copy)
        row.addStretch(1)
        lay.addLayout(row)

        bb = QDialogButtonBox()
        bb.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    # ---------------------------------------------------------------- 协议
    def _webui_module(self):
        """惰性导入 webui（不放在模块顶层，避免启动时加载重模块）。"""
        if self._webui is None:
            from .. import webui
            self._webui = webui
        return self._webui

    def _new_token(self, webui_mod):
        """签发一次性令牌并登记到 webui._QR_TOKENS（与网页端同一张表）。"""
        webui_mod._qr_gc()
        tok = secrets.token_urlsafe(24)
        bind = str(getattr(self._cfg, "web_bind", "127.0.0.1")
                   or "127.0.0.1").strip()
        if bind in ("", "0.0.0.0", "::"):
            bind = "127.0.0.1"
        port = int(getattr(self._cfg, "web_port", 8080) or 8080)
        host = "%s:%d" % (bind, port)
        url = "http://%s/qr-login?token=%s" % (host, tok)
        with webui_mod._QR_LOCK:
            webui_mod._QR_TOKENS[tok] = {
                "created_at": time.time(), "status": "pending",
                "ip": "", "url": url,
            }
        return tok, url

    def _render_qr(self, url: str) -> bool:
        """惰性导入 segno 生成二维码并渲染为 QPixmap；缺失时给中文提示。"""
        try:
            import segno
            qr = segno.make(url, error="m")
            buf = io.BytesIO()
            qr.save(buf, kind="png", scale=6, border=2)
            pm = QPixmap()
            if not pm.loadFromData(buf.getvalue()):
                raise ValueError("pixmap")
            self.lb_qr.setPixmap(pm)
            return True
        except Exception:
            self.lb_qr.setPixmap(QPixmap())
            self.lb_qr.setText("二维码生成组件不可用，请安装 segno 依赖后重新生成。")
            return False

    # ---------------------------------------------------------------- 动作
    def _regenerate(self):
        """作废旧令牌、签发新令牌、重绘二维码并重启轮询。"""
        self._stop_worker()
        try:
            webui = self._webui_module()
            if self._token:
                webui.reject_qr_token(self._token)   # 复用 qr_login 纯函数
            self._token, self._url = self._new_token(webui)
        except Exception:
            self.lb_status.setText("登录服务不可用，请稍后重试。")
            self.btn_copy.setEnabled(False)
            return
        self.lb_link.setText("登录链接：%s" % self._url)
        ok = self._render_qr(self._url)
        self.btn_copy.setEnabled(ok)
        self.lb_status.setText("正在等待手机端确认…")
        if ok:
            self._start_worker()

    def _copy_link(self):
        if not self._url:
            return
        QApplication.clipboard().setText(self._url)
        self.lb_status.setText("登录链接已复制到剪贴板。")

    # ---------------------------------------------------------------- 轮询
    def _start_worker(self):
        if self._worker is not None and self._worker.isRunning():
            return
        w = _PollWorker(self._token, _make_status_fn(self._webui_module()))
        self._worker = w
        w.confirmed.connect(self._on_confirmed)
        w.state.connect(self._on_state)
        w.finished.connect(lambda w=w: _ORPHANS.discard(w))
        w.start()

    def _on_confirmed(self):
        self.lb_status.setText("登录已确认")
        self._stop_worker()
        QTimer.singleShot(1200, self._finish_and_close)

    def _on_state(self, st: str):
        self._stop_worker()
        if st == "rejected":
            self.lb_status.setText("登录请求已被拒绝，请重新生成二维码。")
        elif st == "expired":
            self.lb_status.setText("二维码已失效，请重新生成二维码。")
        else:
            self.lb_status.setText("轮询登录状态失败，请重新生成二维码。")

    def _finish_and_close(self):
        self._stop_worker()
        self.accept()

    # ---------------------------------------------------------------- 关闭收尾
    def _stop_worker(self):
        """协作式停止轮询线程：摘信号、请求中断、有限等待，必要时托管强引用。"""
        w = self._worker
        if w is None:
            return
        for sig, slot in ((w.confirmed, self._on_confirmed),
                          (w.state, self._on_state)):
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

    def reject(self):
        self._stop_worker()
        super().reject()

    def accept(self):
        # ⚠ V0.0.17 冒烟抓到的竞态：accept()/done() 这类程序化关闭**不走**
        # closeEvent，也**不走** reject()。轮询线程（_PollWorker，无 parent）
        # 若还在跑，对话框随后被回收 → QThread 带着运行中的线程一起被销毁
        # → Qt 直接 fail-fast（0xC0000409，实测崩溃在退出早期时序）。
        # 所有关闭出口一律先停线程，一个都不能漏。
        self._stop_worker()
        super().accept()

    def done(self, result):
        self._stop_worker()
        super().done(result)

    def closeEvent(self, event):
        self._stop_worker()
        super().closeEvent(event)
