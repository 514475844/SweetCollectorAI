"""V0.0.24 护栏：左侧导航栏必须「一屏装下全部」，不许再出现滚动条。

起因（用户原话）：
    「软件首页左边导航栏根本显示不全，还要向下滚动才能显示全。这个必须得
      修改。这边就应该永远是全部显示，包括底下的导出维护设置，还有任务图库
      待审核，换皮肤。」

根因：15 个一级项（行高 40）+ 任务 7 个状态（行高 30）= 22 行占 884px，
加品牌区、三宫格统计、皮肤区与内外边距共 1183px，而 753 高的窗口视口
只有 723px —— 差 460px，怎么排都要滚。

这份护栏钉三件事（钉关系不钉旧值，改尺寸时只要预算够就放行）：
  1. 侧栏内容的「高度预算」必须 ≤ 700px（753 视口下留 23px 余量）；
  2. 窗口最小高度锁在 753（低于它侧栏就要滚，用户不接受）；
  3. 侧栏不许再长出「界面皮肤」这类占行的标签行（改成图标按钮 = 一行）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"
APP = WEB / "App.vue"
SHELL = ROOT / "media_resource_manager" / "gui" / "web_shell.py"

# 753 视口 − 30 状态栏 = 723 可用；留 23 给字体渲染差异与缩放取整。
BUDGET = 700


def _css(sel: str) -> str:
    """取选择器的声明块（只认单选择器，够用）。"""
    src = APP.read_text(encoding="utf-8")
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", src)
    assert m, f"App.vue 里找不到样式块 {sel}"
    return m.group(1)


def _px(block: str, prop: str, default: float = 0.0) -> float:
    """从声明块里取某个属性的第一个像素值。"""
    m = re.search(rf"(?:^|;|\s){re.escape(prop)}\s*:\s*([^;]+);", block)
    if not m:
        return default
    v = re.search(r"(-?\d+(?:\.\d+)?)px", m.group(1))
    return float(v.group(1)) if v else default


def _array_block(name: str) -> str:
    """取 `const <name> = [ ... ]` 之间的内容（只数这一个数组里的条目）。"""
    src = APP.read_text(encoding="utf-8")
    i = src.index(f"const {name} = [")
    j = src.index("\n]", i)
    return src[i:j]


def test_sidebar_fits_in_one_screen():
    # 一级导航项数（navs 数组里每个 `{ path: '...'`）
    nav_rows = len(re.findall(r"\{\s*path:\s*'/[a-z]+'", _array_block("navs")))
    # 任务状态子项数（TASK_VIEWS 里每个 `{ k: '...'`）
    sub_rows = len(re.findall(r"\{\s*k:\s*'[a-z]*'", _array_block("TASK_VIEWS")))
    assert nav_rows == 15, f"一级导航项变成 {nav_rows} 个，请同步核对高度预算"
    assert sub_rows == 7, f"任务状态子项变成 {sub_rows} 个，请同步核对高度预算"

    side = _css(".side")
    pad = _px(side, "padding-top") + _px(side, "padding-bottom")
    gap = _px(side, "gap")

    nav_h = _px(_css(".nav"), "height")
    sub_h = _px(_css(".subitem"), "height")
    brand_h = _px(_css(".brand"), "height")
    theme_h = _px(_css(".theme-box .theme"), "height")

    stats = _css(".stats")
    stats_h = (_px(stats, "padding-top") + _px(stats, "padding-bottom")
               + 16)                       # 11px 字的一行 ≈ 16px

    # 导航区：主项之间 gap 2、子项之间 gap 1，子项上下各留 1 + 2
    nav_block = (nav_rows * nav_h + (nav_rows - 1) * _px(_css("nav"), "gap")
                 + sub_rows * sub_h + (sub_rows - 1) * 1 + 3)

    total = brand_h + nav_block + stats_h + (theme_h + 6) + pad + gap * 2
    assert total <= BUDGET, (
        f"侧栏内容高 {total:.0f}px 超出预算 {BUDGET}px（753 窗口下会出滚动条）。"
        " 要么压行高，要么把某一项挪走，别只把窗口往上调。")


def test_window_min_height_is_753():
    src = SHELL.read_text(encoding="utf-8")
    assert "min_h = 753" in src, "窗口最小高度没锁在 753（侧栏会被压到需要滚动）"
    assert "setMinimumSize(1024, min_h)" in src, "没把最小尺寸真正应用到窗口上"


def test_skin_row_is_one_line_without_label():
    app = APP.read_text(encoding="utf-8")
    assert "theme-label" not in app, "皮肤区又出现了占一行的标签（会把侧栏顶高）"
    box = _css(".theme-box")
    assert "display: flex" in box, "皮肤区应是一行（下拉 + 图标按钮并排）"
    # V0.0.27：品牌区改成「裁掉透明边的字标」按 92px 宽显示 —— 原来 26px 的
    # 方框里字只有 26×9 像素（用户原话「啥也看不到」），所以不再靠 Sweet 文字
    # 撑视觉，改成钉「字标宽度够大 + 用的是裁边后的图」。
    assert "/brand/wordmark.png" in app, "侧栏品牌区没用裁掉透明边的字标"
    bimg = _css(".brand img")
    assert _px(bimg, "width") >= 60, (
        f"品牌字标宽度只有 {_px(bimg, 'width')}px —— 用户明确说太小、啥也看不到")


def test_prototype_matches_shipped_sizes():
    """原型与真机必须同一套尺寸，否则「看图定方案」会失真。"""
    proto = ROOT / "tools" / "build_proto_sidebar.py"
    if not proto.exists():
        return
    src = proto.read_text(encoding="utf-8")
    for need in ("height: 25px", "height: 20px", "width: 208px"):
        assert need in src, f"原型里缺少 {need}（与真机不一致）"
