"""内置灯箱：大图查看 + 翻页 + 元数据，Esc 关闭。

键位**随上下文变**（#56 D21/D22，R153 修订）—— 这是本类最容易写错的地方：
  · 审核页灯箱（``on_decide`` 非空）：← 上一张 / → 喜欢（判完自动下一张），
    ↑/↓ 翻页。**「不喜欢」已按 R153 从灯箱撤掉**（用户原话：「把灯箱里面的
    『不喜欢』这个功能直接去掉吧，在网格页或者列表页有这个功能就够了」）——
    网格/列表页的右键菜单与按钮保留「不喜欢」，本单只动灯箱。
  · 纯浏览灯箱（``on_decide=None``）：四向都只翻页。
底部翻页按钮的文案也跟着上下文走（审核页显示「上一张 (←)」「下一张 (↓)」，
因为 → 是「喜欢」不是翻页），否则按钮上印的键位提示与真实键位相反，
照着按钮按 → 会得到一次「喜欢」而不是翻页。

#26 方向收敛：**灯箱只保留在审核页**（那里能直接「喜欢/不喜欢」才算主流程）。
图库的三个灯箱入口（工具栏「大图」按钮 / 右键「大图查看（灯箱）」/ 网格双击）
已全部撤掉，图库双击统一走系统默认看图软件 —— 与列表模式、实时抓图墙一致。
本类仍保留 on_decide=None 的纯浏览模式（幻灯片、四向翻页），
供审核页之外的纯看图场景直接复用。

═══════════════════════════════════════════════════════════════════════
#46 起：图库双击**重新回到灯箱**（Web 与桌面端行为统一为灯箱）
═══════════════════════════════════════════════════════════════════════
用户新决定推翻了 #26 的「灯箱只留审核页」：图库里双击图片要开**灯箱**，
不是直接下载、不是新标签页裸图。所以：
  · main_window 重新接上 `open_lightbox`（网格/列表双击 + 右键菜单项）；
  · 灯箱补齐主单第 28~31 项能力：**缩放 / 全屏 / 下载原图 / 打开源网页**，
    翻页与 Esc 本来就有（键位语义那条铁律一个字没动，见 `_sync_nav_buttons`）。

⚠ 键位语义铁律（#56 D21/D22，**这条不许被 #46 破例**）：
「按钮文案随上下文走，行为恒为 step(±1)」。新增的缩放/全屏键一律用
**不含歧义的独立键**（+ / - / 0 / F），绝不复用 ← →，否则审核页灯箱里
按 ← 会被 zoom 抢走、变成「不点赞也不翻页」。
"""
from __future__ import annotations

import io
import re
import shutil
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from ..hotkeys import display as _disp, resolve as _hk_resolve
from ..models import ReviewDecision
from .theme import style as _tstyle      # P1-4：行内样式也要跟随皮肤
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox,
)
from PySide6.QtCore import QStandardPaths

# Qt 自带插件里**没有 AVIF**（只有 jpeg/webp/gif/bmp/tiff/svg…）。真机跑完
# 916 张图库：699 webp + 180 avif + 37 jpg —— 也就是说打包版下有 1/5 的图
# 走 QPixmap 直接 isNull()。以前这种图在灯箱里只显示一句「无法读取该图片」，
# 用户看上去就是「很多格式打不开」+「左右切换没反应（翻过去还是那句话）」。
# 所以：Qt 读不出来就无条件退回 Pillow（PIL 12 的 webp/avif/jpg/gif/bmp
# 全能读），不再按下扩展名猜——站点给的扩展名经常跟真实格式对不上。


def _pixmap_via_pillow(path: Path):
    """Pillow 解码 → PNG 字节 → QPixmap（Qt 插件缺格式时的唯一退路）。"""
    try:
        from PIL import Image, ImageOps
        with Image.open(str(path)) as im:
            im = ImageOps.exif_transpose(im)
            # 动图（gif/webp 动画）只取当前帧，灯箱不播动画
            try:
                im.seek(0)
            except Exception:
                pass
            if im.mode in ("P", "LA", "La", "I;16", "I;16B", "I;16L"):
                im = im.convert("RGBA") if "A" in (im.mode or "") else \
                    im.convert("RGB")
            elif im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG")
        pm = QPixmap()
        pm.loadFromData(buf.getvalue())
        return pm if not pm.isNull() else None
    except Exception:
        return None


class Lightbox(QDialog):
    """在图库当前结果里翻看大图。

    rows: list[dict]，需含 path / width / height / source_url / tags / score
    index: 起始位置
    """

    #: #46 缩放步进。上下限由 PreviewLabel 夹（MIN_ZOOM/MAX_ZOOM），
    #: 这里只管步长 —— 倍率是「量」，别把取值当契约钉死。
    _ZOOM_STEP = 1.25

    def __init__(self, rows, index=0, parent=None, on_decide=None,
                 hotkeys=None):
        super().__init__(parent)
        # F19：键位覆盖表（cfg.hotkeys；None/空 = 全默认，行为与改前逐字节一致）。
        # 判定规则在 hotkeys.py / hotkeys_qt.py，本类只翻译不重写。
        self._hotkeys = dict(hotkeys) if isinstance(hotkeys, dict) else {}
        self.rows = [r for r in (rows or []) if r.get("path")]
        self.i = max(0, min(index, len(self.rows) - 1)) if self.rows else 0
        # 审核页传入 on_decide(decision, row)：在灯箱里按 ↑/↓ 直接「喜欢/不喜欢」
        self.on_decide = on_decide
        self.setWindowTitle("大图查看")
        # QDialog 默认只有关闭按钮 → 显式补上 最小化 / 最大化 按钮，
        # 否则放大后没法最小化、也没法还原
        # 关键：必须作为真正的顶级窗口（不挂父窗口），否则在 Windows 上
        # 会被当成"子对话框"而强制隐藏最小化按钮（只能最大化、无法最小化）。
        # 显式给齐 Window + 标题栏 + 系统菜单 + 最小化/最大化/关闭 全套标志，
        # 这样灯箱有自己独立的标题栏，三个按钮都可用。
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowTitleHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint)
        self.resize(920, 720)
        v = QVBoxLayout(self)
        # r32：用「跟着当前尺寸重画」的预览控件，而不是直接 setPixmap。
        # 以前直接 setPixmap(scaled(…))：QLabel 的 sizeHint 被图片撑大 →
        # 窗口最小尺寸跟着图片走 → 想缩窗口缩不下去、越拖越怪（用户报的 BUG）。
        # PreviewLabel 的 sizeHint 固定，窗口能随你怎么拖。
        from .table_view import PreviewLabel
        self.img = PreviewLabel("（没有可显示的图片）", self)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.img, 1)
        self.meta = QLabel("")
        self.meta.setWordWrap(True)
        self.meta.setAlignment(Qt.AlignmentFlag.AlignLeft)
        _tstyle(self.meta, "color:{text_dim};font-size:12px;")
        v.addWidget(self.meta)
        row = QHBoxLayout()
        # 键位提示按上下文切换的**唯一实现在 _sync_nav_buttons()**（它每次翻页都会
        # 重设文案）。审核页灯箱里 → 是「喜欢」（R153 起 ← 也是翻页），
        # 按钮若印「下一张 (→)」就与真实键位矛盾，用户照着按钮提示按 →
        # 会得到一次「喜欢」而不是翻页。
        # 按钮行为始终只做翻页（step），只改文案。
        b_prev = QPushButton("上一张 (←)")
        b_next = QPushButton("下一张 (→)")
        b_close = QPushButton("关闭 (Esc)")
        b_prev.clicked.connect(lambda: self.step(-1))
        b_next.clicked.connect(lambda: self.step(1))
        b_close.clicked.connect(self.close)
        self.b_prev, self.b_next = b_prev, b_next
        row.addWidget(b_prev)
        row.addWidget(b_next)
        # (a) 幻灯片自动播放：放灯箱，自定义键或点按钮开关，默认 3 秒翻一张。
        #     F23 补齐：间隔 2/4/8 秒按钮循环切换；「循环」开关决定播完
        #     到底是绕回第一张还是自动停；固定别名 P 键与自定义键同效。
        self._slideshow_timer = QTimer(self)
        self._slideshow_timer.setInterval(3000)
        self._slideshow_timer.setSingleShot(False)
        self._slideshow_timer.timeout.connect(self._slideshow_tick)
        # F23：循环开关。**默认关 = 播到最后一张自动停**——
        # 与 Web 端 lbStep「不是循环，到底就停住」的口径一致，
        # 别把用户甩回第一张（想循环的人自己去开）。
        self._slideshow_loop = False
        b_slideshow = QPushButton("幻灯片 (空格/P)")
        b_slideshow.setCheckable(True)
        b_slideshow.setToolTip("自动播放：按间隔切换至下一张；使用自定义键或 P 或再次触发可暂停/停止")
        b_slideshow.clicked.connect(self.toggle_slideshow)
        self.b_slideshow = b_slideshow
        row.addWidget(b_slideshow)
        # F23：间隔按钮（2/4/8 秒循环切换，文案随当前值变）。
        # 默认仍是 3000ms（旧行为不变）；第一次点击进入 2→4→8 的循环。
        b_speed = QPushButton(f"间隔 {self._slideshow_timer.interval() // 1000}秒")
        b_speed.setToolTip("自动播放间隔：2 → 4 → 8 秒循环切换")
        b_speed.clicked.connect(self.cycle_slideshow_speed)
        self.b_speed = b_speed
        row.addWidget(b_speed)
        # F23：循环开关（开=到底绕回第一张继续播；关=到最后一张自动停）。
        b_loop = QPushButton("循环:关")
        b_loop.setCheckable(True)
        b_loop.setToolTip("开=播到底绕回第一张继续；关=播到最后一张自动停")
        b_loop.toggled.connect(self._set_slideshow_loop)
        self.b_loop = b_loop
        row.addWidget(b_loop)
        # ---- #46：缩放 / 全屏 / 下载原图 / 打开源网页（主单 28~31 项）----
        # 键位一律用独立键（默认 + - 0 F R，可自定义，见 hotkeys.py），
        # **绝不复用 ← →**（见文件头键位铁律）。
        self._zoom = 1.0
        self._pre_fullscreen = None      # 记下进全屏前的窗口状态，退出时还原
        b_zoom_out = QPushButton("缩小 (-)")
        b_zoom_out.setToolTip("缩小；再按一次或窗口变大就自适应")
        b_zoom_out.clicked.connect(self.zoom_out)
        b_zoom_in = QPushButton("放大 (+)")
        b_zoom_in.setToolTip("放大当前图片（用于看细节）")
        b_zoom_in.clicked.connect(self.zoom_in)
        b_zoom_rst = QPushButton("适应窗口 (0)")
        b_zoom_rst.setToolTip("缩放复位：整张图重新按窗口大小自适应")
        b_zoom_rst.clicked.connect(self.zoom_reset)
        # F19：存引用 —— _sync_key_labels 要按当前生效键位重设这些按钮文案
        self.b_zoom_out, self.b_zoom_in, self.b_zoom_rst = (
            b_zoom_out, b_zoom_in, b_zoom_rst)
        b_fs = QPushButton("全屏 (F)")
        b_fs.setCheckable(True)
        b_fs.setToolTip("全屏看图；再按一次或 Esc 退出全屏")
        b_fs.clicked.connect(self.toggle_fullscreen)
        self.b_fs = b_fs
        b_dl = QPushButton("下载原图")
        b_dl.setToolTip("把这张原图复制一份到「下载」文件夹")
        b_dl.clicked.connect(self.download_original)
        b_src = QPushButton("打开源网页")
        b_src.setToolTip("用默认浏览器打开这张图所在的来源网页")
        b_src.clicked.connect(self.open_source_page)
        # R148：旋转（每点一次 +90°，4 次回正）；默认快捷键 R（不与 ←→/Esc/F 冲突）。
        # 只改显示、绝不写原文件；翻页自动复位（见 render）。
        b_rot = QPushButton("旋转 (R)")
        b_rot.setToolTip("顺时针旋转 90°；只改显示不写原文件；翻页自动复位")
        b_rot.clicked.connect(self.rotate)
        self.b_rot = b_rot
        for b in (b_zoom_out, b_zoom_in, b_zoom_rst, b_fs, b_dl, b_src, b_rot):
            row.addWidget(b)
        self.b_dl, self.b_src = b_dl, b_src
        row.addStretch(1)
        row.addWidget(b_close)
        v.addLayout(row)
        # r37：只有 1 张可翻时把按钮置灰（不禁用的话按了没反应，
        # 用户又会报「左右切换不能用」）。
        self._sync_nav_buttons()
        self._sync_key_labels()
        # #56 翻卡片：灯箱按键语义随上下文不同——
        #   · 审核页灯箱（on_decide 非空）：→ 喜欢，← / ↑↓ 都只翻页
        #     （R153：← 不再判「不喜欢」，恢复上一张翻页）；
        #   · 图库灯箱（无审核上下文）：四向都翻页，保持旧行为
        #     （D21：图库不翻卡片，只做审核页）。
        # F19：键位可自定义 ⇒ 这行提示按**当前生效键位**现场拼
        # （印着 → 实际绑了 L 的提示比没有提示更糟）。
        self._hint = QLabel("")
        _tstyle(self._hint, "color:{text_dim};font-size:12px;")
        v.addWidget(self._hint)
        # _sync_key_labels 里含提示行，而 _hint 到这才建好 ⇒ 再刷一次
        self._sync_key_labels()
        self.render()

    def showEvent(self, ev):
        # 让对话框本身拿到键盘焦点，否则箭头键会被默认聚焦的按钮吃掉
        super().showEvent(ev)
        self.setFocus()
        # r37：灯箱是独立顶级窗口（不挂主窗口），从主窗口弹出来时 Windows
        # 不一定把键盘焦点给它 —— 焦点还在主窗口的话 ← → 根本进不到这里，
        # 表现就是「灯箱打开了、左右键完全没反应」。显式抬起来 + 抢焦点。
        try:
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

    def _hk(self, action_id: str) -> str:
        """某个动作的生效键位（默认键位在 hotkeys.py 注册表，覆盖在 cfg.hotkeys）。"""
        return _hk_resolve(self._hotkeys, action_id)

    def _sync_nav_buttons(self):
        n = len(getattr(self, "rows", None) or [])
        for b in (getattr(self, "b_prev", None), getattr(self, "b_next", None)):
            if b is None:
                continue
            b.setEnabled(n > 1)
            # ⚠️ 这里每次翻页都会重设文案，所以键位提示必须**在这里**按上下文算，
            # 只在__init__ 设一次会被这里悄悄改回「上一张 (←)」。
            # 审核页灯箱（R153）：→ = 喜欢，← / ↑ = 上一张，↓ = 下一张。
            # F19：键位可自定义 ⇒ 文案按**当前生效键位**拼（印着 → 实际绑了 L
            # 的按钮比没有提示更糟——本仓 R23 同类教训）。
            decide_ui = getattr(self, "on_decide", None) is not None
            if b is self.b_prev:
                b.setText("上一张 (%s)" % _disp(self._hk(
                    "rlight.prev" if decide_ui else "glight.prev")))
            else:
                b.setText("下一张 (%s)" % _disp(self._hk(
                    "rlight.next" if decide_ui else "glight.next")))

    def _sync_key_labels(self):
        """F19：把带键位的按钮文案 / 提示行按当前生效键位重拼（默认键位时
        与改前逐字节一致）。翻页无关的静态标签在这里一次性设好。"""
        ss = _disp(self._hk("light.slideshow"))
        if getattr(self, "b_slideshow", None) is not None:
            self.b_slideshow.setText("幻灯片 (%s/P)" % ss)
        for btn, aid, fmt in (
                (getattr(self, "b_zoom_out", None), "light.zoom_out", "缩小 (%s)"),
                (getattr(self, "b_zoom_in", None), "light.zoom_in", "放大 (%s)"),
                (getattr(self, "b_zoom_rst", None), "light.zoom_reset", "适应窗口 (%s)"),
                (getattr(self, "b_fs", None), "light.fullscreen", "全屏 (%s)"),
                (getattr(self, "b_rot", None), "light.rotate", "旋转 (%s)")):
            if btn is not None:
                btn.setText(fmt % _disp(self._hk(aid)))
        if getattr(self, "_hint", None) is None:
            return
        if getattr(self, "on_decide", None) is not None:
            self._hint.setText(
                "%s 喜欢 · %s 上一张 · ↑↓ 翻页 · %s/P 幻灯片 · Esc 关闭"
                " · %s/%s/%s 缩放 · %s 全屏 · %s 旋转" % (
                    _disp(self._hk("rlight.like")), _disp(self._hk("rlight.prev")),
                    ss, _disp(self._hk("light.zoom_in")),
                    _disp(self._hk("light.zoom_out")), _disp(self._hk("light.zoom_reset")),
                    _disp(self._hk("light.fullscreen")), _disp(self._hk("light.rotate"))))
        else:
            self._hint.setText(
                "%s %s / ↑↓ 翻页 · %s/P 幻灯片 · Esc 关闭"
                " · %s/%s/%s 缩放 · %s 全屏 · %s 旋转" % (
                    _disp(self._hk("glight.prev")), _disp(self._hk("glight.next")),
                    ss, _disp(self._hk("light.zoom_in")),
                    _disp(self._hk("light.zoom_out")), _disp(self._hk("light.zoom_reset")),
                    _disp(self._hk("light.fullscreen")), _disp(self._hk("light.rotate"))))

    def keyPressEvent(self, ev):
        k = ev.key()
    def keyPressEvent(self, ev):
        from .hotkeys_qt import event_matches
        k = int(ev.key())
        # ---- F19：可自定义动作按**注册表顺序**逐个匹配，命中即 return ----
        # （顺序即优先级：同键绑两个动作时排前面的生效，与 hotkeys.warnings_for
        #   的文案口径一致；默认键位下与改前逐字节同行为）
        # 灯箱（通用）：幻灯片 / 缩放 / 全屏 / 旋转。
        # 幻灯片另带固定别名 P（不参与自定义，见 hotkeys.FIXED_ALIASES）。
        if (event_matches(ev, self._hk("light.slideshow"))
                or k == int(Qt.Key.Key_P)):
            self.toggle_slideshow()
            ev.accept()
            return
        # ---- #46 缩放 / 全屏：**独立键位**，绝不碰 ← →（见文件头铁律）----
        # 「=」是放大的固定别名（US 键盘 + 与 = 同键，不带 Shift 按出来的是
        # Key_Equal）——改前就认，保留：撤了就是行为回归。
        if (event_matches(ev, self._hk("light.zoom_in"))
                or k in (int(Qt.Key.Key_Plus), int(Qt.Key.Key_Equal))):
            self.zoom_in()
            ev.accept()
            return
        if event_matches(ev, self._hk("light.zoom_out")):
            self.zoom_out()
            ev.accept()
            return
        if event_matches(ev, self._hk("light.zoom_reset")):
            self.zoom_reset()
            ev.accept()
            return
        if event_matches(ev, self._hk("light.fullscreen")):
            self.toggle_fullscreen()
            ev.accept()
            return
        if event_matches(ev, self._hk("light.rotate")):
            self.rotate()
            ev.accept()
            return
        if k == int(Qt.Key.Key_Escape):
            # 全屏态下 Esc 先退全屏，再按才关窗 —— 否则用户「Esc 想退出全屏」
            # 结果整个灯箱没了，下次进来又是全屏，体验上像 bug。
            # Esc 是固定别名，**不参与自定义**（灯箱关了可就什么都没有了，
            # 把关闭键改没了用户连设置都回不来）。
            if self.is_fullscreen():
                self.toggle_fullscreen(False)
                ev.accept()
                return
            self.close()
            ev.accept()
            return
        decide_ctx = (self.on_decide is not None) and bool(self.rows)
        if decide_ctx:
            # 审核页灯箱（R153 起）：→ 喜欢；← 与 ↑↓ 一样只翻页。
            # 「不喜欢」已从灯箱撤掉（网格/列表页入口保持原样）——
            # 防「照着按钮/提示按出误判」：← 恢复为上一张翻页。
            if event_matches(ev, self._hk("rlight.like")):
                self._decide_and_advance(ReviewDecision.LIKE)
                ev.accept()
                return
            if event_matches(ev, self._hk("rlight.prev")):
                self.step(-1)
                ev.accept()
                return
            if event_matches(ev, self._hk("rlight.next")):
                self.step(1)
                ev.accept()
                return
            # ↑↓ 是翻页固定别名（改前就在用，保留）
            if k in (int(Qt.Key.Key_Up), int(Qt.Key.Key_Down)):
                self.step(-1 if k == int(Qt.Key.Key_Up) else 1)
                ev.accept()
                return
            super().keyPressEvent(ev)
            return
        # 图库灯箱（无审核上下文）：← → 是默认绑定，↑↓ 是固定别名，
        # 四向都翻页，保持旧行为（D21 图库不翻卡片）。
        if event_matches(ev, self._hk("glight.prev")):
            self.step(-1)
            ev.accept()
            return
        if event_matches(ev, self._hk("glight.next")):
            self.step(1)
            ev.accept()
            return
        if k in (int(Qt.Key.Key_Up), int(Qt.Key.Key_Down)):
            self.step(-1 if k == int(Qt.Key.Key_Up) else 1)
            ev.accept()
            return
        super().keyPressEvent(ev)

    # ---------------------------------------------------------------- #46 缩放
    def current_row(self) -> dict:
        """当前这张图的行数据；没有图时返回 {}。"""
        rows = getattr(self, "rows", None) or []
        i = getattr(self, "i", 0)
        if not rows or i < 0 or i >= len(rows):
            return {}
        return rows[i] or {}

    def zoom(self) -> float:
        return float(getattr(self, "_zoom", 1.0) or 1.0)

    def _apply_zoom(self, z: float) -> float:
        """把倍率套到预览控件上，并**回写**到 self._zoom。

        控件自己会夹在 [MIN_ZOOM, MAX_ZOOM]，所以这里回写的是**它实际的**
        值 —— 断言「连点放大 100 次仍是 MAX_ZOOM」才成立。
        """
        img = getattr(self, "img", None)
        if img is None:
            return self.zoom()
        z = float(img.set_zoom(z))       # PreviewLabel.set_zoom 会返回生效值
        self._zoom = z
        return z

    def zoom_in(self) -> float:
        return self._apply_zoom(self.zoom() * self._ZOOM_STEP)

    def zoom_out(self) -> float:
        return self._apply_zoom(self.zoom() / self._ZOOM_STEP)

    def zoom_reset(self) -> float:
        return self._apply_zoom(1.0)

    # ------------------------------------------------------------- #46/R148 旋转
    def rotate(self):
        """顺时针旋转当前图 90°（R148）：只改显示、不写原文件。

        角度存在 PreviewLabel 里、随"下一张"复位（render 里 set_rotation(0)），
        所以翻页不会把上一张的角度带过来；缩放以当前显示为基准，互不破坏。
        """
        img = getattr(self, "img", None)
        if img is not None:
            img.rotate_cw()

    # ------------------------------------------------------------- #46 全屏
    def is_fullscreen(self) -> bool:
        """是否处于全屏。offscreen 下几何不可信（R58），但 windowState 的
        WindowFullScreen 标志是 Qt 自己的状态位，可信。"""
        return bool(self.windowState() & Qt.WindowState.WindowFullScreen)

    def toggle_fullscreen(self, force=None):
        """切全屏。force=None 时取反；返回最终是否全屏。"""
        want = (not self.is_fullscreen()) if force is None else bool(force)
        if want == self.is_fullscreen():
            self._sync_fullscreen_button()
            return self.is_fullscreen()
        try:
            if want:
                # 记下进全屏前的状态，退出时原样还原（否则窗口大小/位置丢了）
                self._pre_fullscreen = (self.windowState(),
                                        self.geometry())
                self.showFullScreen()
            else:
                self.showNormal()
                pre = getattr(self, "_pre_fullscreen", None)
                if pre:
                    st, geo = pre
                    try:
                        if st:
                            self.setWindowState(st)
                        self.setGeometry(geo)
                    except Exception:
                        pass
                self._pre_fullscreen = None
        except Exception:
            pass
        self._sync_fullscreen_button()
        return self.is_fullscreen()

    def _sync_fullscreen_button(self):
        b = getattr(self, "b_fs", None)
        if b is not None:
            b.setChecked(self.is_fullscreen())

    # ------------------------------------------------------ #46 下载原图 / 源页
    def download_original(self):
        """把当前这张原图复制一份到系统「下载」文件夹。

        为什么是「复制」而不是「打开所在文件夹」：灯箱里用户按这一下
        的意图是**把图存下来**。复制成功给中文提示、失败也给中文提示
        （作业单铁律：所有用户可见错误必须中文）。
        """
        r = self.current_row()
        p = Path(r.get("path") or "")
        if not str(p):
            QMessageBox.information(self, "提示", "这张图没有本地文件")
            return ""
        if not p.exists():
            QMessageBox.information(self, "提示", "文件已不在磁盘")
            return ""
        try:
            dl = Path(QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation))
            if not str(dl):
                dl = Path.home() / "Downloads"
            dl.mkdir(parents=True, exist_ok=True)
            # 同名文件不要覆盖用户已有文件（收藏集里重名极常见）
            dest = dl / p.name
            n = 1
            while dest.exists():
                dest = dl / f"{p.stem}_{n}{p.suffix}"
                n += 1
            shutil.copy2(str(p), str(dest))
            QMessageBox.information(self, "已保存", f"已保存到\n{dest}")
            return str(dest)
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"保存失败：{e}")
            return ""

    def open_source_page(self):
        """用默认浏览器打开这张图的来源网页（对齐 Web 端第 30 项）。"""
        r = self.current_row()
        u = str(r.get("source_url") or "").strip()
        if not u:
            QMessageBox.information(self, "提示", "这张图没有记录来源网址")
            return ""
        try:
            webbrowser.open(u)
        except Exception as e:
            QMessageBox.warning(self, "打不开", f"打开失败：{e}")
        return u

    # ---------------------------------------------------------------- 幻灯片
    def _slideshow_tick(self):
        """定时器到点：翻到下一张。

        F23 分两种口径（由「循环」开关决定）：
          · 循环开：到底绕回第一张继续（旧行为）；
          · 循环关：**已在最后一张 → 自动停**，不再把用户甩回第一张
            （与 Web 端 lbStep「到底就停住」同口径）。
        """
        if not self.rows:
            self._slideshow_timer.stop()
            self.b_slideshow.setChecked(False)
            return
        if (not self._slideshow_loop) and self.i >= len(self.rows) - 1:
            # 到尾停：把按钮勾选撤了、定时器停掉，文案回到「停止」态。
            self.toggle_slideshow(False)
            return
        self.step(1)

    def toggle_slideshow(self, force=None):
        """开/关自动播放。force=None 时自行取反；返回最终是否在播。"""
        on = (not self._slideshow_timer.isActive()) if force is None else bool(force)
        if on and not self.rows:
            on = False
        if on:
            self._slideshow_timer.start()
        else:
            self._slideshow_timer.stop()
        # 不论以何种方式触发（空格 / 点按钮），都以定时器真实状态为准对齐勾选
        self.b_slideshow.setChecked(self._slideshow_timer.isActive())
        return self._slideshow_timer.isActive()

    def cycle_slideshow_speed(self):
        """F23：间隔 2 → 4 → 8 秒循环切换，按钮文案跟着变。

        初值 3000ms 不在 2/4/8 里 —— 第一次点从 2 秒起（切完即为可选值）。
        """
        seq = (2000, 4000, 8000)
        cur = self._slideshow_timer.interval()
        try:
            nxt = seq[(seq.index(cur) + 1) % len(seq)]
        except ValueError:
            nxt = seq[0]
        self.set_slideshow_interval(nxt)
        self.b_speed.setText(f"间隔 {nxt // 1000}秒")
        return nxt

    def _set_slideshow_loop(self, on: bool):
        """F23：循环开关。开着播的过程中切换也即时生效。"""
        self._slideshow_loop = bool(on)

    def set_slideshow_interval(self, ms):
        """设自动播放间隔（毫秒），至少 200ms，避免手滑设成 0 把界面卡死。"""
        self._slideshow_timer.setInterval(max(200, int(ms)))

    def is_slideshow_running(self):
        return self._slideshow_timer.isActive()

    def _decide_and_advance(self, decision):
        """审核页灯箱：应用决定 + 学偏好，再翻到下一张。

        R153 后灯箱只剩 → 喜欢一条判定入口（dislike 改由网格/列表页负责），
        dislike 分支保留是防御性的（on_decide 回调仍按 decision 分流）。
        """
        try:
            self.on_decide(decision, self.rows[self.i])
        except Exception:
            pass
        self.step(1)

    def closeEvent(self, ev):
        # 关灯箱时停掉幻灯片定时器，避免对象销毁后仍触发（虽被父销毁，停一下更稳）。
        try:
            self._slideshow_timer.stop()
        except Exception:
            pass
        # #46：全屏态下直接关窗会把「全屏」这个状态漏给主窗口/任务栏 ——
        # 用户关掉灯箱后桌面还停在全屏模式，看着像程序坏了。退出前先还原。
        try:
            if self.is_fullscreen():
                self.showNormal()
        except Exception:
            pass
        super().closeEvent(ev)

    # 不需要自己重写 resizeEvent：img 是 PreviewLabel，它自己按当前尺寸重画。

    def step(self, d: int):
        if not self.rows:
            return
        self.i = (self.i + d) % len(self.rows)
        self._sync_nav_buttons()
        self.render()

    def render(self):
        # #46：换图必须把缩放复位 —— 否则上一张放大到 4×，下一张一打开
        # 就是「4 倍裁切」，用户以为图坏了（这是实测里最容易挨骂的一点）。
        self._apply_zoom(1.0)
        if not self.rows:
            self.img.clear_source("没有可显示的图片")
            self.meta.setText("")
            return
        r = self.rows[self.i]
        p = Path(r.get("path") or "")
        n = len(self.rows)
        self.setWindowTitle(f"大图查看  {self.i + 1}/{n}")
        if p.exists():
            pm = QPixmap(str(p))
            if pm.isNull():
                # Qt 插件缺这个解码器（avif 最常见）→ Pillow 兜底
                pm = _pixmap_via_pillow(p) if p.exists() else None
            if pm is not None and not pm.isNull():
                # 只存原图，缩放交给 PreviewLabel 按窗口当前大小自己来
                self.img.setSource(pm)
                self.img.set_rotation(0)      # R148：翻页角度复位（重置回 0°）
            else:
                self.img.clear_source(
                    f"无法预览该格式（{p.suffix.lower() or '未知'}）"
                    "\n可在设置里打开「保存时转成 JPG」下次采集就都能看")
        else:
            self.img.clear_source("文件已缺失")
        # r40：灯箱里的标签按界面语言显示（中文），原始英文仍留在库里
        try:
            from ..i18n import translate_tags as _tt
            _raw = str(r.get("tags") or "")
            _shown = ", ".join(_tt([t for t in re.split(r"[,\s]+", _raw) if t]))
        except Exception:
            _shown = _raw
        self.meta.setText(
            f"{p.name}\n"
            f"第 {self.i + 1}/{n} 张 · "
            f"分辨率 {r.get('width')}x{r.get('height')} · "
            f"score={float(r.get('score') or 0):.2f}\n"
            f"来源 {r.get('source_url') or '-'}\n"
            f"标签 {_shown or '-'}")
