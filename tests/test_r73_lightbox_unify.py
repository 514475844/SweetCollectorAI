# -*- coding: utf-8 -*-
"""R73 · 第 46 项（图库双击 → 灯箱，Web 与桌面端统一）+ 第 24/28~31 项能力。

用户原话（TONIGHT_FINAL_ORDER_PART2 第 46 项）：
  「图库中**双击图片**要以**灯箱**方式打开大图（不是直接下载、不是新标签页裸图）。
    Web 与桌面端行为**统一为灯箱**；灯箱内沿用主单第 24/28–31 项能力
    （左右翻页、缩放、全屏、下载原图、打开源网页、键盘 ← → / Esc）。」

本文件钉的不变量（每条对应一种「做漏了 / 做歪了 / 做成空壳」）：
  1. 桌面端：图库网格与列表双击都进 `open_lightbox`，且它**真的**建 Lightbox。
  2. `open_image_default`（系统看图器）**仍在** —— #46 没删它，两条路并存。
  3. 灯箱能力齐：缩放有上下限夹紧（不夹 = 连点放大能把内存吃光）。
  4. ⚠ 换图必须复位缩放（否则「上一张放大 4×，下一张一开就是裁切」）。
  5. ⚠ 键位铁律：← → 恒为翻页；缩放走 + - 0、全屏走 F，都不许复用 ← →。
     这是 #56 D21/D22 的老铁律，#46 加功能时最容易破的就是这条。
  6. 全屏下 Esc 先退全屏、再按才关窗（否则用户「Esc 退全屏」结果窗没了）。
  7. Web 端：`/orig/<id>` 带 `Content-Disposition: attachment`（下载原图），
     且**原图与缩略图是两个 URL**（以前 full_url 也指 /thumb/ ⇒ 放大是糊的）。
  8. Web 端灯箱有：翻页 / 缩放夹紧 / 全屏 / 下载 / 源页 / 键盘 ← → Esc。
  9. Web 端 dblclick 进的是 `openLightbox`（不是裸开新标签页）。
 10. 两端口径统一：桌面与 Web 都有「翻页 + 缩放 + 全屏 + 下载 + 源页」。

⚠ 判据纪律（R63-D3 铁律）：
  · 断言「关系」不「数值」—— 倍率是「量」，钉 1.25 就会假失败；
  · 判「可达」不判「出现过字面量」—— 源码里满是注释；
  · **判据与被护对象同源**：Web 灯箱的缩放夹紧真从 app.js 里抠出来跑，
    不是在测试里重写一遍 clamp（那就是 R72 踩过的空壳判据）。
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
_MAIN = _ROOT / "media_resource_manager" / "gui" / "main_window.py"
_LB = _ROOT / "media_resource_manager" / "gui" / "lightbox.py"
_TV = _ROOT / "media_resource_manager" / "gui" / "table_view.py"
_WEBUI = _ROOT / "media_resource_manager" / "webui.py"
_APPJS = _ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
_APPCSS = _ROOT / "media_resource_manager" / "web" / "static" / "css" / "app.css"
_INDEX = _ROOT / "media_resource_manager" / "web" / "index.html"


def _code(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _uncommented(text: str) -> str:
    """去掉整行 # 注释（源码里大量注释在讲「以前错在哪」，会骗到字面量搜索）。"""
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


def _func_block(path: Path, name: str) -> str:
    """从磁盘源文件切出某个方法的整段（inspect.getsource 会陈旧，R55 教训）。"""
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


def _func_block_from(text: str, name: str) -> str:
    """从**已处理过的源码文本**里切出某个方法段（配 _code_no_docstring 用）。"""
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"def {name}("):
            start = i
            break
    assert start is not None, f"找不到 {name}"
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for j in range(start + 1, len(lines)):
        ln = lines[j]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent:
            end = j
            break
    return "\n".join(lines[start:end])


@pytest.fixture(autouse=True)
def qapp(monkeypatch):
    """任何 Qt 控件前必须有 QApplication；弹窗一律打桩（真弹会 exec 挂死）。"""
    from PySide6.QtWidgets import QDialog, QMessageBox
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    app = QApplication.instance() or QApplication([])
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    return app


def _send(widget, key):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    ev = QKeyEvent(QEvent.Type.KeyPress, int(key), Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, ev)


def _rows(n):
    """假行：path 必须非空（Lightbox.__init__ 会滤掉空 path 的行）。"""
    return [{"path": f"/no/such/img_{i}.webp", "id": i + 1,
             "width": 800, "height": 600, "score": 0.5,
             "source_url": f"https://example.com/p{i}", "tags": ""}
            for i in range(n)]


# ══════════════════════════════════════════════════════════════════
# 1) 桌面端：图库双击进灯箱
# ══════════════════════════════════════════════════════════════════
def test_gallery_grid_double_click_opens_lightbox(monkeypatch):
    """行为级：真 emit 一次 itemDoubleClicked，看调到的是不是 open_lightbox。"""
    from media_resource_manager.gui.main_window import MainWindow
    from PySide6.QtWidgets import QListWidgetItem
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        hits = []
        monkeypatch.setattr(type(w), "open_lightbox",
                            lambda self, *a: hits.append(1), raising=False)
        w.g_grid.itemDoubleClicked.emit(QListWidgetItem())
        assert hits, "网格双击没进 open_lightbox（第 46 项要求图库双击=灯箱）"
    finally:
        w.hide()
        w.deleteLater()


def test_gallery_grid_and_list_both_wired_to_lightbox():
    """网格与列表两处双击语义必须一致（#26 的教训：同页两种语义）。"""
    joined = _uncommented(_code(_MAIN))
    assert "self.g_grid.itemDoubleClicked.connect(self.open_lightbox)" in joined, \
        "网格双击没直连 open_lightbox"
    # 列表是 lambda 包一层（cellDoubleClicked 带 row/col 两个参数）
    m = re.search(r"self\.g_table\.cellDoubleClicked\.connect\((.*?)\)\n",
                  joined, re.S)
    assert m, "列表双击连接不见了"
    assert "open_lightbox" in m.group(1), \
        f"列表双击没进 open_lightbox：{m.group(1)[:80]}"


def test_open_lightbox_is_not_a_shell():
    """⚠ 判「可达」不判「出现过字面量」：必须真的建 Lightbox 并显示。"""
    block = _func_block(_MAIN, "open_lightbox")
    assert "Lightbox(" in block, "open_lightbox 没真的建 Lightbox（空壳？）"
    assert re.search(r"\.exec\(\)", block), "建了灯箱却不显示（没 exec）"
    # 翻页范围必须是「当前筛选后的全集」而不是单张
    assert "_gal_rows" in block, \
        "灯箱翻页范围没取当前列表全集（按 ← 到底就没反应）"


def test_open_image_default_still_exists():
    """#46 **没有**删掉系统看图器那条路 —— 右键「打开图片」仍用它。

    钉这条是为了防「顺手把 open_image_default 一起删了」：
    那是审核/图库之外的另一条路（要在别的软件里编辑原图时用）。
    """
    from media_resource_manager.gui.main_window import MainWindow
    assert callable(MainWindow.open_image_default)
    block = _uncommented(_code(_MAIN))
    assert 'menu.addAction("打开图片", self.open_image_default)' in block


# ══════════════════════════════════════════════════════════════════
# 3) 缩放：必须有上下限夹紧
# ══════════════════════════════════════════════════════════════════
def test_preview_zoom_is_clamped():
    """⚠ 缩放必须夹在 [MIN_ZOOM, MAX_ZOOM]。

    不夹的后果不是「不好看」，是**连点放大几十次就能让 QPixmap 申请到
    几 GB** —— 灯箱一开就把内存吃光，用户看到的是程序直接没了。
    """
    from media_resource_manager.gui.table_view import PreviewLabel
    lb = PreviewLabel("x")
    try:
        lo, hi = PreviewLabel.MIN_ZOOM, PreviewLabel.MAX_ZOOM
        assert 0 < lo < hi, f"缩放区间不合理：{lo}..{hi}"
        # 连点 100 次放大，仍不许超过上限
        z = 1.0
        for _ in range(100):
            z = lb.set_zoom(z * PreviewLabel.ZOOM_STEP)
        assert z <= hi + 1e-9, f"放大没夹住：{z} > {hi}"
        # 连点 100 次缩小，仍不许低于下限（更糟：负数会让 scaled() 抛异常）
        for _ in range(100):
            z = lb.set_zoom(z / PreviewLabel.ZOOM_STEP)
        assert z >= lo - 1e-9, f"缩小没夹住：{z} < {lo}"
        # 复位回 1.0
        assert lb.zoom_reset() == 1.0
    finally:
        lb.deleteLater()


def test_lightbox_zoom_keys_change_zoom_but_not_page():
    """⚠ 键位铁律：+ - 0 改缩放，**且不许顺带翻页**。

    ⚠ 只断言**关系**（放大后 > 原值、复位后 == 1.0），不钉具体倍率
    —— 倍率是「量」不是契约，钉死 1.25 就会在「步进微调」时集体假失败。
    """
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(_rows(3), 1, None)
    try:
        i0, z0 = lb.i, lb.zoom()
        _send(lb, Qt.Key.Key_Plus)
        assert lb.i == i0, "按 + 却翻页了（键位语义被破坏）"
        assert lb.zoom() > z0, "按 + 缩放没变大"
        z1 = lb.zoom()
        _send(lb, Qt.Key.Key_Minus)
        assert lb.zoom() < z1, "按 - 缩放没变小（单调性反了）"
        assert lb.i == i0, "按 - 却翻页了"
        _send(lb, Qt.Key.Key_0)
        assert lb.zoom() == 1.0, "按 0 没复位成适应窗口"
        assert lb.i == i0, "按 0 却翻页了"
    finally:
        lb.close()
        lb.deleteLater()


def test_lightbox_arrow_keys_still_only_page():
    """⚠ #56 D21 铁律未被 #46/R153 破坏：纯浏览灯箱 ← → 恒为翻页。

    纯浏览灯箱里 ← → 是翻页；审核页灯箱（on_decide 非空）里 → 是「喜欢」、
    ← 是翻页（R153 去掉「不喜欢」）—— 两条都只做翻页/判定，**不做缩放**。
    """
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(_rows(3), 0, None)
    try:
        z0 = lb.zoom()
        _send(lb, Qt.Key.Key_Right)
        assert lb.i == 1, "→ 不翻页"
        assert lb.zoom() == z0, "→ 把缩放也改了（绝不允许）"
        _send(lb, Qt.Key.Key_Left)
        assert lb.i == 0, "← 不翻页"
        assert lb.zoom() == z0, "← 把缩放也改了（绝不允许）"
    finally:
        lb.close()
        lb.deleteLater()


def test_lightbox_decide_mode_arrows_still_decide():
    """审核页灯箱（R153 起）：→ 喜欢；← 恢复「上一张」翻页，不再判「不喜欢」。

    用户裁决：灯箱里的「不喜欢」直接去掉（网格/列表页保留）。
    """
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.review_tab import ReviewDecision
    from media_resource_manager.gui.lightbox import Lightbox
    seen = []
    lb = Lightbox(_rows(3), 0, None,
                  on_decide=lambda d, r: seen.append((d, r["id"])))
    try:
        _send(lb, Qt.Key.Key_Right)
        assert seen == [(ReviewDecision.LIKE, 1)], seen
        # ← 只翻页（R153）：从第 1 张退到第 0 张，不判定
        _send(lb, Qt.Key.Key_Left)
        assert lb.i == 0, lb.i
        assert seen == [(ReviewDecision.LIKE, 1)], (
            f"← 在审核页灯箱只翻页不判定，实际 {seen}")
    finally:
        lb.close()
        lb.deleteLater()


# ══════════════════════════════════════════════════════════════════
# 4) 换图复位缩放
# ══════════════════════════════════════════════════════════════════
def test_paging_resets_zoom():
    """⚠ 换图必须复位缩放。

    不复位的症状：用户把上一张放到 8×，按 → 看下一张 —— 打开就是
    「一大块局部」，看起来像图坏了。这是最容易挨骂的一条。
    """
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(_rows(3), 0, None)
    try:
        for _ in range(6):
            lb.zoom_in()
        assert lb.zoom() > 1.0, "前置条件不成立：没放大起来"
        lb.step(1)
        assert lb.zoom() == 1.0, \
            f"换图后缩放没复位（zoom={lb.zoom()}）—— 下一张会显示成裁切"
    finally:
        lb.close()
        lb.deleteLater()


def test_render_resets_zoom_even_without_step():
    """render() 自己也复位（不只 step）—— 缩放后重画路径同样要复位。"""
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(_rows(2), 0, None)
    try:
        lb.zoom_in()
        lb.zoom_in()
        assert lb.zoom() > 1.0
        lb.render()
        assert lb.zoom() == 1.0, "render() 没复位缩放"
    finally:
        lb.close()
        lb.deleteLater()


# ══════════════════════════════════════════════════════════════════
# 6) 全屏：Esc 先退全屏
# ══════════════════════════════════════════════════════════════════
def test_escape_exits_fullscreen_before_closing():
    """⚠ 全屏下 Esc 只退全屏，**不关窗**。

    否则用户「按 Esc 退出全屏」结果整个灯箱消失，下次打开又是全屏，
    体验上像 bug 复发。
    """
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox(_rows(2), 0, None)
    try:
        lb.show()
        lb.toggle_fullscreen(True)
        assert lb.is_fullscreen(), "前置条件不成立：没进全屏"
        _send(lb, Qt.Key.Key_Escape)
        assert not lb.is_fullscreen(), "Esc 没退出全屏"
        assert lb.isVisible(), "Esc 退出全屏时把灯箱也关掉了（应只退全屏）"
    finally:
        lb.close()
        lb.deleteLater()


def test_fullscreen_state_uses_window_state_flag():
    """判全屏用 windowState 的标志位 —— offscreen 下几何/isVisible 不可信（R58）。"""
    block = _func_block(_LB, "is_fullscreen")
    assert "windowState" in block, "没读 windowState（offscreen 下不可信的几何不能用）"
    assert "WindowFullScreen" in block, "没比对 WindowFullScreen 标志"


# ══════════════════════════════════════════════════════════════════
# 7) Web 端：/orig 下载原图 + 原图≠缩略图
# ══════════════════════════════════════════════════════════════════
def _code_no_docstring(path: Path) -> str:
    """⚠ 去掉注释与 docstring 后的源码。

    R63-M5 / R63-D3 复发点：`_serve_original` 的 **docstring 里就写着
    「Content-Disposition: attachment」** ⇒ 只搜 "attachment" 会被自己的
    文档骗到，变异把实现改成 inline 照样全绿（M5 变异就是这样漏掉的）。
    """
    import ast
    tree = ast.parse(_code(path))
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            d = ast.get_docstring(node, clean=False)
            if d is not None:
                b = node.body[0]
                doc_nodes.add((b.lineno, b.end_lineno))
    lines = _code(path).splitlines()
    keep = []
    for i, ln in enumerate(lines, 1):
        if any(lo <= i <= hi for lo, hi in doc_nodes):
            continue
        keep.append(re.sub(r"#.*$", "", ln))
    return "\n".join(keep)


def test_web_orig_route_sends_attachment():
    """下载原图必须带 Content-Disposition: attachment。

    不带的话浏览器只会在当前页把图打开一下就完事 —— 用户点了
    「下载原图」什么都没发生，最典型的「点了没反应」。

    ⚠ 判据打在**去 docstring + 去注释**的源码上：`_serve_original` 的
    docstring 里就含 "attachment" 字样，早先那版直接搜关键词 ⇒ 变异把
    实现改成 inline 也照样绿（M5 变异实测漏过）。
    """
    body = _code_no_docstring(_WEBUI)
    blk = _func_block_from(body, "_serve_original")
    assert "attachment" in blk, \
        "/orig 没带 attachment（点了不会存盘，只是打开一下）"
    assert "inline" not in blk, "实现里出现了 inline（等于不下载）"
    # 中文文件名要按 RFC 5987 编码，否则老浏览器存成乱码
    assert "filename*=UTF-8''" in blk, "没做 RFC 5987 文件名编码（中文会乱码）"


def test_web_orig_route_is_registered():
    """路由必须真注册（源码里有方法 ≠ 路由通）。"""
    joined = _uncommented(_code(_WEBUI))
    assert 'path.startswith("/orig/")' in joined, "/orig 路由没接上"
    assert "_serve_original(u)" in joined, "/orig 路由没调 _serve_original"


def test_web_thumb_and_orig_are_different_urls():
    """⚠ 原图与缩略图必须**是两个 URL**，且 `orig_url` 这个键**本身**指向 /orig/。

    以前 full_url 和 url 都指 /thumb/ ⇒ 灯箱「放大」放大的是缩略图，
    用户看到一片糊，还以为图源坏了。

    ⚠ 早先那版只判「块里出现过 /orig/」—— 而 `full_url` 里也有 /orig/，
    所以把 `orig_url` 改成 /thumb/ 照样绿（M6 变异实测漏过）。
    正解：**按键取值**再判，而不是判「出现过某个字符串」。
    """
    import ast
    block = _func_block(_WEBUI, "_img_item")
    fn = ast.parse(_code(_WEBUI))
    target = None
    for node in ast.walk(fn):
        if isinstance(node, ast.FunctionDef) and node.name == "_img_item":
            target = node
            break
    assert target is not None, "找不到 _img_item"
    d = None
    for node in ast.walk(target):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            d = node.value
            break
    assert d is not None, "_img_item 没有直接 return 一个 dict"
    got = {}
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            got[k.value] = ast.unparse(v)
    assert "thumb_url" in got, f"thumb_url 键没了：{sorted(got)}"
    assert "orig_url" in got, f"orig_url 键没了：{sorted(got)}"
    assert "/thumb/" in got["thumb_url"], f"thumb_url 不是缩略图：{got['thumb_url']}"
    assert "/orig/" in got["orig_url"], \
        f"orig_url 指向的不是原图（{got['orig_url']}）⇒ 灯箱放大出来是糊的"
    # 缩略图与原图必须真的**不同**
    assert got["orig_url"] != got["thumb_url"], "原图与缩略图是同一个 URL"
    assert block  # 保留：确认切块本身没失效


def test_web_image_payload_has_source_and_size():
    """第 30/37 项：来源网址 + 尺寸 + 采集时间要下发给前端。"""
    block = _func_block(_WEBUI, "_img_item")
    for k in ("source_url", "width", "height", "collected_at"):
        assert f'"{k}"' in block, f"图片负载缺 {k}（灯箱信息栏/打开源页要用）"


def test_web_images_query_selects_new_columns():
    """⚠ 判据与被护对象同源：SELECT 真要带上这些列。

    只在 _img_item 里读 r["source_url"] 而 SELECT 不取 ⇒ 运行期
    KeyError ⇒ 前端整页图片崩。这类「加了字段忘了加查询」只有真跑 /
    真查库才看得见（R50：业务方法单测结构上抓不到）。

    ⚠ 判据写成「把该 SELECT 的列抠出来逐个查」，**不是**匹配整段文本：
    早先那版按换行+前缀精确匹配，产品只是调了缩进就假红（仪器错，R53）。
    """
    # ⚠ 必须在 **_api_images 方法块内**找：全文件里有多条 SELECT…FROM images，
    # 非贪婪匹配会先撞上别处那条（cols 抠出来只有 'path'）——仪器错，R53。
    body = _func_block(_WEBUI, "_api_images")
    # ⚠ 先把 f-string 的**字面量拼接**摊平再匹配：源码里这条 SQL 是
    # 4 段 f"..." 拼起来的（直接跨段匹配 ⇒ 恒 None）。
    flat = re.sub(r'f?"([^"]*)"', r"\1", body)
    m = re.search(r"SELECT\s+(?P<cols>.*?)\s+FROM images\s+WHERE", flat, re.S)
    assert m, "找不到图库分页查询（SELECT 列抠不出来）"
    cols = m.group("cols")
    for c in ("id", "path", "tags", "score", "blacklisted",
              "in_quarantine", "needs_review", "favorite",
              "source_url", "width", "height", "bytes", "collected_at"):
        assert re.search(rf"\b{c}\b", cols), \
            f"图库 SELECT 没取 {c}（_img_item 会 KeyError ⇒ 前端整页图崩）"


# ══════════════════════════════════════════════════════════════════
# 8) Web 端灯箱能力齐
# ══════════════════════════════════════════════════════════════════
def _js():
    return _code(_APPJS)

