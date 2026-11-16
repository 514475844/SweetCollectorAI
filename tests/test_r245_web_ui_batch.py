"""r245 护栏：2026-10-29 那批网页端界面整改（用户 17 条）。

这一批全是「界面挪位置 / 换形式」的改动，最容易在下一次改版里被悄悄改回去
（界面还在、但行为变了，没有任何测试会红）。所以把**每条的用户要求**钉成判据，
并在注释里写清为什么这样放：

  1 图库/审核/隔离区的「按任务」→ 工具栏下拉（紧跟「最新采集」）
  2 任务行内按钮 → 只留图标（文字竖排太丑）
  4 状态筛选 → 左侧导航「任务」下面，点任务才展开
  5 AI 模型页 → 本机配置（CPU/内存/GPU/硬盘/下载目录剩余空间）
  7 代理类型 → 单选（不再手打 socks5://）
  8 网页皮肤 → 删「暗夜」，「qBittorrent」改名「深色」
  9 限速时段表 → 不再横向溢出
 10 设置页红块 → 重复的 .btn.danger 盖掉了白字（红底红字像空块）
 11 旧版网页 → 入口下线（/classic 302，/ 恒定新版）
 12 底栏 → 地址在左、进度在右下
 13 左上角 → 只留 logo（去掉 Sweet 文字）并放大
 14 右下角 → 回到顶部
 15 侧栏 → logo 右边的收起按钮
 16 exe 闪屏 → 白底（原来取的是深色调色板 = 黑底）
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager/webapp/src"


def _read(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1 图库下拉
def test_gallery_task_filter_is_toolbar_dropdown():
    g = _read("components/ImageGrid.vue")
    assert 'class="tasksel"' in g
    assert 'v-model="taskFilter"' in g
    # 紧跟排序下拉，且在方向筛选之前
    seg = g.split('v-model="sort"', 1)[1]
    assert "tasksel" in seg.split('v-model="orient"', 1)[0]
    # 左侧不再有「按任务」这个分组（长列表已挪走）
    assert '<div class="fg-title">按任务' not in g
    # 2026-10-29（第 1 条）：标签也搬进工具栏了，左侧整块面板撤掉
    assert 'class="gside card"' not in g
    assert 'class="tagsel"' in g


# ---------------------------------------------------------------- 2 图标按钮
def test_task_row_actions_are_icon_only():
    t = _read("views/Tasks.vue")
    acts = t.split('<span class="acts"', 1)[1].split("</span>", 1)[0]
    # 只留图标：这一列里不许再出现**可见**中文（竖排的根源）。
    # title / aria-label 里的文字必须保留（去掉文字后用户/读屏软件靠它认按钮）。
    import re
    visible = re.sub(r'(?:title|aria-label)="[^"]*"', "", acts)
    for word in ("开始", "暂停", "编辑", "失败网址", "存模板", "删除"):
        assert word not in visible, f"行内按钮仍有可见文字「{word}」（会竖排）"
    assert acts.count('class="btn sm ico') >= 6
    # 图标按钮必须有 title，否则去掉文字后用户不知道是什么
    assert acts.count("title=") >= 6
    # 全站按钮禁换行（中文一个字一行 = 第 2 条的病根）
    assert "white-space: nowrap" in t


# ---------------------------------------------------------------- 4 侧栏子菜单
def test_status_filter_moved_into_sidebar():
    a = _read("App.vue")
    assert "const TASK_VIEWS = [" in a
    assert "taskOpen" in a and "goTask(" in a
    assert 'class="subnav"' in a
    # 点任务这个条目才展开（要求原文）：显隐由 taskOpen 控制，
    # 且点「任务」会自动展开（收起状态下点一下就该看到子项）。
    assert 'v-if="taskOpen"' in a
    assert '@click="taskOpen = true"' in a
    # 子菜单不能绑当前路由 —— 换到图库就整段消失会像"树自己收起来了"
    assert 'v-if="taskOpen && route.path' not in a
    # 用 URL 承载筛选：刷新/后退/收藏都能落在同一个筛选上
    assert "route.query.status" in a
    t = _read("views/Tasks.vue")
    assert "String(route.query.status" in t
    assert "watch(() => route.query.status" in t
    # 页面里那个独立左栏已移除
    assert 'class="filters card"' not in t


# ---------------------------------------------------------------- 5 机器信息
def test_sysinfo_module_and_api():
    from media_resource_manager import sysinfo

    d = sysinfo.collect()
    assert d["cpu"]["cores_logical"] >= 1
    assert "memory" in d and "total" in d["memory"]
    assert isinstance(d["gpus"], list)
    assert d["drives"], "至少列出一个盘"
    for k in ("app_version", "platform", "onnx_providers"):
        assert k in d
    assert sysinfo.headline(d)

    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    assert '"/api/sysinfo"' in src and "_api_sysinfo" in src
    m = _read("views/Models.vue")
    assert "api.sysinfo()" in m
    for word in ("处理器", "内存", "显卡", "当前下载目录", "推理后端"):
        assert word in m, f"模型页缺「{word}」"
    a = _read("api.js")
    assert "sysinfo:" in a


def test_sysinfo_handles_missing_paths(tmp_path):
    """目录不存在时**往上找第一个存在的祖先**（下载目录可能还没建），不能抛。

    既不是「直接报错」，也不是「返回空」—— 用户刚配好一个还没建出来的输出
    目录时，最需要的恰恰是「那个盘还剩多少」。
    """
    from media_resource_manager import sysinfo

    item = sysinfo.disk_item(tmp_path / "不存在" / "更深")
    assert item["total"] and item["free"] is not None
    assert item["free_text"] != "—"
    # 真正的空值（拿不到容量）走 "—"，不会显示 0 B
    assert sysinfo._fmt_bytes(None) == "—"


# ---------------------------------------------------------------- 7 代理类型
def test_proxy_type_is_radio_group():
    p = _read("views/Proxies.vue")
    assert "const PROXY_TYPES = [" in p
    assert 'type="radio"' in p and "v-model=\"form.scheme\"" in p
    for v in ("http", "https", "socks5"):
        assert f"v: '{v}'" in p
    # 保存时按字段拼 url，不再让人手打协议前缀
    assert "url: `${form.scheme}://${userinfo}${host}:${Number(form.port)}`" in p
    # 改类型顺手换常用端口
    assert "defaultPort(t.v)" in p


# ---------------------------------------------------------------- 8 皮肤
def test_web_themes_drop_night_rename_qbittorrent():
    a = _read("App.vue")
    assert "'night'" not in a.split("const THEMES")[1].split("]", 1)[0]
    assert "深色" in a
    # 老缓存里的 night 要平滑迁到深色，不能变回白色（看起来像设置没保存）
    assert "LEGACY" in a and "night: 'qbittorrent'" in a
    # V0.0.27 改判（用户原话「设置里两个外观选择，加上左下角一共 3 个，这对吗？」）：
    # 设置页那个「网页皮肤」下拉已删除，换皮肤只剩左下角一处。这里从「下拉里有
    # 深色」改钉「设置页不再有第二个皮肤下拉 + 深色仍在左下角那份列表里」。
    s = _read("views/Settings.vue")
    assert "k: 'web_theme'" not in s, "设置页又长出第二个皮肤下拉了"
    assert "深色" in a.split("const THEMES")[1].split("]", 1)[0]


# ---------------------------------------------------------------- 9 时段表
def test_rate_plan_table_cannot_overflow():
    """时段表的判据换过两次，目标一直没变：**不许横向越界**。

    V0.0.26 原生 table 列宽对不齐 → V0.0.27 改行式卡片 → 行式卡片每行自带
    标签、列永远错位（用户连问三轮「是不是超出边框了」）→ V0.0.42 改回有表头
    的网格表格，表头与数据行共用同一条 grid-template-columns。
    这里钉的是「共用列宽 + 列宽有弹性下限 + 窄窗口降级」，不再钉具体是不是 table。
    """
    s = _read("views/Settings.vue")
    assert ".plantable {" in s and ".planhead, .planrow {" in s
    seg = s.split(".planhead, .planrow {", 1)[1].split("}", 1)[0]
    assert "grid-template-columns" in seg, "表头与数据行没共用列宽 ⇒ 列会错位"
    assert "minmax(" in seg, "列宽没有弹性下限 ⇒ 窄窗口会横向溢出"
    # 窄窗口必须把不关键的列藏起来，而不是硬挤成一条线
    assert "planhead span:nth-child(5)" in s


# ---------------------------------------------------------------- 10 红块
def test_settings_has_single_danger_button_style():
    """重复的 .btn.danger 把「白字 + 红底」盖成「红字 + 红底」= 空红块。"""
    s = _read("views/Settings.vue")
    assert s.count(".btn.danger {") == 1
    seg = s.split(".btn.danger {", 1)[1].split("}", 1)[0]
    assert "#fff" in seg and "background" in seg


# ---------------------------------------------------------------- 11 旧版下线
def test_legacy_web_entry_is_gone():
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    # /classic 只做 302，不再送旧界面
    seg = src.split('if path in ("/classic", "/classic/"):', 1)[1].split("if path in", 1)[0]
    assert "302" in seg and "Location" in seg
    # / 恒定新版
    assert 'self._serve_asset("index.v2.html")' in src
    assert 'self._serve_asset("index.html")' not in src
    # 那个开关作废
    assert '"web_ui_v2",' not in src
    a = _read("App.vue")
    assert "切回旧版界面" not in a and 'href="/classic"' not in a
    s = _read("views/Settings.vue")
    assert "web_ui_v2" not in s
    # 旧 service worker 要能自杀（否则用户永远看到旧页面）
    sw = (ROOT / "media_resource_manager/web/sw.js").read_text(encoding="utf-8")
    assert "unregister" in sw and "caches.delete" in sw


# ---------------------------------------------------------------- 12 底栏
def test_statusbar_layout_ip_left_progress_right():
    a = _read("App.vue")
    left = a.split('class="sb-left"', 1)[1].split('<div class="sb-right"', 1)[0]
    right = a.split('<div class="sb-right"', 1)[1].split("</footer>", 1)[0]
    assert "sb-host" in left, "访问地址应在左边"
    assert "个任务" in right and "张图" in right and "待审" in right and "隔离" in right
    # V0.0.45：右下角那颗「退出软件」换成了常显文字的红色胶囊 quit-btn
    assert "quit-btn" in right


# ---------------------------------------------------------------- 13/14/15 顶栏与浮层
def test_brand_logo_only_and_sidebar_toggle():
    a = _read("App.vue")
    brand = a.split('<div class="brand">', 1)[1].split("</div>", 1)[0]
    assert "<span>Sweet</span>" not in brand
    assert "side-btn" in brand and "panel-left" in brand
    # V0.0.29 改判：用户定稿——软件内 logo 一律用原来的（裁边发光字标 92px），
    # 渐变 sweet 母版只作为素材文件交付（D:/Sweet素材/），不入软件。
    assert ".brand img { width: 92px" in a
    assert "/brand/wordmark.png" in a


def test_back_to_top_button():
    a = _read("App.vue")
    assert 'class="totop"' in a
    assert "backToTop" in a and "mainEl" in a
    # 只有滚过一屏才出现，且滚的是主区容器（不是 window）
    assert "scrollTop" in a and "> 240" in a
    icons = _read("icons.js")
    assert '"arrow-up"' in icons


# ---------------------------------------------------------------- 16 闪屏
def test_exe_splash_uses_light_palette():
    src = (ROOT / "media_resource_manager/gui/main_window.py").read_text(encoding="utf-8")
    # 2026-10-29（r257）：条件里加了「进托盘时不画闪屏」，切片关键词随源码同步
    seg = src.split('if (bool(getattr(_cfg, "splash_enabled", True))', 1)[1]
    seg = seg.split("win = MainWindow()", 1)[0]
    # ⚠ 原来是 c(True, …) = 深色调色板 ⇒ 不管什么皮肤都是黑底
    assert "_th.c(False" in seg, "闪屏底色必须取浅色调色板"
    assert "_th.c(True" not in seg
    assert "正在启动…" in seg
    # 浅色 logo 直接放浅底上看不见 → 必须有品牌色垫块
    assert "drawRoundedRect" in seg and 'c(False, "accent")' in seg
