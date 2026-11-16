"""R228 · R154-F17 的 **Web 对称端点**护栏（2026-10-23）。

挂账原文（R154_FEATURES_ALL 收尾段）：
  F17 的 Web 对称端点未做 —— `/api/image/tags` + 顶栏按钮，
  **独立路由不碰 `_api_image_action` 四态机白名单**（R81/R82 契约）。

分三层钉契约：

  L1 纯函数（tags_util —— 桌面端与网页端**同一份**实现）
    - read_tags 保大小写 / 认老库逗号格式
    - merge_tags 去重不覆盖、保序；strip_tags 只给确实变了的返回
    - union_tags 并集保序（删标签弹窗的勾选清单）
    - count_tags 次数倒序、同名排、含空格跳过
    - MainWindow 的三个静态方法**只是转发**（不许再写一份）

  L2 源码 / AST（防"功能被顺手删"）
    - GET：/api/tags 与 /api/image/tags 都接了路由
    - POST：/api/image/tags 独立路由，**不在**四态机白名单里
    - webui 只调 tags_util，不自己解析 tags 字符串
    - index.html 两个顶栏按钮 + app.js 用 mrmQueryWithTag 拼查询串

  L3 行为（真库 + 真 handler；前端纯函数 node 真跑）
    - 加标签写库、已有跳过、done 只数真改的行、**不动四态机标志位**
    - 删标签只删勾中的；并集保序
    - 标签筛选走 search.parse_query 的 tag: 语法（与搜索框同一条路径）

变异自检见 `.workbuddy/_r228_mutate.py`（每条判据都要能红）。
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_resource_manager.db import Database          # noqa: E402
from media_resource_manager import tags_util            # noqa: E402

WEBUI_PY = ROOT / "media_resource_manager" / "webui.py"
APP_JS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "media_resource_manager" / "web" / "index.html"
MW_PY = ROOT / "media_resource_manager" / "gui" / "main_window.py"
STUB = ROOT / "tools" / "r77_dom_stub.js"

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="本机没装 node ⇒ 跳过纯 JS 行为层（不是产品问题）")


# ═══════════════════════ L1 · 纯函数 ═══════════════════════

def test_read_tags_keeps_case_and_legacy_comma():
    assert tags_util.read_tags('["White Stockings", "cute"]') == \
        ["White Stockings", "cute"]
    assert tags_util.read_tags("白丝,黑丝") == ["白丝", "黑丝"]
    assert tags_util.read_tags(None) == []
    assert tags_util.read_tags(["a", "b"]) == ["a", "b"]


def test_merge_tags_dedup_keeps_order_and_skips_existing():
    rows = [{"id": 1, "tags": "a,b"}, {"id": 2, "tags": '["b"]'}]
    plan = tags_util.merge_tags(rows, "c")
    assert plan == [(1, '["a", "b", "c"]'), (2, '["b", "c"]')], plan
    # 已有该标签的行不返回（不写库、不计数）
    assert tags_util.merge_tags([{"id": 1, "tags": '["c"]'}], "c") == []
    assert tags_util.merge_tags(rows, "  ") == []


def test_strip_tags_only_returns_changed_rows():
    rows = [{"id": 1, "tags": "a,b"}, {"id": 2, "tags": '["a"]'}]
    assert tags_util.strip_tags(rows, ["b"]) == [(1, '["a"]')]
    assert tags_util.strip_tags(rows, []) == []
    assert tags_util.strip_tags(rows, ["zzz"]) == []


def test_union_tags_is_order_stable():
    rows = [{"id": 1, "tags": "b,a"}, {"id": 2, "tags": '["a","c"]'}]
    # 保序去重：第一次出现的位置为准（删标签清单的顺序不能随机跳）
    assert tags_util.union_tags(rows) == ["b", "a", "c"]


def test_count_tags_sorted_and_skips_spaced():
    rows = [{"tags": "b,b,a"}, {"tags": '["a","c"]'},
            {"tags": '["white socks"]'}]      # 含空格 → 跳过
    got = tags_util.count_tags(rows, 40)
    assert got[:2] == [("a", 2), ("b", 2)], got
    assert all(t for t, _ in got if " " not in t)
    # 次数相同按名排（顺序稳定，不随机）
    assert [t for t, _ in got] == sorted([t for t, _ in got],
                                         key=lambda t: (-dict(got)[t], t))


def test_main_window_delegates_to_shared_layer():
    """桌面端不许再自带一份：三个静态方法必须转发到 tags_util。"""
    src = MW_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    want = {"_read_tags": "read_tags", "_merge_tags": "merge_tags",
            "_strip_tags": "strip_tags"}
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in want:
            continue
        calls = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Attribute):
                    calls.add(n.func.attr)
                elif isinstance(n.func, ast.Name):
                    calls.add(n.func.id)
        found[node.name] = calls
    for mw_name, fn in want.items():
        assert mw_name in found, f"main_window 丢了 {mw_name}"
        assert fn in found[mw_name], (
            f"{mw_name} 没转发到 tags_util.{fn}（又写了一份？）")


# ═══════════════════════ L2 · 源码 / AST ═══════════════════════

def test_get_routes_are_wired_literally():
    """★ 判「整条分支在不在」，不是判字面量出现过。"""
    # ⚠ 判据必须**连同下一行一起判**：GET 与 POST 的 `if path == ...`
    #   字面量是**同一个字符串**，只判这一行的话把 POST 那条改名（=没接
    #   路由）也照样绿（R73「判据与被护对象分家」的同型坑，M6 实测漏网）。
    src = WEBUI_PY.read_text(encoding="utf-8")
    assert ('if path == "/api/tags":\n'
            '                return self._json(self._api_tags())') in src, \
        "GET /api/tags 没接路由"
    assert ('if path == "/api/image/tags":\n'
            '                return self._json(self._api_image_tags(u))'
            ) in src, "GET /api/image/tags 没接路由"


def test_post_route_is_independent_of_the_four_state_machine():
    src = WEBUI_PY.read_text(encoding="utf-8")
    assert ('if path == "/api/image/tags":\n'
            '                return self._json('
            'self._api_image_tags_post(self._body()))') in src, \
        "POST /api/image/tags 没接路由"
    # 反向：加/删标签**不许**混进审核四态机白名单
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_api_image_action":
            fn = node
    assert fn is not None
    allowed = None
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                isinstance(n.targets[0], ast.Name) and \
                n.targets[0].id == "allowed":
            allowed = [e.value for e in n.value.elts]
    assert allowed is not None
    for bad in ("addtag", "deltag", "tag"):
        assert bad not in allowed, (
            f"{bad} 进了四态机白名单 —— 加标签不是审核决定")


def test_webui_calls_tags_util_and_never_parses_tags_itself():
    src = WEBUI_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_api_") \
                and "tag" in node.name:
            fns[node.name] = node
    for name in ("_api_tags", "_api_image_tags", "_api_image_tags_post"):
        assert name in fns, f"webui 里没有 {name}"
    calls = {}
    for name, fn in fns.items():
        calls[name] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Attribute):
                    calls[name].add(f.attr)
                elif isinstance(f, ast.Name):
                    calls[name].add(f.id)
    assert "count_tags" in calls["_api_tags"]
    assert "union_tags" in calls["_api_image_tags"]
    assert "merge_tags" in calls["_api_image_tags_post"]
    assert "strip_tags" in calls["_api_image_tags_post"]
    # 反向：不许自己 json.loads / re.split 解析 tags（那是第二套口径）
    # ⚠ 必须先把**文档字符串与注释**剥掉再查：本仓注释大量 quoting 过去的错法
    #   （R63 铁律），`_api_tags` 的新注释里恰好写着"不在 Python 里逐行
    #   json.loads 解析"，按原文匹配就会命中自己写的解释 —— 假红。
    import re as _re
    for name, fn in fns.items():
        seg = ast.get_source_segment(src, fn) or ""
        seg = _re.sub(r'"""(?:.|\n)*?"""', "", seg)
        seg = _re.sub(r"#.*", "", seg)
        assert "json.loads" not in seg, f"{name} 自己解析了 tags"
        assert "re.split" not in seg, f"{name} 自己切了 tags 字符串"

def _mk_db(tmp_path):
    """⚠ C10 之后 images.task_id 仍是 NOT NULL（只是外键退役了），
    所以每张图都得挂在真任务上 —— 不建任务会 NOT NULL 报错。"""
    db = Database(tmp_path / "r228.db")
    cur = db.execute("INSERT INTO tasks(name) VALUES (?)", ("R228",))
    db._tid = int(cur.lastrowid)
    return db


def _mk_image(db, tags, favorite=0, quarantine=0, review=0):
    cur = db.execute(
        "INSERT INTO images(task_id, source_url, tags, favorite,"
        " in_quarantine, needs_review) VALUES (?,?,?,?,?,?)",
        (db._tid, "https://x.com/%s.jpg" % os.urandom(4).hex(), tags,
         favorite, quarantine, review))
    return int(cur.lastrowid)


def _handler(db):
    from media_resource_manager import webui

    class _H(webui._Handler):
        def __init__(self, d):
            self._db = d
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)

        @property
        def db(self):
            return self._db

    return _H(db)


def test_api_tags_returns_counts(tmp_path):
    db = _mk_db(tmp_path)
    _mk_image(db, '["白丝", "风景"]')
    _mk_image(db, '["白丝"]')
    _mk_image(db, '["双马尾 少女"]')          # 含空格 → 不进下拉
    h = _handler(db)
    out = h._api_tags()
    assert out["ok"] is True
    items = {o["tag"]: o["n"] for o in out["items"]}
    assert items.get("白丝") == 2 and items.get("风景") == 1
    assert "双马尾 少女" not in items


def test_post_add_tag_writes_once_and_skips_existing(tmp_path):
    db = _mk_db(tmp_path)
    a = _mk_image(db, '["白丝"]')
    b = _mk_image(db, "风景")
    h = _handler(db)
    out = h._api_image_tags_post({"ids": [a, b], "act": "add",
                                  "tags": ["风景"]})
    assert out["ok"] is True and out["act"] == "add"
    assert out["done"] == 1, "已带该标签的图不该计数"
    got = json.loads(db.query("SELECT tags FROM images WHERE id=?",
                              (a,))[0]["tags"])
    assert got == ["白丝", "风景"], "加标签不能覆盖原有标签"
    # 已带该标签的图：一行都不动（连字符串都不该被重写成 JSON）
    assert db.query("SELECT tags FROM images WHERE id=?", (b,))[0]["tags"] \
        == "风景"


def test_post_add_tag_needs_no_side_effect_on_flags(tmp_path):
    """加标签不是审核决定 ⇒ 四态机标志位一动不许动。"""
    db = _mk_db(tmp_path)
    i = _mk_image(db, '["白丝"]', favorite=1, quarantine=0, review=1)
    h = _handler(db)
    assert h._api_image_tags_post(
        {"ids": [i], "act": "add", "tags": ["x"]})["done"] == 1
    r = db.query("SELECT favorite, in_quarantine, needs_review FROM images "
                 "WHERE id=?", (i,))[0]
    assert (r["favorite"], r["in_quarantine"], r["needs_review"]) == (1, 0, 1)


def test_post_remove_tag_only_removes_checked(tmp_path):
    db = _mk_db(tmp_path)
    a = _mk_image(db, '["白丝", "风景", "猫耳"]')
    h = _handler(db)
    out = h._api_image_tags_post({"ids": [a], "act": "remove",
                                  "tags": ["风景"]})
    assert out["ok"] is True and out["done"] == 1
    assert json.loads(db.query("SELECT tags FROM images WHERE id=?",
                               (a,))[0]["tags"]) == ["白丝", "猫耳"]
    # 删不存在的标签 → 一行都没变
    out2 = h._api_image_tags_post({"ids": [a], "act": "remove",
                                   "tags": ["不存在"]})
    assert out2["done"] == 0


def test_get_image_tags_union_is_order_stable(tmp_path):
    db = _mk_db(tmp_path)
    a = _mk_image(db, "b,a")
    b2 = _mk_image(db, '["a","c"]')
    h = _handler(db)
    out = h._api_image_tags(urlparse(
        "/api/image/tags?ids=%d,%d" % (a, b2)))
    assert out["ok"] is True
    assert out["tags"] == ["b", "a", "c"], out["tags"]
    assert [i["tags"] for i in out["items"]] == [["b", "a"], ["a", "c"]]
    # 不带 ids ⇒ 空清单，不许 500
    assert h._api_image_tags(urlparse("/api/image/tags"))["tags"] == []


def test_bad_params_answer_false_not_500(tmp_path):
    h = _handler(_mk_db(tmp_path))
    assert h._api_image_tags_post({"ids": [], "act": "add",
                                   "tags": ["a"]})["ok"] is False
    assert h._api_image_tags_post({"ids": [1], "act": "zzz",
                                   "tags": ["a"]})["ok"] is False
    assert h._api_image_tags_post({"ids": [1], "act": "add",
                                   "tags": []})["ok"] is False
    assert h._api_image_tags_post({"ids": "x", "act": "add",
                                   "tags": ["a"]})["ok"] is False

def _js(body: str):
    """node 里真 require 前端 app.js（同 R77 的桩），执行 body 返回 JSON。"""
    assert NODE, "没有 node"
    script = (
        "require(%s).install();\n"
        "var m = require(%s);\n"
        "var out = (function () { %s })();\n"
        "process.stdout.write(JSON.stringify(out));\n"
        % (json.dumps(str(STUB)), json.dumps(str(APP_JS)), body))
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    assert p.returncode == 0, (
        f"node 执行失败 rc={p.returncode}\nSTDOUT:{p.stdout}\n"
        f"STDERR:{p.stderr}")
    return json.loads(p.stdout)
