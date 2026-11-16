"""桌面端「本机配置」对话框（补齐网页端 AI 模型页的机器信息面板）。

网页端 ``webui._api_sysinfo`` 走的是 ``sysinfo.collect(cfg)`` /
``sysinfo.headline(d)`` 这一套纯函数（标准库 + ctypes + shutil，不依赖
psutil）。本对话框复用同一份数据源与同一套字节格式化口径，不另写一套采集
逻辑，避免桌面端与网页端显示不一致。显卡等读不到的项目一律标注为
「未检测到」，绝不编造数值。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QVBoxLayout,
)


def _collect(cfg):
    """惰性导入 ``sysinfo``，一次采集并返回（一句话摘要, 逐行明细）。

    与 ``webui._api_sysinfo`` 完全同源：``collect`` 负责取数，``headline``
    负责摘要。本函数不依赖任何 Qt 对象，便于复用与静态核验。
    """
    from .. import sysinfo

    fmt = sysinfo._fmt_bytes
    d = sysinfo.collect(cfg)
    head = sysinfo.headline(d)
    lines = []
    lines.append("软件版本：%s" % (d.get("app_version") or "—"))
    lines.append("运行平台：%s" % (d.get("platform") or "—"))
    dd = d.get("data_dir") or {}
    lines.append("数据目录：%s" % (dd.get("path") or "—"))

    cpu = d.get("cpu") or {}
    lines.append("处理器：%s（%d 逻辑核心）" % (
        cpu.get("name") or "未检测到处理器信息",
        int(cpu.get("cores_logical") or 0)))

    mem = d.get("memory") or {}
    if mem.get("total"):
        lines.append("物理内存：总量 %s，可用 %s" % (
            fmt(mem.get("total")), fmt(mem.get("available"))))
    else:
        lines.append("物理内存：未检测到内存信息")

    gpus = d.get("gpus") or []
    if gpus:
        for g in gpus:
            lines.append("显卡信息：%s，显存 %s" % (
                g.get("name") or "未命名显卡",
                g.get("vram_total_text") or "—"))
    else:
        lines.append("显卡信息：未检测到显卡信息")

    lines.append("磁盘分区：")
    drives = d.get("drives") or []
    if drives:
        for it in drives:
            lines.append("    %s  剩余 %s / 总量 %s" % (
                it.get("label") or it.get("path") or "—",
                it.get("free_text") or "—",
                it.get("total_text") or "—"))
    else:
        lines.append("    未检测到磁盘信息")

    dl = d.get("download_dir")
    if dl:
        lines.append("当前下载目录：%s，剩余 %s" % (
            dl.get("path") or "—", dl.get("free_text") or "—"))
    else:
        lines.append("当前下载目录：尚未配置")
    return head, lines


class SysInfoDialog(QDialog):
    """聚合展示 CPU / 内存 / GPU / 各盘剩余 / 下载目录 / 版本与数据目录。

    构造签名固定为 ``(cfg, parent=None)``，供主窗口接线调用。
    """

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self.setWindowTitle("本机配置")
        self.resize(600, 480)
        self._build()
        self.reload()

    def _build(self):
        lay = QVBoxLayout(self)
        self.lb_head = QLabel("")
        self.lb_head.setWordWrap(True)
        lay.addWidget(self.lb_head)

        tip = QLabel("以下信息来自本机实时采集，未读取到的项目将标注为未检测到。")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.txt = QPlainTextEdit()
        self.txt.setReadOnly(True)
        self.txt.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.txt, 1)

        row = QHBoxLayout()
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.reload)
        row.addWidget(self.btn_refresh)
        row.addStretch(1)
        lay.addLayout(row)

        bb = QDialogButtonBox()
        bb.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def reload(self):
        """重新采集并刷新显示；采集失败给出完整中文句子，不呈现底层异常。"""
        try:
            head, lines = _collect(self._cfg)
            self.lb_head.setText(head)
            self.txt.setPlainText("\n".join(lines))
        except Exception:                       # noqa: BLE001 —— 信息面板不能整页报错
            self.lb_head.setText("")
            self.txt.setPlainText("读取本机配置失败，请稍后重试。")
