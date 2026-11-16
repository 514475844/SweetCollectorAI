"""V0.0.17 设置中心 · 纯静态单测（不创建 QApplication，不导入 PySide6）。

只读取 settings_center.py 源码做 ast / 字符串校验：
类名与构造签名、8 个分区名、搜索框、真实 config 字段名、禁用词、
无静态 QFileDialog、actions 缺失容错。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager" / "gui" / "settings_center.py"
CONFIG = ROOT / "media_resource_manager" / "config.py"

# 8 个分区，顺序即产品主链路（与 settings_center.SECTIONS 对齐）。
SECTION_NAMES = (
    "外观与皮肤", "启动与集成", "下载与保存", "AI 与学习",
    "网络与安全", "数据与备份", "维护与诊断", "关于",
)

# 真实存在于 config.AppConfig 的字段名（逐个核对过，禁止凭猜测书写）。
CONFIG_FIELDS = (
    "ai_threads", "min_free_mb", "sensitive_words", "learn_enabled",
    "ai_device", "web_port", "web_bind", "backup_sync_dir",
    "recycle_keep_days", "thumbnail_size", "max_pages", "convert_to_jpg",
    "log_sanitize", "tray_start_minimized", "task_done_goto_gallery",
    "review_key_ask", "ai_model_path",
)

# 界面禁用词（合并用户护栏、r240、v013 的口径）。
BAD_WORDS = (
    "你", "您", "咱", "俺", "吧", "呢", "啊", "呀", "哈", "呗", "嘛", "喔",
    "搞", "弄", "咋", "啥",
    "千万", "绝对", "简直", "超级", "无敌", "翻车", "砸站", "手滑",
    "随便", "记住", "别忘", "一下", "点儿", "顺手", "顺便", "其实",
    "反正", "大概", "差不多", "偷懒", "神器", "一键",
    "白费", "瞎", "乱杀", "骗人", "吓人",
    "点一下", "点开", "搞定", "折腾", "费劲",
    "找不到", "还没填", "还没设置", "还没下载", "还没有",
    "还没", "手动", "一下子", "试试", "没法", "不管了",
    "要找", "别找",
)

STATIC_PICKERS = ("getExistingDirectory", "getOpenFileName",
                  "getSaveFileName", "getOpenFileNames")


def _source() -> str:
    return SRC.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source())


def _class_node() -> ast.ClassDef:
    for node in _tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "SettingsCenterDialog":
            return node
    raise AssertionError("未找到 SettingsCenterDialog 类")


def _string_constants() -> list[str]:
    """源码里全部字符串常量（含 docstring），用于文案与禁用词检查。"""
    out = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


# ------------------------------------------------------------------ 结构
def test_class_and_init_signature():
    """类名、基类、构造签名固定，主窗口按此接线。"""
    cls = _class_node()
    bases = [b.id for b in cls.bases if isinstance(b, ast.Name)]
    assert "QDialog" in bases, "基类应为 QDialog"

    init = next((n for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
    assert init is not None, "缺少 __init__"
    args = [a.arg for a in init.args.args]
    assert args == ["self", "db", "cfg", "actions", "parent"], args
    assert len(init.args.defaults) == 1, "parent 应带默认值 None"


def test_module_uses_relative_imports_and_future_annotations():
    src = _source()
    assert "from __future__ import annotations" in src
    assert "from ..config import" in src or "from .. import" in src
    assert "from . import theme" in src


def test_eight_sections_present():
    src = _source()
    for name in SECTION_NAMES:
        assert name in src, f"缺少分区：{name}"
    # 导航与分页控件
    assert "QListWidget" in src
    assert "QStackedWidget" in src
    assert "QScrollArea" in src
    assert "QFormLayout" in src
    assert "QGroupBox" in src


# ------------------------------------------------------------------ 搜索
def test_search_box_present():
    src = _source()
    assert "QLineEdit" in src
    assert "搜索设置项" in src
    assert "未找到匹配的设置项" in src
    assert "_refresh_search" in src
    assert "ensureWidgetVisible" in src, "单击结果需滚动到对应设置项"
    assert "setFrameShape" in src, "单击结果需高亮对应设置项"


# ------------------------------------------------------------------ 字段
def test_at_least_eight_real_config_fields():
    """字段名必须同时出现在新源码与 config.AppConfig 定义里。"""
    src = _source()
    cfg_src = CONFIG.read_text(encoding="utf-8")
    hit = []
    for field in CONFIG_FIELDS:
        # 字段名以字符串字面量形式出现（引号包裹）
        if not re.search(r'["\']' + re.escape(field) + r'["\']', src):
            continue
        # 且确实是 AppConfig 的字段（防凭猜测书写）
        assert re.search(r'^\s*' + re.escape(field) + r'\s*:', cfg_src, re.M), \
            f"{field} 不是 config.AppConfig 的真实字段"
        hit.append(field)
    assert len(hit) >= 8, f"内联真实字段不足 8 个：{hit}"


def test_inline_uses_matching_widget_types():
    src = _source()
    for widget in ("QSpinBox", "QCheckBox", "QLineEdit", "QComboBox",
                   "QPlainTextEdit"):
        assert widget in src, f"缺少控件类型：{widget}"


# ------------------------------------------------------------------ 文案
def test_no_banned_words_in_user_strings():
    """字符串常量里不得出现口语词、第二人称、术语旧称。"""
    for s in _string_constants():
        for w in BAD_WORDS:
            assert w not in s, f"用户可见文案出现禁用词「{w}」：{s[:60]}"


def test_no_internal_ids_in_user_strings():
    """界面文案不得出现内部单号 R## / #数字。"""
    for s in _string_constants():
        assert not re.search(r"(?<![A-Za-z0-9])R\d{2,3}(?!\d)", s), s
        assert not re.search(r"#\d{1,3}", s), s


def test_no_qapplication_started():
    src = _source()
    assert "QApplication" not in src
    assert "app.exec" not in src


# ------------------------------------------------------------------ 文件对话框
def test_no_static_qfiledialog():
    """禁止阻塞式静态文件选择（真机原生后端会崩）。"""
    src = _source()
    for name in STATIC_PICKERS:
        assert "QFileDialog." + name not in src
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in STATIC_PICKERS, \
                f"出现静态文件选择调用：{node.func.attr}"


def test_dir_file_picks_go_through_actions():
    src = _source()
    assert "pick_dir" in src
    assert "pick_file" in src
    assert "get(" in src or " in self.actions" in src


# ------------------------------------------------------------------ actions 容错
def test_actions_missing_fallback_exists():
    """缺失的键必须禁用按钮并给出统一提示，绝不抛异常。"""
    src = _source()
    assert "该设置入口尚未接入" in src
    assert "setEnabled(False)" in src
    assert "isinstance(actions, dict)" in src
    assert "callable(" in src
    assert "sync_menu_states" in src, "布尔开关需通过可选回调刷新菜单勾选态"
    # 详细设置入口键至少覆盖契约清单
    for key in ("ai_model", "web", "hotkeys", "rate_plan", "db_encryption",
                "onboarding", "skins_dir", "proxy", "cookie", "subs",
                "diskspace", "backup", "full_cleanup", "diagnostic",
                "shortcuts_help"):
        assert '"' + key + '"' in src or "'" + key + "'" in src, \
            f"缺少详细设置入口键：{key}"


def test_bottom_button_bar_and_about_page():
    src = _source()
    for label in ("应用", "确定", "取消", "恢复默认值"):
        assert '"' + label + '"' in src, f"底部缺少按钮：{label}"
    assert "save_config" in src
    assert "AppConfig()" in src, "恢复默认值需新建 AppConfig"
    assert "__version__" in src or "VERSION" in src
    assert "user_data_dir" in src
    assert "PRESETS" in src and "list_custom_skins" in src
