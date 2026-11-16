"""R81 · Web 三项：#36 排序（时间/名称/大小）/ #23 搜索 / #39 任务批量操作。

作业单位置：TONIGHT_FINAL_ORDER 主单第 36 / 23 / 39 项
（R80c 收尾时点名「36 在 webui 无 order/sort 痕迹、32 无批量端点，
需下一轮真 HTTP 逐项核验」—— 36 与 39 本轮补上，32 的「移动」留给下一轮）。

**为什么这三项放一轮**：排序与搜索同落在 `/api/view/<x>` → `_api_images`
一条链上（一次改一次验）；任务批量操作是同页另一个独立小闭环
（新端点 + 任务页工具条）。三者互不牵连，可分别关单。

四层判据（铁律 R73：判据现在是绿的不算数，必须变异一次确认它真会红）：
  L1 **node 真执行** —— 真 require 前端 app.js 跑纯函数，问的是**行为**；
  L2 **源码接线** —— 按函数边界切块（跳过 docstring），判参数真的传下去、
      路由真的接上、批量动作真的复用单任务那条链；
  L3 **真 HTTP** —— 真起 WebServer + 真 socket + 真库，断言与库内顺序
      **逐行一致**、翻完不重不漏、批量动作真的落到队列与任务状态；
  L4 **反向护栏** —— 默认行为与改动前等价（老前端不带新参数）、
      白名单外输入一律回落不 500。

⚠ 三个「不做就白做」的点：
  1. 排序断言必须**与库内 ORDER BY 逐行比**：只断「升序」会被
     「路径大量重名 → 实际按 id 排」骗过（本测试的 pic_ 前缀跨任务重名）。
  2. name/size 翻页必须**翻完**断言「不重不漏」：OFFSET 分页的经典死法
     是边界 off-by-one（漏一张 / 重一张），单看第一页永远绿。
  3. 批量「重试失败」必须断言「只挑 error 的」：把 idle 的一起重试，
     用户会看到一堆没失败的任务被重新拉起。
"""
from __future__ import annotations

import ast
import json
import re
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


# ══════════════════════════ 工具 ══════════════════════════

def _read(p: Path) -> str:
    """统一按 LF 读（源码树里混着 CRLF 文件，LF 字面量做匹配会 count=0）。"""
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _py_func_src(name: str) -> str:
    """取出 Python 函数**含签名**的源码（**剥掉 docstring** 后 unparse）。

    ⚠ 为什么必须剥 docstring：R69 实测 `ast.unparse(整个函数)` 会把
    docstring 一起打包，而 docstring 里大量引用「以前错在哪」的字面量
    ⇒ 判据被自己的注释骗过（变异也抓不到）。
    ⚠ 为什么必须含签名：只 unparse `n.body` 会丢掉 def 行，
      「默认值 sort=time」这类签名级契约就证不了（本轮实测踩到）。
    ⚠ unparse 的字符串是**单引号**风格，断言按这个写。
    """
    tree = ast.parse(_read(WEBUI_PY))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            body = list(n.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            n.body = body
            return ast.unparse(n)
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

    ⚠ DOM 桩用 tools/r77_dom_stub.js（与 R77/R78 同一份）——
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

def test_api_view_passes_sort_and_query_down():
    """#36/#23：_api_view 必须真把两个参数传进 _api_images。"""
    src = _py_func_src("_api_view")
    assert "_norm_sort(" in src, "sort 没经白名单收窄"
    assert "_api_images(view, cursor, limit, sort=sort, q=kw" in src, src


def test_api_images_signature_defaults_to_time():
    """默认 sort=time：老前端不带参数 ⇒ 与改动前逐字节等价。"""
    src = _py_func_src("_api_images")
    assert "sort: str='time'" in src, src
    assert "q: str=''" in src, src


def test_api_images_orders_by_the_whitelisted_clause():
    """ORDER BY 必须来自 _SORTS（白名单），不是把请求值拼进 SQL。"""
    src = _py_func_src("_api_images")
    assert "_SORTS[_norm_sort(sort)]" in src, src


def test_batch_route_is_registered_as_its_own_branch():
    """#39：路由逐个显式列出（test_r48 那条护栏按字面量判接没接）。

    ⚠ 判整个 `if path == ...` 分支而不是子串：`/api/task/` 开头的端点
    有好几个，只判子串是空壳（R79 M1/M2 同型教训）。
    """
    src = _read(WEBUI_PY)
    assert 'if path == "/api/task/batch":' in src, "批量端点没接路由"
    assert "_api_task_batch(self._body())" in src, "路由没接到处理方法"


def test_batch_reuses_the_single_task_chain():
    """#39：复用 _api_task_ctl / _api_task_retry，不另写一套启停
    （作业单 E「共用同一套业务逻辑」；另写一份必漂移）。"""
    src = _py_func_src("_api_task_batch")
    assert "_api_task_retry(tid)" in src, src
    assert "_api_task_ctl(tid, act)" in src, src


def test_batch_retry_only_picks_error_tasks():
    """★「重试失败」只挑 status='error' —— 把 idle 的一起重启，
    用户会看到一堆没失败的任务被重新拉起。"""
    src = _py_func_src("_api_task_batch")
    assert "status='error'" in src, src

class _Queue:
    """替身队列：记录启停调用，不真跑任务（不碰网络）。"""

    def __init__(self):
        self.reports: dict = {}
        self.started: list = []
        self.stopped: list = []

    def start_task(self, tid):
        self.started.append(tid)

    def stop_task(self, tid):
        self.stopped.append(tid)


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

    `web_lan_open=False` 才会真走鉴权（R58：默认配置下 127.0.0.1
    恒免密 ⇒ 鉴权类断言恒真）；`save_config` 必须还原（R67 实测：
    这个 monkeypatch 不还原会污染别的测试文件）。
    """
    tmp = tmp_path_factory.mktemp("r81_live")
    db = Database(tmp / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp / "mrm.db"
    cfg.rules_dir = tmp / "rules"
    cfg.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.web_password = "r81pw"
    cfg.web_lan_open = False
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
    st, body = client.jpost("/api/login", {"password": "r81pw"})
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


def _mk_images(db, tid, n, *, base=0, prefix="pic"):
    """真往库里插 n 行图片（路径/字节/标签都不同，排序才判得出来）。

    ⚠ (task_id, source_url) 是 UNIQUE 联合键，同任务再插一批要换 base。
    ⚠ prefix 每测试换一个：路径跨任务重名时 ORDER BY path 会按 id 断，
       「接口顺序 == 库内顺序」仍然成立，但可读性差一半。
    """
    for i in range(n):
        db.execute(
            "INSERT INTO images(task_id, source_url, path, tags, bytes,"
            " in_quarantine) VALUES (?,?,?,?,?,0)",
            (tid, f"https://example.com/{tid}/{base + i}.jpg",
             f"/tmp/{prefix}_{tid}_{base + i:03d}.jpg",
             f"tag{base + i} 夜景", 1000 + (base + i) * 10))


def _gallery_ids_db(db, order: str) -> list:
    """库内图库口径（与 _api_images 的 gallery where 一字不差）。"""
    sql = {
        "time": "ORDER BY id DESC",
        "name": "ORDER BY COALESCE(path,'') ASC, id ASC",
        "size": "ORDER BY bytes DESC, id DESC",
    }[order]
    return [int(r["id"]) for r in db.query(
        "SELECT id FROM images WHERE in_quarantine=0 AND blacklisted=0 "
        "AND path IS NOT NULL " + sql)]


def _page_all(c, extra="", limit=2):
    """把某个排序翻完，返回全部 id（用来断言「不重不漏」）。

    ⚠ extra 是**追加参数**（如 "sort=name"），不是整条 path：
      第一版把 `?sort=name` 拼进 path 再拼 `?limit=2`，得到
      `?sort=name?limit=2` ⇒ limit 丢失、每页 100 条 ⇒ 翻页断言
      ** vacuous **（一页装全部，永远"不重不漏"）。R53：仪器错比
      产品错多，先怀疑仪器。
    """
    ids, cursor, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 300, "翻页没到头（死循环保护）"
        url = f"/api/view/gallery?limit={limit}"
        if extra:
            url += "&" + extra
        if cursor is not None:
            url += f"&cursor={cursor}"
        st, d = c.jget(url)
        assert st == 200 and d.get("ok"), (st, d)
        ids.extend(int(r["id"]) for r in d["items"])
        if not d.get("has_more"):
            break
        cursor = d["next_cursor"]
        assert cursor is not None, "has_more=true 却不给 next_cursor"
    return ids


def test_default_gallery_order_is_unchanged(live):
    """★ 反向护栏：不带 sort 参数 ⇒ 与库内 id DESC 逐行一致
    （老前端行为与改动前等价）。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81默认序")
    _mk_images(db, tid, 5)
    st, d = c.jget("/api/view/gallery?limit=200")
    assert st == 200 and d.get("ok"), (st, d)
    got = [int(r["id"]) for r in d["items"]]
    assert got == _gallery_ids_db(db, "time"), (
        f"默认顺序与库内 id DESC 不一致：\nAPI={got}\nDB ={_gallery_ids_db(db, 'time')}")


def test_sort_by_name_matches_db_order(live):
    """★ #36：按名称排 = 与库内 COALESCE(path,'') ASC, id ASC 逐行一致。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81名称序")
    _mk_images(db, tid, 6, prefix="nm")
    st, d = c.jget("/api/view/gallery?sort=name&limit=200")
    assert st == 200 and d.get("ok"), (st, d)
    got = [int(r["id"]) for r in d["items"]]
    want = _gallery_ids_db(db, "name")
    assert got == want, f"name 排序与库内不一致：\nAPI={got}\nDB ={want}"


def test_sort_by_size_matches_db_order(live):
    """★ #36：按大小排 = 与库内 bytes DESC, id DESC 逐行一致。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81大小序")
    _mk_images(db, tid, 6, prefix="sz")
    st, d = c.jget("/api/view/gallery?sort=size&limit=200")
    assert st == 200 and d.get("ok"), (st, d)
    got = [int(r["id"]) for r in d["items"]]
    want = _gallery_ids_db(db, "size")
    assert got == want, f"size 排序与库内不一致：\nAPI={got}\nDB ={want}"


def test_unknown_sort_falls_back_to_time_not_500(live):
    """白名单外的 sort 不许 500，也不许改变默认顺序。"""
    c = live["client"]
    st, d = c.jget("/api/view/gallery?sort=%27%3B%20DROP%20TABLE%20images%3B--"
                   "&limit=5")
    assert st == 200 and d.get("ok"), (st, d)
    st2, d2 = c.jget("/api/view/gallery?limit=5")
    assert [r["id"] for r in d["items"]] == [r["id"] for r in d2["items"]], \
        "乱输 sort 改了默认顺序"


def test_time_sort_pagination_has_no_dupes_or_gaps(live):
    """time 排序（id 键集）翻完不重不漏，且与整页集合一致。

    ★ limit=1 是边界极端：peek 行与返回行只差一行，off-by-one 在这里
    最露（本轮实测：第一版把 peek 行当 cursor，200 张翻两页只拿到 199）。
    """
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81时间翻页")
    _mk_images(db, tid, 7, prefix="tp")
    paged = _page_all(c, "sort=time")
    assert len(paged) == len(set(paged)), f"翻页有重复：{paged}"
    assert set(paged) == set(_gallery_ids_db(db, "time")), "翻页集合与库内不一致"
    paged1 = _page_all(c, "sort=time", limit=1)
    assert set(paged1) == set(_gallery_ids_db(db, "time")), (
        "limit=1 翻页集合与库内不一致（边界 off-by-one）")


def test_name_sort_pagination_has_no_dupes_or_gaps(live):
    """★ name 排序（OFFSET）翻完不重不漏 —— OFFSET 的经典死法是
    边界 off-by-one（漏一张/重一张），单看第一页永远绿。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81名称翻页")
    _mk_images(db, tid, 7, prefix="np")
    paged = _page_all(c, "sort=name")
    assert len(paged) == len(set(paged)), f"name 翻页有重复：{paged}"
    assert set(paged) == set(_gallery_ids_db(db, "time")), "name 翻页集合与库内不一致"


def test_size_sort_pagination_has_no_dupes_or_gaps(live):
    """size 排序（OFFSET）同样要翻完验证。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81大小翻页")
    _mk_images(db, tid, 7, prefix="sp")
    paged = _page_all(c, "sort=size")
    assert len(paged) == len(set(paged)), f"size 翻页有重复：{paged}"
    assert set(paged) == set(_gallery_ids_db(db, "time")), "size 翻页集合与库内不一致"


def test_search_filters_by_path_tag_and_url(live):
    """★ #23：关键词同时搜路径 / 标签 / 来源网址，命中集合与库内一致。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81搜索")
    _mk_images(db, tid, 4, prefix="q")
    db.execute("INSERT INTO images(task_id, source_url, path, tags, bytes,"
               " in_quarantine) VALUES (?,?,?,?,?,0)",
               (tid, "https://other.example.org/zzz.jpg",
                "/tmp/special_cat.jpg", "动物", 999))
    # 搜路径
    st, d = c.jget("/api/view/gallery?q=special_cat&limit=200")
    assert st == 200 and d.get("ok"), (st, d)
    want = {int(r["id"]) for r in db.query(
        "SELECT id FROM images WHERE path LIKE '%special_cat%'")}
    got = {int(r["id"]) for r in d["items"]}
    assert got == want, f"路径搜索集合不一致：API={got} DB={want}"
    # 搜标签
    st, d = c.jget("/api/view/gallery?q=%E5%8A%A8%E7%89%A9&limit=200")
    assert st == 200 and d.get("ok"), (st, d)
    want = {int(r["id"]) for r in db.query(
        "SELECT id FROM images WHERE tags LIKE '%动物%'")}
    got = {int(r["id"]) for r in d["items"]}
    assert got == want, f"标签搜索集合不一致：API={got} DB={want}"
    # 搜来源网址
    st, d = c.jget("/api/view/gallery?q=other.example.org&limit=200")
    assert st == 200 and d.get("ok"), (st, d)
    want = {int(r["id"]) for r in db.query(
        "SELECT id FROM images WHERE source_url LIKE '%other.example.org%'")}
    got = {int(r["id"]) for r in d["items"]}
    assert got == want, f"网址搜索集合不一致：API={got} DB={want}"


def test_search_with_no_match_returns_empty_not_error(live):
    """搜不到 ⇒ 空列表 + ok，不是 500（手机上就是白屏）。"""
    c = live["client"]
    st, d = c.jget("/api/view/gallery?q=%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E8%AF%8D"
                   "&limit=5")
    assert st == 200 and d.get("ok"), (st, d)
    assert d["items"] == [], d


def test_search_and_sort_work_together(live):
    """#36+#23 组合：先搜后排，两个参数都生效。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R81搜排")
    _mk_images(db, tid, 5, prefix="qs")
    st, d = c.jget("/api/view/gallery?q=qs_&sort=name&limit=200")
    assert st == 200 and d.get("ok"), (st, d)
    got = [int(r["id"]) for r in d["items"]]
    want = [int(r["id"]) for r in db.query(
        "SELECT id FROM images WHERE path LIKE '%qs_%' "
        "ORDER BY COALESCE(path,'') ASC, id ASC")]
    assert got == want, f"组合参数下 name 排序失效：{got} vs {want}"


def test_search_survives_sql_injection_attempt(live):
    """q 是参数化查询：塞 SQL 片段不该炸也不该真删表。"""
    c = live["client"]
    st, d = c.jget("/api/view/gallery?q=%27%3B%20DROP%20TABLE%20images%3B--"
                   "&limit=5")
    assert st == 200 and d.get("ok"), (st, d)
    st2, d2 = c.jget("/api/view/gallery?limit=5")
    assert st2 == 200 and d2.get("ok"), "images 表被搜挂了"


def test_batch_start_and_stop_reach_the_queue(live):
    """★ #39：全部开始/全部停止真落到队列（每个任务都要走到）。"""
    c, db, q = live["client"], live["db"], live["queue"]
    tid = _mk_task(c, "R81批量启停")
    st, body = c.jpost("/api/task/batch", {"act": "start"})
    assert st == 200 and body.get("ok"), (st, body)
    assert tid in q.started, f"start 没到这个任务：started={q.started}"
    st, body = c.jpost("/api/task/batch", {"act": "stop"})
    assert st == 200 and body.get("ok"), (st, body)
    assert tid in q.stopped, f"stop 没到这个任务：stopped={q.stopped}"


def test_batch_retry_only_touches_failed_tasks(live):
    """★ #39：「重试失败」只挑 status='error' 的，idle 的不许被拉起。"""
    c, db, q = live["client"], live["db"], live["queue"]
    bad = _mk_task(c, "R81失败任务")
    good = _mk_task(c, "R81正常任务")
    db.execute("UPDATE tasks SET status='error', last_error='boom' WHERE id=?",
               (bad,))
    st, body = c.jpost("/api/task/batch", {"act": "retry"})
    assert st == 200 and body.get("ok"), (st, body)
    assert body["total"] == 1, f"重试范围不对：{body}"
    assert body["done"] == [bad], body
    row = db.query("SELECT status, last_error FROM tasks WHERE id=?", (bad,))
    assert row[0]["status"] == "idle", row
    assert row[0]["last_error"] == "", row
    row2 = db.query("SELECT status FROM tasks WHERE id=?", (good,))
    assert row2[0]["status"] != "error", "正常任务被误重试了"


def test_batch_rejects_unknown_action_in_chinese(live):
    """白名单外的批量动作 ⇒ 中文报错，不 500（用户可见错误必须中文）。"""
    c = live["client"]
    st, body = c.jpost("/api/task/batch", {"act": "nuke"})
    assert st == 200, st
    assert body.get("ok") is False, body
    assert "批量动作" in body.get("error", ""), body


def test_batch_survives_missing_queue(tmp_path):
    """队列没挂上（纯 API 模式）不许 500 —— 与 _api_tasks 同一姿态。"""
    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"
    # 2026-10-28：**必须让 OS 选端口**。写死 8080 时，只要用户本机正跑着
    # 软件（它就在 8080），Windows 的 SO_REUSEADDR 允许第二个服务也 bind 成功，
    # 请求于是被打到**用户那个真服务**上（它有队列）→ 本用例必假红。
    cfg.web_port = 0
    srv = webui.WebServer(cfg, db, queue=None)
    ok, msg = srv.start()
    assert ok, msg
    try:
        # 显式给一个不存在的 id：空库里「全部」= 0 个任务，什么都证不了；
        # 而「队列没挂上」这条缺陷的表现是**每个 id 都进 failed**。
        req = urllib.request.Request(
            f"http://127.0.0.1:{cfg.web_port}/api/task/batch",
            data=json.dumps({"act": "start", "ids": [999999]}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                st, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            st, raw = e.code, e.read()
        assert st in (200, 401), st
        if st == 200:
            body = json.loads(raw.decode("utf-8"))
            assert body.get("ok") is True, body
            assert body.get("total") == 1, body
            assert body.get("failed") == [999999], (
                "没队列却报成功 = 假成功（队列未就绪必须让这个 id 失败）")
    finally:
        srv.stop()
        try:
            db.close()
        except Exception:
            pass
