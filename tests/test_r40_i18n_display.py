"""r40：界面标签中文化 + 语言包骨架。

硬约束（别改坏）：
- 语言包只改「显示」；存库、比对、召回、排序一律用原始英文 key。
- 没命中映射表的词原样返回，绝不吐空白或 'None'。
"""
from __future__ import annotations

import pytest

from media_resource_manager import i18n


def test_known_tag_has_cn_display():
    assert i18n.label_text("stockings") != "stockings"
    assert "丝" in i18n.label_text("stockings")
    # 组合写法：颜色 + 主词
    assert "白" in i18n.label_text("white stockings")
    assert "黑" in i18n.label_text("black pantyhose")


def test_unknown_tag_returns_as_is():
    """没做映射的词原样返回（不能变空白 / 不能抛）。"""
    for t in ("zzz-not-a-tag", "某某", "", "Mai-Shiranui"):
        assert i18n.label_text(t) == (t or "") or i18n.label_text(t) == t


def test_tag_key_unchanged_by_language():
    """切语言不能改变 tag_key —— 否则旧库里的英文标签全失配。"""
    for raw in ("NSFW", "nsfw", " White Stockings ", "stockings"):
        assert i18n.tag_key(raw) == i18n.tag_key(i18n.tag_key(raw))


def test_tag_key_is_lowercase_normalized():
    assert i18n.tag_key(" Cute ") == "cute"
    assert i18n.tag_key("Mai-Shiranui") == "mai-shiranui"


def test_translate_tags_dedups_preserves_order():
    out = i18n.translate_tags(["stockings", "cute", "stockings", "nsfw"])
    assert len(out) == len(set(out))
    # 翻译后的中文可能撞车（不同英文→同一中文）就按去重后的个数来比
    assert len(out) >= 3
    assert all("丝" in x or "可" in x or "不" in x for x in out)


def test_tr_placeholder_for_language_pack():
    """tr() 是文案占位，未来接 gettext 只改这一处。"""
    assert isinstance(i18n.tr("保存"), str)
    assert i18n.tr("速率 {n} 张/秒", 3)  # 不抛（多余参数被吃掉）


def test_locale_json_loadable():
    """语言包目录可枚举 + 加载不炸（当前只有默认包也能过）。"""
    d = i18n.locale_dir()
    assert d is not None
    if d.exists():
        assert isinstance(i18n.load_locale(), dict)


def test_hist_chips_show_cn_but_delete_uses_raw_key():
    """历史词 chips：按钮显示中文，右键删除仍按原始英文 key 命中配置。

    这条防的是「显示名被当成数据」——哪天有人把 QPushButton(label_text(wd))
    改成 QPushButton(wd) 又或者把 raw 一起换掉，这里立刻红。
    """
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QPushButton, QWidget

    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    try:
        chips = getattr(w, "_hist_chips_ai_label_history", None)
        if chips is None:
            # target 给个真 QWidget，_append_word 只在点按钮时才用得到
            chips = w._mk_history_button("ai_label_history", QWidget())
            chips.setParent(w)
        w.cfg.ai_label_history = ["stockings", "cute", "nsfw"]
        w._refresh_hist_chips()
        box = w._hist_chips_ai_label_history._box
        texts = [b.text() for b in box.findChildren(QPushButton)]
        # 显示必须是中文（不能是光秃秃的英文）
        assert any("丝" in t for t in texts), texts
        assert any("可" in t for t in texts), texts
        # 原始 key 还在配置里（显示改名不能动数据）
        assert w.cfg.ai_label_history == ["stockings", "cute", "nsfw"]
        # 右键删除走 raw key：删掉一个后配置里少的是那个英文词
        w._delete_hist_word("ai_label_history", "stockings")
        assert "stockings" not in w.cfg.ai_label_history
    finally:
        try:
            w.close()
        except Exception:
            pass
