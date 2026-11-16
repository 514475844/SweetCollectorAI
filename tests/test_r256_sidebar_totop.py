"""r256 护栏：2026-10-29 两条用户反馈（都是"写了但没生效"型）。

1) 右下角「回到顶部」按钮用户根本没见到过。
   根因不是没写，是**监听挂错了时机**：boot() 里 `ready.value = true` 之后
   立刻 `mainEl.value.addEventListener(...)`，但 Vue 的 DOM 更新在下一个
   tick，那一刻 <main> 还没渲染，mainEl 是 null，监听器一次都没挂上 →
   showTop 永远是 false → 按钮永远不出现。
   判据：滚动监听必须绑在**模板**上（@scroll），并额外用 window 捕获阶段兜底。

2) 侧栏点「收起」后回不来。
   收起按钮本身在侧栏里（v-if="!sideHide"），侧栏 display:none 后按钮跟着消失；
   唯一的替代入口是底栏最右侧一个灰色小箭头，用户根本没看见 → 体感是
   "缩回去就出不来了"。
   判据：收起后必须在**原来侧栏所在的左边缘**出现一个显式「展开」把手。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager/webapp/src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ------------------------------------------------------- 1 回到顶部
def test_main_scroll_listener_is_on_template():
    a = _read("App.vue")
    # 监听必须写在 <main> 上：渲染出来就有，不存在"挂载时机"问题
    assert "@scroll.passive=\"onMainScroll\"" in a
    # 原来那句 addEventListener 会命中 null（ready=true 时 DOM 还没打补丁），必须不再出现
    assert "mainEl.value.addEventListener('scroll'" not in a


def test_main_scroll_window_capture_fallback():
    """scroll 事件不冒泡，捕获阶段才能收到后代滚动 —— 页面换滚动容器也不会失灵。"""
    a = _read("App.vue")
    assert "{ capture: true, passive: true }" in a
    assert "removeEventListener('scroll', onMainScroll, { capture: true })" in a


def test_totop_button_and_threshold():
    a = _read("App.vue")
    assert 'class="totop"' in a and 'aria-label="回到顶部"' in a
    # 左下角那种"一进页面就挂个按钮"很碍眼：滚过 240px 且内容确实超过一屏才给
    assert "top > 240" in a and "max > 300" in a
    css = a.split(".totop {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in css and "right:" in css          # 右下角
    assert "bottom:" in css                                      # 压在底栏之上


def test_totop_hides_after_route_change():
    """从长页面跳到短页面时，残留的 showTop=true 会留一个点了没反应的按钮。"""
    a = _read("App.vue")
    assert "watch(() => route.fullPath" in a
    assert "requestAnimationFrame(() => onMainScroll())" in a


# ------------------------------------------------------- 2 侧栏收起后能回来
def test_sidebar_expand_handle_exists():
    a = _read("App.vue")
    # 收起后才出现的把手，贴在左边缘（侧栏原来的位置）
    assert '<button v-if="sideHide" class="side-rail"' in a
    assert "<span>展开</span>" in a
    css = a.split(".side-rail {", 1)[1].split("}", 1)[0]
    assert "position: fixed" in css and "left: 0" in css
    assert "var(--brand)" in css          # 主色，不是灰色小箭头那种看不见的


def test_sidebar_expand_handle_toggles():
    a = _read("App.vue")
    seg = a.split('class="side-rail"', 1)[1].split(">", 1)[0]
    assert "@click=\"toggleSide\"" in seg


def test_sidebar_collapse_entries_still_exist():
    """侧栏折叠入口保留，但**不在状态栏里**了。

    V0.0.39 用户定稿：底栏那两个小于号换成「完全退出」（原话「把右下角的那
    两个小于号缩进，改成完全退出」），折叠改由左上角品牌行那颗按钮 + Ctrl+B
    负责；收起之后左边缘另有竖排「展开」把手，不会缩进去出不来。
    这里钉的就是这三条路都还在。
    """
    a = _read("App.vue")
    assert 'class="side-btn"' in a and "toggleSide" in a, "左上角折叠按钮没了"
    assert "web.side" in _read("uiPrefs.js"), "Ctrl+B 的折叠键位没了"
    assert 'class="side-rail"' in a, "收起后的「展开」把手没了"
    # 底栏那颗按钮的位置现在归「退出软件」，不再是折叠
    seg = a.split('<div class="sb-right">', 1)[1].split("</footer>", 1)[0]
    assert 'class="quit-btn"' in seg and "askQuit" in seg
