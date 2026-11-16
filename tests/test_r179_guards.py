"""R179 / R158-Bug1·2·3：网页端「点了没效果」与「整页刷新丢位置」。

用户真机报的三个缺陷：
  · Bug2 网页图库点「不喜欢」→ 图还在图库（后端只清 needs_review，
    而图库里的图 needs_review 本来就是 0 ⇒ 那条 UPDATE 是空操作）；
  · Bug3 网页隔离区点「审核通过」→ 图还在隔离区（隔离区的标志是
    in_quarantine，后端从不碰它）；
  · Bug1 任何动作成功后 `loadView(true)` 整页重拉 ⇒ 滚动位置必丢。

本文件两条铁律（与既有 Web 测试同口径）：
  ① 判据盯**最终双标志**，不盯"调了哪个函数" —— 只验调用了什么，
     实现改回空操作照样绿（R170 变异漏过的同型坑）。
  ② 前端判据用 **node 真跑**纯函数，不做"源码里有这个字符串"的空壳断言。
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

from media_resource_manager import webui
from media_resource_manager.db import Database

APPJS = (Path(__file__).resolve().parents[1]
         / "media_resource_manager" / "web" / "static" / "js" / "app.js")

# 三个视图的取数口径（与 webui._api_images / 桌面端 quarantine_manager 同源）
GALLERY_WHERE = "in_quarantine=0 AND blacklisted=0"
QUAR_WHERE = "in_quarantine=1 AND needs_review=0"
PENDING_WHERE = "in_quarantine=1 AND needs_review=1"


class _Server:
    """最小 server 替身：只提供 queue 属性。"""

    class _Q:
        def __init__(self):
            self.started, self.stopped = [], []

        def start_task(self, tid):
            self.started.append(tid)

        def stop_task(self, tid):
            self.stopped.append(tid)

    def __init__(self):
        self.queue = self._Q()


class H(webui._Handler):
    """只测业务方法：不走真 socket（与 test_r48_web_features 同款）。"""

    _cfg = None
    _db = None
    _headers = None
    _path = "/api/x"

    def __init__(self, cfg, db, server=None, headers=None):
        self._cfg = cfg
        self._db = db
        self.server = server or _Server()
        self._headers = headers or {}
        self._sent = None

    @property
    def cfg(self):
        return self._cfg

    @property
    def db(self):
        return self._db

    @property
    def headers(self):
        return self._headers

    @property
    def path(self):
        return self._path

    def _send(self, code, body, ctype, extra=None):
        self._sent = (code, body, ctype, extra or {})
        return self._sent

    def _json(self, obj, code=200, extra=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return self._send(code, raw, "application/json; charset=utf-8", extra)

    def json(self):
        return json.loads(self._sent[1].decode("utf-8"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """真 Database（临时文件）+ 免落盘的 cfg + handler。"""
    from media_resource_manager.config import AppConfig

    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"

    import media_resource_manager.config as cfgmod
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)

    h = H(cfg, db)
    yield h, cfg, db
    try:
        db.close()
    except Exception:
        pass


_SEQ = [0]


def _tid(h):
    """images.task_id 是 NOT NULL ⇒ 夹具必须真有一条任务。"""
    if getattr(h, "_r179_tid", None):
        return h._r179_tid
    r = h._api_task_save({"name": "r179", "adapter_name": "",
                          "entry_urls": ["https://example.com/"]})
    h._r179_tid = r.get("task_id") or 1
    return h._r179_tid


def _mk(h, **kw):
    """插一张图，返回 id。默认 = 图库里的普通图（两标志皆 0）。"""
    _SEQ[0] += 1
    cols = {"path": f"r179_{_SEQ[0]}.jpg", "task_id": _tid(h),
            "source_url": f"https://example.com/{_SEQ[0]}.jpg",
            "needs_review": 0, "in_quarantine": 0, "blacklisted": 0,
            "width": 8, "height": 8}
    cols.update(kw)
    keys = ",".join(cols)
    marks = ",".join("?" * len(cols))
    h.db.execute(f"INSERT INTO images({keys}) VALUES({marks})",
                 tuple(cols.values()))
    return h.db.query("SELECT id FROM images ORDER BY id DESC LIMIT 1")[0]["id"]


def _flags(db, iid):
    r = db.query("SELECT in_quarantine, needs_review, blacklisted, "
                 "quarantined_at FROM images WHERE id=?", (iid,))
    if not r:
        # R164-Bug1 起「隔离区里 delete」= 真删除：图片行**真的没了**。
        # 老写法 [0] 直接 IndexError —— 那是仪器问题不是产品问题；但也绝不能
        # 把「行没了」默默当成通过（那就等于放行软删回退），所以这里显式标
        # gone，由调用方按 (起点, 动作) 逐格钉死该不该消失。
        return {"gone": True, "in_quarantine": 0, "needs_review": 0,
                "blacklisted": 0, "quarantined_at": None}
    r = r[0]
    return {"gone": False,
            "in_quarantine": int(r["in_quarantine"] or 0),
            "needs_review": int(r["needs_review"] or 0),
            "blacklisted": int(r["blacklisted"] or 0),
            "quarantined_at": r["quarantined_at"]}


def _in(db, where, iid):
    n = db.query(f"SELECT COUNT(*) c FROM images WHERE {where} AND id=?",
                 (iid,))[0]["c"]
    return int(n or 0) == 1


# ================================================== 纯函数：语义表

def test_sets_like_releases_quarantine():
    """like 必须同时清 in_quarantine（桌面端 _do_apply 同口径）。"""
    s = webui.image_action_sets("like", True)
    assert "in_quarantine=0" in s
    assert "needs_review=0" in s


def test_sets_dislike_enters_quarantine():
    """dislike 必须置 in_quarantine=1（否则图留在图库 = Bug2）。"""
    s = webui.image_action_sets("dislike", False)
    assert "in_quarantine=1" in s
    assert "needs_review=0" in s


def test_sets_dislike_on_quarantined_keeps_timestamp():
    """已在隔离区再点不喜欢：不得刷新 quarantined_at（伪造隔离时间）。"""
    assert "quarantined_at" not in webui.image_action_sets("dislike", True)


def test_sets_blacklist_also_isolates():
    s = webui.image_action_sets("blacklist", False)
    assert "blacklisted=1" in s and "in_quarantine=1" in s


def test_sets_blacklist_on_quarantined_is_idempotent():
    s = webui.image_action_sets("blacklist", True)
    assert "blacklisted=1" in s
    assert "in_quarantine=1" not in s      # 已经在里面了，别改归属


# ================================================== 后端：真实最终态

def test_bug2_gallery_dislike_leaves_gallery(env):
    """Bug2 主判据：图库点不喜欢 → 图**真的不在图库了**，且进了隔离区。"""
    h, _cfg, db = env
    iid = _mk(h)
    assert _in(db, GALLERY_WHERE, iid), "夹具：这张图原本应在图库"
    r = h._api_image_action({"ids": [iid], "act": "dislike"})
    assert r["ok"] is True and r["done"] == 1, r
    assert not _in(db, GALLERY_WHERE, iid), \
        "点完不喜欢，图还在图库 —— Bug2 复发（后端没置 in_quarantine）"
    assert _in(db, QUAR_WHERE, iid), "不喜欢后没进隔离区（桌面端口径是隔离）"


def test_bug3_quarantine_like_returns_to_gallery(env):
    """Bug3 主判据：隔离区点审核通过 → 图回到图库，不在隔离区。"""
    h, _cfg, db = env
    iid = _mk(h, in_quarantine=1, needs_review=0,
              quarantined_at="2020-01-01 00:00:00")
    assert _in(db, QUAR_WHERE, iid), "夹具：这张图原本应在隔离区"
    r = h._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] is True and r["done"] == 1, r
    assert _flags(db, iid)["in_quarantine"] == 0, \
        "审核通过没清 in_quarantine —— Bug3 复发（图永远出不了隔离区）"
    assert _in(db, GALLERY_WHERE, iid), "出隔离区后应回到图库"


def test_pending_like_clears_both_flags(env):
    """待审核（双标志）点通过 → 两个标志都清，不能留在队列里。"""
    h, _cfg, db = env
    iid = _mk(h, in_quarantine=1, needs_review=1)
    r = h._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] is True, r
    f = _flags(db, iid)
    assert (f["in_quarantine"], f["needs_review"]) == (0, 0), f


def test_pending_dislike_lands_in_quarantine_only(env):
    """待审核点拒绝 → 进隔离区，且**不再**是待审核。"""
    h, _cfg, db = env
    iid = _mk(h, in_quarantine=1, needs_review=1)
    h._api_image_action({"ids": [iid], "act": "dislike"})
    assert _in(db, QUAR_WHERE, iid)
    assert not _in(db, PENDING_WHERE, iid)


def test_blacklist_from_gallery_isolates(env):
    """拉黑 = 既打黑名单标记，也移出图库（桌面端 BLACKLIST 同口径）。"""
    h, _cfg, db = env
    iid = _mk(h)
    h._api_image_action({"ids": [iid], "act": "blacklist"})
    f = _flags(db, iid)
    assert f["blacklisted"] == 1 and f["in_quarantine"] == 1, f
    assert not _in(db, GALLERY_WHERE, iid), "拉黑了还留在图库"


def test_favorite_does_not_change_ownership(env):
    """收藏不该改归属：图还在图库，隔离区/待审核的也不该被挪出来。"""
    h, _cfg, db = env
    a = _mk(h)
    b = _mk(h, in_quarantine=1, needs_review=0)
    h._api_image_action({"ids": [a, b], "act": "favorite"})
    assert _in(db, GALLERY_WHERE, a), "收藏把图从图库弄没了"
    assert _in(db, QUAR_WHERE, b), "收藏把隔离区的图放出来了"


def test_delete_still_isolates_and_clears_review(env):
    """删除（R143 既有契约）不得被本次改动带偏。"""
    h, _cfg, db = env
    iid = _mk(h, needs_review=1)
    h._api_image_action({"ids": [iid], "act": "delete"})
    f = _flags(db, iid)
    assert (f["in_quarantine"], f["needs_review"]) == (1, 0), f


def test_no_action_leaves_image_in_both_views(env):
    """互斥：任一动作之后，都不该出现"既是待审核又在隔离区"的行。

    这是 R159 担心的「同一个东西出现在两页」的**可控**部分 ——
    不做数据迁移（那会清空整个待审核队列，见 last_run_report R176 取证），
    但写入侧必须保证自己不再造出双标志行。

    ⚠ R188 改判（不是放宽，是把断言补到新契约上）：
    R164-Bug1 之后「隔离区里点 delete」= **真删除**（行消失），老判据一律
    要求「行还在」⇒ 那一格恒红。这里改成：先照旧查双标志，再**逐格**钉死
    「哪几格该消失、哪几格必须在」。变异自检：把 webui 的 was_q 硬删分支
    去掉 ⇒ `(1,0)/delete` 与 `(1,1)/delete` 两格不再 gone ⇒ 立刻红。
    """
    h, _cfg, db = env
    cases = []
    for start in ((0, 0), (1, 0), (1, 1)):
        for act in ("like", "dislike", "blacklist", "delete"):
            iid = _mk(h, in_quarantine=start[0], needs_review=start[1])
            h._api_image_action({"ids": [iid], "act": act})
            cases.append((start, act, _flags(db, iid)))
    bad = [c for c in cases
           if c[2]["in_quarantine"] == 1 and c[2]["needs_review"] == 1]
    assert not bad, f"这些动作留下了双标志行（会同时出现在两页）：{bad}"

    # ① 只有「起点在隔离区 + delete」这两格允许把行删掉
    purged = sorted((s, a) for s, a, f in cases if f["gone"])
    assert purged == [((1, 0), "delete"), ((1, 1), "delete")], (
        f"只有「起点 in_quarantine=1 + delete」才该真删除，实际消失的是 "
        f"{purged}（多删=误杀、少删=软删回退）")

    # ② 起点在图库/待审核的 delete 仍是「进隔离区可还原」—— 行必须在，
    #    且正好落在隔离区（in_quarantine=1 且 needs_review=0）
    for s, a, f in cases:
        if a == "delete" and s[0] == 0:
            assert not f["gone"], f"图库起点 delete 不该真删除：{s}"
            assert (f["in_quarantine"], f["needs_review"]) == (1, 0), (
                f"图库起点 delete 应进隔离区并清待审核，实际 {f}")


def test_action_still_writes_review_record(env):
    """改归属不能把审核记录改没（r50 既有契约）。"""
    h, _cfg, db = env
    iid = _mk(h)
    h._api_image_action({"ids": [iid], "act": "dislike"})
    assert db.query("SELECT decision FROM reviews WHERE image_id=?", (iid,))


def test_missing_id_is_not_counted(env):
    """不存在的 id 不冒充满分（既有口径，别被批量循环吞掉）。"""
    h, _cfg, db = env
    iid = _mk(h)
    r = h._api_image_action({"ids": [iid, 999999], "act": "dislike"})
    assert r["done"] == 1, r


# ================================================== 前端：node 真跑

def _node(probe: str) -> str:
    out = subprocess.run(["node", "-e", probe], capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _jsfunc(js: str, name: str) -> str:
    i = js.index(f"function {name}")
    return js[i:js.index("\n}", i) + 2]

def _fire_body() -> str:
    """取 fireImageAction 函数体（剥注释后的净代码）。"""
    js = APPJS.read_text(encoding="utf-8")
    i = js.index("function fireImageAction")
    j = js.index("\nfunction ", i + 10)
    body = js[i:j]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return body
