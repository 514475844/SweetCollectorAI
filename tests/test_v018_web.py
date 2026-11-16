"""V0.0.18 · 网页端补齐的静态护栏（源码字面量 + ast）。

覆盖 6 组端点与界面接线：
  1 素材集导出前预览（/api/export/kit/preview，两步工作流 + 标注模板）
  2 导出历史（/api/export/history，Export.vue 表格）
  3 站点统计 / Cookie 提醒（/api/site-stats、/api/cookies/hints → Report.vue）
  4 通知中心（/api/notifications 三端点 + 顶栏铃铛 + 服务端 notify.add 动作点）
  5 全局搜索（/api/search/all 在 webui 内实现 + 顶栏搜索框）
  6 程序更新检查（/api/update/check → Settings.vue 关于区）

纪律：
* **只做静态检查**：读源码文本 + ast.parse，不 import 业务模块、不起
  QApplication、不联网、不启动服务。
* 端点按「do_GET / do_POST 里逐字面量列出」验证，与既有护栏同口径。
* 书面文案禁词表沿用 v017 / r240 / v013 的口径，扫本轮新增 .vue 内容。
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
SRC = PKG / "webapp" / "src"
API = SRC / "api.js"
APP = SRC / "App.vue"
EXPORT = SRC / "views" / "Export.vue"
REPORT = SRC / "views" / "Report.vue"
SETTINGS = SRC / "views" / "Settings.vue"
ICONS = SRC / "icons.js"

WEBUI_AST = ast.parse(WEBUI.read_text(encoding="utf-8"))
WEBUI_SRC = WEBUI.read_text(encoding="utf-8")

# 与 v017 / r240 / v013 同口径的口语/禁用词
FORBIDDEN = [
    "点一下", "点开", "搞", "搞定", "其实", "反正", "大概", "差不多",
    "折腾", "费劲", "随便", "还没", "还没有", "找不到", "还没填",
    "还没设置", "还没下载", "手动", "弄", "一下子", "试试", "没法",
    "不管了", "顺手", "顺便", "神器", "一键", "白费", "瞎",
    "你", "您", "咱", "吧", "呢", "啊", "呀",
]
EXEMPT = ["整理", "完整", "统一", "唯一", "一次性", "同一", "哈希"]


def _read(p: Path) -> str:
    assert p.is_file(), f"缺少文件 {p}"
    return p.read_text(encoding="utf-8")


def _hits(s: str) -> list[str]:
    t = s
    for w in EXEMPT:
        t = t.replace(w, "")
    return [w for w in FORBIDDEN if w in t]


def _user_strings(raw: str) -> list[str]:
    """用户可见中文串（剥注释/docstring 后取字符串字面量与模板文本节点）。"""
    t = re.sub(r'"""[\s\S]*?"""', '""', raw)
    t = re.sub(r"'''[\s\S]*?'''", "''", t)
    t = re.sub(r"<!--[\s\S]*?-->", "", t)
    t = re.sub(r"/\*[\s\S]*?\*/", "", t)
    t = re.sub(r"^\s*//.*$", "", t, flags=re.M)
    t = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    out = []
    for m in re.finditer(r"""["'`]([^'"`\n]{2,300})["'`]""", t):
        s = m.group(1)
        if re.search(r"[\u4e00-\u9fa5]", s):
            out.append(s)
    tplm = re.search(r"<template>([\s\S]*?)</template>", t)
    if tplm:
        tpl = tplm.group(1)
        for m in re.finditer(r">([^<>{}\n]{2,300})<", tpl):
            s = re.sub(r"\{\{[\s\S]*?\}\}", "", m.group(1)).strip()
            if s and re.search(r"[\u4e00-\u9fa5]", s):
                out.append(s)
    return out


def _func_src(name: str) -> str:
    """取 webui.py 里某个方法的源码（ast 定位，护栏按方法体判）。"""
    for node in ast.walk(WEBUI_AST):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(WEBUI_SRC, node) or ""
    return ""


def _handler_method_names() -> set[str]:
    out = set()
    for node in ast.walk(WEBUI_AST):
        if isinstance(node, ast.ClassDef) and node.name == "_Handler":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef):
                    out.add(sub.name)
    return out


# ---------------------------------------------------------------- 后端端点

GET_ENDPOINTS = (
    "/api/export/history", "/api/site-stats", "/api/cookies/hints",
    "/api/notifications", "/api/search/all", "/api/update/check",
)
POST_ENDPOINTS = (
    "/api/export/kit/preview", "/api/notifications/read",
    "/api/notifications/clear",
)
NEW_METHODS = (
    "_api_export_kit_preview", "_api_export_history", "_api_site_stats",
    "_api_cookies_hints", "_api_notifications_list", "_api_notifications_read",
    "_api_notifications_clear", "_api_search_all", "_api_update_check",
)


def test_backend_python_files_parse():
    ast.parse(WEBUI_SRC)  # 语法坏了这里就红


@pytest.mark.parametrize("ep", GET_ENDPOINTS)
def test_new_get_endpoints_are_routed(ep):
    """do_GET 里必须有逐字面量路由（护栏按源码字面量判路由）。"""
    do_get = _func_src("do_GET")
    assert f'path == "{ep}"' in do_get, f"GET {ep} 没接路由"


@pytest.mark.parametrize("ep", POST_ENDPOINTS)
def test_new_post_endpoints_are_routed(ep):
    do_post = _func_src("do_POST")
    assert f'path == "{ep}"' in do_post, f"POST {ep} 没接路由"


@pytest.mark.parametrize("name", NEW_METHODS)
def test_handler_methods_exist(name):
    assert name in _handler_method_names(), f"_Handler 缺少方法 {name}"


def test_preview_reuses_export_kit_and_training_kit_takes_ids():
    """预览必须调 export_kit.preview_training_kit（禁止另写一套口径）；
    training-kit 必须透传 ids 与 caption_template（勾选白名单闭环）。"""
    pv = _func_src("_api_export_kit_preview")
    assert "preview_training_kit" in pv
    tk = _func_src("_api_training_kit")
    assert "caption_template" in tk and '"ids"' in tk


def test_site_stats_and_hints_reuse_shared_modules():
    st = _func_src("_api_site_stats")
    assert "task_stats" in st and "site_stats" in st
    hn = _func_src("_api_cookies_hints")
    assert "cookie_expiry_hints" in hn
    eh = _func_src("_api_export_history")
    assert "export_history" in eh


def test_search_all_is_readonly_like_in_webui():
    """#7 全局搜索：webui 端点转调 searchall 共享口径（V0.0.18 抽出，
    桌面共用同一份），只读 LIKE、参数化 SQL、q 截 60 的判据落在
    searchall.py 本体上。"""
    src = _func_src("_api_search_all")
    assert src, "_api_search_all 不存在"
    assert "searchall" in src and "search_all(" in src, \
        "端点必须转调共享模块，不许两端各写一套"

    mod = _read(PKG / "searchall.py")
    assert "INSERT" not in mod.upper() and "UPDATE" not in mod.upper() \
        and "DELETE" not in mod.upper(), "全局搜索必须是只读"
    assert "[:60]" in mod, "q 必须截断 60 字符"
    # 三组查询全部参数化（? 占位），LIKE 关键词不进 SQL 文本
    for table in ("tasks", "images", "audit_log"):
        assert table in mod, f"缺少 {table} 分组"
    assert mod.count("LIKE ?") >= 5
    # 图库结果必须带现成缩略图字段
    assert "/thumb/" in mod
    # 分组结构
    for key in ('"tasks"', '"images"', '"logs"'):
        assert key in mod


def test_update_check_delegates_update_check_module():
    src = _func_src("_api_update_check")
    assert "update_check" in src and "check(self.cfg)" in src


def test_notifications_clear_requires_confirm():
    """清空通知走 _confirmed 双保险。"""
    src = _func_src("_api_notifications_clear")
    assert "_confirmed(body)" in src


# ---------------------------------------------------------------- 服务端 notify.add 动作点

NOTIFY_ACTIONS = (
    "_api_task_batch",        # 任务批量操作
    "_api_training_kit",      # 素材集导出
    "_api_site_logs_clear",   # 清空日志
    "_api_site_cleanup",      # 全面清理
    "_api_site_backup_sync",  # 备份同步
)


@pytest.mark.parametrize("name", NOTIFY_ACTIONS)
def test_notify_add_wired_at_action_points(name):
    """5 个动作点成功后必须调 notify.add，且包 try/except（通知失败不伤主流程）。"""
    src = _func_src(name)
    assert src, f"{name} 不存在"
    assert "notify.add(" in src, f"{name} 未接 notify.add"
    # 必须在 try 块内（缩进启发式：notify.add 前有 try: 行）
    seg = src.split("notify.add(")[0]
    assert "try:" in seg[-400:], f"{name} 的 notify.add 未包 try"


# ---------------------------------------------------------------- 前端接线

def test_new_endpoints_have_named_api_methods():
    api_src = _read(API)
    for m in ("exportKitPreview", "exportHistory", "siteStats", "cookieHints",
              "notifications", "notificationsRead", "notificationsClear",
              "searchAll", "updateCheck"):
        assert f"{m}:" in api_src, f"api.js 缺少具名方法 {m}"
    for ep in GET_ENDPOINTS + POST_ENDPOINTS:
        assert ep in api_src, f"api.js 未接端点 {ep}"


def test_export_two_step_workflow():
    """Export.vue：先预览、逐张勾选（全选/反选/取消）、再导出带 ids；
    标注模板输入存在并说明 {terms} 占位符。"""
    t = _read(EXPORT)
    assert "doPreview" in t and "api.exportKitPreview" in t, "缺第一步预览"
    assert "payload.ids" in t, "导出不带勾选 ids"
    assert "selectAll" in t and "invertSelect" in t and "selectNone" in t
    assert "toggleOne" in t, "缺逐张勾选"
    assert "caption_template" in t, "缺标注模板输入"
    assert "{terms}" in t, "缺 {terms} 占位符说明"
    # 磁盘状态标记
    assert "源文件未找到" in t and "零字节" in t
    # 导出历史
    assert "api.exportHistory" in t and "导出历史" in t
    # 缩略图走现成端点 /thumb/
    assert "/thumb/" in t


def test_export_vue_has_no_bare_fetch_except_binary():
    """新增代码不裸写 fetch（二进制下载 downloadEncrypted 是唯一现有例外）。"""
    t = _read(EXPORT)
    fetches = re.findall(r"await fetch\(", t)
    assert len(fetches) == 1, "除加密导出下载外不得裸写 fetch"


def test_site_stats_lands_in_report_page():
    t = _read(REPORT)
    assert "api.siteStats" in t and "站点统计" in t
    assert "api.cookieHints" in t
    assert "疑似登录失效" in t
    # 成功率 / 速率 / 失败Top / 占用四类展示
    for w in ("成功率", "速率", "失败原因", "占用"):
        assert w in t
    # 提示条给去 Cookie 管理的入口（不新增导航项，跳设置页）
    assert "gotoCookie" in t and "/settings" in t


def test_bell_and_searchbox_in_app():
    t = _read(APP)
    # 铃铛：图标名 + 面板 + 轮询
    assert "'bell'" in t or '"bell"' in t or "name=\"bell\"" in t or ":name=\"notif.unread ? 'bell'" in t
    assert "通知中心" in t
    assert "api.notifications" in t and "api.notificationsRead" in t
    assert "api.notificationsClear" in t
    assert "setInterval(refreshNotif, 60000)" in t, "缺 60 秒未读轮询"
    assert "window.confirm" in t, "清空通知缺前端确认"
    # 全局搜索框：400ms 防抖 + 回车 + 分组结果
    assert "api.searchAll" in t
    assert "400" in t, "缺 400ms 防抖"
    assert "keyup.enter" in t
    for grp in ("任务", "图库", "日志"):
        assert grp in t
    # 三组跳转
    assert "router.push" in t


def test_new_icon_names_defined():
    """前端用到的新增图标名必须在 icons.js 有定义（r234 护栏同口径）。"""
    icons = set(re.findall(r'^\s*"([a-z0-9-]+)":', _read(ICONS), re.M))
    for n in ("bell", "bell-off", "eye"):
        assert n in icons, f"icons.js 缺少 {n}"
    app = _read(APP)
    used = set(re.findall(r"'([a-z0-9-]+)'", app)) & {"bell", "bell-off"}
    for u in used:
        assert u in icons


def test_update_check_lands_in_settings_about():
    t = _read(SETTINGS)
    assert "api.updateCheck" in t, "Settings 未接 /api/update/check"
    assert "关于" in t and "检查程序更新" in t
    # 有新版本给下载页链接、新标签打开
    assert 'target="_blank"' in t and "前往下载页" in t


# ---------------------------------------------------------------- 书面文案

# v013 / r240 的整文件清单（这两条护栏在现有文件上本来就是绿的，
# 整文件扫只用到这两个清单的词；更严的 FORBIDDEN 用于本轮新增语句）
FULL_LISTS = [
    "点一下", "点开", "搞定", "顺手", "顺便", "其实", "反正",
    "大概", "差不多", "折腾", "费劲", "随便",
    "找不到", "还没填", "还没设置", "还没下载", "还没有",
]

# 本轮新增、必须真实存在且过严格禁词表的用户可见句子
NEW_PHRASES = {
    "App.vue": ["确定清空全部通知吗？此操作不可撤销。", "暂无通知",
                "通知中心", "标记已读", "全部已读", "搜索任务 / 图库 / 日志"],
    "views/Export.vue": ["预览不落盘，仅列出候选图片供核对与勾选。",
                         "源文件未找到", "零字节", "可导出", "标注模板",
                         "{terms}", "留空则用默认写法", "暂无导出记录。",
                         "导出历史", "筛选摘要", "生成预览"],
    "views/Report.vue": ["站点统计", "疑似登录失效", "前往 Cookie 管理",
                         "失败原因", "成功率", "暂无失败记录"],
    "views/Settings.vue": ["检查程序更新", "前往下载页", "检查结果"],
}


def test_full_files_clean_vs_existing_guardlists():
    """整文件扫（只按 r240/v013 已有清单）：本轮新增内容不得使老护栏变红。"""
    bad = []
    for f in (EXPORT, REPORT, APP, SETTINGS):
        for s in _user_strings(_read(f)):
            h = [w for w in FULL_LISTS if w in s]
            if h:
                bad.append((f.name, s[:90], h[:3]))
    assert not bad, "本轮界面出现口语化文案：\n" + "\n".join(
        f"  {n}: {s}  ← {h}" for n, s, h in bad)


def test_new_copy_phrases_exist_and_strict_clean():
    """本轮新增语句存在，且过最严禁词表（含 v017 口径：试试/还没/还没…等）。"""
    rel_map = {
        "App.vue": APP, "views/Export.vue": EXPORT,
        "views/Report.vue": REPORT, "views/Settings.vue": SETTINGS,
    }
    for rel, phrases in NEW_PHRASES.items():
        text = _read(rel_map[rel])
        for p in phrases:
            assert p in text, f"{rel} 缺少本轮新增文案：{p}"
        # 逐句过严格表：句子本身在文件里，字符串口径按整句扫
        for p in phrases:
            h = _hits(p)
            assert not h, f"{rel} 新增文案含禁用词：{p} → {h}"


def test_copy_uses_formal_missing_wording():
    """本轮新增文案的缺失口径统一：「未找到 / 暂无 / 尚未」。"""
    t = _read(EXPORT)
    assert "源文件未找到" in t
    assert "暂无导出记录" in t
    r = _read(REPORT)
    assert "暂无站点统计数据" in r or "暂无失败记录" in r
    assert "暂无通知" in _read(APP)


def test_terms_use_target_and_exclusion_language():
    """术语统一：界面用「目标词」，不许出现「要找 / 别找」。"""
    for f in (EXPORT, REPORT, APP, SETTINGS):
        for s in _user_strings(_read(f)):
            assert "要找" not in s and "别找" not in s, f"{f.name}: {s}"
    # V0.0.27 改判：导出页把「附加目标词」改成了更直白的「额外写进标注的词」，
    # 功能没变（那串词会一起写进每张图的标注文件）。
    assert "额外写进标注的词" in _read(EXPORT)


def test_nav_and_routes_unchanged():
    """15 个导航项与 main.js 路由一一对应（新功能只进现有页/顶栏）。"""
    main = _read(SRC / "main.js")
    app = _read(APP)
    block = app.split("const navs = [", 1)[1].split("\n]", 1)[0]
    paths = re.findall(r"path: '([^']+)'", block)
    assert len(paths) == 15, f"导航项数量变化：{len(paths)}"
    for p in paths:
        assert f"path: '{p}'" in main, f"main.js 缺少路由 {p}"
