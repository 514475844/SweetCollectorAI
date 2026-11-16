"""R176 / R177 / R182 护栏。

订单：
* `.workbuddy/R176_WATERMARK_MOVE.md` —— 水印「继续往左移」+ 锚到左侧导航
  （任务/审核/图库/隔离区）**正下方**；R175 的竖排 / ALPHA=178 / 穿透
  / 三页同位置一律不动。
* `.workbuddy/R177_CONCURRENT_LAG.md` —— 全部开始 / 全部停止不再卡 UI：
  谁都不许在点击那一下同步重建任务列表（走 500ms 合并的挂号刷新）。
* `.workbuddy/R182_GLOBAL_HOTKEY.md` —— 软件不许装任何系统级键盘钩子，
  不许用应用级快捷键吞 Ctrl。

判据风格（本仓铁律）：
* 行为断言优先（真建 QWidget、真算几何、真解析 AST）；
* 文本断言**先剥注释**再匹配（R104 铁律：头部注释里写一句就能骗过字面判据）；
* 断言「关系」不「数值」。
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "media_resource_manager"
MAINWIN = SRC / "gui" / "main_window.py"
WM = SRC / "gui" / "watermark.py"

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402


@pytest.fixture(scope="module")
def qapp():  # noqa: D103
    import sys

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


def _strip_comments(text: str) -> str:
    """剥掉注释与 docstring 之外的注释行（R104：字面判据先剥注释）。"""
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            continue
        out.append(tok.string)
    return " ".join(out)


# ══════════════════════════════════════════════ R176：水印锚导航下方
def _mk(qapp, w=900, h=700):
    """建一个**已显示**的宿主。

    ⚠ 必须 show()：没显示的 QWidget 的子控件 `isVisible()` 恒为 False，
    水印会走「导航不可见」的兜底分支 —— 那是产品行为，不是 bug，
    但会让本文件的 R176 判据变成恒真的空壳。
    """
    host = QWidget()
    host.resize(w, h)
    host.show()
    return host


def test_no_below_keeps_bottom_left_exactly(qapp):
    """没传 below ⇒ 位置与 R48/R175 逐像素一致（老护栏靠这条活着）。"""
    from media_resource_manager.gui import watermark as W

    host = _mk(qapp)
    wm = W.WatermarkOverlay(parent=host, anchor=host)
    wm.attach(host)
    g = wm.geometry()
    assert g.x() == W.INSET + W.OFFSET_X, f"左边不该动：{g.x()}"
    assert g.y() + g.height() == host.height() - W.INSET + W.OFFSET_Y, (
        f"底边不该动：{g.y() + g.height()}")


def test_below_anchors_under_nav_and_moves_left(qapp):
    """R176 核心：顶边 = 导航底边 + BELOW_GAP，且比原来更靠左。"""
    from media_resource_manager.gui import watermark as W

    host = _mk(qapp)
    nav = QWidget(host)
    nav.setGeometry(0, 120, 120, 200)          # 底边 y=319（host 坐标）
    nav.show()
    wm = W.WatermarkOverlay(parent=host, anchor=host, below=nav)
    wm.attach(host)
    g = wm.geometry()
    expect_top = nav.geometry().bottom() + W.BELOW_GAP
    assert abs(g.y() - expect_top) <= 1, (
        f"没锚到导航下方：顶边 {g.y()} vs 期望 {expect_top}")
    assert g.x() == W.LEFT_INSET, f"没用 LEFT_INSET：{g.x()}"
    assert g.x() < W.INSET + W.OFFSET_X, "R176 要求「继续往左移」，x 必须更小"
    assert g.y() + g.height() <= host.height() - W.INSET, "水印压到窗口底边外了"


def test_below_callable_resolved_each_fit(qapp):
    """传 callable 也能锚上（两套导航换皮肤后要重新解析，不能写死一个）。"""
    from media_resource_manager.gui import watermark as W

    host = _mk(qapp)
    a = QWidget(host)
    a.setGeometry(0, 120, 120, 200)
    a.show()
    b = QWidget(host)
    b.setGeometry(0, 220, 120, 120)            # 底边 y=339：比 a 更低，且仍留得下水印
    b.show()
    box = {"w": a}
    wm = W.WatermarkOverlay(parent=host, anchor=host,
                            below=lambda: box["w"])
    wm.attach(host)
    g1 = wm.geometry()
    box["w"] = b
    wm._fit()
    g2 = wm.geometry()
    assert abs(g1.y() - (a.geometry().bottom() + W.BELOW_GAP)) <= 1
    assert g2.y() > g1.y(), "换了下锚控件却没跟着动 ⇒ 写死了"
    assert abs(g2.y() - (b.geometry().bottom() + W.BELOW_GAP)) <= 1


def test_below_falls_back_when_no_room(qapp):
    """下方塞不下（连 6 成都没有）⇒ 退回贴底，绝不把水印挤出可视区。"""
    from media_resource_manager.gui import watermark as W

    host = _mk(qapp, w=900, h=700)
    nav = QWidget(host)
    nav.setGeometry(0, 640, 120, 60)           # 底边 699 ≈ 窗口底
    nav.show()
    wm = W.WatermarkOverlay(parent=host, anchor=host, below=nav)
    wm.attach(host)
    g = wm.geometry()
    assert g.y() + g.height() <= host.height() - W.INSET, (
        f"退回贴底也该留 INSET：底边 {g.y() + g.height()} / {host.height()}")


@pytest.mark.skip(reason="2026-10-27 用户：水印以后取消，不再维护水印几何测试")
def test_main_window_watermark_below_is_opt(qapp):
    """2026-10-27 按 R233 重写：MainWindow 现在**不传** below（水印挂 sidebar
    底部即可，导航最后一项的下方没有内容要避让）。below 仍是 Overlay 的
    可选能力，单测继续守它；这里只守「主窗口行为正确」。"""
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    for _ in range(4):
        app.processEvents()
    try:
        wms = list(getattr(w, "_watermarks", []))
        assert len(wms) == 1, "R233 应只有一个水印（挂 sidebar）"
        wm = wms[0]
        # below 没传 ⇒ 走贴底分支（_place 里 below is None 的路径）
        assert wm._below is None, "主窗口不应传 below（below 挪位后残留参数会锚错）"
        # 几何仍贴 sidebar 左缘+底
        host = wm.parentWidget()
        g = wm.geometry()
        assert abs(g.x() - (12 + 0)) <= 2, f"左缘不对：{g.x()}"
        assert host.height() - (g.y() + g.height()) <= 14, "未贴底"
    finally:
        try:
            w.close()
        except Exception:
            pass
        app.setStyleSheet("")


def test_below_resolver_picks_lowest_visible_nav_button(qapp):
    """`_watermark_below_widget()` 真跑：挑**可见且在 stack 坐标里最低**的那个。"""
    try:
        from media_resource_manager.gui.main_window import MainWindow
    except Exception as e:                       # 环境缺依赖时不假绿
        pytest.skip(f"MainWindow 导入失败：{e}")

    stack = QWidget()
    stack.resize(900, 700)
    upper = QWidget(stack)
    upper.setGeometry(0, 60, 120, 50)           # 底边 109
    lower = QWidget(stack)
    lower.setGeometry(0, 300, 120, 50)          # 底边 349
    hidden = QWidget(stack)
    hidden.setGeometry(0, 600, 120, 50)         # 更低但**不可见**
    stack.show()                                # 不 show ⇒ 全部子控件 isVisible()=False
    hidden.hide()

    class _Fake:
        _nav_btns = [upper]
        _nav_qb = [(lower, None, 2), (hidden, None, 3)]

    _Fake.stack = stack
    got = MainWindow._watermark_below_widget(_Fake())
    assert got is lower, "该挑可见的最低那个（隔离区），不是隐藏的也不是上面的"


# ══════════════════════════════════════════════ R177：开始/停止不卡 UI
def _func_src(tree, name, cls="MainWindow"):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name == name:
                    return f
    raise AssertionError(f"找不到 {cls}.{name}")


def _called_attrs(fn) -> set[str]:
    out = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            out.add(node.func.attr)
    return out


@pytest.mark.parametrize("fname", ["stop_all", "stop_all_quiet", "run_all"])
def test_bulk_actions_do_not_rebuild_list_synchronously(fname):
    """R177 核心：这三个批量动作**都不许**当场 refresh_list()。

    22 个任务的 queued/stopped 事件已经各会挂一次 500ms 合并刷新，
    在这里再同步重建一次列表＝点一下卡一下的直接成因。
    """
    tree = ast.parse(MAINWIN.read_text(encoding="utf-8"))
    fn = _func_src(tree, fname)
    attrs = _called_attrs(fn)
    assert "refresh_list" not in attrs, (
        f"{fname} 还在同步重建列表（R177 要去掉的就是它）")
    assert "_schedule_list_refresh" in attrs, (
        f"{fname} 没改走挂号刷新")


def test_list_refresh_is_throttled(qapp):
    """挂号刷新必须是**单次**定时器且间隔 ≤600ms（不然合并就没意义）。"""
    try:
        from media_resource_manager.gui.main_window import MainWindow
    except Exception as e:
        pytest.skip(f"MainWindow 导入失败：{e}")

    class _Dummy(QWidget):
        """只带 `_schedule_list_refresh` 真正用到的那几个属性。

        ⚠ `_flush_list_refresh` 必须有：生产代码在 `connect()` 之后才把定时器
        挂到 self 上，缺这个槽会抛 AttributeError 被 `except Exception` 吞掉
        —— 测试方法就变成「永远拿不到定时器」的空壳。
        """
        _closing = False

        def _flush_list_refresh(self):  # noqa: D102
            pass

    d = _Dummy()
    MainWindow._schedule_list_refresh(d)
    t = getattr(d, "_list_refresh_timer", None)
    assert t is not None, "没建挂号定时器"
    assert t.isSingleShot(), "不是单次 ⇒ 会周期性空转刷列表"
    assert 0 < t.interval() <= 600, f"间隔 {t.interval()}ms 起不到合并作用"
    MainWindow._schedule_list_refresh(d)          # 第二次不该再叠一个定时器
    assert getattr(d, "_list_refresh_timer", None) is t


# ══════════════════════════════════════════════ R182：不许装全局键盘钩子
#: 「装了系统级键盘钩子」的硬特征。命中任一条＝会把用户的 Ctrl 抢走。
#: ⚠ grabKeyboard 不在此列：它是 Qt widget 级键盘抓取（只在控件有焦点时
#:   生效，不影响其他程序），与 SetWindowsHookEx/pynput 的全局 OS 钩子
#:   性质完全不同。F19 热键录制对话框合法使用它。
_HOOK_PATTERNS = (
    "SetWindowsHookEx",
    "WH_KEYBOARD_LL",
    "LowLevelKeyboard",
    "pynput",
    "keyboard.hook",
    "import keyboard",
    "Qt.ApplicationShortcut",
    "ShortcutContext.ApplicationShortcut",
)


def _py_files():
    for p in sorted(SRC.rglob("*.py")):
        yield p


def test_no_system_keyboard_hook_anywhere():
    """R182 核心：全仓不许有系统级键盘钩子 / 应用级吞键快捷键。

    ⚠ 先剥注释再匹配（R104 铁律）：注释里写一句 "别用 pynput" 就能让
    字面判据假红，反过来「注释里说没装」也骗不到本判据。
    """
    hits = []
    for p in _py_files():
        try:
            raw = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        code = _strip_comments(raw)
        for pat in _HOOK_PATTERNS:
            if re.search(re.escape(pat), code):
                hits.append(f"{p.relative_to(ROOT)}: {pat}")
    assert not hits, "发现全局键盘钩子 / 应用级快捷键（R182 红线）：\n" + "\n".join(
        sorted(set(hits)))


def test_shortcuts_stay_scoped_to_their_widget():
    """`QShortcut` 若用了上下文，必须是 Widget/Window 级（不是 Application）。

    只判「有没有 ApplicationShortcut」，不判具体绑哪个键 —— 绑什么键是
    产品决策，会变；「不许全局吞键」是红线，不变。
    """
    tree_ok = True
    for p in _py_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "ApplicationShortcut":
                tree_ok = False
    assert tree_ok, "存在 Qt.ApplicationShortcut（会把 Ctrl 从别的程序手里抢走）"
