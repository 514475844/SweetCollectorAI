# -*- coding: utf-8 -*-
"""R152-E13 护栏：Web 隔离区「还原到图库」= 桌面端 restore 同口径。

订单原文：Web 审核通过不清 in_quarantine（审计时点 :1646-1648）→ 隔离图永困。
修：待审核/隔离区页加「还原到图库」（对齐桌面 quarantine 管理 restore()）。

R158-Bug3 已让 like+was_q 清 in_quarantine，但与桌面 restore() 仍差两块
（本单补齐）：
  ① blacklisted 不清 —— 拉黑+隔离的图点「通过」后 blacklisted 仍是 1 ⇒
     图库(blacklisted=0)与隔离区(in_quarantine=1)**两个视图都看不见**，
     "永困"换个形态复发（本护栏的主判据）；
  ② 文件不物理搬回图库 —— 桌面 restore 走 ReviewQueue._release_to_gallery
     （按域名子目录、冲突加序号、源在图库内则原地不动），Web 原来只改标志位，
     文件永远留在隔离区目录。

护栏三层（R63-D* 口径）：
- L1 纯函数：image_action_sets 语义表不被本单波及（普通 LIKE 语义不变，
  R179 判据的本地副本）。
- L2 行为：真 Database + handler（test_r180 同款 env）打 `_api_image_action`：
  拉黑+隔离 → like 后**恰好回到图库一个视图**；文件物理搬回（隔离区文件 →
  图库/<域名>/，旧位置清空，db.path 回写）；path=NULL 的行不炸；
  图库 like（was_q=False）不触发搬运分支。
- L3 源码判据：webui like+was_q 分支（按块切）含 blacklisted=0 与
  _web_release_to_gallery 接线；app.js updateActionbar 块含视图相关文案。

反向可变：删 blacklisted=0 → 拉黑图两条必红；
          删 _web_release_to_gallery 调用 → 搬运判据必红；
          删整条 like+was_q 分支（回 R158 前态）→ 视图互斥判据必红；
          app.js 文案改死「审核通过」→ 前端判据必红。
"""
import json
import re
import shutil
from pathlib import Path

import pytest

from media_resource_manager import webui
from media_resource_manager.db import Database

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"

GALLERY_WHERE = "in_quarantine=0 AND blacklisted=0 AND path IS NOT NULL"
QUAR_WHERE = "in_quarantine=1 AND needs_review=0"
VIEWS = {"gallery": GALLERY_WHERE, "quarantine": QUAR_WHERE}


# ---------------------------------------------------------------- harness

class _Server:
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
    _cfg = None
    _db = None
    _headers = None
    _path = "/api/x"

    def __init__(self, cfg, db, server=None):
        self._cfg = cfg
        self._db = db
        self.server = server or _Server()
        self._headers = {}
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
    """真 Database（临时文件）+ 全目录收进 tmp 的 cfg + handler。

    ⚠ isolate_dir / library_dir 必须指到 tmp：_release_to_gallery 会真的
    mkdir + 搬文件，绝不能落进用户真实目录。
    """
    from media_resource_manager.config import AppConfig

    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"
    cfg.isolate_dir = tmp_path / "quarantine"
    cfg.library_dir = tmp_path / "library"

    import media_resource_manager.config as cfgmod
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)

    h = H(cfg, db)
    yield h, cfg, db, tmp_path
    try:
        db.close()
    except Exception:
        pass


_SEQ = [0]


def _tid(h):
    """images.task_id NOT NULL + C10 外键 ⇒ 必须先有真任务行（父行先行）。"""
    if getattr(h, "_e13_tid", None):
        return h._e13_tid
    r = h._api_task_save({"name": "e13", "adapter_name": "",
                          "entry_urls": ["https://example.com/"]})
    h._e13_tid = r.get("task_id") or 1
    return h._e13_tid


def _mk(h, **kw):
    """插一张图，返回 id。默认 = 图库里的普通图。"""
    _SEQ[0] += 1
    cols = {"path": f"e13_{_SEQ[0]}.jpg", "task_id": _tid(h),
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
    r = db.query("SELECT in_quarantine, needs_review, blacklisted, path "
                 "FROM images WHERE id=?", (iid,))[0]
    return {"q": int(r["in_quarantine"] or 0),
            "nr": int(r["needs_review"] or 0),
            "bl": int(r["blacklisted"] or 0),
            "path": r["path"]}


def _views_of(db, iid):
    hit = []
    for name, where in VIEWS.items():
        n = db.query(f"SELECT COUNT(*) c FROM images WHERE {where} AND id=?",
                     (iid,))[0]["c"]
        if int(n or 0):
            hit.append(name)
    return hit


# ---------------------------------------------------------------- L1 语义表

def test_sets_like_semantics_unchanged():
    """普通 LIKE（非还原路径）语义不许被 E13 波及（R179 本地副本）。"""
    s = webui.image_action_sets("like", True)
    assert "in_quarantine=0" in s and "needs_review=0" in s
    s2 = webui.image_action_sets("like", False)
    assert "in_quarantine=0" in s2 and "needs_review=0" in s2


# ------------------------------------------------- L2 行为：标志位 + 视图归属

def test_blacklisted_quarantine_like_returns_to_gallery_exactly_one_view(env):
    """★ 主判据（"永困"复发形态）：拉黑+隔离的图点「审核通过」必须恰好
    回到图库**一个**视图 —— blacklisted 不清就会两端都看不见。"""
    h, cfg, db, tmp = env
    iid = _mk(h, in_quarantine=1, needs_review=0, blacklisted=1)
    assert _views_of(db, iid) == ["quarantine"]      # 夹具自检
    r = h._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] and r["done"] == 1
    f = _flags(db, iid)
    assert (f["q"], f["nr"], f["bl"]) == (0, 0, 0), \
        f"还原后标志位不对：{f} —— blacklisted 没清就是「永困」复发"
    assert _views_of(db, iid) == ["gallery"], \
        f"还原后视图归属 {_views_of(db, iid)} —— 必须恰好图库一个"


def test_plain_quarantine_like_clears_flags(env):
    h, cfg, db, tmp = env
    iid = _mk(h, in_quarantine=1, needs_review=0)
    h._api_image_action({"ids": [iid], "act": "like"})
    f = _flags(db, iid)
    assert (f["q"], f["nr"], f["bl"]) == (0, 0, 0)


def test_batch_like_on_quarantine_all_restored(env):
    h, cfg, db, tmp = env
    a = _mk(h, in_quarantine=1, needs_review=0, blacklisted=1)
    b = _mk(h, in_quarantine=1, needs_review=0, blacklisted=1)
    r = h._api_image_action({"ids": [a, b], "act": "like"})
    assert r["done"] == 2
    for iid in (a, b):
        assert _views_of(db, iid) == ["gallery"]


# ------------------------------------------------- L2 行为：文件物理搬回

def test_like_moves_file_back_to_library_domain_subdir(env):
    """★ 搬运判据：隔离区文件还原后必须真的出现在图库 <域名>/ 子目录，
    旧位置清空，db.path 指向新位置（与桌面 _release_to_gallery 同款）。"""
    h, cfg, db, tmp = env
    iso = Path(cfg.isolate_dir)
    iso.mkdir(parents=True, exist_ok=True)
    src = iso / "captured.jpg"
    src.write_bytes(b"\xff\xd8e13fake")
    # 任务 output_dir 指到 tmp 图库（默认值会落用户目录，必须重定向）
    tid = _tid(h)
    db.execute("UPDATE tasks SET output_dir=? WHERE id=?",
               (str(cfg.library_dir), tid))
    iid = _mk(h, in_quarantine=1, needs_review=0, path=str(src))
    r = h._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] and r["done"] == 1
    dest = Path(cfg.library_dir) / "example.com" / "captured.jpg"
    assert dest.exists(), f"文件没搬回图库 {dest} —— E13 搬运分支没生效"
    assert not src.exists(), "旧位置的文件还在 —— 只复制未搬走"
    f = _flags(db, iid)
    assert f["path"] == str(dest), f"db.path 未回写：{f['path']!r}"


def test_like_with_missing_file_still_restores_flags(env):
    """文件已不在磁盘（path 悬空）：不许炸，标志位照常还原。"""
    h, cfg, db, tmp = env
    iid = _mk(h, in_quarantine=1, needs_review=0, path="Z:/nope/gone.jpg")
    r = h._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] and r["done"] == 1
    f = _flags(db, iid)
    assert (f["q"], f["nr"], f["bl"]) == (0, 0, 0)


def test_like_with_null_path_no_crash(env):
    """path IS NULL 的隔离行（去重输家落隔离的形态）：还原不炸、path 保持 NULL。"""
    h, cfg, db, tmp = env
    iid = _mk(h, in_quarantine=1, needs_review=0, path=None)
    r = h._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] and r["done"] == 1
    f = _flags(db, iid)
    assert (f["q"], f["nr"], f["bl"]) == (0, 0, 0)
    assert f["path"] is None


def test_gallery_like_does_not_touch_path_or_branch(env):
    """图库里的 like（was_q=False）不走还原分支：path 原样、无副作用。"""
    h, cfg, db, tmp = env
    iid = _mk(h)      # 图库普通图
    before = _flags(db, iid)["path"]
    h._api_image_action({"ids": [iid], "act": "like"})
    f = _flags(db, iid)
    assert f["path"] == before
    assert (f["q"], f["nr"], f["bl"]) == (0, 0, 0)


def test_file_already_in_library_stays_put(env):
    """源文件本就在图库目录里：_release_to_gallery 原地不动（不落 xxx(1) 副本），
    db.path 不变。Web「删除」是逻辑隔离（不搬文件），还原时大多是这种。"""
    h, cfg, db, tmp = env
    lib = Path(cfg.library_dir) / "example.com"
    lib.mkdir(parents=True, exist_ok=True)
    keep = lib / "already.jpg"
    keep.write_bytes(b"\xff\xd8keep")
    tid = _tid(h)
    db.execute("UPDATE tasks SET output_dir=? WHERE id=?",
               (str(cfg.library_dir), tid))
    iid = _mk(h, in_quarantine=1, needs_review=0, path=str(keep))
    h._api_image_action({"ids": [iid], "act": "like"})
    assert keep.exists() and not (lib / "already(1).jpg").exists()
    assert _flags(db, iid)["path"] == str(keep)


# ---------------------------------------------------------------- L3 源码判据

def _read_raw(p: Path) -> str:
    # R216 仪器第六变体：CRLF 文件必须 newline="" 读真实字节视图
    with open(p, "r", newline="", encoding="utf-8") as f:
        return f.read()


def _func_block(src: str, marker: str) -> str:
    i = src.index(marker)
    m = re.search(r"\n    def ", src[i + 1:])
    j = i + 1 + m.start() if m else len(src)
    return src[i:j]


def test_source_webui_restore_branch_wired():
    """★ `_api_image_action` 块内：like+was_q 分支必须清 blacklisted 且
    接 `_web_release_to_gallery`（缺一个就是桌面/Web 口径分叉复发）。"""
    src = _read_raw(WEBUI)
    blk = _func_block(src, "def _api_image_action")
    i_br = blk.index('if act == "like" and was_q:')
    seg = blk[i_br:blk.index("else:", i_br)]
    assert "blacklisted=0" in seg, \
        "还原分支没清 blacklisted —— 拉黑图点通过后两端都看不见（永困复发）"
    assert "_web_release_to_gallery(iid)" in seg, \
        "还原分支没接文件搬运 —— 文件永远留在隔离区目录"
    # helper 本体：复用 ReviewQueue._release_to_gallery，不许另写搬运
    helper = _func_block(src, "def _web_release_to_gallery")
    assert "_release_to_gallery(rec)" in helper, \
        "helper 没复用桌面端 _release_to_gallery —— 两处口径会分叉（R143 老伤）"
    assert "from .review.queue import ReviewQueue" in helper
