"""L3 · 站点适配器规则：可加载性 + 逐站封锁结论。

作业单：`.workbuddy/LEGACY_DEBT_ORDER.md` L3
「best-effort 给这些站补/修适配器规则（data/rules/*.yaml）；若返回 403 是
站点侧封锁（需 cookie/登录/代理，本机无），不要死循环重试——写好规则、
记清「需要用户提供 cookie 或代理才能解封」、纳入 check_rules_drift，
然后跳过。验收：能补规则的站跑通一条真实抓取；解不开的站在报告里逐站写明
原因与所需条件。」

**本文件只锁「规则本身是好的」这一层**，不联网：
  ① 全部规则都能被 load_adapter 解析（选择器字段齐、YAML 没写坏；R173 后 21 份）；
  ② L3 三个问题站的规则里写清了封锁原因与解封条件（别过几天又变成
     「不知道为啥抓不到」）；
  ③ gb84791rx 必须放行 302 跳转后的新域名（本轮实测补的，漏了等于白改）；
  ④ validate_adapters 工具不许再因「代理不存在」而全站误报。

**为什么不在测试里联网**：CI/别的机器上网络状况不同，会变成随机红。
实测结论记在 yaml 注释与last_run_report.txt 里，测试只保证「结论没被删」。
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES = os.path.join(ROOT, "data", "rules")


def _all_rules():
    return sorted(f for f in os.listdir(RULES) if f.endswith((".yaml", ".yml")))


# --------------------------------------------------------------- 1. 全部可加载
def test_l3_all_rules_load():
    """全部规则都要能被 load_adapter 解析（YAML 缩进/字段名写错都会在这炸）。

    R187 记：原判据钉死 `== 16`。R173 落地时按用户订单补了 5 份图站规则
    （danbooru/gelbooru/konachan/rule34/yandere，2026-10-20 15:32），规则数
    合法地变成 21 —— 数量钉死会拦住每一次「补新站规则」这种纯加法改动。
    改为下限 16（保底不许少）+ 全部可加载 + 全部 .yaml（下方既有护栏），
    不再拦新增。
    """
    from media_resource_manager.crawler.rules import load_adapter
    from pathlib import Path
    files = _all_rules()
    assert len(files) >= 16, f"规则数低于保底 16，实得 {len(files)}：{files}"
    bad = []
    for f in files:
        try:
            rule = load_adapter(Path(RULES) / f)
            assert rule.name
            assert rule.base_url, f"{f} 缺 base_url"
        except Exception as e:
            bad.append((f, f"{type(e).__name__}: {e}"))
    assert not bad, f"规则加载失败：{bad}"


def test_l3_rule_count_uses_yaml_not_yml(tmp_path):
    """规则是 .yaml 不是 .yml —— 只 glob *.yml 会数到 0 并误判打包失败。

    （这是 R26 已修过的坑，这里再钉一次，防有人改 glob 时踩回去。）
    """
    files = _all_rules()
    assert all(f.endswith(".yaml") for f in files), \
        f"出现了 .yml：{[f for f in files if f.endswith('.yml')]}"


# --------------------------------------------------------------- 2. L3 三站
def test_l3_gb84791rx_allows_redirect_domain():
    """gb84791rx 实测 302 跳到 rn92543wd.com，两个域都必须放行。

    漏了新域名 → 跟随跳转后抓到的页面被域名门禁挡掉，规则等于失效。
    """
    from media_resource_manager.crawler.rules import load_adapter
    from pathlib import Path
    rule = load_adapter(Path(RULES) / "gb84791rx.yaml")
    doms = set(rule.allowed_domains) | rule.domains()
    assert "rn92543wd.com" in doms, (
        "gb84791rx 规则没放行 302 后的新域 rn92543wd.com（L3 实测补的）")
    assert "gb84791rx.com" in doms, "连原域名都丢了"


@pytest.mark.parametrize("site", ["xchina", "spankbang"])
def test_l3_blocked_sites_document_reason_and_unblock(site):
    """解不开的站，规则里必须写清「为什么」和「需要什么条件才能解」。

    这是作业单 L3 的原话要求。不写的话，下次看到 403 又要重新查一轮，
    而查出来的结论多半还是一样的。
    """
    from pathlib import Path
    text = (Path(RULES) / f"{site}.yaml").read_text(encoding="utf-8")
    assert "403" in text, f"{site}.yaml 没写明当前 403"
    assert "Cloudflare" in text or "cloudflare" in text, \
        f"{site}.yaml 没写明是站点侧封锁（Cloudflare）"
    # 解封所需条件：cookie / 代理 / 解除风控，至少提到前两者
    assert "cookie" in text or "代理" in text, \
        f"{site}.yaml 没写「需要 cookie 或代理才能解封」"


def test_l3_no_dead_proxy_in_validator():
    """护栏：validate_adapters 不许无条件套一个可能不存在的代理。

    原写法 `httpx.Client(proxy=PROXY)` 在没装代理的机器上 → 每个站都
    ConnectError，输出清一色「fetch failed: code=None」，看起来像
    「所有站点都挂了」，实际只是本机没代理 —— 会把L3 的结论带偏。
    """
    src = open(os.path.join(ROOT, "tools", "validate_adapters.py"),
               encoding="utf-8").read()
    assert "_proxy_alive" in src, "缺代理探活，退化回「本机没代理就全站误报」"
    # fetch 里必须按探活结果决定用不用代理
    assert "proxy = PROXY if _proxy_alive(PROXY) else None" in src, \
        "fetch() 仍无条件用 PROXY"
    # 也不能再有写死的 D 盘输出目录（本机无 D 盘，mkdir 直接 PermissionError）。
    # ⚠ 判据必须走 AST 只看**字符串常量**，不能全文搜：本仓注释里到处是
    # 「原来写死 D:/c/…」这类解释性文字，全文搜会匹配到自己写的注释而假红
    # （这条坑本轮真踩了一次）。
    import ast
    strs = [n.value for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    dead = [s for s in strs if "D:/c/adapter_overlay" in s]
    assert not dead, f"validate_adapters 里还有死的 D 盘输出目录：{dead}"


def test_l3_candidates_include_all_three_sites():
    """L3 点名的三个站必须在实测候选列表里，否则工具压根不测它们。"""
    src = open(os.path.join(ROOT, "tools", "validate_adapters.py"),
               encoding="utf-8").read()
    for site in ("gb84791rx", "spankbang", "xchina"):
        assert f'"{site}"' in src, f"候选列表缺 {site}，工具不会实测它"