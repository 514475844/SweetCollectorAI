"""Phase B Web 测试（B1~B9）。

三层：
1. **纯函数层**（会话签发校验 / limit 钳制 / 目录穿越防护 / 资产定位）
2. **起真服务层**（随机端口起 WebServer，urllib 打 / 、/manifest、/api/*）
3. **静态断言层**（读 web/ 下前端文件，钉死 8 入口 / SW 注册 / 响应式断点 / 单按钮）

⚠ 起服务必须用 **0 端口**（让OS 分配空闲端口）：写死 8080 会与用户正在跑的
Web 服务撞port，测试直接把用户的Web 服务挤下线。
"""
from __future__ import annotations

import json
import shutil
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from media_resource_manager import webui
from media_resource_manager.db import Database
from media_resource_manager.webui import (
    PAGE_LIMIT_DEFAULT, PAGE_LIMIT_MAX, VIEWS, WebServer,
    _clamp_limit, check_auth, make_session, verify_session,
)

WEB_DIR = Path(webui.__file__).resolve().parent / "web"


# ---------------- fixtures ----------------

class _Cfg:
    """⚠ **实例属性，不是类属性**。写成类属性后`test_settings_never_returns_
    password` 那句 `_Cfg.web_password = "supersecret"` 会**永久改掉类属性**，
    后续每个测试的密码都变了 → 后面几十个用例集体 401。
    这类"测试之间互相污染"最难查，因为它表现为「改了A 测试坏了 B 测试」。"""

    def __init__(self):
        self.web_user = "admin"
        self.web_password = "secret123"
        self.web_bind = "127.0.0.1"
        self.web_port = 0          # 0 = 让 OS 选空闲端口，测试专用
        self.ui_theme = "light"
        self.ai_gpu = False
        self.blur_sensitive = False
        self.review_key_ask = False


@pytest.fixture()
def db():
    td = Path(tempfile.mkdtemp(prefix="webui_db_"))
    d = Database(td / "mrm.db")
    try:
        yield d
    finally:
        d.close()                     # ⚠ Windows 上句柄开着不能删目录
        shutil.rmtree(td, ignore_errors=True)


@pytest.fixture()
def srv(db):
    """真起一个 Web 服务（随机端口）。测完必须停，否则线程泄漏。"""
    s = WebServer(_Cfg(), db)
    ok, _ = s.start()
    assert ok, "WebServer 启动失败"
    yield s
    s.stop()


def _free_port() -> int:
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def _get(port, path, auth=True, cookie=None, raw_auth=None, allow_4xx=False):
    """发一个 GET。**默认带认证** —— 401/404 是被测行为时要显式allow_4xx。

    ⚠ urllib 对 4xx/5xx 是**抛异常**而不是返回响应，所以测「该 401 的请求
    确实 401」必须用 pytest.raises(HTTPError) 接。给一个 allow_4xx 开关
    是为了让「非 2xx 也是被测行为」的用例写起来不用每次都写 try。
    """
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if raw_auth:
        req.add_header("Authorization", raw_auth)
    elif auth:
        tok = webui._token("admin", "secret123")
        req.add_header("Authorization", tok)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        if allow_4xx:
            return e.code, e.read(), dict(e.headers or {})
        raise


def _get_json(port, path, allow_4xx=False, **kw):
    st, body, hd = _get(port, path, allow_4xx=allow_4xx, **kw)
    return st, json.loads(body.decode("utf-8")), hd


def _add_images(db, n, **cols):
    """插 n 张图。⚠ images.source_url 是 NOT NULL 且 (task_id,url) UNIQUE
    —— 每条必须给**不同** url（项目铁律，写错报出来像"被测代码坏了"）。"""
    db.execute("INSERT INTO tasks(id,name,adapter_name) VALUES (1,'t','generic')")
    for i in range(n):
        extra = ", ".join(f"{k}={v!r}" for k, v in cols.items())
        sep = "," if extra else ""
        db.execute(
            "INSERT INTO images(task_id,source_url,path,tags,score"
            f"{sep}) VALUES (1,'http://x/{i}','/p/{i}.jpg','[]',0.5{sep})")


# ---------------- 1. 纯函数层 ----------------

def test_session_roundtrip():
    tok = make_session("admin", "pw")
    assert verify_session(tok, "admin", "pw") is True


def test_session_rejects_other_password():
    """改密码后旧会话必须立刻失效 —— 否则改了密码等于没改。"""
    tok = make_session("admin", "oldpw")
    assert verify_session(tok, "admin", "newpw") is False


def test_session_rejects_tampered_token():
    assert verify_session("admin:9999999999:deadbeef", "admin", "pw") is False
    assert verify_session("", "admin", "pw") is False
    assert verify_session("garbage", "admin", "pw") is False
    assert verify_session("evil:99999999999:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                          "admin", "pw") is False


def test_session_expires():
    """过期会话必须被拒（时钟前进_SESSION_TTL 之后）。"""
    tok = make_session("admin", "pw", now=1000)
    assert verify_session(tok, "admin", "pw", now=1000 + 60) is True
    assert verify_session(tok, "admin", "pw",
                          now=1000 + webui._SESSION_TTL + 10) is False


def test_basic_auth_still_works():
    """B3 没废掉 Basic Auth：老书签/脚本/curl 必须还能用。"""
    assert check_auth(webui._token("admin", "pw"), "admin", "pw") is True
    assert check_auth("Basic garbage", "admin", "pw") is False
    assert check_auth("", "admin", "pw") is False


def test_clamp_limit_bounds():
    """B2 #47 的服务端硬上限：前端传再大也得夹住。"""
    assert _clamp_limit(None) == PAGE_LIMIT_DEFAULT
    assert _clamp_limit("abc") == PAGE_LIMIT_DEFAULT
    assert _clamp_limit("0") == 1
    assert _clamp_limit("-5") == 1
    assert _clamp_limit("999999") == PAGE_LIMIT_MAX
    assert _clamp_limit("50") == 50


def test_views_are_exactly_eight():
    """B4 #49：桌面端 8 个顶级菜单 → Web 8 个视图，一个不多一个不少。"""
    assert len(VIEWS) == 8
    assert set(VIEWS) == {"gallery", "pending", "quarantine", "dedup",
                          "adapters", "cleanup", "settings", "logstats"}


def test_web_assets_dir_has_index():
    """前端资产必须真实存在（漏打包的护栏）。

    2026-10-29：旧 SPA（index.html / static/css/app.css / static/js/app.js）
    已物理删除。意图不变 —— 「界面所需文件必须都在包里」，只是清单换成
    新版：外壳 + 扫码脚本 + SW + manifest + **Vue 构建产物**。
    """
    d = webui.web_assets_dir()
    for rel in ("index.v2.html", "static/js/qr-login.js",
                "manifest.webmanifest", "sw.js"):
        assert (d / rel).is_file(), f"缺前端资产 {rel}"
    built = list((d / "static" / "app" / "assets").glob("*.js"))
    assert built, "缺前端产物 static/app/assets/*.js（漏了 npm run build）"

def test_index_is_served(srv):
    st, body, hd = _get(srv.port, "/")
    assert st == 200
    assert "text/html" in hd.get("Content-Type", "")
    assert b"Sweet" in body

def test_manifest_served(srv):
    """B9 #17 PWA：manifest 必须能按 URL 取到（否则无法「添加到主屏幕」）。"""
    st, body, hd = _get(srv.port, "/manifest.webmanifest")
    assert st == 200
    assert "manifest" in hd.get("Content-Type", "")
    m = json.loads(body.decode("utf-8"))
    assert m["display"] == "standalone"
    assert m["start_url"] == "/"
    assert m["name"]
    assert any(i.get("sizes") == "512x512" for i in m["icons"])


def test_service_worker_served(srv):
    """B9：sw.js 必须真的能取到，且作用域是根。"""
    st, body, hd = _get(srv.port, "/sw.js")
    assert st == 200
    assert "javascript" in hd.get("Content-Type", "")
    assert b"addEventListener" in body
    assert hd.get("Service-Worker-Allowed") == "/"


def test_sw_never_caches_api_or_thumb(srv):
    """⚠ **反向断言**：sw.js 绝不能缓存任何东西。

    2026-10-29（用户第 11 条）：旧版网页下线，这个 SW 改成**自杀脚本**
    （注销自己 + 清空缓存）。原来那条「不能缓存 /api/ 与 /thumb/」的判据，
    在新方案下收敛成更强的一句：**根本不许出现写入缓存的调用**。
    """
    src = (WEB_DIR / "sw.js").read_text(encoding="utf-8")
    assert "caches.put" not in src and "cache.put" not in src
    assert "caches.open" not in src
    # 必须能把自己注销掉，否则老用户的浏览器会永远拿着旧页面
    assert "unregister" in src
    # 不再有 fetch 拦截（也就不可能缓存数据面）
    assert "addEventListener('fetch'" not in src


def test_unauthenticated_api_gets_401(srv):
    """B3：未登录的 API 请求必须 401（不能把数据漏出去）。"""
    st, body, _ = _get(srv.port, "/api/summary", auth=False, allow_4xx=True)
    assert st == 401
    assert json.loads(body.decode("utf-8"))["ok"] is False


def test_unauthenticated_page_gets_200_login_shell(srv):
    """B3：页面请求**不能**回 401 —— 那会让浏览器弹原生 Basic Auth 对话框，
    手机上体验极差且没法登出。改为 200 + 外壳，前端自己显示密码页。"""
    st, body, hd = _get(srv.port, "/", auth=False, allow_4xx=True)
    assert st == 200
    assert b"Sweet" in body


def test_login_sets_cookie_and_grants_access(srv):
    """B3 完整链路：登录 → 拿 cookie → 免Basic Auth 访问 API。"""
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/login",
        data=json.dumps({"password": "secret123"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200
        sc = r.headers.get("Set-Cookie", "")
    assert "mrm_session=" in sc
    assert "HttpOnly" in sc and "SameSite=Strict" in sc
    tok = sc.split(";")[0]
    st, obj, _ = _get_json(srv.port, "/api/summary", auth=False, cookie=tok)
    assert st == 200 and obj["ok"] is True


def test_login_wrong_password_401(srv):
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/login",
        data=json.dumps({"password": "wrong"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 401
    assert "Set-Cookie" not in (ei.value.headers or {})


def test_logout_clears_cookie(srv):
    """B3 #48 登出：必须下发 Max-Age=0 让浏览器删掉 cookie。"""
    tok = make_session("admin", "secret123")
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/logout", data=b"{}", method="POST",
        headers={"Content-Type": "application/json",
                 "Cookie": f"mrm_session={tok}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        sc = r.headers.get("Set-Cookie", "")
    assert r.status == 200
    assert "Max-Age=0" in sc


def test_expired_cookie_rejected(srv):
    tok = make_session("admin", "secret123", now=1000)
    st, _, _ = _get(srv.port, "/api/summary", auth=False,
                    cookie=f"mrm_session={tok}", allow_4xx=True)
    assert st == 401


def test_summary_counts(srv, db):
    _add_images(db, 3)
    db.execute("UPDATE images SET needs_review=1 WHERE id=1")
    st, obj, _ = _get_json(srv.port, "/api/summary")
    assert st == 200
    assert obj["tasks"] == 1
    assert obj["images"] == 3
    assert obj["pending"] == 1


def test_settings_never_returns_password(srv):
    """⚠ **安全项**：设置页绝不能把 Web 密码回传给网页。"""
    st, obj, _ = _get_json(srv.port, "/api/settings")
    assert st == 200
    assert "secret123" not in json.dumps(obj, ensure_ascii=False)
    assert obj.get("web_password") == "（已设置）"


def test_settings_works_with_real_secret(srv, db):
    """换一个实例、改密码后再看：依然不回显密码本身。"""
    cfg = _Cfg()
    cfg.web_password = "supersecret"
    s = WebServer(cfg, db)
    ok, _ = s.start()
    assert ok
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{s.port}/api/settings")
        req.add_header("Authorization", webui._token("admin", "supersecret"))
        with urllib.request.urlopen(req, timeout=10) as r:
            obj = json.loads(r.read().decode("utf-8"))
        assert "supersecret" not in json.dumps(obj, ensure_ascii=False)
        assert obj["web_password"] == "（已设置）"
    finally:
        s.stop()


# ---------------- B2 #47 无限加载 ----------------

def test_gallery_returns_only_first_batch(srv, db):
    """B2：200 张图，首批**只返 ~100**，绝不一次全量。"""
    _add_images(db, 200)
    st, obj, _ = _get_json(srv.port, "/api/view/gallery")
    assert st == 200
    assert len(obj["items"]) == PAGE_LIMIT_DEFAULT
    assert obj["has_more"] is True
    assert obj["next_cursor"] is not None


def test_gallery_cursor_paginates_without_overlap(srv, db):
    """B2：带cursor 拿下一批，且**与首批无重叠**（漏图/重图都不许）。"""
    _add_images(db, 200)
    _, p1, _ = _get_json(srv.port, "/api/view/gallery")
    _, p2, _ = _get_json(
        srv.port, f"/api/view/gallery?cursor={p1['next_cursor']}")
    assert p2["has_more"] is False
    assert p2["next_cursor"] is None
    ids1 = {i["id"] for i in p1["items"]}
    ids2 = {i["id"] for i in p2["items"]}
    assert not (ids1 & ids2)
    assert len(ids1 | ids2) == 200


def test_gallery_limit_is_clamped_server_side(srv, db):
    """B2：服务端硬上限必须生效（前端传 999999 也只给 100）。"""
    _add_images(db, 10)
    _, obj, _ = _get_json(srv.port, "/api/view/gallery?limit=999999")
    assert len(obj["items"]) <= PAGE_LIMIT_MAX


def test_views_are_filtered_correctly(srv, db):
    """四个图片视图各自的过滤条件不许串。

    ⚠ 注意语义：**图库不排除「待审核」的图** —— 待审核只是"还没审完"，
    它仍然在图库里（桌面端也是这个口径）。图库排除的是隔离区与已拉黑。
    """
    _add_images(db, 4)
    ids = [r["id"] for r in db.query("SELECT id FROM images ORDER BY id")]
    db.execute(f"UPDATE images SET in_quarantine=1 WHERE id={ids[0]}")
    db.execute(f"UPDATE images SET needs_review=1 WHERE id={ids[1]}")
    db.execute(f"UPDATE images SET blacklisted=1 WHERE id={ids[2]}")

    def got(v):
        _, o, _ = _get_json(srv.port, f"/api/view/{v}?limit=100")
        return {i["id"] for i in o["items"]}

    # 图库 = 排除隔离 + 排除拉黑（待审核仍在其中）
    assert got("gallery") == {ids[1], ids[3]}
    assert got("pending") == {ids[1]}
    assert got("quarantine") == {ids[0]}
    # 三个视图必须互不相交（除 pending 可能与 gallery 同集）
    assert not (got("gallery") & got("quarantine"))
    assert not (got("pending") & got("quarantine"))


def test_unknown_view_reports_error(srv):
    _, obj, _ = _get_json(srv.port, "/api/view/nope")
    assert obj["ok"] is False


def test_item_carries_url_and_flag(srv, db):
    _add_images(db, 1)
    _, obj, _ = _get_json(srv.port, "/api/view/gallery")
    it = obj["items"][0]
    assert it["url"].startswith("/thumb/")
    assert "full_url" in it
    assert isinstance(it["flag"], str)


# ---------------- B5 #50 单按钮动作 ----------------

def test_image_action_like_clears_pending(srv, db):
    _add_images(db, 1)
    iid = db.query("SELECT id FROM images")[0]["id"]
    db.execute("UPDATE images SET needs_review=1 WHERE id=?", (iid,))
    st, obj, _ = _post(srv.port, "/api/image/action",
                        {"ids": [iid], "act": "like"})
    assert st == 200 and obj["ok"] is True and obj["done"] == 1
    row = db.query("SELECT needs_review FROM images WHERE id=?", (iid,))[0]
    assert row["needs_review"] == 0


def test_image_action_rejects_unknown_act(srv, db):
    _add_images(db, 1)
    iid = db.query("SELECT id FROM images")[0]["id"]
    _, obj, _ = _post(srv.port, "/api/image/action",
                       {"ids": [iid], "act": "rm -rf"})
    assert obj["ok"] is False


def test_image_action_toggles_favorite(srv, db):
    _add_images(db, 1)
    iid = db.query("SELECT id FROM images")[0]["id"]
    _post(srv.port, "/api/image/action", {"ids": [iid], "act": "favorite"})
    assert db.query("SELECT favorite FROM images WHERE id=?",
                    (iid,))[0]["favorite"] == 1
    _post(srv.port, "/api/image/action", {"ids": [iid], "act": "favorite"})
    assert db.query("SELECT favorite FROM images WHERE id=?",
                    (iid,))[0]["favorite"] == 0


def test_image_action_feeds_learner(srv, db):
    """网页端审核必须真的喂给学习器 —— 否则网页上点了「喜欢」AI 学不到。"""
    _add_images(db, 1)
    iid = db.query("SELECT id FROM images")[0]["id"]
    db.execute("UPDATE images SET tags=? WHERE id=?",
               (json.dumps(["blond hair"]), iid))
    _post(srv.port, "/api/image/action", {"ids": [iid], "act": "like"})
    rows = db.query("SELECT label, weight FROM preferences")
    assert any("blond" in str(r["label"]) for r in rows)


def test_image_action_requires_ids(srv):
    _, obj, _ = _post(srv.port, "/api/image/action", {"ids": [], "act": "like"})
    assert obj["ok"] is False


# ---------------- B7 #52 保守默认 ----------------

def test_cleanup_keeps_preferences(srv, db):
    """⚠ 清理**绝不能碰学习记录** —— 用户教过的偏好最贵，桌面端也明确保留。"""
    db.execute("INSERT INTO preferences(task_id,label,weight,seen) "
               "VALUES (0,'blond hair',1.0,1)")
    _post(srv.port, "/api/cleanup",
          {"plan": {"tasks": True, "reviews": True, "images": True}})
    assert db.query("SELECT COUNT(*) c FROM preferences")[0]["c"] == 1


def test_cleanup_ignores_unknown_plan_keys(srv, db):
    """白名单外的键必须被忽略（不能靠传参删任意表）。"""
    _add_images(db, 2)
    _, obj, _ = _post(srv.port, "/api/cleanup",
                       {"plan": {"preferences": True, "tasks": True}})
    assert "preferences" not in obj["done"]


def test_cleanup_page_is_read_only_view(srv):
    """清理页不需要分页数据。"""
    _, obj, _ = _get_json(srv.port, "/api/view/cleanup")
    assert obj["ok"] is True and obj["items"] == []


def test_logstats_view(srv, db):
    db.audit("tester", "review", "1")
    _, obj, _ = _get_json(srv.port, "/api/view/logstats")
    assert obj["ok"] is True
    assert any("日志条数" in str(i["name"]) for i in obj["items"])


# ---------------- 3. 前端静态断言 ----------------

def _post(port, path, body, auth=True):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": webui._token("admin", "secret123")},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8")), dict(r.headers)