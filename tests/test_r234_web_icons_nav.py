"""R234：新版网页的两处结构调整 + 图标化，锁死别再退回去。

用户 2026-10-28 提的三件事，这里各钉一条：
1. 隔离区从「图库」页签里拆出来，必须是左边独立一项（和 /classic 老界面一致）。
2. 任务表原来「序」和「#」两列并排，用户问“这不是重复的吗”——只显示序。
3. 图标统一走 Lucide（内联 SVG），但**不许引 CDN**：软件要能离线/进 Docker。

这些都是纯前端结构，用源码判据测（和 r152 那批守卫同一套做法）。
"""
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "media_resource_manager" / "webapp" / "src"


def _read(rel: str) -> str:
    p = SRC / rel
    assert p.is_file(), f"缺少前端文件 {rel}"
    return p.read_text(encoding="utf-8")


def test_icons_are_local_not_cdn():
    """图标必须是本地文件 —— 引 CDN 在离线/Docker 里会整片空白。"""
    js = _read("icons.js")
    assert "export const ICONS" in js
    assert "http://" not in js and "https://" not in js
    vue = _read("components/Icon.vue")
    assert "v-html" in vue                      # 内联 SVG 的渲染方式
    assert "cdn" not in vue.lower()


def test_every_icon_name_used_exists():
    """用了不存在的图标名会静默退化成灰点，机器不报错 —— 必须静态查一遍。

    只认 <Icon> 标签上的 name / :name。V0.0.33 加了 Vue 内置
    <Transition name="dlg"> 之后，全局扫 name="..." 会把过渡名误当图标；
    动态绑定 :name="icon" 也不能按字面量查。判据钉的是「图标组件用了什么」，
    不是「文件里出现过哪些 name 属性」。
    """
    import re
    icons = set(re.findall(r'^\s*"([a-z0-9-]+)":', _read("icons.js"), re.M))
    assert len(icons) > 40, "图标集太小，像是没生成"
    bad = {}
    icon_tag = re.compile(r"<Icon\b[^>]*>", re.S)
    static_name = re.compile(r'(?<!:)\bname="([a-z0-9-]+)"')
    quoted_name = re.compile(r":name=\"[^\"]*?'([a-z0-9-]+)'")
    for f in SRC.rglob("*.vue"):
        t = f.read_text(encoding="utf-8")
        for el in icon_tag.findall(t):
            for pat in (static_name, quoted_name):
                for m in pat.finditer(el):
                    if m.group(1) not in icons:
                        bad.setdefault(m.group(1), set()).add(f.name)
    assert not bad, f"这些图标名不存在：{bad}"


def test_quarantine_is_a_top_level_nav_item():
    app = _read("App.vue")
    assert "{ path: '/quarantine', label: '隔离区'" in app
    assert "'/quarantine'" in _read("main.js")
    # 图库里不能再有隔离区页签（否则就是两个入口，计数也对不上）
    gallery = _read("views/Gallery.vue")
    assert "quarantine" not in gallery
    assert 'view="gallery"' in gallery


def test_quarantine_page_restores_instead_of_liking():
    """隔离区那个 like 动作的真实语义是「还原到图库」，按钮上就得这么写。"""
    q = _read("views/Quarantine.vue")
    assert 'view="quarantine"' in q
    grid = _read("components/ImageGrid.vue")
    assert "likeLabel" in grid
    assert "还原" in grid


def test_task_table_has_no_duplicate_id_column():
    """表头和行的格子数必须一致，否则 grid 会整体错位一格 —— 这是纯 CSS 网格，
    多一个 <span> 不会报错，只会默默把状态列显示成进度。"""
    tasks = _read("views/Tasks.vue")
    head = tasks.split('class="th row"', 1)[1].split("</div>", 1)[0]
    row = tasks.split('class="row tr"', 1)[1].split("</div>", 1)[0]
    # 表头里「任务/状态/进度」已经改成可点的排序按钮（qBittorrent 手感），
    # 所以列数 = span + button，不能再只数 span。
    n_head = head.count("<span") + head.count("<button")
    n_row = row.count("<span class") + row.count("<span>")
    assert n_head == 6, f"表头应有 6 列（序/任务/状态/进度/入口/操作），实际 {n_head}"
    assert n_row == 6, f"行内应有 6 个格子，实际 {n_row}"
    assert "<span>#</span>" not in tasks
    # 任务编号还得留着（只是不单独占一列）：搜索和标题都要能用
    assert "#编号" in tasks and "任务编号 #" in tasks


def test_nav_icons_are_lucide_not_glyphs():
    """侧栏原来用 ▶ ▦ ✓ 这类字符充数，用户要求换真图标。"""
    app = _read("App.vue")
    for glyph in ("'▶'", "'▦'", "'✓'", "'⌘'", "'◈'", "'⟳'", "'☰'", "'◔'", "'⚙'"):
        assert glyph not in app, f"侧栏还在用字符图标 {glyph}"
    assert 'icon: \'list-checks\'' in app


@pytest.mark.parametrize("rel", [
    "App.vue", "views/Tasks.vue", "views/Gallery.vue", "views/Quarantine.vue",
    "views/Subs.vue", "views/Settings.vue", "views/Logs.vue", "views/Report.vue",
    "views/Learning.vue", "views/Urls.vue", "views/Adapters.vue", "views/Models.vue",
    "components/ImageGrid.vue",
])
def test_icon_component_is_imported_where_used(rel):
    """模板里用了 <Icon 却没 import，Vue 会当成未知自定义元素 —— 页面上什么都不显示，
    控制台只有一条 warning，跑测试也看不出来。"""
    t = _read(rel)
    if "<Icon " not in t and "<Icon\n" not in t:
        pytest.skip("这个文件没用图标")
    # 同目录下的组件用 './Icon.vue'，pages 里用 '../components/Icon.vue'
    ok = ("components/Icon.vue" in t) or ("'./Icon.vue'" in t)
    assert ok, f"{rel} 用了 <Icon> 但没 import"
