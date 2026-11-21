"""R154-F22：新手引导（首次运行 3 步向导）。

设计纪律：
- 纯逻辑（`match_adapter_for_url`）与界面（`WizardDialog`）分开；向导本体
  **不碰引擎/队列/下载**——第 3 步「创建并开始」通过回调交给 MainWindow
  （`create_first_task` / `start_first_task`），向导只负责收集输入与校验。
- 三步：① 粘贴网址 → 自动匹配适配器 ② 设 AI 与 Web 密码 ③ 创建第一个任务
  （可选立即开始）。任何一步都能「跳过」（= 标记引导完成，不再弹）。
- 首次运行的触发不在 MainWindow.__init__（测试会直接建 MainWindow，弹模态
  框会挂死）——由 `launch()` 调 `maybe_show_onboarding()`，设置菜单里还有
  「重新运行向导」入口。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget,
    QVBoxLayout, QWidget, QMessageBox,
)

from ..utils.url import domain_of


def _is_http_url(url: str) -> bool:
    u = (url or "").strip()
    return u.startswith("http://") or u.startswith("https://")


def match_adapter_for_url(rules_dir, url: str) -> Optional[str]:
    """按域名匹配适配器规则名；无匹配返回 None（用 gallery-dl 自动模式）。

    与 `main_window._auto_pick_adapter` **同口径**（两处必须一致，否则向导
    显示的匹配结果和任务表单里的自动匹配对不上）：
    - 只认 http(s) URL，其余返回 None；
    - `generic-gallery` 兜底规则**排最后**：它的 allowed_domains 覆盖大批
      站点，先匹配它则专属适配器永远没机会被选中；
    - host 与规则的 base_url 域名 / allowed_domains 做
      「相等 / 后缀（www.x.com vs x.com）/ 前缀」三态匹配。
    """
    url = (url or "").strip()
    if not _is_http_url(url):
        return None
    host = domain_of(url)
    if not host:
        return None
    from ..config import load_rule_file
    rules_dir = Path(rules_dir)
    if not rules_dir.exists():
        return None
    files = []
    for pat in ("*.yaml", "*.yml", "*.json"):
        files.extend(rules_dir.glob(pat))
    files = sorted(files, key=lambda f: (f.stem.lower() == "generic-gallery",
                                         f.stem.lower()))
    for f in files:
        try:
            raw = load_rule_file(f)
        except Exception:
            continue
        domains = [str(raw.get("base_url", ""))]
        domains += [str(d) for d in raw.get("allowed_domains", [])]
        for d in domains:
            d = (d.lower().replace("https://", "")
                 .replace("http://", "").strip(" /"))
            if not d or "." not in d:
                continue
            if host == d or host.endswith("." + d) or d.endswith("." + host):
                return str(raw.get("name") or f.stem)
    return None


def list_rule_names(rules_dir) -> list:
    """规则目录里的适配器名（不带扩展名），排序后返回。"""
    rules_dir = Path(rules_dir)
    if not rules_dir.exists():
        return []
    names = []
    for pat in ("*.yaml", "*.yml", "*.json"):
        names.extend(p.stem for p in rules_dir.glob(pat))
    return sorted(set(names))


class WizardDialog(QDialog):
    """3 步新手向导。回调全部可注入（测试用假回调，MainWindow 传真方法）。"""

    PAGES = 3

    def __init__(self, parent, cfg, db, rules_dir,
                 on_open_manager: Optional[Callable] = None,
                 on_create_task: Optional[Callable] = None,
                 on_start_task: Optional[Callable] = None,
                 on_finish: Optional[Callable] = None):
        super().__init__(parent)
        self.cfg = cfg
        self.db = db
        self.rules_dir = Path(rules_dir)
        self.on_open_manager = on_open_manager
        self.on_create_task = on_create_task
        self.on_start_task = on_start_task
        self.on_finish = on_finish
        self.setWindowTitle("新手引导")
        self.resize(640, 480)
        self._build()
        self._reload_rules()

    # ------------------------------------------------------------------ UI
    def _build(self):
        v = QVBoxLayout(self)
        self.stack = QStackedWidget()
        v.addWidget(self.stack, 1)

        # ---- 第 1 步：粘贴网址 ----
        p1 = QWidget()
        l1 = QVBoxLayout(p1)
        l1.addWidget(QLabel(
            "第 1 步 / 共 3 步：粘贴一个图册/相册网址，软件会自动匹配适配器。\n"
            "没有匹配到也不影响——将使用 gallery-dl 自动模式（支持大批站点）。"))
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("https://example.com/album/123")
        self.url_edit.textChanged.connect(self._on_url_changed)
        l1.addWidget(self.url_edit)
        self.adapter_hint = QLabel("等待网址…")
        self.adapter_hint.setWordWrap(True)
        l1.addWidget(self.adapter_hint)
        l1.addWidget(QLabel("适配器（可手工修改）："))
        self.adapter_combo = QComboBox()
        self.adapter_combo.setEditable(True)
        l1.addWidget(self.adapter_combo)
        self.btn_manager = QPushButton("打开网址与适配器管理器…")
        self.btn_manager.clicked.connect(self._on_open_manager)
        l1.addWidget(self.btn_manager)
        l1.addStretch(1)
        self.stack.addWidget(p1)

        # ---- 第 2 步：AI 与密码 ----
        p2 = QWidget()
        l2 = QVBoxLayout(p2)
        l2.addWidget(QLabel(
            "第 2 步 / 共 3 步：设置 AI（可跳过）与 Web 访问密码。\n"
            "AI 不配置也能用——采集照常，只是没有自动识别/过滤。"))
        l2.addWidget(QLabel("AI 后端："))
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("内置 CLIP（推荐，首次使用无需配置）", "builtin")
        self.backend_combo.addItem("本地模型（指定模型目录）", "local")
        self.backend_combo.addItem("在线 API（OpenAI 兼容接口）", "api")
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
        l2.addWidget(self.backend_combo)
        row = QHBoxLayout()
        self.model_path_edit = QLineEdit()
        self.model_path_edit.setPlaceholderText("本地模型目录（local 模式必填）")
        row.addWidget(self.model_path_edit, 1)
        b_browse = QPushButton("浏览…")
        b_browse.clicked.connect(self._on_browse_model)
        row.addWidget(b_browse)
        l2.addLayout(row)
        self.api_group = QWidget()
        ga = QVBoxLayout(self.api_group)
        ga.setContentsMargins(0, 0, 0, 0)
        self.api_url_edit = QLineEdit()
        self.api_url_edit.setPlaceholderText("https://api.example.com/v1")
        ga.addWidget(QLabel("API 地址："))
        ga.addWidget(self.api_url_edit)
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("API Key")
        ga.addWidget(QLabel("API Key："))
        ga.addWidget(self.api_key_edit)
        self.api_model_edit = QLineEdit()
        self.api_model_edit.setPlaceholderText("模型名（留空用接口默认）")
        ga.addWidget(QLabel("模型名："))
        ga.addWidget(self.api_model_edit)
        l2.addWidget(self.api_group)
        l2.addWidget(QLabel("Web 访问密码（先存着；Web 功能在设置里启用）："))
        self.web_password_edit = QLineEdit()
        self.web_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.web_password_edit.setPlaceholderText("至少 4 位")
        l2.addWidget(self.web_password_edit)
        l2.addStretch(1)
        self.stack.addWidget(p2)

        # ---- 第 3 步：创建第一个任务 ----
        p3 = QWidget()
        l3 = QVBoxLayout(p3)
        l3.addWidget(QLabel(
            "第 3 步 / 共 3 步：创建第一个采集任务（可跳过，以后在任务页随时新建）。"))
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        l3.addWidget(self.summary_label)
        l3.addWidget(QLabel("任务名称："))
        self.task_name_edit = QLineEdit()
        l3.addWidget(self.task_name_edit)
        self.start_now_check = QCheckBox("创建后立即开始采集")
        self.start_now_check.setChecked(True)
        l3.addWidget(self.start_now_check)
        l3.addStretch(1)
        self.stack.addWidget(p3)

        # ---- 底栏 ----
        bb = QDialogButtonBox()
        self.btn_back = bb.addButton("上一步", QDialogButtonBox.ButtonRole.ActionRole)
        self.btn_next = bb.addButton("下一步", QDialogButtonBox.ButtonRole.ActionRole)
        self.btn_skip = bb.addButton("跳过引导", QDialogButtonBox.ButtonRole.RejectRole)
        self.btn_finish = bb.addButton("完成", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_back.clicked.connect(self.back_page)
        self.btn_next.clicked.connect(self.next_page)
        self.btn_skip.clicked.connect(self.skip)
        self.btn_finish.clicked.connect(self.finish)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._sync_nav()
        self._refresh_summary()

    def _reload_rules(self):
        """适配器下拉：自动模式 + 规则目录里的规则名（每次开向导都重拉）。"""
        cur = self.adapter_combo.currentText()
        self.adapter_combo.blockSignals(True)
        self.adapter_combo.clear()
        self.adapter_combo.addItem("(自动 - gallery-dl)", "")
        for name in list_rule_names(self.rules_dir):
            self.adapter_combo.addItem(name, name)
        idx = self.adapter_combo.findText(cur)
        if idx >= 0:
            self.adapter_combo.setCurrentIndex(idx)
        self.adapter_combo.blockSignals(False)

    # ------------------------------------------------------------- 导航
    def _sync_nav(self):
        i = self.stack.currentIndex()
        self.btn_back.setVisible(i > 0)
        self.btn_next.setVisible(i < self.PAGES - 1)
        self.btn_finish.setVisible(i == self.PAGES - 1)
        self.btn_next.setText("下一步" if i < self.PAGES - 1 else "创建并开始")

    def next_page(self):
        i = self.stack.currentIndex()
        if i == 0:
            url = self.url_edit.text().strip()
            if not _is_http_url(url):
                QMessageBox.warning(
                    self, "网址不对",
                    "请粘贴以 http:// 或 https:// 开头的完整网址。")
                return
            if not domain_of(url):
                QMessageBox.warning(self, "网址不对", "该网址无法解析出域名，请更换后重试。")
                return
        if i < self.PAGES - 1:
            self.stack.setCurrentIndex(i + 1)
            self._sync_nav()
            if i + 1 == self.PAGES - 1:
                self._refresh_summary()

    def back_page(self):
        i = self.stack.currentIndex()
        if i > 0:
            self.stack.setCurrentIndex(i - 1)
            self._sync_nav()

    def skip(self):
        """跳过 = 标记引导完成（不再每次启动都弹），不创建任何任务。"""
        self._mark_done()
        self.reject()

    def finish(self):
        url = self.url_edit.text().strip()
        if not _is_http_url(url):
            self.stack.setCurrentIndex(0)
            self._sync_nav()
            QMessageBox.warning(
                self, "网址不对", "请先在第 1 步填写有效的网址。")
            return
        self.apply_settings(self.cfg)
        name = self.task_name_edit.text().strip() or domain_of(url) or "第一个任务"
        adapter = self.adapter_combo.currentData()
        if self.on_create_task is not None:
            tid = self.on_create_task(url, adapter or "", name)
        else:
            tid = self._create_task_default(url, adapter or "", name)
        if self.start_now_check.isChecked() and tid:
            if self.on_start_task is not None:
                self.on_start_task(tid)
            else:
                self._start_task_default(tid)
        self._mark_done()
        self.accept()

    # ------------------------------------------------------------- 数据
    def _mark_done(self):
        self.cfg.onboarding_done = True
        if self.on_finish is not None:
            try:
                self.on_finish()
            except Exception:
                pass

    def apply_settings(self, cfg) -> dict:
        """把第 2 步的输入写进 cfg（不落盘——落盘由 MainWindow 统一做）。"""
        changed = {}
        backend = self.backend_combo.currentData() or "builtin"
        cfg.ai_backend = backend
        changed["ai_backend"] = backend
        if backend == "local":
            p = self.model_path_edit.text().strip()
            cfg.ai_model_path = p
            changed["ai_model_path"] = p
        elif backend == "api":
            cfg.ai_api_url = self.api_url_edit.text().strip()
            cfg.ai_api_key = self.api_key_edit.text()
            cfg.ai_api_model = self.api_model_edit.text().strip()
            changed["ai_api_url"] = cfg.ai_api_url
            changed["ai_api_key"] = cfg.ai_api_key
            changed["ai_api_model"] = cfg.ai_api_model
        pw = self.web_password_edit.text()
        if pw:
            # V0.0.78：密码框占位写着「至少 4 位」，与桌面 Web 设置对话框
            # 保持一致的长度校验 —— 太短就不再静默写入（引导里密码是选填项）。
            if len(pw) < 4:
                QMessageBox.warning(
                    self, "Web 密码太短", "Web 访问密码至少 4 位；本次未保存该密码。")
            else:
                cfg.web_password = pw
                changed["web_password"] = pw
        return changed

    def _on_url_changed(self, _text):
        url = self.url_edit.text().strip()
        if not _is_http_url(url):
            self.adapter_hint.setText("等待网址…")
            return
        matched = match_adapter_for_url(self.rules_dir, url)
        if matched:
            self.adapter_hint.setText(f"已自动匹配适配器：{matched}")
            idx = self.adapter_combo.findText(matched)
            if idx >= 0:
                self.adapter_combo.setCurrentIndex(idx)
        else:
            self.adapter_hint.setText(
                "没有匹配到专属适配器——将使用 gallery-dl 自动模式（支持大批站点）。")
        self._refresh_summary()          # 网址变了 → 第 3 步的任务名预填跟着走

    def _on_backend_changed(self, _i):
        backend = self.backend_combo.currentData() or "builtin"
        self.model_path_edit.setEnabled(backend == "local")
        self.api_group.setEnabled(backend == "api")

    def _on_browse_model(self):
        d = QFileDialog.getExistingDirectory(self, "选择模型目录")
        if d:
            self.model_path_edit.setText(d)

    def _on_open_manager(self):
        if self.on_open_manager is not None:
            self.on_open_manager()

    def _refresh_summary(self):
        url = self.url_edit.text().strip()
        adapter = self.adapter_combo.currentData() or "(自动 - gallery-dl)"
        name = self.task_name_edit.text().strip() or domain_of(url) or "第一个任务"
        self.summary_label.setText(
            f"网址：{url or '（未填写）'}\n"
            f"适配器：{adapter}\n"
            f"任务名：{name}")
        if not self.task_name_edit.text().strip() and url:
            self.task_name_edit.setText(domain_of(url))

    # ------------------------------------------------- 默认回调（未注入时）
    def _create_task_default(self, url, adapter, name):
        from ..models import TaskConfig
        from ..repo import save_task
        t = TaskConfig(id=None, name=name, adapter_name=adapter,
                       entry_urls=[url])
        return save_task(self.db, t)

    def _start_task_default(self, tid):
        from ..repo import get_task
        q = getattr(self.parent(), "_queue", None) if self.parent() else None
        t = get_task(self.db, tid)
        if q is not None and t is not None and tid not in q.running:
            q.submit_many([(t, None)])
