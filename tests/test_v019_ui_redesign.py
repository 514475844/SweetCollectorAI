"""V0.0.19 护栏：界面推倒重排（双模式 / 菜单 5 基线 / 表单分层 / 毛玻璃）。

用户拍板「推倒重做，所有功能都要有」，这一轮同时改了：
  1. 顶级菜单 8 → 5（任务/工具/视图/设置/帮助），新手模式藏 工具/视图；
  2. cfg.ui_mode 字段与 set_ui_mode 全链路（菜单显隐 + 表单折叠 + 持久化）；
  3. 新建任务表单三类分层：常驻（网址/保存到/代理）、老鸟直显、高级折叠；
  4. 毛玻璃：gui/acrylic.py（DWM 系统背板）+ 视图菜单开关 + cfg.glass_enabled；
  5. logo 从侧栏左下角水印位迁到侧栏顶部。
全部为静态检查，不启动 QApplication、不联网、不碰任何数据。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"
GUI = PKG / "gui"
MW = GUI / "main_window.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1) 双模式

def test_ui_mode_field_and_switch_chain():
    cfg = _read(PKG / "config.py")
    assert 'ui_mode: str = "pro"' in cfg, "老用户默认必须是老鸟模式（现状零变化）"
    mw = _read(MW)
    assert "def set_ui_mode(self, mode: str):" in mw
    assert "def _apply_ui_mode_menus(self):" in mw
    assert "def _ui_is_newbie(self)" in mw
    # 持久化 + 即时生效：保存配置、刷菜单、刷表单三件事都要做
    body = mw[mw.index("def set_ui_mode"):mw.index("def _ui_is_newbie")]
    assert "save_config(self.cfg)" in body
    assert "self._apply_ui_mode_menus()" in body
    assert "self._refresh_task_form_mode()" in body


def test_newbie_hides_tools_and_view_only():
    mw = _read(MW)
    body = mw[mw.index("def _apply_ui_mode_menus"):
              mw.index("def _refresh_uimode_checks")]
    assert '("_m_tools", "_m_view")' in body
    # 任务/设置/帮助 在新手模式仍可见 —— 隐藏名单里不许出现它们
    assert "_m_task" not in body and "_m_set" not in body and "_m_help" not in body


# ---------------------------------------------------------------- 2) 表单分层

def test_task_form_three_tiers():
    mw = _read(MW)
    assert '_ALWAYS = {"网址", "保存到", "代理"}' in mw
    assert "_PRO = {" in mw
    assert "_adv_toggle" in mw and "_adv_body" in mw
    assert "高级选项" in mw
    assert "def _adv_body_toggle(self" in mw
    # 常驻三行之外，其余字段全部有归宿（pro 或 adv），控件不許被删：
    # 老字段引用必须仍在
    for ref in ("f_entry", "f_proxy", "f_out", "f_tpl", "f_ai_labels",
                "f_ai_excl", "f_cookie", "f_rate", "f_auto"):
        assert ref in mw, f"表单控件 {ref} 被弄丢了"


def test_task_form_mode_refresh():
    mw = _read(MW)
    body = mw[mw.index("def _refresh_task_form_mode"):
              mw.index("def _ui_is_newbie")
              if mw.index("def _refresh_task_form_mode") < mw.index("def _ui_is_newbie")
              else mw.index("def toggle_glass")]
    assert "setVisible(False)" in body
    assert "setChecked(False)" in body


# ---------------------------------------------------------------- 3) 毛玻璃

def test_glass_wired():
    ac = _read(GUI / "acrylic.py")
    for sig in ("def is_supported()", "def apply_glass(", "def remove_glass("):
        assert sig in ac
    # 纯标准库：不许 import 第三方（Qt / psutil / win32 等）
    tree = ast.parse(ac)
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            assert (n.module or "").split(".")[0] in (
                "ctypes", "os", "sys", "pathlib", "__future__"), n.module
        if isinstance(n, ast.Import):
            for a in n.names:
                assert a.name.split(".")[0] in (
                    "ctypes", "os", "sys", "pathlib"), a.name
    cfg = _read(PKG / "config.py")
    assert "glass_enabled: bool = False" in cfg
    mw = _read(MW)
    assert 'QAction("毛玻璃效果（跟随系统）", self)' in mw
    assert "def toggle_glass(self" in mw
    assert "def _apply_glass_on_start(self" in mw
    # 失败静默回退：不允许裸抛
    body = mw[mw.index("def toggle_glass"):mw.index("def _apply_glass_on_start")]
    assert "except" in body


# ---------------------------------------------------------------- 4) logo 迁移

def test_logo_moved_to_sidebar_top():
    mw = _read(MW)
    i = mw.index('hdr = DragArea(self)')
    seg = mw[i:i + 1200]
    assert "Sweet采集" in seg
    assert "icon_path()" in seg
    assert "setFixedHeight(44)" in seg


# ---------------------------------------------------------------- 5) 语法
# 文案红线由项目统一护栏 test_r240_copy_professional / test_v013 负责
# （它们按「只扫界面字符串、不扫注释」的既定口径扫描，此处不重复造锁）。


def test_all_changed_files_parse():
    for p in (MW, PKG / "config.py", GUI / "acrylic.py"):
        ast.parse(_read(p), filename=str(p))
