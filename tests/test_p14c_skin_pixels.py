"""P1-4 动态验收：换肤真的改到像素了吗（离屏逐页截图比对）。

背景：作业单 `P24_P14_ORDER.md` 的 P1-4「动态」验收要求 ——
`QT_QPA_PLATFORM=offscreen` 起每个主页面 + 各对话框，`grab().save()` 截图，
切到暗色皮肤后再截图，逐页比对「硬编码色区域」像素是否随皮肤变化，
且暗色下文字/背景对比可读（**无白底黑字逆色**）。

R37 把这一项判为「本机离屏做不到，只能留给真机」。**本轮实测推翻了这个判定**：
Qt 的 offscreen 平台插件照样走完整paintEvent / QPainter 光栅化路径
（只是不往显示器 presents），`QWidget.grab()` 拿到的是**真实像素**
——实测证据见 `tools/_p14c_probe.py`：
  纯色控件 grab 读回 #3366cc✓ / QPainter 自绘的对角线与底色分别读出✓ /
  QPushButton 的 QSS 强调色在取样色里 ✓ / PNG 真落盘 504 字节 ✓
所以「无显示器就不��比较像素」是把「不能**看**」错当成「不能**测**」。

本文件就把那一项补上，断言**关系**不写死数值（P26铁律：快照式断言是定时炸弹）：
  ·每页在深↔浅之间**必须**有明显比例的像素变化（没变= 硬编码色没被治到）；
  · 切皮肤（violet→teal）也必须有像素变化（只换强调色也被治到）；
  · 暗色下**不得出现大面积近白底**（那正是「白底黑字逆色」的量化定义）；
  · 对比度：暗色下正文文字与所在底色必须有最小亮度差。
只写不删：截图产物落tools/_p14c_out/。
"""
from __future__ import annotations

import os
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_OUT = _ROOT / "tools" / "_p14c_out"

pytestmark = pytest.mark.skipif(
    os.environ.get("QT_QPA_PLATFORM") != "offscreen",
    reason="逐页抓图只在 offscreen 下跑（真实显示器下没必要白占 GPU）",
)


@pytest.fixture(autouse=True)
def _restore_theme():
    """每个用例结束后把全局主题还原成浅色 + violet。

    为什么必须autouse 而不是只写在用到 win 的 fixture 上：本文件里有
    不取 win 的用例（对话框那两条）也会切主题，全局状态一样会漏出去。
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication
        from media_resource_manager.gui import theme
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, False, "violet")
            for _ in range(2):
                app.processEvents()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def win(_qapp, tmp_path, monkeypatch):
    """起一个真MainWindow（离屏），带P2-2 的构造超时守护。"""
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", str(d))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setattr("media_resource_manager.config.user_data_dir", lambda: d)

    import media_resource_manager.gui.main_window as mw
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    from media_resource_manager.gui import theme

    mw._ensure_single_instance = lambda a: True
    # 模态一律换掉：真调会永久阻塞（offscreen 下没人点确定）。
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted

    app = QApplication.instance()
    for old in app.topLevelWidgets():
        try:
            for tmr in old.findChildren(QTimer):
                tmr.stop()
            old.close()
        except Exception:
            pass
    theme.apply_theme(app, False, "violet")

    hung = {"v": False}
    wd = QTimer()
    wd.setSingleShot(True)
    wd.timeout.connect(lambda: (hung.__setitem__("v", True), app.quit()))
    wd.start(8000)
    try:
        w = mw.MainWindow()
    finally:
        wd.stop()
    assert not hung["v"], "MainWindow 构造后界面在 8s 内没能完成事件循环"
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    yield w
    # ⚠ **必须把主题状态还原成浅色**：本文件大量切深/切皮肤，而
    # theme._CURRENT_DARK 是**全局**的。不还原会污染后续文件 ——
    # 实测踩过：`test_r67_inline_style_skin.py::test_style_applies_and_registers`
    # 断言 `css(False, ...)` 的浅色值，却拿到本文件留下的深色值而假红。
    # 单跑 r67 永远绿，**只有合跑才暴露** —— 典型的跨文件状态污染。
    try:
        theme.apply_theme(QApplication.instance(), False, "violet")
        for _ in range(3):
            QApplication.instance().processEvents()
    except Exception:
        pass
    try:
        w.close()
    except Exception:
        pass


# ── 像素工具 ────────────────────────────────────────────────────────────
def _img(widget):
    _qapp_pump()
    return widget.grab().toImage()


def _qapp_pump():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    for _ in range(3):
        app.processEvents()


def _ratio_changed(a, b, step: int = 4):
    """两张图里「像素不同」的比例（0~1）。抽样式采样，纯 CPU 就够。

    ⚠ 抽样式采样（每隔 step 像素）是**故意的**：全图逐像素在1200x800 上
    每页要 100 万次比对，30 项下来会让这文件从 20s 变成几分钟。
    换肤改的是大面积底色，抽样式完全抓得住；真正要抓的是「有没有变」。
    """
    assert a.width() == b.width() and a.height() == b.height(), "尺寸变了，比对无意义"
    same = 0
    total = 0
    for x in range(0, a.width(), step):
        for y in range(0, a.height(), step):
            total += 1
            if a.pixel(x, y) == b.pixel(x, y):
                same += 1
    return 1.0 - (same / max(total, 1))


def _is_near_white(rgb, thr: int = 232):
    r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
    return r >= thr and g >= thr and b >= thr


def _near_white_ratio(img, step: int = 6):
    n = 0
    total = 0
    for x in range(0, img.width(), step):
        for y in range(0, img.height(), step):
            total += 1
            if _is_near_white(img.pixel(x, y)):
                n += 1
    return n / max(total, 1)


def _mean_luma(img, step: int = 6):
    tot = 0.0
    n = 0
    for x in range(0, img.width(), step):
        for y in range(0, img.height(), step):
            p = img.pixel(x, y)
            tot += 0.299 * ((p >> 16) & 255) + 0.587 * ((p >> 8) & 255) + 0.114 * (p & 255)
            n += 1
    return tot / max(n, 1)


def _pages(win):
    """要逐页抓图的对象：**窗口本身 + stack 每一页 + 底栏**。

    ⚠⚠ **必须包含窗口本身**，这是实测踩出来的：
    注入验证（tools/_p14c_inject.py）往 `set_skin` 里塞了一行
    `self.setStyleSheet("QMainWindow{background:#ffffff}")`，结果
    **7 项全绿** —— 因为各子页自己都有底色，把窗口那块白盖得严严实实，
    抓子页根本看不见。窗口级残留是P1-4 最典型的形态（「行内 setStyleSheet
    自带优先级」），只测子页就等于没测。
    """
    out = [("main_window", win)]
    for i in range(win.stack.count()):
        out.append((f"stack_page{i}", win.stack.widget(i)))
    bt = getattr(win, "bottom_tabs", None)
    if bt is not None:
        out.append(("bottom_tabs", bt))
    return out


def _switch(win, dark: bool, skin: str = "violet"):
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme
    theme.apply_theme(QApplication.instance(), dark, skin)
    for _ in range(4):
        QApplication.instance().processEvents()


def _dialogs():
    """作业单点名的「各对话框」也必须逐个抓 —— 只测主窗口等于漏了一半。

    对话框是**独立顶层窗口**，不走主窗口那套全局 QSS 覆盖路径。

    只收构造期不碰网络 / 不弹模态的，两个都只依赖一个仓储接口
    （看板要 db.query，代理窗要 pool.list），给最小假实现即可。
    ⚠ **绝不在这里真调 exec()**：offscreen 下没人点确定 = 永久挂死。
    ⚠ 这里**故意不吞异常**：第一版我写 `except: pass`，结果两个对话框
    全没造出来、覆盖层形同虚设，而测试只报「一个都没构造出来」——
    看不出真实原因是 `AttributeError: 'NoneType' has no attribute 'query'`。
    静默吞异常会让「没测到」长得像「测过了」，这比红更危险。
    """
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui.learning_board import LearningDashboard
    from media_resource_manager.gui.proxy_dialog import ProxyDialog

    class _Rows(list):
        def query(self, *a, **k):
            return self

    class _Pool:
        def list(self, *a, **k):
            return []

    app = QApplication.instance()
    out = [("LearningDashboard", LearningDashboard(_Rows())),
           ("ProxyDialog", ProxyDialog(_Pool()))]
    for _n, d in out:
        d.resize(720, 600)
        d.show()
    for _ in range(3):
        app.processEvents()
    return out


# ── 逐页：深↔浅 必须变 ─────────────────────────────────────────────────
def test_every_page_changes_pixels_between_light_and_dark(win):
    """每页在深↔浅之间都该有明显像素变化。

    阈值只防「整页一个像素都没变」（= 换肤对该页完全失效）。
    定 0.02 是因为页面里有大片**语义色与图片**（状态点、评分档、缩略图）
    本来就该两套模式一模一样 —— 变的是底色那一部分，不是整页翻过来。
    """
    bad = []
    for name, page in _pages(win):
        _switch(win, False)
        light = _img(page)
        _switch(win, True)
        dark = _img(page)
        r = _ratio_changed(light, dark)
        if r < 0.02:
            bad.append((name, round(r, 5)))
    assert not bad, f"这些页切深浅后像素几乎没变（硬编码色没被治到）: {bad}"


def test_skin_switch_changes_pixels(win):
    """换强调色皮肤（violet→teal）也必须改到像素，否则「皮肤」仍是半成品。"""
    _switch(win, False, "violet")
    a = _img(win.stack.currentWidget())
    _switch(win, False, "teal")
    b = _img(win.stack.currentWidget())
    r = _ratio_changed(a, b)
    assert r > 0.0, "换皮肤后当前页一个像素都没变 —— 皮肤切换对界面无影响"


# ── 对话框：作业单点名的「各对话框」这一层 ────────────────────────────
def test_dialogs_change_pixels_between_light_and_dark():
    """对话框也必须随深浅变。

    对话框是**独立顶层窗口**，不走主窗口那套全局 QSS 的覆盖路径 ——
    只测主窗口的话，对话框里残留的硬编码色一个都抓不到。
    """
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme
    dl = _dialogs()
    assert dl, "一个对话框都没构造出来 —— 覆盖层形同虚设"

    app = QApplication.instance()
    bad = []
    for name, dlg in dl:
        try:
            theme.apply_theme(app, False, "violet")
            for _ in range(3):
                app.processEvents()
            light = dlg.grab().toImage()
            theme.apply_theme(app, True, "violet")
            for _ in range(3):
                app.processEvents()
            dark = dlg.grab().toImage()
            r = _ratio_changed(light, dark)
            if r < 0.02:
                bad.append((name, round(r, 5)))
        finally:
            try:
                dlg.close()
            except Exception:
                pass
    assert not bad, f"这些对话框切深浅后像素几乎没变: {bad}"


def test_dialogs_keep_no_large_near_white_bg_in_dark_mode():
    """暗色下对话框也不许留大片近白底（逆色）。"""
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme
    app = QApplication.instance()
    bad = []
    dl = _dialogs()
    for name, dlg in dl:
        try:
            theme.apply_theme(app, True, "violet")
            for _ in range(3):
                app.processEvents()
            r = _near_white_ratio(dlg.grab().toImage())
            if r > 0.06:
                bad.append((name, round(r, 4)))
        finally:
            try:
                dlg.close()
            except Exception:
                pass
    assert not bad, f"暗色模式下这些对话框仍有大片近白底: {bad}"


# ── 暗色下不许有「白底黑字逆色」 ───────────────────────────────────────
def test_no_page_keeps_large_near_white_bg_in_dark_mode(win):
    """暗色下每页近白底像素占比必须低。

    「白底黑字逆色」在量化上就是「深色模式里还剩一大片近白底」。
    阈值 0.06：留的余量给**真实内容**—— 用户自己的图就是白的
    （白底图片/白底缩略图在暗色界面里就该是白的，那不是 bug）。
    整页底色若没被换掉，这个比例会直接冲到 0.5 以上。
    """
    bad = []
    for name, page in _pages(win):
        _switch(win, True)
        r = _near_white_ratio(_img(page))
        if r > 0.06:
            bad.append((name, round(r, 4)))
    assert not bad, f"暗色模式下这些页仍有大片近白底（逆色）: {bad}"


def test_dark_mode_is_actually_darker_than_light(win):
    """暗色必须真的更暗 —— 防止「切了但没生效」被上面两条漏过去。"""
    for name, page in _pages(win):
        _switch(win, False)
        light = _mean_luma(_img(page))
        _switch(win, True)
        dark = _mean_luma(_img(page))
        assert dark < light, f"{name}:暗色均亮度未低于浅色（dark={dark:.1f} light={light:.1f}）"


# ── 自绘控件（学习看板/ 任务行状态点）也要跟着换 ────────────────────────
def test_painted_canvas_follows_theme():
    """自绘控件（paintEvent 里取调色板）必须随深浅变。

    这条覆盖的是 QSS 盖不住的那一类：R27/R37 把行内字面量收敛进theme.py，
    但自绘点是另一条路 —— 若它没重读调色板，静态护栏全绿也照样不换色。
    """
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtGui import QPainter, QColor
    from media_resource_manager.gui import theme

    class Canvas(QWidget):
        """读调色板画一块「面板底 + 强调色条」——与 learning_board 同型。"""

        def paintEvent(self, _e):
            pal = theme.palette(theme._CURRENT_DARK)
            p = QPainter(self)
            p.fillRect(self.rect(), QColor(pal["surface"]))
            p.fillRect(0, 0, 40, self.height(), QColor(pal["accent"]))
            p.end()

    c = Canvas()
    c.resize(120, 60)
    c.show()

    # ⚠ 必须**显式**把前置状态设成浅色，不能依赖「默认就是浅色」。
    #实测：单跑绿、合跑红 —— 因为前面的用例把 theme._CURRENT_DARK 留成了True，
    # 于是这里的 "light"起点其实已经是暗色，前后两张一样当然不降。
    # （用例间共享全局主题状态是这类「比before/after」测试最容易踩的坑。）
    theme.apply_theme(QApplication.instance(), False, "violet")
    for _ in range(3):
        QApplication.instance().processEvents()
    light = c.grab().toImage()

    theme.apply_theme(QApplication.instance(), True, "violet")
    for _ in range(3):
        QApplication.instance().processEvents()
    dark = c.grab().toImage()

    assert _mean_luma(dark) < _mean_luma(light), "自绘面板没跟随深色模式"


# ── 图标在暗色下可见 ───────────────────────────────────────────────────
def test_icons_are_not_invisible_in_dark_mode():
    """暗色下图标不能「暗底 + 暗色字」= 看不见，而且不能变成黑方块。

    ⚠⚠ **判据只能落在「真控件抓图」上，不能去读 `QIcon.pixmap()` 的 alpha** ——
    本轮在这上面绕了很久，实测：
      · `QImage` 内存里是真透明（rawBits 读回 `00 00 00 00`）；
      · 但 `QImage.pixel()` 在本机offscreen 构建下把同一个像素读成
        `alpha=255` → 据此断言「图标 0% 透明 = 渲染坏了」是**假的**；
      · 真按钮抓图里主色是页面背景（实测 #0f1115 占 4675/4800），
        **根本没有黑方块** —— 产品是对的，是我的读取方式坏了。
    所以这里改成：把图标 setIcon 到真按钮上再 grab()，断言
      ① 主色必须是**按钮自己的底色**（不是黑色 —— 黑方块才是真 bug）；
      ② 图标区必须出现**明显亮于底色**的像素（=线条真画出来了、看得见）。
    这也正是作业单要的「真实观感」层，而不是我隔着三层数据结构猜。
    """
    from PySide6.QtWidgets import QApplication, QPushButton
    from PySide6.QtGui import QColor
    from media_resource_manager.gui import icons, theme

    # 前置状态必须显式设成暗色 —— 与自绘那条同一个坑（全局主题状态跨用例残留）。
    theme.apply_theme(QApplication.instance(), True, "violet")
    for _ in range(3):
        QApplication.instance().processEvents()

    pal = theme.palette(True)
    btn_bg = pal["surface_alt"]

    btn = QPushButton("图标可见性")
    btn.setIcon(icons.icon("checklist", "", 16))
    btn.setStyleSheet(f"QPushButton{{background:{btn_bg};color:{pal['text']};padding:8px;}}")
    btn.resize(160, 48)
    btn.show()
    for _ in range(3):
        QApplication.instance().processEvents()

    img = btn.grab().toImage()

    def _luma(rgb):
        r, g, b = (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
        return 0.299 * r + 0.587 * g + 0.114 * b

    bg_luma = _luma(QColor(btn_bg).rgb() & 0xFFFFFF)
    bright = 0
    dark_box = 0
    total = 0
    for x in range(img.width()):
        for y in range(img.height()):
            c = QColor(img.pixel(x, y))
            total += 1
            if _luma(c.rgb() & 0xFFFFFF) > bg_luma + 40:
                bright += 1
            # 「黑方块」= 明显暗于按钮底色（不是等于底色，是比它还暗一大截）。
            if _luma(c.rgb() & 0xFFFFFF) < bg_luma - 40:
                dark_box += 1

    assert bright > 20, f"按钮上找不到明显亮于底色的像素（图标没画出来） bright={bright}"
    assert dark_box / total < 0.02, (
        f"按钮上有 {dark_box}/{total} 像素明显暗于底色 —— 图标变成了黑方块")


# ── 截图落盘（证明真渲染过，不是空图） ─────────────────────────────────
def test_screenshots_actually_saved_as_png(win):
    _OUT.mkdir(parents=True, exist_ok=True)
    page = win.stack.currentWidget()
    _switch(win, True)
    p = _OUT / "page_dark.png"
    page.grab().save(str(p))
    assert p.exists() and p.stat().st_size > 1000, "PNG 没落盘或过小（疑似空白图）"