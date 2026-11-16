"""R164 护栏：网页图库/隔离区两处（Bug1 删除语义 + Q1 范围/框选）。

Bug1：隔离区里删图仍提示「将送入隔离区」。修复：
  · 前端 doAction 删除确认文案按当前视图分流（隔离区=彻底删除不可还原）；
  · 后端 /api/image/action 在「图已在隔离区(was_q)」收到 delete 走真删除
    （删文件 + 评价 + 图片行），不再软删进隔离区；
  · 前端 mrmActRemoves 隔离区 delete 也判定「离开本视图」→ 就地摘卡。

Q1：三视图（图库/待审核/隔离区）都不支持 Shift 范围选与拖拽框选。修复：
  · 卡片点击支持 Shift=范围选（锚点基，并入选中集）、Ctrl/⌘=反选；
  · 卡片空白区拖拽画框选（≥4px 阈值，触摸走系统滚动，阻止原生拖影）；
  · 图片设为 draggable=false、卡片 dragstart 阻止默认。
"""
from __future__ import annotations

import os
import re

import pytest

import pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
from media_resource_manager import webui  # noqa: E402  (for _H(webui._Handler))
def _read_opt(p):
    """旧版网页（index.html / app.js / app.css）已于 2026-10-29 物理删除。

    这三个文件的读取改成容错：读不到返回空串 —— 让仍依赖它们的断言
    **逐条失败**（好定位、好删），而不是整个测试文件在收集阶段就崩掉。
    """
    try:
        return pathlib.Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


WEB = ROOT / "media_resource_manager" / "web"
APPJS = _read_opt(WEB / "static" / "js" / "app.js")
CSS = _read_opt(WEB / "static" / "css" / "app.css")


def _strip_js_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"(?m)//.*$", "", s)
    return s


# ============================================================ Bug1 · 静态层




# ============================================================ Bug1 · 后端行为层


class _H(webui._Handler):
    """只测图片动作分支，不走真 socket（与 R48/R141/R157 同思路）。"""
    _cfg = None
    _db = None
    _headers = None
    _path = "/api/x"

    def __init__(self, cfg, db, headers=None, client_ip="127.0.0.1"):
        self._cfg = cfg
        self._db = db
        self._headers = headers or {}
        self._path = "/api/x"
        self.client_address = (client_ip, 0)

    def _send(self, code, body, ctype, extra=None):
        return (code, body, ctype, extra or {})

    def _json(self, obj, code=200, extra=None):
        import json as _j
        return self._send(code, _j.dumps(obj, ensure_ascii=False).encode("utf-8"),
                          "application/json", extra)

    def _body(self):
        return {}

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


def _make_db(tmp_path, monkeypatch):
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    from media_resource_manager.config import load_config
    from media_resource_manager.db import Database
    cfg = load_config()
    cfg.web_password = ""
    cfg.web_lan_open = True
    cfg.web_bind = "127.0.0.1"
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    return cfg, db


def test_quarantine_delete_is_hard_delete(tmp_path, monkeypatch):
    """★ 隔离区内 delete = 真删除：文件消失 + 图片行消失 + 评价清理。

    V0.0.5：测试文件放**软件自己的数据目录内** —— 数据目录外的文件属于
    用户原有图库，现在一律受保护（只清登记、不删文件），那是另一条红线。
    """
    cfg, db = _make_db(tmp_path, monkeypatch)
    lib = cfg.db_path.parent / "library"
    lib.mkdir(parents=True, exist_ok=True)
    f = lib / "q1.jpg"
    f.write_bytes(b"fakeimage")
    db.execute("INSERT INTO tasks (name) VALUES ('r164')")
    db.execute(
        "INSERT INTO images (task_id, source_url, path, needs_review,"
        " in_quarantine, blacklisted) VALUES (1,?,?,0,1,0)",
        ("http://example.com/q1.jpg", str(f)))
    iid = db.query("SELECT id FROM images WHERE source_url=?",
                   ("http://example.com/q1.jpg",))[0]["id"]
    db.execute("INSERT INTO reviews (image_id, decision, task_id) VALUES (?,?,1)",
               (iid, "dislike"))

    r = _H(cfg, db)._api_image_action({"ids": [iid], "act": "delete"})
    assert r["ok"] and r["done"] == 1, f"隔离区删除未成功：{r}"

    assert not f.exists(), "隔离区删除没删文件 —— 与桌面端语义不一致（Bug1）"
    assert not db.query("SELECT * FROM images WHERE id=?", (iid,)), \
        "隔离区删除没删图片行 —— 记录残留"
    assert not db.query("SELECT * FROM reviews WHERE image_id=?", (iid,)), \
        "隔离区删除没清评价记录"


def test_gallery_delete_still_soft_to_quarantine(tmp_path, monkeypatch):
    """★ 图库里 delete 仍走软删（进隔离区可还原）—— 不该被 Bug1 改坏。"""
    cfg, db = _make_db(tmp_path, monkeypatch)
    f = tmp_path / "g1.jpg"
    f.write_bytes(b"fakeimage")
    db.execute("INSERT INTO tasks (name) VALUES ('r164')")
    db.execute(
        "INSERT INTO images (task_id, source_url, path, needs_review,"
        " in_quarantine, blacklisted) VALUES (1,?,?,0,0,0)",
        ("http://example.com/g1.jpg", str(f)))
    iid = db.query("SELECT id FROM images WHERE source_url=?",
                   ("http://example.com/g1.jpg",))[0]["id"]

    r = _H(cfg, db)._api_image_action({"ids": [iid], "act": "delete"})
    assert r["ok"] and r["done"] == 1, f"图库删除未成功：{r}"

    row = db.query("SELECT in_quarantine FROM images WHERE id=?", (iid,))[0]
    assert row["in_quarantine"] == 1, \
        "图库删除变成了真删除 —— 软删（进隔离区可还原）语义被破坏"
    assert f.exists(), "图库删除不该删文件"


# ============================================================ Q1 · 静态层






# ============================================================ 集成层（可选）


def _webengine_ok():
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception:
        return False
    return True


def _boot_browser(tmp_path, rows_gallery=0, rows_quarantine=0):
    """起 WebServer + 无头浏览器，返回 (db, srv, view, page, js, wait)。

    浏览器搭法完全照搬已稳定通过的 test_r146_web_action_chain：
      · 新建 QWebEnginePage（同 profile）+ 注入 fetch 记录（window.__reqs），
        用来**确定性**地证明某个动作真发了 POST（不靠渲染/计时猜）；
      · 先等 SPA 初始化（导航条渲染出来 = boot 完成），与具体视图有无卡片无关——
        隔离区测试本身图库就是空的，不能等图库卡片。
    """
    os.environ["MRM_HOME"] = str(tmp_path / "home")
    from media_resource_manager.config import load_config
    from media_resource_manager.db import Database
    from media_resource_manager.webui import WebServer
    from PySide6.QtCore import Qt, QEventLoop, QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript

    cfg = load_config()
    cfg.web_port = 0
    cfg.web_password = ""
    cfg.web_lan_open = True          # 127.0.0.1 免密直进
    cfg.web_bind = "127.0.0.1"
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    db.execute("INSERT INTO tasks (name) VALUES ('r164')")
    for i in range(1, rows_gallery + 1):
        db.execute(
            "INSERT INTO images (task_id, source_url, path, needs_review,"
            " in_quarantine, blacklisted) VALUES (1,?,?,0,0,0)",
            (f"http://example.com/g{i}.jpg", str(tmp_path / f"g{i}.jpg")))
    for i in range(1, rows_quarantine + 1):
        db.execute(
            "INSERT INTO images (task_id, source_url, path, needs_review,"
            " in_quarantine, blacklisted) VALUES (1,?,?,0,1,0)",
            (f"http://example.com/q{i}.jpg", str(tmp_path / f"q{i}.jpg")))
    srv = WebServer(cfg, db)
    ok, msg = srv.start()
    assert ok, msg
    port = cfg.web_port

    app = QApplication.instance() or QApplication([])
    view = QWebEngineView()
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.resize(1280, 900)
    page = QWebEnginePage(view.page().profile(), view)
    view.setPage(page)
    inj = QWebEngineScript()
    inj.setSourceCode(
        "window.__reqs=[];var __of=window.fetch;window.fetch=function(u,o){"
        "var rec={u:String(u),m:(o&&o.method)||'GET',b:(o&&o.body)||null,st:null};"
        "window.__reqs.push(rec);"
        "return __of.apply(this,arguments).then(function(r){rec.st=r.status;"
        "return r;});};")
    inj.setInjectionPoint(QWebEngineScript.DocumentCreation)
    inj.setWorldId(QWebEngineScript.MainWorld)
    page.scripts().insert(inj)

    view.show()
    app.processEvents()
    loop = QEventLoop()
    view.loadFinished.connect(lambda _ok: loop.quit())
    view.load(QUrl(f"http://127.0.0.1:{port}/"))
    QTimer.singleShot(30000, loop.quit)
    loop.exec()

    def js(code, timeout=20000):
        out = {}
        l2 = QEventLoop()

        def cb(v):
            out["v"] = v
            l2.quit()
        page.runJavaScript(code, cb)
        QTimer.singleShot(timeout, l2.quit)
        l2.exec()
        app.processEvents()
        return out.get("v")

    def wait(pred, timeout_s=30.0):
        import time as _t
        deadline = _t.time() + timeout_s
        while _t.time() < deadline:
            if js(pred):
                return True
            _t.sleep(0.4)
        return False

    # 先等 SPA 初始化 + 首屏首拉 settle（loading=false）：否则刚 boot 完就切视图/
    # 点卡片会落在 loading 竞态里，无头环境偶发「切了不渲染 / 点了没反应」。
    assert wait("(function(){return !!window.state && window.state.view==='gallery' "
                "&& window.state.loading===false;})()", 30), \
        "SPA 未初始化或首屏仍在加载（boot 未完成 / 竞态）"
    return db, srv, view, page, js, wait
