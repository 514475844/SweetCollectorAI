"""SKIN_ORDER：暗夜风提升为第 3 个正式皮肤。

## 改造前的模型（务必先理解，否则测不出「改对没有」）

    5 个强调色（紫罗兰/青蓝/海蓝/暖橙/玫瑰）× 一个独立的「深色/浅色模式」开关

也就是**两件事**：用户得先点「深色 / 浅色模式」，再去「皮肤（强调色）」里挑颜色；
而深色本身**不是皮肤**，只是个 `config.dark_mode` 布尔。用户原话是
「深色模式咱们不是说要把它当成一个皮肤来做吗？这样就能凑够 3 个皮肤」。

## 改造后

    一个预设 = 一整套外观（强调色 + 明暗），选择器直接列三项：
    原始工程风 / 现代风 / 暗夜风

判据（作业单验收第5 条）落在下面三类测试里：
  1. **选择器层**：只有 3 个皮肤名，且「深色 / 浅色模式」那个独立勾选框
     已经不在了（用户切皮肤时**不再需要额外勾一个明暗** —— 作业单第 4 条的判据）。
  2. **配色层**：三个预设两两不同、暗夜风真的是暗底浅字、告警色仍醒目。
  3. **像素层**（沿用 P1-4 的 offscreen grab）：三皮肤各切一次真抓图比对。

## 本文件最容易被忽略的一条铁律

**`SKIN_LABELS` 不是用户看到的那三个名字。**
`SKINS` / `SKIN_LABELS` / `SKIN_SOFT` 是**取色引擎**（强调色四档 + 三个衍生物），
键必须严格对齐，自绘控件和 P1-4 的护栏测试都按这套键取色；
`PRESETS` / `PRESET_LABELS` 才是**选择器**。两套键故意不同 ——
所以「界面显示三个皮肤名」这件事**绝不能**用 `SKIN_LABELS` 去渲染。
`test_selector_labels_are_not_the_accent_labels` 把这条钉死。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# 仓库根 = 本文件往上两层。2026-11-09 修：原来写死旧机器路径
# <user-path> FileNotFoundError 假红。
ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


# ── 取色辅助（判据一律「关系」不写死数值，见文件头铁律）──────────────────
def _rgb(hex_or_tuple):
    from PySide6.QtGui import QColor
    c = QColor(hex_or_tuple)
    return (c.red(), c.green(), c.blue())


def _lum(hex_or_tuple):
    """相对亮度（sRGB → 线性）。判「暗底浅字」用它，不手算 gamma。"""
    def ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hex_or_tuple)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast(fg, bg) -> float:
    """WCAG 对比度。≥4.5 才算「正文可读」。"""
    a, b = _lum(fg), _lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# ══════════════════════════════════════════════════════════════════
# 1) 预设表本身：四个皮肤、命名用用户叫法
# ══════════════════════════════════════════════════════════════════
# ⚠ #qB皮肤 之前是三项（作业单原话「凑够 3 个皮肤」）。第四项
#   `qbittorrent` 是后来加的「qBittorrent风」—— 它不只换配色，还换
#   **框架布局**（顶栏工具条 + 分组侧栏 + 底部状态栏，见 theme.LAYOUTS），
#   所以下面「三个」的断言同步改成「四个」，其余铁律一条没松。
def test_preset_count():
    """选择器正好五项：原始工程风 / 紫罗兰风 / 现代风 / 暗夜风 / qBittorrent风。

    ⚠ R59（NATIVE_SKIN_ORDER §1）4 → 5：新增 `violet_look` 承接老classic
    的原观感，`classic` 改成真原生 + 钢灰蓝。
    """
    from media_resource_manager.gui import theme
    assert len(theme.PRESETS) == 5, "皮肤数不是 5：%s" % list(theme.PRESETS)


def test_preset_labels_use_user_wording():
    """命名对齐：代码/界面里用用户叫法「原始工程风/现代风/暗夜风/qBittorrent风」。"""
    from media_resource_manager.gui import theme
    labels = [theme.preset_label(k) for k in theme.PRESETS]
    for want in ("原始工程风", "紫罗兰风", "现代风", "暗夜风", "qBittorrent风"):
        assert want in labels, "缺少用户叫法 %s，实际 %s" % (want, labels)


def test_night_is_dark_and_the_light_ones_are_not():
    """暗夜风才是暗底；另外四个是浅色 —— 五个皮肤不能有两个一样明暗。"""
    from media_resource_manager.gui import theme
    darks = [k for k in theme.PRESETS if theme.preset_dark(k)]
    lights = [k for k in theme.PRESETS if not theme.preset_dark(k)]
    assert darks == ["night"], "暗底预设应只有暗夜风，实际 %s" % darks
    assert len(lights) == len(theme.PRESETS) - 1, (
        "浅色预设应有 %d 个，实际 %s" % (len(theme.PRESETS) - 1, lights))


def test_preset_accent_must_exist_in_the_palette_engine():
    """每个预设的强调色必须是 SKINS/SKIN_SOFT 里真实存在的键。

    预设表与取色引擎是两套表（见文件头），这里防的是「预设写了
    accent='nightblue'，取色时静默回落到 violet」—— 表现是选暗夜风但
    强调色没变，且不报任何错。
    """
    from media_resource_manager.gui import theme
    for k in theme.PRESETS:
        ac = theme.preset_accent(k)
        assert ac in theme.SKINS, "预设 %s 的强调色 %r 不在 SKINS 里" % (k, ac)
        assert ac in theme.SKIN_SOFT, "预设 %s 的强调色 %r 不在 SKIN_SOFT 里" % (k, ac)


def test_selector_labels_are_not_the_accent_labels():
    """**SKIN_LABELS 不是选择器的数据源**（文件头那条铁律的反向护栏）。

    两套表的键故意不同。这里断言它们确实不同，且 SKIN_LABELS 里
    不含三个用户叫法中的任何一个 —— 防止有人顺手拿 SKIN_LABELS 去渲染
    菜单，那样用户会看到「紫罗兰/青蓝/海蓝」而不是三个皮肤名。
    """
    from media_resource_manager.gui import theme
    assert set(theme.SKIN_LABELS) != set(theme.PRESETS), (
        "SKIN_LABELS 与 PRESETS 键集合相同 = 两套表被合并了，"
        "取色引擎与选择器必须解耦")
    for user_word in ("原始工程风", "紫罗兰风", "现代风", "暗夜风", "qBittorrent风"):
        assert user_word not in theme.SKIN_LABELS.values(), (
            "SKIN_LABELS 里出现了用户叫法 %s = 有人拿它当选择器渲染了" % user_word)


def test_unknown_preset_falls_back_instead_of_raising():
    """未知预设键不许抛 KeyError —— 取色路径炸了等于整窗打不开。

    来源真实：config.json 是用户可手改的，且跨版本升级会带来旧键。
    """
    from media_resource_manager.gui import theme
    for bad in ("", None, "no-such-skin", 123):
        info = theme.preset_info(bad)
        assert len(info) == 3
        assert info[0] == theme.preset_label(theme.DEFAULT_PRESET)


def test_default_preset_is_a_light_one():
    """默认必须落在浅色皮肤上。

    判「默认」而不是「用户上次选了什么」：升级后自动变暗底= 用户
    一打开软件界面全变样，第一反应就是「坏了」。
    """
    from media_resource_manager.gui import theme
    assert theme.preset_dark(theme.DEFAULT_PRESET) is False


# ══════════════════════════════════════════════════════════════════
# 2) 配色层：暗夜风是独立定义，不是「把浅色 flip 暗」
# ══════════════════════════════════════════════════════════════════
def test_night_palette_is_genuinely_dark_and_text_is_light():
    """作业单第 2 条：暗底浅字。判据用**相对亮度关系**，不写死 hex。

    「把浅色 flip 暗」会同时满足「底暗」和「字亮」这两条，所以再加一条：
    暗色板不是浅色板的机械取反（见下一条）。
    """
    from media_resource_manager.gui import theme
    d = theme.PALETTE["dark"]
    assert _lum(d["bg"]) < _lum(d["text"]), "暗夜风必须暗底浅字"
    assert _contrast(d["text"], d["bg"]) >= 4.5, (
        "正文对比度不足：%.2f" % _contrast(d["text"], d["bg"]))


def test_night_is_not_a_mechanical_inversion_of_light():
    """暗色板必须**独立写**，不能是浅色板逐位取反。

    机械 flip 的典型后果：卡片底反成近白（白卡片浮在暗底上 =
    作业单点名的「白底黑字逆色」）。R38 修过一次这个 bug，这里钉死。
    """
    from media_resource_manager.gui import theme
    light, dark = theme.PALETTE["light"], theme.PALETTE["dark"]
    inverted = set()
    for k, v in light.items():
        if not isinstance(v, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            continue
        r, g, b = _rgb(v)
        inverted.add("#%02x%02x%02x" % (255 - r, 255 - g, 255 - b))
    # 逐位取反出来的值不该原样出现在暗色板里
    leaked = [k for k, v in dark.items()
              if isinstance(v, str) and v.lower() in inverted]
    assert not leaked, (
        "暗色板这些色位与「浅色逐位取反」逐位相同（机械 flip）：%s" % leaked)


def test_night_card_bg_is_not_near_white():
    """卡片底不能近白 —— 暗色里一块块白卡片就是「白底黑字」。

    这是 R38 真机抓出来的 bug（card_bg 原先 dark 下还留#f6f5ff），
    静态护栏 TOTAL_REAL=0 抓不到它（值来自调色板，只是调色板填错），
    所以这里必须真判亮度。
    """
    from media_resource_manager.gui import theme
    for k in ("bg", "surface", "surface_alt", "card_bg", "row_hover"):
        assert _lum(theme.PALETTE["dark"][k]) < 0.25, (
            "暗夜风 %s 亮度过高（近白 = 白卡片）：%s" % (k, theme.PALETTE["dark"][k]))


def test_night_warn_colors_still_readable():
    """作业单第 2 条：告警/强调色在暗夜风下**仍醒目**。

    深色下如果照搬浅色的「浅底 + 深棕字」，整条横幅在暗底里等于隐形
    （P1-4 之前就是这个 bug）。判据：告警文字对告警底的对比度达标。
    """
    from media_resource_manager.gui import theme
    d = theme.PALETTE["dark"]
    assert _contrast(d["warn_text"], d["warn_bg"]) >= 4.5, (
        "暗夜风告警文字对比度不足：%.2f" % _contrast(d["warn_text"], d["warn_bg"]))
    assert d["warn_bg"] != theme.PALETTE["light"]["warn_bg"], (
        "告警底深浅相同 = 浅底搬到了暗色里")


def test_presets_resolve_to_different_palettes():
    """四个皮肤两两不同 —— 两个皮肤看起来一样等于没做。"""
    from media_resource_manager.gui import theme
    sigs = {}
    for k in theme.PRESETS:
        theme._CURRENT_PRESET = k
        theme._CURRENT_SKIN = theme.preset_accent(k)
        try:
            pal = theme.palette(theme.preset_dark(k))
            sigs[k] = tuple(sorted(pal.items()))
        finally:
            theme._CURRENT_PRESET = theme.DEFAULT_PRESET
            theme._CURRENT_SKIN = "violet"
    assert len(set(sigs.values())) == len(theme.PRESETS), (
        "有 skins 的调色板重复：%s" % sigs)


def test_accent_colors_differ_between_light_presets():
    """三个浅色皮肤应当**强调色互不相同**（否则用户在浅色下分不出它们）。"""
    from media_resource_manager.gui import theme
    acc = {k: theme.preset_accent(k) for k in theme.PRESETS if not theme.preset_dark(k)}
    assert len(set(acc.values())) == len(acc), "浅色预设共用强调色：%s" % acc


# ══════════════════════════════════════════════════════════════════
# 3) 选择器层（菜单）：3 个皮肤名 + 独立明暗勾选框已撤
# ══════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def qapp():
    """本文件**每个**用例会碰 Qt，所以 qapp 必须是 autouse 且 module 作用域。

    ⚠ conftest **没有**全局 qapp夹具（那是各文件自己的约定），少写这一句的
    后果不是「报 ImportError」而是 **MainWindow() 在没有 QApplication 时
    直接把进程带走**：本文件第一版就栽在这（EXIT=127 / 0xc0000374，
    junit 都不生成，看起来像「测试崩了」，真因是少了这个夹具）。
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def win(qapp):
    """真造一个主窗口读菜单树。

    `hide()` 不是可选的洁癖：同进程里若已有可见窗口，抢资源会放大堆压力
    （R25 修的正是这个）。窗口由 conftest 的 module 级
    `_reap_top_level_widgets` 统一回收，这里只管造。
    """
    from media_resource_manager.gui.main_window import MainWindow
    w = MainWindow()
    try:
        w.hide()
    except Exception:
        pass
    return w


def _submenu_labels(win, parent_text, sub_text):
    """取「设置 → 皮肤」子菜单里的条目文案。"""
    mb = win.menuBar()
    for act in mb.actions():
        if act.text() != parent_text:
            continue
        menu = act.menu()
        if menu is None:
            continue
        for a2 in menu.actions():
            if a2.menu() is not None and sub_text in a2.text():
                return [x.text() for x in a2.menu().actions() if not x.isSeparator()]
    return None


def test_skin_selector_lists_the_named_skins(win):
    """选择器**前五项** = 五个用户叫法，顺序与 theme.PRESETS 一致。

    ⚠ R181 改判据（不是放宽，是换了个更能扛事的判据）：
      皮肤子菜单尾部现在还会有「打开皮肤目录…」这类**非皮肤**入口，用户在
      data/skins/ 放了 .qss 时还会多出自定义皮肤项 ⇒ 原来的
      `labels == [五个名字]` 会把这些合法新增全判成回归。
      改成「前 N 项逐项相等」，既保住本条的原意（五项一个不少、顺序不乱、
      用用户叫法而不是内部键），又不会因为新增便利入口假红。
      顺带把写死的五个中文串换成 `preset_label` 派生 —— 加第六个内建
      皮肤时这条护栏会自己跟上，不用人改。
    """
    labels = _submenu_labels(win, "设置", "皮肤")
    assert labels, "设置菜单下找不到皮肤子菜单"
    from media_resource_manager.gui import theme
    expect = [theme.preset_label(k) for k in theme.PRESETS]
    assert labels[: len(expect)] == expect, (
        "选择器前 %d 项不是那几个用户叫法（或顺序不对），实际 %s"
        % (len(expect), labels))


def test_independent_light_dark_toggle_is_gone(win):
    """作业单第 4 条的判据：切皮肤时**不再需要额外勾一个明暗**。

    所以那个独立的「深色 / 浅色模式」动作必须撤掉。撤掉之后
    正向断言「它还在」永远是假失败 —— 所以这是**反向**断言：
    「不许在」。
    """
    from media_resource_manager.gui.main_window import MainWindow
    src = ast.parse((PKG / "gui" / "main_window.py").read_text(
        encoding="utf-8", errors="ignore"))
    texts = [n.value for n in ast.walk(src)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert not any("深色 / 浅色模式" in t for t in texts), (
        "独立的「深色 / 浅色模式」开关还在 —— 用户切皮肤仍要额外勾明暗")
    # 运行期同样不许有那个动作
    found = []
    for act in win.menuBar().actions():
        m = act.menu()
        if act.text() == "设置" and m is not None:
            found = [a.text() for a in m.actions()]
    assert not any("深色" in t for t in found), "设置菜单里仍有深浅开关：%s" % found


def test_toggle_theme_method_is_removed(win):
    """toggle_theme 的存在就是「两件事」的残留 —— 随开关一起撤掉。

    注意用hasattr 而不是 AST 搜 `def toggle_theme`：本仓 main_window.py
    里有大量注释在 quoting 旧写法，全文搜会**匹配到自己写的注释**而假红
    （这条铁律本项目已踩坑多次）。运行期检查绕开整个问题。
    """
    assert not hasattr(win, "toggle_theme"), (
        "toggle_theme 还在 = 明暗仍是独立于皮肤的第二件事")


def test_skin_menu_does_not_occupy_a_top_level_menu(win):
    """皮肤不许占顶级菜单（V0.0.19 新基线 5 个）。"""
    tops = [a.text() for a in win.menuBar().actions()]
    assert "皮肤" not in tops, "皮肤成了顶级菜单：%s" % tops
    assert len(tops) == 5, "顶级菜单数被改了：%s" % tops


def test_skin_actions_are_checkable_and_exactly_one_checked(win):
    """四项互斥选中，且当前选中项与配置一致。"""
    acts = []
    for act in win.menuBar().actions():
        if act.text() != "设置":
            continue
        for a2 in act.menu().actions():
            if a2.menu() is not None and "皮肤" in a2.text():
                acts = list(a2.menu().actions())
    from media_resource_manager.gui import theme
    # ⚠ R181 改判据：子菜单里现在混有**不可勾选**的入口（「打开皮肤目录…」），
    #   所以「项数 == 预设数」这条得盯在**可勾选的皮肤项**上 —— 那才是本条
    #   护栏真正要看的东西（每个预设一项、互斥单选）。
    #   顺带把原来只数个数的判据升级成**名字集合相等**：少一项、多一项、
    #   名字写错都会被抓，比 `len(acts) == len(PRESETS)` 更严。
    picks = [a for a in acts if a.isCheckable()]
    assert len(picks) == len(theme.PRESETS), (
        "可勾选皮肤项数与 PRESETS 不符：%d vs %d"
        % (len(picks), len(theme.PRESETS)))
    assert {a.text() for a in picks} == {theme.preset_label(k)
                                        for k in theme.PRESETS}, (
        "皮肤项名字对不上：%s" % [a.text() for a in picks])
    checked = [a for a in picks if a.isChecked()]
    assert len(checked) == 1, "选中项应恰好一个，实际 %s" % [a.text() for a in checked]
    # 非皮肤入口（打开皮肤目录）必须是**不可勾选**的，否则用户会以为
    # 「选了它就是选了一个皮肤」，而它并不写 ui_preset。
    for a in acts:
        if not a.isCheckable():
            assert not a.isChecked(), "非皮肤入口不该被勾上：%s" % a.text()


# ══════════════════════════════════════════════════════════════════
# 4) 切换行为：选皮肤 → 落配置 + 明暗跟着变 + 日志用皮肤名
# ══════════════════════════════════════════════════════════════════
def test_set_skin_updates_preset_and_dark_flag(win, monkeypatch):
    """选暗夜风 → ui_preset='night' **且** dark_mode=True（两者必须一致）。

    判「一致」而不是写死数值：配置里存的预设与派生出来的明暗标志
    不一致，会让自绘控件（读 dark_mode）和 QSS（读 preset）分成两派。
    """
    from media_resource_manager.gui import theme
    saved = {}
    monkeypatch.setattr("media_resource_manager.config.save_config",
                        lambda cfg, path=None: saved.update(cfg.to_dict()))
    for key in theme.PRESETS:
        win.set_skin(key)
        assert win.cfg.ui_preset == key
        assert bool(win.cfg.dark_mode) == theme.preset_dark(key), (
            "预设 %s 与 dark_mode 不一致（dark_mode=%s）"
            % (key, win.cfg.dark_mode))
        assert win.cfg.skin == theme.preset_accent(key), (
            "预设 %s 的强调色没落到 cfg.skin" % key)
    assert saved, "set_skin 没有保存配置"


def test_set_skin_log_uses_skin_name_not_accent_key(win, monkeypatch):
    """日志里出现皮肤名而不是内部键 —— 用户看不懂 'night'。

    判据用**真实信号**而不是替换 `log_signal`：那是个 Qt Signal，
    给它套property/monkeypatch 都不会拦住既有的 emit（第一版就是这么写的，
    结果 `seen` 是空的，测试**假通过**了 —— 断言 `any(...)` 遇到空列表
    会失败才暴露，但换个写法就会静默放过）。
    连接一个额外槽是抓 Signal 既有发射唯一可靠的办法。
    """
    from media_resource_manager.gui import theme
    seen = []
    try:
        win.log_signal.connect(lambda m: seen.append(m))
    except Exception as e:                # 连不上就直接失败，别静默跳过
        pytest.fail("连不上 log_signal，测不了日志文案：%s" % e)
    win.set_skin("night")
    assert any("暗夜风" in m for m in seen), "日志没用皮肤名：%s" % seen
    assert not any("night" in m for m in seen), (
        "日志漏出了内部键名：%s" % seen)


def test_persisted_preset_survives_reload(win, monkeypatch):
    """落盘的 ui_preset 读回来仍是同一个皮肤（否则重启后界面变了）。"""
    import json
    import tempfile
    from media_resource_manager import config as cfgmod

    saved = {}
    monkeypatch.setattr("media_resource_manager.config.save_config",
                        lambda cfg, path=None: saved.update(cfg.to_dict()))
    win.set_skin("modern")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "config.json"
        p.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
        back = cfgmod.AppConfig.from_dict(json.loads(p.read_text(encoding="utf-8")))
    assert back.ui_preset == "modern"


def test_legacy_config_without_preset_migrates_to_a_valid_skin():
    """老配置**没有** ui_preset 字段（用户升级前存的老config.json）。

    必须回落到默认皮肤而不是抛 TypeError —— from_dict 用的是
    `cls(**{k: v for k, v in d.items() if k in fields})`，缺字段走默认值，
    这条测试防的是有人把 ui_preset 写成**必填**（那会让整个老用户
    打不开软件）。
    """
    from media_resource_manager.config import AppConfig
    cfg = AppConfig.from_dict({"dark_mode": True, "skin": "violet"})
    from media_resource_manager.gui import theme
    assert cfg.ui_preset in theme.PRESETS, "老配置回落到了不存在的皮肤"


def test_legacy_dark_mode_user_migrates_to_night():
    """老用户开着深色 → 升级后必须落在暗夜风，**不能悄悄变浅色**。

    这是真实的数据丢失：用户特意开的主题，升级后被重置 = 功能回退。

    ⚠ 判据必须用**原始 dict** 而不是 AppConfig 实例：`from_dict` 会把缺失
    字段填成dataclass 默认值，传 cfg 的话「这个键压根不存在」这件事就被
    抹掉了 —— 第一版实现就是这么写的，结果对老用户恒返回 classic，
    而**所有断言都绿**（因为它测的是「返回值是个合法预设」）。
    这里额外钉死「老 dict 里没有 ui_preset」这个前提本身。
    """
    from media_resource_manager import config as cfgmod
    from media_resource_manager.gui import theme
    legacy = {"dark_mode": True, "skin": "violet"}      # 注意：没有 ui_preset
    assert "ui_preset" not in legacy, "本测试的前提是「老配置没这个键」"
    assert cfgmod.migrated_preset_for(legacy) == "night", (
        "老用户开着深色，升级后没落到暗夜风 = 主题被悄悄重置")
    assert cfgmod.migrated_preset_for({"dark_mode": False}) == theme.DEFAULT_PRESET


def test_migration_does_not_override_an_explicit_choice():
    """用户**已经**选过皮肤 → 迁移不许改（哪怕 dark_mode 与之矛盾）。"""
    from media_resource_manager import config as cfgmod
    # 已选 night 但 dark_mode 是旧的 False（配置写歪了）→ 尊重 ui_preset
    assert cfgmod.migrated_preset_for(
        {"ui_preset": "night", "dark_mode": False}) == "night"
    assert cfgmod.migrated_preset_for(
        {"ui_preset": "modern", "dark_mode": True}) == "modern"


def test_load_config_applies_migration_end_to_end(tmp_path, monkeypatch):
    """端到端：磁盘上**真的**是一份老 config.json → load_config 后是 night。

    上一条测的是纯函数；这条测的是**它真的被 load_config 调用了** ——
    「辅助函数写对了但忘了接上」是这类迁移最常见的失效形态，
    而纯函数测试对那种失效**完全无感**。
    """
    import json
    from media_resource_manager import config as cfgmod
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"dark_mode": True, "skin": "violet"},
                            ensure_ascii=False), encoding="utf-8")
    cfg = cfgmod.load_config(p)
    assert cfg.ui_preset == "night", (
        "load_config 没把老配置迁到 night，实际 %r" % cfg.ui_preset)


# ══════════════════════════════════════════════════════════════════
# 5) 像素层：三皮肤各切一次真抓图（作业单验收第 5 条）
# ══════════════════════════════════════════════════════════════════
def _opaque_ink(img):
    """只统计**不透明**像素的均亮度。

    ⚠ 整图均亮度会被透明像素拉低（本项目很多页面背景是透明的），
    判「暗底浅字」必须落在真控件 grab() 上，且只算不透明墨迹。
    """
    tot = n = 0
    for y in range(0, img.height(), 3):
        for x in range(0, img.width(), 3):
            p = img.pixel(x, y)
            a = (p >> 24) & 255
            if a < 200:
                continue
            tot += 0.299 * ((p >> 16) & 255) + 0.587 * ((p >> 8) & 255) \
                + 0.114 * (p & 255)
            n += 1
    return tot / max(n, 1), n


def _count_color(img, hexv, step=2):
    """数某颜色在图里出现多少个取样点。"""
    r, g, b = _rgb(hexv)
    n = 0
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            p = img.pixel(x, y)
            if (((p >> 16) & 255), ((p >> 8) & 255), p & 255) == (r, g, b):
                n += 1
    return n


def test_each_light_skin_actually_renders_its_own_accent(qapp, win):
    """每个浅色皮肤必须把**自己的强调色**画进画面，且**不出现**别人的。

    为什么不用「两张图整体差异率」当判据：
      两个浅色皮肤按设计**只差强调色**（作业单：原始工程风=经典浅色、
      现代风=qBittorrent 浅色），实测整体差异只有 2.6% —— 那是**事实**，
      不是缺陷。拿一个猜出来的阈值去卡它，要么过紧假红、要么为了过就
      一路调低，最后阈值失去意义（本项目已吃过「快照式断言」的亏）。

      真正要证明的是**因果**：强调色换了，画面上就该出现新强调色、
      且旧强调色消失。这条判据没有阈值可调，也不可能靠巧合通过 ——
      第一版就是因为浅色 QSS 里按钮写死了暖橙，强调色压根没进画面，
      差异率 0.000，这一条立刻抓到。
    """
    from media_resource_manager.gui import theme
    # ⚠ R60：classic 已改为**真原生**（NATIVE_SKIN_ORDER §3）——
    #   原生模式按设计**不画自绘强调色**（控件交给 Windows 画），
    #   所以它进不了这条「强调色因果」判据：`_count_color(steel) > 50`
    #   对真原生窗口恒为 0。
    #   ⇒ 不是把断言放宽，而是把 classic 挪到下面那条**反向**断言里
    #     （真原生 = 画面里没有自绘强调色），两边合起来把契约钉死。
    for key in ("modern",):
        theme.apply_preset(qapp, key)
        for _ in range(4):
            qapp.processEvents()
        img = win.grab().toImage()
        mine = theme.SKINS[theme.preset_accent(key)][0]
        others = [theme.SKINS[theme.preset_accent(k)][0] for k in theme.PRESETS
                  if theme.preset_accent(k) != theme.preset_accent(key)
                  and not theme.preset_dark(k)]
        assert _count_color(img, mine) > 50, (
            "皮肤 %s 的强调色 %s 几乎没出现在画面上 —— 换皮肤用户看不出来"
            % (key, mine))
        for other in others:
            assert _count_color(img, other) == 0, (
                "皮肤 %s 的画面里还残留别的强调色 %s" % (key, other))


def test_true_native_preset_renders_no_custom_accent(qapp, win):
    """R60 反向契约：真原生预设的画面里**没有**自绘强调色。

    为什么必须成对写（MEMORY 铁律：功能真撤掉后，正向「查在不在」
    就是永远假失败，必须改反向「不许在」）：
      上一条把 classic 从正向判据里挪了出来，若这里不补一条反向的，
      就会出现「classic 无论画成什么样都测不出问题」——
      护栏松了而没人知道。本条要求：classic 画面里
      **任何预设的自绘强调色都不该出现**（含它自己的 steel），
      因为真原生 = 控件全部交给系统样式画。
    """
    from media_resource_manager.gui import theme
    theme.apply_preset(qapp, "classic")
    for _ in range(4):
        qapp.processEvents()
    img = win.grab().toImage()
    for k in theme.PRESETS:
        hexv = theme.SKINS[theme.preset_accent(k)][0]
        assert _count_color(img, hexv) == 0, (
            "真原生皮肤画面里出现了自绘强调色 %s（来自 %s）—— "
            "原生模式应完全交给系统样式画" % (hexv, k))
    # 反向前置：非原生的 modern 必须**真的**画出自己的强调色。
    # 少了这条，本条可能因为「窗口根本没画出来」而一直绿。
    theme.apply_preset(qapp, "modern")
    for _ in range(4):
        qapp.processEvents()
    assert _count_color(win.grab().toImage(),
                        theme.SKINS[theme.preset_accent("modern")][0]) > 50, (
        "前置失败：modern 都画不出自己的强调色 —— "
        "说明是抓图本身坏了，不能拿来判原生")


def test_night_and_light_skins_are_dramatically_different(qapp, win):
    """暗夜风与两个浅色皮肤必须**整体**大幅不同（暗底 vs 浅底）。

    这条用「整体差异率」是合适的：明暗切换改的是**每一页的底色**，
    量级本来就该接近全屏（实测 0.99+）。阈值 0.5 留足余量，
    又不会因为某页恰好全是图片而误报。
    """
    from media_resource_manager.gui import theme
    shots = {}
    for key in theme.PRESETS:
        theme.apply_preset(qapp, key)
        for _ in range(4):
            qapp.processEvents()
        shots[key] = win.grab().toImage()
    light_keys = [k for k in theme.PRESETS if not theme.preset_dark(k)]
    for key in light_keys:
        a, b = shots["night"], shots[key]
        assert a.size() == b.size(), "两次抓图尺寸不同，比不了"
        diff = sum(1 for y in range(0, a.height(), 4)
                   for x in range(0, a.width(), 4)
                   if a.pixel(x, y) != b.pixel(x, y))
        total = len(range(0, a.height(), 4)) * len(range(0, a.width(), 4))
        assert diff / max(total, 1) > 0.5, (
            "暗夜风与 %s 的整体差异只有 %.3f —— 明暗没真正分开"
            % (key, diff / max(total, 1)))


def test_night_really_renders_dark_background_light_text(qapp, win):
    """暗夜风抓图：底暗、字亮、且**没有大面积近白块**。

    「近白块占比低」这一条是白底黑字逆色的直接量化 —— R38 就是靠它
    在真像素上抓到 card_bg 填错的。
    """
    from media_resource_manager.gui import theme
    theme.apply_preset(qapp, "night")
    for _ in range(4):
        qapp.processEvents()
    img = win.grab().toImage()
    lum, n = _opaque_ink(img)
    assert n > 0, "整张图没有不透明像素 —— 抓图方式不对（不是真控件 grab）"
    assert lum < 90, "暗夜风整体亮度过高（暗底浅字应当偏暗）：%.1f" % lum
    near_white = sum(
        1 for y in range(0, img.height(), 3) for x in range(0, img.width(), 3)
        if ((img.pixel(x, y) >> 24) & 255) >= 200
        and _lum("#%02x%02x%02x" % ((img.pixel(x, y) >> 16) & 255,
                                    (img.pixel(x, y) >> 8) & 255,
                                    img.pixel(x, y) & 255)) > 0.75)
    total = len(range(0, img.height(), 3)) * len(range(0, img.width(), 3))
    assert near_white / max(total, 1) < 0.10, (
        "暗夜风里近白像素占比 %.1f%% —— 又是白卡片/白底黑字"
        % (100 * near_white / max(total, 1)))


def test_classic_and_modern_differ_only_by_accent_not_by_brightness(qapp, win):
    """两个浅色皮肤必须**都还是浅底**（别把现代风也做成暗的）。"""
    from media_resource_manager.gui import theme
    # 本条只测**皮肤明暗**，与毛玻璃正交。win 夹具读的是真实运行版配置，
    # V0.0.19-E 起默认 glass_enabled=True，玻璃半透明底会把 grab 像素整体压暗
    # （classic 实测 59 < 140 假红）。测量前关掉玻璃观感，隔离变量。
    try:
        win._set_glass_look(False)
    except Exception:
        pass
    lums = {}
    for key in ("classic", "modern"):
        theme.apply_preset(qapp, key)
        for _ in range(4):
            qapp.processEvents()
        lums[key], _n = _opaque_ink(win.grab().toImage())
    night = None
    theme.apply_preset(qapp, "night")
    for _ in range(4):
        qapp.processEvents()
    night, _ = _opaque_ink(win.grab().toImage())
    assert lums["classic"] > 140, "原始工程风应当是浅底：%.1f" % lums["classic"]
    assert lums["modern"] > 140, "现代风应当是浅底：%.1f" % lums["modern"]
    assert night < min(lums.values()), "暗夜风必须明显暗于两个浅色皮肤"


# ══════════════════════════════════════════════════════════════════
# 6) Web 审图页跟随皮肤（作业单第 3 条）
# ══════════════════════════════════════════════════════════════════
def test_web_page_receives_current_skin_from_server(qapp):
    """Web 端必须能拿到桌面端当前皮肤（否则两端观感不一致）。"""
    from media_resource_manager import webui
    assert hasattr(webui, "web_theme_payload"), (
        "webui 没有导出 web_theme_payload —— Web 端无法跟随桌面皮肤")


def test_web_theme_payload_maps_night_to_dark():
    """暗夜风 → 网页 data-theme=dark；两个浅色皮肤 → light。"""
    from media_resource_manager import webui
    from media_resource_manager.gui import theme
    for key in theme.PRESETS:
        payload = webui.web_theme_payload(key)
        assert payload["data_theme"] == ("dark" if theme.preset_dark(key) else "light")
        assert payload["preset"] == key
        assert payload["accent"].startswith("#"), "强调色没传给网页"


def test_web_theme_payload_survives_unknown_preset():
    """未知预设不许把网页搞崩（回落即可）。"""
    from media_resource_manager import webui
    payload = webui.web_theme_payload("no-such")
    assert payload["data_theme"] in ("dark", "light")

def test_default_accent_follows_the_default_preset():
    """`DEFAULT_ACCENT` 必须等于 `preset_accent(DEFAULT_PRESET)`（钉关系不钉值）。

    R59 把 classic 的强调色从 violet 换成 steel，而 webui 的兜底强调色读的是
    `DEFAULT_ACCENT`。写死字面量的话，换默认皮肤那天 Web 端会悄悄对不上 ——
    表现是「桌面钢灰蓝、网页紫罗兰」，且不报任何错。
    """
    from media_resource_manager.gui import theme
    assert theme.DEFAULT_ACCENT == theme.preset_accent(theme.DEFAULT_PRESET), (
        "DEFAULT_ACCENT=%r 与默认预设 %r 的强调色 %r 不一致"
        % (theme.DEFAULT_ACCENT, theme.DEFAULT_PRESET,
           theme.preset_accent(theme.DEFAULT_PRESET)))
    assert theme.DEFAULT_ACCENT in theme.SKINS, (
        "DEFAULT_ACCENT 不在 SKINS 里：%r" % theme.DEFAULT_ACCENT)


def test_every_preset_has_a_layout_entry():
    """PRESETS 与 LAYOUTS 的键必须一一对应。

    `preset_layout` 对未知键回落到默认布局（不抛），所以「少一个键」不会
    报错，只会**静默**让那个皮肤用错框架 —— qBittorrent风少一格就变成
    「顶栏工具条没了」。所以这里钉死两表同键。
    """
    from media_resource_manager.gui import theme
    assert set(theme.LAYOUTS) == set(theme.PRESETS), (
        "PRESETS 与 LAYOUTS 键不一致，多=%s 少=%s"
        % (sorted(set(theme.PRESETS) - set(theme.LAYOUTS)),
           sorted(set(theme.LAYOUTS) - set(theme.PRESETS))))


def test_the_four_color_engine_tables_share_one_key_set():
    """SKINS / SKIN_LABELS / SKIN_SOFT / SKIN_TINT_DARK 四张表同键。

    R59 给 SKINS 加了 `steel`。取色路径上任何一张表缺这一格都是**静默**
    回落（`SKIN_SOFT.get(k, SKIN_SOFT["violet"])`），表现是「钢灰蓝皮肤
    的选中底是淡紫」—— 不报错、跑测试也发现不了，只能在这里钉。
    """
    from media_resource_manager.gui import theme
    base = set(theme.SKINS)
    for name, tbl in (("SKIN_LABELS", theme.SKIN_LABELS),
                      ("SKIN_SOFT", theme.SKIN_SOFT),
                      ("SKIN_TINT_DARK", theme.SKIN_TINT_DARK)):
        assert set(tbl) == base, (
            "%s 与 SKINS 键不一致，多=%s 少=%s"
            % (name, sorted(set(tbl) - base), sorted(base - set(tbl))))


def test_legacy_classic_users_keep_the_old_look():
    """R59铁律：老配置 `ui_preset == "classic"` 必须迁到 `violet_look`。

    R58 之前 classic 就是那套紫罗兰观感；R59 把 classic 改成真原生 + 钢灰蓝。
    不迁的话老用户一升级界面整个变样 —— 静默、无提示，用户只会觉得
    「软件更新坏了」。
    """
    from media_resource_manager import config as cfgmod
    legacy = {"ui_preset": "classic", "dark_mode": False}
    assert "ui_preset_scheme" not in legacy, (
        "本测试的前提是「老配置没有世代号键」—— 有了就说明它不是老配置了")
    assert cfgmod.migrated_preset_for(legacy) == "violet_look", (
        "老 classic 用户没被迁到 violet_look = 升级后界面静默变样")


def test_the_classic_rename_migration_happens_only_once():
    """**一次性**是这条迁移最容易漏的一半：用户在新版里主动选「原始工程风」
    是合法操作（配置键名没变，还是 "classic"），若无条件搬，他每次启动
    都会被弹回紫罗兰风、永远选不了这个皮肤，且全程不报错。

    所以判据是「世代号」，不是「当前是不是 classic」。
    """
    from media_resource_manager import config as cfgmod
    new_cfg = {"ui_preset": "classic",
               "ui_preset_scheme": cfgmod.PRESET_SCHEME}
    assert cfgmod.migrated_preset_for(new_cfg) == "classic", (
        "新版显式选classic 却被迁走 = 用户永远选不了原始工程风")
    # 迁过之后load_config 会把世代号抬到当前值并写盘 ⇒ 第二次读同一份配置不再搬
    after = {"ui_preset": "violet_look",
             "ui_preset_scheme": cfgmod.PRESET_SCHEME}
    assert cfgmod.migrated_preset_for(after) == "violet_look"


def test_preset_scheme_constant_is_shared_by_config_default():
    """AppConfig 的默认世代号必须就是 `PRESET_SCHEME`（同一个真相）。

    两处各写一个数字迟早漂；漂了的后果是「迁移对新版配置也生效」——
    也就是上面那条一次性护栏在**真实调用链**上失效，而纯函数测试仍全绿。
    """
    from media_resource_manager import config as cfgmod
    assert cfgmod.AppConfig().ui_preset_scheme == cfgmod.PRESET_SCHEME, (
        "AppConfig 默认世代号 %r != PRESET_SCHEME %r"
        % (cfgmod.AppConfig().ui_preset_scheme, cfgmod.PRESET_SCHEME))


def test_load_config_writes_the_scheme_back_so_migration_is_not_repeated(
        tmp_path):
    """端到端：磁盘上是老config.json → load_config 后落盘的文件**带世代号**。

    这是「一次性」在真实链路上的唯一证据：纯函数测对了但 load_config 忘了
    抬世代号 ⇒ 每次启动都重跑迁移，而这类失效纯函数测试**完全无感**
    （它每次传的都是没有世代号的 dict）。
    """
    import json
    from media_resource_manager import config as cfgmod
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"ui_preset": "classic", "dark_mode": False},
                ensure_ascii=False), encoding="utf-8")
    cfg = cfgmod.load_config(p)
    assert cfg.ui_preset == "violet_look", (
        "老配置没被迁到 violet_look，实际 %r" % cfg.ui_preset)
    assert cfg.ui_preset_scheme == cfgmod.PRESET_SCHEME
    # 磁盘上也得有——不然下次启动读到的是「还是老配置」
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk.get("ui_preset_scheme") == cfgmod.PRESET_SCHEME, (
        "世代号没写回磁盘 = 迁移会在每次启动重跑：%s" % on_disk)
    # 二次加载：同一份磁盘配置不许再被搬
    again = cfgmod.load_config(p)
    assert again.ui_preset == "violet_look", (
        "第二次加载把violet_look 改掉了 = 迁移不是一次性：%r" % again.ui_preset)


def test_load_config_bumps_a_stale_scheme_that_the_default_cannot_cover(
        tmp_path):
    """带**过时世代号**的配置必须被抬到当前值（默认值顶不上，必须显式抬）。

    ⚠ 这条是R59 反向验证逼出来的：上一条护栏在删掉 `load_config` 里那行
    `cfg.ui_preset_scheme = PRESET_SCHEME` 之后**仍然全绿** —— 因为
    `AppConfig` 的字段默认值本来就是 PRESET_SCHEME，老配置没这个键时
    `from_dict` 填默认值就填对了。**护栏绿 ≠ 护栏对**：它是因为错误的
    原因而绿的。

    真正只有那行显式赋值能救的是「原始 dict 里带着一个过时的 scheme 值」：
    `from_dict` 会把它**原样**带进 cfg（字段存在、值合法），默认值顶不上，
    不显式抬回去就会每次启动重跑迁移。本条钉的就是这一种。
    """
    import json
    from media_resource_manager import config as cfgmod
    stale = cfgmod.PRESET_SCHEME - 1
    assert stale >= 0
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"ui_preset": "violet_look",
                             "ui_preset_scheme": stale},
                ensure_ascii=False), encoding="utf-8")
    cfg = cfgmod.load_config(p)
    assert cfg.ui_preset_scheme == cfgmod.PRESET_SCHEME, (
        "过时世代号 %r 没被抬到 %r —— load_config 里的显式赋值被删了？"
        % (cfg.ui_preset_scheme, cfgmod.PRESET_SCHEME))
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert on_disk.get("ui_preset_scheme") == cfgmod.PRESET_SCHEME, (
        "过时世代号没写回磁盘 = 迁移会在每次启动重跑：%s" % on_disk)


def test_switching_skin_to_classic_sticks_across_reload(tmp_path):
    """真链路：用户选「原始工程风」→ 存盘 → 重新加载**仍是**原始工程风。

    这是上面两条护栏的合体，也是用户真正能感知的场景。
    """
    import json
    from media_resource_manager import config as cfgmod
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"ui_preset": "violet_look",
                             "ui_preset_scheme": cfgmod.PRESET_SCHEME},
                ensure_ascii=False), encoding="utf-8")
    cfg = cfgmod.load_config(p)
    assert cfg.ui_preset == "violet_look"
    # 模拟用户在设置里点了「原始工程风」
    cfg.ui_preset = "classic"
    cfg.ui_preset_scheme = cfgmod.PRESET_SCHEME
    cfgmod.save_config(cfg, p)
    again = cfgmod.load_config(p)
    assert again.ui_preset == "classic", (
        "用户主动选的原始工程风没能留住 = 一次性迁移失效：%r" % again.ui_preset)
