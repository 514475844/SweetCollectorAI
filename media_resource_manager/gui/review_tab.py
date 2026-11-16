"""Review queue tab: human decisions feed the local preference learner."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QEvent, QTimer
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QMessageBox, QMenu, QSpinBox, QSlider, QCheckBox,
    QFrame,
)

from ..classify.label_bridge import LabelBridge
from ..classify import preannotate
from ..classify.preference import PreferenceLearner
from ..db import Database
from .theme import style as _tstyle      # P1-4：行内样式也要跟随皮肤
from ..models import ReviewDecision
from ..repo import get_image, upsert_image
from ..review.queue import ReviewQueue
# r40：权重明细里展示的标签走 i18n（显示中文），但存库/学习仍用原始英文 key
from ..i18n import label_text

from . import icons
from .dislike_dialog import (ask_dislike_reason, consume_manual_words,
                             consume_correct_labels)
from .zh_labels import zh_buttons
from .lightbox import Lightbox
from .table_view import PreviewLabel, apply_grid_layout, square_thumb_pixmap

# #56 A4：流式取 —— 每次查库多拉 STREAM_SPARE 张备着，下一页优先消费余量，
# 不用每判一张就同步查一次库（连着刷几十张时省掉几十次 SQL 往返）。
STREAM_SPARE = 25
# #56 A4：断点续审 —— 记下「刚审过的最大图片 id」，下次打开审核页光标直接落到
# 它之后的第一张未审图，而不是回到队列顶部从头翻。
CHECKPOINT_KEY = "review_checkpoint_v1"


def card_base_and_tip(rec, p: Path) -> tuple:
    """卡片基础两要素：文字行 + 悬浮提示（V0.0.8 起都带「送审原因」）。

    _make_grid_item 与 _apply_preanno_to_grid 共用这一份 —— 预判徽标
    来回刷新时不能把原因刷丢，两处各写一套早晚对不上。
    模块级函数（不是方法）：两处调用都只用 rec/path，测试替身和真实
    类走同一条渲染路（R117 的 _Tab stub 只绑方法，挂实例上会把替身打红）。
    """
    base = f"#{rec.id} score={(rec.score or 0.0):.2f}"
    tip = f"{p}\n{rec.source_url or ''}\ntags={rec.tags}"
    rs = str(getattr(rec, "review_reason", "") or "")
    if rs:
        short = rs if len(rs) <= 24 else rs[:23] + "…"
        base += "\n" + short
        tip += f"\n送审原因：{rs}"
    return base, tip


class ReviewTab(QWidget):
    def __init__(self, db: Database, isolate_dir: Path, log, cfg=None):
        super().__init__()
        self.db = db
        self.cfg = cfg
        self.queue = ReviewQueue(db, isolate_dir, cfg)
        self.log = log
        self._page_size = 50          # 单页显示多少张（用户可调）
        self._multi_mode = False      # 多选模式：点击逐个勾选
        # #56 A4：流式取余量 + 断点续审。
        # _stream_spare 是「上次查库多拉的那批、还没显示的记录」，下一次 reload
        # 优先从这里切页；_last_decided_id 是断点（刚审过的最大 id）。
        self._stream_spare = []
        self._last_decided_id = None
        # #56 A4：断点只在本页"第一次铺图"时生效一次（见 reload 里的 _ckpt_applied）
        self._ckpt_applied = False
        # r39：主窗口引用（_build 里由 MainWindow 反向挂上来，见 main_window
        # 页1/页2 那段）；拿得到才能走异步缩略图，拿不到就退回「有缓存才同步取」
        self._win = None
        self._thumb_size = 320        # 缩略图缓存边长（放大查看时用）
        # R154-F21：当前页 AI 预判缓存 {image_id: PreAnnotation}。
        # 预判是**展示层**：翻页/重铺时由 reload 重算，开关翻转时重刷文案。
        self._preanno_cache = {}
        self._build()
        # #56 A4 断点续审：开页先把上次审到哪读回来（读不到就 None，不影响审核）
        try:
            self._last_decided_id = self._load_checkpoint()
        except Exception:
            self._last_decided_id = None
        self._install_undo_shortcut()
        # 改「每页」张数时防抖：松手后再 reload，避免拖动数字时反复重建缩略图卡顿
        self._page_timer = QTimer(self)
        self._page_timer.setSingleShot(True)
        self._page_timer.timeout.connect(self.reload)

    def _install_undo_shortcut(self):
        """#12 Ctrl+Z 撤销上一步审核决定（只在审核页生效）。"""
        try:
            from PySide6.QtGui import QKeySequence, QShortcut
            sc = QShortcut(QKeySequence("Ctrl+Z"), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(self.undo_review)
        except Exception:
            pass

    def undo_review(self):
        """#12 撤销上一步：图片状态 + 文件位置 + 学习权重一起回滚。"""
        if not self.queue.can_undo():
            QMessageBox.information(self, "撤销", "没有可撤销的审核操作")
            return
        label = self.queue.undo_label()
        desc = self.queue.undo()
        if desc:
            self.log(f"审核撤销: {label} —— {desc}")
        self.reload()

    def _build(self):
        from PySide6.QtWidgets import QComboBox
        v = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(QLabel("任务:"))
        self.task_combo = QComboBox()
        self.task_combo.addItem("全部任务", None)
        self.task_combo.currentIndexChanged.connect(self._on_task_changed)
        self.task_combo.setToolTip("每个任务有各自的审核队列")
        top.addWidget(self.task_combo, 1)
        top.addWidget(QLabel("每页"))
        self.sb_page = QSpinBox()
        self.sb_page.setRange(12, 300)
        self.sb_page.setValue(self._page_size)
        self.sb_page.setSuffix(" 张")
        self.sb_page.setToolTip("单页最多显示多少张待审核图")
        self.sb_page.valueChanged.connect(self._on_page_size)
        top.addWidget(self.sb_page)
        top.addWidget(QLabel("预览"))
        self.s_thumb_rev = QSlider(Qt.Orientation.Horizontal)
        self.s_thumb_rev.setRange(64, 420)
        self.s_thumb_rev.setValue(int(getattr(self.cfg, "review_thumb_size", 160)))
        self.s_thumb_rev.setFixedWidth(120)
        self.s_thumb_rev.setToolTip("审核页缩略图（预览）尺寸；拖动即时生效，"
                                    "最大化窗口会自动跟着放大")
        self.s_thumb_rev.valueChanged.connect(self.set_review_thumb_size)
        top.addWidget(self.s_thumb_rev)
        self.l_thumb_rev = QLabel(f"{self.s_thumb_rev.value()}px")
        self.l_thumb_rev.setFixedWidth(40)
        self.s_thumb_rev.valueChanged.connect(
            lambda v: self.l_thumb_rev.setText(f"{v}px"))
        top.addWidget(self.l_thumb_rev)
        # 列表模式：与旧版隔离区同款的表格（ID/文件名/状态/大小/AI分/任务/网站）
        self.b_rmode = QPushButton("列表模式")
        icons.set_themed_icon(self.b_rmode, "list", "{text_dim}", 15)
        self.b_rmode.setCheckable(True)
        self.b_rmode.setToolTip("缩略图网格 / 信息表格 两种视图切换；表格模式下同样"
                                "支持 ↑↓ 选择 · → 喜欢 · ← 不喜欢")
        self.b_rmode.toggled.connect(self.toggle_list_mode)
        top.addWidget(self.b_rmode)
        b_apply = QPushButton("应用学到的排除词")
        icons.set_themed_icon(b_apply, "shield", "{accent}", 15)
        b_apply.setIconSize(QSize(15, 15))
        b_apply.setToolTip("将在审核页或图库中标记为「不喜欢」所学到的标签写入任务 AI 排除词，"
                           "下次运行自动跳过这些图")
        _tstyle(b_apply,
                "QPushButton{background:{accent_soft};border:none;border-radius:7px;"
                "color:{accent_deep};font-size:13px;font-weight:600;}"
                "QPushButton:hover{background:{accent_soft_hover};}")
        b_apply.clicked.connect(self.apply_learned_exclusions)
        top.addWidget(b_apply)
        # r33：自动补满挪到这一行**末尾**（前面是「按哪个任务 / 每页多少张 / 看多大预览」，
        # 到这里都是"审完之后怎么继续"的动作，它跟这套语义一致，不再夹在滑块中间）。
        # 前面加一条竖分隔，一眼能看出是动作区里的开关、不是第二个滑块。
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        _tstyle(line, "color:{border};")
        line.setFixedWidth(16)
        top.addWidget(line)
        self.cb_autofill = QCheckBox("自动补满")
        self.cb_autofill.setChecked(
            bool(getattr(self.cfg, "review_auto_fill", True)))
        self.cb_autofill.setToolTip(
            "判定「喜欢 / 不喜欢」后，自动从待审核队列补充图片并填满本页（每页 50 张，"
            "逐张判定后自动续补，无需手工刷新）。\n"
            "关闭后：仅审核本页原有图片，不补充新图 —— 需要「先筛选喜欢的、"
            "其余全选标记为不喜欢」时，不会被新图片插入。需要下一批时单击「刷新」。")
        self.cb_autofill.stateChanged.connect(self._on_auto_fill)
        top.addWidget(self.cb_autofill)
        v.addLayout(top)

        # 主单项7：「待审核」标题行右侧加「刷新」按钮（位置在标题最右、
        # 自动补满那一行下方）。以前只能靠右键菜单里的「刷新」——
        # 用户找不到右键入口时就卡在这一页。reload() 与右键菜单、
        # 需求17 的手动刷新是同一个方法（无条件补满，不理会开关）。
        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(8)
        self.info = QLabel("待审核 0 张（隔离区中等待判定的图片）")
        _tstyle(self.info, "QLabel{color:{text_dim};font-size:13px;}")
        info_row.addWidget(self.info)
        info_row.addStretch(1)
        self.b_refresh = QPushButton("刷新")
        icons.set_themed_icon(self.b_refresh, "refresh", "{text_dim}", 15)
        self.b_refresh.setIconSize(QSize(15, 15))
        self.b_refresh.setToolTip(
            "重新拉取待审核列表（无条件补满本页，不理会「自动补满」开关）")
        self.b_refresh.clicked.connect(lambda: self.reload(fill=True))
        info_row.addWidget(self.b_refresh)
        v.addLayout(info_row)

        self.grid = QListWidget()
        # 与图库网格共用一个几何函数（间距 8 / 单元格=图+28 / 禁拖拽），
        # 两处缩略图网格长得一模一样；尺寸由 apply_grid_size() 按设置再设一次。
        apply_grid_layout(self.grid, 160)
        # 多选：Ctrl/Shift 任选；「多选模式」下点缩略图逐个勾选（见右键菜单）
        self.grid.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.grid.itemDoubleClicked.connect(self.open_file)
        # 右键菜单承载「喜欢/不喜欢/跳过/全选/刷新」等（底部只留整页级动作）
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._grid_menu)
        # 键盘流（见 eventFilter）：默认 ← → 切换 · ↑ 不喜欢 · ↓ 喜欢 · 右Ctrl 待定
        # F19：键位可自定义 ⇒ 提示文案按当前生效键位现场拼（sync_hotkey_hints）
        self.grid.installEventFilter(self)
        self.sync_hotkey_hints()
        v.addWidget(self.grid)

        # ---- 列表模式（与旧版隔离区同款：表格 + 右侧预览）----
        from .table_view import make_table
        self._list_mode = False
        self.table = make_table(
            on_like=lambda: self.decide(ReviewDecision.LIKE, keyboard=True),
            on_dislike=lambda: self.decide(ReviewDecision.DISLIKE, keyboard=True))
        prev_box = QWidget()
        pv = QVBoxLayout(prev_box)
        pv.setContentsMargins(8, 0, 0, 0)
        pv.setSpacing(6)
        pv.addWidget(QLabel("<b>预览</b>"))
        self.t_prev_img = PreviewLabel("（选中一行看预览）")
        self.t_prev_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(self.t_prev_img,
                "QLabel{background:{surface_alt};border:1px solid {border};"
                "border-radius:8px;color:{text_dim};}")
        pv.addWidget(self.t_prev_img, 1)
        self.t_prev_txt = QLabel("")
        self.t_prev_txt.setWordWrap(True)
        _tstyle(self.t_prev_txt, "QLabel{color:{text_dim};font-size:12px;}")
        pv.addWidget(self.t_prev_txt)
        from PySide6.QtWidgets import QSplitter
        self.t_split = QSplitter()
        self.t_split.addWidget(self.table)
        self.t_split.addWidget(prev_box)
        self.t_split.setStretchFactor(0, 1)
        self.t_split.setStretchFactor(1, 0)
        self.t_split.setVisible(False)
        self.table.itemSelectionChanged.connect(self._table_preview)
        # 需求 3：列表模式双击也开灯箱（跟网格模式一致）。
        # 原来只有网格的 itemDoubleClicked 接到 open_file，列表里双击没反应，
        # 用户在这两处来回切时习惯不一致。open_file 内部已按「当前是列表还是
        # 网格」取 id，不用改。
        self.table.cellDoubleClicked.connect(
            lambda *a: self.open_file())
        # #88 列表模式此前漏了右键菜单（只有缩略图网格绑了 _grid_menu），
        # 这里补上同一套审核动作，保证「列表模式」和「网格模式」操作力一致。
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        v.addWidget(self.t_split, 1)

        row = QHBoxLayout()
        # —— 底部只保留整页级动作（用户要求精简）——
        # 需求 20：底部「全部跳过」按钮一并取消。理由同上：
        # 一键给整页定生死，误点代价太大。要清队列就「全选」+「跳过」。
        b_sample = QPushButton("随机抽检")
        icons.set_themed_icon(b_sample, "search", "{accent}", 15)
        b_sample.setToolTip("随机抽 12 张「AI 自动隔离」的图拉进审核，防误杀——"
                            "阈值/排除词太狠会把好图也隔离，定期抽一批人工过一遍")
        b_sample.clicked.connect(self.sample_review)
        b_smart = QPushButton("智能抽样")
        icons.set_themed_icon(b_smart, "bulb", "{accent}", 15)
        b_smart.setToolTip("主动学习：优先选取模型判定最不确定的 20 张（AI 分数最接近阈值）"
                           "拉进审核——边界样本信息量最大，标几张就能把决策边界学准")
        b_smart.clicked.connect(self.smart_review)
        # #12 撤销：Ctrl+Z 回滚上一步（图片状态 + 文件位置 + 学习权重）
        self.b_undo = QPushButton("撤销")
        icons.set_themed_icon(self.b_undo, "refresh", "{warn_text}", 15)
        self.b_undo.setToolTip("撤销上一步审核决定（Ctrl+Z），最近 50 步\n"
                               "图片状态、文件位置、学到的权重一起回滚")
        self.b_undo.clicked.connect(self.undo_review)
        self.b_undo.setEnabled(False)
        for b in (b_sample, b_smart, self.b_undo):
            b.setIconSize(QSize(15, 15))
            _tstyle(b,
                    "QPushButton{background:{surface_alt};border:none;border-radius:7px;"
                    "color:{text};font-size:13px;font-weight:600;}"
                    "QPushButton:hover{background:{accent_soft_hover};}")
            row.addWidget(b)
        v.addLayout(row)
        # ---- 偏好模型：摘要 + 「权重明细」打开可管理的表格（#13 权重可视化）----
        prow = QHBoxLayout()
        self.prefs = QLabel("偏好模型：尚无数据")
        prow.addWidget(self.prefs, 1)
        b_prefs = QPushButton("权重明细")
        icons.set_themed_icon(b_prefs, "list", "{accent}", 15)
        b_prefs.setIconSize(QSize(15, 15))
        b_prefs.setToolTip("查看模型已学习的内容，并可删除错误的权重条目")
        _tstyle(b_prefs,
                "QPushButton{background:{accent_soft};border:none;border-radius:7px;"
                "color:{accent_deep};font-size:13px;font-weight:600;}"
                "QPushButton:hover{background:{accent_soft_hover};}")
        b_prefs.clicked.connect(self.show_prefs_table)
        prow.addWidget(b_prefs)
        # ---- R154-F21：AI 预标注（预判只展示，采纳才写学习权重）----
        self.cb_preanno = QCheckBox("AI 预标")
        self.cb_preanno.setToolTip(
            "对当前页每张图预判「喜欢/不喜欢」倾向，标在缩略图文字下：\n"
            "依据 = 图上已有 AI 标签 × 已学偏好权重（均值口径），纯本地计算。\n"
            "预判不写任何学习权重；点右侧「全部采纳本页预标」才按真实判定写。")
        self.cb_preanno.toggled.connect(self._on_preanno_toggled)
        prow.addWidget(self.cb_preanno)
        self.b_adopt_preanno = QPushButton("全部采纳本页预标")
        icons.set_themed_icon(self.b_adopt_preanno, "checklist", "{accent}", 15)
        self.b_adopt_preanno.setIconSize(QSize(15, 15))
        self.b_adopt_preanno.setToolTip(
            "把当前页 AI 预判按倾向批量执行：喜欢倾向→喜欢、不喜欢倾向→不喜欢，\n"
            "「预判 —」的图不动。与手工判定同一条事务通道（学习/断点/补满都在），\n"
            "喜欢、不喜欢各成一个撤销事务，Ctrl+Z 可分别整批撤销。")
        self.b_adopt_preanno.clicked.connect(self.adopt_preanno)
        _tstyle(self.b_adopt_preanno,
                "QPushButton{background:{accent_soft};border:none;border-radius:7px;"
                "color:{accent_deep};font-size:13px;font-weight:600;}"
                "QPushButton:hover{background:{accent_soft_hover};}")
        prow.addWidget(self.b_adopt_preanno)
        v.addLayout(prow)
        self.apply_grid_size()

    def toggle_list_mode(self, on: bool):
        """审核页 缩略图网格 / 信息表格 视图切换（表格与旧版隔离区同款）。"""
        self._list_mode = bool(on)
        self.b_rmode.setText("网格模式" if on else "列表模式")
        # P1-4：图标改走 set_themed_icon —— 原先 `color="#5a6271"` 是写死的
        # 深色字，深色主题下按钮上直接看不见。set_themed_icon 会登记，
        # 切深浅/换皮肤时由 icons.restyle_themed_icons() 重烤。
        # 这里每次切换都重新登记一次：图标名字随 on 变（grid/list 互换）。
        icons.set_themed_icon(self.b_rmode, "grid" if on else "list",
                              "{text_dim}", 15)
        self.grid.setVisible(not on)
        self.t_split.setVisible(on)
        self.reload()
        # R51：切列表/网格模式后，水印浮层可能停在旧 z 序被内容盖住
        # （固定 alpha 在不同底色上看似「透明度不一致」）。切完立刻让主窗口
        # 把三页水印统一重抬一次。
        win = getattr(self, "_win", None)
        if win is not None:
            try:
                win._refresh_watermarks()
            except Exception:
                pass

    def _table_preview(self):
        """列表模式右侧预览：跟随表格选中行。"""
        ids = self._selected_ids()
        img, txt = self.t_prev_img, self.t_prev_txt
        from PySide6.QtGui import QPixmap
        if not ids:
            img.clear_source()
            txt.setText("")
            return
        rec = get_image(self.db, ids[0])
        if not rec:
            return
        p = Path(rec.path) if rec.path else None
        if p and p.exists():
            pm = QPixmap(str(p))
            if not pm.isNull():
                # 原图存进预览框，按比例缩放到**当前**宽高；以后拖分割条
                # 改变窗格大小，resizeEvent 会自动重画（不再写死 240×240）。
                img.setSource(pm)
            else:
                img.clear_source("（这个文件不是有效图片）")
        else:
            img.clear_source("（磁盘上已无此文件）")
        txt.setText(f"#{rec.id}  {rec.width or '?'}x{rec.height or '?'}  "
                    f"AI分 {rec.score or 0:.3f}\n{p.name if p else '(无路径)'}\n"
                    f"{rec.source_url or ''}"
                    + (f"\n送审原因：{rec.review_reason}"
                       if getattr(rec, "review_reason", "") else ""))

    # ---------------------------------------------- #56 A4 流式取 / 断点续审
    def _fetch_pending(self, tid, limit: int):
        """取待审核列表，**优先消费上次多拉的余量**（流式取）。

        以前每判一张就 ``queue.pending(limit=page_size)`` 查一次库，连着刷
        几十张 = 几十次 SQL 往返 + 每次都重新排一遍序。现在第一次查库时按
        ``limit + STREAM_SPARE`` 多拉一点存在 ``_stream_spare``，下次先把余量
        切出来用；余量不够了才再查库。

        返回 ``(items, from_spare: bool)``；``from_spare=True`` 表示这次没查库。
        """
        want = max(1, int(limit))
        spare = list(getattr(self, "_stream_spare", None) or [])
        if len(spare) >= want:
            items = spare[:want]
            self._stream_spare = spare[want:]
            return items, True
        # 余量不够：重新查库（连同余量一起多拉）
        got = list(self.queue.pending(tid, limit=want + STREAM_SPARE) or [])
        got = [r for r in got if r is not None]
        items = got[:want]
        self._stream_spare = got[want:]
        return items, False

    def _save_checkpoint(self, image_id):
        """#56 A4 断点续审：记下刚审过的 id（持久化到 app_meta）。

        存的是**整个程序生命周期**的断点，不按任务分开 —— 用户的心智模型是
        「我上次审到哪了」，切任务时上一张也确实是断点。存不进去（老库没这张表）
        也绝不能影响审核：包 try，失败只丢断点，不丢判定。
        """
        if image_id is None:
            return
        try:
            self._last_decided_id = int(image_id)
        except Exception:
            return
        try:
            cur = self._last_decided_id
            try:
                prev = int(self.db.meta_get(CHECKPOINT_KEY, "") or 0)
            except Exception:
                prev = 0
            # 只许前进：撤销会把 id 变小，这时**别**把断点拉回去，
            # 否则刚审完又跳回上一张（用户会以为没生效）。
            self.db.meta_set(CHECKPOINT_KEY, str(max(prev, cur)))
        except Exception:
            pass

    def _load_checkpoint(self):
        """读回断点 id（没有就 None）。"""
        try:
            v = self.db.meta_get(CHECKPOINT_KEY, "") or ""
            n = int(v) if str(v).strip() else 0
            return n if n > 0 else None
        except Exception:
            return None

    def _apply_checkpoint_cursor(self):
        """#56 A4 断点续审：把光标落到「断点之后的第一张未审图」。

        队列是 ``ORDER BY id DESC``（新的在前），所以断点之后 = id **小于**
        断点的那些里 id 最大的那张。找不到就保持现状（不乱跳）。
        """
        cp = self._last_decided_id
        if cp is None or not self.grid.count():
            return
        best_row, best_id = -1, None
        for i in range(self.grid.count()):
            v = self.grid.item(i).data(Qt.ItemDataRole.UserRole)
            if v is None:
                continue
            try:
                v = int(v)
            except Exception:
                continue
            if v < cp and (best_id is None or v > best_id):
                best_row, best_id = i, v
        if best_row >= 0:
            try:
                self.grid.setCurrentRow(best_row)
            except Exception:
                pass

    def _info_text(self, shown: int, total: int, missing: int = 0) -> str:
        """审核区标题行：**总数量**在前，本页/缺页提示在后。

        之前只印"待审核 N 张"而 N 是**当前这一页**的条数（默认每页 50），
        用户看不出隔离区里到底压了多少张 → 反馈"缺少总数量显示"。现在分成
        两个数字：总数（加粗显眼）+ 本页还有多少没显示。
        """
        extra = []
        if total > shown:
            extra.append(f"本页 {shown} 张，还有 {total - shown} 张未显示"
                         "（调大下面的『每页张数』或换任务看其余）")
        if missing:
            extra.append(f"{missing} 条磁盘缺失")
        head = f"待审核 <b>{total}</b> 张"
        tail = " · ".join(extra)
        return (f"{head}（{tail}）" if tail
                else f"{head}（{self._key_hint_line()}）")

    def _on_task_changed(self, *_):
        """#56 A4：换任务时清空流式余量。

        余量是按某个任务查出来的，留着会给新任务塞上一批**别的任务**的图
        （用户会看到"我说的是 A 任务，怎么冒出 B 任务的图"）。断点也重置 ——
        换任务本来就是一个新的审核上下文。
        """
        self._stream_spare = []
        self._last_decided_id = self._load_checkpoint()
        self._ckpt_applied = False
        self.reload()

    def reload(self, fill: bool = True):
        """重新拉待审核图铺当前页。

        ``fill=True``（默认，对应「自动补满」开关打开）：队列里还有货就一直把
        本页填满，判掉一张自动续一张，连着刷不用手动点刷新。
        ``fill=False``：只保留**这一页原本那批**还没判掉的，审到剩下几张就是
        几张 —— 想让「剩下的直接全选点不喜欢」时不会中途被新图插进来。
        """
        tid = self.task_combo.currentData() if hasattr(self, "task_combo") else None
        # 记住当前光标位置（id + 行 + 滚动），重建后尽量还原，避免「审核图库乱蹦」
        _cur = self.grid.currentItem()
        _prev_id = _cur.data(Qt.ItemDataRole.UserRole) if _cur else None
        _prev_row = self.grid.currentRow()
        _prev_scroll = self.grid.verticalScrollBar().value()
        # 【R117 P2-5】**先别 clear()**：把旧 item 抓在手里，网格分支里
        # 判断能不能「增量补位」（能就整表保留、只删判掉的 + 末尾补新的）。
        # 只有确定要全量重建时才真的 clear()，并顺手丢掉在飞的缩略图槽位
        # （r39：结果回来 setIcon 打到已销毁的 item 上会整进程崩）。
        _old_grid_items = self._snapshot_grid_items()
        _incr_ok = False
        _win = getattr(self, "_win", None)
        if fill:
            items, _from_spare = self._fetch_pending(tid, self._page_size)
            if not items:
                # 队列真的空了才清余量（余量也该一起没）
                self._stream_spare = []
        else:
            # 【#56 A4 修regression】关掉自动补满时**必须重新查库**，不能吃流式余量。
            # 流式余量是「只进不退」的前向游标：本页显示 6,5,4 后余量里只剩 3,2,1，
            # 下一次再切就会拿 3,2,1 去和本页那批{6,5,4} 求交集 → 空集 → 本页被清空。
            # 而 fill=False 的语义恰恰是「留在本页这批」，两者方向相反，只能重查。
            items = [r for r in (self.queue.pending(tid, limit=self._page_size)
                                 or []) if r is not None]
            # 只留本页原有的这批，别把队列后面的图续进来
            keep = getattr(self, "_page_ids", None) or set()
            if keep:
                items = [r for r in items if r is not None and r.id in keep]
        self._page_ids = {r.id for r in items if r is not None}
        # R154-F21：页面换了 → 预判缓存重算（纯本地展示层，零写入）。
        try:
            self._preanno_cache = self._preanno_compute_for_ids(self._page_ids)
        except Exception:
            self._preanno_cache = {}
        # 真正的总数量（隔离区里 needs_review=1 的全部，不受本页 limit 影响）：
        # 标题行要显示总数，页内条数另外说明，别再让人以为"这一页就是全部"。
        total_pending = self.queue.count_pending(tid)
        # ---- 列表模式：表格（与旧版隔离区同款）----
        if self._list_mode:
            # 切到列表模式：网格那批 item 必须清掉（反之亦然），
            # 否则两套视图里同一张图被重复选中。R117 P2-5：这里恢复
            # 原来的无条件 clear + thumb_slots_reset。
            if _old_grid_items:
                self.grid.clear()
                if _win is not None:
                    try:
                        _win.thumb_slots_reset()
                    except Exception:
                        pass
            self.grid.setVisible(False)
            self.t_split.setVisible(True)
            from .table_view import fill_table
            from ..repo import get_task
            from ..utils.url import domain_of
            rows = []
            for rec in items:
                if rec is None or not rec.path:
                    continue
                if not Path(rec.path).exists():
                    continue
                rows.append({
                    "id": rec.id, "path": rec.path, "task_id": rec.task_id,
                    "score": rec.score, "bytes": rec.bytes,
                    "width": rec.width, "height": rec.height,
                    "source_url": rec.source_url,
                    "favorite": bool(getattr(rec, "favorite", False)),
                    # r32：列表加「入库时间」列（排序键取它）
                    "collected_at": getattr(rec, "collected_at", None),
                })
            shown, missing = fill_table(
                self.table, rows, self.db,
                get_task_fn=lambda t_: get_task(self.db, t_) if t_ else None,
                domain_fn=domain_of,
                select_row=(self.table.currentRow()
                            if self.table.currentRow() >= 0 else None))
            self.info.setText(self._info_text(shown, total_pending, missing))
            b = getattr(self, "b_undo", None)
            if b is not None:
                can = self.queue.can_undo()
                b.setEnabled(can)
                b.setText(f"撤销({self.queue.undo_depth()})" if can else "撤销")
            self._show_prefs()
            return
        self.grid.setVisible(True)
        self.t_split.setVisible(False)
        # 【R117 P2-5】先试「增量补位」，不行才整表重建。
        _incr_ok = self._try_incremental_fill(items, _old_grid_items)
        if not _incr_ok:
            if _old_grid_items:
                self.grid.clear()
                # r39：清空之前把在飞的异步缩略图槽位丢掉，免得解码结果回来时
                # setIcon 打到一个已销毁的 item 上（整进程 0xC0000409）。
                if _win is not None:
                    try:
                        _win.thumb_slots_reset()
                    except Exception:
                        pass
            self._rebuild_grid_items(items)
        self.info.setText(self._info_text(len(items), total_pending, 0))
        # R154-F21：网格落定后把预判徽标刷上（增量/全量两条路统一走这里）
        self._apply_preanno_to_grid()
        # #12 撤销按钮状态跟随撤销栈
        b = getattr(self, "b_undo", None)
        if b is not None:
            can = self.queue.can_undo()
            b.setEnabled(can)
            b.setText(f"撤销({self.queue.undo_depth()})" if can else "撤销")
            b.setToolTip(self.queue.undo_label() or
                         "撤销上一步审核决定（Ctrl+Z），最近 50 步")
        if self.grid.count():
            _restored = False
            if _prev_id is not None:
                for _i in range(self.grid.count()):
                    if self.grid.item(_i).data(Qt.ItemDataRole.UserRole) == _prev_id:
                        self.grid.setCurrentItem(self.grid.item(_i))
                        _restored = True
                        break
            if not _restored:
                # 刚判定的那张已经从待审列表移除（id 找不到了），后面的图会往前
                # 顶一格——按原行号选中，就等于自动落到"下一张"，衔接自然。
                self.grid.setCurrentRow(max(0, min(_prev_row, self.grid.count() - 1)))
            # #56 A4 断点续审：**只在本次打开的第一次**铺页面时按断点落光标。
            # 连着刷的时候不能插手 —— 那时 _prev_row 已经自然落到"下一张"了，
            # 再按断点掰一次反而会把光标甩回去。
            if not getattr(self, "_ckpt_applied", False):
                self._ckpt_applied = True
                if _prev_id is None:
                    self._apply_checkpoint_cursor()
            if self.isVisible():
                self.grid.setFocus()
            # 滚动位置一律还原（以前只在按 id 还原时才还原，导致每次点完
            # 「不喜欢」滚动条被拨回 0、看起来就像跳回第一页从头看）。
            self.grid.verticalScrollBar().setValue(_prev_scroll)
        self._show_prefs()

    # ---------------------------------------------------------------- #
    # 【R117 P2-5】网格的「增量补位」—— 修「点喜欢后图片马上错位」
    # ---------------------------------------------------------------- #
    def _snapshot_grid_items(self) -> list:
        """抓一份当前网格的 (item, image_id) 快照（**不销毁任何 item**）。

        reload() 里要用它判断"能不能只删判掉的、末尾补新的"。返回空列表
        表示本来就空网格（首次进页 / 上一次是列表模式）⇒ 只能全量重建。
        """
        out = []
        try:
            n = self.grid.count()
        except Exception:
            return out
        for i in range(n):
            it = self.grid.item(i)
            if it is None:
                continue
            try:
                out.append((it, it.data(Qt.ItemDataRole.UserRole)))
            except Exception:
                pass
        return out

    def _make_grid_item(self, rec) -> object | None:
        """按记录建一个网格 item（含异步缩略图）；文件不在则返回 None。"""
        if rec is None or not rec.path:
            return None
        p = Path(rec.path)
        if not p.exists():
            return None
        item = QListWidgetItem()
        # r31/r39：缩略图**取图必须异步**（见 _thumb_icon_async），别在主线程
        # 同步解原图。r39 之前是 square_thumb_pixmap(p, 320)。
        self._thumb_icon_async(item, p)
        base, tip = card_base_and_tip(rec, p)
        item.setText(base)
        item.setToolTip(tip)
        item.setData(Qt.ItemDataRole.UserRole, rec.id)
        return item

    def _rebuild_grid_items(self, items) -> int:
        """全量重建网格（首次进页 / 切任务 / 顺序完全变了时的老路径）。"""
        n = 0
        for rec in items:
            it = self._make_grid_item(rec)
            if it is None:
                continue
            self.grid.addItem(it)
            n += 1
        return n

    def _try_incremental_fill(self, items, old_items) -> bool:
        """【R117 P2-5】能增量就增量，**返回 True 表示已处理完**。

        增量条件（三条都满足才做，否则老老实实全量重建）：
          ① 旧网格非空（否则没得"保留"）；
          ② 新旧图片 id 序列里，**保留下来的那张必须是同一张**（顺序没变）
             —— 只要顺序变了，用户就会看到图跳位，那比重建还糟；
          ③ 旧集合去掉被判定的那几张之后，**完全等于**新集合的前缀
             （只允许末尾追加，不允许中间插入/删除）。

        满足时：只 ``takeItem`` 掉不在新集合里的那些 item（判掉的），
        其余 item 连同**已解码好的 icon 原地不动**，末尾 append 新图。
        ⇒ 缩略图不重解码 ⇒ 不闪、不跳 ⇒ 位置稳定可预期。
        """
        if not old_items:
            return False
        # 先算出「期望保留/新增」，中途任何异常都直接退回全量重建
        try:
            want = []
            for rec in items:
                it = self._make_grid_item_check(rec)
                if it is not None:
                    want.append(it)
            new_ids = [i for i in want]
            old_ids = [i for (_it, i) in old_items]
            if not new_ids:
                # 新集合为空：全清（这才是「队列空了」的正确表现）
                for it, _i in old_items:
                    self.grid.takeItem(self.grid.row(it))
                return True
            keep = [i for i in new_ids if i in old_ids]
            # ② 旧序列里保留下来的必须与新序列**前缀**逐个相等。
            # ⚠ 只比「两边共有哪些 id」不够 —— 那会漏掉"中间插一张"这种
            #   破坏顺序的情况（new=[1,77,2,3,4,5]、old=[1,2,3,4,5]，
            #   两边共有的 id 集合完全一样，但 77 插在中间 ⇒ 用户看到图跳位）。
            #   正解是要求 keep **恰好就是 new_ids 的前缀**。
            if new_ids[:len(keep)] != keep:
                return False
            if [i for i in old_ids if i in set(keep)] != keep:
                return False
            # ③ 只允许末尾追加。
            # 【R119 修复】原来写的是 ``len(new_ids) <= len(keep) → False``，
            # 那是把「**没有新图可追加**」也当成不能增量 ⇒ 队列见底时（审到最后
            # 几十张，自动补满拉不到新图）每判一张都整页 clear()+重建 ⇒ 所有
            # 缩略图重解码 ⇒ 用户看到的正是 P2-5 报的那个「点喜欢后图片马上
            # 错位/跳动」。R117 的 8 张小样本碰巧每次都能补上新高图，测不到。
            # 现在只拦「比保留集合还短」这种真异常；等于（纯删除）走增量。
            if len(new_ids) < len(keep):
                return False
            # 删掉判掉的
            keep_set = set(keep)
            for it, i in old_items:
                if i not in keep_set:
                    self.grid.takeItem(self.grid.row(it))
            # 末尾追加新增的（顺带把刚 _make_grid_item_check 造出来的 item 用上）
            rec_by_id = {r.id: r for r in (items or []) if r is not None}
            for i in new_ids[len(keep):]:
                rec = rec_by_id.get(i)
                if rec is None:
                    return False
                it = self._make_grid_item(rec)
                if it is None:
                    return False
                self.grid.addItem(it)
            return True
        except Exception:
            # 中途失败 ⇒ 可能已经删了一半，回滚不可能（Qt 里已被 take 走）
            # —— 所以这里**保守返回 False** 让调用方走全量重建，
            # 而把「已删的」当作已删（下一步 clear 会兜住）。
            return False

    def _make_grid_item_check(self, rec):
        """只想知道这条记录**能不能**做成格子（不真的造 item）。"""
        if rec is None or not rec.path:
            return None
        try:
            return rec.id if Path(rec.path).exists() else None
        except Exception:
            return None

    def _thumb_icon_async(self, item, p):
        """r39：审核网格的缩略图**绝不能在主线程同步解原图**。

        以前这里写的是 ``pm = square_thumb_pixmap(p, 320)`` —— cache_dir 为空，
        等于每次 reload 都在 Qt 主线程里拿 Pillow 一张张开原图（avif/webp 单张
        30~80ms，大图更久），而 reload 又挂在「队列事件 → refresh()」上，采集
        一跑就是连着十几分钟的事件循环假死、界面点啥都没反应。r39 看门狗从
        别的线程扒主线程栈，抓到的就是这条链：

            PIL/WebPImagePlugin.load ← Image.convert ← table_view.square_thumb_pixmap
            ← review_tab.reload ← main_window.refresh ← _on_queue_event

        现在三级：① 内存/磁盘缓存命中 → 同步取（<1ms）；② 没缓存 → 立刻占位，
        丢给主窗口那条线程池（``thumb_request_async``），解码完由
        ``_on_thumb_ready`` 回主线程 setIcon；③ 拿不到线程池（单测/早期启动）
        → 有缓存才取，没缓存就先空着，别把界面拖死。
        """
        size = int(getattr(self, "_thumb_size", 320) or 320)
        win = getattr(self, "_win", None)
        if win is not None:
            try:
                thumbs = Path(win._thumb_dir())
                digest = win._thumb_digest("", p)
                mkey = win._thumb_mem_path(digest, size)
                pm = win._thumb_mem_get(mkey)
                if pm is None:
                    # 【R117 P2-7】判命中统一走 _thumb_cache_hit（先 _fit
                    # 再 _sq），不再自己拼 ``_sq`` 文件名 —— 那是 P2-7
                    # 之前的异步方块版命名，拼对了就等于主动去读方块缩略图。
                    if win._thumb_cache_hit(p, "", size, thumbs):
                        pm = win._thumb_pixmap(p, "", size, thumbs)
                if pm is not None:
                    try:
                        item.setIcon(QIcon(pm))
                    except Exception:
                        pass
                    return
                win.thumb_request_async(p, digest, size, thumbs, item, mkey)
                return
            except Exception:
                pass
        try:
            # 需求 2：审核网格不裁剪，完整显示（crop=False）
            pm = square_thumb_pixmap(p, size, cache_dir=self._thumb_dir(),
                                     crop=False)
            if pm is not None:
                item.setIcon(QIcon(pm))
        except Exception:
            pass

    def _thumb_dir(self):
        try:
            from ..config import user_data_dir
            return user_data_dir() / "thumbs"
        except Exception:
            return None

    def eventFilter(self, obj, ev):
        """审核页键盘流（#56 A4，F19 起键位可自定义）：

        默认 ↑↓ 选图 · → 喜欢 · ← 不喜欢 · E 说原因 · 右Ctrl 待定。
        改键走 hotkeys 注册表（设置 → 快捷键…）；判定规则只有一份
        （hotkeys.parse_sequence），这里只按顺序问「命中没有」。

        用事件过滤器而不是 keyPressEvent，因为焦点在缩略图列表上，
        按键会先被列表吃掉。

        #56 A4 补的 ``E`` 键 = 「这张图为什么不喜欢」原因框。以前这个框只有鼠标
        点得到（右键菜单 / dislike_dialog），键盘流连着刷几十张时中途伸手去点鼠标
        太打断，于是键盘判定的「不喜欢」默认根本不弹框（D28 的黑白名单学习因此
        落不了库）。E 键让它**全程不离键盘**就能勾主题词 / 填排除词。

        ⚠ 检查顺序 = hotkeys.ACTIONS 里同上下文的注册顺序（即优先级）：
        同键绑两个动作时排前面的生效——与设置表告警文案同一口径。
        """
        if obj is self.grid and ev.type() == QEvent.Type.KeyPress:
            from ..hotkeys import resolve as _hk
            from .hotkeys_qt import event_matches
            hk = getattr(self.cfg, "hotkeys", None) or {}
            # 单手键位（与隔离区一致）：↑↓ 上下选择，→ 喜欢，← 不喜欢
            if event_matches(ev, _hk(hk, "review.up")):
                self._move_selection(-1)
                return True
            if event_matches(ev, _hk(hk, "review.down")):
                self._move_selection(1)
                return True
            if event_matches(ev, _hk(hk, "review.like")):
                self.decide(ReviewDecision.LIKE, keyboard=True)
                return True
            if event_matches(ev, _hk(hk, "review.dislike")):
                self.decide(ReviewDecision.DISLIKE, keyboard=True)
                return True
            if event_matches(ev, _hk(hk, "review.reason")):
                # #56 A4：说原因 → 判「不喜欢」并强制弹框（D28 学习落库）
                self.decide(ReviewDecision.DISLIKE, keyboard=True,
                            ask_reason=True)
                return True
            if event_matches(ev, _hk(hk, "review.skip")):
                # 右 Ctrl = 待定。Windows 能区分左右；Linux/ARM 上左右都算。
                self.decide(ReviewDecision.SKIP)
                return True
        return super().eventFilter(obj, ev)

    def _key_hint_line(self) -> str:
        """审核页键位提示行（F19：按当前生效键位拼——改键后界面文案不撒谎）。"""
        from ..hotkeys import display as _d, resolve as _hk
        hk = getattr(self.cfg, "hotkeys", None) or {}
        return ("%s%s 选择 · %s 喜欢 · %s 不喜欢 · %s 说原因 · %s 待定" % (
            _d(_hk(hk, "review.up")), _d(_hk(hk, "review.down")),
            _d(_hk(hk, "review.like")), _d(_hk(hk, "review.dislike")),
            _d(_hk(hk, "review.reason")), _d(_hk(hk, "review.skip"))))

    def sync_hotkey_hints(self) -> None:
        """F19：把审核页所有印着键位的文案按当前生效键位重拼。

        设置 → 快捷键… 保存后由主窗口调用（hotkey_dialog.HotkeyDialog.apply_to
        之后）；不重拼就是「设置里写着 L、界面还印着 →」那类对不上缺陷。
        """
        from ..hotkeys import display as _d, resolve as _hk
        hk = getattr(self.cfg, "hotkeys", None) or {}
        line = self._key_hint_line()
        if getattr(self, "_multi_mode", False):
            self.grid.setToolTip("多选模式：点缩略图逐个勾选；再次右键关掉即恢复正常")
        else:
            self.grid.setToolTip(
                line + "\n"
                "【%s 不喜欢 = 删除这张图】走「删除时先进回收站」开关：\n"
                "  开了 → 进隔离区（回收站）可还原；关了 → 永久删除\n"
                "【%s = 为什么不喜欢】键盘也能填原因，勾的主题词只调低权重、\n"
                "  填的排除词才写进任务排除词（下次运行自动跳过）\n"
                "右键有「喜欢/不喜欢/跳过/全选/刷新」；"
                "「多选模式」下点图逐个勾选" % (
                    _d(_hk(hk, "review.dislike")),
                    _d(_hk(hk, "review.reason"))))
        if getattr(self, "b_rmode", None) is not None:
            self.b_rmode.setToolTip("缩略图网格 / 信息表格 两种视图切换；"
                                    "表格模式下同样支持 " + line)

    def _move_selection(self, delta: int):
        n = self.grid.count()
        if n == 0:
            return
        cur = self.grid.currentRow()
        self.grid.setCurrentRow(0 if cur < 0 else (cur + delta) % n)

    def select_all(self):
        self.grid.selectAll()

    def _on_page_size(self, v: int):
        self._page_size = int(v)
        self._page_timer.start(300)

    def _leave_multi_mode(self):
        """判完一个决定就退回到「单张流式审核」。

        r32 起因：这段代码以前只写在「不喜欢」分支的末尾，「喜欢 / 跳过」分支
        压根走不到 —— 用户反馈"多选模式点完喜欢出不来"（明明只是判了个喜欢）。
        """
        if not getattr(self, "_multi_mode", False):
            return
        self.toggle_multi_mode(False)
        try:
            self.grid.clearSelection()
        except Exception:
            pass

    def _auto_fill_on(self) -> bool:
        """现在到底补不补满 —— 永远看**复选框**，不看 cfg（防显示/行为错位）。"""
        cb = getattr(self, "cb_autofill", None)
        if cb is not None:
            return bool(cb.isChecked())
        return bool(getattr(self.cfg, "review_auto_fill", True))

    def _on_auto_fill(self, state: int):
        """审核页「自动补满」开关：判定后是否自动从队列续图把本页填满。"""
        try:
            self.cfg.review_auto_fill = bool(state)
        except Exception:
            pass
        try:
            from ..config import save_config
            save_config(self.cfg)
        except Exception:
            pass
        # 兜底：万一 cfg 是 None / save_config 失败，把框改回真实行为该有的样子，
        # 绝不允许"框是关的、行为却是开的"这种状态留下。
        try:
            cb = getattr(self, "cb_autofill", None)
            if cb is not None and bool(cb.isChecked()) != bool(
                    getattr(self.cfg, "review_auto_fill", True)):
                cb.setChecked(bool(getattr(self.cfg, "review_auto_fill", True)))
        except Exception:
            pass

    def toggle_multi_mode(self, on: bool | None = None):
        """多选模式：开启后点击逐个加入/移出选择（不用按 Ctrl）。

        需求 4：原来这个开关只对缩略图网格生效，切到列表模式就名存实亡 ——
        而表格本身已经是 ExtendedSelection（Ctrl/Shift 多选本来就可用），
        这里补上同样的显式开关，两处行为一致。
        """
        self._multi_mode = (not self._multi_mode) if on is None else bool(on)
        self.grid.setSelectionMode(
            QListWidget.SelectionMode.MultiSelection if self._multi_mode
            else QListWidget.SelectionMode.ExtendedSelection)
        from PySide6.QtWidgets import QAbstractItemView
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.MultiSelection if self._multi_mode
            else QAbstractItemView.SelectionMode.ExtendedSelection)
        if self._multi_mode:
            self.grid.setToolTip("多选模式：点缩略图逐个勾选；再次右键关掉即恢复正常")
        else:
            self.sync_hotkey_hints()

    def _grid_menu(self, pos):
        """缩略图右键菜单：把喜欢/不喜欢/跳过/全选/刷新搬到这里，底部只留整页动作。
        右键项跟随鼠标：点哪张就以哪张为目标，不再默认操作第一张 / 上次选中的。"""
        it = self.grid.itemAt(pos)
        if it is not None:
            if not it.isSelected():
                if self._multi_mode:
                    # 多选模式：右键项未选中则加入选择组（保留其他已选项）
                    it.setSelected(True)
                else:
                    # 非多选模式：以它为唯一目标（跟随鼠标，避免误伤第一项）
                    self.grid.clearSelection()
                    it.setSelected(True)
                self.grid.setCurrentItem(it)
            else:
                # 已选中（如多选模式里右键已选项）：保留整组多选。注意 ExtendedSelection
                # 下 setCurrentItem 会把多选「折叠成单张」，先记下再恢复。
                saved = self.grid.selectedItems()
                self.grid.setCurrentItem(it)
                for s in saved:
                    s.setSelected(True)
        menu = QMenu(self)
        menu.addAction("喜欢", lambda: self.decide(ReviewDecision.LIKE))
        # 需求 16：「不喜欢」改二级菜单，先选作用范围（仅当前任务 / 全局）。
        m_dis = menu.addMenu("不喜欢")
        m_dis.addAction("仅当前任务",
                        lambda: self.decide(ReviewDecision.DISLIKE,
                                            scope="task"))
        m_dis.addAction("全局黑名单（所有任务）",
                        lambda: self.decide(ReviewDecision.DISLIKE,
                                            scope="global"))
        menu.addAction("跳过", lambda: self.decide(ReviewDecision.SKIP))
        menu.addSeparator()
        menu.addAction("全选", self.select_all)
        a_multi = menu.addAction("多选模式（点击逐个选）")
        a_multi.setCheckable(True)
        a_multi.setChecked(self._multi_mode)
        a_multi.triggered.connect(lambda checked: self.toggle_multi_mode(checked))
        # 需求 17：手动刷新**无条件补满**，不理会「自动补满」开关。
        # 那个开关管的是「判定后要不要续图」；手动点刷新时用户明确是要看
        # 最新一批，不该因为开关关着就只给半页。
        menu.addAction("刷新（补满本页）", lambda: self.reload(fill=True))
        # 需求 12：右键「引用页」—— 默认浏览器打开这张图所在的源网页。
        menu.addAction("引用页", self.open_source_page)
        # 需求 20：网格菜单同样取消「全部通过/全部跳过」（两处必须一致，
        # 留一处等于没取消）。批量就走「全选 + 喜欢/不喜欢/跳过」。
        self._add_manual_tag_menu(menu)
        menu.exec(self.grid.mapToGlobal(pos))

    # ---- r29 主题词分级：人工补二级/三级细分标签 ----------------------
    def _add_manual_tag_menu(self, menu: QMenu):
        """右键菜单里挂「标注细分词（二级/三级）」——只对着选中图生效。

        这些图是「一级命中、但 AI 没把握二级三级」的那批，来审核区让人点一下
        标签就完事；点过的词会写进这张图的标签，并按「喜欢」的正向信号学一次，
        下次同类的图就能自动带细分标签入库。
        """
        ids = self._selected_ids()
        if not ids:
            return
        recs = [r for r in (get_image(self.db, i) for i in ids) if r is not None]
        l2, l3 = self._deep_label_groups(recs)
        if not l2 and not l3:
            return
        sub = menu.addMenu("标注细分词（只影响选中的这张/这批）")
        if l2:
            g2 = sub.addMenu("二级：" + "、".join(l2))
            for w in l2:
                # #60：菜单项上直接标出「这个中文词会被比成哪个英文词」。
                # CLIP 只认英文，用户填「金发」时系统内部拿 "blond hair" 去比，
                # 以前界面上一个字都没提，用户只能猜系统到底在拿什么比。
                a = g2.addAction(self._tag_action_text(w))
                a.triggered.connect(lambda _c=False, w=w: self._apply_manual_tag(ids, [w]))
        if l3:
            g3 = sub.addMenu("三级：" + "、".join(l3))
            for w in l3:
                a = g3.addAction(self._tag_action_text(w))
                a.triggered.connect(lambda _c=False, w=w: self._apply_manual_tag(ids, [w]))
        sub.addAction("撤销这些标签（清掉选中的细分词）",
                      lambda: self._apply_manual_tag(ids, [], clear=True))

    @staticmethod
    def _tag_action_text(word: str) -> str:
        """细分词菜单项的文案：有英文对应词就写成「金发 → blond hair」。

        相似度不在这行里塞——要 CLIP 现场算，菜单构建是热路径（每次右键都跑），
        不能为了菜单一行字去加载/推理模型。点下去之后的日志里才给带相似度的
        完整版（见 ``_apply_manual_tag``）。
        """
        try:
            from ..classify.labels_zh import translate_one
            cands = translate_one(word)
        except Exception:
            cands = []
        cands = [c for c in cands if c != word]
        return f"{word} → {cands[0]}" if cands else str(word)

    @staticmethod
    def _deep_label_groups(recs) -> tuple[list[str], list[str]]:
        """从选中图所属的任务里收集二三级细分词（去重、按任务合并）。"""
        l2: list[str] = []
        l3: list[str] = []
        for r in recs or []:
            try:
                from ..repo import get_task
                t = get_task(r.task_id)
            except Exception:
                t = None
            if t is None:
                continue
            for w in (t.ai_labels_l2 or []):
                if w not in l2:
                    l2.append(w)
            for w in (t.ai_labels_l3 or []):
                if w not in l3:
                    l3.append(w)
        return l2, l3

    def _apply_manual_tag(self, ids, words, clear: bool = False):
        """把人工选的细分词写进选中图的标签（并学一次「喜欢」）。

        clear=True 时做反向操作：只把 words 从这些图的标签里摘掉（words 为空
        就整条标签清空），同时把偏好权重也扣回去，免得越点越偏。
        """
        if clear and not words:
            return
        done = 0
        for i in ids:
            rec = get_image(self.db, i)
            if rec is None:
                continue
            old = list(rec.tags or [])
            if clear:
                new = [t for t in old if t not in words]
                for w in (words or old):
                    try:
                        PreferenceLearner(self.db, rec.task_id).learn(
                            "dislike", [w])
                    except Exception:
                        pass
            else:
                new = list(dict.fromkeys(old + list(words)))
            rec.tags = new
            try:
                upsert_image(self.db, rec)
                done += 1
            except Exception as e:
                self.log(f"写入细分标签失败: {type(e).__name__}: {e}")
        if done:
            who = ("清掉细分标签" if clear
                   else "标注 " + "、".join(words))
            self.log(f"已对 {done} 张图{who}（人工细分标签）")
            # #60：转移原文显示 —— 顺手把「这个中文词实际拿哪个英文词去比、
            # 相似度多少」记进日志。没本地 CLIP 模型时如实显示「相似度 —」，
            # 不编数字。
            if not clear:
                try:
                    from ..classify.labels_zh import translate_one
                    from ..classify.label_transfer import transfer_note
                    for w in (words or []):
                        cands = [c for c in translate_one(w) if c != w]
                        if cands:
                            self.log("  转移：" + transfer_note(w, cands[0]))
                except Exception as e:
                    self.log(f"  转移提示生成失败（不影响标注）: "
                             f"{type(e).__name__}")
                # #64：细分词是用户点的（多半是中文「金发」），而 CLIP 给图打的
                # 标签是英文「blond hair」。不桥接的话这条「喜欢」学不到词表标签上，
                # 闭环在这里也是断的。
                try:
                    tids = {r.task_id for r in
                            (get_image(self.db, i) for i in ids)
                            if r is not None}
                    for t in tids:
                        hits = PreferenceLearner(self.db, t).learn_manual(
                            "like", list(words or []))
                        for h in hits or []:
                            self.log("词表桥接: " + LabelBridge.hint(h))
                except Exception as e:
                    self.log(f"  词表桥接失败（不影响标注）: "
                             f"{type(e).__name__}")
            self.reload()

    def _table_menu(self, pos):
        """列表模式（表格）右键菜单：与缩略图网格同一套审核动作。

        `_selected_ids()` 在列表模式下会自动改读表格选中行，
        所以这里直接复用 decide()/全选/批量接口即可，无需另写取 id 逻辑。
        """
        menu = QMenu(self)
        menu.addAction("喜欢", lambda: self.decide(ReviewDecision.LIKE))
        # 需求 16：「不喜欢」改二级菜单，先选作用范围（仅当前任务 / 全局）。
        m_dis = menu.addMenu("不喜欢")
        m_dis.addAction("仅当前任务",
                        lambda: self.decide(ReviewDecision.DISLIKE,
                                            scope="task"))
        m_dis.addAction("全局黑名单（所有任务）",
                        lambda: self.decide(ReviewDecision.DISLIKE,
                                            scope="global"))
        menu.addAction("跳过", lambda: self.decide(ReviewDecision.SKIP))
        menu.addSeparator()
        menu.addAction("全选", self.select_all)
        a_multi = menu.addAction("多选模式（点击逐个选）")
        a_multi.setCheckable(True)
        a_multi.setChecked(self._multi_mode)
        a_multi.triggered.connect(lambda checked: self.toggle_multi_mode(checked))
        # 需求 17：同上，列表模式的刷新也强制补满
        menu.addAction("刷新（补满本页）", lambda: self.reload(fill=True))
        # 需求 12：列表模式右键同样加「引用页」。
        menu.addAction("引用页", self.open_source_page)
        # 需求 20：取消「全部通过（本页）」和「全部跳过（本页）」。
        # 一次误点就是把一整批图定生死，代价太大且不可逆 —— 用户不要。
        # 想批量过就按住 Ctrl/Shift 多选几行再选择「喜欢 / 不喜欢」，
        # 想清队列就选择「全选」+「跳过」，都是可控的。
        self._add_manual_tag_menu(menu)
        menu.exec(self.table.mapToGlobal(pos))

    def open_source_page(self):
        """需求 12：审核页右键「引用页」—— 取选中首图 source_url 浏览器打开。"""
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "引用页", "先选中一张图片。")
            return
        row = self.db.query(
            "SELECT source_url FROM images WHERE id=?", (ids[0],))
        url = row[0]["source_url"] if row else None
        if not url:
            QMessageBox.information(
                self, "引用页", "这张图没有可用的源网页地址。")
            return
        import webbrowser
        webbrowser.open(url)

    def _selected_id(self):
        item = self.grid.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_ids(self) -> list:
        """当前选中的全部图片 id（Ctrl/Shift 多选；列表模式读表格）。"""
        if getattr(self, "_list_mode", False):
            from .table_view import selected_ids as _sel
            return _sel(self.table)
        out = []
        for it in self.grid.selectedItems():
            iid = it.data(Qt.ItemDataRole.UserRole)
            if iid is not None:
                out.append(iid)
        if not out:
            one = self._selected_id()
            if one is not None:
                out = [one]
        return out

    def apply_grid_size(self, maximized: bool = False):
        """缩略图显示尺寸：基准取设置里的「审核预览尺寸」，最大化时跟着放大。

        走 table_view.apply_grid_layout：间距、单元格行高、禁拖拽都与图库网格
        同一套，改尺寸只是换缩略图边长，其它参数不动。
        """
        base = 160
        if getattr(self, "cfg", None) is not None:
            base = int(getattr(self.cfg, "review_thumb_size", 160) or 160)
        target = base
        if maximized:
            target = int(target * 1.8)
        target = max(96, min(target, 420))
        apply_grid_layout(self.grid, target)

    def set_review_thumb_size(self, v: int):
        """审核页预览（缩略图）尺寸：拖动即时生效并持久化（K②）。"""
        if getattr(self, "cfg", None) is not None:
            self.cfg.review_thumb_size = int(v)
            try:
                from ..config import save_config
                save_config(self.cfg)
            except Exception:
                pass
        w = self.window()
        self.apply_grid_size(bool(w and w.isMaximized()))

    def _task_ai_labels(self, task_id) -> list:
        """任务的一级 + 二级 + 三级主题词（去重、保序）。

        r33：以前只返回一级，选择「不喜欢」时弹的选词框里看不到二三级 —— 你想说
        "其实是二级那个词没对上"都选不了。这里三级一起给出去；注意勾了只做
        **负向学习**（权重调低），绝不写进排除词（白名单铁律）。
        """
        out: list[str] = []
        try:
            from ..repo import get_task
            t = get_task(self.db, task_id)
            if not t:
                return []
            for lv in (getattr(t, "ai_labels", None),
                       getattr(t, "ai_labels_l2", None),
                       getattr(t, "ai_labels_l3", None)):
                for w in (lv or []):
                    w = str(w).strip()
                    if w and w not in out:
                        out.append(w)
        except Exception:
            return []
        return out

    def _task_blacklist(self, task_id) -> list:
        try:
            from ..repo import get_task
            t = get_task(self.db, task_id)
            return list(t.blacklist or []) if t else []
        except Exception:
            return []

    def _task_ai_blacklist(self, task_id) -> list:
        """取任务的「AI 主题排除词」（即用户填的 AI 主题排除词），用于「不喜欢」
        弹窗的黑名单一栏——让用户看到已排除的词并可继续追加。"""
        try:
            from ..repo import get_task
            t = get_task(self.db, task_id)
            return list(t.ai_blacklist or []) if t else []
        except Exception:
            return []

    def _add_exclusions(self, task_id, labels: list[str],
                        scope: str = "task"):
        """同 main_window._add_exclusions：scope="global" 进全局黑名单。

        【硬性规则】主题词（ai_labels）永不进排除词 —— 见主窗口同名方法。
        """
        if not labels:
            return
        try:
            from ..repo import get_task, save_task
            t = get_task(self.db, task_id)
            if not t:
                return
            if scope == "global":
                from ..config import add_global_blacklist
                theme = set(t.ai_labels or [])
                safe = [w for w in labels if w not in theme]
                added = add_global_blacklist(self.cfg, safe)
                if added:
                    self.log(
                        f"已把 {', '.join(added)} 加入**全局黑名单**"
                        f"（所有任务都会跳过这类图）")
                return
            merged = list(t.ai_blacklist or [])
            theme = set(t.ai_labels or [])
            for lab in labels:
                if lab in theme:
                    self.log(f"「{lab}」是主题词，按规则不加入排除词")
                    continue
                if lab not in merged:
                    merged.append(lab)
            t.ai_blacklist = merged
            save_task(self.db, t)
            self.log(f"已把 {len(labels)} 个词条加入任务 #{task_id} 排除词: "
                     f"{', '.join(labels)}")
        except Exception as e:
            self.log(f"写入排除词失败: {type(e).__name__}: {e}")

    def decide(self, decision: ReviewDecision, keyboard: bool = False,
               ask_reason: bool | None = None, scope: str = "task"):
        """执行一个审核决定。

        ``keyboard=True`` 表示来自键盘快捷键（↑不喜欢 / ↓喜欢）。键盘连续判定时
        默认**不弹**「未命中主题词」选词框（太打断），想弹就在设置里打开
        「键盘审核时弹选词框」——鼠标点右键/按钮时不受影响，照常弹。

        #56 A4：新增 ``ask_reason`` 显式覆盖。``True`` = 无论 keyboard 与否都弹
        「为什么不喜欢」框，``False`` = 一定不弹。审核页 ↑↓ 走的就是 ask_reason=True
        —— 探探式连刷时手指不离键盘也能把「为什么不喜欢」写进黑白名单学习（D28）；
        ``None`` 保持上面那套老规则（键盘默认不弹、鼠标照弹）。
        """
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "提示", "先选中一张（可 Ctrl/Shift 多选）")
            return
        recs = [r for r in (get_image(self.db, i) for i in ids) if r is not None]
        # 不喜欢：把选中图所属任务的「未命中主题词 + 黑名单词」列出让用户选要学的
        if decision == ReviewDecision.DISLIKE:
            if recs:
                labels_union: set = set()
                black_union: set = set()
                for r in recs:
                    labels_union |= set(self._task_ai_labels(r.task_id))
                    black_union |= set(self._task_blacklist(r.task_id))
                    black_union |= set(self._task_ai_blacklist(r.task_id))
                # 键盘判定时默认不弹选词框（设置里可打开），鼠标操作照常弹。
                # #56 A4：ask_reason 显式 True 时无条件弹（审核页 ↑↓ 走这条）。
                if ask_reason is True:
                    _ask = True
                elif ask_reason is False:
                    _ask = False
                else:
                    _ask = (not keyboard) or bool(
                        getattr(self.cfg, "review_key_ask", False))
                if _ask and (labels_union or black_union):
                    from .dislike_dialog import (ask_dislike_reason,
                                                 consume_manual_words)
                    # 明说后果：确认后这张图会被删除，走不走回收站取决于那个开关
                    _rc = bool(getattr(self.cfg, "use_recycle", True))
                    hint = ("删除后会"
                            + ("移入回收站（可还原）" if _rc
                               else "从磁盘永久删除（不可恢复）")
                            + "；左栏主题词只做负向学习，不会加入排除词")
                    unmatched, exclusions, mode = ask_dislike_reason(
                        self, sorted(labels_union), sorted(black_union),
                        hint=hint, db=self.db)
                    if mode == "cancel":
                        return
                    # 【硬性规则】左栏「主题词」勾选 = 这张图没匹配上该主题，
                    # 只做负向偏好学习；只有右栏「排除词」才写进任务 ai_blacklist。
                    if exclusions:
                        # 把排除词写进每张图所属任务的排除词（按任务去重）
                        for t in {r.task_id for r in recs}:
                            self._add_exclusions(t, exclusions,
                                                 scope=scope)
                    if unmatched:
                        for t in {r.task_id for r in recs}:
                            PreferenceLearner(self.db, t).learn(
                                "dislike", unmatched)
                    # #64：手填的词走词表桥接 —— 「金发」要记到词表标签
                    # 「blond hair」上，否则学进去了也永远打不着地。
                    manual = consume_manual_words()
                    if manual:
                        for t in {r.task_id for r in recs}:
                            hits = PreferenceLearner(self.db, t).learn_manual(
                                "dislike", manual)
                        for h in hits or []:
                            self.log("词表桥接: " + LabelBridge.hint(h))
                    # R174 §4：用户声明的「正确标签」→ **正向**学习信号。
                    # ⚠ 走 learn_manual（user_typed + 词表桥接）：这是用户亲手
                    #   写的词，是"用户意图"的最高证据等级；方向是 like，
                    #   所以永远不会写进硬黑名单（本单的底线）。
                    correct = consume_correct_labels()
                    if correct:
                        for t in {r.task_id for r in recs}:
                            PreferenceLearner(self.db, t).learn_manual(
                                "like", correct)
                        self.log("误标纠正: 记为正向 " + " ".join(correct))
            # 【修复】自动学习：每次「不喜欢」都用这张图真实匹配到的 AI 主题词做
            # 负向学习，不再依赖弹框勾选——键盘单手操作也能累积权重
            # （之前「选了很多不喜欢仍没权重」的根因：主路径漏了这步）。
            # ⚠ R166：**这里不再显式学习**。原来这里学一遍、下面
            #   `_batch_decide_ids` 的 factory 又学一遍 ⇒ 同一次判断权重被计
            #   两次（"统计数字对不上"的一个真实来源）。现在统一交给
            #   factory（它是被撤销栈包住的位置，Ctrl+Z 能一并回滚），
            #   本处只保留弹窗勾选/手填词的**额外**学习（那些不是整图 tags）。
            self._batch_decide_ids(ids, decision)
            self._leave_multi_mode()
            return
        # 喜欢 / 跳过（单张或多选都走整批事务）；「喜欢」也做正向学习
        # ⚠ R166：同上，正向学习统一走 `_batch_decide_ids` 的 factory，
        #   此处不再显式学（否则与 factory 双计）。
        self._batch_decide_ids(ids, decision)
        # r32：以前只有「不喜欢」会退出多选模式，喜欢/跳过不会 —— 一起走这里
        self._leave_multi_mode()

    def _batch_decide(self, decision: ReviewDecision):
        """批量处理当前页待审核：全部「喜欢」或「跳过」。两者都保留图片（非破坏性）。"""
        ids = [self.grid.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self.grid.count())]
        ids = [i for i in ids if i is not None]
        if not ids:
            QMessageBox.information(self, "提示", "当前页没有待审核的图。")
            return
        self._batch_decide_ids(ids, decision)

    def _batch_decide_ids(self, ids: list, decision: ReviewDecision):
        """对给定的一组图片 id 批量做同一决定（多选/全选共用）。

        #12 整批作为一个撤销事务：Ctrl+Z 一次回滚全部，不会只撤一半。
        R154-F21：返回 ``apply_batch`` 的实际判定张数（调用方此前不接收
        返回值，追加返回值向后兼容；「采纳预标」用它如实记账）。
        """
        recs = [get_image(self.db, i) for i in ids]
        recs = [r for r in recs if r is not None]
        if not recs:
            return
        fbs = []          # V0.0.8：这批判退学到的「别找」词，判完如实播报

        def factory(rec):
            def learn(kind, tags, _rec=rec):
                # R166：审核页的判断统一走 learn_action（记权重 + 来源留痕）。
                # ⚠ 这里是审核路径**唯一**的学习点 —— `decide()` 那边已删掉
                #   显式学习，别再两处都学（双计会让看板数字与真实判断次数对不上）。
                # ⚠ source 固定 "review"：`apply_batch` 也可能被别的入口调，
                #   但走到这个 factory 的必然是审核页。
                if tags:
                    lr = PreferenceLearner(self.db, 0)
                    lr.learn_action(
                        kind, tags, source="review",
                        image_id=getattr(_rec, "id", None),
                        sample_task_id=_rec.task_id)
                    fb = getattr(lr, "last_feedback", None)
                    if fb and fb.get("ok"):
                        fbs.append(fb)
            return learn
        n = self.queue.apply_batch([r.id for r in recs], decision,
                                   on_learn_factory=factory)
        self.log(f"审核批量: {decision.value} ×{n}（Ctrl+Z 可整批撤销）")
        if fbs:
            total = sum(int(f.get("added") or 0) for f in fbs)
            hardened = sorted({w for f in fbs for w in (f.get("hardened") or [])})
            msg = f"已学习 {total} 个排除词"
            if hardened:
                msg += f"，其中 {'、'.join(hardened[:6])} 已在下载前拦截"
            else:
                msg += "（同类再次判退将升级为下载前拦截）"
            self.log(msg)
        # #56 A4 断点续审：这批里最大的 id 就是「审到哪了」。判定过的图
        # needs_review 已被置 0，reload 后光标会自动落到它之后的第一张未审图。
        try:
            self._save_checkpoint(max(int(r.id) for r in recs))
        except Exception:
            pass
        # r33：判定后补不补满，**以复选框当前状态为准**（界面说什么就是什么），
        # 不再去问 cfg —— 以前读 cfg，出现过"框没勾但队列照旧续图"的错位：
        # cfg 里存的是旧值，框却已经被别处改过。cfg 只负责下次启动时的初值。
        self.reload(fill=self._auto_fill_on())
        return n

    # ------------------------------------------------ R154-F21 · AI 预标注
    def _preanno_weights(self) -> dict:
        """预判用的偏好权重：**全局**（task_id=0）—— 学习权重自 R166 起统一
        全局（``preferences.task_id`` 恒 0），预判必须查同一份，别按任务分家。
        """
        from ..repo import get_preferences
        return dict(get_preferences(self.db, 0) or {})

    def _preanno_compute_for_ids(self, ids) -> dict:
        """给定图片 id → {id: PreAnnotation}。逐张 get_image 取**已存的**
        AI 标签（采集时 CLIP 打的），不重新跑模型（主线程跑 CLIP 会卡界面，
        且 CPU-only 无模型机器是常态）。任何一张失败都不影响其余。"""
        try:
            weights = self._preanno_weights()
        except Exception:
            weights = {}
        out = {}
        for i in (ids or set()):
            try:
                rec = get_image(self.db, i)
            except Exception:
                rec = None
            if rec is None:
                continue
            out[int(i)] = preannotate.predict(
                int(i), weights, getattr(rec, "tags", None))
        return out

    def _on_preanno_toggled(self, _on: bool):
        """开关翻转：重算当前页缓存并重刷网格文案（展示层操作，不重查队列）。"""
        try:
            self._preanno_cache = self._preanno_compute_for_ids(
                getattr(self, "_page_ids", None) or set())
        except Exception:
            self._preanno_cache = {}
        self._apply_preanno_to_grid()

    def _apply_preanno_to_grid(self):
        """把预判徽标刷上网格条目：开关开 → 基础行 + 「预判 …」；关 → 恢复基础。

        文案与 tooltip 都**按当前状态整体重写**（不做差量拼接）——增量补位
        复用的旧条目、开关来回切，都收敛到同一个渲染结果，不残留旧徽标。
        列表模式（表格视图）不显示徽标：预判挂在网格条目上，表格列结构
        不动（如实记账，见 R216 台账）。
        """
        grid = getattr(self, "grid", None)
        if grid is None:
            return
        on = bool(getattr(self, "cb_preanno", None) and self.cb_preanno.isChecked())
        cache = getattr(self, "_preanno_cache", None) or {}
        for i in range(grid.count()):
            it = grid.item(i)
            if it is None:
                continue
            try:
                iid = int(it.data(Qt.ItemDataRole.UserRole))
            except (TypeError, ValueError):
                continue
            rec = None
            try:
                rec = get_image(self.db, iid)
            except Exception:
                rec = None
            if rec is not None:
                base, tip = card_base_and_tip(
                    rec, Path(rec.path) if rec.path else Path(""))
            else:                       # 图已不在库（理论少见）：只恢复基础行
                base = f"#{iid}"
                tip = it.toolTip()
            if on:
                pre = cache.get(iid)
                it.setText(f"{base}\n{preannotate.badge(pre)}")
                d = preannotate.detail(pre)
                it.setToolTip(f"{tip}\n{d}" if d else tip)
            else:
                it.setText(base)
                it.setToolTip(tip)

    def adopt_preanno(self):
        """R154-F21「全部采纳本页预标」：预判在这里落成**真判定**。

        喜欢倾向整批 LIKE、不喜欢倾向整批 DISLIKE，各自走
        ``_batch_decide_ids`` —— 那是审核页判定的**唯一咽喉**（学习工厂
        ``learn_action``、断点续审、自动补满、撤销事务全在那条路上），
        本方法**零额外写入**：不碰权重、不碰 reviews，R166 的「采纳后才写」
        由复用天然满足。两批 = 两个撤销事务，Ctrl+Z 各撤一批。
        只采纳**当前网格上还在**的图（缓存可能带着已翻走的旧页）。
        """
        if not bool(getattr(self, "cb_preanno", None)
                    and self.cb_preanno.isChecked()):
            QMessageBox.information(self, "采纳预标",
                                    "先打开顶部的「AI 预标」开关再采纳。")
            return
        page_ids = set()
        for i in range(self.grid.count()):
            it = self.grid.item(i)
            if it is None:
                continue
            v = it.data(Qt.ItemDataRole.UserRole)
            if v is None:
                continue
            try:
                page_ids.add(int(v))
            except (TypeError, ValueError):
                continue
        cache = getattr(self, "_preanno_cache", None) or {}
        like_ids = sorted(i for i, p in cache.items()
                          if p.tendency == preannotate.LIKE and i in page_ids)
        dislike_ids = sorted(i for i, p in cache.items()
                             if p.tendency == preannotate.DISLIKE
                             and i in page_ids)
        if not like_ids and not dislike_ids:
            QMessageBox.information(
                self, "采纳预标",
                "当前页没有可采纳的预判（图上没有 AI 标签，或偏好模型"
                "尚未学习到权重）。\n请先审核若干张，使模型完成偏好学习后再使用预标。")
            return
        n_like = n_dislike = 0
        if like_ids:
            n_like = self._batch_decide_ids(like_ids, ReviewDecision.LIKE) or 0
        if dislike_ids:
            n_dislike = (self._batch_decide_ids(dislike_ids,
                                                ReviewDecision.DISLIKE) or 0)
        self.log(f"采纳预标: 喜欢 ×{n_like}、不喜欢 ×{n_dislike}"
                 "（各成一个撤销事务，Ctrl+Z 可分别整批撤销）")


    def sample_review(self, n: int = 12):
        """误杀抽检：随机把 N 张 AI 自动隔离的图拉进审核队列。"""
        ids = self.queue.sample_quarantine(n)
        if not ids:
            QMessageBox.information(
                self, "抽检", "隔离区里没有可抽检的图了（可能都审过了，或本来就没隔离）。")
            return
        self.log(f"审核抽检: 随机拉入 {len(ids)} 张隔离图待人工复核")
        QMessageBox.information(
            self, "已抽检",
            f"已随机抽 {len(ids)} 张 AI 自动隔离的图进审核队列，\n"
            "过一遍看看有没有被误杀的好图（← → 切换、↑ 不喜欢、↓ 喜欢）。")
        self.reload()

    def smart_review(self, n: int = 20):
        """主动学习：把模型最置信度不足的 N 张（AI 分数离阈值最近）拉进审核。"""
        tid = self.task_combo.currentData()
        ids = self.queue.smart_sample(n, tid if tid is not None else None)
        if not ids:
            QMessageBox.information(
                self, "智能抽样",
                "没有可抽的图了——隔离区里带 AI 分数的图都审过了，\n"
                "或本次跑出来的图分数都离阈值很远（模型很有把握）。")
            return
        self.log(f"审核智能抽样: 拉入 {len(ids)} 张「最置信度不足」的图待人工定边界")
        QMessageBox.information(
            self, "已智能抽样",
            f"已筛选 {len(ids)} 张判定最不确定的图片进入审核队列（← → 切换、"
            "↑ 不喜欢、↓ 喜欢）。\n标几张边界样本，偏好模型就能学得更准。")
        self.reload()

    def open_file(self, *_):
        """双击：内置灯箱预览大图（← → 翻页，Esc 关闭）。"""
        iid = self._selected_id()
        if iid is None:
            return
        tid = self.task_combo.currentData() if hasattr(self, "task_combo") else None
        rows = []
        idx = 0
        for rec in (self.queue.pending(tid) or []):
            if not rec or not rec.path:
                continue
            if not Path(rec.path).exists():
                continue
            rows.append({
                "id": rec.id, "path": rec.path,
                "width": getattr(rec, "width", None),
                "height": getattr(rec, "height", None),
                "source_url": getattr(rec, "source_url", None),
                "tags": getattr(rec, "tags", None),
                "score": getattr(rec, "score", 0.0),
            })
            if rec.id == iid:
                idx = len(rows) - 1
        if not rows:
            QMessageBox.information(self, "提示", "没有可预览的图片")
            return

        _decided = {"v": False}

        def on_decide(decision, row):
            # 灯箱里按 ↑/↓ 直接「喜欢 / 不喜欢」：应用决定 + 学偏好，灯箱自行翻下一张
            iid = row.get("id")
            rec = get_image(self.db, iid)
            if rec is None:
                return
            if decision == ReviewDecision.DISLIKE:
                # 与审核页「不喜欢」保持一致：弹出选词框让用户挑要排除的词，
                # 绝不再把整组主题词（白名单）偷偷塞进排除词（那些词本就是想留的）。
                # 弹窗内部已做「白名单词不进黑名单」的去重，取消则本次不判不改。
                labs = self._task_ai_labels(rec.task_id)
                bl = self._task_ai_blacklist(rec.task_id)
                _rc2 = bool(getattr(self.cfg, "use_recycle", True))
                unmatched, exclusions, mode = ask_dislike_reason(
                    self, labs, bl,
                    hint=("删除后会" + ("移入回收站（可还原）" if _rc2
                                   else "从磁盘永久删除（不可恢复）")
                          + "；左栏主题词只做负向学习，不会加入排除词"),
                    db=self.db)
                if mode == "cancel":
                    return
                # 【硬性规则】左栏主题词只负向学习，绝不写进排除词
                if exclusions:
                    # R152-A3：原来是 scope=scope —— 闭包里没有这个变量，
                    # NameError 被灯箱 _decide_and_advance 的 except: pass
                    # 吞掉 ⇒ 不学习/不入排除词/不删除却照样翻页。与 decide()
                    # 的默认口径一致：灯箱路径写任务级排除词。
                    self._add_exclusions(rec.task_id, exclusions,
                                         scope="task")
                if unmatched:
                    PreferenceLearner(self.db, rec.task_id).learn(
                        "dislike", unmatched)
                # #64：手填词桥接到词表标签再学（灯箱这条路径同样会闭环）
                manual = consume_manual_words()
                if manual:
                    hits = PreferenceLearner(
                        self.db, rec.task_id).learn_manual("dislike", manual)
                    for h in hits or []:
                        self.log("词表桥接: " + LabelBridge.hint(h))
                # R174 §4：「正确标签」→ 正向学习（灯箱路径也要闭环，
                # 否则用户在灯箱里纠正了误标、系统却什么都没学到）
                correct = consume_correct_labels()
                if correct:
                    PreferenceLearner(self.db, rec.task_id).learn_manual(
                        "like", correct)
                    self.log("误标纠正: 记为正向 " + " ".join(correct))
            # V0.0.8：灯箱判定并入统一学习口。以前这里直接调低层 learn，
            # 绕开了「判退 → 别找词记账」和来源留痕（R166 说审核页每个
            # 判断动作都该走 learn_action，灯箱是漏网的最后一个口）。
            # ⚠ 学习必须作为 on_learn 回调交给 queue.apply —— 它在
            #   快照**之后**执行，Ctrl+Z 才能连权重和「别找」词一起回滚；
            #   在 apply 之前学，撤销栈拍到的已经是学完的账（原有缺口）。
            _fbs = []

            def _lb_learn(kind, tags, _rec=rec):
                if not tags:
                    return
                lr = PreferenceLearner(self.db, _rec.task_id)
                lr.learn_action(kind, tags, source="review", image_id=iid)
                fb = getattr(lr, "last_feedback", None)
                if fb and fb.get("ok"):
                    _fbs.append(fb)

            self.queue.apply(iid, decision, on_learn=_lb_learn)
            self.log(f"审核(灯箱) #{iid}: {decision.value}")
            if _fbs:
                total = sum(int(f.get("added") or 0) for f in _fbs)
                hardened = sorted({w for f in _fbs
                                   for w in (f.get("hardened") or [])})
                msg = f"已学习 {total} 个排除词"
                msg += (f"，其中 {'、'.join(hardened[:6])} 已在下载前拦截"
                        if hardened else "（同类再次判退将升级为下载前拦截）")
                self.log(msg)
            _decided["v"] = True

        # F19：灯箱键位与审核页同源（都读 cfg.hotkeys）
        Lightbox(rows, idx, None, on_decide=on_decide,
                 hotkeys=getattr(self.cfg, "hotkeys", None)).exec()
        # 只有灯箱里真的做了审核决定才刷新（否则只是看大图，保持光标位置不跳回第一张）
        if _decided["v"]:
            self.reload()

    def _show_prefs(self):
        rows = self.db.query(
            "SELECT label, weight FROM preferences "
            "ORDER BY ABS(weight) DESC LIMIT 8")
        if not rows:
            self.prefs.setText("偏好模型：尚无数据（审核后会自动学习）")
            return
        parts = [f"{r['label']}:{r['weight']:+.1f}" for r in rows]
        self.prefs.setText("偏好模型（本地全局权重）:  " + "   ".join(parts))

    def show_prefs_table(self):
        """#13 权重可视化：把学到的偏好模型摊开成表格，可删掉学错的某条权重。

        学习记录是全局的（task_id=0）——清空任务后新建的任务也会继承，
        所以这里不再按任务分列。每条权重（标签/权重/命中次数）都可见，
        并允许逐条删除——比如误点了一次「不喜欢」导致某个词被学歪，
        删掉那条即可，不必清空整个模型。
        """
        from PySide6.QtWidgets import (QDialog, QVBoxLayout, QTableWidget,
                                       QTableWidgetItem, QHeaderView,
                                       QAbstractItemView)
        rows = self.db.query(
            "SELECT label, weight, seen, hit_pos, hit_neg FROM preferences "
            "ORDER BY ABS(weight) DESC")
        dlg = QDialog(self)
        dlg.setWindowTitle("学到的偏好模型（本地权重）")
        dlg.resize(560, 420)
        v = QVBoxLayout(dlg)
        if not rows:
            from PySide6.QtWidgets import QLabel as _L
            v.addWidget(_L("尚未学习到任何权重。\n请在审核页单击「喜欢 / 不喜欢」"
                           "或图库标记后，这里会自动积累。"))
            from PySide6.QtWidgets import QDialogButtonBox
            bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            bb.rejected.connect(dlg.close)
            zh_buttons(bb)          # C5：别显示英文 "Close"
            v.addWidget(bb)
            dlg.exec()
            return
        # #37/D3：从「标签 / 权重 / 命中次数」三列扩成四列 ——
        # 权重不再把「来源」折进同一格里，命中次数按方向拆成「主题词命中 /
        # 排除词命中」，最后一列显式写「来源」。以前那版一格里塞
        # `+1.5 · 喜欢(正)`，用户看不出这权重是哪来的、被谁触发的。
        tbl = QTableWidget(len(rows), 4)
        tbl.setHorizontalHeaderLabels(
            ["标签", "主题词命中", "排除词命中", "权重 / 来源"])
        tbl.verticalHeader().setVisible(False)
        tbl.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tbl.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tbl.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        for i, r in enumerate(rows):
            w = float(r["weight"] or 0)
            hp = int(r["hit_pos"] or 0)
            hn = int(r["hit_neg"] or 0)
            src = ("主题词（喜欢）" if w > 0 else
                   ("排除词（不喜欢/拉黑）" if w < 0 else "中性"))
            # r40：界面显示中文（label_text），存库/学习/比对仍用原始英文 key
            tbl.setItem(i, 0, QTableWidgetItem(label_text(str(r["label"]))))
            tbl.setItem(i, 1, QTableWidgetItem(str(hp)))
            tbl.setItem(i, 2, QTableWidgetItem(str(hn)))
            tbl.setItem(i, 3, QTableWidgetItem(f"{w:+.1f} · {src}"))
        v.addWidget(tbl, 1)

        def delete_selected():
            sel = tbl.selectionModel().selectedRows()
            if not sel:
                QMessageBox.information(dlg, "提示", "先在表格里选中一行")
                return
            from ..repo import delete_preference
            n = 0
            for m in sel:
                if delete_preference(self.db, rows[m.row()]["label"]):
                    n += 1
            self.log(f"偏好模型：手工删除 {n} 条权重")
            QMessageBox.information(dlg, "已删除",
                                    f"已删除 {n} 条权重。\n"
                                    "下次运行/审核时会按剩余权重判断。")
            dlg.accept()

        def edit_selected():
            """#60：把选中的那条权重直接改成想要的数值（原来是累加语义）。

            为什么不复用 set_preference：它是**累加**（weight = weight + 新值），
            面板要的是「改成多少就是多少」——用户学歪了想调小一点，不是再加一次。
            命中次数 seen/hit_pos/hit_neg 原样保留：那是被喜欢/不喜欢点过几次的
            历史事实，改个权重不该把这段历史抹掉。
            """
            from PySide6.QtWidgets import QInputDialog
            sel = tbl.selectionModel().selectedRows()
            if not sel:
                QMessageBox.information(dlg, "提示", "先在表格里选中一行")
                return
            r = rows[sel[0].row()]
            old = float(r["weight"] or 0)
            new, ok = QInputDialog.getDouble(
                dlg, "改权重", f"标签「{label_text(str(r['label']))}」的新权重\n"
                f"（正数＝喜欢方向，负数＝排除词方向；当前 {old:+.1f}）",
                old, -99.0, 99.0, 2)
            if not ok:
                return
            from ..repo import set_preference_weight
            if not set_preference_weight(self.db, r["label"], new):
                QMessageBox.warning(dlg, "没改成",
                                    "数据库中已无该条权重（可能刚被清除）。")
                dlg.accept()
                return
            self.log(f"偏好模型: 权重 {label_text(str(r['label']))} "
                     f"{old:+.1f} -> {new:+.1f}（手工修改）")
            self.reload()
            dlg.accept()

        from PySide6.QtWidgets import QDialogButtonBox
        bb = QDialogButtonBox()
        b_edit = bb.addButton("改选中权重…",
                              QDialogButtonBox.ButtonRole.ActionRole)
        b_del = bb.addButton("删除选中权重",
                             QDialogButtonBox.ButtonRole.ActionRole)
        b_close = bb.addButton(QDialogButtonBox.StandardButton.Close)
        b_edit.clicked.connect(edit_selected)
        b_del.clicked.connect(delete_selected)
        b_close.clicked.connect(dlg.close)
        zh_buttons(bb)          # C5：addButton 加的 Close 也要改名
        v.addWidget(bb)
        dlg.exec()
        self._show_prefs()

    def apply_learned_exclusions(self):
        """把学到的**用户手填**排除词写进任务的 AI 排除词，使下次运行
        自动跳过这些图（真正的本地学习闭环）。

        ★ R174（本单核心）：候选词**只取 `source='user_typed'`**。

        以前这里把 ``weight<0`` 的标签**一律**写进 ``ai_blacklist``，而负权
        绝大多数来自「用户点了不喜欢 / 删了图」时被记下的**模型自动标签**。
        模型是会认错的：用户想要「红丝」这类图，机器认不准，用户删了几张，
        系统反而把「红丝」写进硬黑名单（engine 命中即删）—— 变成
        「软件屏蔽了用户真正想要的东西」。用户原话：「这难道不是一个 bug 吗？」

        现在的口径：**模型标签只是假设，只进软学习（权重），绝不进硬黑名单**；
        只有用户亲手输入过的词才配硬排除。剩下的模型自动负权词可以在
        「学习看板 → 仅清模型自动词」里一键清掉。
        """
        from ..repo import get_task, save_task, negative_user_typed_labels

        tid = self.task_combo.currentData()
        if tid is None:
            ids = [r["id"] for r in self.db.query(
                "SELECT id FROM tasks WHERE is_template=0 ORDER BY id")]
        else:
            ids = [tid]
        if not ids:
            QMessageBox.information(self, "应用排除词", "没有可用任务。")
            return

        neg = negative_user_typed_labels(self.db)
        # 模型自动词有多少（用于「一条都没写」时把原因说清楚 —— 不说就是
        # 「点了没反应」，用户只会以为功能坏了）
        try:
            auto_neg = len(self.db.query(
                "SELECT label FROM preferences WHERE task_id=0 AND weight<0 "
                "AND source='model_auto'"))
        except Exception:
            auto_neg = 0

        added_total = 0
        detail = []
        for task_id in ids:
            t = get_task(self.db, task_id)
            if t is None:
                continue
            if not neg:
                continue
            existing = set(t.ai_blacklist or [])
            new = [lbl for lbl in neg if lbl not in existing]
            if not new:
                continue
            t.ai_blacklist = (t.ai_blacklist or []) + new
            save_task(self.db, t)
            added_total += len(new)
            detail.append(f"#{task_id}: +{len(new)}")

        if added_total == 0:
            if auto_neg and not neg:
                QMessageBox.information(
                    self, "应用排除词",
                    f"库里有 {auto_neg} 个负权词，但它们**全是模型自动标签**，"
                    "本次一条都没写。\n\n"
                    "模型认错图时会把不相关的标签记成负权（比如把「红丝」认成别的东西），"
                    "写入硬黑名单会同时屏蔽目标内容 —— 因此仅将**手工填写过**"
                    "的排除词写入黑名单。\n"
                    "想清掉这批模型自动词：AI 与过滤 → 学习看板 → 「仅清模型自动词」。")
            else:
                QMessageBox.information(
                    self, "应用排除词",
                    "没有新的负向标签可加入排除词（可能已全部在黑名单里，"
                    "或尚未在不喜欢弹窗中手工填写排除词）。")
            return
        self.log(f"审核页: 应用学到的排除词，新增 {added_total} 个 → {', '.join(detail)}")
        QMessageBox.information(
            self, "完成",
            f"已将**手工填写过**的排除词加入任务 AI 排除词，共 {added_total} 个。\n"
            f"下次运行这些任务时自动生效。\n"
            "（模型自动标签不写入硬黑名单，避免误标屏蔽目标内容）\n"
            + "\n".join(detail))
        self._show_prefs()
