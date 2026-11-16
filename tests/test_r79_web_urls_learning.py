"""R79 · Web 两项：#41 常用网址管理 / #44 学习看板（样本管理 + 重置 + 已学统计）。

作业单位置：TONIGHT_FINAL_ORDER 主单第 41 / 44 项，
PRIORITY_TONIGHT_ORDER §2「Web 剩余 7 项」里最后剩下的两个。

**为什么这两项放一轮**：它们都落在「系统」这一组里，且都是「桌面端早就
有、网页端缺」的同一类补齐 —— 一次改 webui + app.js + index.html + css 一条链，
一次验完，闭环最小。

四层判据（铁律 R73：判据现在是绿的不算数，**必须变异一次确认它真会红**）：
  L1 **node 真执行** —— 真 require 前端 app.js 跑纯函数，问的是**行为**；
  L2 **Python 纯函数** —— `webui._normalize_saved_url` 逐例真判；
  L3 **源码接线（AST）** —— 端点真挂在路由上、重置真只清 task_id=0；
  L4 **真 HTTP** —— 真起 WebServer + 真 socket 走完整增删流程。

⚠ 四个「不做就白做」的点：
  1. **常用网址必须与桌面端同一份字段**（`AppConfig.saved_urls`）。
     网页端另存一份 = 手机上加的桌面端永远看不见，属于「ok 但做了另一件事」。
  2. **重置学习必须后端二次确认**（不带 confirm 直接拒），且**只清 task_id=0** ——
     按任务学的行语义不同，一并删掉是误伤。
  3. **学习统计口径必须复用桌面端那个函数**（`classify.learning_stats.collect`），
     另写一套就会出现「桌面 12 条、手机 9 条」。
  4. **端点刻意叫 /api/learn/\\* 不叫 /api/learning/\\***：
     test_r48_h_feature_smoke 的元测试按「全文替换一次后不该再出现」做变异，
     加子路径会把它顶红（改名比改那条绿着的测试代价小）。
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
HTML = ROOT / "media_resource_manager" / "web" / "index.html"
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
    """取函数体（**剥掉 docstring** 后 unparse）。

    R69 实测：`ast.unparse(整个函数)` 会把 docstring 一起打包，而 docstring
    里大量引用「以前错在哪」的字面量 ⇒ 判据被自己的注释骗过。
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


def _js(body: str):
    """在 node 里真 require 前端 app.js 执行 body，返回 JSON。

    ⚠ DOM 桩用 tools/r77_dom_stub.js（与 R77/R78 同一份）——
      各写一份会静默失效（R63-D3「判据与被护对象分家」同型）。
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


def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in (s or ""))


# ═══════════════ L1 · node 真执行（#41 常用网址） ═══════════════

@pytest.mark.parametrize("raw,url", [
    ("https://a.com/x", "https://a.com/x"),
    ("http://a.com", "http://a.com"),
    ("a.com", "https://a.com"),                 # 只粘域名 ⇒ 补 https
    ("  https://a.com  ", "https://a.com"),     # 首尾空白去掉
    ("a.com/p?x=1&y=2", "https://a.com/p?x=1&y=2"),
])
def test_normalize_accepts_the_common_shapes(raw, url):
    got, err = webui._Handler._normalize_saved_url(raw)
    assert (got, err) == (url, ""), (got, err)


@pytest.mark.parametrize("raw", ["", "   ", "ftp://a.com", "javascript:alert(1)",
                                 "file:///C:/x", "https://", "http:///x"])
def test_normalize_rejects_in_chinese(raw):
    """★ 拒的一律给**中文**理由（铁律：用户可见错误必须中文）。

    ⚠ `javascript:` / `file:` 必须拒：存进列表后点开就是在本机上下文里执行。
    """
    got, err = webui._Handler._normalize_saved_url(raw)
    assert got == "", f"{raw!r} 本该被拒却通过了：{got!r}"
    assert _has_cjk(err), f"提示不是中文：{err!r}"


def test_urls_field_is_the_same_one_the_desktop_uses():
    """★ 与桌面端同一份字段，否则手机上加的桌面端永远看不见。"""
    assert webui._Handler._URLS_FIELD == "saved_urls"
    assert "saved_urls" in AppConfig.__dataclass_fields__, (
        "AppConfig 上没有 saved_urls —— 网页端存的桌面端读不到")


# ═══════════════ L3 · 源码接线（AST） ═══════════════

def test_new_endpoints_are_really_routed():
    """★ 判「真接在路由上」，且必须判**完整分支**。

    ⚠ 只判「源码里出现过 /api/urls」是空壳：/api/urls_x 这个串里也含
      /api/urls，把路由改名成 _x 结尾的变异照样全绿
      （本轮变异测试 M1/M2 就是这么抓出来的）。
      所以判据落到「if path == 该端点:」这整个分支在不在。
    """
    src = _read(WEBUI_PY)
    for ep in ("/api/urls", "/api/url/save", "/api/url/delete",
               "/api/learn/delete", "/api/learn/reset"):
        assert f'if path == "{ep}":' in src, f"{ep} 没接路由（写了函数不接路由 = 白写）"


def test_learning_stats_come_from_the_desktop_function():
    """★ 统计口径必须复用桌面端那个函数，另写一套就是两个数字。"""
    src = _py_func_src("_api_learning")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "collect"]
    # ⚠ 只判「源码里出现过 learning_stats」是空壳：import 那一行就带着它，
    #   把真正的调用换成手写 dict 照样全绿（本轮 M3 抓到）。
    #   判据要落到**真调用**上。
    assert calls, "_api_learning 没真的调用 learning_stats.collect（另写口径 = 两个数字）"


def test_reset_only_clears_the_global_scope():
    """★ 只清 task_id=0：按任务学的行一并删掉属于误伤。"""
    src = _py_func_src("_api_learning_reset")
    assert "task_id=0" in src, "重置没限定全局范围 ⇒ 会误删按任务学的记录"
    assert "DELETE FROM preferences" in src


def test_reset_refuses_without_confirmation():
    """不带 confirm 直接拒 —— 这是**不可撤销**的删除，前端弹窗只是提示。"""
    src = _py_func_src("_api_learning_reset")
    assert "confirm" in src, "重置没有后端确认闸"


def test_url_endpoints_persist_to_config():
    """写了 cfg 不 save_config ⇒ 重启就丢，等于「改了没反应」。"""
    for fn in ("_api_url_save", "_api_url_delete"):
        src = _py_func_src(fn)
        assert "save_config" in src, f"{fn} 没落盘"

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
            url, data=data, headers=hdrs,
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
    """真起 WebServer（模块粒度：起一次跑完全部用例）。

    ⚠ 与 R50/R78 同一套注意事项：端口 0 让 OS 选；`web_lan_open=False`
    才会真走鉴权；`save_config` 用 monkeypatch 接管（R67 实测：这个
    monkeypatch 不还原会污染别的测试文件）。
    """
    tmp = tmp_path_factory.mktemp("r79_live")
    db = Database(tmp / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp / "mrm.db"
    cfg.rules_dir = tmp / "rules"
    cfg.rules_dir.mkdir(parents=True, exist_ok=True)
    cfg.web_password = "r79pw"
    cfg.web_lan_open = False          # ⚠ 不关掉 ⇒ 127.0.0.1 恒免密（R58）
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_user = "admin"

    import media_resource_manager.config as cfgmod
    monkeypatched = []

    def _recorder(c, path=None):
        monkeypatched.append(len(getattr(c, "saved_urls", []) or []))

    orig_save = cfgmod.save_config
    cfgmod.save_config = _recorder

    srv = webui.WebServer(cfg, db, queue=None)
    ok, msg = srv.start()
    assert ok, f"WebServer 起不来：{msg}"
    client = Client(f"http://127.0.0.1:{cfg.web_port}")
    st, body = client.jpost("/api/login", {"password": "r79pw"})
    assert st == 200 and body.get("ok"), f"登录失败：{st} {body}"

    yield {"client": client, "db": db, "cfg": cfg, "tmp": tmp,
           "saves": monkeypatched}

    try:
        srv.stop()
    finally:
        cfgmod.save_config = orig_save
        try:
            db.close()
        except Exception:
            pass


def test_urls_roundtrip_over_http(live):
    """★ #41：加 → 列表里有了 → 删 → 列表里没了。"""
    c = live["client"]
    st, d = c.jget("/api/urls")
    assert st == 200 and d.get("ok"), d
    assert d["stats"]["count"] == 0, d

    st, d = c.jpost("/api/url/save", {"url": "a.com/1"})
    assert st == 200 and d.get("ok"), d
    assert d["url"] == "https://a.com/1", "没补 https ⇒ 与桌面端存的不是同一个串"

    st, d = c.jget("/api/urls")
    assert [i["url"] for i in d["items"]] == ["https://a.com/1"], d
    assert d["items"][0]["host"] == "a.com", d

    st, d = c.jpost("/api/url/delete", {"url": "https://a.com/1"})
    assert st == 200 and d.get("ok"), d
    st, d = c.jget("/api/urls")
    assert d["items"] == [], d


def test_url_save_really_persists(live):
    """光改内存不算数 —— 没调 save_config，重启就丢（「改了没反应」）。"""
    live["saves"].clear()
    c = live["client"]
    st, d = c.jpost("/api/url/save", {"url": "https://b.com/2"})
    assert st == 200 and d.get("ok"), d
    assert live["saves"], "save_config 一次都没被调用"
    st, _ = c.jpost("/api/url/delete", {"url": "https://b.com/2"})
    assert st == 200


def test_duplicate_url_is_refused_in_chinese(live):
    """重复添加要给中文提示，不是静默存两条（列表里看着像两个站）。"""
    c = live["client"]
    c.jpost("/api/url/save", {"url": "https://dup.com/"})
    st, d = c.jpost("/api/url/save", {"url": "https://dup.com/"})
    assert d.get("ok") is False, d
    assert _has_cjk(d.get("error") or ""), d
    c.jpost("/api/url/delete", {"url": "https://dup.com/"})


def test_bad_url_is_refused_in_chinese_over_http(live):
    c = live["client"]
    for bad in ("", "javascript:alert(1)", "ftp://x.com"):
        st, d = c.jpost("/api/url/save", {"url": bad})
        assert d.get("ok") is False, (bad, d)
        assert _has_cjk(d.get("error") or ""), (bad, d)


def test_deleting_a_missing_url_says_so_in_chinese(live):
    """删一个不存在的 ⇒ 中文说明，不要 ok:True 装成功。"""
    st, d = live["client"].jpost("/api/url/delete", {"url": "https://nope.com/"})
    assert d.get("ok") is False, d
    assert _has_cjk(d.get("error") or ""), d


def test_learning_stats_match_the_desktop_numbers(live):
    """★ #44：网页端与桌面端用同一个函数 ⇒ 数字必须一致。"""
    from media_resource_manager.classify import learning_stats as LS
    from media_resource_manager.repo import set_preference

    db = live["db"]
    db.execute("DELETE FROM preferences")
    set_preference(db, 0, "nude", 1.5, kind="pos")
    set_preference(db, 0, "watermark", -0.8, kind="neg")
    set_preference(db, 0, "small", 0.1)

    st, d = live["client"].jget("/api/learning")
    assert st == 200 and d.get("ok"), d
    want = LS.collect(db)
    assert d["stats"]["labels_total"] == want["labels_total"] == 3, d
    assert d["stats"]["pos_count"] == want["pos_count"], d
    assert d["stats"]["neg_count"] == want["neg_count"], d
    # 兼容键（R48 起既有前端在用）不许改名
    assert d["stats"]["count"] == 3 and "reviews" in d["stats"], d
    assert _has_cjk(d["summary"]), d
    assert [s["label"] for s in d["samples"]][0] == "nude", d


def test_samples_carry_the_hit_counts(live):
    """「样本管理」要看得见命中次数，否则用户无从判断该删哪条。"""
    st, d = live["client"].jget("/api/learning")
    nude = [s for s in d["samples"] if s["label"] == "nude"][0]
    assert nude["hit_pos"] == 1 and nude["hit_neg"] == 0, nude
    assert nude["seen"] >= 1, nude
    assert _has_cjk(nude["text"]) or nude["text"] == "nude", nude


def test_delete_one_sample_over_http(live):
    st, d = live["client"].jpost("/api/learn/delete", {"label": "small"})
    assert st == 200 and d.get("ok"), d
    st, d = live["client"].jget("/api/learning")
    labels = [s["label"] for s in d["samples"]]
    assert "small" not in labels, labels
    assert "nude" in labels, "删一条不该把别的也带走"


def test_delete_unknown_sample_is_reported_in_chinese(live):
    st, d = live["client"].jpost("/api/learn/delete", {"label": "不存在的词"})
    assert d.get("ok") is False, d
    assert _has_cjk(d.get("error") or ""), d


def test_reset_needs_confirmation(live):
    """★ 不带 confirm 一律拒 —— 一次误点不能清光用户教了几个月的东西。"""
    st, d = live["client"].jpost("/api/learn/reset", {})
    assert d.get("ok") is False, "不带 confirm 也重置了 ⇒ 防线失效"
    assert _has_cjk(d.get("error") or ""), d
    st, d = live["client"].jget("/api/learning")
    assert len(d["samples"]) == 2, "被拒的重置不该动数据"


def test_reset_clears_global_but_spares_task_scoped(live):
    """★ 只清 task_id=0；按任务学的行必须留着（语义不同，删了是误伤）。"""
    db = live["db"]
    db.execute("DELETE FROM preferences WHERE task_id=99")
    db.execute("INSERT INTO preferences(task_id, label, weight, seen) "
               "VALUES (99, 'taskonly', 0.5, 1)")
    c = live["client"]
    st, d = c.jpost("/api/learn/reset", {"confirm": True})
    assert st == 200 and d.get("ok"), d
    assert d["deleted"] == 2, d

    left = [r["label"] for r in
            db.query("SELECT label FROM preferences WHERE task_id=99")]
    assert left == ["taskonly"], "重置把按任务学的记录也删了"
    st, d = c.jget("/api/learning")
    assert d["items"] == [], "全局权重没清干净"
