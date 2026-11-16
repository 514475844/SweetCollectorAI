"""V0.0.17 护栏：界面重排、两端补齐与打包清单。

这一轮同时改了菜单结构、设置入口、桌面补齐模块与网页补齐端点，
护栏要钉住三件事：
  1. 八个顶级菜单的名字与顺序不许被重排弄丢（verify_r11 基线口径）；
  2. 分组标题、统一设置入口、新动作必须真的接上槽函数；
  3. 新增的惰性导入模块必须进 PyInstaller，否则只在 exe 里表现为
     「菜单点了没反应」。
全部为静态检查，不启动 QApplication、不联网、不碰任何数据。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "media_resource_manager"
GUI = PKG / "gui"
WEB = PKG / "webapp" / "src"
MW = GUI / "main_window.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _mw() -> str:
    return _read(MW)


# ---------------------------------------------------------------- 菜单基线
# 【V0.0.19 用户拍板推倒重排】8 → 5。锁仍在，只是换成新基线。
TOP_MENUS = ("任务", "工具", "视图", "设置", "帮助")


def test_top_menus_unchanged():
    src = _mw()
    got = re.findall(r'mb\.addMenu\("([^"]+)"\)', src)
    assert got == list(TOP_MENUS), f"顶级菜单顺序被改动：{got}"
    assert "menuBar().addMenu" not in src, "不许新建第九个顶级菜单"


def test_every_long_menu_has_sections():
    """重排的核心：长菜单按功能分区，不再是一整片平铺。

    V0.0.19-E 更新：「工具」菜单从 11 组平铺改成 7 个子菜单分档，分组标题
    因此从 `m_tools.addSection` 挪进了各子菜单变量（m_site / m_ai / m_lib /
    m_data / m_stat / m_fix / m_log）。本护栏的**意图不变**（长菜单必须分区、
    不许有平铺大杂烩、不许恢复明暗开关），只是认新载体：工具区整体仍必须
    有分组标题，只是不再要求挂在 m_tools 这一个变量上。
    """
    src = _mw()
    # 任务 / 视图 / 设置 仍是平铺 + addSection（这三块没子菜单化）
    for var, name in (("m_task", "任务"), ("m_view", "视图"),
                      ("m_set", "设置")):
        hits = re.findall(rf'{var}\.addSection\("([^"]+)"\)', src)
        assert hits, f"「{name}」菜单没有分组标题"
        assert all("深色" not in h for h in hits), "不许恢复已撤掉的明暗开关"

    # 工具区：V0.0.19-E 起分组标题在各子菜单变量上，要求「工具」段内
    # （mb.addMenu("工具") 到下一个顶级菜单之间）仍有分组标题。
    i = src.index('mb.addMenu("工具")')
    j = src.index('mb.addMenu("视图")')
    tools_seg = src[i:j]
    tool_secs = re.findall(r'\.addSection\("([^"]+)"\)', tools_seg)
    assert tool_secs, "「工具」区没有分组标题（子菜单化后仍需分区）"
    assert all("深色" not in h for h in tool_secs), "不许恢复已撤掉的明暗开关"

    sections = re.findall(r'\.addSection\("([^"]+)"\)', src)
    assert len(sections) >= 15, f"分组标题只有 {len(sections)} 处，重排没落地"
    # 认新结构里的分区名（工具子菜单化后，部分旧分区名并入子菜单标题）
    for want in ("任务管理", "执行", "网址与规则", "页面", "检索",
                 "界面模式", "外观", "启动与集成", "网络与常用行为", "退出"):
        assert want in sections, f"缺分组「{want}」"
    # 工具区子菜单标题本身承担「分类」职责，逐个钉住（一个都不能少）
    for sub in ("采集与站点", "AI 与学习", "图库与隔离", "数据与备份",
                "统计与看板", "检查与修复", "日志统计"):
        assert f'addMenu("{sub}")' in tools_seg, f"工具区缺子菜单「{sub}」"



def test_settings_center_is_first_class_entry():
    src = _mw()
    assert '"设置中心…", self' in src or "QAction(\"设置中心…\", self)" in src
    assert "self.act_settings_center.triggered.connect(self.show_settings_center)" in src
    assert "def show_settings_center(self):" in src
    # 侧栏与状态栏都要有快捷入口，且不能混进与 stack 页序绑定的导航按钮
    assert "self._nav_settings_btn" in src
    assert "self._status_settings_btn" in src
    assert "lv.addWidget(self._nav_settings_btn)" in src
    assert "_nav_btns.append" in src, "五个页面导航按钮必须保留"
    assert "SettingsCenterDialog(self.db, self.cfg, acts, self)" in src


def test_settings_center_sections_and_callbacks():
    src = _read(GUI / "settings_center.py")
    assert "class SettingsCenterDialog(QDialog):" in src
    assert "def __init__(self, db, cfg, actions: dict, parent=None):" in src
    for section in ("外观与皮肤", "启动与集成", "下载与保存", "AI 与学习",
                    "网络与安全", "数据与备份", "维护与诊断", "关于"):
        assert f'"{section}"' in src, f"设置中心缺分区「{section}」"
    assert "搜索设置项" in src
    assert "未找到匹配的设置项" in src
    # 说明文字里提到 QFileDialog 不算违规，只禁止真实调用（ast 精确判定）
    tree = ast.parse(src)
    bad = [n for n in ast.walk(tree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and isinstance(n.func.value, ast.Name)
           and n.func.value.id == "QFileDialog"]
    assert not bad, "设置中心不许调用静态文件对话框"
    assert "PICK_DIR_KEY" in src, "目录选择必须走注入回调"
    mw = _mw()
    for key in ("sync_menu_states", "pick_dir", "pick_file", "ai_model",
                "web", "hotkeys", "backup", "full_cleanup"):
        assert f'"{key}"' in mw, f"主窗口没注入设置中心回调 {key}"


def test_menu_state_sync_blocks_signals():
    """设置中心改完开关后刷新菜单勾选态，绝不能反过来触发 toggled。"""
    src = _mw()
    i = src.index("def sync_settings_menu_states(self):")
    block = src[i:i + 2400]
    assert "blockSignals(True)" in block
    assert "act_web" in block, "Web 开关必须一起对齐"
    assert "ai_device_effective" in block, "推理设备四档必须一起对齐"


# ---------------------------------------------------------------- 桌面补齐
def test_desktop_new_entries_wired():
    src = _mw()
    for label, method in (
            ("完整备份管理…", "show_fullbackup_manager"),
            ("本机配置…", "show_sysinfo"),
            ("手机扫码登录…", "show_qrlogin"),
            ("删除模板", "_delete_selected_template"),
            ("从接口拉取模型列表", "fetch_models"),
            ("设置中心…", "show_settings_center")):
        assert label in src, f"缺动作「{label}」"
        assert method in src, f"动作「{label}」没接上 {method}"
    assert "def restore_full_backup(self, path: str):" in src
    assert 'def import_backup(self, path: str = ""):' in src
    assert "def _restore_task_table_columns(self):" in src
    assert "def _reset_one_task_column(self, idx):" in src


def test_new_desktop_modules_exist_and_clean():
    expect = {
        "settings_center.py": "class SettingsCenterDialog(QDialog):",
        "fullbackup_dialog.py": "class FullBackupDialog(QDialog):",
        "sysinfo_dialog.py": "class SysInfoDialog(QDialog):",
        "qrlogin_dialog.py": "class QRLoginDialog(QDialog):",
    }
    for name, marker in expect.items():
        src = _read(GUI / name)
        assert marker in src, f"{name} 缺类定义"
        assert "QFileDialog.get" not in src, f"{name} 用了静态文件对话框"
    om = _read(PKG / "online_models.py")
    assert "def fetch_models(" in om
    assert "urllib" in om and "PySide6" not in om, "在线模型模块必须无 Qt 依赖"


def test_threaded_dialogs_stop_worker_on_all_close_paths():
    """V0.0.17 冒烟抓到的真崩溃（0xC0000409）的回归护栏。

    凡是启动后台 QThread 的对话框，程序化关闭（done/accept）不走
    reject/closeEvent；线程还在跑时对话框被回收 = QThread 带活线程销毁
    = Qt fail-fast 直接崩进程。done() 是 reject/accept 的共同底层出口，
    必须与 closeEvent 一起重写。
    """
    offenders = []
    for f in sorted(GUI.glob("*.py")):
        src = _read(f)
        if "QThread" not in src:
            continue
        tree = ast.parse(src)
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            meths = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
            if "_start_worker" not in meths:
                continue
            missing = {"done", "closeEvent"} - meths
            if missing:
                offenders.append(f"{f.name}::{cls.name} 缺关闭出口 {missing}")
    assert not offenders, f"带线程对话框关闭出口没堵全: {offenders}"


# ---------------------------------------------------------------- 网页补齐
WEB_ENDPOINTS = (
    "/api/web/hotkeys", "/api/db/master", "/api/image/reveal",
    "/api/diskspace/trend", "/api/logs/stats/export",
    "/api/skins", "/api/skins/css", "/api/skins/upload",
)


def test_web_endpoints_and_components():
    backend = _read(PKG / "webui.py")
    api = _read(WEB / "api.js")
    for ep in WEB_ENDPOINTS:
        assert f'"{ep}"' in backend, f"后端缺端点 {ep}"
        assert ep in api, f"api.js 缺具名封装 {ep}"
    for name in ("ShortcutsDialog.vue", "Onboarding.vue", "DiskTrend.vue",
                 "SkinManager.vue", "DbMaster.vue"):
        assert (WEB / "components" / name).is_file(), f"缺前端组件 {name}"
    # 加密导出红线：新统计报告走 .mrmx，绝不出明文。
    # 加密发生在方法体里，不在路由分发行旁边，所以按方法定义定位。
    j = backend.index("def _api_logs_stats_export(")
    seg = backend[j:j + 2000]
    assert "encrypt_bytes" in seg, "统计报告导出必须调用加密层"
    assert ".mrmx" in seg, "统计报告导出必须是 .mrmx 加密包"


def test_web_nav_and_routes_still_paired():
    app = _read(WEB / "App.vue")
    routes = _read(WEB / "main.js")
    navs = re.findall(r"path: '(/[a-z-]+)'", app)
    rt = re.findall(r"path: '(/[a-z-]+)'", routes)
    assert navs, "没解析出导航项"
    assert set(navs).issubset(set(rt)), f"有导航没路由：{set(navs) - set(rt)}"


# ---------------------------------------------------------------- 打包与版本
def test_hiddenimports_and_version():
    spec = _read(ROOT / "build" / "pyinstaller.spec")
    for mod in ("media_resource_manager.online_models",
                "media_resource_manager.gui.settings_center",
                "media_resource_manager.gui.fullbackup_dialog",
                "media_resource_manager.gui.sysinfo_dialog",
                "media_resource_manager.gui.qrlogin_dialog",
                "segno"):
        assert f"'{mod}'" in spec, f"PyInstaller 漏了 {mod}"
    # 【V0.0.21 改锁】原来把版本钉死在 0.0.19，每次升版都要改测试，
    # 属于「钉旧值」。改为钉关系：版本号格式合法、单调不回退、且与
    # 打包注释里的版本一致。具体版本由 __init__.py 单一真源决定。
    import re as _re
    from media_resource_manager import VERSION
    m = _re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m, f"版本号格式不合法：{VERSION}"
    assert int(m.group(1)) >= 19, f"版本号发生回退：{VERSION}"
    assert f"V{VERSION}" in spec, "打包注释里的版本与 __init__.py 不一致"


def test_all_changed_python_parses():
    files = [MW, PKG / "webui.py", PKG / "site_ops.py",
             PKG / "online_models.py"]
    files += [GUI / n for n in ("settings_center.py", "fullbackup_dialog.py",
                                "sysinfo_dialog.py", "qrlogin_dialog.py")]
    for p in files:
        ast.parse(_read(p), filename=str(p))
