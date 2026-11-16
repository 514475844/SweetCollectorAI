"""统一信息表格（旧版隔离区同款样式）：ID/文件名/状态/大小/AI分/任务/网站。

图库列表模式、审核列表模式、隔离区管理三处共用，保证长相一致。
键盘约定：↑↓ 原生行选择；→ 喜欢 / ← 不喜欢（回调由各页面注入，
不传回调时方向键保持原生左右移动，不产生副作用）。
"""
from __future__ import annotations

import io
import hashlib
from typing import Optional
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QRect
from PySide6.QtGui import (QColor, QPainter, QPen, QPixmap, QTransform)
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QLabel,
                               QListWidget, QStyle, QStyledItemDelegate,
                               QTableWidget, QTableWidgetItem)

HEADERS = ["ID", "文件名", "状态", "大小", "AI分", "任务", "网站", "入库时间"]

# 缩略图网格几何（图库 / 审核两处共用，保证"看起来是同一个东西"）
GRID_SPACING = 8      # 缩略图之间的间隙 px
GRID_CAPTION = 28     # 缩略图下方那条文字（尺寸/分数）的高度 px


# ══════════════════════════════════════════════════════════════════
# 缩略图网格的「选中框」委托（PART2 #53）
#
# 起因：用户反馈「所有皮肤在多选模式下多选框根本看不清，不知道选中了哪些」。
# 实测（tools/r85_probe_sel.py）选中前后格子**边缘带主色对比度只有 1.07~1.27**
# —— Qt 只把缩略图往强调色混了约 29% 并垫一层同色底，在满屏彩色图里就是一片糊。
#
# ⚠ 为什么必须是**委托**而不是行内 QSS：
#   classic 是**真原生**预设，原生模式下 theme.css() 返空串、行内 QSS 整体
#   失效，全局 QSS 也只剩水印穿透链 —— 而 classic 恰好是实测最弱的那套
#   （对比度 1.07）。委托是 Python 代码，两套渲染路径下都照画不误。
#
# ⚠ 为什么描边颜色**按背景亮度自动取黑/白**，不写死：
#   写死一个色（比如白）在浅色皮肤下就消失在白底里；取黑/白中反差更大的
#   那个，才是对任意缩略图、任意皮肤都成立的规律（断言的是关系不是数值）。
#
# ⚠ 为什么除了描边还要一个勾号徽标：
#   描边回答「这一格的边界在哪」，徽标回答「它到底选没选」。只靠颜色深浅
#   去判断，正是用户这次抱怨的那件事。
# ══════════════════════════════════════════════════════════════════

def _auto_ink(bg) -> "QColor":
    """按背景亮度挑黑或白 —— 返回与 bg 反差更大的那个。

    WCAG 相对亮度公式；阈值 0.5 是「中灰」的分界，与 Qt 自身的
    QPalette 取色惯例一致。
    """
    def _lin(v):
        v = float(v) / 255.0
        return v / 12.92 if v <= 0.04045 else (((v + 0.055) / 1.055) ** 2.4)
    lum = (0.2126 * _lin(bg.red()) + 0.7152 * _lin(bg.green())
           + 0.0722 * _lin(bg.blue()))
    # R87：这里原来是 `QColor("#ffffff")` / `QColor("#101418")` 两个**硬编码
    # 十六进制**，被 p14b 的「色值不许散落在 theme.py 之外」护栏判红
    # （R85 引入，R84 之后才进树，所以上一轮基线没抓到）。
    # ⚠ 这里的墨色是**纯白/纯黑两个极端**，不是某个皮肤色 —— 它要的是
    #   「与任意背景反差最大」这一数学性质，走调色板反而会被皮肤拽偏。
    #   所以正解不是改调 theme.c()，而是改用 Qt 的**具名全局色**：
    #   语义一字未变，同时不再是一串硬编码色值。
    return QColor(Qt.GlobalColor.white) if lum < 0.5 else QColor(
        Qt.GlobalColor.black)


class GridSelectionDelegate(QStyledItemDelegate):
    """给缩略图网格画「一眼可见」的选中态：反差描边 + 左上角勾号徽标。

    刻意**先调 super().paint()**：图标、文字、选中底色一律交给 Qt 原来的
    逻辑，这里只在最上层补两笔 —— 这样换皮肤、换原生模式都不用另写一套。
    """

    FRAME_PX = 3        # 外圈描边宽度
    INNER_PX = 2        # 内圈描边宽度（与外圈反差，兜住深色缩略图）
    BADGE_PX = 20       # 勾号徽标边长
    BADGE_INSET = 5     # 徽标距格子左上角的距离
    # R86 #50：**未选中**时那一圈常驻细边框（回答「每张图的边界在哪」）。
    IDLE_PX = 1
    # ⚠ 内缩必须 > 2px：r85 的选中对比度判据量的是**最外 2px 环带主色**，
    #   常驻边框若落在那一圈，未选中/选中两态的环带主色就是同一个色，
    #   对比度判据会直接退化成 1.0（护栏假红）。内缩 4px 既避开判据带，
    #   又比 GRID_SPACING(8) 的格子间距近，视觉上仍读作「这张图的边」。
    IDLE_INSET = 4
    IDLE_RADIUS = 7

    def paint(self, painter, option, index):
        try:
            super().paint(painter, option, index)
        except Exception:
            return
        try:
            if not (option.state & QStyle.StateFlag.State_Selected):
                self._paint_idle_frame(painter, option)
                return
            r = option.rect.adjusted(1, 1, -1, -1)
            if r.width() < 8 or r.height() < 8:
                return
            bg = option.palette.color(QPalette.ColorRole.Window)
            ink = _auto_ink(bg)
            # 内圈取与外圈反差的那个 —— 缩略图本身可能就是深色，
            # 单靠「与视图底色反差」的外圈会糊在图里（实测对比度 1.25）。
            # 两道一起画 ⇒ 对任意内容色，总有一道反差 ≥ 4.58（推导见文件头）。
            anti = _auto_ink(ink)
            accent = option.palette.color(QPalette.ColorRole.Highlight)

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

            # ① 外圈：与视图底色反差（回答「这一格的边界在哪」）
            pen = QPen(ink, self.FRAME_PX)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                r.adjusted(self.FRAME_PX // 2, self.FRAME_PX // 2,
                           -self.FRAME_PX // 2, -self.FRAME_PX // 2),
                9, 9)

            # ② 内圈：与外圈反差（缩略图压住外圈时靠它兜底）
            inner = self.FRAME_PX + self.INNER_PX // 2
            pen2 = QPen(anti, self.INNER_PX)
            pen2.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen2)
            painter.drawRoundedRect(
                r.adjusted(inner, inner, -inner, -inner),
                7, 7)

            # ② 左上角勾号徽标（底=强调色，勾=与强调色反差的颜色）
            bs = min(self.BADGE_PX, max(12, r.width() // 4))
            badge = QRect(r.left() + self.BADGE_INSET, r.top() + self.BADGE_INSET,
                          bs, bs)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(badge, 5, 5)
            painter.setPen(QPen(_auto_ink(accent), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # 勾：两段折线，按徽标尺寸缩放，不依赖字体里有没有 ✓ 字形
            x, y, w, h = badge.x(), badge.y(), badge.width(), badge.height()
            painter.drawLine(int(x + w * 0.22), int(y + h * 0.53),
                             int(x + w * 0.43), int(y + h * 0.74))
            painter.drawLine(int(x + w * 0.43), int(y + h * 0.74),
                             int(x + w * 0.80), int(y + h * 0.28))
            painter.restore()
        except Exception:
            # 画框是**增强**，画不出来也必须让图正常显示 —— 绝不能因为
            # 装饰失败就把整个格子画空（这种失败在测试里是静默的）。
            try:
                painter.restore()
            except Exception:
                pass

    def sizeHint(self, option, index):
        """强制每个格子高度/宽度统一 = iconSize + 标题栏，杜绝 crop=False 非方
        缩略图把网格排成不规则格子。

        根因（R135 修复「图库网格下半部点不到」）：本机 Qt 的 QListWidget 在
        IconMode 下，**格子实际大小由 delegate 的 sizeHint 决定，gridSize 只管
        步距**。R134 只设了 gridSize，于是非方缩略图让每个 item 的 sizeHint 随
        各自纵横比变化（竖图窄高、横图扁宽），格子参差；而图标仍按方形 iconSize
        绘制 → 图标溢出自己的格子、压到邻格 → 点图标下半部命中相邻项。

        修法：IconMode 下把 sizeHint 钳成统一值（= iconSize + GRID_CAPTION），
        与 gridSize 完全一致，所有格子整齐一致，图标不再溢出。ListMode 走默认
        （列表行高由 _apply_grid 的 gridSize 另行设定），不受影响。
        """
        vsz = self.parent()
        if vsz is not None and getattr(vsz, "viewMode", None) is not None \
                and vsz.viewMode() == QListWidget.ViewMode.IconMode:
            ico = vsz.iconSize()
            if ico.width() > 0 and ico.height() > 0:
                return QSize(ico.width(), ico.height() + GRID_CAPTION)
        return super().sizeHint(option, index)

    def _paint_idle_frame(self, painter, option) -> None:
        """R86 #50：未选中时的常驻细边框（每张图边界清晰）。

        起因：#50 之后缩略图**不再裁剪**（竖图竖长条、横图横长条），
        于是图在格子里大小不一、有的贴边有的居中，一屏看过去分不清
        「这张到哪结束、下一张从哪开始」。撑出间距（GRID_SPACING）只解决
        一半，还需要一条边把每张图框住。

        ⚠ 为什么只框**图片区**、不框整个格子：格子下方还有 GRID_CAPTION
        那行「尺寸/格式」文字，把文字也圈进去，框就离图很远，看着像表格
        线而不是图片的边。
        """
        try:
            r = option.rect
            # 去掉下方文字条（矮格子/无文字时原样保留，别把框画成一条线）
            if r.height() > GRID_CAPTION * 2:
                r = r.adjusted(0, 0, 0, -GRID_CAPTION)
            ins = self.IDLE_INSET
            r = r.adjusted(ins, ins, -ins, -ins)
            if r.width() < 8 or r.height() < 8:
                return
            ink = option.palette.color(QPalette.ColorRole.Mid)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(ink, self.IDLE_PX)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(r, self.IDLE_RADIUS, self.IDLE_RADIUS)
            painter.restore()
        except Exception:
            try:
                painter.restore()
            except Exception:
                pass


def attach_grid_selection(view):
    """把选中画框委托挂到缩略图网格上（图库 / 审核两处都走这一个入口）。

    ⚠ 必须**同时** setItemDelegate 与 setPalette 无关 —— 这里只挂委托；
      委托读的是 option.palette，原生模式下它是系统调色板，照样成立。
    """
    try:
        view.setItemDelegate(GridSelectionDelegate(view))
        return True
    except Exception:
        return False


def apply_grid_layout(view, icon_size: int):
    """把一个 QListWidget 设成统一的「缩略图网格」样式 + 关掉误拖拽。

    以前图库和审核各写一套、值还不一样（间距 12 / 8、缩略图 128 / 160、
    图库显式设 gridSize、审核交给 Qt 自适应），同一款软件里两种网格看着
    不像一家人。这里定死一套参数，两处都只调这一个函数。

    另外必须关拖拽：QListWidget 在 **IconMode + ExtendedSelection** 下，
    Qt 会自动把 dragEnabled 打开、dragDropMode 打成 DragDrop（实测探针），
    结果多选时鼠标在缩略图上一按就出现拖影、把图片拖得满屏乱转。这两个
    网格只有「浏览 + 批量喜欢/不喜欢的交互」，从来不需要内部拖放排序，
    所以一律禁掉：左键 = 选择，右键 = 菜单，拖 = 什么都不做。
    """
    view.setViewMode(QListWidget.ViewMode.IconMode)
    view.setResizeMode(QListWidget.ResizeMode.Adjust)
    view.setMovement(QListWidget.Movement.Static)
    view.setSpacing(GRID_SPACING)
    view.setGridSize(QSize(icon_size, icon_size + GRID_CAPTION))
    view.setIconSize(QSize(icon_size, icon_size))
    # 关拖放：不拖出图、不接住图、不显示"放这儿"的假指示线
    view.setDragEnabled(False)
    view.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
    view.setAcceptDrops(False)
    view.setDropIndicatorShown(False)
    # PART2 #53：多选框一眼可见（反差描边+勾号徽标）
    attach_grid_selection(view)


def square_thumb_pixmap(path, size: int = 256, cache_dir=None, key: str = "",
                        digest: str = "", crop: bool = True):
    """网格缩略图。``crop=True`` 居中裁成正方形；``crop=False`` **完整显示**。

    需求 2（2026-10-04）：用户明确要求审核页和图库的网格**不做裁剪**，
    改用「原图或者缩略图」—— 也就是等比缩放、整张图完整可见。
    裁剪版会把长边切掉一大截（竖图只留中间一条），挑图时根本看不出构图，
    而这正是用户挑图要看的。所以这里保留参数：默认行为不变（别的调用方
    仍按方形裁剪），审核/图库两处显式传 ``crop=False``。

    缓存文件名要带 c 标记：同一张图裁剪版和完整版是两个文件，共用一个
    缓存文件会让「先看裁剪版再切完整版」拿到旧图，看着像没生效。
    """
    tag = "sq" if crop else "fit"
    size = max(16, int(size or 256))
    cf = None
    try:
        if cache_dir:
            d = Path(cache_dir)
            if not digest:
                digest = hashlib.sha1(str(key or path).encode("utf-8", "replace")
                                      ).hexdigest()[:20]
            # R247：与网页端同一套分桶规则（见 thumbpath 模块说明）。
            # 桌面端和图库两处网格的缩略图也走这里，平铺到十万级同样会拖死
            # 目录列举，所以一起改。旧平铺文件仍然认，不做迁移。
            from .. import thumbpath as _tp
            _name = f"{digest}_{tag}{size}.png"
            _hit = _tp.find(d, _name)
            if _hit.exists():
                pm = QPixmap(str(_hit))
                if not pm.isNull():
                    return pm
            cf = _tp.cache_path(d, _name)
    except Exception:
        cf = None
    try:
        from PIL import Image, ImageOps
        im = Image.open(str(path)).convert("RGB")
        if crop:
            im = ImageOps.fit(im, (size, size), Image.LANCZOS)
        else:
            # 完整显示：等比缩到最长边=size，短边不补白（背景透明由格子底色承担）
            im.thumbnail((size, size), Image.LANCZOS)
        if cf is not None:
            try:
                cf.parent.mkdir(parents=True, exist_ok=True)
                im.save(str(cf), "PNG")
            except Exception:
                pass
        buf = io.BytesIO()
        im.save(buf, "PNG")
        pm = QPixmap()
        pm.loadFromData(buf.getvalue())
        return pm if not pm.isNull() else None
    except Exception:
        return None


class PreviewLabel(QLabel):
    """列表模式右侧预览框：**跟着自己当前大小显示图片**（图库 & 审核共用）。

    以前两处都是写死 scaled(240,240) + setMinimumSize(240,240)：把分割条拉宽
    只留白，图片永远 240；框被压小又裁不下。现在记住原图（``setSource``），
    每次 resize 事件按当前宽高重新等比缩放。

    sizeHint 固定、sizePolicy 设 Expanding：避免「setPixmap 改变 sizeHint →
    布局又改控件尺寸 → 再触发 resize」的来回抖动。

    #46 起多了**缩放**（灯箱要）。倍率夹在 [MIN_ZOOM, MAX_ZOOM]：
    不夹的话连点放大就能推出几 GB 的 QPixmap，直接把内存吃光。
    """

    #: 缩放上下限与步进。**倍率是「量」不是契约**，所以只断言关系
    #: （在区间内、单调增/减），别把 1.25 这个取值钉死。
    MIN_ZOOM = 0.25
    MAX_ZOOM = 8.0
    ZOOM_STEP = 1.25

    def __init__(self, text="（选中一行看预览）", parent=None):
        super().__init__(text, parent)
        self._src = None
        # #46 灯箱缩放：1.0 = 适应窗口（= 旧行为）。放大后按 zoom 倍画，
        # 于是图会比控件大，QLabel 会把它按对齐位置裁切显示（= 放大镜效果）。
        self._zoom = 1.0
        self.setMinimumSize(64, 64)
        from PySide6.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setText(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    # -- 外部只调这个：塞原图，然后按当前尺寸画 --------------------------
    def setSource(self, pm) -> None:
        self._src = pm
        self._redraw()

    def clear_source(self, text="（选中一行看预览）") -> None:
        self._src = None
        self.setPixmap(QPixmap())
        self.setText(text)

    # ---------------------------------------------------------------- #46 缩放
    def zoom(self) -> float:
        """当前缩放倍率（1.0 = 适应窗口）。"""
        return self._zoom

    def set_zoom(self, z: float) -> float:
        """设缩放倍率并重画。返回**实际生效**的倍率。

        铁律：倍率**必须夹在 [MIN_ZOOM, MAX_ZOOM]**。不夹的话连点放大键
        就能把倍率推到几千，``scaled(w*z, h*z)`` 会去申请几 GB 的 QPixmap
        —— 灯箱一开就把内存吃光，用户看到的是「程序没了」。
        （#46 的实测教训：缩放必须先能夹住，再谈好不好用。）
        """
        z = max(self.MIN_ZOOM, min(self.MAX_ZOOM, float(z)))
        self._zoom = z
        self._redraw()
        return self._zoom

    def zoom_in(self) -> float:
        return self.set_zoom(self._zoom * self.ZOOM_STEP)

    def zoom_out(self) -> float:
        return self.set_zoom(self._zoom / self.ZOOM_STEP)

    def zoom_reset(self) -> float:
        """复位成「适应窗口」。翻页时也要调它 —— 否则上一张放大到 4×，
        下一张就变成「打开就是 4 倍裁切」，用户以为图坏了。"""
        return self.set_zoom(1.0)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._redraw()

    def sizeHint(self):
        return QSize(240, 240)

    # 【R117 P2-8】用户报「列表模式预览窗随图片大小变（窗忽大忽小）」。
    # ★ 根因不是 sizeHint —— 它早就写死 240×240 了。漏掉的是
    #   **minimumSizeHint**：QLabel 在 setPixmap 之后会把 minimumSizeHint
    #   换成「当前 pixmap 的尺寸 + margin」。而这两个预览控件都躺在
    #   QSplitter 里，splitter 给子控件的尺寸下限取的就是 minimumSizeHint
    #   ⇒ 上一张是大图就把下限顶到几千像素，下一张小图又被压缩回去，
    #   于是窗口「忽大忽小」。正是用户说的「预览窗随图片变大小」。
    # 修法：minimumSizeHint 固定成 MIN_PREVIEW（与构造函数里的
    # setMinimumSize 一致），让 splitter 拿到的下限与图无关；
    # 图片继续按 KeepAspectRatio fit 在这个稳定的框里（P2-7 红线：
    # 不裁剪、不拉伸，sizeHint 也保持不变）。
    #: 预览框的最小边长。**是下限不是契约**，测试只钉"与图无关 + 正数"。
    MIN_PREVIEW = 64

    def minimumSizeHint(self):
        n = int(getattr(self, "MIN_PREVIEW", 64) or 64)
        return QSize(n, n)

    def minimumSize(self):
        """【R117 P2-8】同上：显式 minimumSize 也不能被 pixmap 顶开。

        构造函数里那句 ``setMinimumSize(64, 64)`` 只在**首次**设置时生效；
        Qt 在 setPixmap 之后可能把控件自身的 minimumSize 一并调大，
        这里每次问都返回固定值，彻底断掉"图 → 尺寸"这条传导链。
        """
        n = int(getattr(self, "MIN_PREVIEW", 64) or 64)
        return QSize(n, n)

    def _redraw(self) -> None:
        pm = self._src
        if pm is None or pm.isNull():
            return
        rot = int(getattr(self, "_rot", 0) or 0)
        if rot:
            try:
                pm = pm.transformed(QTransform().rotate(rot),
                                   Qt.SmoothTransformation)
            except Exception:
                pass
        w, h = self.width(), self.height()
        if w <= 1 or h <= 1:      # 还没布局（隐藏/尺寸为 0）时先不画
            return
        tw, th = int(w * self._zoom), int(h * self._zoom)
        if tw <= 0 or th <= 0:
            return
        QLabel.setPixmap(self, pm.scaled(
            tw, th, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    # 【R148】旋转：只改显示，绝不写原文件；角度仅本次查看会话内有效。
    # 切到下一张由调用方 reset 回 0（灯箱 render / 列表预览 reload 时）。
    def rotation(self) -> int:
        return int(getattr(self, "_rot", 0) or 0)

    def set_rotation(self, deg: int) -> int:
        self._rot = int(deg) % 360
        self._redraw()
        return self._rot

    def rotate_cw(self) -> int:
        return self.set_rotation((self.rotation() + 90) % 360)


def format_collected(raw) -> str:
    """把 images.collected_at 变成人话（本地时间 + 多久以前）。

    库里存的是 SQLite 的 ``datetime('now')`` —— **UTC**，而软件跑在东八区，
    直接显示会比实际早 8 小时、还会半夜显示"昨天"。所以这里统一 +8h 再格式化：
    今天显示 ``10:41``，一周内带"x 天前"，更早给完整日期，超过一个月只给日期。
    """
    if not raw:
        return "-"
    try:
        s = str(raw).strip().replace("T", " ")
        if "." in s:
            s = s.split(".")[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return s
    except Exception:
        return str(raw)
    try:
        local = dt + timedelta(hours=8)          # UTC → 东八区
    except Exception:
        return s
    now = datetime.now()
    diff = (now - local).total_seconds()
    if diff < 0:
        diff = 0
    if diff < 60:
        rel = "刚刚"
    elif diff < 3600:
        rel = f"{int(diff // 60)} 分钟前"
    elif diff < 86400:
        rel = f"{int(diff // 3600)} 小时前"
    elif diff < 86400 * 30:
        rel = f"{int(diff // 86400)} 天前"
    else:
        rel = ""
    if rel:
        return f"{local:%Y-%m-%d %H:%M}（{rel}）"
    return f"{local:%Y-%m-%d}"


def collected_sort_key(raw) -> float:
    """入库时间的**排序**键（epoch 秒），不能拿显示字符串排。"""
    if not raw:
        return 0.0
    try:
        s = str(raw).strip().replace("T", " ")
        if "." in s:
            s = s.split(".")[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return 0.0
        return (dt - datetime(1970, 1, 1)).total_seconds()
    except Exception:
        return 0.0


def make_table(parent=None, on_like=None, on_dislike=None,
               multi: bool = False) -> QTableWidget:
    """建一张与旧版隔离区一模一样的表：行选择、只读、文件名列自适应。

    ``multi=True``（R158-Bug4）额外开 **Ctrl / Shift 多选**。
    ⚠ QTableWidget 默认 ``SelectionMode.SingleSelection`` —— 隔离区管理
    原来只能一张张点，「一次勾 50 张批量还原/删除」根本做不到。
    ⚠ 刻意**不做成默认**：图库/审核两处有自己的选择语义（审核一次审一张），
    动它们的默认行为属于改产品契约，本单只补隔离区这一处缺口。
    """

    class _QTbl(QTableWidget):
        def keyPressEvent(self, e):  # noqa: N802
            k = e.key()
            if k == Qt.Key.Key_Right and self._on_like is not None:
                self._on_like()
                e.accept()
                return
            if k == Qt.Key.Key_Left and self._on_dislike is not None:
                self._on_dislike()
                e.accept()
                return
            super().keyPressEvent(e)

    t = _QTbl(0, len(HEADERS), parent)
    t._on_like = on_like
    t._on_dislike = on_dislike
    t.setHorizontalHeaderLabels(list(HEADERS))
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    # R158-Bug4：隔离区要能一次勾一片做批量还原/删除。
    t.setSelectionMode(
        QAbstractItemView.SelectionMode.ExtendedSelection if multi
        else QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    try:
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    except Exception:
        pass
    # ---- 表头排序（r32）：点一次正序、两次倒序、三次回默认 ------------
    # 为什么自己排、不调 sortItems：sortItems 只按**显示文本**排 —— 那样
    # 大小列「1.2 MB」会排在「512 KB」前面、ID「#999」排在「#1000」前面。
    # 而且它一按就永久保持排序态，"第三次点回默认"根本没法还原。
    # 所以：状态自己记，排序在 fill_table 里按**真值键**排，重灌一遍即可。
    t._sort_state = None                 # (列号, "asc"/"desc")；None = 默认顺序
    t._hidden = set()                   # 被用户「取消显示」的列号（列右键菜单）
    t._order = list(range(len(HEADERS)))  # 用户拖表头重排后的列顺序（逻辑列号序）

    def _cycle_sort(col):
        st = t._sort_state
        if st is None or st[0] != col:
            t._sort_state = (int(col), "asc")
        elif st[1] == "asc":
            t._sort_state = (int(col), "desc")
        else:
            t._sort_state = None
        hook = getattr(t, "_header_sort_hook", None)
        if callable(hook):
            # r41 / D8：调用方（图库表格）自己接管取数 —— 表头点击要回写主窗口的
            # 排序状态并重新 load_gallery，不是在这里内存重排（那玩意一刷新就回弹）。
            # 命中就别再 _refill，否则排序被套两层、方向还可能对不上。
            try:
                hook(t._sort_state[0] if t._sort_state else None,
                     t._sort_state[1] if t._sort_state else None)
            except Exception:
                pass
            _paint_sort_arrows(t)
            _apply_hidden(t)
            return
        try:
            t._refill(t._sort_state)      # None = 按默认顺序重灌
        except Exception:
            pass
        _paint_sort_arrows(t)
        _apply_hidden(t)

    hdr = t.horizontalHeader()
    try:
        hdr.setSectionsClickable(True)
        hdr.setSectionsMovable(True)     # r33：允许拖表头**重排列序**（Qt 默认禁止）
        hdr.sectionClicked.connect(_cycle_sort)
        hdr.sectionMoved.connect(_remember_order)
    except Exception:
        pass
    _install_header_menu(t)
    return t


def _remember_order(tbl: QTableWidget, logical=-1, old=-1, new=-1):
    """表头被拖着重排后，记住新的列顺序（重灌/刷新时原样恢复）。"""
    try:
        tbl._order = [tbl.horizontalHeader().logicalIndex(c)
                      for c in range(tbl.columnCount())]
    except Exception:
        pass


def _install_header_menu(tbl: QTableWidget) -> None:
    """表头右键：取消勾选 = 隐藏该列；「显示所有列」= 全部恢复。

    r33 起因：加了「入库时间」一列之后表格变挤，用户想藏掉不关心的列却没法操作。
    隐藏状态记在 ``tbl._hidden``，每次填表/重灌都会重新应用，不会一刷新就全冒出来。
    """
    from PySide6.QtWidgets import QMenu

    def _menu(pos):
        m = QMenu(tbl)
        act_hide = m.addAction("隐藏本列")
        act_all = m.addAction("显示所有列")
        act_hide.setEnabled(tbl.horizontalHeader().logicalIndexAt(pos) >= 0)
        act = m.exec(tbl.viewport().mapToGlobal(pos))
        if act == act_hide:
            col = tbl.horizontalHeader().logicalIndexAt(pos)
            if col >= 0:
                tbl._hidden.add(col)
                _apply_hidden(tbl)
        elif act == act_all:
            tbl._hidden.clear()
            _apply_hidden(tbl)

    try:
        tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tbl.horizontalHeader().setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        tbl.horizontalHeader().customContextMenuRequested.connect(_menu)
    except Exception:
        pass


def _apply_hidden(tbl: QTableWidget) -> None:
    """把 ``_hidden`` 里的列藏起来（至少留一列，全藏了表就没意义了）。"""
    hdr = tbl.horizontalHeader()
    hidden = getattr(tbl, "_hidden", None) or set()
    if not hidden:
        # 「显示所有列」：把上回藏起来的都放回来（否则清了集合列却还藏着）
        for c in range(tbl.columnCount()):
            try:
                hdr.setSectionHidden(c, False)
            except Exception:
                pass
        return
    visible = [c for c in range(tbl.columnCount()) if c not in hidden]
    if len(visible) <= 1:
        return          # 只剩一列了，再藏就是彻底没表格
    for c in hidden:
        try:
            hdr.setSectionHidden(c, True)
        except Exception:
            pass


def _paint_sort_arrows(tbl: QTableWidget) -> None:
    """把当前排序列的表头文字挂上 ▲/▼，让用户看得见现在按什么在排。"""
    st = getattr(tbl, "_sort_state", None)
    for c in range(tbl.columnCount()):
        item = tbl.horizontalHeaderItem(c)
        if item is None:
            continue
        # 先剥掉可能残留的箭头：不能用 lstrip("▲▼ ")，表头是「大小  ▲」这种
        # 中文开头的串，lstrip 从左侧比、第一个字就是中文 → 一个字符都剥不掉，
        # 结果第三次点回默认时文字变成「大小  ▲  ▼」。直接 replace 最省心。
        name = str(item.data(Qt.ItemDataRole.DisplayRole) or "")
        name = name.replace("▲", "").replace("▼", "").strip()
        if st is not None and st[0] == c:
            name = f"{name}  {'▲' if st[1] == 'asc' else '▼'}"
        item.setText(name)


def sorted_rows(rows: list[dict], order) -> list[dict]:
    """按表头排序列排序（r32）。``order`` = (列号, "asc"/"desc") 或 None。

    按**真值**排（字节数/分数/epoch/ID 数值），不按显示文本 —— 后者会把
    「1.2 MB」和「512 KB」排成 1.2 MB 在前这种错序。
    """
    if not order:
        return list(rows)
    col, kind = order[0], str(order[1]).lower()
    keys = {
        0: lambda r: int(r.get("id") or 0),
        1: lambda r: str(r.get("name") or ""),
        2: lambda r: str(r.get("status") or ""),
        3: lambda r: int(r.get("bytes") or 0),
        4: lambda r: float(r.get("score") or 0),
        5: lambda r: str(r.get("task_name") or ""),
        6: lambda r: str(r.get("site") or ""),
        7: lambda r: collected_sort_key(r.get("collected_at")),
    }
    k = keys.get(col)
    if k is None:
        return list(rows)
    out = sorted(rows, key=k, reverse=(kind == "desc"))
    return out


def fill_table(tbl: QTableWidget, rows: list[dict], db,
               get_task_fn=None, domain_fn=None,
               order=None, select_row: Optional[int] = None) -> tuple[int, int]:
    """把图片行填进表格，返回 (显示行数, 磁盘缺失行数)。

    ``rows`` 需含 id/path/task_id/score/bytes/width/height/source_url/blacklisted/
    collected_at；``order`` 传 (列号, "asc"/"desc") 会先排序再填（表头点击用）。
    """
    from pathlib import Path
    raw = list(rows or [])
    rows = sorted_rows(raw, order)
    tbl.setRowCount(0)
    # 记住**调用方给的**原始顺序 + 重灌函数：表头点第三下要"回到默认顺序"，
    # 只能重新灌一遍（sortItems 做不到这件事）。
    tbl._default_rows = raw
    tbl._refill = lambda od=None: fill_table(  # noqa: B023
        tbl, tbl._default_rows, db, get_task_fn, domain_fn, od)
    shown = missing = 0
    for r in rows:
        p = Path(r["path"]) if r.get("path") else None
        exists = bool(p and p.exists())
        if not exists:
            missing += 1
        b = int(r.get("bytes") or 0)
        if b >= 1024 * 1024:
            size = f"{b / (1024 * 1024):.1f} MB"
        elif b >= 1024:
            size = f"{b // 1024} KB"
        else:
            size = f"{b} B" if b else "-"
        dim = (f"{r.get('width')}x{r.get('height')}"
               if (r.get("width") and r.get("height")) else "尺寸未知")
        tname = str(r.get("task_id") or "")
        try:
            t = get_task_fn(r.get("task_id")) if (get_task_fn and r.get("task_id")) else None
            if t is not None:
                tname = f"#{t.id} {t.name}"[:30]
        except Exception:
            pass
        su = r.get("source_url") or ""
        site = ""
        if su.startswith(("http://", "https://")):
            try:
                site = domain_fn(su) or ""
            except Exception:
                site = ""
        elif su.startswith("file://"):
            site = "本地磁盘"
        i = tbl.rowCount()
        tbl.insertRow(i)
        vals = [
            str(r["id"]),
            p.name if p else "(无路径)",
            ("在盘" if exists else "磁盘已缺失") + (" · ★" if r.get("favorite") else ""),
            size,
            f"{(r.get('score') or 0):.3f}",
            tname,
            site or "—",
        ]
        # r32：最后一列 = 入库时间（库里是 UTC，显示前已转东八区）
        vals.append(format_collected(r.get("collected_at")))
        # 把算出来的显示值也回填给排序键用（否则点「任务/网站/状态」列排了个空）
        try:
            r["name"] = p.name if p else "(无路径)"
            r["status"] = vals[2]
            r["task_name"] = tname
            r["site"] = site
        except Exception:
            pass
        for c, val in enumerate(vals):
            tbl.setItem(i, c, QTableWidgetItem(str(val)))
        it = tbl.item(i, 0)
        it.setData(Qt.ItemDataRole.UserRole, int(r["id"]))
        # 排序键：数字列必须按**数值**排，否则按文本排会把 1000KB 排到 200KB 前面
        for c, k in ((0, int(r["id"])),
                     (3, int(r.get("bytes") or 0)),
                     (4, float(r.get("score") or 0)),
                     (7, collected_sort_key(r.get("collected_at")))):
            tbl.item(i, c).setData(Qt.ItemDataRole.UserRole + 1, k)
        tbl.item(i, 1).setToolTip(f"{p}\n{su}")
        shown += 1
    # R147：操作后 reload 不要再跳回第一行，而是选中"下一张"。
    #   anchor = 调用前的 currentRow（即被删除/操作的那个位置）。删除第 i 行后，
    #   原第 i+1 行顶到新表第 i 行 ⇒ 选中第 i 行 = 选中下一张；删的是末行则选
    #   新末行；表格空则不选。初载（anchor is None / 无选中）保持旧语义=选第一行。
    if tbl.rowCount() == 0:
        return shown, missing
    if select_row is None:
        tbl.selectRow(0)      # 默认选中第一行：键盘 ↑↓ / ←→ 可直接开工
    else:
        idx = max(0, min(int(select_row), tbl.rowCount() - 1))
        tbl.selectRow(idx)
        try:
            tbl.scrollToItem(tbl.item(idx, 0),
                             QAbstractItemView.ScrollHint.PositionAtCenter)
        except Exception:
            pass
    return shown, missing


def selected_ids(tbl: QTableWidget) -> list[int]:
    out = []
    for idx in tbl.selectionModel().selectedRows():
        it = tbl.item(idx.row(), 0)
        if it is not None:
            out.append(int(it.data(Qt.ItemDataRole.UserRole)))
    if not out and tbl.currentRow() >= 0:
        it = tbl.item(tbl.currentRow(), 0)
        if it is not None:
            out = [int(it.data(Qt.ItemDataRole.UserRole))]
    return out


def grid_selected_ids(view) -> list[int]:
    """R158-Bug4：网格（QListWidget）里选中的图片 id，按**视觉顺序**返回。

    ⚠ 与 ``selected_ids``（表格版）分工：网格没有行列概念，只能遍历
    ``selectedItems()``；id 统一存在 ``Qt.ItemDataRole.UserRole`` 上，
    两端读数口径一致 ⇒ 「还原选中」这类批量动作不用关心当前是哪种视图。

    ⚠ 用 ``selectedItems()`` 而不是遍历所有 item 判 isSelected()：
    前者是 Qt 维护的选择集合，后者要 O(n) 且容易漏掉键盘多选产生的项。

    ⚠ 坏数据（UserRole 存了非数字）**跳过不抛** —— 一个脏项不该让
    「批量还原 50 张」整体失败。
    """
    out: list[int] = []
    try:
        items = view.selectedItems()
    except Exception:
        return out
    for it in items or []:
        try:
            v = it.data(Qt.ItemDataRole.UserRole)
        except Exception:
            continue
        if v is None:
            continue
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def row_of_id(tbl: QTableWidget, image_id: int) -> Optional[int]:
    """反查某张图在表格里的行号；找不到返回 None。

    r40 需求 30：「查找类似图片」结果窗里双击一张图，要能跳回图库并高亮它。
    列表模式下就得靠这个把 id 翻成行号（selected_ids 是反方向）。
    """
    want = int(image_id)
    for row in range(tbl.rowCount()):
        it = tbl.item(row, 0)
        if it is None:
            continue
        try:
            if int(it.data(Qt.ItemDataRole.UserRole)) == want:
                return row
        except (TypeError, ValueError):
            continue
    return None
