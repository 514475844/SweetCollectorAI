"""R78 · Web 两项：#26 任务进度（本次 / 累计张数）/ #38 失败原因白话中文 + 日志。

作业单位置：TONIGHT_FINAL_ORDER 主单第 26 / 38 项，
PRIORITY_TONIGHT_ORDER §2「Web 剩余 7 项」里剩下的四个中的两个。

**为什么这两项放一轮**：它们落在同一条链上（`/api/tasks` → `_taskRow`），
一次改、一次验，闭环最小；拆开做等于把同一个接口改两遍。

四层判据（铁律 R73：判据现在是绿的不算数，必须变异一次确认它真会红）：
  L1 **node 真执行** —— 真 require 前端 app.js 跑纯函数，问的是**行为**；
  L2 **Python 纯函数** —— `plain_reason` 真翻译，且**非空结果必含中文**；
  L3 **源码接线** —— 纯函数必须被**真的调用**（跳过 docstring，防注释骗到）；
  L4 **真 HTTP** —— 真起 WebServer + 真 socket，断言 got / total / error / errors。

⚠ 三个「不做就白做」的点：
  1. 「本次」和「累计」**必须同时断言且互不相等**才有意义 ——
     只断言其中一个，把 total 抄给 got 也能全绿（同型坑见 R72）。
  2. 队列没挂上时 `reports` 取空 dict，接口**不能**报错（纯 API 模式）。
  3. 判据与被护对象**同源**：L1/L4 走的都是磁盘上那一份文件。
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager import webui                      # noqa: E402
from media_resource_manager.config import AppConfig           # noqa: E402
from media_resource_manager.db import Database                # noqa: E402

WEBUI_PY = ROOT / "media_resource_manager" / "webui.py"
JS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
CSS = ROOT / "media_resource_manager" / "web" / "static" / "css" / "app.css"
STUB = ROOT / "tools" / "r77_dom_stub.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="本机没装 node ⇒ 跳过纯 JS 行为层（不是产品问题）")

CJK = r"[一-龥]"


# ══════════════════════════ 工具 ══════════════════════════

def _read(p: Path) -> str:
    """统一按 LF 读（源码树里混着 CRLF 文件，LF 字面量做匹配会 count=0）。"""
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _py_func_src(name: str) -> str:
    """取出 Python 函数体（**去掉 docstring** 后 unparse）。

    ⚠ 为什么必须剥 docstring：R69 已实测 `ast.unparse(整个函数)` 会把
    docstring 一起打包，而 docstring 里大量引用「以前错在哪」的字面量
    ⇒ 判据被自己的注释骗过（变异也抓不到）。
    """
    tree = ast.parse(_read(WEBUI_PY))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            body = list(n.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return ast.unparse(ast.Module(body=body, type_ignores=[]))
    raise AssertionError(f"webui.py 里找不到函数 {name}")


def _js_func(name: str) -> str:
    """按**花括号配平**切出 JS 函数体（不靠行号、不靠缩进）。"""
    src = _read(JS)
    key = "function " + name + "("
    i = src.find(key)
    assert i >= 0, f"app.js 里找不到 {name}"
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"{name} 花括号没配平")


def _js(body: str):
    """在 node 里真 require 前端 app.js，执行 body 并返回 JSON 结果。

    ⚠ DOM 桩用 tools/r77_dom_stub.js（与 R77 同一份）——
      两处各写一份会静默失效（R63-D3「判据与被护对象分家」同型）。
    """
    assert NODE, "没有 node"
    script = (
        "require(%s).install();\n"
        "var m = require(%s);\n"
        "var out = (function () { %s })();\n"
        "process.stdout.write(JSON.stringify(out));\n"
        % (json.dumps(str(STUB)), json.dumps(str(JS)), body)
    )
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    assert p.returncode == 0, (
        f"node 执行失败 rc={p.returncode}\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}")
    return json.loads(p.stdout)


# ═══════════════ L1 · node 真执行（前端行为层） ═══════════════

# 2026-10-29：清旧界面用例时这段常量被误删（它紧跟在一个被删函数后面），
# 从首次提交里原样取回 —— 「错误文案与桌面端同一句」这组判据仍然有效。
_ZH_CASES = [
    ("fetch failed: All connection attempts failed", "站点无法连接"),
    ("HTTPSConnectionPool: Read timed out", "连接超时"),
    ("HTTP 404 Not Found", "页面不存在"),
    ("429 Too Many Requests", "请求太频繁"),
    ("403 Forbidden", "403"),
    ("robots.txt disallow", "robots"),
    ("需要 cookie 才能访问", "cookie"),
    ("No space left on device", "磁盘"),
]

@pytest.mark.parametrize("raw,zh", _ZH_CASES)
def test_known_errors_get_the_desktop_wording(raw, zh):
    """★ 认得出来的错误必须跟桌面端**同一句**（不另写一套翻译）。"""
    from media_resource_manager.stats import short_reason
    assert webui.plain_reason(raw) == short_reason(raw)
    assert zh in webui.plain_reason(raw)


def test_unknown_english_error_still_shows_chinese():
    """★ 认不出来也不能甩一行英文给用户（手机竖屏 = 等于没说）。

    同时**原文照带**：用户要反馈时还得靠它，不能为了「好看」把证据删了。
    """
    raw = "ValueError: something exploded in module xyz"
    out = webui.plain_reason(raw)
    assert "something exploded" in out, out
    assert "任务出错" in out, out


def test_empty_error_is_empty_string():
    assert webui.plain_reason("") == ""
    assert webui.plain_reason(None) == ""


@pytest.mark.parametrize("raw", [
    "ValueError: something exploded in module xyz",
    "fetch failed: All connection attempts failed",
    "未产出图：该页没有符合要求的图片",
])
def test_every_non_empty_result_contains_chinese(raw):
    """一条铁律直接钉死：用户可见错误**必须**有中文（不能全是英文）。"""
    import re
    out = webui.plain_reason(raw)
    assert out, out
    assert re.search(CJK, out), f"没有中文：{out!r}"


# ═══════════════ L3 · 源码接线层 ═══════════════

def test_api_tasks_really_reads_both_sources():
    """★ `_api_tasks` 必须**真的**读两个来源（各写一份口径是灾难）。"""
    src = _py_func_src("_api_tasks")
    assert "task_progress" in src, "累计张数没走 taskio.task_progress"
    assert "reports" in src, "本次张数没读队列报告"
    assert "\"got\"" in src or "'got'" in src, "没有 got 字段"
    assert "\"total\"" in src or "'total'" in src, "没有 total 字段"


def test_api_tasks_keeps_error_raw_for_the_title():
    """翻译了就把原文丢了 = 用户反馈时无从下手，所以 error_raw 必须留。"""
    src = _py_func_src("_api_tasks")
    assert "error_raw" in src
    assert "plain_reason" in src

class _Queue:
    """替身队列：只提供 reports，不真跑任务（不碰网络）。"""

    def __init__(self):
        self.reports: dict = {}

    def start_task(self, tid):
        pass

    def stop_task(self, tid):
        pass


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def raw(self, path, body=None, method=None):
        url = self.base + path
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(
            url, data=data, headers=hdrs, method=method
            or ("POST" if body is not None else "GET"))
        try:
            with self.opener.open(req, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def jget(self, path):
        st, raw = self.raw(path)
        try:
            return st, json.loads(raw.decode("utf-8"))
        except Exception:
            return st, None

    def jpost(self, path, body=None):
        st, raw = self.raw(path, body if body is not None else {})
        try:
            return st, json.loads(raw.decode("utf-8"))
        except Exception:
            return st, None


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """真起一个 WebServer（模块粒度：起一次服务跑完全部用例）。

    ⚠ 与 R50 同一套注意事项：端口 0 让 OS 选；`web_lan_open=False`
    才会真走鉴权；`save_config` 必须还原（R67 实测：这个 monkeypatch
    不还原会污染别的测试文件）。
    """
    tmp = tmp_path_factory.mktemp("r78_live")
    db = Database(tmp / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp / "mrm.db"
    cfg.rules_dir = tmp / "rules"
    cfg.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.web_password = "r78pw"
    cfg.web_lan_open = False          # ⚠ 不关掉 ⇒ 127.0.0.1 恒免密（R58）
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_user = "admin"

    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    q = _Queue()
    srv = webui.WebServer(cfg, db, queue=q)
    ok, msg = srv.start()
    assert ok, f"WebServer 起不来：{msg}"
    client = Client(f"http://127.0.0.1:{cfg.web_port}")
    st, body = client.jpost("/api/login", {"password": "r78pw"})
    assert st == 200 and body.get("ok"), f"登录失败：{st} {body}"

    yield {"client": client, "db": db, "queue": q, "tmp": tmp}

    try:
        srv.stop()
    finally:
        cfgmod.save_config = orig_save
        try:
            db.close()
        except Exception:
            pass


def _mk_task(c, name):
    st, body = c.jpost("/api/task/save", {
        "name": name, "entry_urls": "https://example.com/" + name,
        "adapter_name": "", "concurrency": 1, "max_depth": 1,
        "delay": 0, "priority": 5, "ai_enabled": False})
    assert st == 200 and body.get("ok"), body
    return int(body["task_id"])


def _mk_images(db, tid, n, *, quarantine=0, base=0):
    """真往库里插 n 行图片记录（路径非空 = 已落盘）。

    ⚠ `base` 是给「同任务再插一批」用的：`(task_id, source_url)` 是
    **UNIQUE 联合键**，第二次从头编号会直接 IntegrityError。
    """
    for i in range(n):
        db.execute(
            "INSERT INTO images(task_id, source_url, path, in_quarantine) "
            "VALUES (?,?,?,?)",
            (tid, f"https://example.com/{tid}/{base + i}.jpg",
             f"/tmp/{tid}_{base + i}.jpg", quarantine))


def test_tasks_api_really_reports_two_different_counts(live):
    """★★ #26 主验收：本次 7 / 累计 3，两个都在，且**互不相等**。

    「互不相等」这条不是凑数：只断言其中一个，把 total 抄给 got 也能全绿。
    """
    c, db, q = live["client"], live["db"], live["queue"]
    tid = _mk_task(c, "R78进度")
    _mk_images(db, tid, 3)
    q.reports[tid] = {"downloaded": 7, "images_found": 10, "failed": 2,
                      "errors": []}
    st, rows = c.jget("/api/tasks")
    assert st == 200 and isinstance(rows, list), (st, rows)
    row = [r for r in rows if int(r["id"]) == tid]
    assert row, f"列表里没有刚建的任务：{[r['id'] for r in rows]}"
    r = row[0]
    assert r["got"] == 7, r
    assert r["total"] == 3, r
    assert r["got"] != r["total"], "两个张数成了同一个数 ⇒ 口径被抄成一份了"


def test_cumulative_count_excludes_quarantined(live):
    """累计**只数还在库里的**（隔离 / 拉黑不算）—— 与 exe 批量导出同一口径。"""
    c, db, q = live["client"], live["db"], live["queue"]
    tid = _mk_task(c, "R78隔离")
    _mk_images(db, tid, 4)
    _mk_images(db, tid, 6, quarantine=1, base=100)
    st, rows = c.jget("/api/tasks")
    assert st == 200
    r = [x for x in rows if int(x["id"]) == tid][0]
    assert r["total"] == 4, f"隔离的 6 张被算进累计了：{r}"


def test_task_without_report_gets_zero_for_this_run(live):
    """没跑过的任务：本次 = 0，累计照常 —— 0 不是 bug，是「还没跑」。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R78未跑")
    _mk_images(db, tid, 2)
    st, rows = c.jget("/api/tasks")
    assert st == 200
    r = [x for x in rows if int(x["id"]) == tid][0]
    assert r["got"] == 0, r
    assert r["total"] == 2, r


def test_failure_reason_is_plain_chinese_over_http(live):
    """★ #38 主验收：库里存的是英文异常，接口吐出来的是一句人话。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R78报错")
    db.execute("UPDATE tasks SET status='error', last_error=? WHERE id=?",
               ("fetch failed: https://x.com/: All connection attempts failed",
                tid))
    st, rows = c.jget("/api/tasks")
    assert st == 200
    r = [x for x in rows if int(x["id"]) == tid][0]
    assert "站点无法连接" in r["error"], r
    assert "All connection attempts failed" in r["error_raw"], r


def test_report_errors_are_delivered_as_chinese_list(live):
    """本次运行的逐条报错也要中文（errors 数组），不是只翻 last_error。"""
    c, q = live["client"], live["queue"]
    tid = _mk_task(c, "R78日志")
    q.reports[tid] = {"downloaded": 1, "images_found": 2, "failed": 1,
                      "errors": ["Read timed out",
                                 "weird failure αβγ"]}
    st, rows = c.jget("/api/tasks")
    assert st == 200
    r = [x for x in rows if int(x["id"]) == tid][0]
    assert r["errors"], r
    assert "连接超时" in r["errors"][0], r
    assert any("任务出错" in e for e in r["errors"]), r


def test_tasks_api_survives_a_missing_queue(tmp_path):
    """队列没挂上（纯 API 模式）也要能出列表 —— reports 取空 dict，不许 500。"""
    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"
    cfg.rules_dir = tmp_path / "rules"
    cfg.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.web_password = "x"
    cfg.web_lan_open = False
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_user = "admin"
    import media_resource_manager.config as cfgmod
    orig = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None
    srv = webui.WebServer(cfg, db, queue=None)
    try:
        ok, msg = srv.start()
        assert ok, msg
        cli = Client(f"http://127.0.0.1:{cfg.web_port}")
        st, body = cli.jpost("/api/login", {"password": "x"})
        assert st == 200 and body.get("ok"), (st, body)
        st, rows = cli.jget("/api/tasks")
        assert st == 200, (st, rows)
        assert isinstance(rows, list), rows
        for r in rows:
            assert r.get("got") == 0, r
            assert isinstance(r.get("total"), int), r
    finally:
        cfgmod.save_config = orig
        try:
            srv.stop()
        except Exception:
            pass
        try:
            db.close()
        except Exception:
            pass
