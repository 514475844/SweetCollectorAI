"""r237 护栏：带动画的图标（svg-spinners / line-md）+ 静态/动态总开关。

这类"纯观感"的功能最危险的地方在于**它坏了也不会报错**：
- 图标键写错 → 画一个空白 SVG，页面看着像少了东西，控制台一声不吭；
- 动画素材里混进 `<style>` → keyframes 变全局，能把别的元素带歪；
- 开关只改了配置、没同步到运行时 → 用户点完以为没生效，来回点十次。

所以全部用静态判据钉住，不靠肉眼。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager/webapp/src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def _anim_block() -> str:
    """ANIM 对象本体。

    ⚠ 不能用 `rsplit("}", 1)` —— 文件里还有一个 ANIM_FALLBACK 对象，
    那样会一路切到文件末尾，把兜底表也算进来（第一次就这么写错的）。
    """
    txt = _read("icons-anim.js")
    seg = txt.split("export const ANIM = {", 1)[1]
    return seg.split("export const ANIM_FALLBACK", 1)[0]


def _anim_keys() -> list[str]:
    return re.findall(r'^  "([a-z0-9-]+)":', _anim_block(), re.M)


def _static_keys() -> set[str]:
    body = _read("icons.js").split("export const ICONS = {", 1)[1].rsplit("}", 1)[0]
    return set(re.findall(r'^  "([a-z0-9-]+)":', body, re.M))


# ------------------------------------------------------------ 素材本身

def test_anim_file_is_well_formed():
    body = _anim_block().rsplit("}", 1)[0]      # 去掉 ANIM 自己的收尾括号
    assert body.count("{") == body.count("}"), "花括号不成对 → 有图标被写到对象外面"
    keys = _anim_keys()
    assert len(keys) >= 10, keys
    assert len(keys) == len(set(keys)), "语义键重复"


def test_animations_are_smil_not_css():
    """必须是 SMIL（<animate>/<animateTransform>）。

    如果是 CSS（<style> 内联 keyframes），塞进 v-html 后 keyframes 会泄漏到全局，
    可能把别的元素也带动起来 —— 这类 bug 极难定位，从源头卡住。
    """
    body = _anim_block()
    assert "<animate" in body, "一个动画都没有？"
    assert "<style" not in body, "动画素材里混进了 <style>，会把 keyframes 泄漏到全局"
    assert "@keyframes" not in body


def test_every_anim_has_valid_fallback():
    """关掉动态图标时退回的静态图标必须真实存在，否则那个位置会变成空白。"""
    fallback = _read("icons-anim.js").split("export const ANIM_FALLBACK = {", 1)[1]
    fallback = fallback.rsplit("}", 1)[0]
    pairs = dict(re.findall(r'^  "([a-z0-9-]+)": "([a-z0-9-]+)",', fallback, re.M))
    static = _static_keys()
    anim = set(_anim_keys())
    for sem, fb in pairs.items():
        assert sem in anim, f"兜底表里的 {sem} 在 ANIM 里不存在"
        assert fb in static, f"兜底图标 {fb} 不在静态图标集里（会画成空白）"
    missing = anim - set(pairs)
    assert not missing, f"这些动画没有静态兜底，关掉开关会空白：{missing}"


# ------------------------------------------------------------ 组件能力

def test_icon_component_supports_anim_prop():
    i = _read("components/Icon.vue")
    assert "anim: { type: String" in i
    assert "ANIM[props.anim]" in i
    assert "ANIM_FALLBACK[props.anim]" in i


def test_icon_requires_all_three_conditions():
    """必须同时满足：有语义键 + 用户开着 + 系统没要求减少动效。

    少任何一个都会出问题：忽略 prefersReduced 是无视无障碍设置；
    不判"素材存在"则模板里写错一个键就整块空白，且不报错。
    """
    i = _read("components/Icon.vue")
    seg = i.split("const animBody = computed", 1)[1].split("})", 1)[0]
    assert "props.anim" in seg
    assert "animatedIcons.value" in seg
    assert "prefersReduced" in seg


def test_prefs_module_has_reduced_motion_guard():
    p = _read("uiPrefs.js")
    assert "prefers-reduced-motion: reduce" in p
    assert "export const animatedIcons = ref(true)" in p


# ------------------------------------------------------------ 总开关

def test_config_field_defaults_on():
    cfg = (ROOT / "media_resource_manager/config.py").read_text(encoding="utf-8")
    assert re.search(r"ui_animated_icons: bool = True", cfg), "默认应为开（用户要求的）"


def test_setting_is_web_writable():
    from media_resource_manager.webui import _Handler
    assert "ui_animated_icons" in _Handler._SETTINGS_WRITABLE


def test_setting_has_label():
    """没写中文标签时设置页会显示成英文键名。"""
    from media_resource_manager.webui import _SETTINGS_LABELS
    assert _SETTINGS_LABELS.get("ui_animated_icons"), "缺中文标签"


def test_settings_page_toggles_runtime_state():
    """改开关必须**立刻**作用于全局 ref，否则用户要刷新整页才看到变化。"""
    s = _read("views/Settings.vue")
    assert "ui_animated_icons" in s
    assert "setAnimatedIcons(body.ui_animated_icons)" in s
    assert "setAnimatedIcons(d.ui_animated_icons)" in s      # 进页面时也同步一次


def test_app_boot_syncs_switch():
    a = _read("App.vue")
    assert "setAnimatedIcons" in a
    # 拉设置失败不能让整个界面进不去
    assert ".catch(() => { /* 忽略 */ })" in a


# ------------------------------------------------------------ 实际用在哪

def test_animation_only_where_something_happens():
    """动画必须挂在"真有事在发生"的位置，不能是装饰。

    这里逐个盯：刷新（转圈）、运行中（脉冲）、下载中（数据流）、找相似（转圈）。
    """
    tasks = _read("views/Tasks.vue")
    assert "anim=\"pulse\"" in tasks, "运行中的任务没有脉冲指示"
    assert ":anim=\"loading ? 'spin' : ''\"" in tasks, "刷新按钮在加载时不转圈"

    grid = _read("components/ImageGrid.vue")
    assert "simBusy ? 'spin-ring'" in grid, "找相似没有转圈提示"

    subs = _read("views/Subs.vue")
    assert "anim=\"dl\"" in subs, "订阅抓取没有下载动画"

    models = _read("views/Models.vue")
    assert "anim=\"dl\"" in models, "模型下载没有数据流动画"

    app = _read("App.vue")
    assert "anim=\"bars\"" in app, "底栏速率没有动画柱"


def test_idle_state_is_not_animated():
    """0 速率/没任务在跑时不能还有东西在动 —— 那是骗人。"""
    app = _read("App.vue")
    assert 'v-if="st.rate_bps > 0" anim="bars"' in app
    assert "(summary.running || 0) > 0 ? 'pulse' : ''" in app
