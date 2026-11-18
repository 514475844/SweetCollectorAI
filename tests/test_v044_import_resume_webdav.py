"""V0.0.44 护栏：导入的断点续导、手打路径、WebDAV 来源。

用户三条（原话口径）：
1.「导入，不支持断点导入吗」—— 真实情况：索引阶段每张图都会落一行 images
   （ai_state=0 待打标 / 1 已打标），停/崩/关机都不丢；再对同一来源导一次
   天然就是「跳过已入库的、接着打没打的」。缺的是**界面把这件事说出来**，
   所以这版加了 /api/import/last + 顶部「接着打标」条。
2.「尚未选择文件夹，这个框太小了和其他地方风格不一样，不支持手动输入路径」
   —— 原来是 320px 的只读 code 块，改成普通输入框（占满一行、可粘贴 UNC）。
3.「想导入 NAS 上的图片，或者某个 WebDAV 的图片」—— NAS 走 \\\\主机\\共享
   路径即可；WebDAV 新增一条链：PROPFIND 枚举 → 下载到 library/webdav/
   → 交给本地导入（索引/去重/打标/学习全复用）。

这里用**真 HTTP 服务**跑 PROPFIND/GET（不是打桩），因为 WebDAV 的坑全在
协议细节上：Depth 头、multistatus 命名空间、href 相对/绝对、401 的语义。
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from media_resource_manager import webdav_import as wd
from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"


def _png(color=(120, 90, 220)) -> bytes:
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (8, 6), color).save(buf, "PNG")
    return buf.getvalue()


# ───────────────────── 假的 WebDAV 服务（真协议） ─────────────────────
class _DAV:
    """一个够用的 WebDAV：PROPFIND 列目录 + GET 取文件 + Basic 认证。"""

    def __init__(self, files: dict[str, bytes], dirs: tuple[str, ...] = (),
                 user: str = "", password: str = ""):
        self.files = files                       # "/root/a.png" -> bytes
        self.dirs = set(dirs)                    # "/root/sub"
        self.user = user
        self.password = password
        self.hits: list[tuple[str, str]] = []

    def children(self, path: str) -> list[str]:
        base = path.rstrip("/") + "/"
        out = []
        for p in list(self.files) + list(self.dirs):
            if p == path.rstrip("/"):
                continue
            if p.startswith(base):
                rest = p[len(base):]
                if "/" not in rest:
                    out.append(p)
        return sorted(out)

    def is_dir(self, p: str) -> bool:
        return p.rstrip("/") in self.dirs

    def xml(self, path: str, depth: int) -> bytes:
        rows = []
        paths = [path.rstrip("/") or "/"]
        if depth >= 1:
            paths += self.children(path)
        for p in paths:
            d = self.is_dir(p)
            size = "" if d else str(len(self.files.get(p, b"")))
            rows.append(
                "<d:response><d:href>{href}</d:href><d:propstat>"
                "<d:prop><d:resourcetype>{rt}</d:resourcetype>"
                "<d:getcontentlength>{size}</d:getcontentlength></d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
                .format(href=p + ("/" if d and not p.endswith("/") else ""),
                        rt="<d:collection/>" if d else "", size=size))
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<d:multistatus xmlns:d="DAV:">' + "".join(rows)
                + "</d:multistatus>")
        return body.encode("utf-8")


def _serve(dav: _DAV):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _auth_ok(self):
            if not dav.user:
                return True
            got = self.headers.get("Authorization") or ""
            want = "Basic " + base64.b64encode(
                f"{dav.user}:{dav.password}".encode()).decode()
            return got == want

        def do_PROPFIND(self):                   # noqa: N802 —— 协议方法名
            dav.hits.append(("PROPFIND", self.path))
            # 必须把请求体读干净：PROPFIND 带 body，HTTP/1.1 长连接下不读会
            # 让剩下的字节被当成下一个请求，客户端收到连接重置（10053）。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
            except Exception:
                pass
            if not self._auth_ok():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="dav"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            depth = int(self.headers.get("Depth") or "1")
            raw = dav.xml(self.path, depth)
            self.send_response(207)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Connection", "close")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):                        # noqa: N802
            dav.hits.append(("GET", self.path))
            if not self._auth_ok():
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = dav.files.get(self.path)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):               # 别把测试输出淹掉
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


@pytest.fixture
def dav_server():
    files = {
        "/root/a.png": _png((200, 60, 60)),
        "/root/sub/b.png": _png((60, 200, 60)),
        "/root/sub/deep/c.png": _png((60, 60, 200)),
        "/root/readme.txt": b"not an image",
    }
    dav = _DAV(files, dirs=("/root/sub", "/root/sub/deep"))
    srv, base = _serve(dav)
    yield dav, base
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    import media_resource_manager.config as cfgmod
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)
    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp_path / "mrm.db"
    cfg.library_dir = tmp_path / "library"
    cfg.library_dir.mkdir(parents=True, exist_ok=True)
    yield cfg, db
    try:
        db.close()
    except Exception:
        pass


# ───────────────────── ① WebDAV：枚举 ─────────────────────
def test_list_images_walks_subdirs(dav_server):
    _dav, base = dav_server
    got = wd.list_images(base + "/root/", recursive=True)
    rels = sorted(r for _u, r in got)
    assert rels == ["a.png", "sub/b.png", "sub/deep/c.png"], rels


def test_list_images_non_recursive_only_top(dav_server):
    _dav, base = dav_server
    got = wd.list_images(base + "/root/", recursive=False)
    assert [r for _u, r in got] == ["a.png"]


def test_list_images_skips_nas_thumbnail_dirs(dav_server):
    """@eaDir / .thumbnails 这类目录里全是同名缩略图，进了图库就是垃圾。"""
    dav, base = dav_server
    dav.dirs.add("/root/@eaDir")
    dav.files["/root/@eaDir/a.png"] = _png()
    got = wd.list_images(base + "/root/", recursive=True)
    assert all("@eaDir" not in r for _u, r in got), got


# ───────────────────── ② WebDAV：下载 + 入库 ─────────────────────
def test_webdav_import_end_to_end(env, dav_server):
    cfg, db = env
    _dav, base = dav_server
    res = wd.import_webdav(db, cfg, base + "/root/", do_tag=False)
    assert res.get("ok") is True, res
    assert res["indexed"] == 3, res
    assert res["source"] == "webdav"
    # 落点在软件图库里，删图不会牵扯源站
    files = [p for p in (cfg.library_dir / "webdav").rglob("*.png")]
    assert len(files) == 3, files
    assert all(str(cfg.library_dir) in str(p) for p in files)


def test_webdav_second_run_reuses_downloads(env, dav_server):
    """断点续导的落点：同一地址再导一次，已下载的直接复用、不再重复下载。"""
    cfg, db = env
    dav, base = dav_server
    wd.import_webdav(db, cfg, base + "/root/", do_tag=False)
    gets_before = sum(1 for m, _p in dav.hits if m == "GET")
    res2 = wd.import_webdav(db, cfg, base + "/root/", do_tag=False)
    gets_after = sum(1 for m, _p in dav.hits if m == "GET")
    assert res2["ok"] is True, res2
    assert res2["indexed"] == 0, "第二遍不该再入库"
    assert gets_after == gets_before, "第二遍不该重新下载"
    assert res2["already_here"] == 3


def test_webdav_rejects_non_http():
    db = cfg = None
    res = wd.import_webdav(db, cfg, r"\\192.168.1.2\photo")
    assert res["ok"] is False
    assert "http" in res["error"]


def test_webdav_reports_bad_password(tmp_path, monkeypatch):
    import media_resource_manager.config as cfgmod
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)
    dav = _DAV({"/r/a.png": _png()}, dirs=(), user="u", password="right")
    srv, base = _serve(dav)
    try:
        db = Database(tmp_path / "mrm.db")
        cfg = AppConfig()
        cfg.library_dir = tmp_path / "lib"
        res = wd.import_webdav(db, cfg, base + "/r/", user="u", password="wrong",
                               do_tag=False)
        assert res["ok"] is False
        assert "401" in res["error"] or "密码" in res["error"], res
        ok = wd.import_webdav(db, cfg, base + "/r/", user="u", password="right",
                              do_tag=False)
        assert ok["ok"] is True, ok
        db.close()
    finally:
        srv.shutdown()
        srv.server_close()


# ───────────────────── ③ 断点：/api/import/last ─────────────────────
def _handler(cfg, db):
    from media_resource_manager import webui

    class _Server:
        class queue:
            @staticmethod
            def start_task(tid):
                pass

            @staticmethod
            def stop_task(tid):
                pass

    class H(webui._Handler):
        _headers = None
        _path = "/api/x"

        def __init__(self, cfg, db):
            self._cfg, self._db = cfg, db
            self.server = _Server()
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

    return H(cfg, db)


def test_import_last_reports_unfinished_work(env, tmp_path):
    cfg, db = env
    folder = tmp_path / "pics"
    folder.mkdir()
    h = _handler(cfg, db)
    from media_resource_manager.models import TaskConfig, TaskStatus
    from media_resource_manager.repo import save_task

    tid = save_task(db, TaskConfig(id=None, name="pics", adapter_name="",
                                   entry_urls=[str(folder)], output_dir=str(folder),
                                   max_depth=0, concurrency=1, delay=0.0,
                                   status=TaskStatus.IDLE))
    with db.transaction():
        for name, state in (("a.jpg", 1), ("b.jpg", 0), ("c.jpg", 0)):
            db.execute(
                """INSERT INTO images (task_id, source_url, sha256, width, height,
                                       bytes, path, tags, score, blacklisted,
                                       in_quarantine, ai_state)
                   VALUES (?,?,?,8,8,10,?,'[]',0,0,0,?)""",
                (tid, f"file://{folder / name}", name, str(folder / name), state))
    r = h._api_import_last()
    assert r["ok"] is True
    item = r["items"][0]
    assert item["total"] == 3 and item["pending"] == 2, item
    assert item["folder"] == str(folder) and item["exists"] is True


def test_import_last_ignores_downloaded_http_images(env):
    """下载任务的行 source_url 是 http(s)，不是 file:// —— 不能当成「文件夹导入剩余」。"""
    cfg, db = env
    from media_resource_manager.models import TaskConfig, TaskStatus
    from media_resource_manager.repo import save_task
    tid = save_task(db, TaskConfig(id=None, name="site", adapter_name="x",
                                   entry_urls=["https://e.com"], output_dir="",
                                   max_depth=1, concurrency=1, delay=0.0,
                                   status=TaskStatus.IDLE))
    with db.transaction():
        db.execute("""INSERT INTO images (task_id, source_url, sha256, width, height,
                        bytes, path, tags, score, blacklisted, in_quarantine, ai_state)
                      VALUES (?,?,?,8,8,10,'','[]',0,0,0,0)""",
                   (tid, "https://e.com/a.jpg", "x"))
    r = _handler(cfg, db)._api_import_last()
    assert r["items"] == [], r


# ───────────────────── ④ 前端：路径可输入 / 两个来源 ─────────────────────
def _comp() -> str:
    p = WEB / "components" / "ImportFolder.vue"
    assert p.is_file()
    return p.read_text(encoding="utf-8")


def test_path_is_editable_input():
    s = _comp()
    assert 'v-model.trim="imp.folder"' in s, "路径框不能手打"
    assert 'class="imp-path"' not in s, "只读的 code 块又回来了"
    assert 'class="pathin"' in s and "flex: 1 1 420px" in s, \
        "路径框没有占满一行（还会像以前一样窄）"


def test_two_sources_and_webdav_fields():
    s = _comp()
    assert "本地文件夹" in s and "WebDAV 地址" in s
    assert 'v-model.trim="imp.url"' in s
    assert 'v-model="imp.pass"' in s and 'type="password"' in s


def test_resume_bar_wired():
    s = _comp()
    assert "继续识别" in s and "loadLast" in s
    job = (WEB / "importJob.js").read_text(encoding="utf-8")
    assert "export async function loadLast" in job
    assert "api.importLast()" in job
    # 表单要持久化（手打路径/地址重开软件还在）
    assert "imp.url = s.url" in job and "saveForm" in job


def test_api_and_routes_exist():
    api = (WEB / "api.js").read_text(encoding="utf-8")
    assert "importLast:" in api and "importWebdav:" in api
    w = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    assert '"/api/import/last"' in w
    assert '"/api/import/webdav"' in w
    assert "def _api_import_last" in w and "def _api_import_webdav" in w


def test_version_at_least_v044():
    from media_resource_manager import __version__
    parts = tuple(int(x) for x in __version__.split("."))
    assert parts >= (0, 0, 44), __version__
