"""R218 棒护栏：R152 C 组（C2 / C3 / C5 / C6 / C7 / C9）。

行为断言优先、源码判据只做**兜底**（每条行为断言都对应一次变异自检，
见 .workbuddy/_r218_mutate_c.py）。

覆盖：
- C2：gallery-dl 流式下载静默看门狗（iter_stdout_lines 超时 / 正常 EOF /
      download_iter 真杀卡死的子进程 / 正常流式仍能出图）；
- C3：discovery 自己炸了也必须给 worker 发停工哨兵（drain_workers 行为）；
- C5：收尾扫盘只认本轮新文件（历史文件不再被当成"新下载"重处理）；
- C6：黑名单合并点（任务词 ∪ 全局词）覆盖快路径 / 追溯 / 回填三处；
- C7：EXIF 必须在 os.replace **之前**读（拍摄时间不再恒丢）；
- C9：失败率过高默认只告警、不自动暂停整条队列（开关打开才暂停）。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager.backends import gallery_dl as gd
from media_resource_manager.config import AppConfig
from media_resource_manager.engine import (
    apply_blacklist_existing,
    drain_workers,
    merged_blacklist,
)

# 全文件统一的安全阀：任何"真挂住"的用例也不许拖死整轮全量
_HARD_LIMIT = 20.0


def _code(text: str) -> str:
    """剥掉行注释再判（R73 铁律：判据不许被注释/文档串骗到）。

    本仓修 bug 时爱把"以前怎么写"写进注释，注释里因此满是被禁的字面量 ——
    不剥注释的判据要么假红、要么（更糟）删掉注释就永远绿。
    """
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def _run_with_watchdog(fn, limit: float = _HARD_LIMIT):
    """在子线程里跑 `fn`，超时即判失败（**绝不**让卡死变成整轮超时）。"""
    box = {}

    def _t():
        try:
            box["val"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=_t, daemon=True)
    th.start()
    th.join(limit)
    assert not th.is_alive(), f"看门狗触发：{fn} 超过 {limit}s 未返回（疑似挂起）"
    if "err" in box:
        raise box["err"]
    return box.get("val")


# ══════════════════ C2：流式下载的静默看门狗 ══════════════════

class _HungStream:
    """永不吐字的 stdout：模拟 gallery-dl 卡死（不 EOF、不输出）。"""

    def __iter__(self):
        while True:
            time.sleep(0.05)


class _LineStream:
    """吐完几行就 EOF 的 stdout。"""

    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        for ln in self._lines:
            yield ln


class _FakeProc:
    def __init__(self, stream):
        self.stdout = stream
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        raise subprocess.TimeoutExpired("gallery-dl", timeout or 0)


def test_c2_iter_stdout_lines_stops_when_silent():
    """静默超过 idle_timeout 秒必须收工（不再无限等子进程）。"""
    proc = _FakeProc(_HungStream())
    st: dict = {}

    def _go():
        return list(gd.iter_stdout_lines(proc, 0.3, st))

    t0 = time.monotonic()
    out = _run_with_watchdog(_go, limit=10)
    cost = time.monotonic() - t0

    assert out == []
    assert st.get("idle") is True, "静默超时必须标记 idle（调用方据此杀进程）"
    assert st.get("eof") is False
    assert cost < 8.0, f"看门狗没生效：等了 {cost:.1f}s"


def test_c2_iter_stdout_lines_yields_all_until_eof():
    """正常 EOF：一行不丢，且**不**被误判成静默超时。"""
    proc = _FakeProc(_LineStream(["a\n", "b\n", "c\n"]))
    st: dict = {}
    out = _run_with_watchdog(lambda: list(gd.iter_stdout_lines(proc, 5, st)))
    assert out == ["a\n", "b\n", "c\n"]
    assert st.get("eof") is True
    assert st.get("idle") is False


def _mk_backend(dest, idle_timeout=0.3):
    be = gd.GalleryDLBackend(dest_root=Path(dest), idle_timeout=idle_timeout)
    return be


def test_c2_download_iter_kills_hung_process(monkeypatch, tmp_path):
    """卡死的子进程必须被 terminate/kill，并给用户留中文说明。"""
    dest = tmp_path / "dest"
    dest.mkdir()
    be = _mk_backend(dest, idle_timeout=0.3)

    held = {}

    def _spawn(cmd, **kw):
        p = _FakeProc(_HungStream())
        held["proc"] = p
        return p

    monkeypatch.setattr(gd, "installed", lambda: True, raising=True)
    monkeypatch.setattr(gd.GalleryDLBackend, "_build_cmd",
                        lambda self, url, d, extra=None: (["fake"], tmp_path / "cfg.json"))
    monkeypatch.setattr(gd.childproc, "spawn_hidden", _spawn, raising=True)
    monkeypatch.setattr(gd.childproc, "unregister_child", lambda p: None, raising=True)

    out = _run_with_watchdog(lambda: list(be.download_iter("https://x.test/a")))

    p = held.get("proc")
    assert p is not None, "子进程根本没起来"
    assert out == [], "卡死时不应再产出文件"
    assert p.terminated is True, "C2 回归：卡死的子进程没被 terminate"
    assert p.killed is True, "terminate 不理（wait 超时）后必须补 kill"
    assert "卡死" in (be.notice or ""), f"用户看不到原因：notice={be.notice!r}"


def test_c2_download_iter_still_streams_new_files(monkeypatch, tmp_path):
    """正常流式：stdout 里出现的真实文件照旧 yield（看门狗不许误伤）。"""
    dest = tmp_path / "dest2"
    dest.mkdir()
    f = dest / "pic.jpg"
    f.write_bytes(b"\xff\xd8\xff\xd9")

    be = _mk_backend(dest, idle_timeout=5)

    monkeypatch.setattr(gd, "installed", lambda: True, raising=True)
    monkeypatch.setattr(gd.GalleryDLBackend, "_build_cmd",
                        lambda self, url, d, extra=None: (["fake"], tmp_path / "c.json"))
    monkeypatch.setattr(gd.childproc, "spawn_hidden",
                        lambda cmd, **kw: _FakeProc(_LineStream([f"{f}\n"])),
                        raising=True)
    monkeypatch.setattr(gd.childproc, "unregister_child", lambda p: None, raising=True)

    out = _run_with_watchdog(lambda: list(be.download_iter("https://x.test/b")))

    assert [p.name for p in out] == ["pic.jpg"]
    assert be.notice == "", "正常跑完不该留卡死提示"


# ══════════════════ C5：扫盘只认本轮新文件 ══════════════════

def test_c5_sweep_skips_files_older_than_run(tmp_path):
    """历史文件（mtime 早于本轮开始）不得再被当成新下载。"""
    old = tmp_path / "old.jpg"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 86400, time.time() - 86400))
    new = tmp_path / "new.jpg"
    new.write_bytes(b"y")

    since = time.time() - 2.0
    got = [p.name for p in gd.sweep_new_files(tmp_path, set(), since)]
    assert "new.jpg" in got
    assert "old.jpg" not in got, "C5 回归：重跑又把历史文件当新下载处理了"


def test_c5_download_iter_does_not_reprocess_history(monkeypatch, tmp_path):
    """端到端口径：子进程一声不吭就退出时，历史文件也不许被 yield 出来。"""
    dest = tmp_path / "dest3"
    dest.mkdir()
    old = dest / "history.jpg"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 86400, time.time() - 86400))

    be = _mk_backend(dest, idle_timeout=5)
    monkeypatch.setattr(gd, "installed", lambda: True, raising=True)
    monkeypatch.setattr(gd.GalleryDLBackend, "_build_cmd",
                        lambda self, url, d, extra=None: (["fake"], tmp_path / "c.json"))
    monkeypatch.setattr(gd.childproc, "spawn_hidden",
                        lambda cmd, **kw: _FakeProc(_LineStream([])), raising=True)
    monkeypatch.setattr(gd.childproc, "unregister_child", lambda p: None, raising=True)

    out = _run_with_watchdog(lambda: list(be.download_iter("https://x.test/c")))
    assert out == [], f"历史文件被当成新下载：{[p.name for p in out]}"


# ══════════════════ C3：discovery 炸了不许漏 worker ══════════════════

def test_c3_drain_workers_always_stops_them():
    """哨兵 + 收工：worker 自己不会退，drain 之后必须全部 done。"""
    async def _main():
        q: asyncio.Queue = asyncio.Queue()
        exits: list[str] = []

        async def worker():
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if item is None:
                    exits.append("sentinel")     # 只有收到哨兵才会退
                    return
                exits.append(str(item))
        workers = [asyncio.create_task(worker()) for _ in range(3)]
        await asyncio.sleep(0.02)
        await drain_workers(workers, q)
        return len(exits)

    # ⚠ 判据必须看「worker 是不是靠哨兵退的」，不能只看 task.done()：
    # drain_workers 会吞掉外层取消（设计如此），一旦 worker 是被取消带走的，
    # task 同样是 done —— 那样即使哨兵没发，判据也照样绿（M5 当初就这么漏的）。
    n = _run_with_watchdog(lambda: asyncio.run(asyncio.wait_for(_main(), 15)), limit=25)
    assert n == 3, f"C3 回归：只有 {n}/3 个 worker 是收到停工哨兵后退的"


def test_c3_engine_uses_finally_to_drain():
    """源码兜底：engine.run 里 discovery 的收工必须写在 finally 里。"""
    src = _code((ROOT / "media_resource_manager" / "engine.py").read_text(encoding="utf-8"))
    i = src.index("discovery_run = asyncio.create_task")
    block = src[i:i + 4000]
    assert "except Exception" in block, "discovery 的普通异常没人接（worker 会漏）"
    assert "finally:" in block, "收工没放进 finally ⇒ 异常路径照样漏 worker"
    assert "drain_workers(workers, queue)" in block
    # finally 块必须真的包住收工调用（不是写在异常分支里）
    fin = block[block.index("finally:"):]
    assert "drain_workers(workers, queue)" in fin


# ══════════════════ C6：黑名单合并（任务词 ∪ 全局词）══════════════════

def test_c6_merged_blacklist_union():
    cfg = AppConfig(global_blacklist=["g1", "g2"])
    task = types.SimpleNamespace(blacklist=["t1"])
    got = merged_blacklist(task, cfg)
    assert set(got) == {"t1", "g1", "g2"}
    # 顺序稳定：任务词在前（三处调用点的语义都要求"任务优先"）
    assert got[0] == "t1"


def test_c6_apply_blacklist_existing_honours_global(tmp_db, tmp_config):
    """追溯应用：只写在**全局**黑名单里的词，也必须拦住已入库的图。"""
    tmp_db.execute(
        "INSERT INTO images(id, task_id, source_url, path, tags) "
        "VALUES (1, 1, 'https://x.test/globalbad.jpg', 'C:/x.jpg', '[]')")
    task = types.SimpleNamespace(id=1, blacklist=[])
    tmp_config.global_blacklist = ["globalbad"]

    n = apply_blacklist_existing(tmp_db, task, cfg=tmp_config)

    assert n == 1, "C6 回归：全局黑名单对追溯应用无效"
    row = tmp_db.query("SELECT blacklisted, needs_review FROM images WHERE id=1")[0]
    assert row["blacklisted"] == 1 and row["needs_review"] == 1


def test_c6_backfill_merges_global_blacklist(tmp_db, tmp_config, monkeypatch):
    """本地回填：全局词也要拦（老目录里的历史图同样适用）。"""
    from media_resource_manager.engine import backfill_local_files
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task

    out = tmp_config.library_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "globalbad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    t = TaskConfig(id=None, name="bf", adapter_name="", output_dir=str(out),
                   blacklist=[], entry_urls=["file:///" + str(out)])
    tid = save_task(tmp_db, t)
    tmp_config.global_blacklist = ["globalbad"]

    # 回填里 merged_blacklist(t) 走"当前进程生效配置"，这里显式登记
    from media_resource_manager import config as cfgmod
    monkeypatch.setattr(cfgmod, "_ACTIVE_CFG", tmp_config, raising=False)

    backfill_local_files(tmp_db, tid)

    rows = tmp_db.query(
        "SELECT blacklisted, in_quarantine FROM images WHERE task_id=?", (tid,))
    assert rows, "回填一条都没写进去"
    assert any(r["blacklisted"] == 1 for r in rows), \
        "C6 回归：回填没用合并后的黑名单（全局词漏了）"


def test_c6_fastpath_site_uses_merged_rules():
    """源码兜底：engine 里**任何**一处判黑名单都不许再单拿 task.blacklist。

    判整个文件的代码（已剥注释）而不是某一行的锚点：三处调用点分散在
    run()/apply_blacklist_existing()/backfill_local_files()，按单个锚点查，
    补一处漏两处 —— 这正是 C6 当初漏成三个洞的原因。
    """
    # AST 而不是文本搜索：docstring 里也写着这段历史（判据被文档骗过的坑已经踩过一次）
    import ast

    tree = ast.parse(
        (ROOT / "media_resource_manager" / "engine.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_hit_blacklist"]
    assert len(calls) >= 4, f"_hit_blacklist 调用点变少了（判据失效？）：{len(calls)}"
    for c in calls:
        blacklist_arg = ast.unparse(c.args[2])
        # 回填那一支走局部变量（black = merged_blacklist(t)），所以两个变量名都禁
        assert blacklist_arg not in ("task.blacklist", "t.blacklist"), \
            f"第 {c.lineno} 行仍单拿任务自己的黑名单（全局词在这条路径上无效）"
    # 反方向：合并点必须真的被用到（防止有人干脆绕开合并逻辑）
    assert "merged_blacklist(task, self.cfg)" in ast.unparse(tree)


def test_c6_engine_blacklist_rules_delegates_to_merge():
    src = (ROOT / "media_resource_manager" / "engine.py").read_text(encoding="utf-8")
    i = src.index("def _blacklist_rules(self, task)")
    block = src[i:i + 900]
    assert "merged_blacklist(task, self.cfg)" in block, "合并点被绕开（两处逻辑会漂移）"


# ══════════════════ C7：EXIF 不能恒丢 ══════════════════

def test_c7_exif_is_read_before_replace():
    """源码兜底（顺序即语义）：image_exif(part) 必须排在 os.replace 之前。"""
    src = _code((ROOT / "media_resource_manager" / "downloader" /
                 "downloader.py").read_text(encoding="utf-8"))
    i = src.index("sha = sha256_bytes(part.read_bytes())")
    block = src[i:i + 900]
    i_exif = block.index("image_exif(part)")
    i_repl = block.index("os.replace(part, final)")
    assert i_exif < i_repl, \
        "C7 回归：EXIF 又排到 os.replace 之后（replace 一走 part 就没了）"


_JPEG_1X1 = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffc0000b080001000101011100ffc40014"
    "0001000000000000000000000000000000009fffc4001410010000000000000000"
    "00000000000000000000ffda0008010100003f00d2cf20ffd9")


class _BytesFetcher:
    """只吐固定字节的假取流器（不碰网络）。"""

    def __init__(self, data=_JPEG_1X1):
        self.data = data
        self.cfg = AppConfig(retry=0, min_free_mb=0, min_width=0,
                             min_height=0, min_bytes=0)

    async def fetch_bytes(self, url, delay=0, range_start=0):
        return 200, self.data


def test_c7_exif_survives_into_outcome(monkeypatch, tmp_path):
    """行为本质：EXIF 在读的那一刻文件还在 ⇒ 拍摄时间真的落进 outcome.detail。

    旧顺序（replace 之后才读）下探针看到的是 `exists=False`（part 已被
    改名搬走），detail 里的 taken_at 恒为 null —— 图库「按时间排序」就是
    全表乱序。这条同时钉死"顺序"和"结果"两头。
    """
    import media_resource_manager.downloader.downloader as dlmod

    seen = {}

    def _probe_exif(path):
        seen["path"] = str(path)
        seen["exists"] = Path(path).exists()
        return ("JPEG", "2024:05:06 07:08:09")

    monkeypatch.setattr(dlmod, "image_exif", _probe_exif, raising=True)

    async def _go():
        d = dlmod.Downloader(_BytesFetcher(), tmp_path)
        return await d.download_one("https://x.test/pic.jpg", site="s")

    out = _run_with_watchdog(lambda: asyncio.run(_go()), limit=15)

    assert out.ok, f"下载没成功，探针无意义：{out.reason}"
    assert seen.get("exists") is True, \
        f"C7 回归：读 EXIF 时文件已不在原路径（{seen.get('path')}）"
    assert "2024:05:06" in (out.detail or ""), \
        f"拍摄时间没落进 detail（图库拿不到）：{out.detail!r}"


# ══════════════════ C9：失败率过高默认只告警 ══════════════════

def _mk_queue(cfg):
    from media_resource_manager.scheduler.queue import TaskQueue
    q = TaskQueue(cfg, max_concurrent=1)
    return q


def _mk_rep(failed=10, found=0):
    return types.SimpleNamespace(failed=failed, images_found=found,
                                 errors=["http 403"])


def test_c9_high_failure_ratio_only_warns_by_default(tmp_config):
    """默认：失败率 ≥60% 只发告警事件，**不**自动暂停队列。"""
    q = _mk_queue(tmp_config)
    paused = []
    events = []
    q.pause = lambda reason="": paused.append(reason)
    q.on_event = lambda *a, **k: events.append(a)

    q._maybe_auto_throttle(1, _mk_rep(), None)

    assert paused == [], "C9 回归：默认又擅自暂停了整条队列"
    assert any(a[0] == "auto_throttle" for a in events), "告警事件没发出去"
    msg = " ".join(str(a[-1]) for a in events if a[0] == "auto_throttle")
    assert "队列继续运行" in msg, f"告警文案没说清队列没停：{msg}"


def test_c9_opt_in_switch_still_pauses(tmp_config):
    """用户显式打开开关 → 行为照旧（真暂停）。"""
    tmp_config.auto_pause_on_failure = True
    q = _mk_queue(tmp_config)
    paused = []
    q.pause = lambda reason="": paused.append(reason)

    q._maybe_auto_throttle(2, _mk_rep(), None)

    assert paused and "失败率" in paused[0], "开关打开后应当照旧自动暂停"


def test_c9_config_default_is_off():
    """安全默认：新配置里这个开关必须是关的（不能悄悄替用户做重决定）。"""
    assert AppConfig().auto_pause_on_failure is False
