"""V0.0.19 护栏：首页看板卡片化 + 双模式分层 + 毛玻璃现代化视觉。

静态判据为主，运行时只做最小 Qt 冒烟（不 exec、不起事件循环）。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"
GUI = PKG / "gui"
BOARD = GUI / "learning_board.py"
MODERN = GUI / "modern.py"
MW = GUI / "main_window.py"
SPEC = ROOT / "build" / "pyinstaller.spec"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1) 看板结构
def test_board_chip_and_status_cards_exist():
    src = _read(BOARD)
    for token in ("class _Chip(", "class _ChipFlow(", "def set_chips(",
                  "def _refresh_status_cards(", "def _refresh_next_tip(",
                  "def _on_chip_pick(", "def _count(",
                  "self._chip_flow", "self._status_cards", "self._next_tip"):
        assert token in src, f"看板缺 {token}"


def test_board_ui_mode_wiring():
    src = _read(BOARD)
    assert "def apply_ui_mode(" in src
    assert "_current_ui_mode()" in src
    assert "self._pro_widgets" in src
    assert "setVisible(not self._newbie)" in src
    # 弹窗/单测无活动配置时必须按老鸟全开，不能一上来就把图表藏掉
    assert 'return "pro"' in src


def test_board_keeps_readonly_redline():
    """新芯片不许引入单条改删；看板零 SQL 的红线继续成立。"""
    src = _read(BOARD)
    for bad in ("DELETE FROM preferences", "UPDATE preferences",
                "INSERT INTO preferences", "set_preference_weight",
                "delete_preference"):
        assert bad not in src, f"看板自己写库了：{bad}"
    assert "reset_learning" in src or "clear_model_auto_negatives" in src


def test_board_keeps_all_original_controls():
    """r63 / p14c / r47 依赖的控件一个都不能改名或删掉。"""
    src = _read(BOARD)
    for token in ("self.lbl_summary", "self._cards", "self.ch_pos",
                  "self.ch_neg", "self.ch_trend", "self.lbl_dec",
                  "self.lbl_src", "self.lbl_origin", "self.b_clear",
                  "self.b_reset", "self.lbl_terms", "self.lbl_feedback",
                  "self.in_term", "self.b_add_term", "self._bb"):
        assert token in src, f"原控件丢了：{token}"


def test_main_window_applies_mode_to_board():
    src = _read(MW)
    assert 'self._board_tab.apply_ui_mode(' in src
    assert 'board.apply_ui_mode(mode)' in src
    assert 'def _set_glass_look(' in src
    assert 'modern.apply_look(' in src


# ---------------------------------------------------------------- 2) 现代化视觉
def test_modern_qss_roundtrip():
    from media_resource_manager.gui import modern
    base = "QWidget{color:#123456;}"
    once = modern.glass_stylesheet(base)
    assert modern.BEGIN in once and "border-radius" in once
    assert "rgba(" in once, "玻璃层要有中性半透明"
    assert modern.glass_stylesheet(once) == once, "重复追加必须幂等"
    back = modern.strip_glass(once).rstrip()
    assert back == base.rstrip(), f"关闭后没还原：{back!r}"
    assert modern.strip_glass(base) == base, "没开过玻璃时摘除应原样返回"


def test_modern_qss_does_not_override_semantic_colors():
    """只加圆角/间距/中性半透明，不许写死强调色或文字色。"""
    from media_resource_manager.gui import modern
    qss = modern.GLASS_QSS
    for bad in ("color:", "background:#", "color:#"):
        assert bad not in qss, f"玻璃样式改写了语义色：{bad}"


def test_hiddenimports_have_glass_modules():
    spec = _read(SPEC)
    for mod in ("media_resource_manager.gui.acrylic",
                "media_resource_manager.gui.modern"):
        assert mod in spec, f"打包清单缺 {mod}，exe 里点毛玻璃会没反应"


def test_all_changed_files_parse():
    for p in (BOARD, MODERN, MW):
        ast.parse(_read(p), filename=str(p))


# ---------------------------------------------------------------- 3) 运行时冒烟
def test_board_mode_toggles_visibility():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from media_resource_manager.db import Database
    from media_resource_manager.gui.learning_board import LearningDashboard

    db = Database(":memory:")
    dlg = LearningDashboard(db)
    assert len(dlg._pro_widgets) >= 8
    assert all(w.isVisibleTo(dlg) for w in dlg._pro_widgets), \
        "默认（老鸟/弹窗）明细区应全部可见"
    assert dlg._status_cards.keys() >= {"fav", "review", "running", "export"}
    assert dlg._chip_flow is not None

    dlg.apply_ui_mode("newbie")
    assert all(not w.isVisibleTo(dlg) for w in dlg._pro_widgets), \
        "新手模式应收起图表与明细"
    dlg.apply_ui_mode("pro")
    assert all(w.isVisibleTo(dlg) for w in dlg._pro_widgets), \
        "切回老鸟模式应全部恢复"
    dlg.close()
    db.close()


# ---------------------------------------------------------------- 4) 文案红线
def test_copy_professional():
    banned = ("点一下", "点开", "搞定", "其实", "反正", "大概", "差不多",
              "折腾", "费劲", "随便", "找不到", "还没填", "还没设置",
              "还没下载", "还没有", "要找", "别找")
    for p in (BOARD, MODERN, MW):
        raw = _read(p)
        # 与项目护栏同口径：剥掉 docstring 与注释，只看用户可见字符串
        import re
        t = re.sub(r'"""[\s\S]*?"""', '""', raw)
        t = re.sub(r"'''[\s\S]*?'''", "''", t)
        t = "\n".join(x for x in t.splitlines()
                      if not x.lstrip().startswith("#"))
        for m in re.finditer(r"""["']([^'"\n]{2,300})["']""", t):
            s = m.group(1)
            if not re.search(r"[\u4e00-\u9fa5]", s):
                continue
            for w in banned:
                assert w not in s, f"{p.name} 界面文案出现「{w}」：{s[:40]}"
