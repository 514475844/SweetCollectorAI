"""V0.0.19 护栏：网页端界面模式与桌面端共用同一配置口径。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"
WEB = PKG / "webapp" / "src"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_backend_ui_mode_endpoints():
    src = _read(PKG / "webui.py")
    assert 'path == "/api/ui/mode"' in src
    assert "def _api_ui_mode_get(" in src
    assert "def _api_ui_mode_set(" in src
    # GET/POST 两条路由都要接上，只有一条等于半个功能
    post_i = src.index("def do_POST")
    assert '"/api/ui/mode"' in src[:post_i], "缺 GET 路由"
    assert '"/api/ui/mode"' in src[post_i:], "缺 POST 路由"
    assert "self.cfg.ui_mode = mode" in src
    assert "save_config(self.cfg)" in src
    assert '"newbie", "pro"' in src


def test_api_named_methods():
    src = _read(WEB / "api.js")
    assert "uiMode: () => req('/api/ui/mode')" in src
    assert "saveUiMode: (mode) =>" in src


def test_uiprefs_single_source():
    src = _read(WEB / "uiPrefs.js")
    for token in ("export const uiMode", "export function applyUiMode(",
                  "export function setUiMode(", "async function loadUiMode(",
                  "api.saveUiMode(v)", "api.uiMode()"):
        assert token in src, f"uiPrefs 缺 {token}"
    assert _read(PKG / "config.py").count("ui_mode: str") >= 1, \
        "桌面端 config 必须存在同一个 ui_mode 字段"


def test_app_has_mode_switch():
    src = _read(WEB / "App.vue")
    assert "toggleUiMode" in src and "loadUiMode()" in src
    assert "setUiMode(uiMode.value === 'newbie' ? 'pro' : 'newbie')" in src
    assert 'class="mode-btn"' in src
    assert "uiMode, setUiMode, loadUiMode" in src


def test_task_form_tiered_without_dropping_fields():
    src = _read(WEB / "views" / "Tasks.vue")
    assert 'class="modal card task-form"' in src
    assert ":class=\"{ newbie: uiMode === 'newbie', 'adv-open': advOpen }\"" in src
    assert "class=\"adv-toggle\"" in src
    assert ".task-form.newbie:not(.adv-open) .adv" in src
    assert src.count('class="f adv"') + src.count('class="f2 adv"') \
        + src.count('class="f4 adv"') >= 8
    # 基础项必须常驻：入口网址与代理不能被标成高级
    assert '<textarea v-model="form.entry_urls"' in src
    assert '<label>代理</label>' in src
    for line in src.splitlines():
        if "<label>代理</label>" in line or 'v-model="form.entry_urls"' in line:
            assert "adv" not in line, f"基础项被误标为高级：{line.strip()}"


def test_settings_has_mode_radio():
    src = _read(WEB / "views" / "Settings.vue")
    assert "setUiMode('newbie')" in src and "setUiMode('pro')" in src
    assert "<label>界面模式</label>" in src


def test_icon_exists_and_navs_unchanged():
    icons = _read(WEB / "icons.js")
    assert '"panel-left"' in icons
    app = _read(WEB / "App.vue")
    block = app[app.index("const navs = ["): app.index("const navs = [") + 1800]
    assert block.count("path: '/") >= 12, "导航项被删减，功能不能丢"


def test_copy_professional():
    banned = ("点一下", "点开", "搞定", "其实", "反正", "大概", "差不多",
              "折腾", "费劲", "随便", "找不到", "还没填", "还没设置",
              "还没下载", "还没有", "要找", "别找")
    for p in (WEB / "App.vue", WEB / "views/Tasks.vue",
              WEB / "views/Settings.vue", WEB / "uiPrefs.js"):
        raw = _read(p)
        t = re.sub(r"<!--[\s\S]*?-->", "", raw)
        t = re.sub(r"/\*[\s\S]*?\*/", "", t)
        t = "\n".join(x for x in t.splitlines()
                      if not x.lstrip().startswith(("//", "#")))
        for m in re.finditer(r"""["'`]([^'"`\n]{2,300})["'`]""", t):
            s = m.group(1)
            if not re.search(r"[\u4e00-\u9fa5]", s):
                continue
            for w in banned:
                assert w not in s, f"{p.name} 出现「{w}」：{s[:40]}"
