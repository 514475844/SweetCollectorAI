"""R251：站点 Cookie（插件直推 / JSON 导入 / 配对码 / 列表不回值）。

安全侧的用例比功能侧更重要：这条链路写进去的是**登录态**，
一旦 `/api/cookie/ingest` 被随便哪个网页打得通，等于账号被接管。
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把 Cookie 存储指到临时目录（否则会写进真实 data/cookies.json）。"""
    from media_resource_manager import cookies as ck
    monkeypatch.setattr(ck, "STORE_PATH", tmp_path / "cookies.json")
    return ck


# ───────────────────────── 归一化与导入 ─────────────────────────
def test_normalize_domain_strips_scheme_dot_port(store):
    assert store.normalize_domain(".Example.COM") == "example.com"
    assert store.normalize_domain("https://a.b.com:8443/x?y=1") == "a.b.com"
    assert store.normalize_domain("") == ""
    assert store.normalize_domain("  ") == ""


def test_rows_from_cookie_editor_array(store):
    rows = store.rows_from_payload([
        {"name": "a", "value": "1", "domain": ".x.com"},
        {"name": "b", "value": "2", "host": "x.com"},
        {"nope": 1},                      # 无 name → 跳过
        "junk",                            # 非 dict → 跳过
        {"name": "c", "value": "3"},       # 无 domain → 跳过
    ])
    assert rows == [{"domain": "x.com", "name": "a", "value": "1"},
                    {"domain": "x.com", "name": "b", "value": "2"}]


def test_rows_from_wrapped_and_mapping_forms(store):
    wrapped = store.rows_from_payload({"cookies": [
        {"name": "a", "value": "1", "domain": "x.com"}]})
    assert wrapped == [{"domain": "x.com", "name": "a", "value": "1"}]
    mapping = store.rows_from_payload({"y.com": "k=v; k2=v2"})
    assert {r["name"] for r in mapping} == {"k", "k2"}


def test_import_merges_by_name(store):
    store.set_cookie("x.com", "keep=1; cf=old")
    store.import_payload([{"name": "cf", "value": "new", "domain": "x.com"}])
    header = store.get_cookie("x.com")
    assert "keep=1" in header and "cf=new" in header and "cf=old" not in header


def test_import_replace_mode_drops_others(store):
    store.set_cookie("x.com", "keep=1; cf=old")
    store.import_payload([{"name": "cf", "value": "new", "domain": "x.com"}],
                         merge=False)
    header = store.get_cookie("x.com")
    assert header == "cf=new"


def test_summary_never_returns_values(store):
    store.set_cookie("x.com", "cf_clearance=SECRETVALUE; sid=S2")
    s = store.summary()
    assert s[0]["domain"] == "x.com" and s[0]["count"] == 2
    blob = json.dumps(s, ensure_ascii=False)
    assert "SECRETVALUE" not in blob and "S2" not in blob


def test_summary_truncates_long_name_lists(store):
    store.set_cookie("x.com", "; ".join(f"k{i}=v" for i in range(30)))
    s = store.summary()[0]
    assert len(s["names"]) == 12 and s["more"] == 18 and s["count"] == 30


# ───────────────────────── 配对码 ─────────────────────────
def test_pair_code_generated_once_and_stable(tmp_path):
    from media_resource_manager import cookies as ck
    from media_resource_manager.config import AppConfig
    cfg = AppConfig()
    cfg.cookie_pair_code = ""
    saved = {"n": 0}
    code = ck.pair_code(cfg, save=lambda: saved.__setitem__("n", saved["n"] + 1))
    assert len(code) == 6 and code.isdigit()
    assert saved["n"] == 1, "生成后要落盘一次，否则重启就换码"
    assert ck.pair_code(cfg) == code
    assert ck.pair_code(cfg, save=lambda: saved.__setitem__("n", saved["n"] + 1)) == code
    assert saved["n"] == 1, "已有配对码时不该重写配置"


def test_check_pair_code_rejects_blank_config():
    from media_resource_manager import cookies as ck
    from media_resource_manager.config import AppConfig
    cfg = AppConfig()
    cfg.cookie_pair_code = ""
    assert ck.check_pair_code(cfg, "") is False
    assert ck.check_pair_code(cfg, "000000") is False
    cfg.cookie_pair_code = "123456"
    assert ck.check_pair_code(cfg, "123456") is True
    assert ck.check_pair_code(cfg, " 123456 ") is True
    assert ck.check_pair_code(cfg, "123457") is False


# ───────────────────────── 服务端接线 ─────────────────────────
def test_ingest_runs_before_origin_and_auth():
    """插件带 `Origin: chrome-extension://…`，过不了同源校验 ——
    所以 ingest 必须排在它之前，否则插件永远 403。"""
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def do_POST", 1)[1]
    i_host = seg.index("_host_header_ok()")
    i_ingest = seg.index('path == "/api/cookie/ingest"')
    i_origin = seg.index("_origin_ok()")
    i_auth = seg.index("_auth_ok()")
    assert i_host < i_ingest < i_origin < i_auth


def test_ingest_writes_response():
    """返回字典而不写响应 = 客户端挂到超时（探针实测 20 秒，日志无错）。"""
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def do_POST", 1)[1]
    line = [ln for ln in seg.splitlines() if '"/api/cookie/ingest"' in ln][0]
    nxt = seg.split(line, 1)[1].splitlines()
    body = "\n".join(nxt[:4])
    assert "_json(" in body, "ingest 分支没有调用 _json 写响应"


def test_options_only_for_ingest():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def do_OPTIONS", 1)[1].split("def do_POST", 1)[0]
    assert '"/api/cookie/ingest"' in seg
    assert "chrome-extension://" in seg
    assert "204" in seg


def test_cors_headers_not_wildcard():
    """写 Cookie 的接口不该让任意网页读到响应。"""
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def _cors_extension", 1)[1].split("def _ingest_throttle_ok", 1)[0]
    assert 'Allow-Origin", "*"' not in seg
    assert "chrome-extension://" in seg


def test_ingest_never_logs_cookie_values():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def _api_cookie_ingest", 1)[1].split("def _cors_extension", 1)[0]
    # 审计只记域名 + 条数
    assert 'self.db.audit("cookie", "push", d' in seg
    assert "LOG." not in seg, "推送里不该打日志（可能带出 Cookie 内容）"


def test_cookie_endpoints_registered():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    for k in ('"/api/cookies"', '"/api/cookie/ingest"'):
        assert k in src


# ───────────────────────── 插件与前端 ─────────────────────────
def test_extension_has_push_button_and_reads_config():
    d = ROOT / "extension" / "Sweet采集助手"
    html = (d / "popup.html").read_text(encoding="utf-8")
    js = (d / "popup.js").read_text(encoding="utf-8")
    assert "b_push" in html and "配对码" in html
    assert "/api/cookie/ingest" in js
    assert "chrome.storage.local" in js
    assert "navigator.userAgent" in js, "UA 必须一起送（cf_clearance 与 UA 绑定）"
    mf = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    # 要能访问本地软件（<all_urls> 已覆盖；改成精确域名时要记得带上两个回环）
    hosts = " ".join(mf.get("host_permissions") or [])
    assert "<all_urls>" in hosts or "127.0.0.1" in hosts


def test_extension_js_parses():
    """插件语法错在浏览器里只是「点了没反应」，必须静态兜住。"""
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("没有 node，跳过语法检查")
    f = ROOT / "extension" / "Sweet采集助手" / "popup.js"
    r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:400]


def test_frontend_has_cookie_panel():
    st = (PKG / "webapp/src/views/Settings.vue").read_text(encoding="utf-8")
    assert "<CookieMgr" in st
    ck = (PKG / "webapp/src/components/CookieMgr.vue").read_text(encoding="utf-8")
    for k in ("配对码", "粘贴 Cookie", "同名合并", "值不会显示"):
        assert k in ck, f"Cookie 面板缺「{k}」"
    api = (PKG / "webapp/src/api.js").read_text(encoding="utf-8")
    for k in ("cookiesList:", "cookiesPost:"):
        assert k in api


def test_icons_used_by_cookie_panel_exist():
    s = (PKG / "webapp/src/components/CookieMgr.vue").read_text(encoding="utf-8")
    icons = (PKG / "webapp/src/icons.js").read_text(encoding="utf-8")
    names = set(re.findall(r'<Icon\s+name="([a-z0-9-]+)"', s))
    missing = [n for n in names if f'"{n}":' not in icons]
    assert not missing, f"icons.js 里没有：{missing}"


def test_built_bundle_contains_cookie_panel():
    d = PKG / "web/static/app/assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    assert "配对码" in js and "/api/cookies" in js, "前端没重新构建"
