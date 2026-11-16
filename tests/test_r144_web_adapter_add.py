"""R144 护栏：网页端「添加适配器 + 复制 Prompt」真能用。

钉的是**行为**（真 handler 替身落盘真文件、再用桌面端同一个 `load_adapter`
读回来）+ 关键源码约束，不钉数值：

  · 保存：合法 JSON/YAML → 真写进 `cfg.rules_dir`，桌面端 `load_adapter`
    读得出（同一目录、同一解析器，这是「网页加了桌面端也能看到」的判据）；
  · **不合法一律不落盘**：空内容 / 缺 base_url+entry_urls / kind 写错 /
    规则名带 `../` → 中文报错，且 `rules_dir` 里一个文件都不多；
  · 删除：只认白名单规则名；
  · Prompt：含网址占位符、含 css/xpath/regex 三种、字段与 `AdapterRule`
    **逐字段对齐**（AdapterRule 以后加字段而话术没跟 ⇒ 这里红）。

反向可变（都能让本文件变红）：
  · 把 `webui.py` 的路由字面量改掉 → 路由测试红；
  · 去掉规则名白名单 → 路径穿越测试红；
  · 把「先校验后落盘」改成「先落盘再校验」→ 不落盘类测试红；
  · 从 Prompt 里删掉一个 AdapterRule 字段 → 字段对齐测试红。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest

from media_resource_manager import webui
from media_resource_manager.crawler.rules import AdapterRule, load_adapter

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"

GOOD_YAML = """
name: probe_site
base_url: https://probe.example.com/
allowed_domains: [probe.example.com]
entry_urls: [https://probe.example.com/latest]
image_link_selectors:
  - kind: css
    value: img.photo
next_page_selectors:
  kind: css
  value: a.next
"""

GOOD_JSON = json.dumps({
    "name": "json_site",
    "base_url": "https://json.example.com/",
    "image_link_selectors": [{"kind": "css", "value": "img.photo"}],
    "next_page_selectors": {"kind": "xpath", "value": "//a[@rel='next']"},
}, ensure_ascii=False)


# ------------------------------------------------------------ handler 替身


class _H(webui._Handler):
    """与 R141 同思路：只跑适配器两个接口，不走真 socket。

    ⚠ `cfg` 在基类里是 property，子类不能直接赋值 ⇒ 走 `_cfg` 私有槽。
    """

    _db = None
    _headers = None
    _path = "/api/adapter/save"

    def __init__(self, rules_dir, body=None):
        # ⚠ web_password 等必须给全：`do_POST` 的 `_auth_ok` 第一步就要读，
        # 少了它 ⇒ 路由分发测试直接 AttributeError，看着像接口没接上。
        self._cfg = type("C", (), {
            "rules_dir": rules_dir,
            "web_password": "",
            "web_lan_open": True,
            "web_ip_whitelist": [],
        })()
        self._db = None
        self._headers = {}
        self._path = "/api/adapter/save"
        self._body_obj = body or {}
        self._sent = None
        self.client_address = ("127.0.0.1", 0)

    def _send(self, code, body, ctype, extra=None):
        self._sent = (code, body, ctype, extra or {})
        return self._sent

    def _json(self, obj, code=200, extra=None):
        # ⚠ 基类的 `_json` **不 return**（只负责发出去）。不覆写的话
        # `do_POST()` 拿到的是 None，路由分发测试会假红。
        return self._send(code, json.dumps(obj, ensure_ascii=False)
                          .encode("utf-8"), "application/json; charset=utf-8",
                          extra)

    def _body(self):
        return dict(self._body_obj)

    @property
    def cfg(self):
        return self._cfg

    @property
    def headers(self):
        return self._headers

    @property
    def path(self):
        return self._path


@pytest.fixture()
def rdir(tmp_path):
    d = tmp_path / "rules"
    d.mkdir()
    return d


def _src() -> str:
    return WEBUI.read_text(encoding="utf-8")


def _js() -> str:
    return APPJS.read_text(encoding="utf-8")


def _rule_files(rdir):
    return sorted(p.name for p in rdir.iterdir())


# ------------------------------------------------------------ 路由


def test_both_routes_are_wired():
    """两个新接口必须真的挂在 do_POST 上（字面量逐个列出，别改成循环）。"""
    s = _src()
    assert '"/api/adapter/save"' in s
    assert '"/api/adapter/delete"' in s
    for name in ("_api_adapter_save", "_api_adapter_delete"):
        assert f"self._{name[1:]}" in s or f"self.{name}" in s


def test_save_route_dispatches(rdir):
    """真走一次 do_POST 的分发，证明「路由接上了」不是只在源码里写字面量。"""
    h = _H(rdir, {"name": "routed", "rule": GOOD_YAML})
    h._path = "/api/adapter/save"
    code, raw, _ct, _extra = h.do_POST()
    assert code == 200, raw
    assert json.loads(raw.decode("utf-8"))["ok"] is True


# ------------------------------------------------------------ Prompt 模板


def test_prompt_has_url_placeholder():
    p = webui.adapter_prompt_template()
    assert "【在这里粘贴你要采集的网站网址】" in p


def test_prompt_declares_three_selector_kinds_only():
    p = webui.adapter_prompt_template()
    for kind in ("css", "xpath", "regex"):
        assert kind in p, f"Prompt 没说明选择器类型 {kind}"


def _prompt_json_block() -> str:
    """取出 Prompt 里那段**字段示例**（「输出格式」～「选择器规范」之间）。

    ⚠ 为什么只查这一段而不是全文：全文里字段说明（「- image_url_replace：…」）
    也会提到字段名，只查全文的话「示例里删了字段、说明里还留着」照样全绿
    ⇒ AI 生成时压根不知道还有这个字段，护栏却是绿的。实测踩到过一次。
    """
    p = webui.adapter_prompt_template()
    i = p.index("【输出格式】")
    j = p.index("【选择器规范】")
    return p[i:j]


def test_prompt_covers_every_adapter_rule_field():
    """★ 最有价值的一条：话术的**字段示例**必须与 AdapterRule 逐字段对齐。

    少写一个字段 ⇒ AI 不知道还能给；多写一个 ⇒ load_adapter 静默丢弃，
    用户以为存进去了其实没有。以后 AdapterRule 加字段，这条会红，逼着
    同步模板 —— 这正是 R101/R102 那类「两份配方各写一份」的老坑。
    """
    seg = _prompt_json_block()
    missing = [f.name for f in dataclasses.fields(AdapterRule)
               if f.name not in seg]
    assert not missing, f"Prompt 的字段示例漏了 AdapterRule 字段：{missing}"


def test_prompt_example_is_parseable_json():
    """示例本身必须是**能解析**的 JSON —— 给 AI 看的模板自己都解不开，
    那就是让用户去 debug 一份话术。"""
    seg = _prompt_json_block()
    body = seg[seg.index("{"):seg.rindex("}") + 1]
    obj = json.loads(body)
    assert isinstance(obj, dict)
    assert obj["image_link_selectors"], "示例必须给出图片选择器"


def test_prompt_states_required_fields():
    p = webui.adapter_prompt_template()
    for key in ("base_url", "entry_urls", "image_link_selectors",
                "next_page_selectors"):
        assert key in p, f"Prompt 没点名必备字段 {key}"


def test_prompt_is_served_by_adapters_api(rdir):
    """模板由后端下发：前端不自带一份，才不会和桌面端漂移。"""
    h = _H(rdir)
    data = h._api_adapters()
    assert data["prompt"] == webui.adapter_prompt_template()


# ------------------------------------------------------------ 保存：合法


def test_save_yaml_lands_in_rules_dir_and_desktop_can_read(rdir):
    h = _H(rdir)
    r = h._api_adapter_save({"name": "probe_site", "rule": GOOD_YAML})
    assert r["ok"] is True, r
    saved = rdir / "probe_site.yaml"
    assert saved.exists(), f"没落盘：{_rule_files(rdir)}"
    # 桌面端同一个解析器能读回来 = 网页加的规则桌面端也能看到
    rule = load_adapter(saved)
    assert rule.base_url == "https://probe.example.com/"
    assert rule.name == "probe_site"


def test_saved_rule_shows_up_in_adapters_list(rdir):
    h = _H(rdir)
    assert h._api_adapter_save({"name": "listed", "rule": GOOD_YAML})["ok"]
    names = [i["name"] for i in h._api_adapters()["items"]]
    assert "listed" in names


def test_json_pasted_rule_is_normalized_to_yaml(rdir):
    """粘 JSON 也行，但**统一按 .yaml 落盘** —— 否则两边都按 *.yaml 列举，
    存成 .json 会直接在列表里消失（用户以为没保存上）。"""
    h = _H(rdir)
    r = h._api_adapter_save({"name": "json_site", "rule": GOOD_JSON})
    assert r["ok"] is True, r
    assert r["file"].endswith(".yaml")
    assert load_adapter(rdir / "json_site.yaml").base_url == \
        "https://json.example.com/"


# ------------------------------------------------------------ 保存：不合法一律不落盘


@pytest.mark.parametrize("name,rule,why", [
    ("empty", "", "空内容"),
    ("nobase", "name: nobase\nimage_link_selectors:\n  - kind: css\n    value: img\n",
     "既无 base_url 也无 entry_urls"),
    ("badkind", "name: badkind\nbase_url: https://x.example.com/\n"
                "image_link_selectors:\n  - kind: jq\n    value: img\n",
     "选择器 kind 写错"),
    ("notmap", "- a\n- b\n", "不是对象（列表）"),
])
def test_invalid_rules_are_rejected_and_never_written(rdir, name, rule, why):
    h = _H(rdir)
    r = h._api_adapter_save({"name": name, "rule": rule})
    assert r["ok"] is False, f"{why} 竟然通过了：{r}"
    assert r.get("error"), "失败必须带中文原因"
    assert _rule_files(rdir) == [], f"{why} 不该落盘，却多了：{_rule_files(rdir)}"


@pytest.mark.parametrize("bad_name", [
    "../evil", "..\\evil", "a/b", "sub/name", "有中文", "a b", "x.py",
])
def test_path_traversal_and_odd_names_rejected(rdir, bad_name):
    """规则名直接拼进文件路径 ⇒ 不做白名单就能写到 rules_dir 外面去。"""
    h = _H(rdir)
    r = h._api_adapter_save({"name": bad_name, "rule": GOOD_YAML})
    assert r["ok"] is False, f"危险规则名 {bad_name!r} 被放过了"
    assert _rule_files(rdir) == []


def test_blank_takes_the_name_inside_the_rule(rdir):
    """网页上不填名是允许的：用规则里的 name（省一步手输）。

    ⚠ 但**取出来之后照样要过白名单** —— 规则是外部 AI 生成的，
    里面的 name 同样不可信。
    """
    h = _H(rdir)
    r = h._api_adapter_save({"name": "", "rule": GOOD_YAML})
    assert r["ok"] is True and r["name"] == "probe_site"
    assert (rdir / "probe_site.yaml").exists()


def test_name_inside_the_rule_is_also_whitelisted(rdir):
    """外部 AI 生成的规则里带了个穿越名 ⇒ 一样拒。"""
    h = _H(rdir)
    r = h._api_adapter_save({"name": "", "rule": GOOD_YAML.replace(
        "name: probe_site", "name: ../evil")})
    assert r["ok"] is False
    assert _rule_files(rdir) == []


def test_duplicate_name_is_refused(rdir):
    """同名覆盖会让一条正在用的规则被人从网页上悄悄改掉。"""
    h = _H(rdir)
    assert h._api_adapter_save({"name": "dup", "rule": GOOD_YAML})["ok"]
    r = h._api_adapter_save({"name": "dup", "rule": GOOD_YAML})
    assert r["ok"] is False
    assert "dup" in (r.get("error") or "")


def test_error_messages_are_chinese(rdir):
    for body in ({"name": "x", "rule": ""},
                 {"name": "../x", "rule": GOOD_YAML},
                 {"name": "y", "rule": "- 1\n"}):
        r = _H(rdir)._api_adapter_save(body)
        msg = r.get("error") or ""
        assert msg, "失败必须有原因"
        for en in ("Error", "Exception", "Traceback"):
            assert en not in msg, f"错误提示混进英文：{msg}"


# ------------------------------------------------------------ 删除


def test_delete_removes_rule(rdir):
    h = _H(rdir)
    h._api_adapter_save({"name": "todelete", "rule": GOOD_YAML})
    assert (rdir / "todelete.yaml").exists()
    assert _H(rdir)._api_adapter_delete({"name": "todelete"})["ok"] is True
    assert not (rdir / "todelete.yaml").exists()


@pytest.mark.parametrize("bad_name", ["../evil", "a/b", "", ".."])
def test_delete_rejects_non_whitelisted_names(rdir, bad_name):
    assert _H(rdir)._api_adapter_delete({"name": bad_name})["ok"] is False


def test_delete_missing_rule_reports_chinese(rdir):
    r = _H(rdir)._api_adapter_delete({"name": "nope_not_here"})
    assert r["ok"] is False
    assert r.get("error"), "找不到也得给中文原因"


# ------------------------------------------------------------ 前端
