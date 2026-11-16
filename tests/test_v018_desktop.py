"""V0.0.18 · 桌面端补齐护栏（纯静态检查，不起 QApplication、不联网）。

钉住本轮桌面四件事：
  1. 素材集导出两步流（预览→勾选→导出）与标注模板入口；
  2. 全局搜索对话框存在、口径只走 searchall 一处；
  3. 站点统计看板存在、口径只走 site_stats / export_kit 两处；
  4. 菜单接线与打包 hiddenimports 齐全（漏一个=exe 里点了没反应）。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"
GUI = PKG / "gui"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _parses(p: Path) -> None:
    ast.parse(_read(p), filename=str(p))


# ---------------------------------------------------------------- 1) 两步导出

def test_training_kit_two_step_flow():
    src = _read(GUI / "training_kit_dialog.py")
    _parses(GUI / "training_kit_dialog.py")
    assert "预览候选图片" in src
    assert "preview_training_kit" in src, "预览必须走 export_kit 同一口径"
    assert "caption_template" in src, "标注模板必须进入 options()"
    assert "selected_ids" in src and "_picked" in src
    # 填充期间必须屏蔽信号，否则 itemChanged 会连环触发
    assert "blockSignals(True)" in src and "blockSignals(False)" in src
    # 「源文件未找到 / 零字节」要提前在预览里可见
    assert "源文件未找到" in src and "零字节" in src


def test_main_window_passes_ids_and_db():
    src = _read(GUI / "main_window.py")
    i = src.index("def export_training_kit(self):")
    body = src[i:i + 1800]
    assert "TrainingKitDialog(self.cfg, self, db=self.db)" in body
    assert "dlg.selected_ids()" in body
    assert 'opts["ids"] = picked' in body


# ---------------------------------------------------------------- 2) 全局搜索

def test_searchall_dialog_wired():
    p = GUI / "searchall_dialog.py"
    _parses(p)
    src = _read(p)
    assert "class SearchAllDialog(QDialog)" in src
    # 口径只有一处：必须调 searchall.search_all，禁止自己写 SQL
    assert "searchall.search_all" in src
    for forbidden in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in src, f"搜索对话框不得内嵌 SQL：{forbidden}"
    # 跳转复用主窗口现成机制（导航/编辑/灯箱），不另造
    assert "_nav_go" in src and "_edit_by_id" in src and "Lightbox" in src
    mw = _read(GUI / "main_window.py")
    assert "def show_search_all(self):" in mw
    # 【V0.0.19-E】菜单重排后挂载变量变了（m_lib→视图菜单）；护栏钉
    # 「动作文字 + 槽接线」这对关系，不钉当年那个变量名。
    assert 'addAction("全局搜索…", self.show_search_all)' in mw


# ---------------------------------------------------------------- 3) 统计看板

def test_site_stats_dialog_wired():
    p = GUI / "site_stats_dialog.py"
    _parses(p)
    src = _read(p)
    assert "class SiteStatsDialog(QDialog)" in src
    # 三个口径全部复用纯模块，禁止内嵌 SQL
    assert "site_stats.task_stats" in src
    assert "site_stats.cookie_expiry_hints" in src
    assert "export_kit.export_history" in src
    for forbidden in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in src, f"统计看板不得内嵌 SQL：{forbidden}"
    for tab in ("任务与站点", "失败原因", "Cookie 提醒", "素材集导出历史"):
        assert tab in src
    mw = _read(GUI / "main_window.py")
    assert "def show_site_stats(self):" in mw
    assert 'addAction("站点采集统计…", self.show_site_stats)' in mw


# ---------------------------------------------------------------- 4) 打包清单

def test_hiddenimports_complete():
    spec = _read(ROOT / "build" / "pyinstaller.spec")
    for mod in ("media_resource_manager.searchall",
                "media_resource_manager.site_stats",
                "media_resource_manager.notify",
                "media_resource_manager.gui.searchall_dialog",
                "media_resource_manager.gui.site_stats_dialog",
                "media_resource_manager.gui.notify_dialog"):
        assert mod in spec, f"打包清单缺 {mod}——exe 里菜单点了会没反应"


# ---------------------------------------------------------------- 4b) 通知中心

def test_notify_dialog_wired():
    p = GUI / "notify_dialog.py"
    _parses(p)
    src = _read(p)
    assert "class NotifyDialog(QDialog)" in src
    # 口径只在 notify.py，对话框不写 SQL
    assert "notify.list_all" in src and "notify.mark_read" in src \
        and "notify.clear" in src
    for forbidden in ("SELECT ", "INSERT ", "UPDATE ", "DELETE "):
        assert forbidden not in src, f"通知面板不得内嵌 SQL：{forbidden}"
    # 清空是不可恢复动作，必须二次确认
    assert "QMessageBox.question" in src and "不可恢复" in src
    mw = _read(GUI / "main_window.py")
    assert "def show_notifications(self):" in mw
    assert 'addAction("通知中心…", self.show_notifications)' in mw


# ---------------------------------------------------------------- 5) 文案护栏

_BANNED = ("点一下", "点开", "搞定", "其实", "反正", "还没", "找不到",
           "手动", "试试", "没法", "神器", "白费", "瞎", "顺手", "顺便")


def test_copy_professional_new_files():
    for p in (GUI / "searchall_dialog.py", GUI / "site_stats_dialog.py",
              PKG / "searchall.py", PKG / "site_stats.py", PKG / "notify.py"):
        src = _read(p)
        for w in _BANNED:
            assert w not in src, f"{p.name} 出现口语词「{w}」"
        # 术语唯一：目标词 / 排除词
        assert "要找" not in src and "别找" not in src
