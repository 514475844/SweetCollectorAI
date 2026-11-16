"""R253 · 让 AI 写适配器（网页端也能用），并且**先本地验证再入库**。

为什么以前没做：``adapter_proposer.py`` 的注释写着「软件里只有视觉 CLIP、
没有文本生成模型，所以不做 AI 猜选择器」。那个前提**已经过时** ——
设置里本来就有 OpenAI 兼容通道（``ai_api_url`` / ``ai_api_key`` /
``ai_api_model``），模型页一直在用它。

三条设计红线
------------
1. **不打包任何模型**：走用户已配的在线通道，体积 +0。塞本地 7B 会 +4–5GB
   且慢十倍，不值得。
2. **模型会编选择器**，所以「本地试抓验证」不能省：拿真页面 HTML 跑一遍
   ``image_link_selectors``，一张都匹配不到就**不入库**，退回纯启发式并说明
   原因。没有这道闸，自动生成的规则会静默产出 0 张图 —— 比没有更糟。
3. **确认才落盘**：生成结果回给前端预览，用户点保存才写 yaml
   （复用 ``/api/adapter/save`` 那条已经带校验的通道）。
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Optional

#: 送给模型的 HTML 上限。整页几十万字符既贵又没用 —— 选择器只需要结构。
HTML_LIMIT = 60000

#: 试抓验证时最多看多少张图（够判断"选择器对不对"就行）
SAMPLE_LIMIT = 20

_SYS = (
    "你是网页抓取规则生成器。只输出一个 JSON 对象，不要解释、不要寒暄。"
    "字段必须严格用给定清单里的名字，拿不准的字段直接省略，不要编造。"
)


def page_excerpt(html: str, limit: int = HTML_LIMIT) -> str:
    """压一压 HTML 再送给模型：去掉脚本/样式/注释，保留标签结构。

    不这么做的话，一个现代页面 80% 是 inline script，既烧 token 又把
    真正的 DOM 结构埋掉。
    """
    s = str(html or "")
    s = re.sub(r"(?is)<script.*?</script>", "", s)
    s = re.sub(r"(?is)<style.*?</style>", "", s)
    s = re.sub(r"(?is)<!--.*?-->", "", s)
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


def extract_json(text: str) -> Optional[dict]:
    """从模型回复里取出那个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    s = str(text or "")
    m = re.search(r"```(?:json|yaml|yml)?\s*(\{.*?\})\s*```", s, re.S)
    raw = m.group(1) if m else None
    if raw is None:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            raw = s[i:j + 1]
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def chat(cfg, system: str, user: str, timeout: int = 90) -> str:
    """调 OpenAI 兼容的 /chat/completions。**同一套配置**，不另建通道。

    ⚠ 返回值取 ``message.content``（OpenAI 标准）。老代码只看 ``message.text``，
    那是某个自建网关的形状 —— 标准接口上永远是空串，功能会静默失效。
    """
    base = str(getattr(cfg, "ai_api_url", "") or "").rstrip("/")
    if not base:
        raise RuntimeError("没有配置在线 AI 地址（设置 → AI 模型 → 接口地址）")
    key = str(getattr(cfg, "ai_api_key", "") or "")
    model = str(getattr(cfg, "ai_api_model", "") or "") or "gpt-4o-mini"
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {key}"} if key else {})},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    msg = ((data.get("choices") or [{}])[0].get("message") or {})
    return str(msg.get("content") or msg.get("text") or "")


def build_prompt(url: str, html: str, template: str = "") -> str:
    """拼提示词：字段清单（与 AdapterRule 对齐）+ 目标网址 + 页面结构。"""
    return (
        f"{template}\n\n"
        f"【目标网址】\n{url}\n\n"
        f"【页面结构（已去掉脚本与样式）】\n{page_excerpt(html)}\n"
    )


def rule_from_dict(obj: dict, fallback_name: str = "adapter"):
    """把模型给的 dict 变成 AdapterRule —— **复用 load_adapter 的全部校验**。

    不自己写一份解析：字段名、选择器 kind 白名单、name 清洗、allowed_domains
    自动补全，全在 ``crawler.rules.load_adapter`` 里。这里写第二份必然漂移。
    """
    from pathlib import Path
    import tempfile

    from .crawler.rules import load_adapter

    suffix = ".json"
    tmp = Path(tempfile.mkdtemp(prefix="mrm_rule_ai_")) / f"rule{suffix}"
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        rule = load_adapter(tmp)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    if not rule.name or rule.name == "adapter":
        rule.name = fallback_name
    return rule


def count_selector_images(rule, html: str, url: str) -> dict:
    """用规则里的 ``image_link_selectors`` **只跑选择器**，数匹配到几张图。

    为什么不用 ``AdapterRunner.extract_images``：它还会捞 og:image 与
    JSON-LD。一个只有 og:image 的页面（列表页、广告页）会让验证"通过"，
    而真正抓的时候一张图也拿不到 —— 验证就白做了。
    """
    from .crawler.adapter import AdapterRunner, _extract_with
    from .utils.url import normalize_url

    runner = AdapterRunner(rule)
    soup = runner._soup(html)
    out, warnings = [], []
    for sel in rule.image_link_selectors or []:
        try:
            vals = _extract_with(soup, sel, html)
        except Exception as e:                     # 选择器语法错、缺 lxml 等
            warnings.append(f"选择器 {sel.get('expr')} 执行失败：{e}")
            continue
        if not vals:
            warnings.append(f"选择器 {sel.get('expr')} 匹配 0 个节点")
        for v in vals:
            n = normalize_url(str(v), url)
            if n and n not in out:
                out.append(n)
    return {"images": out[:SAMPLE_LIMIT], "count": len(out),
            "warnings": warnings}


def validate(rule, html: str, url: str) -> dict:
    """规则是否真能用：结构齐全 + 选择器能匹配到图。"""
    from .crawler.rules import healthy

    if not healthy(rule):
        missing = []
        if not (rule.base_url or rule.entry_urls):
            missing.append("base_url/entry_urls")
        if not rule.allowed_domains:
            missing.append("allowed_domains")
        if not rule.image_link_selectors:
            missing.append("image_link_selectors")
        return {"ok": False, "count": 0, "images": [],
                "reason": "规则缺必备字段：" + "、".join(missing) + "（通常说明"
                          "这个页面不是图册页，或模型没看懂结构）"}
    res = count_selector_images(rule, html, url)
    ok = res["count"] > 0
    return {
        "ok": ok, "count": res["count"], "images": res["images"],
        "warnings": res["warnings"],
        "reason": "" if ok else (
            "图片选择器在这张页面上匹配到 0 张图。可能原因：页面是 JS 渲染的"
            "（HTML 里没有图）、选择器写错、或这个网址不是图册页。"
            "已给出纯启发式结果供你参考。"),
    }


def propose(cfg, url: str, html: str, template: str = "",
            use_ai: bool = True) -> dict:
    """生成候选规则。**AI 优先，验证不过就退回启发式。**

    返回 ``{"ok", "rule", "rule_text", "source", "count", "notes"}``，
    ``ok=False`` 表示两种方式都没能产出"能抓到图"的规则 —— 这时前端应该
    让用户去桌面端人工预览，而不是把一条 0 图的规则存进去。
    """
    from .crawler.adapter_proposer import dump_rule_yaml, propose_adapter
    from .utils.url import domain_of

    dom = domain_of(url) or "adapter"
    notes: list[str] = []
    ai_rule = None
    ai_count = 0

    if use_ai and str(getattr(cfg, "ai_api_url", "") or ""):
        try:
            reply = chat(cfg, _SYS, build_prompt(url, html, template))
            obj = extract_json(reply)
            if obj is None:
                notes.append("模型回复里没有可解析的 JSON，已改用纯启发式")
            else:
                ai_rule = rule_from_dict(obj, fallback_name=dom)
                v = validate(ai_rule, html, url)
                ai_count = v["count"]
                if v["ok"]:
                    notes.append(f"AI 规则验证通过：本页匹配到 {ai_count} 张图")
                else:
                    notes.append("AI 规则未通过本地验证：" + v["reason"])
                    ai_rule = None
        except Exception as e:                      # 网络/额度/格式都可能
            notes.append(f"调用在线模型失败（{type(e).__name__}），已改用纯启发式：{e}")

    if ai_rule is not None:
        return {"ok": True, "rule": ai_rule, "rule_text": dump_rule_yaml(ai_rule),
                "source": "ai", "count": ai_count, "notes": notes}

    # 启发式兜底：纯本地、不联网，用页面里的 img/链接猜
    try:
        h_rule, meta = propose_adapter(url, html)
    except Exception as e:
        return {"ok": False, "rule": None, "rule_text": "", "source": "none",
                "count": 0, "notes": notes + [f"启发式生成失败：{e}"]}
    v2 = validate(h_rule, html, url)
    notes.append(f"启发式规则本页匹配到 {v2['count']} 张图"
                 + ("（可直接用）" if v2["ok"] else "（建议人工补充选择器）"))
    if meta.get("note"):
        notes.append(str(meta["note"]))
    return {"ok": v2["ok"], "rule": h_rule,
            "rule_text": dump_rule_yaml(h_rule), "source": "heuristic",
            "count": v2["count"], "notes": notes}


def fetch_page(cfg, url: str, timeout: float = 20.0) -> str:
    """抓一页 HTML 来验证规则。**带上软件已有的 UA 与站点 Cookie**。

    为什么不用 ``adapter_proposer.fetch_url``：那个写死
    ``User-Agent: Mozilla/5.0``、也不带 Cookie。对需要登录或挡 UA 的站，
    用它抓回来的只会是验证页 —— 于是「AI 生成的规则验证失败」，
    而真正的原因是抓页面这一步就被拒了，用户根本看不出区别。
    """
    import httpx

    from .cookies import cookie_for_task
    from .utils.url import domain_of

    ua = str(getattr(cfg, "user_agent", "") or "") or "Mozilla/5.0"
    dom = domain_of(url)
    cookie = cookie_for_task("", dom) if dom else ""
    proxy = str(getattr(cfg, "proxy", "") or "") or None
    headers = {"User-Agent": ua}
    if cookie:
        headers["Cookie"] = cookie
    with httpx.Client(timeout=timeout, follow_redirects=True,
                      proxy=proxy, verify=bool(getattr(cfg, "ssl_verify", True)),
                      headers=headers) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text
