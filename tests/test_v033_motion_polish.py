"""V0.0.33「交互质感」护栏。

这一轮只加反馈、不动功能，真正容易退回去的是两条**看不出来的暗坑**：
  ① 页面进入若用 transform 且让 transform 常驻，页面里的吸底保存条
     （position: sticky/fixed）会改以 .page 为包含块，整条跑偏；
  ② 遮罩 animation 若带 fill-mode，播完仍用动画值压住 opacity，
     <Transition> 的离场淡出会被吃掉，弹窗又变成「啪」地消失。
判据钉的是关系，不钉具体秒数或颜色值。
"""
from pathlib import Path
import re

SRC = Path(__file__).resolve().parents[1] / "media_resource_manager" / "webapp" / "src"


def _read(rel: str) -> str:
    p = SRC / rel
    assert p.is_file(), f"缺少前端文件 {rel}"
    return p.read_text(encoding="utf-8")


def _keyframes(css: str, name: str) -> str:
    """取整个 @keyframes 块 —— 里面 from/to 自带花括号，不能非贪婪到第一个 }。"""
    head = re.search(r"@keyframes\s+" + re.escape(name) + r"\s*\{", css, re.S)
    assert head, f"缺少关键帧 {name}"
    depth, i = 1, head.end()
    while i < len(css) and depth:
        depth += (css[i] == "{") - (css[i] == "}")
        i += 1
    assert depth == 0, f"{name} 花括号没配对"
    return css[head.end():i - 1]


def _rule(css: str, selector: str) -> str:
    """取一条规则的花括号内容（多行块也能取全）。"""
    head = re.search(re.escape(selector) + r"\s*\{", css, re.S)
    assert head, f"缺少规则 {selector}"
    depth, i = 1, head.end()
    while i < len(css) and depth:
        depth += (css[i] == "{") - (css[i] == "}")
        i += 1
    return css[head.end():i - 1]


# ---------------------------------------------------------------- 动效基座
def test_motion_tokens_are_centralized():
    css = _read("style.css")
    for token in ("--dur-fast", "--dur:", "--ease", "--shadow-hover"):
        assert token in css, f"动效节奏没走统一变量：{token}"
    assert ".btn:active:not(:disabled)" in css, "按钮缺按压反馈"
    assert ":focus-visible" in css, "键盘焦点环没了"
    assert "::-webkit-scrollbar" in css, "滚动条没统一"
    assert "prefers-reduced-motion: reduce" in css, "系统减少动效没兜底"


def test_page_enter_never_leaves_a_transform():
    """① 吸底保存条不能被祖先 transform 改了包含块。"""
    css = _read("style.css")
    frame = _keyframes(css, "mrm-page-in")
    assert "transform" not in frame, "页面进入不能用位移（sticky/fixed 会跑偏）"
    line = next(ln for ln in css.splitlines()
                if ln.strip().startswith(".page {") and "animation:" in ln)
    assert "mrm-page-in" in line
    assert "both" in line, "页面淡入要保留终帧，否则加载完成瞬间会闪一下"


def test_pop_in_ends_with_transform_none():
    frame = _keyframes(_read("style.css"), "mrm-pop-in")
    assert "transform: none" in frame, "面板动画终帧必须把 transform 还回去"


def test_mask_animation_has_no_fill_mode():
    """② 有 fill-mode 会压住 Transition 的离场 opacity。"""
    css = _read("style.css")
    block = _rule(css, ".mask")
    assert "mrm-mask-in" in block
    assert not re.search(r"\b(both|forwards|backwards)\b", block), \
        "遮罩动画不能带 fill-mode，否则弹窗淡出会失效"
    assert "backdrop-filter" in block, "遮罩虚化没了"


# ---------------------------------------------------------------- 进出场
def test_dialogs_and_popovers_have_transitions():
    css = _read("style.css")
    for cls in (".dlg-enter-active", ".dlg-leave-to", ".pop-enter-active", ".pop-leave-to"):
        assert cls in css, f"缺统一进出场类 {cls}"
    app = _read("App.vue")
    assert app.count('<Transition name="pop">') == 2, "顶栏两个浮层都要有进出场"
    tasks = _read("views/Tasks.vue")
    assert tasks.count('<Transition name="dlg">') == 5, "任务页五个弹窗都要有进出场"
    assert tasks.count("</Transition>") >= 5


def test_tabs_have_selection_underline():
    css = _read("style.css")
    assert ".tab.on::after" in css and "mrm-tab-underline" in css
    assert "scaleX" in _keyframes(css, "mrm-tab-underline")


# ---------------------------------------------------------------- 空态 / 加载
def test_empty_state_is_a_shared_component_and_used():
    es = _read("components/EmptyState.vue")
    assert "defineProps" in es and "defineEmits" in es
    assert "Icon" in es
    for rel in ("views/Tasks.vue", "views/Export.vue"):
        t = _read(rel)
        assert "<EmptyState" in t and "EmptyState from" in t, f"{rel} 没用统一空态"
    # 原来的措辞要保住（护栏与用户都已认这些字）
    assert "暂无任务" in _read("views/Tasks.vue")
    assert "暂无导出记录。" in _read("views/Export.vue")
    tasks = _read("views/Tasks.vue")
    assert 'v-if="loading && !tasks.length"' in tasks, "首屏读取没有骨架占位"
    assert 'class="skeleton skeleton-line"' in tasks


def test_skeleton_uses_theme_colors_not_hardcoded_white():
    css = _read("style.css")
    block = css.split(".skeleton {", 1)[1].split("}", 1)[0]
    assert "var(--line)" in block and "var(--panel)" in block, \
        "骨架屏写死浅色会在深色皮肤下刺眼"


# ---------------------------------------------------------------- 侧栏
def test_sidebar_active_indicator_does_not_take_layout_space():
    app = _read("App.vue")
    assert ".nav::before" in app and ".nav.active::before" in app
    seg = app.split(".nav::before {", 1)[1].split("}", 1)[0]
    assert "position: absolute" in seg, "指示条必须绝对定位（侧栏 753 一屏是硬约束）"
    assert "scaleY" in seg
