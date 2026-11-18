"""V0.0.54 护栏：适配器市场上线 + AI 生成规则入口移除。

用户定案：
1. GitHub 建适配器市场仓库（订阅源 = index.json + rules/*.yaml）；
2. 适配器页删掉「自动生成规则」「用别的 AI 自己生成」两块；
3. 「导入本地规则文件」旁加「适配器市场」按钮，点开独立页面订阅安装；
4. 安装必须复用手动保存同一道校验链，禁止另写落盘特判。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"


def _read(p):
    return (ROOT / p).read_text(encoding="utf-8")


def test_market_module_exists_and_routes():
    s = _read("media_resource_manager/rule_market.py")
    assert "DEFAULT_SOURCE_URL" in s and "SweetAdapterHub" in s
    ui = _read("media_resource_manager/webui.py")
    for route in ('"/api/market/sources"', '"/api/market/index"',
                  '"/api/market/manage"', '"/api/market/install"'):
        assert route in ui, f"缺路由 {route}"
    # 安装复用 adapter_save，不许另写落盘
    seg = ui.split("def _api_market_install", 1)[1][:1200]
    assert "_api_adapter_save" in seg


def test_market_path_traversal_guard():
    s = _read("media_resource_manager/rule_market.py")
    assert '".." in f.split("/")' in s or '.." in f.split' in s


def test_ai_generation_ui_removed():
    s = (WEB / "views/Adapters.vue").read_text(encoding="utf-8")
    assert "自动生成规则" not in s
    assert "用别的 AI 自己生成" not in s
    assert "copyPrompt" not in s and "fromReply" not in s
    assert "适配器市场" in s and "/market" in s


def test_market_page_registered():
    m = _read("media_resource_manager/webapp/src/main.js")
    assert "RuleMarket" in m
    assert "'/market'" in m
    rm = (WEB / "views/RuleMarket.vue").read_text(encoding="utf-8")
    for kw in ("marketSources", "marketIndex", "marketManage", "marketInstall"):
        assert kw in rm, f"市场页缺 {kw}"


def test_api_js_has_market_calls():
    a = _read("media_resource_manager/webapp/src/api.js")
    for kw in ("marketSources:", "marketIndex:", "marketManage:", "marketInstall:"):
        assert kw in a


def test_sources_persist_in_config():
    c = _read("media_resource_manager/config.py")
    assert "rule_market_sources" in c
    s = _read("media_resource_manager/rule_market.py")
    assert "save_config" in s


def test_version_bumped():
    import re
    v = _read("media_resource_manager/__init__.py")
    m = re.search(r'VERSION = "([\d.]+)"', v)
    parts = tuple(int(x) for x in m.group(1).split("."))
    assert parts >= (0, 0, 54)
