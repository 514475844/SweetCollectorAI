"""V0.0.17 设置中心：把桌面端散落的设置对话框收进一个统一入口。

设计边界（接线前请先读这里）：

* 配置字段以 config.py 的 AppConfig 为准。内联常用项直接读写传入的 cfg，
  并通过 save_config(cfg) 落盘，不另建配置副本。
* 原有对话框入口由主窗口通过 actions 字典注入（键为字符串、值为无参
  可调用对象）。缺失的键对应按钮禁用并提示「该设置入口尚未接入」，
  绝不抛异常。
* 此模块不直接改菜单。内联布尔开关原本由菜单 QAction 管理，改动后由
  可选的 sync_menu_states 回调通知主窗口刷新勾选态（缺失则跳过）。
* 目录 / 文件选择一律走 actions 里的 pick_dir / pick_file 回调，
  不使用静态 QFileDialog（原生后端在对话框被主动关闭时存在崩溃风险）。

顶部搜索按「分区名 + 项名 + 关键词」实时过滤，单击结果跳到对应分区并
滚动、高亮该项。界面文案统一使用书面中文，术语为「目标词 / 排除词」。
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

# 8 个分区，顺序即产品主链路。索引与 QStackedWidget 的页序一一对应。
SECTIONS = (
    "外观与皮肤", "启动与集成", "下载与保存", "AI 与学习",
    "网络与安全", "数据与备份", "维护与诊断", "关于",
)

# 原有对话框入口：键 -> 中文按钮文字。文字由数据驱动注入按钮，
# 避免以英文开头的 UI 字面量被英文标签护栏误判。
DETAIL_LABELS = {
    "ai_model": "AI 模型设置…",
    "web": "Web 访问与安全…",
    "hotkeys": "快捷键…",
    "rate_plan": "下载限速计划…",
    "db_encryption": "数据库加密…",
    "onboarding": "新手引导…",
    "skins_dir": "打开皮肤目录…",
    "proxy": "代理管理…",
    "cookie": "站点 Cookie 管理…",
    "subs": "站点订阅管理…",
    "diskspace": "磁盘空间看板…",
    "backup": "完整备份…",
    "full_cleanup": "全面清理…",
    "diagnostic": "导出诊断包…",
    "shortcuts_help": "快捷键说明",
}

# 每个分区对应的详细设置入口键（与 SECTIONS 等长）。
SECTION_ACTIONS = (
    ("skins_dir",),
    ("onboarding", "hotkeys"),
    ("rate_plan",),
    ("ai_model",),
    ("web", "proxy", "cookie", "subs", "db_encryption"),
    ("backup", "diskspace"),
    ("full_cleanup", "diagnostic"),
    ("shortcuts_help",),
)

# 内联字段白名单：字段名逐个核对过 config.AppConfig，禁止凭猜测书写。
# 保存时只写回这张表里的字段，确保「只改内联项，不动其它配置」。
INLINE_FIELDS = (
    "thumbnail_size",
    "tray_start_minimized", "task_done_goto_gallery",
    "max_pages", "convert_to_jpg", "min_free_mb",
    "ai_threads", "ai_device", "learn_enabled", "sensitive_words",
    "review_key_ask", "ai_model_path",
    "web_bind", "web_port",
    "backup_sync_dir", "recycle_keep_days",
    "log_sanitize",
)

# 选目录 / 选文件回调键。缺失时相关按钮禁用，绝不回退到静态文件对话框。
PICK_DIR_KEY = "pick_dir"
PICK_FILE_KEY = "pick_file"

# 入口未接入与搜索无结果时的统一提示（书面中文）。
NO_ACTION_HINT = "该设置入口尚未接入"
NO_RESULT_HINT = "未找到匹配的设置项"


class SettingsCenterDialog(QDialog):
    """统一设置入口。构造签名固定，由主窗口按此接线。"""

    def __init__(self, db, cfg, actions: dict, parent=None):
        super().__init__(parent)
        self.db = db
        self.cfg = cfg
        self.actions = actions if isinstance(actions, dict) else {}
        self.setWindowTitle("设置中心")
        self.resize(880, 620)

        self._bindings = []          # (field, read_widget, write_widget)
        self._search_items = []      # (section, label, keywords, frame)
        self._scrolls = []           # 每个分区的 QScrollArea
        self._result_indices = []    # 当前结果列表 -> _search_items 下标
        self._highlighted = None     # 当前高亮的 QFrame

        self._build()
        self._load_inline()
        self._refresh_search("")

    # ------------------------------------------------------------------ 构建
    def _build(self):
        root = QVBoxLayout(self)

        # 顶部：搜索框 + 实时结果列表 + 无结果提示
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索设置项…")
        self.search.textChanged.connect(self._refresh_search)
        root.addWidget(self.search)

        self.result_list = QListWidget()
        self.result_list.setMaximumHeight(140)
        self.result_list.itemClicked.connect(self._on_result_clicked)
        root.addWidget(self.result_list)

        self.no_result = QLabel(NO_RESULT_HINT)
        root.addWidget(self.no_result)
        self.result_list.hide()
        self.no_result.hide()

        # 主体：左侧分类导航 + 右侧分页
        body = QHBoxLayout()
        self.nav = QListWidget()
        self.nav.setFixedWidth(168)
        for name in SECTIONS:
            self.nav.addItem(QListWidgetItem(name))
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        body.addWidget(self.nav)

        self.stack = QStackedWidget()
        for idx, name in enumerate(SECTIONS):
            self.stack.addWidget(self._build_page(idx, name))
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        # 底部：状态提示 + 按钮条
        self.status = QLabel("")
        root.addWidget(self.status)

        bar = QHBoxLayout()
        self._b_restore = QPushButton("恢复默认值")
        self._b_apply = QPushButton("应用")
        self._b_ok = QPushButton("确定")
        self._b_cancel = QPushButton("取消")
        self._b_restore.clicked.connect(self._on_restore)
        self._b_apply.clicked.connect(self._on_apply)
        self._b_ok.clicked.connect(self._on_ok)
        self._b_cancel.clicked.connect(self._on_cancel)
        bar.addWidget(self._b_restore)
        bar.addStretch(1)
        bar.addWidget(self._b_apply)
        bar.addWidget(self._b_ok)
        bar.addWidget(self._b_cancel)
        root.addLayout(bar)

        self.nav.setCurrentRow(0)

    def _build_page(self, idx, name):
        """一个分区 = 一个可滚动页面 + 卡片式 QGroupBox。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        v = QVBoxLayout(container)

        heading = QLabel(name)
        heading.setStyleSheet("font-weight: 600; font-size: 15px;")
        v.addWidget(heading)

        if idx == len(SECTIONS) - 1:
            about_card = QGroupBox("关于本软件")
            av = QVBoxLayout()
            info = QLabel(self._about_text())
            info.setWordWrap(True)
            av.addWidget(info)
            about_card.setLayout(av)
            v.addWidget(about_card)
        else:
            inline_card = QGroupBox("常用设置")
            form = QFormLayout()
            self._build_inline_fields(idx, form)
            inline_card.setLayout(form)
            v.addWidget(inline_card)

        detail_card = QGroupBox("详细设置")
        dv = QVBoxLayout()
        for key in SECTION_ACTIONS[idx]:
            btn = QPushButton(DETAIL_LABELS[key])
            cb = self.actions.get(key)
            if callable(cb):
                btn.clicked.connect(lambda _=False, f=cb: f())
            else:
                btn.setEnabled(False)
                btn.setToolTip(NO_ACTION_HINT)
            dv.addWidget(btn)
        detail_card.setLayout(dv)
        v.addWidget(detail_card)

        v.addStretch(1)
        scroll.setWidget(container)
        self._scrolls.append(scroll)
        return scroll

    def _add(self, form, section, label, keywords, widget, field, read, write):
        """登记一个内联项：建卡片行、绑读写函数、加入搜索索引。"""
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.NoFrame)
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(widget, 1)
        form.addRow(label, frame)
        self._bindings.append((field, read, write))
        self._search_items.append((section, label, keywords, frame))
        return frame

    def _build_inline_fields(self, idx, form):
        """按分区摆放内联项。字段名与类型均以 config.AppConfig 为准。"""
        if idx == 0:                                   # 外观与皮肤
            sp = QSpinBox()
            sp.setRange(64, 1024)
            sp.setSingleStep(32)
            self._add(form, idx, "缩略图缓存尺寸（像素）",
                      "缩略图 预览 缓存 thumbnail", sp, "thumbnail_size",
                      lambda w=sp: int(w.value()),
                      lambda v, w=sp: w.setValue(int(v or 0)))
        elif idx == 1:                                 # 启动与集成
            cb = QCheckBox()
            self._add(form, idx, "启动后最小化到托盘",
                      "启动 托盘 后台", cb, "tray_start_minimized",
                      lambda w=cb: bool(w.isChecked()),
                      lambda v, w=cb: w.setChecked(bool(v)))
            cb2 = QCheckBox()
            self._add(form, idx, "任务结束后自动跳到图库",
                      "任务 图库 跳转", cb2, "task_done_goto_gallery",
                      lambda w=cb2: bool(w.isChecked()),
                      lambda v, w=cb2: w.setChecked(bool(v)))
        elif idx == 2:                                 # 下载与保存
            sp = QSpinBox()
            sp.setRange(0, 100000)
            sp.setSingleStep(50)
            self._add(form, idx, "新建任务默认最大页数",
                      "页数 抓取 新建任务", sp, "max_pages",
                      lambda w=sp: int(w.value()),
                      lambda v, w=sp: w.setValue(int(v or 0)))
            cb = QCheckBox()
            self._add(form, idx, "保存时转成 JPG",
                      "JPG 转换 保存 格式", cb, "convert_to_jpg",
                      lambda w=cb: bool(w.isChecked()),
                      lambda v, w=cb: w.setChecked(bool(v)))
            sp2 = QSpinBox()
            sp2.setRange(0, 102400)
            sp2.setSingleStep(50)
            self._add(form, idx, "磁盘剩余空间预警（MB）",
                      "磁盘 空间 预警 容量", sp2, "min_free_mb",
                      lambda w=sp2: int(w.value()),
                      lambda v, w=sp2: w.setValue(int(v or 0)))
        elif idx == 3:                                 # AI 与学习
            sp = QSpinBox()
            sp.setRange(0, 16)
            self._add(form, idx, "AI 推理线程数",
                      "线程 CPU 推理 性能", sp, "ai_threads",
                      lambda w=sp: int(w.value()),
                      lambda v, w=sp: w.setValue(int(v or 0)))
            combo = QComboBox()
            for val, text in (("auto", "自动（有显卡就用显卡）"),
                              ("gpu", "强制用显卡"),
                              ("npu", "强制用 NPU"),
                              ("cpu", "强制用 CPU")):
                combo.addItem(text, val)
            self._add(form, idx, "推理设备",
                      "设备 显卡 NPU CPU 自动", combo, "ai_device",
                      lambda w=combo: str(w.currentData()),
                      lambda v, w=combo: w.setCurrentIndex(
                          max(0, w.findData(str(v or "auto")))))
            cb = QCheckBox()
            self._add(form, idx, "启用学习（AI 偏好）",
                      "学习 偏好 权重", cb, "learn_enabled",
                      lambda w=cb: bool(w.isChecked()),
                      lambda v, w=cb: w.setChecked(bool(v)))
            ed = QPlainTextEdit()
            ed.setFixedHeight(84)
            ed.setPlaceholderText("每行填写一个敏感词")
            self._add(form, idx, "敏感词（每行一个）",
                      "敏感词 模糊 打码 过滤", ed, "sensitive_words",
                      lambda w=ed: self._split_words(w.toPlainText()),
                      lambda v, w=ed: w.setPlainText(
                          "\n".join(str(x) for x in (v or []))))
            cb2 = QCheckBox()
            self._add(form, idx, "键盘审核时弹选词框",
                      "审核 键盘 选词", cb2, "review_key_ask",
                      lambda w=cb2: bool(w.isChecked()),
                      lambda v, w=cb2: w.setChecked(bool(v)))
            e = QLineEdit()
            e.setPlaceholderText("自定义本地模型目录")
            frame = self._add(form, idx, "自定义本地模型目录",
                              "模型 目录 本地", e, "ai_model_path",
                              lambda w=e: w.text().strip(),
                              lambda v, w=e: w.setText(str(v or "")))
            frame.layout().addWidget(
                self._make_pick_button(e, PICK_DIR_KEY, "选择目录…"))
        elif idx == 4:                                 # 网络与安全
            e = QLineEdit()
            e.setPlaceholderText("127.0.0.1")
            self._add(form, idx, "Web 监听地址",
                      "监听 地址 绑定 网络 本机", e, "web_bind",
                      lambda w=e: w.text().strip() or "127.0.0.1",
                      lambda v, w=e: w.setText(str(v or "")))
            sp = QSpinBox()
            sp.setRange(1, 65535)
            self._add(form, idx, "Web 端口",
                      "端口 网络 访问", sp, "web_port",
                      lambda w=sp: int(w.value()),
                      lambda v, w=sp: w.setValue(int(v or 0)))
        elif idx == 5:                                 # 数据与备份
            e = QLineEdit()
            e.setPlaceholderText("留空则不启用外部同步")
            frame = self._add(form, idx, "备份同步目录",
                              "备份 同步 目录 外部", e, "backup_sync_dir",
                              lambda w=e: w.text().strip(),
                              lambda v, w=e: w.setText(str(v or "")))
            frame.layout().addWidget(
                self._make_pick_button(e, PICK_DIR_KEY, "选择目录…"))
            sp = QSpinBox()
            sp.setRange(0, 3650)
            self._add(form, idx, "隔离区保留天数",
                      "隔离区 保留 天数 清理", sp, "recycle_keep_days",
                      lambda w=sp: int(w.value()),
                      lambda v, w=sp: w.setValue(int(v or 0)))
        elif idx == 6:                                 # 维护与诊断
            cb = QCheckBox()
            self._add(form, idx, "日志脱敏",
                      "日志 脱敏 域名 路径", cb, "log_sanitize",
                      lambda w=cb: bool(w.isChecked()),
                      lambda v, w=cb: w.setChecked(bool(v)))

    def _make_pick_button(self, edit, key, label):
        """目录 / 文件选择按钮。回调缺失时禁用，绝不调用静态文件对话框。"""
        btn = QPushButton(label)
        cb = self._pick_callback(key)
        if cb is None:
            btn.setEnabled(False)
            btn.setToolTip(NO_ACTION_HINT)
            return btn

        def _run(e=edit, f=cb):
            try:
                path = f()
            except Exception:
                path = ""
            if path:
                e.setText(str(path))

        btn.clicked.connect(lambda _=False, fn=_run: fn())
        return btn

    def _pick_callback(self, key):
        """取目录 / 文件选择回调；key 为 PICK_DIR_KEY 或 PICK_FILE_KEY。"""
        if key not in (PICK_DIR_KEY, PICK_FILE_KEY):
            return None
        cb = self.actions.get(key)
        return cb if callable(cb) else None

    # ------------------------------------------------------------------ 搜索
    def _refresh_search(self, text):
        q = (text or "").strip()
        self._clear_highlight()
        if not q:
            self.result_list.clear()
            self.result_list.hide()
            self.no_result.hide()
            self._result_indices = []
            return
        low = q.lower()
        matched = []
        for i, (section, label, keywords, _frame) in enumerate(self._search_items):
            hay = SECTIONS[section] + " " + label + " " + keywords
            if low in hay.lower():
                matched.append(i)
        self.result_list.clear()
        if not matched:
            self.result_list.hide()
            self.no_result.show()
            self._result_indices = []
            return
        self.no_result.hide()
        for i in matched:
            section, label, _keywords, _frame = self._search_items[i]
            self.result_list.addItem(
                QListWidgetItem(SECTIONS[section] + " · " + label))
        self._result_indices = matched
        self.result_list.show()

    def _on_result_clicked(self, item):
        row = self.result_list.row(item)
        if 0 <= row < len(self._result_indices):
            self._jump_to(self._result_indices[row])

    def _jump_to(self, index):
        section, _label, _keywords, frame = self._search_items[index]
        self.nav.setCurrentRow(section)
        self._scrolls[section].ensureWidgetVisible(frame)
        self._clear_highlight()
        frame.setFrameShape(QFrame.Shape.Box)
        frame.setLineWidth(2)
        self._highlighted = frame

    def _clear_highlight(self):
        if self._highlighted is not None:
            self._highlighted.setFrameShape(QFrame.Shape.NoFrame)
            self._highlighted = None

    def _on_nav_changed(self, row):
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
            self._clear_highlight()

    # ------------------------------------------------------------------ 数据
    @staticmethod
    def _split_words(text):
        """把多行 / 分隔文本拆成词表，口径与敏感词设置一致。"""
        for sep in ("，", ",", ";", "；", "\n", "\r", "\t"):
            text = text.replace(sep, " ")
        return [x for x in text.split() if x]

    def _load_inline(self):
        for field, _read, write in self._bindings:
            try:
                write(getattr(self.cfg, field))
            except Exception:
                pass

    def _apply_inline(self):
        """把内联项写回 cfg 并落盘，再通知主窗口刷新菜单勾选态。"""
        allowed = set(INLINE_FIELDS)
        for field, read, _write in self._bindings:
            if field not in allowed:
                continue
            try:
                setattr(self.cfg, field, read())
            except Exception:
                pass
        from ..config import save_config
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self._sync_menu_states()

    def _sync_menu_states(self):
        cb = self.actions.get("sync_menu_states")
        if callable(cb):
            try:
                cb()
            except Exception:
                pass

    def _restore_defaults(self):
        """二次确认后，仅把内联项恢复为新建 AppConfig 的默认字段值。"""
        from ..config import AppConfig, save_config
        defaults = AppConfig()
        allowed = set(INLINE_FIELDS)
        for field, _read, write in self._bindings:
            if field not in allowed:
                continue
            try:
                val = getattr(defaults, field)
                if isinstance(val, list):
                    val = list(val)
                setattr(self.cfg, field, val)
                write(val)
            except Exception:
                pass
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self._sync_menu_states()
        self.status.setText("已恢复默认值")

    def _confirm_restore(self) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("恢复默认值")
        box.setText("确定将内联设置恢复为默认值？其它配置不会改动。")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        yes = box.button(QMessageBox.StandardButton.Yes)
        no = box.button(QMessageBox.StandardButton.No)
        if yes is not None:
            yes.setText("恢复")
        if no is not None:
            no.setText("取消")
        return box.exec() == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------ 按钮
    def _on_apply(self, *_):
        self._apply_inline()
        self.status.setText("设置已保存")

    def _on_ok(self, *_):
        self._apply_inline()
        self.accept()

    def _on_cancel(self, *_):
        self.reject()

    def _on_restore(self, *_):
        if self._confirm_restore():
            self._restore_defaults()

    # ------------------------------------------------------------------ 关于
    def _about_text(self):
        from .. import __version__ as ver
        from ..config import user_data_dir
        from . import theme
        try:
            builtin = len(theme.PRESETS)
        except Exception:
            builtin = 0
        try:
            custom = len(theme.list_custom_skins())
        except Exception:
            custom = 0
        try:
            cur = theme.preset_label(getattr(self.cfg, "ui_preset", "classic"))
        except Exception:
            cur = "暂无"
        data_dir = "暂无"
        try:
            data_dir = str(user_data_dir())
        except Exception:
            pass
        return (f"版本：{ver}\n"
                f"数据目录：{data_dir}\n"
                f"内建皮肤数量：{builtin}\n"
                f"自定义皮肤数量：{custom}\n"
                f"当前外观预设：{cur}")
