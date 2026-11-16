"""R180 护栏：R159 隔离区/待审核**互斥硬约束** + R158-Bug4 桌面隔离区多选与网格。

两条铁律（沿用本仓库既有 Web/GUI 测试的口径）：
  ① 判据盯**最终双标志**与**视图归属**，不盯"调了哪个函数"——
     只验调用了什么，实现改回空操作照样绿（R170 变异漏过的同型坑）。
  ② 隔离区对话框**不能真 exec**（``dlg.exec()`` 会阻塞）⇒ 多选/网格这类
     交互只能落到**模块级纯函数**上验（R141 教训：判据函数放实例方法上，
     纯 Python 替身会 AttributeError 被吞 ⇒ 假绿）。接线部分用源码判据补。

数据来源（真实口径，不是本文件自创）：
  · gallery   : in_quarantine=0 AND blacklisted=0
  · quarantine: in_quarantine=1 AND needs_review=0
  · pending   : in_quarantine=1 AND needs_review=1
  与 ``review/queue.py::pending()``、桌面端 ``quarantine_manager`` 的
  ``_QUAR_WHERE`` 逐字一致。
"""
import json
import re
from pathlib import Path

import pytest

from media_resource_manager import webui
from media_resource_manager.db import Database

MAIN_PY = (Path(__file__).resolve().parents[1]
           / "media_resource_manager" / "gui" / "main_window.py")

GALLERY_WHERE = "in_quarantine=0 AND blacklisted=0"
QUAR_WHERE = "in_quarantine=1 AND needs_review=0"
PENDING_WHERE = "in_quarantine=1 AND needs_review=1"
VIEWS = {"gallery": GALLERY_WHERE, "quarantine": QUAR_WHERE,
         "pending": PENDING_WHERE}


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
    if getattr(h, "_r180_tid", None):
        return h._r180_tid
    r = h._api_task_save({"name": "r180", "adapter_name": "",
                          "entry_urls": ["https://example.com/"]})
    h._r180_tid = r.get("task_id") or 1
    return h._r180_tid


def _mk(h, **kw):
    """插一张图，返回 id。默认 = 图库里的普通图（两标志皆 0）。"""
    _SEQ[0] += 1
    cols = {"path": f"r180_{_SEQ[0]}.jpg", "task_id": _tid(h),
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
                 "quarantined_at FROM images WHERE id=?", (iid,))[0]
    return {"q": int(r["in_quarantine"] or 0),
            "nr": int(r["needs_review"] or 0),
            "bl": int(r["blacklisted"] or 0),
            "at": r["quarantined_at"]}


def _views_of(db, iid):
    """这张图当前出现在**哪几个**视图里（正常必须恰好一个）。"""
    hit = []
    for name, where in VIEWS.items():
        n = db.query(f"SELECT COUNT(*) c FROM images WHERE {where} AND id=?",
                     (iid,))[0]["c"]
        if int(n or 0):
            hit.append(name)
    return hit


# ---------------------------------------------------------------------------
# R159：互斥硬约束
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("act", ["like", "dislike", "blacklist"])
@pytest.mark.parametrize("was_q", [True, False])
def test_action_always_clears_needs_review(act, was_q):
    """任何审核动作后都必须出待审核队列。

    R159 的核心：三个动作都是"这件事已经定了"，留着 needs_review=1 就会
    「点了通过还占着待审核」，也是两个视图重复显示同一张图的来源。
    """
    clause = webui.image_action_sets(act, was_q)
    assert "needs_review=0" in clause


@pytest.mark.parametrize("act", ["like", "dislike", "blacklist"])
@pytest.mark.parametrize("start", ["gallery", "quarantine", "pending"])
def test_after_action_image_lives_in_exactly_one_view(env, act, start):
    """R159 第 3/4 条：一张图**永远只属于一个视图**（互斥）。

    这是"用户在两个页面看到同一张图"的直接判据 —— 比逐条比对 SQL 片段
    更硬：谁把 needs_review 漏掉，这里立刻从 1 个视图变成 2 个。
    """
    h, cfg, db = env
    iid = _mk(h, **{"gallery": {},
                    "quarantine": {"in_quarantine": 1},
                    "pending": {"in_quarantine": 1, "needs_review": 1}}[start])
    assert _views_of(db, iid) == [start]     # 夹具自检：起点必须干净
    h._api_image_action({"ids": [iid], "act": act})
    assert len(_views_of(db, iid)) == 1


def test_dislike_from_gallery_moves_into_quarantine(env):
    """R158-Bug2：图库点「不喜欢」必须真的离开图库（原来那条 UPDATE 是空操作）。"""
    h, cfg, db = env
    iid = _mk(h)                                   # 图库：q=0, nr=0
    h._api_image_action({"ids": [iid], "act": "dislike"})
    assert _views_of(db, iid) == ["quarantine"]


def test_like_from_quarantine_returns_to_gallery(env):
    """R158-Bug3：隔离区点「审核通过」必须真的离开隔离区（原来从不清 in_quarantine）。"""
    h, cfg, db = env
    iid = _mk(h, in_quarantine=1)
    h._api_image_action({"ids": [iid], "act": "like"})
    assert _views_of(db, iid) == ["gallery"]


def test_like_from_pending_returns_to_gallery(env):
    """待审核点通过：既出待审核也出隔离区 ⇒ 落回图库，不会卡在半路。"""
    h, cfg, db = env
    iid = _mk(h, in_quarantine=1, needs_review=1)
    h._api_image_action({"ids": [iid], "act": "like"})
    assert _views_of(db, iid) == ["gallery"]


def test_no_image_appears_in_two_views_after_mixed_actions(env):
    """R159 第 4 条（自测要求）：批量混合操作后，库里**不存在**跨视图重复的图。"""
    h, cfg, db = env
    ids = [_mk(h) for _ in range(3)]
    ids.append(_mk(h, in_quarantine=1))
    ids.append(_mk(h, in_quarantine=1, needs_review=1))
    for i, iid in enumerate(ids):
        h._api_image_action({"ids": [iid], "act": "dislike" if i % 2 else "like"})
    rows = db.query("SELECT id FROM images") or []
    for r in rows:
        assert len(_views_of(db, r["id"])) == 1


def test_blacklist_from_gallery_also_isolates(env):
    """拉黑 = 既 blacklisted 也进隔离区（桌面端 BLACKLIST 同口径）。"""
    h, cfg, db = env
    iid = _mk(h)
    h._api_image_action({"ids": [iid], "act": "blacklist"})
    f = _flags(db, iid)
    assert (f["bl"], f["q"], f["nr"]) == (1, 1, 0)
    assert _views_of(db, iid) == ["quarantine"]


def test_quarantined_at_not_refreshed_when_already_quarantined(env):
    """已在隔离区的图再点一次不喜欢，不该把隔离时间刷成现在（伪造历史）。"""
    h, cfg, db = env
    iid = _mk(h, in_quarantine=1)
    db.execute("UPDATE images SET quarantined_at='2020-01-01 00:00:00' "
               "WHERE id=?", (iid,))
    h._api_image_action({"ids": [iid], "act": "dislike"})
    assert _flags(db, iid)["at"] == "2020-01-01 00:00:00"


def test_action_counts_only_real_rows(env):
    """done 只数真正作用到的行（不存在的 id 不冒充满分）。"""
    h, cfg, db = env
    iid = _mk(h)
    r = h._api_image_action({"ids": [iid, 999999], "act": "like"})
    assert r["done"] == 1


# ---------------------------------------------------------------------------
# R158-Bug4：桌面隔离区多选 + 网格
# ---------------------------------------------------------------------------

def _qapp():
    """复用已有 QApplication（不 new、不 quit —— conftest 明令禁止）。"""
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _grid(ids):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidget, QListWidgetItem
    app = _qapp()                                   # noqa: F841
    v = QListWidget()
    v.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
    for i in ids:
        it = QListWidgetItem(f"#{i}")
        it.setData(Qt.ItemDataRole.UserRole, i)
        v.addItem(it)
    return v


def test_grid_selected_ids_returns_all_selected_in_order():
    from media_resource_manager.gui.table_view import grid_selected_ids
    v = _grid([10, 20, 30, 40])
    for i in (0, 2, 3):
        v.item(i).setSelected(True)
    assert grid_selected_ids(v) == [10, 30, 40]


def test_grid_selected_ids_empty_without_selection():
    from media_resource_manager.gui.table_view import grid_selected_ids
    assert grid_selected_ids(_grid([1, 2, 3])) == []


def test_grid_selected_ids_skips_dirty_userrole():
    """脏项跳过不抛：一个坏数据不该让"批量还原 50 张"整体失败。"""
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.table_view import grid_selected_ids
    v = _grid([1, 2])
    v.item(0).setData(Qt.ItemDataRole.UserRole, "not-a-number")
    v.item(0).setSelected(True)
    v.item(1).setSelected(True)
    assert grid_selected_ids(v) == [2]


def test_make_table_multi_enables_extended_selection():
    """Bug4 第 1 条：隔离区表格必须能 Ctrl/Shift 多选（默认是单选）。"""
    from PySide6.QtWidgets import QAbstractItemView
    from media_resource_manager.gui.table_view import make_table
    _qapp()
    multi = make_table(multi=True)
    single = make_table()
    assert multi.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    assert single.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection


def _quar_src():
    """取出 quarantine_manager 的源码块（到下一个同级 def 为止）。"""
    text = MAIN_PY.read_text(encoding="utf-8")
    m = re.search(r"^    def quarantine_manager\(self\):.*?(?=^    def |\Z)",
                  text, re.S | re.M)
    assert m, "quarantine_manager 不见了"
    return m.group(0)


def test_quarantine_manager_wires_multi_and_grid():
    """Bug4 接线判据：多选、网格容器、网格取 id 三件都必须在位。"""
    src = _quar_src()
    assert "multi=True" in src, "隔离区表格没开多选"
    assert "grid_selected_ids" in src, "网格选中的 id 没接上"
    assert "QStackedWidget" in src, "没有表格/网格切换容器"


def test_quarantine_batch_actions_read_current_view():
    """★ 切到网格后批量动作必须读**网格**的选择，否则弹「先选中」＝看着像坏了。

    ⚠ 切块必须按 **8 空格**（嵌套函数）切，不是 4 空格（方法）：
    按 4 空格切会得到整个 quarantine_manager 剩余部分，里面到处是
    current_ids ⇒ 把 restore 改回读表格照样绿（本棒写第一版时的假绿）。
    """
    src = _quar_src()
    for fn in ("def restore():", "def purge():"):
        block = src.split(fn, 1)[1].split("\n        def ", 1)[0]
        assert "current_ids()" in block, f"{fn} 没走 current_ids()"
        assert "tbl_selected(tbl)" not in block, f"{fn} 直接读了表格"
    assert "def current_ids" in src


def test_quarantine_has_view_toggle_button():
    src = _quar_src()
    assert "网格视图" in src and "列表视图" in src
    assert "setCurrentWidget" in src


def test_quarantine_grid_items_sets_id_and_text(tmp_path):
    """网格 item：id 存 UserRole（与 grid_selected_ids 同口径）。"""
    from PySide6.QtCore import Qt
    from media_resource_manager.gui.main_window import quarantine_grid_items
    _qapp()
    p = tmp_path / "a.jpg"
    p.write_bytes(b"x")
    items = quarantine_grid_items(
        [{"id": 7, "path": str(p), "source_url": "https://e.com/a.jpg"}],
        cache_dir=None)
    assert len(items) == 1
    assert items[0].data(Qt.ItemDataRole.UserRole) == 7
    assert "a.jpg" in items[0].text()


def test_quarantine_grid_items_marks_missing_file(tmp_path):
    """磁盘已缺失的记录照样列出（批量清理「磁盘已缺失」时它们就是目标）。"""
    from media_resource_manager.gui.main_window import quarantine_grid_items
    _qapp()
    items = quarantine_grid_items(
        [{"id": 9, "path": str(tmp_path / "gone.jpg")}], cache_dir=None)
    assert len(items) == 1
    assert "文件缺失" in items[0].text()


def test_quarantine_grid_items_marks_no_path():
    """没路径 ≠ 文件缺失（数据问题 vs 磁盘问题，补救入口不同）。"""
    from media_resource_manager.gui.main_window import quarantine_grid_items
    _qapp()
    items = quarantine_grid_items([{"id": 11, "path": None}], cache_dir=None)
    assert len(items) == 1
    assert "无路径" in items[0].text()


def test_quarantine_grid_is_multi_select_grid():
    """网格视图本身必须多选 + IconMode + 禁拖拽。

    ⚠ 用**工厂函数**而不是源码搜字符串：搜字符串的话「把 ExtendedSelection
    改回单选」这种回归照样绿（本棒变异 M10 第一轮就是这么漏过的）。
    """
    from PySide6.QtWidgets import QAbstractItemView, QListWidget
    from media_resource_manager.gui.main_window import make_quarantine_grid
    _qapp()
    g = make_quarantine_grid()
    assert g.selectionMode() == QAbstractItemView.SelectionMode.ExtendedSelection
    assert g.viewMode() == QListWidget.ViewMode.IconMode
    assert g.dragEnabled() is False


def test_quarantine_grid_items_respects_cap(tmp_path):
    """cap 之外不解码 —— 隔离区几千条时首屏不能假死。

    ⚠ 必须用**真能解码的图**：拿 1 字节假 jpg 当素材，解码条条失败、
    图标个数恒为 0 ⇒ 不管 cap 生不生效断言都绿（本棒变异 M8 第一轮漏过，
    就是这个原因）。
    """
    from PIL import Image
    from media_resource_manager.gui.main_window import quarantine_grid_items
    _qapp()
    rows = []
    for i in range(5):
        p = tmp_path / f"c{i}.png"
        Image.new("RGB", (8, 8), (i * 10, 0, 0)).save(p)
        rows.append({"id": i, "path": str(p)})
    items = quarantine_grid_items(rows, cache_dir=str(tmp_path), cap=2)
    assert len(items) == 5                       # 一个都不能少（只是不解码）
    got = sum(1 for it in items if not it.icon().isNull())
    assert got == 2, f"cap=2 却解码了 {got} 张"


def test_quarantine_grid_items_skips_bad_id():
    from media_resource_manager.gui.main_window import quarantine_grid_items
    _qapp()
    items = quarantine_grid_items([{"id": "x"}, {"path": None}], cache_dir=None)
    assert items == []
