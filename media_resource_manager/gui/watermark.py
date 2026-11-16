"""水印控件（需求 1 / 23）：Sweet 图放在右下角，压在文字底下。

用户要求：
- 取消左上角的 logo，那个 Sweet 图改成**右下角日志框/统计框的水印**；
- 水印要**压在文字底下**，透明度我定（在 8%~12% 区间取 10%）。

**两个真问题的修法**（前一版「代码里有、界面上看不见」）：

1. **图源取错**：早先用 `QPixmap(str(ico))`，而 `.ico` 是多帧容器 ——
   它只加载内部最小的 16x16 帧，放大到水印尺寸后糊成一团，
   alpha 10% 下等于没画。正确做法是按内嵌尺寸取最大的那一帧。
2. **水印控件铺满整窗**（2026-10-06 台式机真机 bug）：
   前一版把水印控件 `setGeometry(0,0,host.w,host.h)` 铺满父容器，只在
   paintEvent 里把 logo 画在右下角 —— 控件本身占满整窗，等于一个**覆盖整窗
   的浮层**。真机上它会干扰兄弟控件的重绘（用户反馈："所有按钮/菜单/底部
   统计文字只有鼠标悬停才显示"、"顶部菜单有淡色重影"），右下角也容易被误画成
   巨大 logo。**现在水印控件只占右下角那一小块**（≤260px + 12px 边距），
   物理上不可能盖到别的控件，整窗浮层的重绘副作用随之消失。

压文字底下靠 z 序 —— ⚠ **这一条在 B2（真机 2026-10-08）被推翻**，见 `attach()`。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QEvent, QPoint, QRect
from PySide6.QtGui import QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QScrollArea, QSplitter, QStackedWidget, QWidget)

# 用户给的 logo 优先（PNG，缩放不糊）；找不到才退回软件自带的 icon.ico。
USER_LOGO_NAMES = ("logo.png", "sweet_logo.png", "watermark.png")

# 水印不透明度。用户定的：**不透明**（早期试过 10%，实测右下角最大色差只有
# 13 级 —— 浅青蓝 logo (96,192,224) 压在近白底上，10% 等于没画；后来 28%
# (72/255) 实测色差 35，真机 PrintWindow 取证指纹 3988 px，可见了，
# 但用户反馈仍然不算看得清）→ 所以现在直接给满 255。
#
# ⚠ 2026-10-05（#42 开工）：用户用试色 demo 试出 **ALPHA=178（70%）** 是他最舒服的
#   值（原话「数值设置 178，也就是 70% 是最舒服的」）→ 下面一行已改成 178。
#   改前是 255（100% 不透明）：当时是「宁可太重也必须看得见」，后来才试出 178 更舒服。
#   界面上「设置 → 水印不透明度」有临时滑条可以就地调（当前值存 cfg.watermark_alpha）。
#
# 这个 logo 天生对比度低：底色 255、图色 96，差值只有 159，
# 合成色差 ≈ (255-96) × alpha/255，所以 alpha 给多大就差多少，没有"刚刚好"的
# 中间值能同时满足"淡"和"看得见"—— 这就是当初 10% 怎么都看不见的原因。
ALPHA = 255            # R233：水印挪到最左列后不挡任何内容，用户要实心不透明（原 178/70%）

# 水印四边留白（px）。用户反馈「水印怎么被裁剪了，完整放上去别裁」——
# 查下来 app 里其实没裁（真机 PrintWindow 整窗 1382x899 实测水印在
# x 922..1140 / y 758..806，右距边 242px、下距底 93px），是**我交付的那张
# 对比图自己裁的**（crop 底边 y=760，正好切在水印 758..806 的中间）。
# 不过这条规矩不能靠"看着没裁"，所以给死一个硬下限：四边至少留这么多 px，
# 数字上就永远不可能贴边被切。
INSET = 12

# 水印小方框的边长上限（px）。用户定「约 256px」，这里取 260 与历史一致。
BOX_MAX = 260

# B1：算「可视内容底边」时必须**跳过**的布局骨架类。
# 它们贴底是布局决定的，不是「底部状态栏/底栏」—— 实测页0 里一个
# QSplitter(y=49,h=791) 会被误判成底栏，把水印压成 25x25。
# 只认 QLabel/QTextEdit/QListWidget 这类真正承载内容的东西。
_STRUCTURAL = (QSplitter, QScrollArea, QStackedWidget)
# 用户 10-07：水印相对角落基准的额外偏移。
#
# ⚠ 2026-10-09（R48 / 作业单 A1）：水印从**右下角改到左下角**（贴左 + 贴底，
#   INSET 留白不变）。原来右下角时 OFFSET_X 是「在右边界基础上再向左 50」，
#   那个方向**只对右边界有意义** —— 换成贴左后若照抄 -50，x = INSET-50 = -38
#   会跑到窗口外左侧、直接看不见（这正是 #42 那次「水印怎么不见了」的同型坑）。
#   所以贴左时水平偏移必须归零：x = INSET，INSET 就是那条「不许贴边」的硬下限。
OFFSET_X = 0     # 贴左：不再有「向右/向左」的额外偏移，INSET 12 已是留白
OFFSET_Y = -50   # 向上 50px（沿用 10-07 用户实测调校值，见下方锚点统一后的实测）

# R176：水印「继续往左移」+ 锚到左侧导航（任务/审核/图库/隔离区）正下方。
#   左边留白单独一个常量：INSET 那条是「不许贴边」的硬下限，不能为了
#   「再往左一点」去动它 —— 一动就把贴底那条底线的语义也改了（R48 同型坑）。
LEFT_INSET = 4
# 顶边与导航底边的间距。
BELOW_GAP = 12


def _load_png() -> QPixmap | None:
    """找用户提供的 PNG logo（走 resources.logo_path()）。"""
    try:
        from ..resources import logo_path
        p = logo_path()
    except Exception:
        p = None
    if p is None:
        return None
    try:
        pm = QPixmap(str(p))
        return None if pm.isNull() else pm
    except Exception:
        return None


def _load_ico_best() -> QPixmap | None:
    """从 .ico 取最大的内嵌帧（QPixmap 只会拿最小的 16x16）。"""
    try:
        from ..resources import icon_path
        p = Path(icon_path())
        if not p.exists():
            return None
        from PIL import Image
        with Image.open(p) as im:
            im = im.convert("RGBA")
            if im.width < 128:                    # 帧太小，放大必糊
                im = im.resize((256, 256), Image.LANCZOS)
            data = im.tobytes("raw", "RGBA")
            img = QImage(data, im.width, im.height, im.width * 4,
                         QImage.Format.Format_RGBA8888).copy()
        return None if img.isNull() else QPixmap.fromImage(img)
    except Exception:
        return None


def watermark_pixmap() -> QPixmap | None:
    """水印原图：优先用户 PNG，其次 ico 最大帧，都没有返回 None。

    R175（2026-10-20 用户口述）：取到图后**顺时针旋转 90°**，横向 logo
    变成贴左边缘的竖条；旋转后立即把画布**裁到内容 bbox**——
    ⚠ 这一步不能省：logo 是 241x84 的横向图形装在 256x256 方画布里，
    只旋转不裁剪的话画布仍是方的，下游 contain 缩放会把竖条内容
    缩到几乎看不见（84px 高的内容被缩进 260px 方框后只剩 ~90px 宽）。
    裁剪后画布尺寸 = 内容尺寸（84 宽 x 241 高），contain 与方框分支
    都能按真实内容比例工作。加载期做一次，paintEvent 不变。
    """
    src = _load_png() or _load_ico_best()
    if src is None:
        return None
    return _rotate_vertical(src)


def _rotate_vertical(pm: QPixmap) -> QPixmap:
    """R175：顺时针转 90° 成竖条，并把画布裁到非透明内容的最小包围盒。"""
    if pm is None or pm.isNull():
        return pm
    img = pm.toImage()
    if img.isNull():
        return pm
    # 先裁内容 bbox（去掉四周透明边），再旋转 —— 顺序无所谓，都做才对。
    left, top = img.width(), img.height()
    right = bottom = -1
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 8:
                if x < left:
                    left = x
                if x > right:
                    right = x
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
    if right >= 0 and (right - left + 1 < img.width()
                       or bottom - top + 1 < img.height()):
        img = img.copy(left, top, right - left + 1, bottom - top + 1)
    # 顺时针 90°：横向 logo → 竖条（顶部在右、底部在左，读的方向不变）
    tr = QTransform().rotate(90)
    out = img.transformed(tr, Qt.TransformationMode.SmoothTransformation)
    if out.isNull():
        return pm
    return QPixmap.fromImage(out)


class WatermarkOverlay(QWidget):
    """只占 host 右下角一小块的水印浮层。

    尺寸靠**自己覆写 resizeEvent/showEvent + eventFilter 跟父容器**，
    不能只在建页时 setGeometry 一次 —— 后者一改尺寸就对不上，
    于是「代码里有、界面上看不见」。
    """

    def __init__(self, alpha: int = ALPHA, parent: QWidget | None = None,
                 anchor: QWidget | None = None,
                 below=None):
        super().__init__(parent)
        self._alpha = int(max(0, min(255, alpha)))
        self._pm = watermark_pixmap()
        self._tight = None      # 内容 bbox 缓存，见 _tight_rect
        # R48-A2：三页水印的**共同参照系**（默认退回 parent）。
        # MainWindow 显式把 stack 传给三处水印 —— 这是「三页同一位置」的根：
        # 各页自己算 _content_bottom() 会随当前页跳动（实测 848/812/791）。
        self._anchor = anchor
        # R176：可选「挂在它正下方」的控件（左侧导航最后一项＝隔离区）。
        #   允许传 callable —— 平铺/分组两套导航按皮肤二选一，写死一个
        #   会在换布局后锚到**隐藏**那套上（隐藏控件 mapTo 出来的是旧坐标）。
        self._below = below
        self._filtered_refs: list = []   # 已装过 eventFilter 的参照系（R48）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.installEventFilter(self)

    # ---- 浮层：脱离布局，只占右下角一块小矩形 ----
    # 踩过两次弯路，都记在这：
    # ① 装进布局（insertWidget(0, stretch=1)）：水印变成"占位控件"，
    #    被挤到内容下面、盖不到右下角 —— 布局管的是**位置**，不是**背景**。
    # ② 只靠 eventFilter 判 Resize/Show：QWidget.resize() 在布局尚未
    #    activate 时不给子控件发事件（实测 resize(640,400) 后仍是 800x260）。
    # 正解：既不进布局，也不只等事件 —— attach 时**主动装上父容器的
    # resize 事件处理器**，再重算一次几何。两条腿走路才稳。
    #
    # ⚠ 2026-10-06（台式机真机 bug 修复）：控件几何**只占右下角小方框**，
    #    不再铺满整个 stack。铺满＝覆盖整窗的浮层，真机会干扰兄弟控件重绘。
    def _host(self):
        return self.parentWidget()

    # ---- B1：按「真实可视内容矩形」算右下角，别按 host 全高 ----
    #
    # 真机 2026-10-08 反馈「水印整体向下偏移约 20px」。实测根因：`_target_box()`
    # 直接用 `host.height()` 当底边，但 host（stack / 各页）的**底边并不是用户
    # 眼里的「内容底边」** —— 页面自己还有一排底部操作条 + 一行状态文字
    # （实测 review 页底部 QLabel 在 y=833..860、gallery 页在 y=847..860，
    # 而 host 高 848）。拿 host 全高当基准，水印就压到了那排底栏**下面**，
    # 视觉上等于比正确位置低了「底栏高度」那一段。
    #
    # 修法：先算出 host 里**最低的可见内容控件**的顶边，把那当基准底边；
    # 一个都没有才退回 host.height()。这样"排除底部状态栏/底栏"是**实测**
    # 出来的，不是猜一个常数。
    def _ref(self):
        """几何计算的**参照系**：anchor（一般是 stack）优先，退回 host。

        R48-A2：三页水印必须落在「完全相同的 host 坐标」。做法不是让三页
        各自算各自的 `_content_bottom()`（实测随当前页在 848/812/791 之间
        跳，同一时刻三页位置就不同），而是**统一到 stack 这一个参照系**上算，
        再把结果映射回各自的 host 坐标 —— 只要 anchor 一样，算出来的位置
        就必然一样。

        ⚠ 这里**故意不检查 anchor 的尺寸**：`_build_ui` 阶段 attach() 跑在
        布局之前，那时 stack 还是 0x0，一旦「宽为 0 就退回 host」，就永远
        装不上 anchor 的 resize 转发 → 改窗口尺寸时另外两页不跟着重算
        （实测 resize 到 1100x700 后 stack 那只到了 y=327，另两只还停在 526）。
        参照系「暂时没尺寸」不是「参照系无效」，两者必须分开。
        """
        a = self._anchor
        if a is not None:
            try:
                a.isVisible()      # 只探活，不据此改变返回值
            except RuntimeError:
                return self._host()
            return a
        return self._host()

    def _below_widget(self):
        """R176：解析「挂在谁下方」—— 支持 callable（两套导航动态二选一）。"""
        b = self._below
        if callable(b):
            try:
                b = b()
            except Exception:
                return None
        return b

    def _place(self, ref, h: int, bh: int) -> tuple:
        """R176：算方框左上角（**ref 坐标**），返回 (x, y)。

        没传 `below`（或它不可见）⇒ 完全沿用 R48/R175 的贴左贴底，
        一个像素都不动（老护栏都靠这条活着，不许顺手改）。
        传了且可见 ⇒ 顶边 = 导航底边 + BELOW_GAP，左边 = LEFT_INSET。
        下方塞不下（连 6 成高度都不够）就退回贴底 —— 宁可没锚准，
        也不能把水印挤出可视区。
        """
        below = self._below_widget()
        if below is not None and ref is not None and h > 2 * INSET:
            try:
                if below.isVisible() and below.height() > 0:
                    top = below.mapTo(ref, QPoint(0, below.height())).y() + BELOW_GAP
                    if 0 <= top and top + bh <= h - INSET:
                        return LEFT_INSET, int(top)
                    if 0 <= top and (h - INSET - top) >= bh * 0.6:
                        return LEFT_INSET, int(h - INSET - bh)
            except (RuntimeError, TypeError):
                pass
        # R233：窄列（sidebar 导航栏，宽<300）贴到最底（OFFSET_Y 是 stack 内容区
        # 避底栏的上移调校值；导航列要贴到最底=隔离区按钮下方）。
        oy = 0 if (ref is not None and ref.width() < 300) else OFFSET_Y
        return INSET + OFFSET_X, h - bh - INSET + oy

    def _ensure_ref_filter(self):
        """保证 anchor 的 resize/子页变化能转发到 `_fit()`（幂等，可反复调）。

        必须在**每次** `_fit()` 里调，不能只在 `attach()` 调一次：attach 时
        布局还没跑，anchor 可能还没被识别成有效参照系（见 `_ref()` 的说明）。
        """
        ref = self._ref()
        host = self._host()
        if ref is None or ref is host or ref is self:
            return
        try:
            if ref not in self._filtered_refs:
                ref.installEventFilter(self)
                self._filtered_refs.append(ref)
        except (RuntimeError, TypeError):
            pass
        # R176：导航那一项自己也会 resize/换位置（换皮肤、改窗口高），
        # 不监听它的话水印会停在旧底边上。
        bl = self._below_widget()
        if bl is not None and bl is not host and bl is not self:
            try:
                if bl not in self._filtered_refs:
                    bl.installEventFilter(self)
                    self._filtered_refs.append(bl)
            except (RuntimeError, TypeError):
                pass

    # ---- B1：按「真实可视内容矩形」算角落，别按 host 全高 ----
    #
    # 真机 2026-10-08 反馈「水印整体向下偏移约 20px」。实测根因：`_target_box()`
    # 直接用 `host.height()` 当底边，但 host（stack / 各页）的**底边并不是用户
    # 眼里的「内容底边」** —— 页面自己还有一排底部操作条 + 一行状态文字
    # （实测 review 页底部 QLabel 在 y=833..860、gallery 页在 y=847..860，
    # 而 host 高 848）。拿 host 全高当基准，水印就压到了那排底栏**下面**，
    # 视觉上等于比正确位置低了「底栏高度」那一段。
    #
    # 修法：先算出 host 里**最低的可见内容控件**的顶边，把那当基准底边；
    # 一个都没有才退回 host.height()。这样"排除底部状态栏/底栏"是**实测**
    # 出来的，不是猜一个常数。
    def _content_bottom(self) -> int:
        """参照系坐标下的「可视内容底边」。找不到可见内容就退回参照系全高。

        ⚠ 这里必须同时卡五条，缺一条就会算错基准：
        ① `r.height() < h` —— 铺满整页的容器（stack 的各页就是这种）不是底栏，
           它 y=0，算进去会让 content_bottom 直接变 0、水印缩成 0x0；
        ② 宽度 ≥ 容器一半 —— 只要「横贯底部的一整条」，不要单个按钮
          （底栏上面那排按钮比状态文字高十几像素，取 min 会把基准抬得过高）；
        ③ **必须排除「结构型容器」** —— QSplitter / QScrollArea / QStackedWidget
           这类是布局骨架，本身就占满大片区域、贴底是**结构决定**的，不是
           「底栏」。实测页0 里一个 QSplitter rect=(12,49,1258,791) gap=8，
           取 min 会把基准压到 49 → 水印缩成 25x25（实测踩过）。
           只认 QLabel/QTextEdit/QListWidget 这类**真正的内容条**。
        ④ 底边离容器底 ≤ 40px —— 贴着底的那一层才算底栏。
        ⑤ **R48-A2 新增：只认「当前可见的那一页」**。
           遍历 stack 的全部后代时，不可见的兄弟页（review/gallery 未激活时）
           几何停在默认 640x480，会被误判成"底部薄条"把基准抬飞。
           这里额外卡一条：控件必须在**可见**的顶层页子树内。
        """
        ref = self._ref()
        if ref is None:
            return 0
        try:
            h, w = ref.height(), ref.width()
            lowest = h
            # ⑤ 当前可见的「顶层子页」集合：只有落在这些页里的控件才参与。
            #    直接子控件里，可见且不是水印自身的那些就是候选页。
            top_pages = []
            for ch in ref.children():
                if not isinstance(ch, QWidget) or ch is self:
                    continue
                try:
                    if ch.isVisible() and not ch.isWindow():
                        top_pages.append(ch)
                except RuntimeError:
                    continue

            def _in_visible_page(obj) -> bool:
                for pg in top_pages:
                    if obj is pg:
                        return True
                    try:
                        if pg.isAncestorOf(obj):
                            return True
                    except RuntimeError:
                        continue
                return False

            for ch in ref.findChildren(QWidget):
                if ch is self or ch.isWindow():
                    continue
                if isinstance(ch, _STRUCTURAL):        # ③ 布局骨架
                    continue
                try:
                    if not ch.isVisible():
                        continue
                    if not _in_visible_page(ch):        # ⑤
                        continue
                    r = QRect(ch.mapTo(ref, QPoint(0, 0)), ch.size())
                except (RuntimeError, TypeError):
                    continue          # 控件已被 C++ 侧销毁，跳过
                if r.width() <= 0 or r.height() <= 0:
                    continue
                if r.height() >= h:            # ① 铺满整页的容器，跳过
                    continue
                if r.width() < w / 2:          # ② 不是横贯底部的一整条
                    continue
                if r.y() < 0 or r.y() + r.height() > h:   # ④ 超坐标控件
                    continue
                # ⚠ 必须再卡「薄条」：底栏 = 状态栏/按钮条那种**一条**，
                # 不是整个底部面板。实测页0 的底部面板高 300px，不卡这条时
                # 基准被抬到 y=540，水印跑到页面正中（距窗底 488px），
                # 比原来还离谱。留 40px 正好对上真机那 ~20px 的偏移量级。
                if r.height() > 40:
                    continue
                if h - (r.y() + r.height()) <= 40:   # ⑥ 贴着底
                    lowest = min(lowest, r.y())
            return lowest if lowest < h else h
        except RuntimeError:
            return ref.height()


    def _target_box(self) -> QRect:
        """**左下角**水印小方框（**host 坐标**）。

        R48-A1/A2：算在 anchor（stack）坐标里，再 `mapFrom` 回自己的 host，
        所以三页拿到的是同一个锚点算出的同一个位置 —— 不允许各页各算。

        ⚠ **为什么 anchor 模式下不再逐页扫 `_content_bottom()`**：
        作业单 A2 要求三页落在「完全相同的 host 坐标」。而逐页扫出来的
        基线是**跟着当前页变**的（实测同一窗口下任务页 848 / 审核页 812 /
        图库页 791 —— 三页底部操作条本来就不等高）。这两条要求**不可能
        同时满足**：只要还按"当前页自己的底栏"算，切页水印就必然跳。
        取舍很明确 —— **同一位置优先**（用户抱怨的正是错位），所以
        anchor 模式一律用参照系全高当底边；「别被底栏压住」这件事由
        `OFFSET_Y = -50`（向上 50px）保证，那个值是 10-07 真机调校出来的。
        非 anchor 模式（自建 host，无参照系）才保留逐页扫的旧行为。

        host 太小就退化成空矩形（不画）。
        """
        host = self._host()
        if host is None:
            return QRect()
        ref = self._ref()
        # ⚠ `unified` 只看「有没有 anchor」，**不能**看 `ref is not host`：
        #   stack 上那只水印的 host 就是 stack 自己（ref is host），但它同样
        #   传了 anchor，同样必须走统一分支 —— 否则只有它一页还在逐页扫
        #   _content_bottom()，实测它在 848/812/791 之间跳，三页又不齐了。
        unified = self._anchor is not None
        w = ref.width()
        h = ref.height() if unified else self._content_bottom()
        if w <= 2 * INSET or h <= 2 * INSET:
            return QRect()
        # R175：竖排水印（旋转后画布 height > width）走**瘦高方框**分支——
        # 高度上限 BOX_MAX，宽度按图真实宽高比收缩，且不超过参照系宽 1/6。
        # 仍在左下角、同样的 INSET/OFFSET，只换「方框形状」不换锚定语义。
        # 横图/方图（含历史方 logo）维持原 square 分支，行为不变。
        pm = self._pm
        if pm is not None and not pm.isNull() and pm.height() > pm.width():
            bh = min(BOX_MAX, h - 2 * INSET)
            bh = max(1, int(bh))
            bw = int(round(bh * pm.width() / pm.height()))
            bw = max(1, min(bw, max(1, w - 2 * INSET)))  # R233：窄列(sidebar)竖条也不压成细线
        else:
            # 方框边长：≤BOX_MAX，且不超过参照系宽 1/3、不超过可用高/宽
            side = min(BOX_MAX, max(60, w // 3), h - 2 * INSET, w - 2 * INSET)
            side = max(1, int(side))
            bw = bh = side
        # R176：位置统一交给 _place()（没传 below 时与旧代码逐像素一致）。
        x0, y0 = self._place(ref, h, bh)
        box_in_ref = QRect(x0, y0, bw, bh)
        if not unified:
            return box_in_ref
        # 换算回 host 坐标。
        # ⚠⚠ **方向别搞反**（R48 实测踩过）：`ref.mapTo(host, QPoint(0,0))`
        #   求的是「**ref 的原点**在 host 里落在哪」，对 ref 是 host 的祖先
        #   这种情况毫无意义 —— 实测恒返回 (102, 23)（那是 stack 自己在
        #   MainWindow 里的位置），害我以为三页真有 102px 错位。
        #   正确方向是 `host.mapTo(ref, QPoint(0,0))` = 「host 的原点在 ref
        #   坐标系里落在哪」。实测 review/gallery 都是 stack 的**直接子控件**
        #   且偏移恒为 (0,0)，所以三页算出的坐标完全一致。
        try:
            off = host.mapTo(ref, QPoint(0, 0))
        except (RuntimeError, TypeError):
            return QRect()
        if off.x() or off.y():
            return box_in_ref.translated(off)
        return box_in_ref

    def attach(self, host):
        """浮到 host **最上层**，并只占左下角一小块（B2 + R48-A1）。

        ⚠ B2（真机 2026-10-08）：原来这里 `lower()`，靠「把 stack 的后代
        QWidget 全刷 transparent」让水印从内容底下透出来。实测**网格/列表页
        完全看不见水印**（探针量到页 1/页 2 可见水印像素 = **0**），
        因为网格是 `QListWidget + setItemWidget` 的自绘条目 + 每项自己的
        实底，透明 QSS 根本管不到；而任务页那格是 QTableWidget，恰好被
        #43 的穿透规则覆盖，所以「只有任务页看得见」。

        正确做法（作业单 B2 原文）：**置顶浮层 + 穿透点击**。
        控件本身只占一角 ≤260px 一小块，物理上盖不到别的控件；
        `WA_TransparentForMouseEvents` 保证不吃点击。
        """
        if host is None:
            return False
        self.setParent(host)
        try:
            # 直接挂在父容器的 resize 上：resize() 一定会来（无论布局是否
            # activate），这时把它转发给水印的 _fit。
            host.installEventFilter(self)
        except Exception:
            pass
        # R48-A2：anchor（stack）尺寸变化同样要重算 —— 三页水印的位置
        # 现在由它决定。这里装一次；`_fit()` 里还会幂等补装（attach 跑在
        # 布局之前，那时 anchor 可能还没被识别，见 `_ref()` 的说明）。
        self._ensure_ref_filter()
        self._raise_top()
        self._fit()
        return True

    def _raise_top(self):
        """把自己抬到 host 子控件的最上层（B2）。

        `raise_()` 在 Qt 里是 O(兄弟数)，但这里只在几何变化/attach 时调，
        不是 paint 热路径。水印对兄弟控件数量的敏感度实测可接受。
        """
        try:
            self.raise_()
        except RuntimeError:
            pass


    def eventFilter(self, obj, ev):  # noqa: N802
        """父容器 / 参照系的任何几何变化都重新贴合，并把水印顶回上层（B2）。

        ⚠ B2：只 `_fit()` 不够 —— 页面里的网格/列表在切页、刷新后会
        新增子控件（`setItemWidget` 等），新控件默认叠在水印**上面**，
        于是水印又看不见了。所以每次贴合后必须重新 `raise_()`。

        ⚠ R48-A2：anchor（stack）的 resize/子页变化同样要触发 —— 三页水印
        位置由它算，只监听自己的 host 会在窗口改尺寸后三页不一致。
        """
        try:
            if (obj is self.parentWidget() or obj is self._ref()
                    or obj is self._below_widget()):
                if self._target_box() != self.geometry() or \
                        ev.type() in (QEvent.Type.ChildAdded,
                                      QEvent.Type.ChildPolished,
                                      QEvent.Type.Show):
                    self._fit()
        except RuntimeError:
            pass
        return super().eventFilter(obj, ev)

    def _fit(self):
        host = self._host()
        if host is None:
            return
        try:
            self._ensure_ref_filter()   # R48：attach 时布局未跑，这里补装
            self.setGeometry(self._target_box())
            self._raise_top()          # ← B2：置顶，不再 lower()
        except RuntimeError:
            pass

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._fit()

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self._fit()

    def set_pixmap(self, pm: QPixmap) -> None:
        self._pm = pm
        self._tight = None      # 换图了，缓存作废
        self._fit()             # 尺寸可能随图变，重算一次
        self.update()

    def set_alpha(self, alpha: int) -> None:
        self._alpha = int(max(0, min(255, alpha)))
        self.update()

    @property
    def has_image(self) -> bool:
        return self._pm is not None and not self._pm.isNull()

    def paintEvent(self, e):  # noqa: N802
        """把整张原图等比画进自己这块小方框（右下角）。

        **用户定的规矩（10-04 晚）：整张 256x256 原图完整放上去，不许裁剪。**
        所以源矩形就是整张原图 QRect(0,0,w,h)，等比缩放（contain）填进
        self.rect()。控件几何已经保证在右下角 + 留白，paintEvent 只管把图
        漂漂亮亮画进去 —— 不再自己算右下角偏移（那套是把控件铺满整窗时的
        老算法，控件既然只占右下角了，就没必要再算一遍）。
        """
        if not self.has_image:
            return
        src = QRect(0, 0, self._pm.width(), self._pm.height())
        box = self.rect()
        if box.width() <= 0 or box.height() <= 0:
            return
        # 等比缩放（contain）：图不被拉伸变形，居中放
        ar = src.width() / max(1, src.height())
        bw, bh = box.width(), box.height()
        tw = bw
        th = max(1, int(tw / ar))
        if th > bh:
            th = bh
            tw = max(1, int(th * ar))
        target = QRect(box.x() + (bw - tw) // 2,
                       box.y() + (bh - th) // 2, tw, th)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setOpacity(self._alpha / 255.0)
        p.drawPixmap(target, self._pm, src)
        p.end()

    def _tight_rect(self) -> QRect:
        """原图里真正有内容的那块（去掉四周透明边）。结果缓存 ——
        扫一遍是 256x256=65536 次取色，paintEvent 每次都扫会明显卡顿。

        （现版 paintEvent 画的是整图，不再用它；保留给需要贴合内容 bbox
        的场景/测试，删了怕有外部引用。）"""
        if self._tight is not None:
            return self._tight
        img = self._pm.toImage()
        if img.isNull():
            return QRect(0, 0, self._pm.width(), self._pm.height())
        # QImage 没有 getbbox，手工扫一遍 alpha 的上下左右边界
        left, top = img.width(), img.height()
        right = bottom = -1
        for y in range(img.height()):
            for x in range(img.width()):
                if img.pixelColor(x, y).alpha() > 8:
                    if x < left:
                        left = x
                    if x > right:
                        right = x
                    if y < top:
                        top = y
                    if y > bottom:
                        bottom = y
        if right < 0:
            self._tight = QRect(0, 0, img.width(), img.height())
        else:
            self._tight = QRect(left, top, right - left + 1, bottom - top + 1)
        return self._tight
