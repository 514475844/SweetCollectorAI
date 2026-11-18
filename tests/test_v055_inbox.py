# -*- coding: utf-8 -*-
"""V0.0.55 批1 护栏：手机收件口 + 公众号规则。

收件口 = 安卓壳分享来的链接自动建任务运行（复用现有 quick 链，
不另发明鉴权/token），去重靠"同链接有没有未结束任务"。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── 收件口链接解析 ────────────────────────────────────────
def test_clean_link_extracts_url():
    from media_resource_manager.inbox import clean_link
    assert clean_link("https://mp.weixin.qq.com/s/abc") == \
        "https://mp.weixin.qq.com/s/abc"
    # 微信分享文本常带标题/尾巴
    assert clean_link("好看 https://mp.weixin.qq.com/s/abc 来自微信") == \
        "https://mp.weixin.qq.com/s/abc"
    # 中文标点截断
    assert clean_link("看下 https://a.com/x.png，很不错") == \
        "https://a.com/x.png"
    assert clean_link("没有链接") == ""


def test_clean_link_strips_cjk_trailing_punctuation():
    """V0.0.57：真机微信/浏览器分享文本拿标点收尾，链接不能挂标点。

    模拟器塞的是纯 URL，测不出这个；真机一分享「标题。链接。」就 404。
    """
    from media_resource_manager.inbox import clean_link
    u = "https://mp.weixin.qq.com/s/abc123"
    # 中文句号（前后）、全角问号、省略号、书名号——微信常见形态
    assert clean_link("快看这篇文章。" + u + "。") == u
    assert clean_link(u + "？") == u
    assert clean_link("【重磅】" + u + "…") == u
    assert clean_link("文章标题：" + u + "，快看！") == u
    # 半角句末标点单独剥
    assert clean_link(u + ".") == u
    # URL 自带 query 不能被误伤
    q = u + "?a=1&b=2"
    assert clean_link(q) == q
    # 原有纯 URL / 带尾巴不回归
    assert clean_link("标题 " + u + " 来自微信公众号") == u


def test_reachable_site_rejects_non_web():
    from media_resource_manager.inbox import is_reachable_site
    assert is_reachable_site("ftp://x")[0] is False
    assert is_reachable_site("https://mp.weixin.qq.com/s/abc")[0] is True


# ── 接口路由与建任务链 ────────────────────────────────────
def test_inbox_endpoint_routed_and_uses_existing_chain():
    ui = _read("media_resource_manager/webui.py")
    assert '"/api/inbox/link"' in ui, "收件口路由没接"
    m = ui.split("def _api_inbox_link", 1)[1]
    # 复用桌面/网页同一条建任务与入队链，不另写引擎
    for k in ("from .repo import save_task", "from .models import TaskConfig",
              "q.submit(", "clean_link(", "find_open_task_for("):
        assert k in m, f"收件口缺「{k}」"


def test_inbox_dedup_query_open_tasks():
    from media_resource_manager.inbox import find_open_task_for
    import sqlite3
    db = type("D", (), {})()
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE tasks (id int, entry_urls text, status text,"
                " is_template int)")
    con.execute("INSERT INTO tasks VALUES (7, ?, 'running', 0)",
                ('["https://mp.weixin.qq.com/s/abc"]',))
    db.query = lambda sql, p=(): [dict(zip(("id", "entry_urls", "status"), r))
                                  for r in con.execute(sql, p)]
    assert find_open_task_for(db, "https://mp.weixin.qq.com/s/abc") == 7
    assert find_open_task_for(db, "https://other/x") is None


def test_inbox_in_hiddenimports():
    assert '"media_resource_manager.inbox"' in _read("build/pyinstaller.spec")


# ── 公众号规则已本地化 ────────────────────────────────────
def test_wechat_rule_present_locally():
    p = ROOT / "data" / "rules" / "wechat_mp_article.yaml"
    s = p.read_text(encoding="utf-8")
    assert "#js_content img[data-src]" in s
    # 640 缩略 → /0? 原图（实测 221KB→343KB）
    assert "wx_fmt" in s and "/640" in s
    assert "mmbiz.qpic.cn" in s
