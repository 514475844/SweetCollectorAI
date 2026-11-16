"""R253：AI 写适配器（先本地验证，再用确认才入库）。

三条红线各有用例守着：
  ① 不打包模型（走用户已配的在线通道）；
  ② **模型编的选择器不许直接入库** —— 必须拿真页面验证匹配到图；
  ③ 生成 ≠ 保存（预览在前，落盘在后）。
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"

HTML = """<html><head><script>var secret=1</script><style>a{color:red}</style></head>
<body><div class="gallery">
<a href="/p/1"><img src="/t/1.jpg"></a>
<a href="/p/2"><img src="/t/2.jpg"></a>
<a href="/p/3"><img src="/t/3.jpg"></a>
</div><a class="next" href="/p?page=2">下一页</a></body></html>"""


@pytest.fixture()
def ai():
    from media_resource_manager import adapter_ai
    return adapter_ai


class Cfg:
    """最小配置：不连网。"""
    ai_api_url = ""
    ai_api_key = ""
    ai_api_model = ""
    user_agent = "UA"
    proxy = ""
    ssl_verify = True


# ───────────────────────── 提示词与解析 ─────────────────────────
def test_excerpt_strips_script_and_style(ai):
    out = ai.page_excerpt(HTML)
    assert "secret" not in out and "color:red" not in out
    assert "gallery" in out          # 结构要留着


def test_extract_json_tolerates_fences_and_chatter(ai):
    assert ai.extract_json('说明\n```json\n{"a":1}\n```\n完') == {"a": 1}
    assert ai.extract_json('{"a":1}') == {"a": 1}
    assert ai.extract_json('前言 {"a": {"b": 2}} 后记') == {"a": {"b": 2}}
    assert ai.extract_json("没有 JSON") is None
    assert ai.extract_json('{"a":') is None
    assert ai.extract_json("[1,2,3]") is None, "顶层数组不是规则对象"


def test_prompt_includes_url_and_structure(ai):
    p = ai.build_prompt("https://x.com/a/1", HTML, template="【输出格式】…")
    assert "https://x.com/a/1" in p and "gallery" in p
    assert "【输出格式】" in p


# ───────────────────────── 校验：编的规则不许过 ─────────────────────────
def _rule(ai, **kw):
    base = {"name": "t", "base_url": "https://t.com",
            "allowed_domains": ["t.com"],
            "image_link_selectors": [{"kind": "css", "expr": "div.gallery img",
                                      "attr": "src"}]}
    base.update(kw)
    return ai.rule_from_dict(base)


def test_validate_ok_counts_only_selector_matches(ai):
    v = ai.validate(_rule(ai), HTML, "https://t.com/a")
    assert v["ok"] is True and v["count"] == 3
    assert v["images"][0].endswith("/t/1.jpg")


def test_validate_rejects_wrong_selector(ai):
    """重点：模型最常见的问题就是写出一个匹配不到东西的选择器。"""
    r = _rule(ai, image_link_selectors=[{"kind": "css", "expr": "img.nope",
                                        "attr": "src"}])
    v = ai.validate(r, HTML, "https://t.com/a")
    assert v["ok"] is False and v["count"] == 0
    assert "0 张图" in v["reason"]


def test_validate_ignores_og_image_false_positive(ai):
    """页面只有 og:image 时不能算「验证通过」—— 那抓不到图。

    ``AdapterRunner.extract_images`` 会把 og:image 也算进来，直接拿它验证
    就会假阳性；所以这里只跑 image_link_selectors。
    """
    html = ('<html><head><meta property="og:image" content="/o.jpg"></head>'
            '<body><p>列表页，没有图</p></body></html>')
    v = ai.validate(_rule(ai), html, "https://t.com/list")
    assert v["ok"] is False, "og:image 让空选择器判过了"


def test_validate_reports_missing_required_fields(ai):
    r = _rule(ai)
    r.image_link_selectors = []
    v = ai.validate(r, HTML, "https://t.com/a")
    assert v["ok"] is False and "必备字段" in v["reason"]


# ───────────────────────── 生成：AI 失败要退回启发式 ─────────────────────────
def test_propose_falls_back_to_heuristic_without_ai(ai):
    r = ai.propose(Cfg(), "https://t.com/a", HTML)
    assert r["source"] == "heuristic"
    assert r["rule_text"].strip(), "启发式也要给出可编辑的 YAML"
    assert r["ok"] is True, r["notes"]


def test_propose_falls_back_when_ai_returns_broken_selector(ai, monkeypatch):
    """AI 给的规则验证不过 → **不能用它**，退回启发式并在说明里写清原因。"""
    def fake_chat(cfg, system, user, timeout=90):
        return json.dumps({"name": "t", "base_url": "https://t.com",
                           "allowed_domains": ["t.com"],
                           "image_link_selectors": [
                               {"kind": "css", "expr": "#nothing", "attr": "src"}]})

    monkeypatch.setattr(ai, "chat", fake_chat)
    cfg = Cfg()
    cfg.ai_api_url = "http://127.0.0.1:9/v1"
    r = ai.propose(cfg, "https://t.com/a", HTML)
    assert r["source"] == "heuristic", "验证不过的 AI 规则被采用了"
    assert any("未通过本地验证" in n for n in r["notes"]), r["notes"]


def test_propose_uses_ai_when_validation_passes(ai, monkeypatch):
    def fake_chat(cfg, system, user, timeout=90):
        return ("好的：\n```json\n" + json.dumps({
            "name": "t", "base_url": "https://t.com",
            "allowed_domains": ["t.com"],
            "image_link_selectors": [{"kind": "css", "expr": "div.gallery img",
                                      "attr": "src"}],
        }) + "\n```")

    monkeypatch.setattr(ai, "chat", fake_chat)
    cfg = Cfg()
    cfg.ai_api_url = "http://127.0.0.1:9/v1"
    r = ai.propose(cfg, "https://t.com/a", HTML)
    assert r["source"] == "ai" and r["ok"] is True and r["count"] == 3


def test_propose_survives_network_error(ai, monkeypatch):
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(ai, "chat", boom)
    cfg = Cfg()
    cfg.ai_api_url = "http://127.0.0.1:9/v1"
    r = ai.propose(cfg, "https://t.com/a", HTML)
    assert r["source"] == "heuristic"
    assert any("失败" in n for n in r["notes"])


def test_chat_requires_api_url(ai):
    with pytest.raises(RuntimeError):
        ai.chat(Cfg(), "s", "u")


def test_chat_reads_message_content(ai, monkeypatch):
    """OpenAI 标准是 message.content；只看 text 的实现在标准接口上永远空。"""
    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {
                "content": "你好"}}]}).encode()

    monkeypatch.setattr(ai.urllib.request, "urlopen", lambda *a, **k: R())
    cfg = Cfg()
    cfg.ai_api_url = "http://x/v1"
    assert ai.chat(cfg, "s", "u") == "你好"


# ───────────────────────── 不打包模型 / 前端接线 ─────────────────────────
def test_ai_adapter_adds_no_model_to_package(ai):
    """体积答案是 +0：走在线通道，绝不在包里塞本地生成模型。"""
    src = (PKG / "adapter_ai.py").read_text(encoding="utf-8")
    assert "onnx" not in src.lower()
    assert "llama" not in src.lower() and "gguf" not in src.lower()


def test_adapter_proposer_importable_without_qt():
    """纯函数要能在无 Qt 环境（服务端/Docker）里用。"""
    import importlib
    m = importlib.import_module("media_resource_manager.crawler.adapter_proposer")
    assert hasattr(m, "propose_adapter") and hasattr(m, "HAS_QT")


def test_backend_routes_exist():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    for k in ('"/api/adapter/ai"', '"/api/adapter/ai/job"'):
        assert k in src
    # 生成不许直接落盘：必须走 save
    seg = src.split("def _api_adapter_ai(", 1)[1].split("def _api_adapter_ai_job", 1)[0]
    assert "rdir.write" not in seg and "dump_rule_file" not in seg


def test_frontend_wires_ai_generation():
    s = (PKG / "webapp/src/views/Adapters.vue").read_text(encoding="utf-8")
    for k in ("自动生成规则", "genAi", "saveAi", "使用在线 AI", "本页匹配到"):
        assert k in s, f"适配器页缺「{k}」"
    assert "onUnmounted(stopAiTimer)" in s, "离开页面要停轮询"
    api = (PKG / "webapp/src/api.js").read_text(encoding="utf-8")
    assert "adapterAi:" in api and "adapterAiJob:" in api


def test_icons_used_by_ai_panel_exist():
    s = (PKG / "webapp/src/views/Adapters.vue").read_text(encoding="utf-8")
    icons = (PKG / "webapp/src/icons.js").read_text(encoding="utf-8")
    names = set(re.findall(r'name="([a-z0-9-]+)"', s)) | set(
        re.findall(r":name=\"[^\"]*'([a-z0-9-]+)'", s))
    missing = [n for n in names if n and f'"{n}":' not in icons]
    assert not missing, f"icons.js 里没有：{missing}"


def test_built_bundle_contains_ai_panel():
    d = PKG / "web/static/app/assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    assert "自动生成规则" in js, "前端没重新构建"
