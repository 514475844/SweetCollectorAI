# -*- coding: utf-8 -*-
"""手机收件口（2026-11-18 批1）：安卓壳分享来的链接 → 自动建任务。

设计（与用户定案一致）：安卓壳只做"收件 + 转发"，所有功能留在电脑。
本模块就是电脑这边的收件口：

* 收到一个网页链接 → 按现有适配器规则自动匹配站点 → 建任务 → 立即运行；
* 没有匹配规则时退回通用采集（引擎自带的 BFS/DFS 兜底路径），
  同样建任务跑起来 —— 壳那边不需要懂任何业务；
* 去重：同一链接的**未结束**任务不重复建（跑完的可以再收一次，
  增量采集会跳过已入库图）。

鉴权：复用 Web 层现有会话/密码体系（走标准 do_POST 管道即可），
本模块不另发明一套 token —— 手机壳登录一次（Web 密码），壳自己保存
会话 Cookie，之后调用和网页等价。
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

# V0.0.57：真机微信/浏览器分享文本常拿标点收尾——「快看。https://x/s/abc。」
# 「https://x/s/abc？」「【标题】https://x/s/abc…」。老正则只排除了中文逗号等，
# 漏了句号/全角问号/省略号，链接末尾就挂个标点 → 采集时 404。
# 合法 URL 里不会出现未编码的全角字符，统一列进排除集；半角句末标点单独剥尾部。
_PUNCT_EXCLUDE = (
    "，。、；：？！（）【】《》「」『』“”‘’…—～·"   # 全角标点
    ",<>\"'）)】"                                  # 半角/原有
)
_URL_BODY = r"https?://[^\s" + re.escape(_PUNCT_EXCLUDE) + r"]+"
_URL_RE = re.compile("^" + _URL_BODY + "$", re.I)
_SEARCH_RE = re.compile(_URL_BODY, re.I)
_TAIL_TRIM = ".,;!?"   # 只剥最末尾的半角句末标点，域名/路径中间的点不受影响


def clean_link(raw) -> str:
    """从分享文本里抠出第一个网页链接。

    安卓"更多打开方式"传来的一般就是纯 URL，但也常见：
    「文章标题 https://mp.weixin.qq.com/s/xxx 来自微信公众号」
    「快看这篇文章。https://mp.weixin.qq.com/s/xxx。」
    —— 微信/浏览器分享文本常带标题和标点尾巴，都要剔干净。抠不到返回空串。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _URL_RE.match(text) or _SEARCH_RE.search(text)
    if not m:
        return ""
    return m.group(0).rstrip(_TAIL_TRIM)


def is_reachable_site(url: str) -> tuple[bool, str]:
    """收件口只收 http/https 网页；顺手挡明显不是网页的地址。

    不做完整 SSRF（那是采集链 fetcher 的职责，跑任务时自然会拦）；
    这里只把明显垃圾（空、非 web 协议）挡在门口，给壳一个即时反馈。
    """
    try:
        sp = urlsplit(url)
    except ValueError:
        return False, "网址格式不对"
    if sp.scheme not in ("http", "https"):
        return False, "只支持 http/https 网页链接"
    if not sp.netloc:
        return False, "网址缺少域名"
    return True, ""


def find_open_task_for(db, url: str) -> int | None:
    """同链接已有未结束任务（idle 以外）→ 返回其 id，不重复建。"""
    import json as _json
    try:
        rows = db.query(
            "SELECT id, entry_urls, status FROM tasks "
            "WHERE is_template=0 AND status IN ('running','paused','queued')")
    except Exception:
        return None
    for r in rows or []:
        try:
            urls = _json.loads(r["entry_urls"] or "[]")
        except Exception:
            continue
        if url in [str(x).strip() for x in urls]:
            return int(r["id"])
    return None
