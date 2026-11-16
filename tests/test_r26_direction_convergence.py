"""#26 / A3：方向收敛 —— ⚠ 已被 #46 部分推翻，见本文件末尾「#46 覆盖」。

#46（TONIGHT_FINAL_ORDER）：「图库中双击图片要以**灯箱**方式打开大图，
Web 与桌面端行为统一为灯箱」⇒ **图库双击重新回到灯箱**。
仍有效的部分：工具栏不许有「大图」按钮、审核页灯箱保留 on_decide、
顶级菜单 8 个、实时墙双击残留清零。

历史背景（#26 原文，保留以便追溯）：

用户诉求：「方向收敛，灯箱只留审核页，图库页撤」。图库原本有**三个**灯箱入口：
  ① 工具栏「大图」按钮；
  ② 右键菜单「大图查看（灯箱）」；
  ③ 网格模式 itemDoubleClicked。
而列表模式 cellDoubleClicked 与实时抓图墙走的都是**系统默认看图软件** ——
同一个页面里两种双击语义，用户按之前记的习惯去双击就会弹出不想看的窗口。

这里钉的不变量（每条都对应一种「撤漏了 / 撤多了 / 撤坏」）：
1. 图库工具栏**没有「大图」按钮**（真删，不是 setHidden —— 藏在布局里的按钮
   仍然是死代码，用户看到的还是「有个按钮在那儿」，这正是 r45 踩过的坑）。
2. 图库右键菜单**没有「大图查看（灯箱）」**。
3. 网格双击与列表双击、实时墙**统一**为 open_image_default（默认看图器）。
4. `open_lightbox` 方法与 main_window 的 `Lightbox` import 都已删除，
   钉「连类常量/导入一起删」（删方法不删 import = 留下死代码）。
5. **审核页灯箱必须还在**：ReviewTab 仍 import 并使用 Lightbox，且带
   on_decide（灯箱里能直接喜欢/不喜欢）。这是收敛的另一半，只撤图库不撤审核
   等于把主流程功能删了。
6. 灯箱**纯浏览模式仍可用**：on_decide=None 时四向翻页、幻灯片都不坏
   （lightbox 组件本身不删，只是没有图库调用方了）。
7. **不新增/不删顶级菜单**：还是 8 个（verify_r11 的基线，也是 r45 的教训）。
"""
import inspect
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

_MAIN = Path(__file__).resolve().parents[1] / "media_resource_manager" / "gui" / "main_window.py"
_REVIEW = Path(__file__).resolve().parents[1] / "media_resource_manager" / "gui" / "review_tab.py"
_LIGHTBOX = Path(__file__).resolve().parents[1] / "media_resource_manager" / "gui" / "lightbox.py"


@pytest.fixture(autouse=True)
def qapp(monkeypatch):
    """建任何 Qt 控件前都必须有 QApplication，否则 Qt 直接 fatal。

    弹窗一律打桩：真弹会 exec 挂住整个测试（r41 踩过）。
    """
    from PySide6.QtWidgets import QDialog, QMessageBox
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.No)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    return app


def _send(widget, key):
    """按键必须走 QApplication.sendEvent —— 直接调 widget.keyPressEvent()
    不会经过 Qt 的事件分发，shortcut/焦点状态不对，r56 的实测教训。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    ev = QKeyEvent(QEvent.Type.KeyPress, int(key),
                   Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, ev)


def _fake_rows(n, start_id=1):
    """假行：**path 必须非空**。

    Lightbox.__init__ 里 `self.rows = [r for r in rows if r.get("path")]`
    会把空 path 的行全过滤掉 —— 写 `path: ""` 会让 rows 变成空列表，
    于是四向翻页/判定全部静默失效（测出来就是「按 ←→ 没反应」，
    很容易误判成灯箱坏了，其实是自己的测试数据不合格）。
    文件本身不必真实存在：render() 对缺失文件有容错。
    """
    return [{"path": f"/no/such/img_{i}.webp", "id": start_id + i,
             "width": 8, "height": 6, "score": 0.5,
             "source_url": "https://example.com/x", "tags": ""}
            for i in range(n)]


@pytest.fixture(scope="module")
def _cache():
    return {}


def _win():
    from media_resource_manager.gui.main_window import MainWindow
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


def _code(path):
    return path.read_text(encoding="utf-8")


def _uncommented_lines(text):
    """去掉行内 # 注释后返回代码行（作业单铁律 6 之外的补充）。

    源码里满是「# 图库也有灯箱」这类**注释**，全文搜关键词会被注释误导；
    钉死行为时要按「去注释后的代码」来判。
    """
    out = []
    for line in text.splitlines():
        out.append(re.sub(r"#.*$", "", line))
    return out


# ---------------- 1) 工具栏没有「大图」按钮 ----------------
def test_gallery_toolbar_has_no_big_image_button(_cache):
    """工具栏第二排曾有 QPushButton("大图")，撤掉后一个都不能剩。

    扫全部 QPushButton 文本而不是走布局层级 —— 布局在别的分支可能被重建。
    """
    w = _cache.get("w") or _cache.setdefault("w", _win())
    from PySide6.QtWidgets import QPushButton
    btn_texts = [b.text() for b in w.findChildren(QPushButton)]
    assert "大图" not in btn_texts, (
        f"图库工具栏仍残留「大图」按钮，实际有：{btn_texts}")
    # 同义改名的也要抓
    assert not [t for t in btn_texts if t.startswith("大图")], btn_texts


def test_big_image_button_truly_deleted_from_source():
    """源码级钉死：QPushButton("大图") 整段都不在了（防有人改个名字再接回来）。"""
    code = _code(_MAIN)
    assert not re.search(r'QPushButton\(\s*"大图', code), \
        "源码里还有 QPushButton(\"大图\") 变体（可能改了名字）"
    assert not re.search(r'QPushButton\(\s*"大图查看', code)


# ---------------- 2) 右键菜单没有灯箱项 ----------------
def _func_block(path, name):
    """从磁盘源文件里切出某个方法/函数的整段代码。

    作业单铁律 6：inspect.getsource 靠 linecache、会陈旧，断言要读磁盘。
    这里还要避一个更狠的坑：**PySide6 的 C++ 类属性赋值是静默失效的**
    （`QMenu.exec = lambda ...` 不报错，但真调用仍进原生模态 exec）。
    所以想验「菜单构造了哪些项」只能读源码，绝不能真调 _gallery_menu ——
    它的最后一行是 menu.exec(...)，模态，不拦就挂死（实测挂到 timeout）。
    """
    lines = _code(path).splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"def {name}("):
            start = i
            break
    assert start is not None, f"{path.name} 里找不到 {name}"
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            end = j
            break
    return "\n".join(lines[start:end])


def test_gallery_context_menu_has_no_lightbox_action():
    """图库右键菜单里不能有「大图查看（灯箱）」。

    用**读磁盘源码切块**而不是真调 _gallery_menu()：后者的末行是
    `menu.exec(...)`（模态），而 PySide6 上给 C++ 类属性赋桩是**静默失效**的
    （QMenu.exec = lambda 不报错、但真调用仍进原生模态循环）→ 必挂死。
    """
    block = _func_block(_MAIN, "_gallery_menu")
    adds = re.findall(r'menu\.addAction\(\s*"([^"]+)"', block)
    assert adds, "没解析出任何 addAction，测试本身失效"
    # #46：右键也要有灯箱项（用户不一定记得双击）
    assert "大图查看（灯箱）" in adds, f"右键菜单缺灯箱项：{adds}"
    # ⚠「打开图片」**必须仍在**：它走系统看图器，与灯箱并存（#46 没删它）
    # 关键项还在（不能把整排菜单误删）
    for must in ("打开图片", "打开所在文件夹", "在网站中查找相似"):
        assert must in adds, f"误删了「{must}」，实际：{adds}"
    # 维护子菜单也一并查（它也是菜单项，容易漏）
    assert "清理 0kb 孤儿" in block, "维护子菜单被误删"


def test_review_context_menu_keeps_its_lightbox_entry():
    """反向钉死：审核页**不该**被这次收敛波及（它没有右键灯箱项也无所谓，
    但 main_window 里除图库外不得再有灯箱菜单项）。"""
    for name in ("_gallery_menu",):
        block = _func_block(_MAIN, name)
        assert "Lightbox" not in block, f"{name} 里还有 Lightbox 引用"


# ---------------- 3) 三处双击语义统一 ----------------
def test_gallery_grid_double_click_opens_default_viewer(_cache, monkeypatch):
    """网格双击 = 系统默认看图器（与列表/实时墙统一），不再是灯箱。

    行为级验证：真的发一次 itemDoubleClicked，看被调到的是不是
    open_image_default。**不用** signal.receivers() —— PySide6 的
    SignalInstance 没有这个属性（只有 PyQt5 有）。
    """
    w = _cache.get("w") or _cache.setdefault("w", _win())
    hits = []
    # #46：图库双击开灯箱（不再是系统看图器）
    monkeypatch.setattr(type(w), "open_lightbox",
                        lambda self, *a: hits.append(1), raising=False)
    # 信号签名是 itemDoubleClicked(QListWidgetItem*)，必须传一个参数
    from PySide6.QtWidgets import QListWidgetItem
    w.g_grid.itemDoubleClicked.emit(QListWidgetItem())
    assert hits, "网格双击没调到 open_lightbox（#46 要求图库双击=灯箱）"


def test_gallery_grid_and_list_double_click_are_consistent(_cache):
    """网格与列表双击都走 open_image_default（收敛的核心：语义统一）。"""
    w = _cache.get("w") or _cache.setdefault("w", _win())
    from media_resource_manager.gui.main_window import MainWindow
    assert callable(MainWindow.open_image_default)
    # 列表：cellDoubleClicked -> lambda 调 open_image_default
    code = _uncommented_lines(_code(_MAIN))
    joined = "\n".join(code)
    assert "self.g_table.cellDoubleClicked.connect(" in joined
    # 网格：直连 open_image_default
    assert "self.g_grid.itemDoubleClicked.connect(self.open_lightbox)" \
        in joined, "网格双击没直连 open_lightbox（#46 要求图库双击=灯箱）"
    # 【C7】原来这里还断言「实时墙也一致」（live_grid.itemDoubleClicked）。
    # 实时抓图墙已按用户要求整个移除，那条断言随之作废—— 但**不能**顺手改成
    # 断言"live_grid 还在"（部件真删了，那样测试必挂）。
    # 现在改成钉住C7 的结果：全仓库不许再有 live_grid 的双击连接残留。
    assert "live_grid.itemDoubleClicked" not in joined, \
        "抓图墙已移除，却还有 live_grid 双击连接残留（死代码）"


def test_gallery_widgets_connect_open_lightbox():
    """#46：图库双击**必须**接 open_lightbox（方法已重新加回来）。

    ⚠ 仍保留 #26 的另一半：实时墙（live_grid）已整个移除，
    不许有它的双击连接残留。
    """
    from media_resource_manager.gui.main_window import MainWindow
    assert callable(MainWindow.open_lightbox), "open_lightbox 方法不在了"
    code = _code(_MAIN)
    lines = _uncommented_lines(code)
    joined = "\n".join(lines)
    assert "open_lightbox" in joined, "图库没有任何地方接 open_lightbox"
    assert "live_grid.itemDoubleClicked" not in joined, \
        "抓图墙已移除，却还有 live_grid 双击连接残留（死代码）"


# ---------------- 4) 方法与 import 都删干净 ----------------
def test_open_lightbox_method_is_wired_to_real_lightbox():
    """#46：open_lightbox 必须真的建出 Lightbox（不是空壳方法）。

    ⚠ 判「可达」不判「出现过字样」：只搜 `Lightbox` 会被注释骗到。
    这里要求方法体里**真的调用**了 Lightbox(...) 并 exec 出来。
    """
    block = _func_block(_MAIN, "open_lightbox")
    assert "Lightbox(" in block, "open_lightbox 没有真的建 Lightbox（空壳？）"
    assert ".exec()" in block, "open_lightbox 建了灯箱却不显示（没 exec）"


# ---------------- 5) 审核页灯箱必须还在（收敛的另一半） ----------------
def test_review_tab_lightbox_still_wired_with_on_decide():
    """审核页是主流程，灯箱 + on_decide（灯箱里 → 直接「喜欢」）必须保留。

    R153 起灯箱只剩「喜欢」一条判定（「不喜欢」归网格/列表页），
    on_decide 接线本身不动。
    """
    code = _code(_REVIEW)
    lines = _uncommented_lines(code)
    assert re.search(r"^\s*from\s+\.lightbox\s+import\s+Lightbox", code,
                      re.M), "审核页不再 import Lightbox"
    assert "on_decide=on_decide" in code, "审核页灯箱丢了 on_decide（灯箱内无法判定）"
    # F19：构造多传了一个 hotkeys=（键位覆盖表）；判据跟着改成
    # 「rows/idx/None 定位参数原样 + on_decide 还在 + hotkeys 传的是 cfg 的」
    assert re.search(r"Lightbox\(rows, idx, None, on_decide=on_decide,", code), \
        "审核页灯箱构造被改坏（定位参数 / on_decide 丢了）"
    assert re.search(r"hotkeys=getattr\(self\.cfg, \"hotkeys\", None\)", code), \
        "审核页灯箱没把 cfg.hotkeys 传进去（F19 改键在审核灯箱里不生效）"


def test_review_open_file_still_opens_lightbox(_cache):
    """跑真代码：审核页 open_file 仍打开灯箱（不是被这次收敛误伤）。"""
    from PySide6.QtWidgets import QDialog
    from media_resource_manager.gui.lightbox import Lightbox

    opened = []

    class _Spy(Lightbox):
        def __init__(self, *a, **k):
            opened.append((a, k))
            # 不真建 QDialog，避免 exec 阻塞；只验参数

    import media_resource_manager.gui.review_tab as rt
    assert hasattr(rt.ReviewTab, "open_file"), "审核页没有 open_file"

    # 只验签名链路，不真弹窗
    src = inspect.getsource(rt.ReviewTab.open_file)
    assert "Lightbox(" in src, "审核页 open_file 不再打开灯箱"
    assert "on_decide=on_decide" in src
    assert QDialog is not None  # 灯箱基类未被替换


# ---------------- 6) 纯浏览模式仍可用 ----------------
def test_lightbox_browse_mode_still_works(qapp):
    """灯箱组件本身没坏：on_decide=None 时四向翻页 + 幻灯片。"""
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.lightbox import Lightbox

    rows = _fake_rows(3)
    lb = Lightbox(rows, 0, None)
    try:
        _send(lb, Qt.Key.Key_Right); assert lb.i == 1
        _send(lb, Qt.Key.Key_Down); assert lb.i == 2
        _send(lb, Qt.Key.Key_Left); assert lb.i == 1
        _send(lb, Qt.Key.Key_Up); assert lb.i == 0
        # 幻灯片仍可用
        assert lb.toggle_slideshow() is True
        assert lb.is_slideshow_running() is True
        assert lb.toggle_slideshow() is False
    finally:
        lb.close()


def test_lightbox_decide_mode_still_works(qapp):
    """on_decide 非空（审核页模式，R153 起）：→ 直接判定「喜欢」；← 只翻页。"""
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.review_tab import ReviewDecision
    from media_resource_manager.gui.lightbox import Lightbox

    rows = _fake_rows(3)
    seen = []
    lb = Lightbox(rows, 0, None,
                  on_decide=lambda d, r: seen.append((d, r["id"])))
    try:
        _send(lb, Qt.Key.Key_Right)
        assert seen == [(ReviewDecision.LIKE, 1)], seen
        # ← 只翻页（R153：灯箱去掉「不喜欢」）
        _send(lb, Qt.Key.Key_Left)
        assert lb.i == 0, lb.i
        assert seen == [(ReviewDecision.LIKE, 1)], seen
        _send(lb, Qt.Key.Key_Up)          # 只翻页（环绕回第 2 张）
        assert lb.i == 2, lb.i
        assert len(seen) == 1
    finally:
        lb.close()


# ---------------- 7) 顶级菜单数量不变（r45 教训） ----------------
def test_top_level_menu_count_unchanged(_cache):
    """#26 是「从图库撤掉」，不是新增顶级菜单 —— 守住 verify_r11 的 8 个基线。"""
    w = _cache.get("w") or _cache.setdefault("w", _win())
    names = [a.text() for a in w.menuBar().actions()]
    # 【V0.0.19 改锁】8 → 5（用户拍板推倒重排）；图库/隔离区走左侧导航。
    assert len(names) == 5, f"顶级菜单数被改动：{names}"
    assert "工具" in names and "视图" in names


# ---------------- 8) 文档同步（别让说明书骗用户） ----------------
def test_docs_do_not_claim_gallery_lightbox():
    """⚠ 判据方向已被 #46 反过来 —— 现在钉的是**旧的 #26 说法残留**。

    #26 曾要求「灯箱只在审核页、图库撤掉」，#46 又把图库双击接回灯箱
    （Web 与桌面统一），所以今天**正确的**文档写法恰恰是「图库、审核页
    都有灯箱」。这条守的不再是"别写图库有灯箱"，而是"别再把过时的
    #26 口径写回说明书骗用户"：

      · 不许出现「灯箱只在审核页」/「图库…用默认看图软件打开」这类句子；
      · 图库工具栏的「大图（灯箱）」**按钮**至今仍是撤掉的（另一条测）。
    """
    docs = Path(__file__).resolve().parents[1] / "docs"
    # 过时口径的特征串（#46 之后写这些就是骗人）
    stale_marks = ("灯箱只在审核页", "灯箱只在审核", "图库用默认看图软件打开")
    offenders = []
    for md in sorted(docs.glob("*.md")):
        for i, line in enumerate(
                md.read_text(encoding="utf-8").splitlines(), 1):
            if any(mk in line for mk in stale_marks):
                offenders.append(f"{md.name}:{i}: {line.strip()[:70]}")
    assert not offenders, "文档仍写着 #26 时代的过时口径（图库没有灯箱）：\n" + "\n".join(offenders)


def test_f1_help_text_does_not_claim_gallery_lightbox():
    """F1 快捷键说明必须跟着改口径。

    这是最容易漏的一处：F1 弹窗里原本写「灯箱（双击缩略图打开）」+
    「双击缩略图 灯箱 / 用默认看图软件打开」，#26 之后图库已无灯箱，
    不改就是**用户照着 F1 找不到灯箱**。

    F19 起审核页/灯箱/任务三段改成按当前生效键位**现场拼**（源码里是
    f-string + _k('action_id')），所以「+ / - / 0」「F 」这类字面量
    不再出现在源码里——判据随之改为「这些动作仍然被 F1 引用」
    （意图不变：新能力必须写进 F1，否则用户不知道有缩放/全屏）。
    """
    block = _func_block(_MAIN, "show_shortcuts")
    # #46：F1 必须说图库双击也开灯箱（否则用户照着 F1 找不到灯箱）
    assert "图库" in block, "F1 灯箱段没提图库"
    assert "灯箱" in block, "F1 里没有灯箱说明"
    # 新增能力（28~31 / F23 幻灯片 / F19 任务键）也要写进 F1，否则用户不知道
    for aid in ("light.zoom_in", "light.zoom_out", "light.zoom_reset",
                "light.fullscreen", "light.rotate", "light.slideshow",
                "task.start", "task.stop"):
        assert aid in block, f"F1 没引用动作 {aid!r}（键位说明不完整）"
    assert "缩放" in block and "全屏" in block, "F1 没写缩放/全屏说明"


def test_docs_gallery_row_no_longer_lists_big_image():
    """图库操作第二排的清单里不许再出现「大图（灯箱）」。"""
    docs = Path(__file__).resolve().parents[1] / "docs"
    for md in sorted(docs.glob("*.md")):
        for i, line in enumerate(
                md.read_text(encoding="utf-8").splitlines(), 1):
            if "大图（灯箱）" not in line:
                continue
            if any(mk in line for mk in ("原本", "已撤", "撤掉", "#26")):
                continue
            raise AssertionError(
                f"{md.name}:{i} 仍把「大图（灯箱）」列为图库现有按钮："
                f"{line.strip()[:70]}")
