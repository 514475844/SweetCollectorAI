"""FIX_ALL_20261007 生产测试报告的回归护栏（桌面机 19:20 报告）。

来源：C:\\Users\\Admin\\Desktop\\Sweet采集AI版\\生产力测试报告.md
用户原话：「把这些全部修好以后，再重新拿到桌面上。」

覆盖：
  P0-1 #6  重试无效 + 静默吞异常（queue.retry_failed 用了不存在的 _pq）
  P0-2 #8  delay / ai_threshold 写库被 int() 吃掉精度（静默丢用户数据）
  P0-3 #1  AppConfig 缺 web_* 五字段，新用户开不了 Web + 端口可能随机
  P1-1「重试」只重试一张都没下到的任务，99% 404 反而标 DONE
  P1-2「新建要空白」被 _fill_form 偷偷填回上次目录
  P1-5 allowed_hours / rate_limit 数据层有、界面无入口
  P2-1 build_nodel.py 打包后处理必须自动补 gallery-dl.exe 与 data/rules

⚠ 本文件严禁 rmtree / shutil.rmtree —— 会触发 safe-delete 守卫，
把真失败和守卫噪音混在一起（MEMORY 铁律 8）。需要清理临时文件时用
os.unlink + os.rmdir 逐个删。
"""
from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ══════════════════════════════════════════════════════════
# P0-1  #6 retry_failed 必须真的入队，且不许静默吞异常
# ══════════════════════════════════════════════════════════

def _body_only(fn) -> str:
    """只返回函数体里的**可执行代码**（剥掉 docstring）。

    ⚠ 本项目大量关键信息写在 docstring 里，直接 inspect.getsource 全文 grep
    会把说明文字当成代码判违规。也不能只剥注释不剥 docstring（那是另一种
    大面积假失败）。这里按 AST 精确剥：只去掉第一个 Expr(Constant str)。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    fdef = tree.body[0]
    body = list(fdef.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return ast.unparse(ast.Module(body=body, type_ignores=[]))


def _make_queue(tmpdir):
    """造一个不启动事件循环的 TaskQueue —— retry_failed 必须在这种状态下也能工作。"""
    from media_resource_manager.config import AppConfig
    from media_resource_manager.scheduler.queue import TaskQueue

    cfg = AppConfig()
    cfg.db_path = os.path.join(tmpdir, "t.db")
    from media_resource_manager.db import Database
    Database(cfg.db_path).close()
    return TaskQueue(cfg)


def test_p0_1_retry_failed_actually_enqueues():
    """#6：failed 里的任务点「重试」必须真的进队列。

    上一轮实现是 self._pq.put_nowait(...)：TaskQueue._pq 只在 _main() 里
    首次创建（asyncio.PriorityQueue），事件循环没起来时**属性根本不存在**
    → 每次 AttributeError 被 `except Exception: pass` 吞掉 → 用户点了没反应。
    """
    with tempfile.TemporaryDirectory(prefix="p0_1_") as td:
        q = _make_queue(td)
        # 强制「事件循环未启动」这一最常见现场：_pq 不存在
        assert not hasattr(q, "_pq"), "本测试前提：_pq 应尚未创建"

        q.failed = {11: "boom", 22: "bang"}
        ok = q.retry_failed()

        assert ok == 2, f"应真实入队 2 条，实际 {ok}"
        assert len(q._pending) == 2, f"_pending 应有 2 条，实际 {len(q._pending)}"
        assert {tid for tid, _ in q._pending} == {11, 22}


def test_p0_1_retry_failed_never_swallows_exception():
    """不许再 `except Exception: pass` —— 入队失败必须留下日志。"""
    from media_resource_manager.scheduler.queue import TaskQueue
    src = _body_only(TaskQueue.retry_failed)
    assert "except Exception" in src, "应保留 except 分支以免整批重试崩掉"
    body = src.split("except Exception", 1)[1]
    # except 体第一句不能是 pass（那就是静默吞异常 = 原 bug）
    first = body.strip().splitlines()[0].strip()
    assert first != "pass", f"retry_failed 又把异常静默吞了：{first!r}"
    assert "LOG.warning" in body, "入队失败必须写日志，不能无声无息"


def test_p0_1_retry_failed_uses_public_api_not_private_pq():
    """必须走 submit/submit_many（线程安全），不许再直接捅 asyncio.PriorityQueue。"""
    from media_resource_manager.scheduler.queue import TaskQueue
    # 剥 docstring 再看（MEMORY：docstring 里大量写"旧实现是 _pq..."，
    # 直接 grep 源码会把说明文字当成违规代码）
    code = _body_only(TaskQueue.retry_failed)
    assert "_pq" not in code, \
        f"retry_failed 又直接用 _pq 了（loop 未起时该属性不存在）：{code}"
    assert "submit_many(" in code or "submit(" in code, \
        "必须走公开的 submit/submit_many"


def test_p0_1_failed_dict_keeps_unenqueued_ones():
    """入队失败的任务必须留在 failed 里，用户才能再点一次重试。"""
    from media_resource_manager.scheduler.queue import TaskQueue
    with tempfile.TemporaryDirectory(prefix="p0_1c_") as td:
        q = _make_queue(td)
        q.failed = {7: "boom"}
        # 制造一次入队失败：submit_many 直接抛异常
        def boom(items):
            raise RuntimeError("no loop")
        q.submit_many = boom
        assert q.retry_failed() == 0
        assert 7 in q.failed, "入队失败的任务被从 failed 里抹掉了 —— 用户再也点不到重试"


def test_p0_1_retry_failed_empty_is_zero_not_crash():
    with tempfile.TemporaryDirectory(prefix="p0_1b_") as td:
        q = _make_queue(td)
        assert q.retry_failed() == 0


def test_p0_1_caller_surfaces_zero_result():
    """调用方不许无视返回值：0 条要在 UI 上明确提示，不许静默。"""
    p = os.path.join(ROOT, "media_resource_manager", "gui", "main_window.py")
    src = open(p, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "retry_failed":
            fn = n
    assert fn is not None, "main_window 里找不到 retry_failed"
    body = ast.unparse(fn)
    assert "retry_failed()" in body
    assert "= self._queue.retry_failed()" in body or \
           "n = self._queue.retry_failed()" in body, \
        "main_window.retry_failed 必须接住返回值（n = ...）"


# ══════════════════════════════════════════════════════════
# P0-2  #8 delay / ai_threshold 写库不许丢精度
# ══════════════════════════════════════════════════════════

def test_p0_2_float_helper_exists_and_is_float():
    from media_resource_manager import repo
    assert hasattr(repo, "_flt"), "repo 缺少 float 版转换函数 _flt"
    assert repo._flt(2.75) == pytest.approx(2.75)
    assert repo._flt("0.42") == pytest.approx(0.42)
    assert repo._flt(None, 1.5) == pytest.approx(1.5)
    assert repo._flt("abc", 0.25) == pytest.approx(0.25)


def test_p0_2_delay_and_threshold_use_flt_not_num():
    """delay / ai_threshold 是 float 字段，四处（INSERT/UPDATE × 两字段）必须用 _flt。"""
    p = os.path.join(ROOT, "media_resource_manager", "repo.py")
    src = open(p, encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Name):
            continue
        fn = n.func.id
        if fn not in ("_num", "_flt"):
            continue
        arg = n.args[0] if n.args else None
        if arg is None:
            continue
        txt = ast.unparse(arg)
        if txt.startswith("t."):
            field = txt[2:]
            if field in ("delay", "ai_threshold") and fn == "_num":
                bad.append(field)
    assert not bad, f"这些 float 字段仍在用 _num（丢精度）：{bad}"


def test_p0_2_roundtrip_precision():
    """真跑一遍 save_task → get_task，断言小数原样回来。"""
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task, get_task
    from media_resource_manager.db import Database

    td = tempfile.mkdtemp(prefix="p0_2_")
    dbpath = os.path.join(td, "t.db")
    db = Database(dbpath)
    try:
        t = TaskConfig(id=None, adapter_name="generic", name="x",
                       entry_urls=["https://a.com/"])
        t.delay = 2.75
        t.ai_threshold = 0.42
        tid = save_task(db, t)
        t2 = get_task(db, tid)
        assert t2 is not None, "get_task 拿不到刚存的任务"
        assert abs(float(t2.delay) - 2.75) < 1e-6, \
            f"delay 精度被吃掉：{t2.delay!r}（应为 2.75）"
        assert abs(float(t2.ai_threshold) - 0.42) < 1e-6, \
            f"ai_threshold 精度被吃掉：{t2.ai_threshold!r}（应为 0.42）"

        # 再走一次 UPDATE 分支（L75-83 那一段）
        t2.delay = 1.25
        t2.ai_threshold = 0.87
        save_task(db, t2)
        t3 = get_task(db, t2.id if hasattr(t2, "id") else tid)
        assert abs(float(t3.delay) - 1.25) < 1e-6, f"UPDATE 也丢精度：{t3.delay!r}"
        assert abs(float(t3.ai_threshold) - 0.87) < 1e-6
    finally:
        db.close()
        for f in os.listdir(td):
            os.unlink(os.path.join(td, f))
        os.rmdir(td)


def test_p0_2_int_fields_still_use_num():
    """反护栏：真正的 int 字段不许被顺手改成 _flt（会掩盖整数校验）。"""
    p = os.path.join(ROOT, "media_resource_manager", "repo.py")
    tree = ast.parse(open(p, encoding="utf-8").read())
    must_be_num = {"concurrency", "max_depth", "priority", "min_width",
                   "min_height", "min_bytes", "max_bytes", "max_pages",
                   "rate_limit"}
    seen = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "_num" and n.args:
            seen.add(ast.unparse(n.args[0])[2:])
    missing = must_be_num - seen
    assert not missing, f"这些 int 字段不再走 _num：{missing}"


# ══════════════════════════════════════════════════════════
# P0-3  #1 AppConfig 必须补齐 web_* 五字段
# ══════════════════════════════════════════════════════════

def test_p0_3_appconfig_has_web_fields():
    import dataclasses
    from media_resource_manager.config import AppConfig
    names = {f.name for f in dataclasses.fields(AppConfig)}
    # ⚠ 字段名以**代码**为准：主窗口/设置对话框用的是 web_enabled（不是 web_enable）
    for f in ("web_enabled", "web_bind", "web_port", "web_user", "web_password"):
        assert f in names, f"AppConfig 缺字段 {f}"


def test_p0_3_load_config_empty_gives_sane_web_defaults():
    """删掉 config.json 首启 → web_port 必须是正整数、web_bind 非空。"""
    from media_resource_manager.config import load_config
    with tempfile.TemporaryDirectory(prefix="p0_3_") as td:
        cfg = load_config(Path(td) / "config.json")
        assert isinstance(cfg.web_port, int) and cfg.web_port > 0, \
            f"web_port={cfg.web_port!r} 不是正整数"
        assert int(cfg.web_port) > 0
        assert str(cfg.web_bind).strip() != "", "web_bind 为空"
        assert isinstance(cfg.web_enabled, bool)
        assert isinstance(cfg.web_user, str) and cfg.web_user


def test_p0_3_load_config_legacy_file_backfills():
    """老配置文件里没有 web_* 键也必须补上默认值，不许 None、不许崩。"""
    from media_resource_manager.config import load_config
    with tempfile.TemporaryDirectory(prefix="p0_3b_") as td:
        p = Path(td) / "config.json"
        with open(str(p), "w", encoding="utf-8") as f:
            f.write('{"max_concurrent": 3}')
        cfg = load_config(p)
        assert int(cfg.web_port) > 0, "老配置没补上 web_port 默认值"
        assert str(cfg.web_bind).strip() != ""


def test_p0_3_webui_never_binds_random_port():
    """port 必须是**确定的正整数**：None / "" / "abc" 一律落到明确默认端口。

    ⚠ 修正作业单的假设：`0` 是**刻意的哨兵值**（让 OS 选空闲端口，且
    start() 会把真实端口回写 cfg.web_port）—— 有测试依赖它，不能改成
    "0 也回落到默认"。真正要防的是 None / 空串 / 非数字：**绝不能让
    ThreadingHTTPServer 拿 None 去绑**（那会绑到随机端口，用户不知道网址）。
    """
    from media_resource_manager.webui import WebServer, DEFAULT_PORT

    class _C:
        web_bind = "127.0.0.1"
        web_password = "abcd"
        web_user = "admin"
        web_port = None

        def __getattr__(self, k):
            return None

    for bad in (None, "", "abc", "not-a-port"):
        _C.web_port = bad
        srv = WebServer.__new__(WebServer)
        srv.cfg = _C()
        got = srv.port
        assert isinstance(got, int) and got > 0, f"web_port={bad!r} → {got!r}，不是正整数"
        assert got == DEFAULT_PORT, f"web_port={bad!r} 应回落到 DEFAULT_PORT"

    # 正常值与哨兵 0 都必须原样透传（0 = 自动分配，不是错误）
    for ok_v, expect in ((8080, 8080), (9000, 9000), (0, 0)):
        _C.web_port = ok_v
        srv = WebServer.__new__(WebServer)
        srv.cfg = _C()
        assert srv.port == expect, f"web_port={ok_v!r} 应透传为 {expect}"

    # url 里的端口永远不是 0/None，否则用户抄到一个打不开的网址
    _C.web_port = None
    srv = WebServer.__new__(WebServer)
    srv.cfg = _C()
    assert "None" not in srv.url and ":0/" not in srv.url, srv.url


# ══════════════════════════════════════════════════════════
# P1-1  部分失败（99/100 404）也必须可重试
# ══════════════════════════════════════════════════════════

def test_p1_1_partial_failure_is_retryable():
    """100 张成 1 张、99 张 404 → 旧逻辑标 DONE，用户点重试没反应。"""
    from media_resource_manager.scheduler import queue as qmod
    src = inspect.getsource(qmod)
    assert "_fail_ratio" in src or "PARTIAL_FAIL_RATIO" in src, \
        "queue 里没有部分失败判定"
    assert "rep.downloaded == 0" in src, \
        "原判定应保留（一张都没下到仍是硬失败）"


def test_p1_1_partial_failure_visible_in_status():
    """即便不算 hard_error，「完成但有 N 张失败」也要写进状态/日志。"""
    from media_resource_manager.scheduler import queue as qmod
    src = inspect.getsource(qmod)
    assert "部分下载失败" in src, "部分失败没有可见提示"


def test_p1_1_threshold_is_module_constant_not_magic():
    """阈值必须是可推导的模块常量（MEMORY：断言关系不写死数值）。"""
    from media_resource_manager.scheduler import queue as qmod
    assert hasattr(qmod, "PARTIAL_FAIL_RATIO"), \
        "阈值应提为模块常量 PARTIAL_FAIL_RATIO，别散在表达式里"
    v = qmod.PARTIAL_FAIL_RATIO
    assert 0 < float(v) <= 1, f"阈值不合理：{v!r}"


# ══════════════════════════════════════════════════════════
# P1-2  「新建任务要空白」不许被偷偷填回上次目录
# ══════════════════════════════════════════════════════════

def test_p1_2_new_task_does_not_autofill_output_dir():
    p = os.path.join(ROOT, "media_resource_manager", "gui", "main_window.py")
    src = open(p, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == "_fill_form":
            fn = n
    assert fn is not None
    body = ast.unparse(fn)
    assert "last_output_dir" not in body, \
        "_fill_form 仍在读 cfg.last_output_dir —— 新建任务会被偷偷填回上次目录"


def test_p1_2_reuse_is_explicit_opt_in():
    """要保留便利就必须显式可点：默认不勾的「沿用上次目录」入口。"""
    p = os.path.join(ROOT, "media_resource_manager", "gui", "main_window.py")
    src = open(p, encoding="utf-8").read()
    assert "last_output_dir" in src, \
        "若要提供沿用入口，必须在别处显式引用 last_output_dir（默认不勾）"


# ══════════════════════════════════════════════════════════
# P1-5  allowed_hours / rate_limit 界面要有入口
# ══════════════════════════════════════════════════════════

def test_p1_5_task_form_has_hours_and_rate_controls():
    """新建/编辑任务对话框必须有「允许运行时间窗」和「速率限制」两个控件。"""
    found = {}
    for rel in ("gui/main_window.py", "gui/task_form.py"):
        p = os.path.join(ROOT, "media_resource_manager", rel)
        if not os.path.exists(p):
            continue
        src = open(p, encoding="utf-8").read()
        for key in ("allowed_hours", "rate_limit"):
            if key in src:
                found.setdefault(key, []).append(rel)
    for key in ("allowed_hours", "rate_limit"):
        assert key in found, f"{key} 在界面代码里完全没出现 —— 数据层有 UI 无入口"


def test_p1_5_controls_are_collected_and_refilled():
    """collect 里要收集、fill_form 里要回填（照 delay/ai_threshold 的写法）。"""
    src = open(os.path.join(ROOT, "media_resource_manager", "gui", "main_window.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    collect = ast.unparse(fns["_collect"]) if "_collect" in fns else ""
    fill = ast.unparse(fns["_fill_form"]) if "_fill_form" in fns else ""
    assert "allowed_hours" in collect, "_collect() 没收集 allowed_hours"
    assert "allowed_hours" in fill, "_fill_form() 没回填 allowed_hours"
    assert "rate_limit" in collect, "_collect() 没收集 rate_limit"
    assert "rate_limit" in fill, "_fill_form() 没回填 rate_limit"


# ══════════════════════════════════════════════════════════
# P2-1  打包后处理必须自动补 gallery-dl.exe 与 data/
# ══════════════════════════════════════════════════════════

def test_p2_1_build_nodel_postprocess_copies_gallery_dl():
    p = os.path.join(ROOT, "build_nodel.py")
    src = open(p, encoding="utf-8").read()
    ast.parse(src)                      # 语法自检
    assert "gallery-dl" in src, "build_nodel.py 完全没提 gallery-dl"
    # R105：不再硬编码字节数字面量（旧值 17977277 是坏 onedir 二进制，硬编码
    # 导致每次换真身都要手工同步、漏一处就假红/假绿）——改为从 build_nodel.py
    # 解析 GALLERY_DL_BYTES，与 vendor/ 真身实测字节数比对。
    import re as _re
    m = _re.search(r"GALLERY_DL_BYTES\s*=\s*(\d+)", src)
    assert m, "未找到 GALLERY_DL_BYTES 常量（防拷到半截文件）"
    vendor_gdl = os.path.join(ROOT, "vendor", "gallery-dl.exe")
    assert os.path.isfile(vendor_gdl), "vendor/gallery-dl.exe 缺失"
    assert int(m.group(1)) == os.path.getsize(vendor_gdl), \
        f"GALLERY_DL_BYTES={m.group(1)} 与 vendor 真身实测 " \
        f"{os.path.getsize(vendor_gdl)} 不符（拷到半截/旧版）"
    assert "vendor" in src, "gallery-dl 真身应放仓库 vendor/ 下，不许从 dist 现拷"


def test_p2_1_build_fails_loudly_when_copy_fails():
    """拷贝失败必须让 build 失败（rc≠0），不许只 warning 后继续。"""
    src = open(os.path.join(ROOT, "build_nodel.py"), encoding="utf-8").read()
    i = src.find("gallery-dl")
    seg = src[max(0, i - 1500):i + 2500]
    assert ("SystemExit" in seg or "sys.exit" in seg
            or "raise SystemExit" in seg), \
        "拷贝失败没有硬失败出口 —— 会静默交付一个缺 gallery-dl 的包"


def test_p2_1_vendor_dir_or_source_declared():
    """vendor 真身目录要么已建，要么脚本里有明确的期望路径常量。"""
    src = open(os.path.join(ROOT, "build_nodel.py"), encoding="utf-8").read()
    assert "GALLERY_DL_SRC" in src or "vendor/gallery-dl.exe" in src, \
        "需要明确的可信源常量（例如 GALLERY_DL_SRC）"


# ══════════════════════════════════════════════════════════
# P2-3  旧版 exe 别散在用户桌面
# ══════════════════════════════════════════════════════════

def test_p2_3_no_old_exe_scattered_on_desktop():
    d = os.path.expanduser("~/Desktop")
    if not os.path.isdir(d):
        pytest.skip("本机无 Desktop 目录（可能是另一台机器的作业单）")
    stray = [f for f in os.listdir(d)
             if f.endswith(".old.exe") or f.endswith(".prev.exe")]
    assert not stray, f"桌面散落旧版 exe，请挪到 dist/_旧版本/：{stray}"

# ══════════════════════════════════════════════════════════
# P1-2（补测）  沿用必须是显式可点、默认不勾
# ══════════════════════════════════════════════════════════

def _fn_src(fname: str) -> str:
    """按 AST 行号精确截取某个函数的完整源码（别用固定长度切片 ——
    函数头一动就够不到真正的代码，会假失败）。"""
    p = os.path.join(ROOT, "media_resource_manager", "gui", "main_window.py")
    lines = open(p, encoding="utf-8").read().splitlines(True)
    for node in ast.walk(ast.parse("".join(lines))):
        if isinstance(node, ast.FunctionDef) and node.name == fname:
            return "".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"main_window 里找不到 {fname}")


def test_p1_2_reuse_checkbox_default_unchecked():
    seg = _fn_src("_open_task_form")
    assert "_cb_reuse_last" in seg, "找不到「沿用上次目录」复选框"
    assert "setChecked(False)" in seg, \
        "沿用复选框必须默认不勾（静默填充的根源）"
    assert "沿用上次目录" in seg, "缺少人话标签"


def test_p1_2_new_task_output_dir_starts_empty():
    """新建对话框打开时 f_out 应为空 —— 除非用户主动点了「沿用」。

    ⚠ R47/B9：回填逻辑已从 `_open_task_form` 内部**搬进**
    `_on_reuse_last_toggled`（回调专用方法）。所以这条护栏改成
    「在**填 f_out 的那个函数**里，回填必须由 toggled 驱动」，
    而不再死钉「代码写在 _open_task_form 的第几行」——
    钉位置会把「搬家」误判成回归（这轮就踩了）。
    语义没变：新建路径不许无条件回填。
    """
    open_seg = _fn_src("_open_task_form")
    tog_seg = _fn_src("_on_reuse_last_toggled")

    # 回填只在 toggled 处理函数里，且由勾选状态决定
    assert "self.f_out.setText(last if on else \"\")" in tog_seg, \
        "回填必须在 _on_reuse_last_toggled 里按勾选状态走"
    # 新建路径本身**不许**出现无条件回填
    assert "self.f_out.setText(last)" not in open_seg, \
        "_open_task_form 里出现了无条件回填 —— 新建任务会被静默填回目录"
    # 勾选框必须默认不勾（静默填充的根源）
    assert "_cb_reuse_last" in open_seg, "找不到「沿用上次目录」复选框"
    assert "setChecked(False)" in open_seg, "沿用复选框必须默认不勾"
    # 新建路径必须先重置表单（B9：全字段清空）
    assert "_reset_form_for_new" in open_seg, \
        "新建任务没调 _reset_form_for_new —— 会残留上一个任务（B9）"


# ══════════════════════════════════════════════════════════
# P1-3  不支持站点的报错不许说黑话
# ══════════════════════════════════════════════════════════

def _unsupported_strings() -> list:
    """返回 engine.py 里 `if rule is None:` 那条报错分支里的**字符串字面量**。

    ⚠ 两个坑（都踩过）：
    ① 只能查字符串字面量 —— 代码注释里写这些词是为了解释「改了什么」，
       用户根本看不到；全源码 grep 会把注释判成违规。
    ② 不能按固定字符数切片 —— 分支很长，切片会跨到下一个函数体，
       parse 时直接 IndentationError。用 AST 走语句级定位。
    """
    p = os.path.join(ROOT, "media_resource_manager", "engine.py")
    src = open(p, encoding="utf-8").read()
    i = src.find("if rule is None:")
    assert i >= 0, "engine.py 里找不到 `if rule is None:` 分支"
    line_no = src[:i].count("\n") + 1
    tree = ast.parse(src)
    branch = None
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and node.lineno == line_no:
            branch = node
            break
    assert branch is not None, "按行号没定位到那个 if 分支"
    return [n.value for n in ast.walk(branch)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_p1_3_error_message_is_plain_language():
    lits = _unsupported_strings()
    blob = "\n".join(lits)
    # 2026-10-28（文案专业化）：原句「这个网址我们还不会抓」改为书面表述，
    # 判据跟着换 —— 语义不变：**第一句先给结论**，不堆术语。
    assert "该网址暂无可用抓取规则" in blob, "报错应先给出结论（第一句说清是什么问题）"
    for jargon in ("gallery-dl", "适配器规则", "mrm check", "examples/"):
        assert jargon not in blob, f"给用户看的报错里仍出现黑话：{jargon}"


def test_p1_3_error_points_to_autogen_entry():
    """报错必须指向那个真实存在的入口「自动生成规则」。"""
    blob = "\n".join(_unsupported_strings())
    assert "分析这个网址 / 自动生成规则" in blob, \
        "报错没指向「自动生成规则」这个可点入口"
    # 入口必须真的存在（别写一个不存在的按钮）
    mw = open(os.path.join(ROOT, "media_resource_manager", "gui", "main_window.py"),
              encoding="utf-8").read()
    assert "分析这个网址 / 自动生成规则" in mw, \
        "报错推荐的入口在界面上并不存在 —— 那是骗人的指引"


def test_p1_3_site_count_claim_is_true():
    """文案里说「16 个站点」就得真有 16 份规则，不许编数字。"""
    rd = os.path.join(ROOT, "data", "rules")
    n = len([f for f in os.listdir(rd)
             if f.endswith(".yaml") or f.endswith(".yml")])
    blob = "\n".join(_unsupported_strings())
    if "内置支持" in blob:
        import re
        m = re.search(r"内置支持\s*(\d+)\s*个站点", blob)
        assert m, "提到站点数但写不出数字"
        assert int(m.group(1)) == n, \
            f"文案说 {m.group(1)} 个站点，data/rules 实有 {n} 份规则"


# ══════════════════════════════════════════════════════════
# P1-4  皮肤：自绘控件也必须跟随（调色板 + 切换后强制重绘）
# ══════════════════════════════════════════════════════════

def test_p1_4_palette_has_all_semantic_keys():
    """调色板必须给足语义色位，且明暗两套都齐（缺一个就会有控件回退到字面量）。"""
    from media_resource_manager.gui.theme import PALETTE
    need = {"bg", "surface", "surface_alt", "border", "text", "text_dim",
            "on_accent"}
    for mode in ("dark", "light"):
        assert mode in PALETTE, f"PALETTE 缺 {mode}"
        missing = need - set(PALETTE[mode])
        assert not missing, f"{mode} 调色板缺：{missing}"


def test_p1_4_palette_follows_current_skin():
    """换皮肤 → 强调色必须跟着换（这就是 P1-4 的核心诉求）。"""
    from media_resource_manager.gui import theme
    before = theme.palette(False)["accent"]
    theme._CURRENT_SKIN = "teal"
    after = theme.palette(False)["accent"]
    assert after != before, "换皮肤后自绘强调色没变 —— 皮肤仍是半成品"
    assert after == theme.SKINS["teal"][0], "强调色没对上所选皮肤"
    theme._CURRENT_SKIN = "violet"          # 还原，别污染别的用例


def test_p1_4_c_helper_never_raises():
    """c() 取不到键必须给fallback，不许 KeyError 把绘制路径炸了。"""
    from media_resource_manager.gui.theme import c
    assert c(True, "accent")
    assert c(False, "no_such_key", "#123456") == "#123456"
    assert c(True, "no_such_key")


def test_p1_4_custom_draw_uses_palette_not_literals():
    """自绘处（迷你折线图）不许再写死色值。

    ⚠ 断言的是「颜色从调色板来」这个**关系**，不是某个字面量的有无 ——
    c(dark, "accent", "#8b7cf8") 里那个 fallback 是兜底默认值（只有调色板
    缺键时才用），不是写死配色。按字面量断言会逼着人删掉正当的兜底。
    """
    seg = _fn_src("_draw_chart")
    assert '_tc(dark, "accent"' in seg or 'c(dark, "accent"' in seg, \
        "折线图线色没从调色板取 —— 换皮肤不变色"
    assert '_tc(dark, "surface_alt"' in seg or 'c(dark, "surface_alt"' in seg, \
        "折线图底色没从调色板取"
    # 原来那两个「只有 violet 皮肤才对」的字面量不许以独立三元形式出现
    assert '"#9d8cff" if dark else "#8b7cf8"' not in seg, \
        "折线图线色仍是写死的双主题三元组 —— 换皮肤不变色"
    assert '"#121426" if dark else "#f4f5fa"' not in seg, "折线图底色仍是写死三元组"


def test_p1_4_theme_repolishes_visible_widgets():
    """切皮肤后必须强制已显示控件重绘（否则自绘缓存旧色）。"""
    src = open(os.path.join(ROOT, "media_resource_manager", "gui", "theme.py"),
               encoding="utf-8").read()
    seg = src[src.find("def apply_theme("):]
    for tok in ("unpolish", "polish", "update()"):
        assert tok in seg, f"apply_theme 缺 {tok} —— 切皮肤后已显示控件不重绘"


def test_p1_4_palette_values_match_qss_semantics():
    """反护栏：调色板值必须与 QSS 里同义色一致，别自成一套（改一处忘一处）。"""
    from media_resource_manager.gui.theme import PALETTE, DARK_QSS, LIGHT_QSS
    assert PALETTE["dark"]["surface"] in DARK_QSS, \
        "深色 surface 与 QSS 里的卡片底不一致"
    assert PALETTE["light"]["surface"] in LIGHT_QSS, \
        "浅色 surface 与 QSS 里的卡片底不一致"
    assert PALETTE["dark"]["text"] in DARK_QSS, "深色文字色与 QSS 不一致"
    assert PALETTE["light"]["text"] in LIGHT_QSS, "浅色文字色与 QSS 不一致"


def test_p2_1_rules_land_in_data_rules_subdir():
    """规则必须落 data/rules/，不是 data/ 根下。

    ⚠ 本轮真踩：`_deploy_rules(src, _deliver / "data")` 把 16 份 yaml 直接
    铺在了 data/ 根下，而程序读的是 data/rules/ → 交付件里「有规则但读不到」，
    比缺规则更难查。断言的是**路径关系**（结尾必须是 / "rules"）。
    """
    src = open(os.path.join(ROOT, "build_nodel.py"), encoding="utf-8").read()
    # 找**调用点**而不是定义（定义那行是 `def _deploy_rules(src_rules: ...`）
    hits = [ln for ln in src.splitlines()
            if "_deploy_rules(" in ln and not ln.strip().startswith("def ")]
    assert hits, "build_nodel 里找不到 _deploy_rules 调用"
    seg = hits[0].strip()
    assert seg.endswith('"data" / "rules")'), \
        f"_deploy_rules 目标目录不是 data/rules/：{seg!r}"
