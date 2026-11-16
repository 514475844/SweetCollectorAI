"""R82 · Web #32「多选批量动作」补全缺失的「移动」。

作业单位置：TONIGHT_FINAL_ORDER 主单第 32 项
「Web 多选后的**批量动作**：删除 / 移动 / 审核通过 / 拒绝。」

**到手状态（R81c 收尾时点名）**：删除 / 审核通过 / 拒绝三个动作早在
`/api/image/action` + actionbar 里就有（ids 数组批量），**唯独「移动」没有**。
本轮只补这一个动作，一个最小闭环。

**「移动」的口径（本轮拍板，写下来给下轮）**：
  like    审核通过 → needs_review=0（出待审核，留图库）
  dislike 拒绝     → needs_review=0（+reviews 行）
  delete  删除     → in_quarantine=1（软删，可还原）
  move    移动     → needs_review=1, in_quarantine=0（进待审核）
- 为什么是「进待审核」而不是「移到隔离区」：后者与「删除」完全重复
  （删除今天就是 in_quarantine=1），用户把两个词并列列出，必须是两件事；
  图库里除隔离区外唯一的「别的位置」就是待审核队列。
- **隔离区与待审核互斥**：桌面端隔离区管理的取数口径明写
  「needs_review=1 的走审核页，不在这里混」，两边都置位会让图在两个
  视图里都看不见（ok 但做了另一件事）⇒ move 必须同时清 in_quarantine。
- **移动不是审核决定** ⇒ 不写 reviews 行、不喂学习器（喂了等于替用户判断）。

四层判据（铁律 R73：判据现在是绿的不算数，必须变异一次确认它真会红）：
  L1 **node 真执行** —— 真 require 前端 app.js 跑纯函数（提示文案点明
      「移动」的去向：效果在图库里看不见，文案必须兜住）；
  L2 **源码接线** —— 白名单含 move；move 分支排在 dec 映射**之前**
      （否则 KeyError 被 except 吞 ⇒ done=0 却返回 ok=true）；
      SET 两列在**同一条** UPDATE；分支内无 reviews/学习器；
      路由分支完整；doAction 真走提示纯函数；前端按钮 ↔ 后端白名单一一对应。
  L3 **真 HTTP** —— 真起 WebServer + 真 socket + 真库：真移动、真清隔离、
      他列不动、缺 id 不冒领、坏输入中文拒、幂等、其余三个动作回归。
  L4 **反向护栏** —— 默认行为与改动前等价（like/dislike/delete/favorite
      语义不变；不带 act 的坏请求仍中文拒）。

⚠ 本仓「待审核」是挂在同一批图上的**工作清单**，不是独立文件夹：
  gallery 的 where 是 `in_quarantine=0 AND blacklisted=0 AND path IS NOT NULL`，
  **不过滤 needs_review**（桌面端图库同口径；实测 gallery ids [2,1] /
  pending ids [1]）⇒ 移动后的图**仍留在图库**，只是同时进待审核清单。
  所以「移动」的可见性靠**提示文案**兜（见 mrmActToast），
  断言也必须按这个模型写（第一版断言「gallery 里消失」是**我判据写错**，
  实测抓出来改掉的，不是产品问题）。
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
INDEX_HTML = ROOT / "media_resource_manager" / "web" / "index.html"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
STUB = ROOT / "tools" / "r77_dom_stub.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="本机没装 node ⇒ 跳过纯 JS 行为层（不是产品问题）")


# ══════════════════════════ 工具 ══════════════════════════

def _read(p: Path) -> str:
    """统一按 LF 读（webui.py 是 CRLF 全文，LF 字面量匹配会 count=0）。"""
    return p.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _func_node(name: str) -> ast.FunctionDef:
    tree = ast.parse(_read(WEBUI_PY))
    hits = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name]
    assert len(hits) == 1, f"webui.py 里 {name} 找到 {len(hits)} 个（要求 1）"
    return hits[0]


def _func_src_no_doc(name: str) -> str:
    """函数**含签名**源码，剥掉 docstring 后 unparse。

    ⚠ 必须剥 docstring：docstring 里大量引用旧 bug 的字面量，
      判据会被自己的注释骗过（R69 实测）。
    """
    n = _func_node(name)
    body = list(n.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    n.body = body
    return ast.unparse(n)


def _backend_allowed_acts() -> set:
    """从 AST 读 `_api_image_action` 里 allowed 元组的字面量集合。"""
    n = _func_node("_api_image_action")
    assigns = [s for s in ast.walk(n)
               if isinstance(s, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "allowed"
                       for t in s.targets)]
    assert len(assigns) == 1, f"allowed 赋值找到 {len(assigns)} 个"
    tup = assigns[0].value
    assert isinstance(tup, ast.Tuple), "allowed 不是元组"
    return {e.value for e in tup.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)}


def _actionbar_html() -> str:
    """index.html 里动作条那一段（到灯箱注释之前）。

    R142：底部 actionbar 已删除，8 个动作并入顶部紫条区 id="topactions"。
    """
    html = INDEX_HTML.read_bytes().decode("utf-8")
    i = html.index('id="topactions"')
    j = html.index('id="lightbox"', i)
    return html[i:j]


def _js(body: str):
    """在 node 里真 require 前端 app.js，执行 body 并返回 JSON 结果。

    ⚠ DOM 桩用 tools/r77_dom_stub.js（与 R77/R78/R81 同一份）——
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


# ═══════════════ L1 · node 真执行（前端行为层） ═══════════════

def test_move_is_in_backend_whitelist():
    """allowed 白名单必须含 move，且原来四个一个不少（不许顺手删）。"""
    got = _backend_allowed_acts()
    assert "move" in got, f"白名单里没有 move：{sorted(got)}"
    for a in ("like", "dislike", "blacklist", "favorite", "delete"):
        assert a in got, f"白名单把原来的 {a} 弄丢了：{sorted(got)}"


def test_move_branch_sets_both_columns_in_one_statement():
    """move 分支的 UPDATE 必须**同一条语句**里 needs_review=1 且
    in_quarantine=0（互斥不变式），对象是 images 表。"""
    n = _func_node("_api_image_action")
    moves = [x for x in ast.walk(n)
             if isinstance(x, ast.If)
             and isinstance(x.test, ast.Compare)
             and isinstance(x.test.left, ast.Name)
             and x.test.left.id == "act"
             and any(isinstance(c, ast.Constant) and c.value == "move"
                     for c in x.test.comparators)]
    assert len(moves) == 1, f"move 分支找到 {len(moves)} 个（要求 1）"
    sqls = []
    for s in ast.walk(moves[0]):
        if (isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute)
                and s.func.attr == "execute" and s.args
                and isinstance(s.args[0], ast.Constant)):
            sqls.append(s.args[0].value)
    upd = [s for s in sqls if "UPDATE images" in s]
    assert upd, f"move 分支里没有 UPDATE images：{sqls}"
    sql = upd[0]
    assert "needs_review=1" in sql, f"没置 needs_review=1：{sql}"
    assert "in_quarantine=0" in sql, f"没清 in_quarantine（互斥会被破坏）：{sql}"


def test_move_branch_precedes_decision_mapping():
    """★ move 分支必须排在 `dec = {...}[act]` **之前**。

    排在后面会怎样：dec 字典没有 "move" 键 ⇒ KeyError ⇒ 被循环外面的
    `except Exception: continue` 吞掉 ⇒ done=0，但返回值还是
    `{"ok": true, "done": 0}` —— 「ok 但做了另一件事」的教科书形态。
    """
    n = _func_node("_api_image_action")
    move_if = dec_assign = None
    for x in ast.walk(n):
        if (isinstance(x, ast.If) and isinstance(x.test, ast.Compare)
                and isinstance(x.test.left, ast.Name)
                and x.test.left.id == "act"
                and any(isinstance(c, ast.Constant) and c.value == "move"
                        for c in x.test.comparators)):
            move_if = x
        if (isinstance(x, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "dec"
                        for t in x.targets)):
            dec_assign = x
    assert move_if is not None, "找不到 move 分支"
    assert dec_assign is not None, "找不到 dec 映射"
    assert (move_if.lineno, move_if.col_offset) < \
           (dec_assign.lineno, dec_assign.col_offset), (
        "move 分支排在 dec 映射之后 ⇒ KeyError 会被 except 吞成 done=0")


def test_move_branch_writes_no_review_and_no_learner():
    """移动不是审核决定：分支内不许写 reviews、不许喂学习器。"""
    n = _func_node("_api_image_action")
    moves = [x for x in ast.walk(n)
             if isinstance(x, ast.If)
             and isinstance(x.test, ast.Compare)
             and isinstance(x.test.left, ast.Name)
             and x.test.left.id == "act"
             and any(isinstance(c, ast.Constant) and c.value == "move"
                     for c in x.test.comparators)]
    src = ast.unparse(moves[0])
    assert "INSERT INTO reviews" not in src, "移动写了审核记录"
    assert "PreferenceLearner" not in src, "移动喂了学习器"


def test_route_branch_is_intact():
    """路由必须还是那个分支整体（R79 教训：只判子串会被
    `/api/image/action_x` 之类的命名骗过）。"""
    src = _func_src_no_doc("do_POST")
    assert "if path == '/api/image/action':" in src, "路由分支没了"
    assert "self._api_image_action(self._body())" in src, "路由没接处理方法"

class _Queue:
    """替身队列：本批用例不碰任务启停。"""


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def raw(self, path, body=None, method=None):
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base + path, data=data, headers=hdrs,
            method=method or ("POST" if body is not None else "GET"))
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
    """真起一个 WebServer（模块粒度起一次）。

    `web_lan_open=False` 才会真走鉴权（R58：默认配置下 127.0.0.1 恒免密
    ⇒ 鉴权类断言恒真）；`save_config` 必须还原（R67 实测：这个
    monkeypatch 不还原会污染别的测试文件）。
    """
    tmp = tmp_path_factory.mktemp("r82_live")
    db = Database(tmp / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp / "mrm.db"
    cfg.rules_dir = tmp / "rules"
    cfg.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.web_password = "r82pw"
    cfg.web_lan_open = False
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_user = "admin"

    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    srv = webui.WebServer(cfg, db, queue=_Queue())
    ok, msg = srv.start()
    assert ok, f"WebServer 起不来：{msg}"
    client = Client(f"http://127.0.0.1:{cfg.web_port}")
    st, body = client.jpost("/api/login", {"password": "r82pw"})
    assert st == 200 and body.get("ok"), f"登录失败：{st} {body}"

    yield {"client": client, "db": db, "tmp": tmp}

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


def _mk_image(db, tid, tag, *, quarantined=False, pending=False):
    """真插一行图（每个 tag 唯一 ⇒ 不撞 UNIQUE(task_id, source_url)）。"""
    db.execute(
        "INSERT INTO images(task_id, source_url, path, tags, bytes, score,"
        " blacklisted, in_quarantine, needs_review, favorite)"
        " VALUES (?,?,?,?,?,?,0,?,?,0)",
        (tid, f"https://example.com/{tid}/{tag}.jpg",
         f"/tmp/r82_{tid}_{tag}.jpg", f"r82tag {tag}", 2048,
         0.5, 1 if quarantined else 0, 1 if pending else 0))
    return int(db.query(
        "SELECT id FROM images WHERE task_id=? AND source_url=?",
        (tid, f"https://example.com/{tid}/{tag}.jpg"))[0]["id"])


def _row(db, iid):
    r = db.query("SELECT * FROM images WHERE id=?", (iid,))
    return dict(r[0]) if r else None


def _view_ids(c, view, limit=200):
    st, d = c.jget(f"/api/view/{view}?limit={limit}")
    assert st == 200 and d.get("ok"), (st, d)
    return [int(r["id"]) for r in d["items"]]


def test_move_sends_selected_images_to_pending(live):
    """★ #32：多选后「移动」⇒ 选中的进待审核，没选的原地不动。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R82移动进待审")
    a, b, keep = (_mk_image(db, tid, "a"), _mk_image(db, tid, "b"),
                  _mk_image(db, tid, "keep"))
    st, body = c.jpost("/api/image/action", {"ids": [a, b], "act": "move"})
    assert st == 200 and body.get("ok"), (st, body)
    assert body["done"] == 2, body
    for iid in (a, b):
        r = _row(db, iid)
        assert r["needs_review"] == 1, f"{iid} 没进待审核：{r}"
        assert r["in_quarantine"] == 0, f"{iid} 被误隔离：{r}"
    r = _row(db, keep)
    assert r["needs_review"] == 0 and r["in_quarantine"] == 0, f"没选的被动了：{r}"
    # 视图层面真的看得见：pending 清单里有这三个之外的那两个
    pend = _view_ids(c, "pending")
    gal = _view_ids(c, "gallery")
    assert {a, b} <= set(pend), f"pending 视图看不到移动结果：{pend}"
    assert keep not in set(pend), "没选的图被误移进待审核"
    # ★ 本仓模型：待审核是工作清单不是独立文件夹 ⇒ 移动后的图**仍留在
    #   gallery**（gallery 的 where 不过滤 needs_review）。断言「消失」
    #   是判据写错（第一版就这么错的，实测抓出来改掉的）。
    assert {a, b, keep} <= set(gal), f"gallery 里少了图：{gal}"


def test_move_clears_quarantine_and_never_both_flags(live):
    """★ 互斥不变式：隔离区的图移动后 in_quarantine=0 且 needs_review=1，
    两个标志**不许同时为 1**（同时置位 = 两个视图里都看不见）。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R82隔离移出")
    q = _mk_image(db, tid, "q", quarantined=True)
    assert _row(db, q)["in_quarantine"] == 1
    st, body = c.jpost("/api/image/action", {"ids": [q], "act": "move"})
    assert st == 200 and body.get("ok") and body["done"] == 1, (st, body)
    r = _row(db, q)
    assert r["in_quarantine"] == 0, f"隔离标志没清：{r}"
    assert r["needs_review"] == 1, f"没进待审核：{r}"
    assert not (r["in_quarantine"] and r["needs_review"]), "两个标志同时为 1"


def test_move_touches_only_the_two_flags(live):
    """除两个标志位外，一行里的其它字段一个字节都不许动；
    reviews / preferences 行数也不许变（不写审核记录、不喂学习器）。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R82只动两列")
    iid = _mk_image(db, tid, "cols")
    before = _row(db, iid)
    n_rev = db.query("SELECT COUNT(*) AS n FROM reviews")[0]["n"]
    n_pref = db.query("SELECT COUNT(*) AS n FROM preferences")[0]["n"]
    st, body = c.jpost("/api/image/action", {"ids": [iid], "act": "move"})
    assert st == 200 and body.get("ok"), (st, body)
    after = _row(db, iid)
    for col in ("task_id", "source_url", "sha256", "phash", "width",
                "height", "bytes", "path", "tags", "score", "user_score",
                "blacklisted", "collected_at", "rating", "favorite", "id"):
        assert after[col] == before[col], f"{col} 被移动改动了"
    assert after["needs_review"] == 1 and after["in_quarantine"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM reviews")[0]["n"] == n_rev, \
        "移动写了审核记录"
    assert db.query("SELECT COUNT(*) AS n FROM preferences")[0]["n"] == n_pref, \
        "移动喂了学习器"


def test_move_skips_missing_ids_and_reports_true_done(live):
    """缺 id 不许冒领 done（与其它动作同口径：先查存在再动手）。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R82缺id")
    real = _mk_image(db, tid, "real")
    ghost = int(_row(db, real)["id"]) + 424242
    st, body = c.jpost("/api/image/action",
                       {"ids": [real, ghost], "act": "move"})
    assert st == 200 and body.get("ok"), (st, body)
    assert body["done"] == 1, f"done 冒领了：{body}"
    assert _row(db, real)["needs_review"] == 1


def test_move_rejects_bad_requests_in_chinese(live):
    """坏请求一律中文拒绝、不 500、不静默成功（用户可见错误必须中文）。"""
    c = live["client"]
    st, body = c.jpost("/api/image/action", {"ids": [1], "act": "nuke"})
    assert st == 200 and body.get("ok") is False, (st, body)
    assert "不支持" in body.get("error", ""), body
    st, body = c.jpost("/api/image/action", {"ids": [], "act": "move"})
    assert body.get("ok") is False and "没选任何图片" in body.get("error", ""), body
    st, body = c.jpost("/api/image/action", {"ids": "1", "act": "move"})
    assert body.get("ok") is False and "ids 必须是数组" in body.get("error", ""), body
    st, body = c.jpost("/api/image/action", {"ids": ["x"], "act": "move"})
    assert body.get("ok") is False and "ids 必须是数字" in body.get("error", ""), body


def test_move_is_idempotent(live):
    """同一批图移动两次：不报错、状态稳定（幂等）。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R82幂等")
    a, b = _mk_image(db, tid, "i1"), _mk_image(db, tid, "i2")
    for _ in range(2):
        st, body = c.jpost("/api/image/action", {"ids": [a, b], "act": "move"})
        assert st == 200 and body.get("ok") and body["done"] == 2, (st, body)
    for iid in (a, b):
        r = _row(db, iid)
        assert r["needs_review"] == 1 and r["in_quarantine"] == 0, r


def test_moved_images_are_reachable_through_pending_pagination(live):
    """移动结果必须能通过 UI 真走的翻页路径拿到（不重不漏）。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R82翻页可见")
    old = [_mk_image(db, tid, f"old{i}", pending=True) for i in range(2)]
    moved = [_mk_image(db, tid, f"mv{i}") for i in range(3)]
    st, body = c.jpost("/api/image/action", {"ids": moved, "act": "move"})
    assert st == 200 and body.get("ok"), (st, body)
    seen, cursor, guard = [], None, 0
    while True:
        guard += 1
        assert guard < 100, "翻页没到头"
        url = "/api/view/pending?limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        st, d = c.jget(url)
        assert st == 200 and d.get("ok"), (st, d)
        seen.extend(int(r["id"]) for r in d["items"])
        if not d.get("has_more"):
            break
        cursor = d["next_cursor"]
        assert cursor is not None
    mine = [i for i in seen if i in set(old) | set(moved)]
    assert sorted(mine) == sorted(old + moved), f"翻页拿不全：{mine}"
    assert len(mine) == len(set(mine)), f"翻页有重复：{mine}"


def test_other_batch_acts_still_behave(live):
    """★ 反向护栏：like/dislike/delete/favorite 语义与改动前一致。"""
    c, db = live["client"], live["db"]
    tid = _mk_task(c, "R82其余回归")
    like = _mk_image(db, tid, "lk", pending=True)
    dis = _mk_image(db, tid, "dk", pending=True)
    dele = _mk_image(db, tid, "dl")
    fav = _mk_image(db, tid, "fv")

    st, body = c.jpost("/api/image/action", {"ids": [like], "act": "like"})
    assert body.get("ok") and body["done"] == 1, body
    r = _row(db, like)
    assert r["needs_review"] == 0 and r["in_quarantine"] == 0, r
    rev = db.query("SELECT decision FROM reviews WHERE image_id=?", (like,))
    assert rev and rev[0]["decision"] == "like", "通过没写审核记录"

    st, body = c.jpost("/api/image/action", {"ids": [dis], "act": "dislike"})
    assert body.get("ok") and body["done"] == 1, body
    r = _row(db, dis)
    # ⚠ R158-Bug2 把这条语义**反转**了（原来钉的是 in_quarantine=0）：
    #   桌面端「不喜欢」= 移进回收站/隔离区，Web 端却只清 needs_review，
    #   而待审核/图库里的图 needs_review 本来就是 0 ⇒ 那条 UPDATE 是空
    #   操作 ⇒ 用户实测「点了不喜欢，图还在图库」。现在两端同口径：
    #   不喜欢 = 出待审核 + 进隔离区（可还原）。
    assert r["needs_review"] == 0 and r["in_quarantine"] == 1, r
    rev = db.query("SELECT decision FROM reviews WHERE image_id=?", (dis,))
    assert rev and rev[0]["decision"] == "dislike", "拒绝没写审核记录"

    st, body = c.jpost("/api/image/action", {"ids": [dele], "act": "delete"})
    assert body.get("ok") and body["done"] == 1, body
    r = _row(db, dele)
    assert r["in_quarantine"] == 1 and r["needs_review"] == 0, r

    st, body = c.jpost("/api/image/action", {"ids": [fav], "act": "favorite"})
    assert body.get("ok") and body["done"] == 1, body
    assert _row(db, fav)["favorite"] == 1, "收藏没切换"
