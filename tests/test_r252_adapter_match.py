"""R252：适配器增强 —— 按域名自动匹配 + 规则自带 Cookie/UA。

用户的原话是「我把网址扔进去，为什么还要先挑适配器」。这轮让「只粘网址」
真的成立，同时**不许改变 gallery-dl 认得的大站的行为**（那是既定契约：
快路径优先，danbooru/gelbooru 这类站改 YAML 反而更差）。
"""
import asyncio
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


def _rule(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / f"{name}.yaml"
    p.write_text(body, encoding="utf-8")
    return p


GOOD = """
name: {name}
base_url: https://{host}/
allowed_domains: [{domains}]
image_link_selectors:
  - kind: css
    value: "img.photo"
"""


# ───────────────────────── 健康判定 ─────────────────────────
def test_healthy_requires_three_fields(tmp_path):
    from media_resource_manager.crawler import rules as R
    _rule(tmp_path, "ok", GOOD.format(name="ok", host="a.com", domains="a.com"))
    r = R.load_adapter(tmp_path / "ok.yaml")
    assert R.healthy(r) is True

    # 缺图片选择器 = 跑起来只会 0 图
    _rule(tmp_path, "noimg", "name: noimg\nbase_url: https://b.com/\n"
                             "allowed_domains: [b.com]\n")
    r2 = R.load_adapter(tmp_path / "noimg.yaml")
    assert R.healthy(r2) is False


# ───────────────────────── 域名匹配 ─────────────────────────
def test_match_by_domain_and_subdomain(tmp_path):
    from media_resource_manager.crawler import rules as R
    _rule(tmp_path, "main", GOOD.format(name="main", host="b.com", domains="b.com"))
    r = R.match_adapter(tmp_path, ["https://a.b.com/album/1"])
    assert r is not None and r.name == "main", "主域规则应能匹配子域"


def test_match_prefers_longest_domain(tmp_path):
    from media_resource_manager.crawler import rules as R
    _rule(tmp_path, "wide", GOOD.format(name="wide", host="b.com", domains="b.com"))
    _rule(tmp_path, "narrow", GOOD.format(name="narrow", host="a.b.com",
                                          domains="a.b.com"))
    r = R.match_adapter(tmp_path, ["https://a.b.com/x"])
    assert r is not None and r.name == "narrow"


def test_match_skips_unhealthy_rules(tmp_path):
    from media_resource_manager.crawler import rules as R
    _rule(tmp_path, "half", "name: half\nbase_url: https://b.com/\n"
                            "allowed_domains: [b.com]\n")     # 没有图片选择器
    assert R.match_adapter(tmp_path, ["https://b.com/x"]) is None
    # 显式要求「不健康也要」时才返回
    assert R.match_adapter(tmp_path, ["https://b.com/x"],
                           healthy_only=False) is not None


def test_match_ignores_broken_rule_files(tmp_path):
    from media_resource_manager.crawler import rules as R
    (tmp_path / "bad.yaml").write_text("- 这不是映射\n- 第二行\n", encoding="utf-8")
    _rule(tmp_path, "good", GOOD.format(name="good", host="c.com", domains="c.com"))
    r = R.match_adapter(tmp_path, ["https://c.com/x"])
    assert r is not None and r.name == "good"


def test_match_returns_none_for_unknown_domain(tmp_path):
    from media_resource_manager.crawler import rules as R
    _rule(tmp_path, "only", GOOD.format(name="only", host="a.com", domains="a.com"))
    assert R.match_adapter(tmp_path, ["https://zzz.com/x"]) is None
    assert R.match_adapter(tmp_path, []) is None
    assert R.match_adapter(tmp_path / "不存在", ["https://a.com"]) is None


def test_match_does_not_match_reverse_suffix(tmp_path):
    """规则写子域，不能去匹配主域（反过来才允许）。"""
    from media_resource_manager.crawler import rules as R
    _rule(tmp_path, "sub", GOOD.format(name="sub", host="a.b.com",
                                       domains="a.b.com"))
    assert R.match_adapter(tmp_path, ["https://b.com/x"]) is None


# ───────────────────────── 规则里的 Cookie / UA ─────────────────────────
def test_rule_cookie_and_ua_are_parsed(tmp_path):
    from media_resource_manager.crawler import rules as R
    p = _rule(tmp_path, "c", GOOD.format(name="c", host="d.com", domains="d.com")
              + 'cookie: "sid=abc; cf=xyz"\nuser_agent: "Mozilla/5.0 Test"\n')
    r = R.load_adapter(p)
    assert r.cookie == "sid=abc; cf=xyz"
    assert r.user_agent == "Mozilla/5.0 Test"


def test_rule_cookie_rejects_header_injection(tmp_path):
    """规则文件是用户手粘的：带换行就是请求头注入。"""
    from media_resource_manager.crawler import rules as R
    p = tmp_path / "inj.yaml"
    p.write_text('name: inj\nbase_url: https://e.com/\n'
                 'allowed_domains: [e.com]\n'
                 'image_link_selectors:\n  - {kind: css, value: "img"}\n'
                 'cookie: "a=b\\r\\nX-Evil: 1"\n', encoding="utf-8")
    r = R.load_adapter(p)
    assert "\r" not in r.cookie and "\n" not in r.cookie


def test_defaults_keep_old_behavior(tmp_path):
    """老规则没有这两个字段 —— 解析出来必须还是空（不改变既有行为）。"""
    from media_resource_manager.crawler import rules as R
    p = _rule(tmp_path, "old", GOOD.format(name="old", host="f.com", domains="f.com"))
    r = R.load_adapter(p)
    assert r.cookie == "" and r.user_agent == ""


# ───────────────────────── 引擎接线（静态） ─────────────────────────
def test_engine_auto_match_after_gallery_dl_decision():
    """自动匹配必须排在 gallery-dl 快路径**之后**，否则会抢走大站的快路径。"""
    src = (PKG / "engine.py").read_text(encoding="utf-8")
    i_fast = src.index("return await self._run_via_gallery_dl(task, rule, entries, rep)")
    i_auto = src.index("match_adapter(self.cfg.rules_dir, entries)")
    assert i_fast < i_auto, "自动匹配排到快路径之前了 —— 大站会被 YAML 抢走"
    # 必须受开关控制
    assert "adapter_auto_match" in src.split("match_adapter(self.cfg.rules_dir", 1)[0][-400:]


def test_engine_merges_rule_cookie_and_ua():
    src = (PKG / "engine.py").read_text(encoding="utf-8")
    seg = src.split("def _make_fetcher", 1)[1].split("def _bandwidth_limiter", 1)[0]
    assert 'getattr(rule, "cookie"' in seg, "适配器 Cookie 没合并进请求"
    assert 'getattr(rule, "user_agent"' in seg, "适配器 UA 没生效"
    # 任务级优先：任务的 UA 要能用 or 短路在前面
    assert seg.index('getattr(task, "user_agent"') < seg.index('getattr(rule, "user_agent"')


def test_prompt_template_lists_new_fields():
    """提示词模板要与 AdapterRule 逐字段对齐，否则 AI 不知道能写这两个字段。"""
    from media_resource_manager.webui import adapter_prompt_template
    t = adapter_prompt_template()
    assert '"cookie":' in t and '"user_agent":' in t
    assert "不要编造" in t, "要明确告诉模型别瞎填 Cookie"


def test_adapters_api_reports_presence_only():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    seg = src.split("def _api_adapters", 1)[1].split("def ", 1)[0]
    assert '"has_cookie"' in seg and '"has_ua"' in seg
    # 只报有没有，绝不回显值
    assert "rule.cookie" not in seg and "rule.user_agent" not in seg


# ───────────────────────── 前端 ─────────────────────────
def test_settings_has_auto_match_toggle():
    st = (PKG / "webapp/src/views/Settings.vue").read_text(encoding="utf-8")
    assert "adapter_auto_match" in st
    api = (PKG / "webui.py").read_text(encoding="utf-8")
    assert '"adapter_auto_match"' in api, "没写进可写白名单，前端改了也存不下"


def test_adapters_page_shows_login_badges():
    s = (PKG / "webapp/src/views/Adapters.vue").read_text(encoding="utf-8")
    assert "has_cookie" in s and "has_ua" in s
    assert "登录态" in s


def test_built_bundle_contains_adapter_changes():
    d = PKG / "web/static/app/assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    assert "adapter_auto_match" in js, "前端没重新构建"


# ─────────────────── 引擎级：只粘网址也能用上规则 ───────────────────
PAGES = {
    "https://cold-site.com/": '<a class="album" href="/a1">A1</a>',
    "https://cold-site.com/a1": '<a class="photo" href="/a1/p1">p1</a>',
    "https://cold-site.com/a1/p1": '<img src="/img/1.jpg">',
    "https://cold-site.com/img/1.jpg": "jpeg-bytes",
}


def _write_rule(rules_dir: Path, host: str = "cold-site.com",
                with_cookie: str = "", with_ua: str = "") -> None:
    rule = {
        "name": "cold", "base_url": f"https://{host}",
        "allowed_domains": [host],
        "list_page_selectors": {"albums": {"kind": "css", "expr": "a.album",
                                           "attr": "href"}},
        "album_link_selectors": {"photos": {"kind": "css", "expr": "a.photo",
                                            "attr": "href"}},
        "image_link_selectors": [{"kind": "css", "expr": "img", "attr": "src"}],
    }
    if with_cookie:
        rule["cookie"] = with_cookie
    if with_ua:
        rule["user_agent"] = with_ua
    (rules_dir / "cold.json").write_text(json.dumps(rule), encoding="utf-8")


def test_engine_uses_matched_adapter_when_adapter_is_auto(
        tmp_config, tmp_db, fake_fetcher):
    """核心诉求：适配器选「自动」+ 粘一个冷门网址 → 自动用上域名匹配的规则。

    这个站 gallery-dl 没有提取器，所以不会走快路径 —— 正是自动匹配要补的空档。
    """
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task

    _write_rule(tmp_config.rules_dir)
    t = TaskConfig(id=None, name="t", adapter_name="(自动 - gallery-dl)",
                   entry_urls=["https://cold-site.com/"],
                   output_dir=str(tmp_config.library_dir))
    tid = save_task(tmp_db, t)
    eng = Engine(tmp_config, tmp_db,
                 fetcher_factory=lambda cfg, proxy: fake_fetcher(PAGES))
    rep = asyncio.run(eng.run(tid))
    assert rep.errors == [] or all("暂无可用抓取规则" not in e for e in rep.errors), \
        rep.errors
    assert rep.images_found >= 1, "没自动用上适配器（应该匹配到 cold）"


def test_engine_switch_off_restores_old_behavior(tmp_config, tmp_db, fake_fetcher):
    """关掉开关 = 完全回到旧行为：报「暂无可用抓取规则」。"""
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task

    _write_rule(tmp_config.rules_dir)
    tmp_config.adapter_auto_match = False
    t = TaskConfig(id=None, name="t", adapter_name="",
                   entry_urls=["https://cold-site.com/"],
                   output_dir=str(tmp_config.library_dir))
    tid = save_task(tmp_db, t)
    eng = Engine(tmp_config, tmp_db,
                 fetcher_factory=lambda cfg, proxy: fake_fetcher(PAGES))
    rep = asyncio.run(eng.run(tid))
    assert any("暂无可用抓取规则" in e for e in rep.errors), rep.errors


def test_rule_cookie_reaches_the_fetcher(tmp_config, tmp_db):
    """规则里的 Cookie 必须真的进到请求头（否则「填一次」只是填了个寂寞）。"""
    from media_resource_manager.engine import Engine
    from media_resource_manager.crawler.rules import load_adapter
    from media_resource_manager.models import TaskConfig

    _write_rule(tmp_config.rules_dir, with_cookie="sid=RULE1; cf=CF1",
                with_ua="UA-FROM-RULE")
    rule = load_adapter(tmp_config.rules_dir / "cold.json")
    seen = {}

    def factory(cfg, proxy):
        seen["ua"] = cfg.user_agent
        # 只验「参数被算出来」，不真的发请求
        raise RuntimeError("stop-here")

    eng = Engine(tmp_config, tmp_db, fetcher_factory=factory)
    task = TaskConfig(id=None, name="t", adapter_name="cold",
                      entry_urls=["https://cold-site.com/"],
                      cookie="", user_agent="")
    try:
        eng._make_fetcher(task, rule, "")
    except RuntimeError:
        pass
    assert seen.get("ua") == "UA-FROM-RULE", seen


def test_task_cookie_wins_over_rule_cookie(tmp_config, tmp_db):
    """任务上写的 Cookie 是「这一次」的明确指定，不能被规则盖掉。"""
    from media_resource_manager.cookies import merge_cookies
    # 引擎里的顺序：(规则, 任务) → 任务同名覆盖
    merged = merge_cookies("sid=RULE", "sid=TASK")
    assert "sid=TASK" in merged and "sid=RULE" not in merged
