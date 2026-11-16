"""#63 / A6 ·「学习看板」对话框（AI 与过滤 → 学习看板…）。

看板回答的是用户最常问的一句话：**「你到底从我这儿学了什么？」**
以前只有一张「权重明细」表格（能看能删，但看不出全貌），这里补一个概览面板：
五个概览数字 + 喜欢/排除双向Top-N 条形 + 最近 14 天审核趋势 + 判定分布。

设计上的几条刻意选择：
- **不新增顶级菜单**。verify_r11 把顶级菜单钉死成 8 个，加第9 个直接破基线；
  「学习看板」挂在「AI与过滤」下——它本来就是 AI 学习的东西。
- **数据全部走 :mod:`..classify.learning_stats` 的纯函数**，本文件只负责画。
  这样统计口径能离线单测，不必起 QApplication。
- **只读为主**。看板上不提供**改单条**权重/删单条权重，那些仍在「权重明细」
  表格里做（一处改动入口，避免两个地方写同一张表）。
  ⚠ 仅有两处**批量**例外（都不是"改某一条权重"，且都收在 repo 层：
    写路径不写在这里，避免两个界面各写一份 SQL）：
    · R174「仅清模型自动词」—— 把机器认错攒下来的那批负权词一次性作废，
      只有看板能一眼说清这批词有多少、从哪来，放到权重明细里反而找不到；
    · R169「重置学习统计」—— 全清（订单第 4 条要求两端都有一键重置入口）。
      两者代价差一个量级，UI 上刻意分成两个按钮、文案互相指路。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QWidget,
    QSizePolicy, QDialogButtonBox, QPushButton, QScrollArea, QFrame,
)

from ..classify import learning_stats as LS
from .zh_labels import zh_buttons
from .theme import style as _tstyle   # P1-4：行内样式也要跟随皮肤

# P1-4：这些色值原本是**模块级常量**（导入那一刻就定死），所以换皮肤 /
# 切深浅时看板永远是紫配浅灰 —— 恰恰是「皮肤半成品」最扎眼的一处。
# 现在改成 paintEvent 里现取：每次重绘都问调色板要一遍当前值。
# 深浅用 theme._CURRENT_DARK（apply_theme 维护），不靠构造时传参 ——
# 看板是切换后才重绘的，那时它手上没有"当初是浅色"这个信息。
_C_POS   = "#5b3fd6"   # 仅作 _pc() 调色板缺键时的兜底
_C_NEG   = "#c2410c"
_C_TRACK = "#e7e9f0"
_C_TEXT  = "#3a4150"
_C_MUTED = "#6b7280"


def _pal():
    """看板所有取色的唯一入口：跟着当前皮肤 + 当前深浅走。"""
    from . import theme
    return theme.palette(theme._CURRENT_DARK)


def _pc(key: str, fallback: str) -> str:
    try:
        return _pal().get(key, fallback)
    except Exception:
        return fallback


class _BarChart(QWidget):
    """横向条形图（自己QPainter 画，不引任何图表库）。

    为什么要自己画：项目零第三方 GUI 依赖是硬约束（打包体积 + 依赖树），
    引入 QChart/matplotlib 会把 exe 撑大几百 MB。
    """

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self._title = title
        self._items: list[dict] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Minimum)
        self.setMinimumHeight(40)

    def set_items(self, items):
        self._items = list(items or [])
        self.updateGeometry()
        self.update()

    def sizeHint(self):
        # 每条固定 22px + 上下留白；空数据也要留个最小高度（否则布局塌成一条线）
        return QSize(self.width() or 320, max(48, len(self._items) * 22 + 16))

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        top = 4
        if self._title:
            f = QFont(self.font())
            f.setBold(True)
            p.setFont(f)
            p.setPen(QPen(QColor(_pc('text_dim', _C_MUTED))))
            p.drawText(QRectF(0, 0, w, 16), Qt.AlignLeft | Qt.AlignVCenter,
                       self._title)
            top = 20
        if not self._items:
            p.setPen(QPen(QColor(_pc('text_dim', _C_MUTED))))
            p.drawText(QRectF(0, top, w, max(16, h - top)),
                       Qt.AlignLeft | Qt.AlignVCenter, "（暂无数据）")
            p.end()
            return
        scale = LS.top_scale(self._items)
        # 右侧留 64px 画数值文字，否则长标签会把数字挤出可视区
        label_w = min(140, max(70, int(w * 0.32)))
        val_w = 64
        bar_area = max(10, w - label_w - val_w - 8)
        row_h = 22
        for i, it in enumerate(self._items):
            y = top + i * row_h
            if y + row_h > h:
                break                       # 高度不够就少画几条，绝不溢出
            # 标签（中文，超宽省略）
            p.setPen(QPen(QColor(_pc('text', _C_TEXT))))
            fm = p.fontMetrics()
            txt = fm.elidedText(str(it.get("text") or it.get("label") or ""),
                                Qt.ElideRight, label_w - 6)
            p.drawText(QRectF(0, y, label_w - 6, row_h),
                       Qt.AlignLeft | Qt.AlignVCenter, txt)
            # 条形（底槽 + 实条）
            bx = label_w
            by = y + 5
            bh = row_h - 10
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(_pc('track', _C_TRACK)))
            p.drawRoundedRect(QRectF(bx, by, bar_area, bh), 3, 3)
            ratio = LS.bar_ratio(it, scale)
            if ratio > 0:
                p.setBrush(QColor(_pc("pos", _C_POS) if it.get("sign") == "pos"
                        else _pc("neg", _C_NEG)))
                p.drawRoundedRect(QRectF(bx, by, bar_area * ratio, bh), 3, 3)
            # 数值
            p.setPen(QPen(QColor(_pc('text', _C_TEXT))))
            p.drawText(QRectF(w - val_w, y, val_w - 2, row_h),
                       Qt.AlignRight | Qt.AlignVCenter,
                       f"{float(it.get('weight') or 0):+.1f}")
        p.end()


class _TrendChart(QWidget):
    """最近 N 天审核次数的柱状趋势图（含 0 值日）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._daily: list[dict] = []
        self._max = 1
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)
        self.setFixedHeight(74)

    def set_daily(self, daily, dmax):
        self._daily = list(daily or [])
        self._max = max(1, int(dmax or 1))
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = self.width(), self.height()
        # 底部基线
        p.setPen(QPen(QColor(_pc('track', _C_TRACK)), 1))
        p.drawLine(0, h - 1, w, h - 1)
        if not self._daily:
            p.setPen(QPen(QColor(_pc('text_dim', _C_MUTED))))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter, "（暂无审核记录）")
            p.end()
            return
        n = len(self._daily)
        slot = w / n
        bw = max(2.0, min(18.0, slot - 3))
        for i, d in enumerate(self._daily):
            c = int(d.get("count") or 0)
            x = i * slot + (slot - bw) / 2
            bh = (h - 8) * (c / self._max)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(_pc("pos", _C_POS) if c else _pc("track", _C_TRACK)))
            p.drawRoundedRect(QRectF(x, h - 1 - max(1.0, bh), bw,
                                     max(1.0, bh)), 2, 2)
        # 只标首尾日期，中间不标 —— 14 个日期挤在一起必然重叠成一坨
        p.setPen(QPen(QColor(_pc('text_dim', _C_MUTED))))
        fm = p.fontMetrics()
        for idx in (0, n - 1):
            if n <= 1 and idx == 1:
                continue
            d = str(self._daily[idx].get("date") or "")
            txt = d[5:] if len(d) >= 10 else d
            tw = fm.horizontalAdvance(txt)
            x = idx * slot + (slot - tw) / 2
            p.drawText(QRectF(max(0, x), 0, min(tw + 4, w), 12),
                       Qt.AlignLeft | Qt.AlignVCenter, txt)
        p.end()


def _stat_card(text: str, caption: str) -> tuple[QWidget, QLabel]:
    """一个概览数字卡片。数字用大字号，副标题小字灰。

    返回 ``(卡片, 数字标签)`` —— **把数字标签显式带回给调用方**，
    而不是让调用方回头 ``findChild(QLabel)`` 去捞：那样依赖子控件创建顺序，
    以后有人把副标题改成 QLabel 之外的东西（或调换 addWidget 顺序）就静默指错。
    """
    box = QWidget()
    # V0.0.19-E 真机截图抓出的显示 bug：卡片不设最小高度时，老鸟模式
    # 下整排状态卡会被同布局里的大图区压扁（实拍 47px 高，19px 的
    # 大字号数字直接被裁掉，看着像一排空白条）。64px = 数字 24 +
    # 副标题 14 + 上下内边距 16，两种模式下都放得下。
    box.setMinimumHeight(64)
    # P1-4：卡片底/描边原先写死淡紫（#f6f5ff/#e2ddfb）—— 那是 violet 皮肤
    # 的专属色，换 teal/blue/amber 皮肤时这张卡片就明显不属于那个主题。
    # 改走 _tstyle 模板：登记进重渲表，切深浅/换皮肤自动跟着变。
    _tstyle(box, "QWidget{background:{card_bg};border:1px solid {card_border};"
                  "border-radius:9px;}")
    v = QVBoxLayout(box)
    v.setContentsMargins(12, 8, 12, 8)
    v.setSpacing(1)
    lab = QLabel(str(text))
    f = lab.font()
    f.setPointSize(max(11, f.pointSize() + 5))
    f.setBold(True)
    lab.setFont(f)
    _tstyle(lab, "color:{accent_deep};border:none;")
    cap = QLabel(str(caption))
    _tstyle(cap, "color:{text_dim};border:none;font-size:11px;")
    v.addWidget(lab)
    v.addWidget(cap)
    return box, lab


# ---------------------------------------------------------- V0.0.19 词表芯片
class _Chip(QPushButton):
    """一枚词表芯片。颜色区分来源与方向，单击交由外部回调处理。

    芯片本身**不写库**：看板红线是「改权重只在审核页权重明细」，
    这里只承担展示与选中，绝不出现单条改删函数。
    """

    def __init__(self, text: str, kind: str, on_pick):
        super().__init__(text)
        self.word = str(text)
        self.kind = str(kind)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setToolTip({
            "manual": "手工填写的目标词",
            "learned": "从图库学到的目标词",
            "avoid": "排除词或判退累积的回避词",
        }.get(self.kind, "词表项"))
        if self.kind == "manual":
            tpl = ("QPushButton{background:{accent_soft};color:{accent_deep};"
                   "border:1px solid {accent_soft};border-radius:11px;"
                   "padding:3px 10px;font-size:11px;}"
                   "QPushButton:hover{background:{accent_soft_hover};}")
        elif self.kind == "learned":
            tpl = ("QPushButton{background:{tint_bg};color:{tint_title};"
                   "border:1px solid {tint_border};border-radius:11px;"
                   "padding:3px 10px;font-size:11px;}"
                   "QPushButton:hover{background:{accent_soft};}")
        else:
            tpl = ("QPushButton{background:{card_bg};color:{danger};"
                   "border:1px solid {card_border};border-radius:11px;"
                   "padding:3px 10px;font-size:11px;}"
                   "QPushButton:hover{background:{accent_soft};}")
        _tstyle(self, tpl)
        self.clicked.connect(lambda: on_pick(self.word, self.kind))


class _ChipFlow(QWidget):
    """四列网格的词表芯片容器：整齐分行，不长句堆叠。"""

    COLS = 4
    MAX = 24

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(6)
        self._empty = QLabel("暂无词表记录。导入本地图库或手工添加后生成。")
        _tstyle(self._empty, "color:{text_dim};font-size:11px;")
        self._grid.addWidget(self._empty, 0, 0, 1, self.COLS)

    def set_chips(self, chips, on_pick):
        """``chips`` 为 ``(词, 类型)`` 序列；类型取 manual/learned/avoid。"""
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                if w is not self._empty:
                    w.deleteLater()
        items = list(chips or [])[:self.MAX]
        if not items:
            self._grid.addWidget(self._empty, 0, 0, 1, self.COLS)
            self._empty.setVisible(True)
            return
        for i, (word, kind) in enumerate(items):
            self._grid.addWidget(_Chip(word, kind, on_pick),
                                 i // self.COLS, i % self.COLS)


def _current_ui_mode() -> str:
    """读取当前界面模式。无活动配置（弹窗/单测）时按老鸟全开处理。"""
    try:
        from .. import config as _cfg
        cfg = _cfg.get_active_config()
        return "newbie" if str(getattr(cfg, "ui_mode", "pro") or "pro") == "newbie" \
            else "pro"
    except Exception:
        return "pro"


class LearningDashboard(QDialog):
    """学习看板。``db`` 只读使用；本对话框不提供任何改权重入口。

    V0.0.9：``embed=True`` 时作为**主界面首页**内嵌使用 —— 不再是弹窗，
    底部「关闭」按钮条整体隐藏（页面常驻，没有"关闭"语义），
    数据刷新交给主窗口切页时调 :meth:`reload`。默认 False 保持老弹窗
    行为（菜单入口 / 既有测试都用老形态，不破坏）。
    """

    def __init__(self, db, parent=None, embed: bool = False):
        super().__init__(parent)
        self.db = db
        self._embed = embed
        self.setWindowTitle("学习看板（AI 学到了什么）")
        self.resize(720, 620)
        self._build()
        self._build_terms()
        if embed:
            # 内嵌首页：QDialog 被挂进布局后 Qt 会自动按普通子部件渲染
            # （弹窗标志只对"没有父件的顶层"生效），这里只处理弹窗专属物：
            # ① 底部「关闭/刷新」按钮条整条隐掉 —— 页面常驻，没有关闭语义；
            # ② 弹窗时代标题栏那句「学习看板（AI 学到了什么）」内嵌后看不见，
            #    补一行页头，用户打开软件第一眼就知道这页是干什么的。
            try:
                self._bb.setVisible(False)
            except Exception:
                pass
            hdr = QLabel("学习看板 · 已学到的偏好与当前正在使用的找图词表")
            _tstyle(hdr, "color:{text};font-size:15px;font-weight:700;padding:2px;")
            # 页头挂外层（滚动区之外）：第一眼固定可见，不随内容滚走。
            self.layout().insertWidget(0, hdr)
        self.reload()
        self.apply_ui_mode(_current_ui_mode())

    # ---------- V0.0.19：新手 / 老鸟分层 ----------
    def apply_ui_mode(self, mode: str):
        """新手模式收起图表与明细长句，保留词表芯片与数字卡；老鸟全开。

        ``_pro_widgets`` 里的控件**只是隐藏**，数据仍在 reload 中照常刷新，
        切回老鸟模式立即可见；弹窗形态（无活动配置）默认老鸟，保持原行为。
        """
        self._newbie = str(mode) == "newbie"
        for w in getattr(self, "_pro_widgets", ()):
            try:
                w.setVisible(not self._newbie)
            except Exception:
                pass
        tip = getattr(self, "_next_tip", None)
        if tip is not None:
            tip.setVisible(self._newbie and bool(self._embed))
        self._refresh_next_tip()

    # ---------- V0.0.5：正在按哪些词找图（决定清单「词表可见可改」）----------
    def _build_terms(self):
        """顶部区块：软件采集时实际用的「要找什么 / 别找什么」词表。

        用户初衷里最关键的一环——学到的词必须能在这里看见；看见之后
        还能手动加词（加的词按「用户亲口说的」记正权重，下次采集就用）。
        """
        from PySide6.QtWidgets import QLineEdit
        from PySide6.QtCore import Qt as _Qt
        cont = QWidget()
        _tstyle(cont, "QWidget{background:{card_bg};"
                      "border:1px solid {card_border};border-radius:12px;}")
        box = QVBoxLayout(cont)
        box.setContentsMargins(14, 12, 14, 12)
        box.setSpacing(7)

        # V0.0.19：新手专属「下一步建议」引导条（弹窗形态不显示）。
        self._next_tip = QLabel("…")
        _tstyle(self._next_tip,
                "QLabel{background:{tint_bg};color:{tint_title};"
                "border:1px solid {tint_border};border-radius:9px;"
                "padding:7px 10px;font-size:12px;}")
        self._next_tip.setWordWrap(True)
        self._next_tip.setVisible(False)
        box.addWidget(self._next_tip)

        # V0.0.19：当前状态四卡（与下方学习统计五卡分属两套账）。
        # V0.0.19-E 真机截图抓出布局 bug：裸 QHBoxLayout 直接 addLayout 进
        # 本卡片（cont）的内层 QVBoxLayout 时，cont 的高度 sizeHint 没把这行
        # 算够，四张卡和下方芯片流挤在同一条 y 区间里互相叠字（实拍：状态卡
        # 只占左 1/5 宽、芯片流顶进了卡片区）。改成独立宿主 QWidget —— 它自带
        # 布局，sizeHint 正常聚合，行高与横向撑满都不再依赖外层怎么算账。
        self._status_cards = {}
        sc_host = QWidget()
        sc_row = QHBoxLayout(sc_host)
        sc_row.setContentsMargins(0, 0, 0, 0)
        sc_row.setSpacing(8)
        for key, sc_cap in (("fav", "已收藏"), ("review", "待审核"),
                            ("running", "运行中任务"), ("export", "可导出素材")):
            card, lab = _stat_card("0", sc_cap)
            self._status_cards[key] = lab
            sc_row.addWidget(card, 1)
        box.addWidget(sc_host)

        cap = QLabel("当前采集所用目标词与排除词（下载前即按此筛选）")
        _tstyle(cap, "color:{text};font-size:12px;font-weight:600;")
        box.addWidget(cap)
        # V0.0.19：长句词表改芯片网格，颜色区分来源与方向。
        self._chip_flow = _ChipFlow()
        box.addWidget(self._chip_flow)
        # 保留原词表标签：承载计数摘要与单击反馈，供无障碍与既有调用方使用。
        self.lbl_terms = QLabel("…")
        _tstyle(self.lbl_terms, "color:{text_dim};font-size:11px;")
        self.lbl_terms.setWordWrap(True)
        self.lbl_terms.setTextInteractionFlags(
            _Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.lbl_terms)
        row_host = QWidget()
        row = QHBoxLayout(row_host)
        row.setContentsMargins(0, 0, 0, 0)
        self.in_term = QLineEdit()
        self.in_term.setPlaceholderText("手工添加目标词，回车或单击右侧按钮")
        _tstyle(self.in_term,
                "QLineEdit{background:{card_bg};border:1px solid {border};"
                "border-radius:7px;color:{text};padding:4px 8px;font-size:12px;}")
        self.in_term.returnPressed.connect(self._add_term)
        self.b_add_term = QPushButton("添加")
        _tstyle(self.b_add_term,
                "QPushButton{background:{card_bg};border:1px solid {border};"
                "border-radius:7px;color:{text};font-size:12px;}"
                "QPushButton:hover{background:{accent_soft};}")
        self.b_add_term.clicked.connect(self._add_term)
        row.addWidget(self.in_term, 1)
        row.addWidget(self.b_add_term)
        box.addWidget(row_host)
        tip = QLabel("如需删除某个词，请前往审核页「权重明细」删除该词权重。")
        _tstyle(tip, "color:{text_dim};font-size:11px;")
        tip.setWordWrap(True)
        box.addWidget(tip)

        # ---- V0.0.7：把下错的图点「不喜欢 / 拉黑」教出来的词 ----
        # 必须看得见、删得掉：用户明确说过「黑名单里多了我从没写过的词」
        # 是不能接受的，所以这里既显示状态，也留单独删 / 全清空的口子。
        fb_cap = QLabel("判退图片学到的回避词")
        _tstyle(fb_cap, "color:{text};font-size:12px;font-weight:600;")
        box.addWidget(fb_cap)
        self.lbl_feedback = QLabel("…")
        _tstyle(self.lbl_feedback, "color:{text_dim};font-size:11px;")
        self.lbl_feedback.setWordWrap(True)
        self.lbl_feedback.setTextInteractionFlags(
            _Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.lbl_feedback)
        fb_host = QWidget()
        fb_row = QHBoxLayout(fb_host)
        fb_row.setContentsMargins(0, 0, 0, 0)
        self.in_fb_del = QLineEdit()
        self.in_fb_del.setPlaceholderText("输入要删除的词")
        _tstyle(self.in_fb_del,
                "QLineEdit{background:{card_bg};border:1px solid {border};"
                "border-radius:7px;color:{text};padding:4px 8px;font-size:12px;}")
        self.in_fb_del.returnPressed.connect(self._del_feedback)
        b_fb_del = QPushButton("删除该词")
        b_fb_clear = QPushButton("全部清空")
        for b in (b_fb_del, b_fb_clear):
            _tstyle(b,
                    "QPushButton{background:{card_bg};border:1px solid {border};"
                    "border-radius:7px;color:{text};font-size:12px;}"
                    "QPushButton:hover{background:{accent_soft};}")
        b_fb_del.clicked.connect(self._del_feedback)
        b_fb_clear.clicked.connect(self._clear_feedback)
        fb_row.addWidget(self.in_fb_del, 1)
        fb_row.addWidget(b_fb_del)
        fb_row.addWidget(b_fb_clear)
        box.addWidget(fb_host)
        # 插到内容区最顶部（滚动区内）。外层 self.layout() 现在是 outer，
        # 页头 hdr 才挂外层；这块词表区块要随内容滚动，挂 _inner_lay。
        self._inner_lay.insertWidget(0, cont)

    def _refresh_terms(self):
        try:
            from .. import find_terms as _ft
            self._terms = _ft.global_terms(self.db)
            learned_set = {w.lower() for w in self._terms.learned}
            chips = [(str(w), "learned" if str(w).lower() in learned_set
                      else "manual") for w in self._terms.want]
            chips += [(str(w), "avoid") for w in self._terms.avoid]
            self._chip_flow.set_chips(chips, self._on_chip_pick)
            self.lbl_terms.setText(
                f"目标词 {len(self._terms.want)} 个 · 排除词 "
                f"{len(self._terms.avoid)} 个；单击芯片可选用该词。")
        except Exception as e:
            self._chip_flow.set_chips([], self._on_chip_pick)
            self.lbl_terms.setText(f"词表读取失败：{e}")
        self._refresh_status_cards()
        self._refresh_next_tip()
        self._refresh_feedback()

    def _on_chip_pick(self, word: str, kind: str):
        """芯片选中：只把词填进相应输入框，绝不直接改写学习权重。"""
        if kind == "avoid":
            self.in_fb_del.setText(word)
            self.in_fb_del.setFocus()
            self.lbl_terms.setText(
                f"已选中排除词「{word}」；确认后请单击「删除该词」。")
        else:
            self.in_term.setText(word)
            self.lbl_terms.setText(
                f"已选中目标词「{word}」。如需移除，请前往审核页「权重明细」。")

    def _count(self, sql: str, args=()):
        """只读计数；老库缺列、假数据库、无数据一律按 0 处理。"""
        try:
            rows = self.db.query(sql, tuple(args))
            if not rows:
                return 0
            row = rows[0]
            try:
                return int(row[0] or 0)
            except Exception:
                return int(list(dict(row).values())[0] or 0)
        except Exception:
            return 0

    def _refresh_status_cards(self):
        """四张状态卡：收藏 / 待审 / 运行中 / 可导出（只读聚合）。"""
        cards = getattr(self, "_status_cards", None)
        if not cards:
            return
        try:
            from .. import config as _cfg
            cfg = _cfg.get_active_config()
            thr = float(getattr(cfg, "ai_threshold", 0.35) or 0.35)
        except Exception:
            thr = 0.35
        values = {
            "fav": self._count(
                "SELECT COUNT(*) FROM images WHERE favorite=1 "
                "AND in_quarantine=0 AND blacklisted=0"),
            "review": self._count(
                "SELECT COUNT(*) FROM images WHERE needs_review=1 "
                "AND in_quarantine=0"),
            "running": self._count(
                "SELECT COUNT(*) FROM tasks WHERE is_template=0 "
                "AND status='running'"),
            "export": self._count(
                "SELECT COUNT(*) FROM images WHERE in_quarantine=0 "
                "AND blacklisted=0 AND score>=?", (thr,)),
        }
        for k, v in values.items():
            try:
                cards[k].setText(str(v))
            except Exception:
                pass
        self._status_values = values

    def _refresh_next_tip(self):
        """新手引导条：按当前库状态给一句明确的下一步建议。"""
        tip = getattr(self, "_next_tip", None)
        if tip is None or not getattr(self, "_embed", False):
            return
        vals = getattr(self, "_status_values", None) or {}
        n_task = self._count("SELECT COUNT(*) FROM tasks WHERE is_template=0")
        n_rev = int(vals.get("review") or 0)
        n_exp = int(vals.get("export") or 0)
        if n_task <= 0:
            msg = "下一步建议：尚未创建采集任务。请前往「任务」页新建任务并填写入口网址。"
        elif n_rev > 0:
            msg = (f"下一步建议：当前有 {n_rev} 张图片待审核。"
                   "建议先完成审核，使学习结果更准确。")
        elif n_exp > 0:
            msg = (f"下一步建议：当前有 {n_exp} 张合格图片。"
                   "可前往「导出」页生成训练素材集。")
        else:
            msg = "下一步建议：暂无待处理事项。可启动采集任务继续积累素材。"
        tip.setText(msg)

    # ---------- V0.0.7：纠错反馈词（看得见、删得掉）----------
    def _refresh_feedback(self):
        try:
            from .. import feedback as _fb
            items = _fb.list_all(self.db)
            hard = _fb.HARD_HITS
        except Exception as e:
            self.lbl_feedback.setText(f"读取失败：{e}")
            return
        if not items:
            self.lbl_feedback.setText(
                "（暂无记录。对误采图片执行「不喜欢 / 拉黑」后自动累积。）")
            return
        parts = [f"{x['word']}（判退 {x['hits']} 次，"
                 f"{'下载前拦截' if x['hard'] else '待审核复核'}）"
                 for x in items[:16]]
        more = f" …等共 {len(items)} 个" if len(items) > 16 else ""
        self.lbl_feedback.setText(
            f"判退 1 次进入待审核复核；累计判退 {hard} 次后，下载前直接拦截。"
            + "、".join(parts) + more)

    def _del_feedback(self):
        from PySide6.QtWidgets import QMessageBox
        w = self.in_fb_del.text().strip()
        if not w:
            return
        try:
            from .. import feedback as _fb
            ok = _fb.remove(self.db, w)
            if not ok:
                QMessageBox.information(self, "未找到", f"记录中不存在「{w}」")
            self.in_fb_del.clear()
            self._refresh_feedback()
            self.reload()
        except Exception as e:
            QMessageBox.warning(self, "删词失败", f"{type(e).__name__}: {e}")

    def _clear_feedback(self):
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(
                self, "确认清空",
                "清空后全部回避词立即失效，无法恢复。确认继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            from .. import feedback as _fb
            n = _fb.clear(self.db)
            QMessageBox.information(self, "已清空", f"已清除 {n} 个词")
            self._refresh_feedback()
            self.reload()
        except Exception as e:
            QMessageBox.warning(self, "清空失败", f"{type(e).__name__}: {e}")

    def _add_term(self):
        """手动加一个要找的词 —— 记成正偏好（来源=用户手填），下次采集生效。"""
        from PySide6.QtWidgets import QMessageBox
        w = self.in_term.text().strip()
        if not w:
            return
        try:
            from ..repo import (set_preference, PREF_ORIGIN_USER,
                                get_preferences)
            prefs = get_preferences(self.db)
            if float(prefs.get(w.lower(), 0.0)) < 2.0:
                set_preference(self.db, 0, w, 2.0, kind="pos",
                               origin=PREF_ORIGIN_USER)
            self.in_term.clear()
            self._refresh_terms()
            self.reload()
        except Exception as e:
            QMessageBox.warning(self, "加词失败", f"{type(e).__name__}: {e}")

    # ---------- UI ----------
    def _build(self):
        # V0.0.19-E 真机截图抓出叠字 bug：改版加进「状态四卡 + 词表芯片」后，
        # 本页各行的**最小高度总和**（实测 790px）超过了首页可用高度（724px），
        # Qt 只能把行压破最小值硬塞 —— 实拍就是状态卡被压扁、文字叠成一片。
        # 根治：主体内容进滚动区，高度不够就滚动，绝不挤压行高。
        # 外层布局只放两样：内嵌页头（insertWidget(0) 的目标，v009 钉着
        # layout().itemAt(0) 必须是页头）和底部按钮条 _bb（弹窗形态可见）。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # ⚠ 视口必须显式透明：它的类名恰好是裸 QWidget，命中全局通配
        # `QWidget{background-color:…}` 的实底规则，而换肤时 Qt 不会重绘
        # 这块底 —— 实测 night→classic 后视口留着 #0f1115 暗底，整页
        # 抓图均亮 51（r68 亮度护栏当场抓红）。透明后底色交给窗口层，
        # 换肤自动跟随。
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollArea>QWidget>QWidget{background:transparent;}")
        scroll.viewport().setStyleSheet("background:transparent;")
        inner = QWidget()
        v = QVBoxLayout(inner)
        v.setSpacing(10)
        scroll.setWidget(inner)
        # insertWidget(0, …) 的两处（页头 / 顶部词表区块）插的是**内容区**，
        # 存引用而不是回头 self.layout() 捞 —— 外层现在是 outer，不是 v。
        self._inner_lay = v
        outer.addWidget(scroll, 1)

        self.lbl_summary = QLabel()
        self.lbl_summary.setWordWrap(True)
        _tstyle(self.lbl_summary, "color:{text};")
        v.addWidget(self.lbl_summary)

        cards = QHBoxLayout()
        cards.setSpacing(8)
        # (卡片, 数字标签) 一并存下来，reload 时直接 setText，不靠 findChild 去捞
        self._cards = {}
        for key, cap in (("pos", "喜欢方向权重"), ("neg", "排除方向权重"),
                         ("strong", "强权重（|w|≥1）"),
                         ("hit", "累计命中次数"), ("rev", "累计审核次数")):
            box, lab = _stat_card("0", cap)
            self._cards[key] = lab
            cards.addWidget(box, 1)
        v.addLayout(cards)

        # 双向Top-N
        self.ch_pos = _BarChart("喜欢方向 Top-N（AI 觉得「好」的标签）")
        self.ch_neg = _BarChart("排除方向 Top-N（AI 觉得「不要」的标签）")
        v.addWidget(self.ch_pos)
        v.addWidget(self.ch_neg)

        # 趋势
        t_cap = QLabel("最近审核趋势（按天）")
        _tstyle(t_cap, "color:{text_dim};font-size:11px;")
        v.addWidget(t_cap)
        self.ch_trend = _TrendChart()
        v.addWidget(self.ch_trend)

        # 判定分布（一行文字即可，不必再画图）
        self.lbl_dec = QLabel()
        _tstyle(self.lbl_dec, "color:{text};font-size:12px;")
        self.lbl_dec.setWordWrap(True)
        v.addWidget(self.lbl_dec)

        # R166：样本来源（一行文字）。与「判定分布」配对看 —— 上面是"审了多少次"，
        # 这里是"喂了多少个样本、从哪喂的"。两行数字对不上说明有动作没接学习链路。
        self.lbl_src = QLabel()
        _tstyle(self.lbl_src, "color:{text_dim};font-size:12px;")
        self.lbl_src.setWordWrap(True)
        v.addWidget(self.lbl_src)

        # R174：来源构成一行 + 一键清除按钮
        self.lbl_origin = QLabel()
        _tstyle(self.lbl_origin, "color:{text};font-size:12px;")
        self.lbl_origin.setWordWrap(True)
        v.addWidget(self.lbl_origin)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.b_clear = QPushButton("仅清模型自动词")
        _tstyle(self.b_clear,
                "QPushButton{background:{card_bg};border:1px solid {border};"
                "border-radius:7px;color:{text};font-size:12px;}"
                "QPushButton:hover{background:{accent_soft};}"
                "QPushButton:disabled{color:{text_dim};}")
        self.b_clear.setToolTip(
            "清除「模型自动学习且为负权」的记录：模型误判图片时产生的错误词条"
            "（权重为负且非手工填写），并将其从任务 AI 排除词中移除。"
            "手工填写的排除词与「喜欢」方向的学习记录均不受影响。")
        self.b_clear.clicked.connect(self._clear_model_auto)
        row.addWidget(self.b_clear)
        # R169 第 4 条：一键「重置学习统计」。
        # ⚠ 与「仅清模型自动词」**必须是两个按钮**：两者代价差一个量级 ——
        #   右边那个只清"机器认错攒下来的负权词"，用户教了几个月的成果都在；
        #   这个把**全部**学习权重清零（含正权/用户手填词），不可撤销。
        #   混成一个按钮，用户想清脏词就会顺手清光整个人格模型。
        self.b_reset = QPushButton("重置学习统计…")
        _tstyle(self.b_reset,
                "QPushButton{background:{card_bg};border:1px solid {border};"
                "border-radius:7px;color:{danger};font-size:12px;}"
                "QPushButton:hover{background:{accent_soft};}"
                "QPushButton:disabled{color:{text_dim};}")
        self.b_reset.setToolTip(
            "将**全部**学习权重清零（含「喜欢」方向与手工填写的词），"
            "恢复至未学习状态。\n"
            "此操作不可撤销 —— 如仅需清除模型误判产生的词条，请使用左侧"
            "「仅清模型自动词」。")
        self.b_reset.clicked.connect(self._reset_all_learning)
        row.addWidget(self.b_reset)
        row.addStretch(1)
        v.addLayout(row)

        tip = QLabel("提示：单条权重的修改 / 删除请到审核页的「权重明细」。"
                     "这里只做概览，除上面两个清除按钮外不改动任何学习数据。")
        _tstyle(tip, "color:{text_dim};font-size:11px;")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # V0.0.19 分层：以下明细/图表在新手模式收起（数据照常刷新）。
        # 老鸟模式与弹窗形态全部可见，与改版前的观感一致。
        self._pro_widgets = [
            self.lbl_summary, self.ch_pos, self.ch_neg, t_cap, self.ch_trend,
            self.lbl_dec, self.lbl_src, self.lbl_origin,
            self.b_clear, self.b_reset, tip,
        ]

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._bb = bb                 # V0.0.9：内嵌首页时整条要隐藏，存个引用
        b_reload = bb.addButton("刷新", QDialogButtonBox.ButtonRole.ActionRole)
        # ⚠ R47/B8：这里原来还连了一句 `b_reload.clicked.connect(self.accept)`
        # （C5「点了要关」），于是用户点「刷新」→ 重算数据 → **窗口直接消失**，
        # 真机表现就是「点一下刷新看板就没了」。
        # 「刷新」和「关闭」是两个意图不同的按钮：刷新只重算 + 重绘，
        # **绝不碰窗口的生死**（open/close/accept/reject/deleteLater 一个都不许）。
        # 刷新过程中抛异常也不该把窗口带走，所以整体兜 try/except。
        def _reload_only(*_a):
            try:
                self.reload()
            except Exception:
                # 兜底：刷新失败只留窗口，不往上抛（抛出去会被 Qt 吞成崩溃）
                pass
        b_reload.clicked.connect(_reload_only)
        bb.rejected.connect(self.reject)
        zh_buttons(bb)                # C5：Close → 关闭
        outer.addWidget(bb)           # 按钮条留在外层：不随内容滚动

    # ---------- 数据 ----------
    def reload(self):
        self._refresh_terms()
        st = LS.collect(self.db)
        self._stats = st
        self.lbl_summary.setText(LS.summary_line(st))
        self._cards["pos"].setText(str(st["pos_count"]))
        self._cards["neg"].setText(str(st["neg_count"]))
        self._cards["strong"].setText(str(st["strong_count"]))
        self._cards["hit"].setText(
            str(st["hit_pos_total"] + st["hit_neg_total"]))
        self._cards["rev"].setText(str(st["reviews_total"]))
        self.ch_pos.set_items(st["top_pos"])
        self.ch_neg.set_items(st["top_neg"])
        self.ch_trend.set_daily(st["daily"], st["daily_max"])
        parts = [f"{v['zh']} {v['count']}" for v in st["decisions"].values()]
        self.lbl_dec.setText("判定分布：" + " · ".join(parts))
        # R166：样本来源一行（订单第 4 条）——「全部动作参与学习」之后，
        # 用户要能看出每种判断各喂了多少个样本、从哪个视图喂的。
        # 与上面「判定分布」是两套账：那个读 reviews，这个读 learn_samples 账本。
        self.lbl_src.setText(LS.sample_line(st))
        # R174：来源构成 + 一键清除入口。放在「样本来源」下面：那行说的是
        # "喂了多少样本"，这行说的是"这些词里有多少是机器自己认的"。
        self._origins = LS.preference_origins(self.db)
        self.lbl_origin.setText(LS.origin_line(self._origins))
        n = int(self._origins.get("model_auto_neg") or 0)
        self.b_clear.setEnabled(n > 0)
        self.b_clear.setText(
            f"仅清模型自动词（{n}）" if n else "仅清模型自动词")
        # R169：重置按钮的可用性跟"有没有任何学习数据"走 —— 空库时点了
        # 只会弹一句"没有可重置的"，不如直接置灰（少一次无意义的点击）。
        self.b_reset.setEnabled(int(st.get("labels_total") or 0) > 0)
        return st

    # ---------- R174：清除模型自动词 ----------
    def _clear_model_auto(self):
        """清掉「模型自动 + 负权」的那一批，并把它们从任务硬黑名单摘掉。

        ⚠ 只清负权：正权那头是"用户喜欢什么"，清掉等于把用户教了几个月的
        成果一起抹了。污染黑名单的恰恰只有负权那一半。
        """
        from PySide6.QtWidgets import QMessageBox
        from ..repo import clear_model_auto_negatives

        n = int((getattr(self, "_origins", {}) or {}).get("model_auto_neg") or 0)
        if n <= 0:
            QMessageBox.information(self, "清除模型自动词",
                                    "当前没有「模型自动 + 负权」的学习记录可清。")
            return
        ok = QMessageBox.question(
            self, "清除模型自动词",
            f"将清除 {n} 条**模型自动**学到的负权记录，并把这些词从各任务的 "
            "AI 排除词里摘掉。\n\n"
            "· 仅清除负权，不影响「喜欢」方向的学习记录；\n"
            "· 手工填写的排除词不作任何修改。\n\n"
            "此操作不可撤销，确认继续？")
        if ok != QMessageBox.StandardButton.Yes:
            return
        try:
            done = clear_model_auto_negatives(self.db)
        except Exception as e:
            QMessageBox.warning(self, "清除失败", f"{type(e).__name__}: {e}")
            return
        self.reload()
        QMessageBox.information(
            self, "已清除",
            f"清掉 {done} 条模型自动学到的负权记录，"
            "相关词也已从任务 AI 排除词中摘除。")

    # ---------- R169 第 4 条：重置学习统计 ----------
    def _reset_all_learning(self):
        """把全部学习权重清零（task_id=0，全局）。

        与 :meth:`_clear_model_auto` 的区别**必须在文案里说清** —— 两者代价
        差一个量级，用户最容易在"我想清脏词"时误点这个把人格模型清光。

        ⚠ 二次确认是**必做**的（不可撤销的删除）。这里用默认按钮为 No 的
        ``question``：手快连点回车不该把用户教了几个月的成果删掉。
        """
        from PySide6.QtWidgets import QMessageBox
        from ..repo import reset_learning

        n = int((self._stats or {}).get("labels_total") or 0)
        if n <= 0:
            QMessageBox.information(self, "重置学习统计",
                                    "当前没有任何学习权重可重置。")
            return
        box = QMessageBox(self)
        box.setWindowTitle("重置学习统计")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"将把**全部 {n} 条**学习权重清零（含「喜欢」方向与"
                    "手工填写的词）。")
        box.setInformativeText(
            "此操作不可撤销，完成后将恢复至未学习状态。\n\n"
            "· 如仅需清除模型误判产生的词条，请选择「取消」并改用左侧"
            "「仅清模型自动词」；\n"
            "· 任务里的「AI 主题词 / 排除词」是**任务配置**，"
            "不在这批数据里，不会被清掉。")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.Cancel)
        # 默认落在「取消」：手快连按回车不该把用户教了几个月的成果删掉。
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("确定重置")
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            before = reset_learning(self.db)
        except Exception as e:
            QMessageBox.warning(self, "重置失败", f"{type(e).__name__}: {e}")
            return
        self.reload()
        QMessageBox.information(
            self, "已重置",
            f"已清空 {int(before)} 条学习权重。\n"
            "下次审核 / 判断时会重新开始积累。")


def open_learning_dashboard(parent, db):
    """菜单入口。返回被exec 的对话框（测试里可以拿它断言，不阻塞调用方）。"""
    # ⚠ R160 修 P0：这里原是 `from .classify.preference import ...`（单点）。
    #   单点相对导入解析到**当前包自己**（media_resource_manager.gui）名下，
    #   而 classify 是 gui 的**兄弟包**（media_resource_manager/classify/）——
    #   要够到它必须两个点（同 main_window.py 的 `from ..classify.preference`）。
    #   单点版运行时 ModuleNotFoundError 被 PySide6 静默吞掉 ⇒ 点「学习看板」
    #   菜单毫无反应（与 2026-09-30 那次 search.py 事故同一形态）。
    #   R149-B 引入（10-17 11:44），R159 全量分段跑揪出、但误判为「仪器错」
    #   把护栏改弱了；R160 修正产品码 + 恢复护栏。
    from ..classify.preference import is_learning_enabled
    dlg = LearningDashboard(db, parent)
    # R149-B：标题栏同步显示学习开关状态（开/关），让"关掉没生效"一眼可见。
    state = "开" if is_learning_enabled() else "关"
    dlg.setWindowTitle(f"学习看板（AI 学到了什么） · 学习开关：{state}")
    dlg.exec()
    return dlg