"""维护 → 使用在线 AI 生成站点规则…（桌面端对话框）。

口径与网页端 ``webui._api_adapter_ai`` 完全一致：在后台 ``QThread`` 里依次
调用 ``adapter_ai.fetch_page`` → ``propose`` → ``validate``，全部耗时动作都不
落主线程；结果只通过信号回传。用户确认后才用 ``config.dump_rule_file`` 落盘，
并由 ``crawler.rules.load_adapter`` 读回自检，不另写一套规则解析。

模块顶层只依赖标准库与 PySide6；包内模块一律在函数/方法内部惰性导入，
避免把在线模型、抓取、Web 服务等链路拖进导入阶段。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)


def _describe_error(exc: BaseException) -> str:
    """把底层异常转成书面中文说明，不把英文异常原样抛给界面。"""
    name = type(exc).__name__
    text = str(exc).strip()
    if "没有配置在线 AI 地址" in text or "ai_api_url" in text:
        return ("尚未配置在线 AI 接口地址。请到「AI 模型设置 → 在线 API」"
                "填写接口地址与密钥后重试。")
    if "401" in text or "403" in text or "Unauthorized" in text:
        return ("在线模型接口拒绝了请求。请检查 API 密钥是否有效、额度是否充足。")
    if name in ("URLError", "ConnectError", "ConnectTimeout", "gaierror"):
        return ("无法连接在线模型接口或目标站点。请检查网络、代理设置与接口地址。")
    if name in ("TimeoutError", "ReadTimeout", "socket.timeout"):
        return "请求超时。请检查网络状况后重试，或改用其它网址。"
    if name in ("HTTPError", "HTTPStatusError"):
        return ("站点返回了错误状态，可能需要登录或已被限制访问。"
                "请先在浏览器中确认该网址可正常访问。")
    return ("处理过程中出现未预期的状况，未生成可用规则。"
            "请检查在线模型配置与网址后重试；详情可查看日志文件。")


class _GenerateWorker(QThread):
    """后台生成：抓页面 → 生成候选规则 → 本地验证。

    只发信号，绝不触碰任何控件（跨线程操作控件会导致进程崩溃）。
    """

    progress = Signal(int, str)
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, cfg, url: str, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._url = url

    def run(self):
        try:
            from .. import adapter_ai as A
        except Exception:
            self.failed.emit(
                "生成站点规则失败：在线 AI 组件尚未就绪，请确认安装完整后重试。")
            return
        try:
            self.progress.emit(10, "正在抓取页面…")
            html = A.fetch_page(self._cfg, self._url)
            if self.isInterruptionRequested():
                return
            if not html or len(html) < 200:
                self.failed.emit(
                    "页面内容过短，可能被站点拦截或要求登录。"
                    "请先在浏览器中打开该网址确认能够正常看到图片，"
                    "必要时通过浏览器插件把 Cookie 推送给软件后重试。")
                return
            self.progress.emit(45, "正在生成候选规则…")
            # 与网页端共用同一份字段清单模板；取不到时退回空模板，
            # propose 仍可走纯启发式，不影响主流程。
            try:
                from ..webui import adapter_prompt_template
                template = adapter_prompt_template()
            except Exception:
                template = ""
            res = A.propose(self._cfg, self._url, html,
                            template=template, use_ai=True)
            if self.isInterruptionRequested():
                return
            rule = res.get("rule")
            if rule is None:
                notes = "；".join(str(x) for x in (res.get("notes") or []))
                self.failed.emit(
                    "未能生成可用的站点规则。"
                    + (notes or "请更换网址后重试。"))
                return
            self.progress.emit(80, "正在验证规则…")
            verdict = A.validate(rule, html, self._url)
            self.progress.emit(100, "完成")
            self.succeeded.emit({
                "url": self._url,
                "rule": rule,
                "rule_text": res.get("rule_text") or "",
                "source": res.get("source") or "",
                "count": res.get("count") or 0,
                "notes": res.get("notes") or [],
                "verdict": verdict,
            })
        except Exception as e:                      # 作业线程必须兜住全部异常
            self.failed.emit("生成站点规则失败，未获得可用结果：" + _describe_error(e))


class AIAdapterDialog(QDialog):
    """输入网址 → 后台生成 → 预览规则与验证结果 → 确认后落盘并读回自检。"""

    def __init__(self, parent=None, cfg=None):
        super().__init__(parent)
        self._cfg = cfg
        self._worker = None
        self._result = None
        self._closing = False
        self.setWindowTitle("使用在线 AI 生成站点规则")
        self.resize(720, 620)

        v = QVBoxLayout(self)
        intro = QLabel(
            "请填写一个完整的图册页网址（以 http:// 或 https:// 开头）。"
            "软件会抓取该页面，调用已配置的在线模型生成站点规则，"
            "并对规则做本地验证；确认无误后才会写入适配器目录。")
        intro.setWordWrap(True)
        v.addWidget(intro)

        f = QFormLayout()
        self._url_in = QLineEdit()
        self._url_in.setPlaceholderText("https://example.com/gallery")
        f.addRow("目标网址", self._url_in)
        v.addLayout(f)

        row = QHBoxLayout()
        self._b_gen = QPushButton("开始生成")
        self._b_save = QPushButton("保存规则")
        self._b_close = QPushButton("关闭")
        self._b_save.setEnabled(False)
        row.addWidget(self._b_gen)
        row.addWidget(self._b_save)
        row.addStretch()
        row.addWidget(self._b_close)
        v.addLayout(row)

        self._status = QLabel("尚未生成规则。")
        self._status.setWordWrap(True)
        v.addWidget(self._status)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText("识别到的规则与验证结果将在此显示。")
        v.addWidget(self._preview, 1)

        self._b_gen.clicked.connect(self._on_generate)
        self._url_in.returnPressed.connect(self._on_generate)
        self._b_save.clicked.connect(self._on_save)
        self._b_close.clicked.connect(self.reject)

    # ---- 生成流程 ----
    def _set_busy(self, busy: bool, text: str = ""):
        self._b_gen.setEnabled(not busy)
        self._b_save.setEnabled((not busy) and self._result is not None)
        self._b_gen.setText("正在生成…" if busy else "开始生成")
        if text:
            self._status.setText(text)

    def _on_generate(self):
        url = self._url_in.text().strip()
        if not url.lower().startswith(("http://", "https://")):
            QMessageBox.warning(
                self, "网址无效",
                "请填写完整的网址，需以 http:// 或 https:// 开头。")
            return
        # 连续单击「开始生成」不叠加线程：先收掉上一轮。
        self._stop_worker()
        self._result = None
        self._preview.setPlainText("")
        self._set_busy(True, "正在准备生成…")

        w = _GenerateWorker(self._cfg, url)
        # 用默认参数把 worker 身份带进槽，旧线程的迟到信号不会污染新一轮。
        w.progress.connect(lambda p, m, w=w: self._on_progress(w, p, m))
        w.succeeded.connect(lambda r, w=w: self._on_succeeded(w, r))
        w.failed.connect(lambda m, w=w: self._on_failed(w, m))
        w.finished.connect(lambda w=w: self._on_finished(w))
        self._worker = w
        w.start()

    def _on_progress(self, w, pct, msg):
        if w is not self._worker or self._closing:
            return
        self._status.setText(f"{msg}（{pct}%）")

    def _on_succeeded(self, w, result):
        if w is not self._worker or self._closing:
            return
        self._result = result
        self._render(result)

    def _on_failed(self, w, message):
        if w is not self._worker or self._closing:
            return
        self._status.setText(message)
        QMessageBox.warning(self, "生成失败", message)

    def _on_finished(self, w):
        if w is not self._worker:
            return
        self._worker = None
        if not self._closing:
            self._set_busy(False)
            self._b_save.setEnabled(
                bool(self._result and self._result.get("rule_text")))

    # ---- 结果预览 ----
    def _render(self, result):
        rule = result.get("rule")
        verdict = result.get("verdict") or {}
        notes = result.get("notes") or []
        src = {"ai": "在线模型", "heuristic": "纯启发式",
               "none": "未生成"}.get(result.get("source"), "未知")
        if verdict.get("ok"):
            check = ("验证通过：本页匹配到 %d 张图"
                     % int(verdict.get("count") or 0))
        else:
            check = ("验证未通过：" + (verdict.get("reason")
                                    or "规则在本页未匹配到图片"))
        lines = [
            "—— 识别结果 ——",
            f"目标网址：{result.get('url', '')}",
            f"生成方式：{src}",
            f"规则名称：{getattr(rule, 'name', '') or '暂无'}",
            "允许域名："
            + ("、".join(str(x) for x in (getattr(rule, "allowed_domains", []) or []))
               or "暂无"),
            f"入口网址：{getattr(rule, 'base_url', '') or '暂无'}",
            "",
            "—— 本地验证 ——",
            check,
        ]
        warns = verdict.get("warnings") or []
        if warns:
            lines.append("验证提示：" + "；".join(str(x) for x in warns))
        imgs = verdict.get("images") or []
        if imgs:
            lines.append("示例图片：" + "、".join(str(x) for x in imgs[:5]))
        if notes:
            lines.append("")
            lines.append("—— 生成说明 ——")
            lines.extend(str(x) for x in notes)
        lines.append("")
        lines.append("—— 规则正文 ——")
        lines.append(result.get("rule_text") or "暂无规则正文")
        self._preview.setPlainText("\n".join(lines))
        if verdict.get("ok"):
            self._status.setText("生成完成，验证通过，请确认后单击「保存规则」。")
        else:
            self._status.setText(
                "生成完成，但验证未通过，建议更换网址后重试；仍可预览规则正文。")

    # ---- 确认落盘 ----
    def _on_save(self):
        result = self._result
        if not result or not result.get("rule_text"):
            QMessageBox.warning(self, "无法保存", "暂无可保存的规则，请先生成。")
            return
        text = str(result.get("rule_text") or "")
        try:
            from ..config import dump_rule_file, load_rule_file
            from ..crawler.rules import load_adapter
        except Exception:
            QMessageBox.critical(
                self, "保存失败",
                "规则模块尚未就绪，无法保存。请确认安装完整后重试。")
            return
        try:
            rdir = Path(self._cfg.rules_dir)
            rdir.mkdir(parents=True, exist_ok=True)
        except Exception:
            QMessageBox.critical(
                self, "保存失败",
                "适配器目录无法打开，请检查数据目录权限后重试。")
            return
        # 与网页端保存通道同一口径：规则正文先经 load_rule_file 解析成 dict，
        # 再由 load_adapter 校验，不另写一套解析。
        suffix = ".json"
        try:
            json.loads(text)
        except Exception:
            suffix = ".yaml"
        tmp = Path(tempfile.gettempdir()) / (
            f"mrm_rule_ai_{os.getpid()}_{time.time_ns()}{suffix}")
        try:
            tmp.write_text(text, encoding="utf-8")
            try:
                data = load_rule_file(tmp)
                rule = load_adapter(tmp)
            except Exception:
                QMessageBox.critical(
                    self, "保存失败",
                    "规则正文解析或校验失败，未写入任何文件。请重新生成后重试。")
                return
            if not isinstance(data, dict):
                QMessageBox.critical(
                    self, "保存失败",
                    "规则正文不是有效的规则对象，未写入任何文件。")
                return
            name = str(rule.name or "").strip()
            if not name:
                QMessageBox.critical(
                    self, "保存失败", "规则名称为空，未写入任何文件。")
                return
            final = rdir / f"{name}.yaml"
            if final.exists():
                QMessageBox.warning(
                    self, "保存失败",
                    f"已经有一条名为「{name}」的规则，请更换名称后重试。")
                return
            data["name"] = name
            try:
                dump_rule_file(data, final)
                load_adapter(final)          # 写回自检：读不出来就不报成功
            except Exception:
                try:
                    final.unlink(missing_ok=True)
                except OSError:
                    pass
                QMessageBox.critical(
                    self, "保存失败",
                    "写入规则或读回自检失败，未保留半成品文件。"
                    "请检查目录权限后重试。")
                return
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        self._preview.appendPlainText(f"\n已保存并通过读回自检：{final}")
        QMessageBox.information(
            self, "已保存",
            f"站点规则已保存并通过读回自检：\n{final}")
        self.accept()

    # ---- 关闭时安全停线程 ----
    def _stop_worker(self):
        """QThread 在运行状态被析构会让进程直接退出，关闭前必须收干净。"""
        w = self._worker
        if w is None:
            return
        try:
            w.requestInterruption()
            if w.isRunning() and not w.wait(3000):
                w.terminate()
                w.wait(1000)
        except Exception:
            pass

    def done(self, r):
        self._closing = True
        self._stop_worker()
        super().done(r)

    def closeEvent(self, event):
        self._closing = True
        self._stop_worker()
        super().closeEvent(event)
