# -*- coding: utf-8 -*-
"""V0.0.78 护栏：五项已确认 bug 的回归钉子。

  1. retry_failed_urls 必须先 _prepare_ai（重试每次新建 Engine，AI 分类器全空，
     _refresh_terms 首次只记版本号返回 False → 重试路径 AI 过滤整条失效）。
  2. gallery-dl 快路径写 status='stopped' / 'done' 前必须查 db.is_closed()
     （关窗口「先停队列再 close db」，宽 except 会把任务误记 ERROR）。
  3. gallery-dl 快路径每张图入库前必须接 _refresh_terms + 重建分类器
     （V0.0.77 的运行中词表热更新原来只长在 _process_image）。
  4. find_terms.collect 的反馈词必须套 _clashes_with_theme 部分重叠保护
     （主题「风景」+ 反馈「风景 插画」，auto_harden 开时曾硬拦整个主题）。
  5. 空正则规则 "//" strip("/") 后为空串，必须跳过（re.search("", blob) 恒真）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest


# ── 1. retry_failed_urls 先准备 AI ──────────────────────────────────
def test_retry_prepares_ai_before_processing(tmp_config, tmp_db, monkeypatch):
    """行为断言：重试路径真的会调 _prepare_ai，且早于 _process_image。"""
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig

    task = TaskConfig(id=1, name="t", adapter_name="", entry_urls=[])
    order: list[str] = []

    import media_resource_manager.repo as repo
    monkeypatch.setattr(repo, "get_task", lambda db, tid: task)
    monkeypatch.setattr(tmp_db, "list_failed_urls",
                        lambda tid: [{"url": "https://e.com/cat.jpg"}])
    monkeypatch.setattr(tmp_db, "is_closed", lambda: False)
    monkeypatch.setattr(tmp_db, "audit", lambda *a, **k: None)
    monkeypatch.setattr(tmp_db, "record_failed_url", lambda *a, **k: None)
    monkeypatch.setattr(tmp_db, "delete_failed_urls", lambda *a, **k: None)

    e = Engine(tmp_config, tmp_db)
    monkeypatch.setattr(e, "_prepare_ai", lambda t, r: order.append("prepare"))
    monkeypatch.setattr(e, "_resolve_proxy", lambda t: "")

    class _StubFetcher:
        async def close(self):
            pass

    monkeypatch.setattr(e, "_make_fetcher", lambda *a, **k: _StubFetcher())
    monkeypatch.setattr(e, "_make_downloader", lambda *a, **k: object())

    async def _process(*a, **k):
        order.append("process")

    monkeypatch.setattr(e, "_process_image", _process)

    out = asyncio.run(e.retry_failed_urls(1))
    assert out["ok"] is True
    assert order == ["prepare", "process"], (
        f"重试必须先 _prepare_ai 再 _process_image，实际顺序 {order}")

    # 静态护栏：即便有人把显式调用挪到 downloader 之后，上面行为测试仍红；
    # 这条额外钉住「调用写在 retry_failed_urls 函数体内」防整段删除。
    src = inspect.getsource(Engine.retry_failed_urls)
    assert "self._prepare_ai(task, rep)" in src, "retry_failed_urls 未接 _prepare_ai"


# ── 2 & 3. gallery-dl 快路径 ────────────────────────────────────────
def test_gallery_dl_closed_db_skips_all_status_writes(
        tmp_config, tmp_db, monkeypatch):
    """行为断言：库已关时，取消分支和正常收尾都不许写 status。"""
    from media_resource_manager.engine import Engine, RunReport

    task = SimpleNamespace(id=7, output_dir=None, use_proxy=False,
                           proxy_id=None, proxy=None, delay=0.5, keywords=[])
    written: list[str] = []
    monkeypatch.setattr(tmp_db, "is_closed", lambda: True)
    monkeypatch.setattr(tmp_db, "execute",
                        lambda sql, *a, **k: written.append(sql))
    monkeypatch.setattr(tmp_db, "audit", lambda *a, **k: None)

    import media_resource_manager.proxy as proxymod

    class _Pool:
        def __init__(self, db):
            pass

        def resolve(self, *a, **k):
            return ""

    monkeypatch.setattr(proxymod, "ProxyPool", _Pool)

    import media_resource_manager.backends.gallery_dl as gd

    class _Backend:
        def __init__(self, **kw):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(gd, "GalleryDLBackend", _Backend)
    monkeypatch.setattr(gd, "is_bare_homepage", lambda url: False)

    import media_resource_manager.engine as engmod
    monkeypatch.setattr(engmod, "ua_override_allowed", lambda url: False)

    e = Engine(tmp_config, tmp_db)
    monkeypatch.setattr(e, "_insecure_entry", lambda entries: False)

    async def _cancel(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(e, "_consume_gallery_dl", _cancel)
    rep = RunReport(task_id=7)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(e._run_via_gallery_dl(task, None,
                                          ["https://e.com/p/1"], rep))
    assert not written, f"库已关，取消分支不应写库：{written}"

    # 正常收尾：库已关 → 直接 return，不写 status='done'
    written.clear()

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr(e, "_consume_gallery_dl", _noop)
    asyncio.run(e._run_via_gallery_dl(task, None, ["https://e.com/p/1"], rep))
    assert not written, f"库已关，正常收尾不应写库：{written}"


def test_gallery_dl_fastpath_wires_hot_reload():
    """静态护栏：快路径每张图入库前必须接词表热更新 + 重建分类器。"""
    from media_resource_manager.engine import Engine
    src = inspect.getsource(Engine._consume_gallery_dl)
    assert "_refresh_terms(task)" in src, "快路径未接运行中词表热更新"
    assert "self._ai_ready_for = None" in src, "词表变化后未强制重建分类器"
    assert "self._prepare_ai(task, rep)" in src, "词表变化后未重建分类器"


# ── 4. collect 反馈词部分重叠保护 ───────────────────────────────────
def test_collect_downgrades_feedback_clashing_with_theme(tmp_db):
    """主题「风景」+ 反馈学到「风景 插画」→ 必须降级，不许硬拦整主题。"""
    from media_resource_manager import find_terms
    from media_resource_manager.models import TaskConfig

    tmp_db.meta_set("feedback_avoid.v1", json.dumps({
        "风景 插画": {"hits": 2, "last": "", "acts": "dislike"},
    }, ensure_ascii=False))
    task = TaskConfig(id=1, name="t", adapter_name="", ai_labels=["风景"])
    cfg = SimpleNamespace(global_blacklist=[], feedback_auto_harden=True)

    terms = find_terms.collect(tmp_db, task, cfg)
    assert "风景" in terms.want, "主题词不该被反馈词顶掉"
    assert "风景 插画" not in terms.avoid, (
        "反馈词与主题词部分重叠时必须降级，auto_harden 开着也不能硬拦整主题")


# ── 5. 空正则规则跳过 ───────────────────────────────────────────────
def test_empty_regex_rule_is_ignored():
    """规则 "//" strip("/") 后为空串，不得让所有图片命中排除。"""
    from media_resource_manager.engine import _hit_blacklist
    from media_resource_manager import find_terms

    # engine 侧：空正则不得命中任何图；正常正则仍须生效
    assert _hit_blacklist("https://e.com/a.jpg", [], ["//"]) is False
    assert _hit_blacklist("https://e.com/cat.jpg", [], ["/cat/"]) is True

    # find_terms 侧：同样跳过，且正常正则大小写不敏感口径不变
    assert find_terms._word_hit("//", "anything") is False
    assert find_terms._word_hit("/cosplay/", "a COSPLAY pic") is True
