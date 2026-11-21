"""V0.0.17 · 网页端补齐的静态护栏（源码字面量 + ast）。

覆盖 8 项功能的后端端点、前端入口、加密导出红线与界面书面文案：
  1 键盘快捷键说明与自定义  2 新手引导  3 数据库主密码  4 剪贴板直读
  5 在文件夹中显示          6 磁盘 7 日趋势  7 日志统计报告加密导出
  8 自定义皮肤

纪律：
* **只做静态检查**：读源码文本 + ast.parse，不 import 业务模块、不起
  QApplication、不联网、不启动服务，不依赖 PySide6 是否安装。
* 端点按「do_GET / do_POST 里逐字面量列出」验证，与既有护栏同口径。
* 书面文案既做「存在」的正向断言，也做禁用词表的反向断言；新组件整份
  扫描，既有文件只校验本轮新增的具体句子，避免与历史文案互相干扰。
* 断言都指向具体字面量，不放宽成永真。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"
WEBUI = PKG / "webui.py"
SITE_OPS = PKG / "site_ops.py"
SRC = PKG / "webapp" / "src"
API = SRC / "api.js"
APP = SRC / "App.vue"
MAIN = SRC / "main.js"

CJK = re.compile("[\u4e00-\u9fa5]")

# 用户 2026-11 明确列出的口语/禁用词（叠加 r240 / v013 的高频项）
FORBIDDEN = [
    "点一下", "点开", "搞", "搞定", "其实", "反正", "大概", "差不多",
    "折腾", "费劲", "随便", "还没", "还没有", "找不到", "还没填",
    "还没设置", "还没下载", "手动", "弄", "一下子", "试试", "没法",
    "不管了", "顺手", "顺便", "神器", "一键", "白费", "瞎",
    "你", "您", "咱", "吧", "呢", "啊", "呀", "要找", "别找",
    "千万", "绝对", "简直", "超级", "无敌", "翻车", "砸站", "手滑",
]
INTERNAL_ID = re.compile(r"(?<![A-Za-z0-9])R\d{2,3}(?!\d)|#\d{1,3}\s*(?:[，。、）)】]|[\u4e00-\u9fa5])")

GET_ENDPOINTS = (
    "/api/diskspace/trend", "/api/logs/stats/export",
    "/api/skins", "/api/skins/css", "/api/web/hotkeys", "/api/db/master",
)
POST_ENDPOINTS = (
    "/api/image/reveal", "/api/web/hotkeys", "/api/db/master",
    "/api/skins/upload",
)
NEW_METHODS = (
    "_api_image_reveal", "_api_diskspace_trend", "_api_logs_stats_export",
    "_api_web_hotkeys_get", "_api_web_hotkeys_set",
    "_api_db_master_get", "_api_db_master_set",
    "_api_skins", "_api_skins_css", "_api_skins_upload",
)
NEW_COMPONENTS = (
    "ShortcutsDialog.vue", "Onboarding.vue", "DiskTrend.vue",
    "SkinManager.vue", "DbMaster.vue",
)


def _read(p) -> str:
    return Path(p).read_text(encoding="utf-8")


def _method_src(name: str) -> str:
    """按 ast 取出 _Handler 里某个方法的源码文本。"""
    src = _read(WEBUI)
    tree = ast.parse(src, str(WEBUI))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _strip_comments(t: str) -> str:
    t = re.sub(r"<!--[\s\S]*?-->", "", t)
    t = re.sub(r"/\*[\s\S]*?\*/", "", t)
    t = re.sub(r"^\s*//.*$", "", t, flags=re.M)
    return t


def _user_strings(raw: str) -> list[str]:
    """前端里用户可见的中文片段（字符串字面量 + 模板文本节点 + 提示属性）。"""
    t = _strip_comments(raw)
    tpl = "".join(re.findall(r"<template>([\s\S]*?)</template>", t))
    out = []
    for m in re.finditer(r"""["'`]([^"'`\n]{2,300})["'`]""", t):
        s = m.group(1)
        if CJK.search(s):
            out.append(s)
    for m in re.finditer(r">([^<>{}\n]{2,300})<", tpl):
        s = re.sub(r"\{\{[\s\S]*?\}\}", "", m.group(1)).strip()
        if s and CJK.search(s):
            out.append(s)
    for m in re.finditer(r'(?:title|placeholder|alt|aria-label)="([^"]{2,300})"', tpl):
        if CJK.search(m.group(1)):
            out.append(m.group(1))
    return out


def _hits(s: str) -> list[str]:
    return [w for w in FORBIDDEN if w in s]


# ---------------------------------------------------------------- 后端语法

def test_backend_python_files_parse():
    """改过的 .py 必须能被 ast.parse（不起服务、不导入）。"""
    for p in (WEBUI, SITE_OPS):
        ast.parse(_read(p), str(p))


def test_handler_methods_exist():
    tree = ast.parse(_read(WEBUI), str(WEBUI))
    methods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "_Handler":
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(b.name)
    for m in NEW_METHODS:
        assert m in methods, f"缺少后端方法 {m}"


def test_site_ops_new_functions_exist():
    tree = ast.parse(_read(SITE_OPS), str(SITE_OPS))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "reveal_image_path" in names
    assert "audit_log_stats_csv" in names


# ---------------------------------------------------------------- 路由

def test_new_get_endpoints_are_routed():
    src = _read(WEBUI)
    for ep in GET_ENDPOINTS:
        assert f'if path == "{ep}":' in src, f"{ep} 未接 do_GET 路由"


def test_new_post_endpoints_are_routed():
    src = _read(WEBUI)
    for ep in POST_ENDPOINTS:
        assert f'if path == "{ep}":' in src, f"{ep} 未接 do_POST 路由"


def test_new_endpoints_have_named_api_methods():
    js = _read(API)
    pairs = {
        "/api/diskspace/trend": "diskspaceTrend",
        "/api/logs/stats/export": "logStatsExportUrl",
        "/api/skins": "skins",
        "/api/skins/css": "skinCss",
        "/api/skins/upload": "skinUpload",
        "/api/web/hotkeys": "webHotkeys",
        "/api/db/master": "dbMaster",
        "/api/image/reveal": "imageReveal",
    }
    for ep, method in pairs.items():
        assert ep in js, f"api.js 缺少端点 {ep}"
        assert re.search(r"\b" + re.escape(method) + r"\s*:", js), \
            f"api.js 缺少具名方法 {method}（端点 {ep}）"
    assert "webHotkeysSave" in js and "dbMasterSet" in js


# ---------------------------------------------------------------- 加密红线

def test_log_stats_export_is_encrypted_mrmx():
    fn = _method_src("_api_logs_stats_export")
    assert fn, "未找到日志统计报告导出方法"
    assert "encrypt_bytes" in fn
    assert "audit_log_stats_csv" in fn
    assert ".mrmx" in fn
    assert 'application/octet-stream' in fn
    assert "text/csv" not in fn and "application/zip" not in fn
    # 明文统计 CSV 只在 site_ops 里生成，导出端点绝不直接下发
    assert "Content-Disposition" in fn


def test_db_master_reuses_crypt_and_never_persists_echo():
    get_fn = _method_src("_api_db_master_get")
    set_fn = _method_src("_api_db_master_set")
    assert "get_db_master_password" in get_fn
    assert '"set": has' in get_fn
    assert "set_db_master_password" in set_fn
    assert "crypt.encrypt_bytes" in set_fn and "crypt.decrypt_bytes" in set_fn
    # 原断言把 docstring 里「绝不写进 config.json」这句说明误当成落盘证据。
    # 升级为：剥掉 docstring 后，函数体里不得出现任何写盘动作（更严，不是放宽）。
    body = re.sub(r'"""[\s\S]*?"""', '""', set_fn)
    assert "config.json" not in body
    assert "save_config" not in body
    assert "write_bytes" not in body and "open(" not in body


# ---------------------------------------------------------------- 各项功能入口

@pytest.mark.parametrize("rel, needles", [
    # 1 快捷键：全局弹窗 + 侧栏入口 + 设置页入口 + 键位持久化与全局监听
    ("App.vue", ["ShortcutsDialog", "快捷键说明", "matchWebAction",
                 "setWebHotkeys", "F1"]),
    ("views/Settings.vue", ["openShortcuts", "快捷键说明"]),
    ("components/ShortcutsDialog.vue", ["桌面端键位（与 F1 说明对应）",
                                        "录键", "保存键位", "mrm-shortcuts"]),
    ("uiPrefs.js", ["WEB_HOTKEY_DEFAULTS", "matchWebAction", "setWebTheme"]),
    # 2 新手引导：V0.0.75 重写为 4 步流程引导 + 设置页重新运行 + localStorage 首次访问
    ("App.vue", ["Onboarding", "mrm-skin"]),
    ("views/Settings.vue", ["rerunOnboarding", "重新运行新手引导"]),
    ("components/Onboarding.vue", ["第 1 步 / 共 4 步", "第 2 步 / 共 4 步",
                                   "第 3 步 / 共 4 步", "第 4 步 / 共 4 步",
                                   "mrm.onboarding.done",
                                   "mrm-onboarding"]),
    # 3 数据库主密码：type=password、绝不回显、设置按钮
    ("views/Settings.vue", ["<DbMaster", "启用后请在下方设置主密码"]),
    ("components/DbMaster.vue", ["type=\"password\"", "设置主密码",
                                 "dbMasterSet", "当前状态"]),
    # 4 剪贴板直读：readText + 中文失败提示 + 保留手工粘贴
    ("views/Tasks.vue", ["从剪贴板读取", "navigator.clipboard.readText",
                         "手工粘贴", "<textarea"]),
    # 5 在文件夹中显示：右键菜单 + 具名方法
    ("components/ImageGrid.vue", ["在文件夹中显示", "api.imageReveal",
                                  "folder-open"]),
    ("site_ops.py", ["reveal_image_path", "已登记目录"]),
    # 6 磁盘 7 日趋势：端点 + 空态 + 纯 SVG
    ("views/Settings.vue", ["<DiskTrend"]),
    ("components/DiskTrend.vue", ["api.diskspaceTrend", "<svg", "empty"]),
    # 7 日志统计报告导出：加密 .mrmx
    ("views/Logs.vue", ["导出统计报告（加密）", "api.logStatsExportUrl()"]),
    # 8 自定义皮肤：列出/上传 + 选择统一到左下角下拉（V0.0.27 合并三处入口）
    ("views/Settings.vue", ["<SkinManager"]),
    ("components/SkinManager.vue", ["api.skins", "api.skinUpload",
                                    "自定义皮肤文件", "左下角"]),
])
def test_feature_entry_points(rel, needles):
    text = _read(SRC / rel) if (SRC / rel).is_file() else _read(PKG / rel)
    for n in needles:
        assert n in text, f"{rel} 缺少功能入口字面量：{n}"


# ---------------------------------------------------------------- 书面文案

# 本轮新增、必须真实存在且不含口语词的用户可见句子
NEW_COPY = {
    "App.vue": ["快捷键说明"],
    # V0.0.23 起「说明」与「改键」拆成两个入口，旧的那句合并文案已经删掉，
    # 这里改钉现在真实存在的两个按钮（钉关系不钉旧句）。
    "views/Settings.vue": ["启用后请在下方设置主密码", "重新运行新手引导",
                           "界面辅助", "查看快捷键说明", "自定义快捷键"],
    "views/Tasks.vue": ["从剪贴板读取",
                        "剪贴板读取失败：浏览器未授予读取权限或当前环境不支持，请手工粘贴网址后继续。",
                        "读取失败时请在下方文本框手工粘贴"],
    "views/Logs.vue": ["导出统计报告（加密）"],
    "components/ImageGrid.vue": ["在文件夹中显示", "已打开图片所在文件夹。"],
    "components/ShortcutsDialog.vue": [
        "桌面端键位（与 F1 说明对应）", "键位已保存并生效。",
        "单击「录键」后按下新的组合键，按 Esc 取消本次录入；也可在输入框中手工填写键位。",
        "桌面端快捷键对话框保存时会清理网页专用键位，请勿在两端交替修改。",
    ],
    "components/Onboarding.vue": [
        # V0.0.75：重写为 4 步流程（学图库 → 填网站 → 黑白名单 → 保存运行）
        "未匹配的站点走 gallery-dl 通用模式，支持大批网站。",
        "白名单（主题词）：仅保留画面命中这些词的图片，多个词以空格分隔。",
        "黑名单（排除词）：命中这些词的图片不予收录。",
        "确认以下信息，单击「保存并运行任务」立即开始采集。",
        "是否已经收藏有本地图片库？",
    ],
    "components/DiskTrend.vue": ["近 7 日增长趋势",
                                 "暂无历史快照数据，趋势将在每日记录后呈现。"],
    "components/SkinManager.vue": [
        "自定义皮肤文件", "上传自定义 CSS 文件", "尚未上传自定义皮肤",
        "换皮肤不在这里", "全软件只有这一处",
    ],
    "components/DbMaster.vue": ["设置主密码", "当前状态",
                                "主密码仅保存在运行内存中，界面不会回显已保存的密码。"],
}


def test_formal_copy_exists_and_is_clean():
    for rel, phrases in NEW_COPY.items():
        text = _read(SRC / rel)
        for phrase in phrases:
            assert phrase in text, f"{rel} 缺少书面文案：{phrase}"
            assert not _hits(phrase), \
                f"{rel} 文案含禁用词：{phrase} → {_hits(phrase)}"
            assert not INTERNAL_ID.search(phrase), \
                f"{rel} 文案含内部单号：{phrase}"


def test_new_components_have_no_colloquial_copy():
    bad = []
    for name in NEW_COMPONENTS:
        raw = _read(SRC / "components" / name)
        for s in _user_strings(raw):
            if _hits(s):
                bad.append((name, s[:80], _hits(s)))
            if INTERNAL_ID.search(s):
                bad.append((name, s[:80], "内部单号"))
    assert not bad, "新组件出现口语化文案：\n" + "\n".join(
        f"  {n}: {s} ← {h}" for n, s, h in bad)


def test_new_components_use_known_icon_names():
    icons = set(re.findall(r'^\s*"([a-z0-9-]+)":', _read(SRC / "icons.js"), re.M))
    assert len(icons) > 40
    for name in NEW_COMPONENTS:
        raw = _read(SRC / "components" / name)
        assert ("components/Icon.vue" in raw) or ("'./Icon.vue'" in raw), \
            f"{name} 用了图标却没 import"
        for m in re.finditer(r'name="([a-z0-9-]+)"', raw):
            assert m.group(1) in icons, f"{name} 用了不存在的图标 {m.group(1)}"
        for m in re.finditer(r":name=\"[^\"]*?'([a-z0-9-]+)'", raw):
            assert m.group(1) in icons, f"{name} 用了不存在的图标 {m.group(1)}"


# ---------------------------------------------------------------- 结构防回归

def test_nav_and_routes_unchanged():
    """15 个导航项与 main.js 路由一一对应，本轮没有新增导航项。"""
    app = _read(APP)
    assert "const navs = [" in app
    block = app.split("const navs = [", 1)[1].split("\n]", 1)[0]
    paths = re.findall(r"path: '([^']+)'", block)
    assert len(paths) == 15, f"导航项数量变化：{len(paths)}"
    main = _read(MAIN)
    for p in paths:
        assert f"path: '{p}'" in main, f"main.js 缺少路由 {p}"


def test_destructive_and_exit_switches_absent():
    """明确不做的开关：网页端不得出现退出服务 / 关闭 Web 功能。"""
    for rel in ("App.vue", "views/Settings.vue"):
        t = _read(SRC / rel)
        assert "退出服务" not in t
        assert "关闭 Web 功能" not in t
