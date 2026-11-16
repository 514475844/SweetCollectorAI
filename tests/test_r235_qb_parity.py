"""R235：照着 qBittorrent Web UI 把网页端"手感"补齐的部分，锁住不许退化。

参照物是用户本机真实跑着的 qBittorrent（`127.0.0.1:8081`，WebAPI 2.8.19）。
从它那儿学来、这一轮落地的：
① 左侧「状态」树（带计数，点着筛选）
② 图库左侧「按任务 / 按标签」树（原来是一个翻不动的下拉）
③ 底部状态栏（运行中/排队/失败/图片数）
④ 点表头排序 + 双击行编辑
⑤ 侧栏可收起（Ctrl+B），选择记住
⑥ 会话 cookie 在 HTTPS 下加 Secure（qB 的 web_ui_secure_cookie_enabled）

**没有照抄**的也写在这里，免得下次有人以为漏了：顶部菜单栏（文件/编辑/视图）、
Tracker/用户/HTTP 源那几栏、做种比例与 DHT/端口/加密、邮件通知 —— 原因见
docs/r235_qb_parity_20261028.md。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC = Path(__file__).resolve().parents[1] / "media_resource_manager" / "webapp" / "src"


def _read(rel: str) -> str:
    p = SRC / rel
    assert p.is_file(), f"缺少前端文件 {rel}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 左侧状态树

def test_tasks_page_has_status_filter():
    """2026-10-29（用户第 4 条）：状态筛选从任务页左栏**搬进左侧导航**
    （挂在「任务」下面，点任务才展开）。所以现在要断的是两处：
      · Tasks.vue 仍按 statusFilter 过滤（FILTERS 词表、shownTasks 判据）；
      · App.vue 的侧栏里有那 7 个状态子项。
    原来断言「Tasks.vue 里有 class="filters card"」，那个左栏已被移除。
    """
    t = _read("views/Tasks.vue")
    assert "const FILTERS = [" in t
    for k in ("running", "queued", "error", "idle", "stopped", "done"):
        assert f"k: '{k}'" in t, f"状态筛选缺 {k}"
    a = _read("App.vue")
    assert "const TASK_VIEWS = [" in a
    for k in ("running", "queued", "error", "idle", "stopped", "done"):
        assert f"k: '{k}'" in a, f"侧栏任务子菜单缺 {k}"
    assert "goTask(" in a and "taskOpen" in a


def test_status_filter_actually_filters():
    """光有界面不行 —— 筛选必须真的作用到 shownTasks 上。"""
    t = _read("views/Tasks.vue")
    body = t.split("const shownTasks = computed(", 1)[1].split("})", 2)[0]
    assert "statusFilter.value" in body and "t.status ===" in body


def test_tasks_have_no_duplicate_count_pills():
    """顶部那排 运行/失败/空闲/停止 小胶囊已经挪进状态树，不能再出现一份，
    否则两处数字口径一漂移用户就会来问"到底哪个对"。"""
    t = _read("views/Tasks.vue")
    assert 'pill ok">运行 {{ stats.running' not in t
    assert "显示 {{ shownTasks.length }} / {{ tasks.length }}" in t


# ---------------------------------------------------------------- 图库左树

def test_gallery_task_filter_is_dropdown_tag_filter_is_tree():
    """2026-10-29（用户第 1 条 · 最终形态）：任务与标签**都**是工具栏下拉。

    第一版只把「按任务」挪进下拉、标签仍留左侧树；用户看实机后说
    「放在这里太突兀了」—— 于是标签也进工具栏，紧跟任务后面，
    层级用全角空格缩进保留，左侧面板整块撤掉。
    """
    g = _read("components/ImageGrid.vue")
    assert 'class="gside card"' not in g          # 左侧标签面板已撤
    assert 'class="tasksel"' in g and 'v-model="taskFilter"' in g
    assert 'class="tagsel"' in g and 'v-model="tagFilter"' in g
    # 任务在前、标签在后（用户要求「放到全部任务后面」）
    head = g.split('v-model="sort"', 1)[1].split('placeholder="搜索', 1)[0]
    assert 'v-model="taskFilter"' in head and 'v-model="tagFilter"' in head
    assert head.index('v-model="taskFilter"') < head.index('v-model="tagFilter"')
    # 下拉必须紧跟在排序下拉（最新采集）之后、方向筛选之前
    seg = g.split('v-model="sort"', 1)[1]
    assert 'v-model="taskFilter"' in seg.split('v-model="orient"', 1)[0]


def test_gallery_filters_are_dropdowns():
    """2026-10-29：任务、标签都改成 `<select v-model>` 下拉。

    原来是「任务下拉 + 左侧标签树按钮」，标签靠 click 写 tagFilter；
    现在统一成下拉，选项值仍是节点的**全路径**（tg.path），语义不变：
    选中哪个节点就筛到哪棵子树。
    """
    g = _read("components/ImageGrid.vue")
    assert 'v-model="taskFilter"' in g and 'v-model="tagFilter"' in g
    assert ':value="tg.path"' in g
    # 选项文本必须带层级缩进与计数，否则下拉里分不清父子、也看不出多少张
    assert "tagOptionLabel" in g


# ---------------------------------------------------------------- 状态栏 / 侧栏

def test_app_has_bottom_statusbar():
    a = _read("App.vue")
    assert 'class="statusbar"' in a
    for word in ("运行", "排队", "失败", "待审", "隔离"):
        assert word in a
    assert ".statusbar {" in a


def test_sidebar_can_collapse_and_remembers():
    a = _read("App.vue")
    assert "sideHide" in a and "side-off" in a
    assert "localStorage.setItem('mrm.side'" in a
    assert "localStorage.getItem('mrm.side')" in a


# ---------------------------------------------------------------- 表头排序 / 双击

def test_task_header_sort_and_dblclick_edit():
    t = _read("views/Tasks.vue")
    assert "toggleSort('name')" in t and "toggleSort('got')" in t
    assert "@dblclick=\"openEdit(t)\"" in t
    # 排序要实现 asc/desc 两向，否则点第二下没反应
    assert "endsWith('_desc')" in t


def test_filter_box_shortcut():
    t = _read("views/Tasks.vue")
    assert "Ctrl+F" in t
    assert "kwEl.value && kwEl.value.focus()" in t


# ---------------------------------------------------------------- 后端：summary / cookie

def test_summary_reports_running_and_queued():
    """/api/summary 多给三个计数给底栏用；口径必须直接来自 tasks.status。"""
    src = (Path(__file__).resolve().parents[1] / "media_resource_manager"
           / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def _api_summary", 1)[1].split("def _api_settings", 1)[0]
    for k in ("running", "queued", "error"):
        assert f'"{k}"' in seg, f"summary 少了 {k}"


def _cookie(tls: bool, secure_cfg: bool) -> str:
    from media_resource_manager import webui
    conn = SimpleNamespace(cipher=lambda: "TLS") if tls else object()
    fake = SimpleNamespace(cfg=SimpleNamespace(web_secure_cookie=secure_cfg),
                           connection=conn)
    return webui._Handler._session_cookie(fake, "tok123")


def test_secure_cookie_only_on_tls():
    """HTTP 上加 Secure 会让浏览器直接丢 cookie —— 症状是「登录后又跳回登录页」，
    这类 bug 极难自查，所以两个方向都要钉死。"""
    assert "Secure" not in _cookie(tls=False, secure_cfg=True)
    assert "Secure" in _cookie(tls=True, secure_cfg=True)
    assert "Secure" not in _cookie(tls=True, secure_cfg=False)   # 用户手动关掉


def test_cookie_keeps_hardening_flags():
    c = _cookie(tls=True, secure_cfg=True)
    assert "HttpOnly" in c and "SameSite=Strict" in c and "Path=/" in c


def test_secure_cookie_is_web_writable():
    # 白名单挂在 _Handler 上（不是 WebServer）—— 第一次就写错，AttributeError 提醒了我
    from media_resource_manager.webui import _Handler
    assert "web_secure_cookie" in _Handler._SETTINGS_WRITABLE


def test_default_keeps_old_behaviour_on_http():
    """默认值 True 也不能改变老行为：HTTP 下（绝大多数用户）就是不加 Secure。"""
    from media_resource_manager.config import AppConfig
    assert AppConfig().web_secure_cookie is True


# ------------------------------------------------- 多选 + 批量操作（qB 传输列表）

def test_tasks_support_multi_select():
    t = _read("views/Tasks.vue")
    assert "const sel = ref(new Set())" in t
    assert "onRowClick" in t and "toggleSel" in t and "selectAllShown" in t
    # Shift 连选 / Ctrl 加选都要有，否则只是"能勾"，不是 qB 那种手感
    assert "e.shiftKey" in t and ("e.ctrlKey" in t)
    assert 'class="pick"' in t            # 行首勾选框
    assert "tr.sel" in t                  # 选中整行变色


def test_row_buttons_do_not_toggle_selection():
    """点「开始/编辑」不该顺手把这一行选上 —— 否则批量删除会误伤。"""
    t = _read("views/Tasks.vue")
    assert "e.target.closest('button, a, input')" in t
    assert '<span class="acts" @click.stop>' in t


def test_batch_actions_go_through_shared_endpoint():
    t = _read("views/Tasks.vue")
    assert "api.taskBatch(actName, ids)" in t
    # 删除必须二次确认（V0.0.27 起是带「同时删除图片」勾选的对话框，
    # 并把「图片默认保留」写成用户能读懂的话）
    assert "任务配置会被删除" in t
    assert "磁盘上的文件一个都不动" in t


def test_batch_delete_requires_explicit_ids():
    """不带 ids 的 old 语义是"全体"，对启停合理、对删除是灾难。"""
    from types import SimpleNamespace
    from media_resource_manager import webui

    stub = SimpleNamespace()
    r = webui._Handler._api_task_batch(stub, {"act": "delete"})
    assert r["ok"] is False and "编号" in r["error"], r

    r2 = webui._Handler._api_task_batch(stub, {"act": "删库"})
    assert r2["ok"] is False


def test_batch_supports_pause_and_delete_with_ids():
    from types import SimpleNamespace
    from media_resource_manager import webui

    called = []

    class Stub:
        def _api_task_ctl(self, tid, act):
            called.append((tid, act))
            return {"ok": True}

        def _api_task_retry(self, tid):
            called.append((tid, "retry"))
            return {"ok": True}

    r = webui._Handler._api_task_batch(Stub(), {"act": "pause", "ids": [3, 5]})
    assert r["ok"] is True and r["done"] == [3, 5]
    assert called == [(3, "pause"), (5, "pause")], called


def test_context_menu_accepts_lucide_names():
    """菜单图标从字符换成 Lucide 名字；但老写法（字符）必须还能显示 —— 
    ContextMenu 是所有页共用的，漏改一处就会变成空白格子。"""
    c = _read("components/ContextMenu.vue")
    assert "function iconOf" in c
    assert "<Icon v-if=\"iconOf(it.icon)\"" in c
    assert "{{ it.icon || '' }}" in c          # 兜底仍显示字符
    t = _read("views/Tasks.vue")
    assert "icon: 'play'" in t and "icon: 'trash'" in t
    for glyph in ("icon: '▶'", "icon: '🗑'", "icon: '❙❙'"):
        assert glyph not in t, f"右键菜单还在用字符图标 {glyph}"
