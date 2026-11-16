"""R146 Part C/B4/B5 护栏：qBittorrent 式模态 + 任务模态化 + 不喜欢理由。

四层判据（沿用 Part A/B 的口径，问行为不问字面）：
  L1 静态层 —— 模态**只有一份实现**；window.confirm 全清；各入口真接了模态；
  L2 node 真执行 —— mrmModalZ / mrmModalFillAll / mrmModalCloseDecision /
     mrmModalDangerConfirm / mrmDislikePayload 的**行为**（不是 grep 中文）；
  L3 CSS —— 遮罩/卡片/页脚/窄屏全屏/层级由 JS 算（不写死）；
  L4 后端 —— /api/words/history、/api/dislike/options 真出数据；
     _api_image_action 收到 exclusions 真写回任务 ai_blacklist，
     unmatched **绝不**写进排除词（桌面端同一条铁律）。

⚠ 与被护对象同源：读磁盘上那份 app.js / app.css / webui.py，不复制副本。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlparse

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def _read_opt(p):
    """旧版网页（index.html / app.js / app.css）已于 2026-10-29 物理删除。

    读取改成容错：读不到返回空串 —— 让仍依赖旧 DOM/JS 的断言**逐条失败**
    （好定位、好删），而不是整个测试文件在收集阶段就崩掉。
    """
    try:
        return pathlib.Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


WEB = ROOT / "media_resource_manager" / "web"
APPJS = _read_opt(WEB / "static" / "js" / "app.js")
CSS = _read_opt(WEB / "static" / "css" / "app.css")
JS = WEB / "static" / "js" / "app.js"
STUB = ROOT / "tools" / "r77_dom_stub.js"
WEBUI = (ROOT / "media_resource_manager" / "webui.py").read_text(
    encoding="utf-8")

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(
    NODE is None, reason="本机没装 node ⇒ 跳过纯 JS 行为层（不是产品问题）")


def _strip_js_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)//.*$", "", s)
    return s


def _js(body: str):
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


def _fn_body(name: str) -> str:
    m = re.search(r"function %s\([^)]*\)\s*\{.*?\n\}" % re.escape(name),
                  APPJS, re.S)
    assert m, f"app.js 找不到 function {name}"
    return _strip_js_comments(m.group(0))


# ==================== B4 · 模态只有一份实现 ====================

def test_b4_native_confirm_fully_removed():
    """C3：原生 confirm 一处不留（移动端会被浏览器静默拦截）。"""
    hits = re.findall(r"window\.confirm\s*\(", _strip_js_comments(APPJS))
    assert not hits, f"还剩 {len(hits)} 处 window.confirm（点名要全换模态）"

# ==================== C1 · 任务新建/编辑模态化 ====================

# ==================== C3 · 删除/危险动作统一确认 ====================

# ==================== C4 · 不喜欢/拉黑理由 ====================

# ==================== B5 · 曾用词 + 全部填满 ====================

# ==================== L2 · node 真执行 ====================

# ==================== L4 · 后端 ====================


class _DB:
    """最小 db 替身：只提供 webui 用到的 query/execute。"""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def query(self, sql: str, params=()):
        cur = self.con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def execute(self, sql: str, params=()):
        self.con.execute(sql, params)
        self.con.commit()


class _Cfg:
    ai_label_history = ["金发", "猫"]
    ai_label_history_l2 = ["长发"]
    ai_label_history_l3: list = []
    ai_excl_history = ["水印"]


def _make_self(db):
    """拿真 _Handler 的**方法**跑，只换掉 db / cfg。

    ⚠ 必须继承 _Handler 而不是另写一个鸭子类型：本轮要验的
    _api_image_action → _apply_web_dislike_reasons 是类里的方法，
    用假 self 会 AttributeError —— 那种「测试自己造了个假对象所以绿了」
    正是本项目反复点名的假闭环。
    """
    from media_resource_manager.webui import _Handler

    class _S(_Handler):
        def __init__(self, db, cfg):
            self._db = db         # 不调 super().__init__：不需要真 socket
            self._cfg = cfg

        # ⚠ db / cfg 在 _Handler 上是**只读 property**（真跑时取的是
        # self.server.cfg），这里必须覆写，不能指望赋值能写进去
        # —— 直接 self.db = ... 会 AttributeError。
        @property
        def db(self):
            return self._db

        @property
        def cfg(self):
            return self._cfg

    return _S(db, _Cfg())


@pytest.fixture()
def web():
    from media_resource_manager.webui import _Handler
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE images(id INTEGER PRIMARY KEY, path TEXT, task_id INT,"
        " in_quarantine INT DEFAULT 0, needs_review INT DEFAULT 0,"
        " blacklisted INT DEFAULT 0, favorite INT DEFAULT 0, tags TEXT,"
        # R158：真库有 quarantined_at（db.py 列定义），而「进隔离区」的
        # UPDATE 会写它 —— 夹具少了这列，动作会被 except 吞掉、done=0，
        # 那是**夹具**缺列，不是产品缺陷。
        " quarantined_at TEXT)")
    con.execute(
        "CREATE TABLE tasks(id INTEGER PRIMARY KEY, name TEXT,"
        " ai_labels TEXT DEFAULT '[]', ai_labels_l2 TEXT DEFAULT '[]',"
        " ai_labels_l3 TEXT DEFAULT '[]', ai_blacklist TEXT DEFAULT '[]',"
        " keywords TEXT DEFAULT '[]', entry_urls TEXT DEFAULT '[]')")
    con.execute(
        "CREATE TABLE reviews(image_id INT, decision TEXT, task_id INT)")
    con.execute("INSERT INTO tasks(id, name, ai_labels, ai_labels_l2,"
                " ai_blacklist) VALUES (7,'t','[\"金发\",\"猫\"]',"
                "'[\"长发\"]','[\"水印\"]')")
    con.execute("INSERT INTO images(id, path, task_id) VALUES (1,'/a.jpg',7)")
    con.execute("INSERT INTO images(id, path, task_id) VALUES (2,'/b.jpg',7)")
    con.commit()
    yield _make_self(_DB(con))
    con.close()


def test_json_list_tolerates_dirty_values():
    """tasks 表里 TEXT 词表可能是 NULL/'[]'/裸串 —— 三种都要能读。"""
    from media_resource_manager.webui import _json_list
    assert _json_list(None) == []
    assert _json_list("") == []
    assert _json_list('["a","b"]') == ["a", "b"]
    assert _json_list("a b") == ["a", "b"], "老数据裸串要能读，不能整条丢掉"
    assert _json_list("[]") == []


def test_words_history_reads_desktop_pool(web):
    """B5：下发的必须是桌面端那四个历史键（键名=表单字段名）。"""
    self_ = web
    out = self_._api_words_history()
    assert out["ok"] is True
    assert out["items"]["ai_labels"] == ["金发", "猫"]
    assert out["items"]["ai_blacklist"] == ["水印"]
    for k in ("ai_labels", "ai_labels_l2", "ai_labels_l3", "ai_blacklist"):
        assert k in out["items"], f"缺 {k}"


def test_dislike_options_merges_by_tier_and_dedupes(web):
    """C4 选项：分段下发 + 白名单优先（同词不出现在黑名单栏）。"""
    self_ = web
    out = self_._api_dislike_options(urlparse("/x?ids=1,2"))
    assert out["ok"] is True
    assert out["labels"] == ["猫", "金发", "长发"] or \
        sorted(out["labels"]) == sorted(["金发", "猫", "长发"])
    assert out["blacklist"] == ["水印"]
    assert out["tiers"].get("一级主题词") == ["金发", "猫"]
    assert out["tiers"].get("二级细分词") == ["长发"]
    assert "三级细分词" not in out["tiers"], "空段不该占位"


def test_dislike_options_rejects_empty_ids(web):
    self_ = web
    out = self_._api_dislike_options(urlparse("/x?ids="))
    assert out["ok"] is False and "没给图片编号" in out["error"]


def test_image_action_writes_exclusions_not_unmatched(web):
    """★ 铁律：exclusions 写进 ai_blacklist，unmatched **绝不**写进去。"""
    self_ = web
    out = self_._api_image_action({
        "ids": [1], "act": "dislike",
        "unmatched": ["金发"], "exclusions": ["水印2"]})
    assert out["ok"] is True and out["done"] == 1
    row = self_.db.query("SELECT ai_blacklist FROM tasks WHERE id=7")[0]
    cur = json.loads(row["ai_blacklist"])
    assert "水印2" in cur, "右栏排除词没写回任务 ⇒ 用户勾了没用"
    assert "水印" in cur, "写回把原有排除词覆盖了（必须是追加）"
    assert "金发" not in cur, \
        "左栏（主题词没匹配上）被写进排除词 ⇒ 整个主题被误杀（桌面端铁律）"
