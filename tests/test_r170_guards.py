"""R170 护栏：R154-F7「失败 URL 清单落库 + 按 URL 粒度重试」（执行方笔记本棒）。

行为断言优先、源码判据兜底；变异自检 .workbuddy/_r170_mutate.py。

覆盖（与 R154-F7 订单逐条对齐）：
- DB 五方法：record/list/count/delete/clear —— UNIQUE(task_id,url) 不堆行、
  tries 累加、任务间隔离、delete 返回**真实**删除行数（UI 汇报靠它）；
- engine._process_image 下载失败 → failed_urls 落库（**完整清单**替代原来的
  「错误采样最多 8 条」截断）；
- engine._consume_gallery_dl 入口级异常 → 按入口 URL 记一条（诚实粒度：
  gallery-dl 子进程内部拿不到单图 URL，不臆造）；
- engine.retry_failed_urls 三类结局（succeeded / still_failed / filtered）
  + 守卫（任务不存在 / 适配器不合法 / 无合规授权 / 空清单 / 子集重试 /
  内部异常不消失）+ audit 记账；
- queue.retry_failed_urls 管线：任务在跑拒（两个 Engine 叠在一处会互踩）、
  loop 未起拒、正常跑通发 on_event("retry-urls")；
- webui 三 API：读清单 / 重试（队列 None、任务在跑、空清单、超时 pending、
  正常透传）/ 清空 + 中文错误 + 路由字面量护栏（不改成 f-string 循环）。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager.downloader.downloader import DownloadOutcome
from media_resource_manager.models import TaskConfig
from media_resource_manager.repo import save_task


# ==================== 共用小工具 ====================

def _mk_task(tmp_config, tmp_db, **kw) -> int:
    """建一条任务（默认自动适配器 adapter_name="" ⇒ gallery-dl/自动路径）。"""
    kw.setdefault("adapter_name", "")
    t = TaskConfig(id=None, name="f7",
                   entry_urls=["https://site.com/a1"],
                   output_dir=str(tmp_config.library_dir), **kw)
    return save_task(tmp_db, t)


def _authorize(tmp_db, site="site.com"):
    tmp_db.execute(
        "INSERT INTO compliance(site, robots_ok, basis) VALUES (?,?,?)",
        (site, 1, "fixture test"))


class _FakeDL:
    """按 URL 配置结果的假下载器：不碰网络/磁盘。

    results: url -> DownloadOutcome；没配的 URL 一律 ok=False。
    """

    def __init__(self, results=None):
        self._res = dict(results or {})
        self.calls: list[str] = []

    async def download_one(self, url, *, site="site", album="", date="",
                           keyword="", meta=None, already=None):
        self.calls.append(url)
        return self._res.get(url) or DownloadOutcome(
            url=url, path=None, ok=False, reason="http 500")


class _BoomBE:
    """gallery-dl 假替身：download_iter 一律抛（入口级失败）。"""

    def download_iter(self, url):
        raise RuntimeError("gallery-dl exploded")

    def stop(self):
        pass


def _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch):
    """真 Engine（真库）+ 假 fetcher/下载器，返回 (eng, pages)。"""
    from media_resource_manager.engine import Engine
    eng = Engine(tmp_config, tmp_db,
                 fetcher_factory=lambda cfg, proxy: fake_fetcher({}))
    monkeypatch.setattr(eng, "_make_downloader",
                        lambda task, fetcher: dl)
    return eng


# ==================== 1. DB 五方法 ====================

def _seed_tasks(tmp_db, *tids):
    """R221/C10：FK 开启后 failed_urls 的父行（tasks）必须先存在。

    这些用例测的是失败清单本身，与 tasks 表无关，但外键不管这个 ——
    父行不铺，INSERT 直接 IntegrityError（R220 r38 手搓对象同族）。
    """
    for tid in tids:
        tmp_db.execute("INSERT INTO tasks(id,name) VALUES (?,?)",
                       (tid, f"t{tid}"))


def test_db_record_failed_url_inserts_one_row(tmp_db):
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "http 500")
    rows = tmp_db.list_failed_urls(1)
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["url"] == "https://s.com/1.jpg"
    assert r["error"] == "http 500"
    assert int(r["tries"]) == 1
    assert r["first_at"] and r["last_tried"], "时间戳没记上"


def test_db_record_same_url_increments_tries_no_dup_rows(tmp_db):
    """反复失败同一 URL：tries 累加、原因刷新、绝不堆行（UNIQUE 约束）。"""
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "http 500")
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "timeout")
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "timeout again")
    rows = tmp_db.list_failed_urls(1)
    assert len(rows) == 1, f"堆行了：{[dict(r) for r in rows]}"
    assert int(rows[0]["tries"]) == 3
    assert rows[0]["error"] == "timeout again", "原因没刷新成最新一次"


def test_db_failed_urls_isolated_by_task(tmp_db):
    """清单按任务隔离：A 任务的失败不该出现在 B 任务页。"""
    _seed_tasks(tmp_db, 1, 2)
    tmp_db.record_failed_url(1, "https://s.com/a.jpg", "e1")
    tmp_db.record_failed_url(2, "https://s.com/b.jpg", "e2")
    assert [r["url"] for r in tmp_db.list_failed_urls(1)] == \
        ["https://s.com/a.jpg"]
    assert [r["url"] for r in tmp_db.list_failed_urls(2)] == \
        ["https://s.com/b.jpg"]
    assert tmp_db.failed_url_count(1) == 1
    assert tmp_db.failed_url_count(2) == 1
    assert tmp_db.failed_url_count(99) == 0


def test_db_record_empty_url_ignored(tmp_db):
    tmp_db.record_failed_url(1, "", "x")
    tmp_db.record_failed_url(1, "   ", "x")
    assert tmp_db.list_failed_urls(1) == []


def test_db_delete_failed_urls_returns_real_rowcount(tmp_db):
    """重试成功后划掉条目：返回**真实**删除行数（UI  toast 靠它）。"""
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "e")
    tmp_db.record_failed_url(1, "https://s.com/2.jpg", "e")
    n = tmp_db.delete_failed_urls(1, ["https://s.com/1.jpg",
                                      "https://s.com/never.jpg"])
    assert n == 1, f"删了不存在的行也计数了？n={n}"
    assert [r["url"] for r in tmp_db.list_failed_urls(1)] == \
        ["https://s.com/2.jpg"]
    assert tmp_db.delete_failed_urls(1, []) == 0


def test_db_clear_failed_urls_only_target_task(tmp_db):
    """显式清空只清本任务，不动别的任务、更不动图片数据。"""
    _seed_tasks(tmp_db, 1, 2)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "e")
    tmp_db.record_failed_url(1, "https://s.com/2.jpg", "e")
    tmp_db.record_failed_url(2, "https://s.com/3.jpg", "e")
    n = tmp_db.clear_failed_urls(1)
    assert n == 2
    assert tmp_db.list_failed_urls(1) == []
    assert len(tmp_db.list_failed_urls(2)) == 1


def test_db_schema_has_failed_urls_unique(tmp_db):
    """源码判据：表 + UNIQUE(task_id,url) + 任务索引都在（老库自动迁移靠
    CREATE TABLE IF NOT EXISTS）。"""
    src = (ROOT / "media_resource_manager" / "db.py").read_text(
        encoding="utf-8")
    idx = src.find("CREATE TABLE IF NOT EXISTS failed_urls")
    assert idx != -1, "db.py 没有 failed_urls 表"
    block = src[idx:idx + 700]
    assert "UNIQUE(task_id, url)" in block or \
        "UNIQUE(task_id,url)" in block, "缺 UNIQUE 约束（会堆行）"
    assert "idx_failed_urls_task" in src, "缺任务索引"


# ==================== 2. engine 失败落库 ====================

def test_process_image_failure_records_full_url(tmp_config, tmp_db,
                                                fake_fetcher, monkeypatch):
    """下载失败 → failed_urls 落库（这是 F7 的'完整清单'，采样只是摘要）。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    dl = _FakeDL()          # 默认全部 ok=False
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    from media_resource_manager.engine import RunReport
    rep = RunReport(task_id=tid)
    asyncio.run(eng._process_image(
        _task_of(tmp_db, tid), None, dl,
        "https://site.com/img/9.jpg", "album", "https://site.com/a1", rep))
    assert rep.failed == 1
    rows = tmp_db.list_failed_urls(tid)
    assert len(rows) == 1, rows
    assert rows[0]["url"] == "https://site.com/img/9.jpg"
    assert "http 500" in (rows[0]["error"] or ""), rows[0]["error"]


def test_process_image_repeat_failure_accumulates(tmp_config, tmp_db,
                                                  fake_fetcher, monkeypatch):
    """同一 URL 两次失败 → 清单里仍是一条，tries=2（重试次数看得见）。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    dl = _FakeDL()
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    from media_resource_manager.engine import RunReport
    task = _task_of(tmp_db, tid)
    for _ in range(2):
        rep = RunReport(task_id=tid)
        asyncio.run(eng._process_image(
            task, None, dl, "https://site.com/img/9.jpg", "a", "p", rep))
    rows = tmp_db.list_failed_urls(tid)
    assert len(rows) == 1 and int(rows[0]["tries"]) == 2, rows


def test_process_image_success_records_nothing(tmp_config, tmp_db,
                                               fake_fetcher, monkeypatch,
                                               tmp_path):
    """下载成功的 URL 不进失败清单（不误报）。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    good = tmp_path / "ok.jpg"
    good.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)   # 像个 JPEG 头
    dl = _FakeDL({"https://site.com/img/1.jpg": DownloadOutcome(
        url="https://site.com/img/1.jpg", path=good, ok=True,
        sha256="a" * 64, width=100, height=100, bytes=68)})
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    from media_resource_manager.engine import RunReport
    rep = RunReport(task_id=tid)
    asyncio.run(eng._process_image(
        _task_of(tmp_db, tid), None, dl,
        "https://site.com/img/1.jpg", "a", "p", rep))
    assert rep.downloaded == 1
    assert tmp_db.list_failed_urls(tid) == []


def test_gallery_dl_entry_exception_records_entry_url(tmp_config, tmp_db,
                                                      fake_fetcher,
                                                      monkeypatch):
    """快路径入口级异常 → 按**入口 URL** 记一条（gallery-dl 子进程内部
    拿不到单图 URL，粒度就是入口；不臆造单图地址）。"""
    from media_resource_manager.engine import Engine, RunReport
    eng = Engine(tmp_config, tmp_db,
                 fetcher_factory=lambda cfg, proxy: fake_fetcher({}))
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    task = _task_of(tmp_db, tid)
    rep = RunReport(task_id=tid)
    asyncio.run(eng._consume_gallery_dl(task, _BoomBE(),
                                        "https://site.com/a1", rep))
    rows = tmp_db.list_failed_urls(tid)
    assert len(rows) == 1, rows
    assert rows[0]["url"] == "https://site.com/a1"
    assert "gallery-dl" in (rows[0]["error"] or "")
    assert any("gallery-dl" in e for e in rep.errors), rep.errors


def _task_of(tmp_db, tid):
    from media_resource_manager.repo import get_task
    t = get_task(tmp_db, tid)
    assert t is not None
    return t


# ==================== 3. engine.retry_failed_urls ====================

def test_retry_succeeded_removes_from_list_and_audits(tmp_config, tmp_db,
                                                      fake_fetcher,
                                                      monkeypatch, tmp_path):
    """重试下载成功 → succeeded + 自动从清单划掉 + audit 记账。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    url = "https://site.com/img/1.jpg"
    tmp_db.record_failed_url(tid, url, "http 500")
    good = tmp_path / "ok.jpg"
    good.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    dl = _FakeDL({url: DownloadOutcome(
        url=url, path=good, ok=True, sha256="b" * 64,
        width=100, height=100, bytes=68)})
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    assert out["ok"] is True, out
    assert out["retried"] == 1
    assert out["succeeded"] == [url], out
    assert out["still_failed"] == []
    assert out["filtered"] == []
    assert tmp_db.list_failed_urls(tid) == [], "成功的 URL 没划掉"
    rows = tmp_db.query(
        "SELECT action, detail FROM audit_log "
        "WHERE action='retry-failed-urls'")
    assert rows and "ok=1" in rows[0]["detail"], rows


def test_retry_still_failed_keeps_row_and_bumps_tries(tmp_config, tmp_db,
                                                      fake_fetcher,
                                                      monkeypatch):
    """仍失败 → 留清单 + tries+1 + 原因刷新（用户能看到"试过几次还不行"）。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    url = "https://site.com/img/2.jpg"
    tmp_db.record_failed_url(tid, url, "http 500")
    dl = _FakeDL()          # 继续失败
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    assert out["still_failed"] == [url], out
    rows = tmp_db.list_failed_urls(tid)
    assert len(rows) == 1 and int(rows[0]["tries"]) == 2, rows


def test_retry_filtered_keyword_not_counted_failure(tmp_config, tmp_db,
                                                    fake_fetcher,
                                                    monkeypatch):
    """被关键字/黑名单/时段跳过 → filtered，不算失败也不划掉清单。"""
    tid = _mk_task(tmp_config, tmp_db, keywords=["cat"])
    _authorize(tmp_db)
    url = "https://site.com/img/dog.jpg"
    tmp_db.record_failed_url(tid, url, "http 500")
    dl = _FakeDL({url: DownloadOutcome(url=url, path=None, ok=True,
                                       sha256="c" * 64)})
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    # V0.0.6：手填的是「要找什么」，不是「网址里必须写什么」。网址里没有
    # 「别找」的词时不该在重试阶段被硬拦（旧口径会整站 0 图），放行去下载；
    # 真正该过滤的只有命中黑名单的那一条（另有用例覆盖）。
    assert out["succeeded"] == [url] and out["filtered"] == [], out
    assert out["still_failed"] == []
    assert dl.calls, "没命中不要的词就该真的发起下载"
    assert len(tmp_db.list_failed_urls(tid)) == 0, "重试成功后应划掉清单"


def test_retry_skipped_exists_counts_as_succeeded(tmp_config, tmp_db,
                                                  fake_fetcher,
                                                  monkeypatch):
    """库里已有（幂等跳过）也算成功：这条 URL 没必要再失败了。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    url = "https://site.com/img/3.jpg"
    tmp_db.record_failed_url(tid, url, "http 500")
    dl = _FakeDL({url: DownloadOutcome(url=url, path=None, ok=True,
                                       skipped=True)})
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    assert out["succeeded"] == [url], out
    assert tmp_db.list_failed_urls(tid) == []


def test_retry_internal_exception_counts_still_failed(tmp_config, tmp_db,
                                                      fake_fetcher,
                                                      monkeypatch):
    """★_process_image 内部抛异常（不是下载失败）→ 仍记失败，这条 URL
    绝不从清单里悄悄消失。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    url = "https://site.com/img/4.jpg"
    tmp_db.record_failed_url(tid, url, "http 500")

    class _Boom:
        async def download_one(self, *a, **k):
            raise RuntimeError("disk on fire")

    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, _Boom(),
                            monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    assert out["still_failed"] == [url], out
    rows = tmp_db.list_failed_urls(tid)
    assert len(rows) == 1 and int(rows[0]["tries"]) == 2, rows
    assert "disk on fire" in (rows[0]["error"] or ""), rows[0]["error"]


def test_retry_missing_task_returns_chinese_error(tmp_config, tmp_db,
                                                  fake_fetcher, monkeypatch):
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    dl = _FakeDL()
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(9999))
    assert out["ok"] is False
    assert "不存在" in out["error"], out


def test_retry_empty_list_returns_note(tmp_config, tmp_db, fake_fetcher,
                                       monkeypatch):
    """清单为空 / 子集不在清单里 → ok=True + retried=0 + note，不是报错。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    dl = _FakeDL()
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    assert out["ok"] is True and out["retried"] == 0, out
    assert out.get("note"), out
    assert dl.calls == []


def test_retry_subset_only_requested_urls(tmp_config, tmp_db, fake_fetcher,
                                          monkeypatch):
    """勾选子集重试：只跑勾中的，没勾的一条不碰。"""
    tid = _mk_task(tmp_config, tmp_db)
    _authorize(tmp_db)
    u1, u2, u3 = ("https://site.com/img/1.jpg",
                  "https://site.com/img/2.jpg",
                  "https://site.com/img/3.jpg")
    for u in (u1, u2, u3):
        tmp_db.record_failed_url(tid, u, "http 500")
    dl = _FakeDL()          # 全部继续失败
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid, [u2]))
    assert out["retried"] == 1, out
    assert dl.calls == [u2], dl.calls
    assert out["still_failed"] == [u2]
    # 没勾的还在清单里，且 tries 没被 +1
    left = {r["url"]: int(r["tries"]) for r in tmp_db.list_failed_urls(tid)}
    assert left == {u1: 1, u2: 2, u3: 1}, left


def test_retry_runs_without_compliance(tmp_config, tmp_db, fake_fetcher,
                                       monkeypatch):
    """2026-10-26 门禁删除后的守卫：无授权记录重试也必须照跑。"""
    tid = _mk_task(tmp_config, tmp_db)
    tmp_db.record_failed_url(tid, "https://site.com/img/1.jpg", "e")
    dl = _FakeDL()
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    assert "授权" not in str(out.get("error", "")), out


def test_retry_bad_adapter_name_rejected(tmp_config, tmp_db, fake_fetcher,
                                         monkeypatch):
    """任务配了不存在的/穿越的适配器名 → 中文拒绝，不进下载。"""
    tid = _mk_task(tmp_config, tmp_db, adapter_name="../evil")
    _authorize(tmp_db)
    tmp_db.record_failed_url(tid, "https://site.com/img/1.jpg", "e")
    dl = _FakeDL()
    eng = _eng_with_fake_dl(tmp_config, tmp_db, fake_fetcher, dl, monkeypatch)
    out = asyncio.run(eng.retry_failed_urls(tid))
    assert out["ok"] is False, out
    assert "适配器" in out["error"], out
    assert dl.calls == []


def test_retry_source_guard_reuses_process_image():
    """源码判据：重试走 _process_image（复用单 URL 下载+管线），
    不许在 retry 里另抄一套下载逻辑（两套口径迟早漂移）。"""
    src = (ROOT / "media_resource_manager" / "engine.py").read_text(
        encoding="utf-8")
    i = src.find("async def retry_failed_urls")
    assert i != -1
    block = src[i:i + 4200]
    assert "_process_image(" in block, "retry 没复用 _process_image"
    assert "_make_downloader(" in block, "retry 没走共用构造助手"
    assert "_resolve_proxy(" in block, "retry 没走共用代理解析"


# ==================== 4. queue 管线 ====================

def test_queue_retry_returns_none_when_task_running(tmp_config):
    """★任务正在跑 → None（两个 Engine 叠在一个任务上会互相踩）。"""
    import time as _t
    from media_resource_manager.scheduler.queue import TaskQueue
    q = TaskQueue(cfg=tmp_config, max_concurrent=1)
    q.start()
    try:
        assert q._ready.wait(2), "队列线程没起来"
        q.running[7] = _t.monotonic()
        assert q.retry_failed_urls(7) is None
        q.running.pop(7, None)
        # 不在跑就能提交（返回 Future）
        fut = q.retry_failed_urls(7)
        assert fut is not None
        try:
            fut.result(timeout=10)
        except Exception:
            pass       # 任务不存在只影响结果，不影响"提交成功"
    finally:
        try:
            q.stop()
        except Exception:
            pass


def test_queue_retry_returns_none_when_loop_not_started(tmp_config):
    """loop 没起（队列没 start）→ None，调用方给中文提示，不许抛。"""
    from media_resource_manager.scheduler.queue import TaskQueue
    q = TaskQueue(cfg=tmp_config, max_concurrent=1)
    assert q.retry_failed_urls(1) is None


def test_queue_retry_runs_and_emits_event(tmp_config, tmp_db, monkeypatch):
    """正常路径：跑 engine.retry_failed_urls → on_event("retry-urls")。"""
    from media_resource_manager.scheduler import queue as qmod
    from media_resource_manager.scheduler.queue import TaskQueue

    seen: list[tuple] = []

    class _FakeEngine:
        def __init__(self, cfg, db):
            self.db = db

        async def retry_failed_urls(self, task_id, urls=None):
            return {"ok": True, "retried": 2, "succeeded": ["u1", "u2"],
                    "still_failed": [], "filtered": []}

        def shutdown(self, wait=False):
            pass

    monkeypatch.setattr(qmod, "Engine", _FakeEngine)
    q = TaskQueue(cfg=tmp_config, max_concurrent=1,
                  on_event=lambda kind, tid, payload:
                  seen.append((kind, tid, payload)))
    q.start()
    try:
        assert q._ready.wait(2), "队列线程没起来"
        fut = q.retry_failed_urls(3, ["u1", "u2"])
        assert fut is not None
        out = fut.result(timeout=15)
        assert out["ok"] is True and out["retried"] == 2, out
        assert ("retry-urls", 3, out) in seen, seen
    finally:
        try:
            q.stop()
        except Exception:
            pass


def test_queue_retry_source_guard_semaphore():
    """源码判据：重试占 _sem 并发额度（不绕过 max_concurrent 另起一队）。"""
    src = (ROOT / "media_resource_manager" / "scheduler" / "queue.py"
           ).read_text(encoding="utf-8")
    i = src.find("async def _do_retry_failed_urls")
    assert i != -1
    block = src[i:i + 500]
    assert "_sem" in block, "重试没走并发额度"


# ==================== 5. webui API ====================

def _handler(db, queue=None):
    from media_resource_manager import webui
    h = object.__new__(webui._Handler)
    h.server = types.SimpleNamespace(db=db, queue=queue)
    return h


def test_api_failed_urls_lists_items(tmp_db):
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "http 500")
    tmp_db.record_failed_url(1, "https://s.com/2.jpg", "timeout")
    h = _handler(tmp_db)
    out = h._api_task_failed_urls(1)
    assert out["ok"] is True and out["task_id"] == 1
    urls = [it["url"] for it in out["items"]]
    assert urls == ["https://s.com/1.jpg", "https://s.com/2.jpg"]
    assert out["items"][0]["error"] == "http 500"
    assert out["items"][0]["tries"] == 1
    assert out["items"][0]["last_tried"]


def test_api_retry_urls_empty_list_error(tmp_db):
    """清单空 → 中文错（前端按钮此时应禁用/提示，不是打空拳）。"""
    h = _handler(tmp_db, queue=object())
    out = h._api_task_retry_urls({"task_id": 1})
    assert out["ok"] is False and "空" in out["error"], out


def test_api_retry_urls_bad_task_id(tmp_db):
    h = _handler(tmp_db, queue=object())
    assert h._api_task_retry_urls({"task_id": "abc"})["ok"] is False
    assert h._api_task_retry_urls({"task_id": 0})["ok"] is False
    assert h._api_task_retry_urls({"task_id": 1,
                                   "urls": "notalist"})["ok"] is False


def test_api_retry_urls_no_queue(tmp_db):
    """纯 API 模式没队列 → 中文错，不许 AttributeError。"""
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "e")
    h = _handler(tmp_db, queue=None)
    out = h._api_task_retry_urls({"task_id": 1})
    assert out["ok"] is False and "队列" in out["error"], out


def test_api_retry_urls_busy_none_future(tmp_db):
    """任务在跑（queue 返回 None）→ 中文提示先停止。"""
    class _BusyQ:
        def retry_failed_urls(self, task_id, urls=None):
            return None
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "e")
    h = _handler(tmp_db, queue=_BusyQ())
    out = h._api_task_retry_urls({"task_id": 1})
    assert out["ok"] is False and "运行" in out["error"], out


def test_api_retry_urls_pending_on_timeout(tmp_db):
    """重试超过 300s → pending=True（前端稍后刷新清单，不是报错）。"""
    class _SlowQ:
        def retry_failed_urls(self, task_id, urls=None):
            class _F:
                def result(self, timeout=None):
                    raise concurrent.futures.TimeoutError()
            return _F()
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "e")
    h = _handler(tmp_db, queue=_SlowQ())
    out = h._api_task_retry_urls({"task_id": 1})
    assert out["ok"] is True and out.get("pending") is True, out


def test_api_retry_urls_passes_through_result(tmp_db):
    """正常透传 engine 摘要（ok/retried/succeeded/still_failed/filtered）。"""
    _seed_tasks(tmp_db, 1)
    want = {"ok": True, "retried": 2, "succeeded": ["a"],
            "still_failed": ["b"], "filtered": []}

    class _Q:
        def __init__(self):
            self.got = None

        def retry_failed_urls(self, task_id, urls=None):
            self.got = (task_id, urls)

            class _F:
                def result(self, timeout=None):
                    return want
            return _F()
    q = _Q()
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "e")
    tmp_db.record_failed_url(1, "https://s.com/2.jpg", "e")
    h = _handler(tmp_db, queue=q)
    out = h._api_task_retry_urls({"task_id": 1, "urls": ["https://s.com/1.jpg"]})
    assert q.got == (1, ["https://s.com/1.jpg"]), q.got
    assert out["ok"] is True and out["succeeded"] == ["a"]
    assert out["still_failed"] == ["b"], out


def test_api_failed_urls_clear_works_and_audits(tmp_db):
    _seed_tasks(tmp_db, 1)
    tmp_db.record_failed_url(1, "https://s.com/1.jpg", "e")
    h = _handler(tmp_db)
    out = h._api_task_failed_urls_clear({"task_id": 1})
    assert out["ok"] is True, out
    assert tmp_db.list_failed_urls(1) == []
    rows = tmp_db.query(
        "SELECT actor, action, target FROM audit_log "
        "WHERE action='failed-urls-clear'")
    # V0.0.25 起来源拆成 client / browser（旧值 web 只留给历史记录）：
    # 这条测试没有 X-MRM-Surface 头 ⇒ 按「浏览器」记，别再钉死旧编码。
    assert rows and rows[0]["actor"] in ("browser", "client", "web") \
        and rows[0]["target"] == "1"
    # 坏输入中文拒绝
    assert h._api_task_failed_urls_clear({"task_id": "x"})["ok"] is False


def test_api_errors_are_chinese(tmp_db):
    """H 段铁律：用户可见错误必须中文。"""
    h = _handler(tmp_db, queue=None)
    cases = [
        h._api_task_retry_urls({"task_id": 1}),
        h._api_task_failed_urls_clear({"task_id": -1}),
    ]
    for r in cases:
        if r.get("ok") is False:
            err = r.get("error") or ""
            assert err and any("一" <= ch <= "鿿" for ch in err), err


def test_webui_routes_are_explicit_literals():
    """★路由护栏：三个端点必须逐个显式列出（test_r48 同款判据）——
    改成 f-string 循环展开后字面量消失，护栏会假红。"""
    src = (ROOT / "media_resource_manager" / "webui.py").read_text(
        encoding="utf-8")
    for ep in ('"/api/task/failed-urls"', '"/api/task/retry-urls"',
               '"/api/task/failed-urls/clear"'):
        assert ep in src, f"{ep} 没接路由（或改成了循环展开）"
