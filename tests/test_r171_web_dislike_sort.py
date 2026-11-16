"""R171 护栏：网页「不喜欢」弹窗右栏黑名单的排序与分组。

订单要点（2026-10-20 用户真机反馈）：
  1. 后端 blacklist 候选统一按**拼音升序**返回、去重；
  2. 前端右栏分「已学进库（全局学习）」/「本任务排除词」两组；
  3. 前端兜底再排一次（防后端旧缓存顺序）；
  4. 左栏主题词保留 tiers 分档 + 组内拼音排序；
  5. 每组组头有「全选 / 清空本组」。

判据统统盯**最终产物**：接口返回的名单顺序、前端渲染出的 DOM 分组与词序，
不盯"调了哪个函数"。
"""
from __future__ import annotations


import pathlib
import re
import sys
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _read_opt(p):
    """旧版网页（app.js / app.css）已于 2026-10-29 物理删除。

    读不到返回空串 —— 让依赖旧 JS 的断言逐条失败，而不是整个文件收集崩溃。
    """
    try:
        return pathlib.Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


APPJS = _read_opt(ROOT / "media_resource_manager" / "web" / "static" / "js"
                   / "app.js")
APPCSS = _read_opt(ROOT / "media_resource_manager" / "web" / "static" / "css"
                   / "app.css")
DOMSTUB = ROOT / "tests" / "_r171_dom_stub.js"


def _jsstr(p: str) -> str:
    import json as _json
    return _json.dumps(p)


# ---- node 真跑基础设施（R171：判据必须落在真跑出来的 DOM 树上）----

#: 前置：装最小 DOM 替身 + _dislikeCol 用到的 el 助手（真实现里来自别处，
#: 这里给够用的替身，避免为一个测试把整个 app.js 拉进来）。
_PROBE_PRELUDE = """
var _stub = require(%s);
_stub.install();
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) { e.className = cls; }
  if (text !== undefined && text !== null) { e.textContent = String(text); }
  return e;
}
""" % _jsstr(DOMSTUB.as_posix())


def _js(fn_names, probe: str):
    """装 DOM 替身 → 抽 app.js 里指定函数 → 真跑 probe → 解析最后一行 JSON。

    ⚠ 顺序不能反：`_stub.install()` 必须在**调用**这些函数之前跑（函数声明会
    提升，所以放后面也没关系），但 el/`_dislikeCheck` 这类依赖在调用时求值。
    """
    import json as _json
    import subprocess

    if isinstance(fn_names, str):
        fn_names = [fn_names]
    src = "\n".join(_fn_source(n) for n in fn_names)
    p = subprocess.run(["node", "-e", _PROBE_PRELUDE + src + "\n" + probe],
                       capture_output=True, text=True, timeout=60,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"node 跑 {fn_names} 失败：{p.stderr}"
    out = (p.stdout or "").strip().splitlines()
    assert out, f"node 没输出（{fn_names}）"
    return _json.loads(out[-1])


def _fn_source(name: str) -> str:
    """抽 app.js 里的顶层定义：``function f(...) {...}`` 或 ``var X = [...]``。"""
    m = re.search(r"^function %s\([^)]*\)\s*\{" % re.escape(name),
                  APPJS, re.M)
    if m:
        i = m.start()
        j = APPJS.index("\n}", i) + 2
        return re.sub(r"/\*.*?\*/", "", APPJS[i:j], flags=re.S)
    m = re.search(r"^var %s\s*=\s*\[" % re.escape(name), APPJS, re.M)
    if m:
        i = m.start()
        j = APPJS.index("\n];", i) + 3
        return re.sub(r"/\*.*?\*/", "", APPJS[i:j], flags=re.S)
    raise AssertionError(f"app.js 找不到定义 {name}")


def _fn_body(name: str) -> str:
    return re.sub(r"^\s*//.*$", "", _fn_source(name), flags=re.M)


def _node(probe: str) -> str:
    import subprocess

    p = subprocess.run(["node", "-e", probe], capture_output=True,
                       text=True, timeout=60, encoding="utf-8",
                       errors="replace")
    assert p.returncode == 0, p.stderr
    return (p.stdout or "").strip()


# ==================== 排序工具（media_resource_manager/zhsort.py） ============


def test_pinyin_order_is_real_pinyin_not_codepoint():
    """★ 核心判据：排序必须是**拼音序**，不是 Unicode 码点序。

    GB2312 一级汉字区按拼音编码 ⇒ 红(hong) 应排在 金(jin) 之前，
    而按 Unicode 码点 金(U+91D1) 排在 红(U+7EA2) 之后 —— 两者结论相反，
    所以这条断言真能区分"拼音排"和"没排"。
    """
    from media_resource_manager import zhsort

    words = ["金发", "红丝", "美丽", "可爱", "猫"]
    out = zhsort.sort_words(words)
    assert out == ["红丝", "金发", "可爱", "猫", "美丽"], out
    assert out != sorted(words), "结果与码点序相同 ⇒ 说明压根没按拼音排"


def test_pinyin_order_is_locale_independent():
    """排序结果不依赖进程 locale —— 否则同一份词表两台机器两种顺序。"""
    import locale as _locale

    from media_resource_manager import zhsort

    words = ["制服", "白丝", "和服", "黑丝", "旗袍"]
    before = zhsort.sort_words(words)
    try:
        _locale.setlocale(_locale.LC_COLLATE, "C")
        after = zhsort.sort_words(words)
    finally:
        try:
            _locale.setlocale(_locale.LC_COLLATE, "")
        except Exception:
            pass
    assert before == after, f"locale 影响排序：{before} != {after}"


def test_sort_words_dedupes_and_drops_blanks():
    from media_resource_manager import zhsort

    out = zhsort.sort_words(["水印", "金发", "水印", None, "", "  "])
    assert out == ["金发", "水印"], out


def test_sort_words_put_unencodable_to_tail_not_middle():
    """GB2312 编不出的字（emoji / 囧 这类后补字）归到末尾档，不插在拼音序中间。"""
    from media_resource_manager import zhsort

    out = zhsort.sort_words(["猫", "\U0001f600", "美丽", "囧"])
    assert out[:2] == ["猫", "美丽"], out
    assert set(out[2:]) == {"\U0001f600", "囧"}, out


# ==================== 后端 _api_dislike_options ====================


def _make_web(excl_history=None):
    """拿真 ``_Handler`` 的方法跑（只换 db / cfg）——与 test_r146_part_c 同型。

    ⚠ 不能用「假 self 鸭子类型」：`_api_dislike_options` 是类里的方法，读的是
    `self.db` / `self.cfg` 两个只读 property。另造一个假对象会 AttributeError，
    而"测试自己造了个假对象所以绿了"正是本项目反复点名的假闭环。
    """
    import sqlite3

    from media_resource_manager.webui import _Handler

    class _DB:
        def __init__(self, con):
            self.con = con

        def query(self, sql, params=()):
            cur = self.con.execute(sql, params)
            return cur.fetchall()

        def execute(self, sql, params=()):
            self.con.execute(sql, params)
            self.con.commit()

    class _Cfg:
        ai_excl_history = list(excl_history or [])

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE images(id INTEGER PRIMARY KEY, path TEXT, task_id INT,"
        " in_quarantine INT DEFAULT 0, needs_review INT DEFAULT 0,"
        " blacklisted INT DEFAULT 0, favorite INT DEFAULT 0, tags TEXT,"
        " quarantined_at TEXT)")
    con.execute(
        "CREATE TABLE tasks(id INTEGER PRIMARY KEY, name TEXT,"
        " ai_labels TEXT DEFAULT '[]', ai_labels_l2 TEXT DEFAULT '[]',"
        " ai_labels_l3 TEXT DEFAULT '[]', ai_blacklist TEXT DEFAULT '[]',"
        " keywords TEXT DEFAULT '[]', entry_urls TEXT DEFAULT '[]')")
    con.execute(
        "INSERT INTO tasks(id, name, ai_labels, ai_labels_l2, ai_labels_l3,"
        " ai_blacklist) VALUES (7,'t','[\"金发\",\"猫\"]','[\"长发\"]',"
        "'[]','[\"红丝\",\"水印\"]')")
    con.execute("INSERT INTO images(id, path, task_id) VALUES (1,'/a.jpg',7)")
    con.commit()

    class _S(_Handler):
        def __init__(self, db, cfg):
            self._db = db
            self._cfg = cfg

        @property
        def db(self):
            return self._db

        @property
        def cfg(self):
            return self._cfg

    return _S(_DB(con), _Cfg())


def test_backend_blacklist_is_pinyin_sorted():
    """R171-1：接口返回的 blacklist 按拼音升序（红丝 hong < 水印 shui）。

    ⚠ 素材刻意选**拼音序与码点序相反**的一对：红(U+7EA2) 在 水(U+6C34) 之后，
    所以按拼音排是「红丝/水印」、退化成码点序是「水印/红丝」—— 两者可分。
    """
    out = _make_web()._api_dislike_options(urlparse("/x?ids=1"))
    assert out["ok"] is True
    assert out["blacklist"] == ["红丝", "水印"], out["blacklist"]
    assert out["blacklist"] != sorted(["红丝", "水印"]),         "退回了码点序 ⇒ 没按拼音排（码点序是 水印/红丝）"


def test_backend_labels_and_tiers_are_pinyin_sorted():
    """R171-4：左栏主题词（L1+L2+L3 并集）与其 tiers 分档一并按拼音排。

    长(zhang) 金(jin) 猫(mao) —— GB2312 里 长 < 金 < 猫，与拼音序一致，
    与码点序（金U+91D1 < 长U+957F）相反 ⇒ 本断言能区分"排了"和"没排"。
    """
    out = _make_web()._api_dislike_options(urlparse("/x?ids=1"))
    assert out["labels"] == ["长发", "金发", "猫"], out["labels"]
    assert out["labels"] != sorted(out["labels"]), "退回了码点序 ⇒ 没按拼音排"
    assert out["tiers"]["一级主题词"] == ["金发", "猫"]
    assert out["tiers"]["二级细分词"] == ["长发"]


def test_backend_returns_two_blacklist_groups():
    """R171-2：右栏两组 —— 本任务排除词 / 已学进库（全局曾用词池）。"""
    out = _make_web(["和服", "水印", "白丝"])._api_dislike_options(
        urlparse("/x?ids=1"))
    g = out["blacklist_groups"]
    # 水印 同时在任务与全局池里 ⇒ 只归「本任务排除词」，不重复出现
    assert g["task"] == ["红丝", "水印"], g
    assert g["learned"] == ["白丝", "和服"], g
    assert not (set(g["task"]) & set(g["learned"])), \
        "同一个词出现在两组 ⇒ 用户在弹窗里看到重复两条"


def test_backend_groups_exclude_theme_words():
    """铁律：主题词（白名单）绝不进任何一组排除词。"""
    out = _make_web(["金发", "猫", "和服"])._api_dislike_options(
        urlparse("/x?ids=1"))
    g = out["blacklist_groups"]
    for k in ("task", "learned"):
        assert "金发" not in g[k] and "猫" not in g[k], \
            f"{k} 混进主题词 ⇒ 误杀整个主题"
    assert "金发" not in out["blacklist"] and "猫" not in out["blacklist"]


def test_backend_keeps_legacy_keys_for_old_frontend():
    """老前端只认 labels/blacklist/tiers —— 三个旧键不许因为加分组就消失。"""
    out = _make_web()._api_dislike_options(urlparse("/x?ids=1"))
    for k in ("labels", "blacklist", "tiers"):
        assert k in out, f"旧键 {k} 被删 ⇒ 老前端拿不到候选词"
    assert sorted(out["blacklist_groups"]) == ["learned", "task"]


def test_backend_empty_ids_still_rejects():
    out = _make_web()._api_dislike_options(urlparse("/x?ids="))
    assert out["ok"] is False and "没给图片编号" in out["error"]


# ==================== 前端渲染 ====================
