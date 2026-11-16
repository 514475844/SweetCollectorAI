"""R181 护栏：R160 #85 —— 自定义皮肤目录（数据目录/skins/*.qss）。

三条铁律（沿用本仓库既有护栏口径）：
  ① 判据盯**最终 QSS / payload / HTML**，不盯"调了哪个函数"——
     只验调用关系，实现改回空操作照样绿（R170 变异漏过的同型坑）。
  ② 皮肤路径必须**隔离**（MRM_HOME 指到 tmp）—— 用真实数据目录扫描
     会让测试互相踩，且可能把测试文件当用户皮肤读进去。
  ③ 名字校验走**行为**（真去拼路径、真读文件），不只在字符串层面断言 ——
     R152-C1 的教训：sanitize 把坏输入收敛成好输入后，老护栏"坏输入必须
     被拒"会被掩没；这里反过来，坏名字必须**永远读不到文件**。

为什么自定义皮肤是「叠加层」不是「新预设」：内建预设 = 强调色 + 明暗 +
框架布局，全在代码里；用户文件只在该预设之上追加一段 QSS。所以自定义键
的 dark/accent/layout/native **一律取它叠加的那个内建预设**（base），
只有 label 取文件名 —— 这几条是本文件的核心判据。
"""
import re
from pathlib import Path

import pytest

from media_resource_manager.gui import theme as T
from media_resource_manager import webui

MAIN_PY = (Path(__file__).resolve().parents[1]
           / "media_resource_manager" / "gui" / "main_window.py")
THEME_PY = (Path(__file__).resolve().parents[1]
            / "media_resource_manager" / "gui" / "theme.py")
WEBUI_PY = (Path(__file__).resolve().parents[1]
            / "media_resource_manager" / "webui.py")

QSS_HEAD = "/* mrm-base: night */\nQPushButton { background: #123456; }"


@pytest.fixture
def skins(tmp_path, monkeypatch):
    """把皮肤目录隔离到 tmp，并返回可写目录。"""
    home = tmp_path / "home"
    (home / T.CUSTOM_DIRNAME).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", str(home))
    return home / T.CUSTOM_DIRNAME


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


# ───────────────────────── 键空间与名字校验 ─────────────────────────

def test_prefix_never_collides_with_builtin():
    """自定义键带前缀 ⇒ **永不**与内建预设撞键（用户放 classic.qss 也不顶掉 classic）。"""
    for k in T.PRESETS:
        assert not k.startswith(T.CUSTOM_PREFIX)
    assert "custom:classic" != "classic"


def test_stem_roundtrip():
    assert T.custom_stem_of("custom:abc") == "abc"
    assert T.custom_stem_of("custom: 我的暗色 ") == "我的暗色"
    assert T.custom_stem_of("classic") is None
    assert T.custom_stem_of("custom:") is None      # 空 stem 不算自定义键
    assert T.custom_stem_of(None) is None
    assert T.custom_stem_of(123) is None            # 非字符串不许抛


def test_name_ok_rejects_traversal_and_reserved():
    for bad in ("../evil", "..", ".", "a/b", "a\\b", "", "   ", "a:b", "a*b",
                "a?b", 'a"b', "a<b", "a>b", "a|b", "a\x00b", "x" * 65):
        assert not T.custom_name_ok(bad), bad


def test_name_ok_accepts_chinese_and_plain():
    for good in ("my", "我的暗色", "Night-Blue_2", "皮肤 A"):
        assert T.custom_name_ok(good), good


def test_bad_name_can_never_reach_a_file(skins):
    """★ 行为判据：穿越名必须**读不到文件**，不只是"名字不合法"。

    把目标文件放在皮肤目录**外面**，再用穿越名去读 —— 拿得到内容即失守。
    """
    outside = skins.parent / "secret.qss"
    outside.write_text("QWidget { background: red; }", encoding="utf-8")
    for probe in ("../secret", "..\\secret", "../secret.qss"):
        assert T.custom_skin_qss(T.custom_key_of(probe)) == "", probe


# ───────────────────────── 目录扫描 ─────────────────────────

def test_list_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "nope"))
    assert T.list_custom_skins() == []          # 目录不存在 ⇒ 空列表，绝不抛


def test_list_picks_qss_and_css_dedupes_and_sorts(skins):
    _write(skins, "beta.qss", "QWidget{}")
    _write(skins, "alpha.css", "body{}")         # 只有 css，也算一个皮肤
    _write(skins, "alpha.qss", "QWidget{}")      # 同名 ⇒ 仍是**一个**皮肤
    _write(skins, "gamma.txt", "not a skin")     # 扩展名不对 ⇒ 忽略
    (skins / "delta.qss").mkdir()                # 目录 ⇒ 忽略
    _write(skins, "../evil.qss", "x")            # 目录外 ⇒ 扫不到
    got = T.list_custom_skins()
    assert got == [("custom:alpha", "alpha"), ("custom:beta", "beta")]


def test_list_survives_unreadable_entries(skins):
    """一个坏条目不该让整份列表变成异常（建菜单路径上的硬要求）。"""
    _write(skins, "ok.qss", "QWidget{}")
    _write(skins, "bad name.qss", "x")           # 含空格其实合法，但不影响
    got = dict(T.list_custom_skins())
    assert "custom:ok" in got


# ───────────────────────── 读取 / base 解析 ─────────────────────────

def test_qss_reads_content_and_empty_for_missing(skins):
    _write(skins, "one.qss", QSS_HEAD)
    body = T.custom_skin_qss("custom:one")
    assert "#123456" in body
    assert T.custom_skin_qss("custom:missing") == ""
    assert T.custom_skin_qss("classic") == ""    # 非自定义键 ⇒ 空


def test_qss_size_capped(skins):
    """超大文件**截断**而不是把整份喂给 Qt（setStyleSheet 是同步解析）。"""
    _write(skins, "huge.qss", "QWidget{}" + "x" * (T.CUSTOM_MAX_BYTES + 5000))
    body = T.custom_skin_qss("custom:huge")
    assert 0 < len(body.encode("utf-8")) <= T.CUSTOM_MAX_BYTES


def test_base_from_directive(skins):
    _write(skins, "a.qss", "/* mrm-base: night */\nQWidget{}")
    _write(skins, "b.qss", "/* mrm-base = night */\nQWidget{}")
    _write(skins, "c.qss", "/* mrm-base: nosuch */\nQWidget{}")
    _write(skins, "d.qss", "QWidget{}")
    assert T.custom_skin_base("custom:a") == "night"
    assert T.custom_skin_base("custom:b") == "night"
    assert T.custom_skin_base("custom:c") == T.DEFAULT_PRESET   # 坏名回落
    assert T.custom_skin_base("custom:d") == T.DEFAULT_PRESET   # 没写回落
    assert T.custom_skin_base("custom:nofile") == T.DEFAULT_PRESET


def test_base_is_always_a_builtin(skins):
    _write(skins, "a.qss", "/* mrm-base: custom:zzz */\nQWidget{}")
    assert T.custom_skin_base("custom:a") in T.PRESETS


# ───────────────────────── 取预设口径落到 base 上 ─────────────────────────

def test_custom_inherits_base_appearance(skins):
    """自定义键的 dark/accent/layout/native 一律 = base 的，只有 label 是文件名。"""
    _write(skins, "n.qss", "/* mrm-base: night */\nQWidget{ background:#010203; }")
    key = "custom:n"
    assert T.preset_label(key) == "n"
    assert T.preset_label(key) != T.preset_label("night")     # 不回落成内建名
    assert T.preset_dark(key) is True                          # night 是暗色
    assert T.preset_accent(key) == T.preset_accent("night")
    assert T.preset_layout(key) == T.preset_layout("night")
    assert T.preset_is_native(key) is T.preset_is_native("night")


def test_custom_without_file_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "empty"))
    key = "custom:ghost"
    assert T.preset_dark(key) == T.preset_dark(T.DEFAULT_PRESET)
    assert T.preset_accent(key) == T.preset_accent(T.DEFAULT_PRESET)
    assert T.preset_layout(key) == T.DEFAULT_LAYOUT


def test_resolve_preset_key_three_ways(skins):
    _write(skins, "n.qss", "/* mrm-base: night */\nQWidget{}")
    assert T.resolve_preset_key("night") == "night"            # 内建原样
    assert T.resolve_preset_key("custom:n") == "custom:n"      # 自定义原样
    assert T.resolve_preset_key("bogus") == T.DEFAULT_PRESET   # 都不是→默认
    assert T.resolve_preset_key(None) == T.DEFAULT_PRESET


# ───────────────────────── 真 QSS 行为（要 QApplication） ─────────────────────────

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    import sys
    return QApplication.instance() or QApplication([])


def test_extra_qss_appended_after_base(qapp, skins):
    """★ 核心判据：自定义 QSS 必须**拼在整套样式最后**（后写者才盖得住）。"""
    _write(skins, "n.qss", QSS_HEAD)
    T.apply_preset(qapp, "custom:n")
    css = qapp.styleSheet()
    assert "#123456" in css
    assert css.index("#123456") > css.index("QWidget")   # 晚于内建那一段
    qapp.setStyleSheet("")


def test_apply_preset_custom_keeps_base_colours(qapp, skins):
    """自定义皮肤是叠加层：**底层内建皮肤不能消失**（night 的暗底还在）。"""
    _write(skins, "n.qss", "/* mrm-base: night */\nQPushButton{background:#123456;}")
    T.apply_preset(qapp, "night")
    base = qapp.styleSheet()
    T.apply_preset(qapp, "custom:n")
    custom = qapp.styleSheet()
    assert base and custom.startswith(base.rstrip("\n")[:200])
    assert "#123456" in custom
    qapp.setStyleSheet("")


def test_native_mode_still_takes_user_overlay(qapp, skins):
    """原生底 + 用户叠加层同时成立。

    ★ 这条判据是**裁决结论**不是随手写的：真原生约束的是内建那个预设，
      自定义皮肤是用户显式选的一层 —— 静默丢弃会变成「选了没反应」。
      同时必须验证内建那一大套 QSS **仍然没被套上**（原生底没被污染）。
    """
    _write(skins, "c.qss", "/* mrm-base: classic */\nQWidget{background:#123456;}")
    T.apply_preset(qapp, "classic")
    native_css = qapp.styleSheet()
    assert "#navbtn" not in native_css             # 内建那套确实没上
    T.apply_preset(qapp, "custom:c")
    css = qapp.styleSheet()
    assert native_css.strip() and css.startswith(native_css)
    assert "#123456" in css                        # 用户那一层必须生效
    assert "#navbtn" not in css                    # 原生底仍未被污染
    qapp.setStyleSheet("")


def test_empty_extra_qss_keeps_old_bytes(qapp):
    """默认空串 ⇒ 老调用点行为**逐字节不变**（不得多一个换行）。"""
    T.apply_theme(qapp, False, "steel")
    plain = qapp.styleSheet()
    T.apply_theme(qapp, False, "steel", extra_qss="")
    assert qapp.styleSheet() == plain
    T.apply_theme(qapp, False, "steel", extra_qss="   \n  ")
    assert qapp.styleSheet() == plain
    qapp.setStyleSheet("")


def test_apply_is_idempotent(qapp, skins):
    _write(skins, "n.qss", QSS_HEAD)
    T.apply_preset(qapp, "custom:n")
    first = qapp.styleSheet()
    T.apply_preset(qapp, "custom:n")
    assert qapp.styleSheet() == first
    qapp.setStyleSheet("")


# ───────────────────────── 网页端 ─────────────────────────

def test_web_css_read_and_style_close_neutralized(skins):
    _write(skins, "w.css", "body{color:red}</style><script>alert(1)</script>")
    css = T.custom_skin_web_css("custom:w")
    assert "color:red" in css
    assert not re.search(r"<\s*/\s*style", css, flags=re.I)     # 记号必须被抹掉
    assert "</STYLE" not in css.upper()


def test_web_css_variants_neutralized(skins):
    """大小写 + 空白变体都要挡住（只挡 `</style` 一种写法等于没挡）。"""
    _write(skins, "v.css", "a{}</ style>\nb{}</STYLE >\nc{}")
    css = T.custom_skin_web_css("custom:v")
    assert not re.search(r"<\s*/\s*style", css, flags=re.I)


def test_web_theme_payload_follows_base(skins):
    _write(skins, "n.qss", "/* mrm-base: night */\nQWidget{}")
    p = webui.web_theme_payload("custom:n")
    assert p["preset"] == "custom:n"
    assert p["label"] == "n"
    assert p["data_theme"] == "dark"          # 跟 base(night) 走，不是默认浅色

def test_theme_presets_includes_custom(skins, monkeypatch):
    _write(skins, "n.qss", "/* mrm-base: night */\nQWidget{}")
    presets = webui._theme_presets()
    assert "custom:n" in presets
    assert "night" in presets                  # 内建一个都不能少


# ───────────────────────── 接线（源码判据） ─────────────────────────

def test_main_window_builds_custom_menu_entries():
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "theme.list_custom_skins()" in src
    assert "_open_skins_dir" in src
    # 自定义项必须也进 _skin_actions，否则勾不上（切过去看不出选中）
    assert "_skin_actions[_ck]" in src


def test_main_window_set_skin_uses_resolver():
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "theme.resolve_preset_key(key)" in src


def test_theme_wires_extra_qss_into_apply():
    src = THEME_PY.read_text(encoding="utf-8")
    assert "extra_qss" in src
    assert "custom_skin_qss(_CURRENT_PRESET)" in src


def test_webui_uses_resolver_not_inline_membership():
    """webui 不许再写 `preset in PRESETS`（那会把自定义键判成非法）。"""
    src = WEBUI_PY.read_text(encoding="utf-8")
    assert "_theme.resolve_preset_key(" in src
    assert "preset if preset in _theme.PRESETS" not in src
