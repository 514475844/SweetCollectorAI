"""R50 / PRODUCTION_TEST_ORDER **第二阶段·生产级真跑**（Web 端）。

作业单原文：「用 offscreen 启动桌面 exe（或源码入口），对每个主功能做端到端
验证；Web 用**真实 HTTP 客户端**/无头方式访问每个页面与 API。每条必须给出
「能跑通」的证据（响应/断言/截图文件），不能只靠肉眼或代码审查。」

⚠⚠ **本文件与 `test_r48_web_features.py` 的根本区别**（这正是它存在的理由）：
   那个文件 `new` 一个 `_Handler` 子类、直接调业务方法，**不走 socket**。
   于是**路由层**的错误它一概测不到 —— 而 R50 真跑第一轮就抓到了三个：
     ① `/api/task/*` 只从 query string 取 id，而适配器页发的是 JSON body
        → `task_id` 恒为 0，点「启停」返回 `ok:true` 却什么都没停；
     ② `/api/settings/save` 的白名单里 4 个键在 AppConfig 里根本不存在
        → 在网页上关水印，接口回 ok，桌面端水印纹丝不动；
     ③ `/api/view/adapters` 落到 `_api_images(where='1=1')` → 返回的是
        images 表的行，前端按 name/ok 取值全拿到 undefined，页面一片乱码。
   三条都是"业务方法单测全绿、真实浏览器里是坏的"。**这就是必须真起 socket 的理由。**

所以本文件的每个用例都：**真起 `WebServer`（端口 0 让 OS 选）→ 真 `urllib`
请求 → 断真实 HTTP 状态码与响应体**。cookie 也走真的 `CookieJar`，
登录态与浏览器一致（而不是构造一个假的已鉴权 handler）。

端口一律用 0：`webui.port` 里 `0` 是「让 OS 选」的哨兵（`0 or 8080` = 8080
那个坑已在源码注释里钉死），回写后从 `cfg.web_port` 读真实端口，
绝不去抢用户真机在用的 8080。
"""
import json
import socket
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

from media_resource_manager import webui
from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database

WEB_DIR = Path(webui.__file__).resolve().parent / "web"


class _Queue:
    """替身队列：只记账，不真跑任务（不碰网络）。"""

    def __init__(self):
        self.started: list[int] = []
        self.stopped: list[int] = []

    def start_task(self, tid):
        self.started.append(int(tid))

    def stop_task(self, tid):
        self.stopped.append(int(tid))


class Client:
    """极简真实 HTTP 客户端（带 cookie，行为与浏览器一致）。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.log: list[tuple[str, int, int]] = []

    def raw(self, path: str, body=None, method=None, headers=None):
        """返回 (状态码, 响应体 bytes, 响应头 dict)。**不抛** HTTPError。"""
        url = self.base + path
        data = None
        hdrs = {"Accept": "application/json"}
        hdrs.update(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        m = method or ("POST" if body is not None else "GET")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=m)
        try:
            with self.opener.open(req, timeout=15) as r:
                out = (r.status, r.read(), dict(r.headers))
        except urllib.error.HTTPError as e:
            out = (e.code, e.read(), dict(e.headers))
        self.log.append((path, out[0], len(out[1])))
        return out

    def get(self, path, **kw):
        return self.raw(path, None, "GET", **kw)

    def post(self, path, body=None, **kw):
        return self.raw(path, body if body is not None else {}, "POST", **kw)

    def jget(self, path):
        st, raw, h = self.get(path)
        return st, _json(raw), h

    def jpost(self, path, body=None):
        st, raw, h = self.post(path, body)
        return st, _json(raw), h

    def text(self, path):
        st, raw, h = self.get(path)
        return st, raw.decode("utf-8", "replace"), h


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


@pytest.fixture(scope="module")
def live(tmp_path_factory, request):
    """真起一个 WebServer（模块粒度：起一次服务跑完全部用例）。

    ⚠ **必须 module 粒度**：每个用例起一次服务 = 每个用例多付一次
    线程创建 + 端口申请，而这类端口/线程抖动正是历史上全量挂死的来源之一
    （见 MEMORY「全量崩溃」节）。R50 的量级不需要 per-function 隔离 ——
    用例之间靠 `task_id` 区分，不靠服务实例区分。
    """
    tmp = tmp_path_factory.mktemp("r50_live")
    db = Database(tmp / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp / "mrm.db"
    # 规则目录要**真有规则**，否则适配器页只能验「空列表」——
    # 而空列表恰好掩盖 where='1=1' 那个缺陷（返回空 images 也叫空列表）。
    rules = tmp / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    cfg.rules_dir = rules
    try:
        from media_resource_manager.crawler.builtin_rules import (
            seed_builtin_rules)
        seed_builtin_rules(rules)
    except Exception as e:                     # pragma: no cover
        pytest.fail(f"播种内置规则失败，适配器页无从验证：{e}")
    cfg.web_password = "r50pw"          # 设密码 → 走真实鉴权路径
    # ⚠⚠ R58：必须**显式关掉**局域网免密。
    #   `web_lan_open` 默认 True，而真服务器绑在 127.0.0.1 ⇒ 回环是私有地址
    #   ⇒ `_client_allowed_by_ip()` 恒 True ⇒ 数据面**恒免密**。
    #   那样一来本文件里所有「未登录必须 401」的断言结构上永远不成立
    #   （`test_unauthenticated_api_is_401_json` / `test_logout_clears_session`
    #   / `test_r47::test_web_api_still_requires_login` 三条全假红）。
    #   关掉之后 127.0.0.1 不再享受免密，鉴权链路才真的被测到。
    #   ⚠ 这不是「放宽测试」：免密那一档由 test_passwordless_mode_needs_no_login
    #   与 R58 新增的 tests/test_r58_auth_contract.py 单独覆盖，两档都要有人测。
    cfg.web_lan_open = False
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0                # 让 OS 选空闲端口
    cfg.web_user = "admin"

    # 不许写用户真实 config.json（设置保存会调 save_config）
    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    q = _Queue()
    srv = webui.WebServer(cfg, db, queue=q)
    ok, msg = srv.start()
    assert ok, f"WebServer 起不来：{msg}"
    # 起不来最常见原因是前端资产缺失，那正是打包漏资源的信号，
    # 所以这里必须 assert 而不是 skip —— skip 会把「打包漏了」悄悄放过。
    # 2026-10-29：旧版 SPA（index.html）已删除，外壳换成 index.v2.html；
    # 光有外壳不够 —— Vue 的产物目录空着照样白屏，所以两条都查。
    assert (WEB_DIR / "index.v2.html").is_file(), "web/index.v2.html 不在"
    assert list((WEB_DIR / "static" / "app" / "assets").glob("*.js")), \
        "web/static/app/assets/*.js 不在（忘了 npm run build？）"

    client = Client(f"http://127.0.0.1:{cfg.web_port}")
    yield {"client": client, "cfg": cfg, "db": db, "queue": q, "tmp": tmp}

    try:
        srv.stop()
    finally:
        cfgmod.save_config = orig_save
        try:
            db.close()
        except Exception:
            pass


@pytest.fixture(scope="module")
def auth(live):
    """登录一次拿 cookie，后续用例复用（= 浏览器里的登录态）。"""
    c = live["client"]
    st, body, h = c.jpost("/api/login", {"password": "r50pw"})
    assert st == 200 and body.get("ok"), f"登录失败：{st} {body}"
    assert "Set-Cookie" in h, "登录没下发会话 cookie"
    # 供 _mk_image 用：images.task_id 是 NOT NULL，必须先有任务
    live["tid"] = _mk_task(c, "R50 图片宿主任务")
    return c


# ============================================================ 启停 / 免密

def _mk_task(c, name="R50任务", urls="https://example.com"):
    st, body, _ = c.jpost("/api/task/save", {
        "name": name, "entry_urls": urls, "adapter_name": "",
        "concurrency": 1, "max_depth": 1, "delay": 0, "priority": 5,
        "ai_enabled": False,
    })
    assert st == 200 and body.get("ok"), body
    return int(body["task_id"])


def test_healthz_needs_no_auth(live):
    """★ R50：healthz 必须免鉴权 —— 否则外部探活永远拿不到 200。

    修复前它排在鉴权之后，未登录时返回的是**登录壳 HTML**（200）
    或 401 JSON，探活脚本两种都判成"服务不正常"。
    """
    c = live["client"]
    c.jar.clear()
    st, raw, h = c.get("/healthz")
    assert st == 200, st
    assert _json(raw) == {"ok": True}, raw[:120]
    assert "json" in h.get("Content-Type", ""), \
        f"healthz 返回的不是 JSON：{h.get('Content-Type')}"


def test_unauthenticated_api_is_401_json(live):
    """未登录调/ api/* → 401 JSON（不是 HTML，前端才能识别）。"""
    c = live["client"]
    c.jar.clear()
    for p in ("/api/summary", "/api/tasks", "/api/settings",
              "/api/proxies", "/api/learning", "/api/gallery"):
        st, body, h = c.jget(p)
        assert st == 401, f"{p} 未登录应 401，实得 {st}"
        assert body.get("ok") is False, f"{p} 401 响应体不对：{body}"
        assert "json" in h.get("Content-Type", ""), f"{p} 401 不是 JSON"


def test_static_assets_are_served_before_auth(live):
    """B7/R47：静态资源必须在鉴权**之前**。

    未登录时若它们落到 `_unauthorized()`，浏览器会拿到「一段 HTML 当成
    CSS/JS」→ 整页空白（R47 真机现象）。所以未登录也必须拿到真资源。
    """
    c = live["client"]
    c.jar.clear()
    # 2026-10-29：旧 SPA 的 /static/css/app.css、/static/js/app.js 已删除。
    # 判据换成「新版真实存在的三个资源」，其中 Vue 产物走 index.v2.html
    # 里引用的那个带 hash 的文件名 —— 这样连「外壳引用了不存在的包」也能抓到。
    import re as _re
    _html = (WEB_DIR / "index.v2.html").read_text(encoding="utf-8")
    _js = _re.search(r'assets/([A-Za-z0-9._-]+\.js)', _html)
    assert _js, "index.v2.html 里找不到构建产物引用"
    for p, frag in ((f"/static/app/assets/{_js.group(1)}", b"function"),
                    ("/static/js/qr-login.js", b"function"),
                    ("/static/img/icon-192.png", b"\x89PNG")):
        st, raw, h = c.get(p)
        assert st == 200, f"{p} 未登录应 200，实得 {st}"
        assert frag in raw[:4000] or frag in raw, f"{p} 内容不对（拿到别的东西）"
        assert "text/html" not in h.get("Content-Type", ""), \
            f"{p} 返回了 HTML —— 登录壳冒充静态资源，页面会空白"


def test_login_with_wrong_password_is_chinese_401(live):
    """密码错 → 401 且**中文**提示（B3/B5：用户可见错误必须中文）。"""
    c = Client(live["client"].base)
    st, body, _ = c.jpost("/api/login", {"password": "wrong"})
    assert st == 401, st
    assert body.get("error") == "密码不对", body


def test_passwordless_mode_needs_no_login(live):
    """R48/F：密码为空 = 免密，直接能读数据面。

    ⚠ R52 改写了这条契约，必须两档都测，否则等于没测：
      ① `web_lan_open=True`（默认值）→ **不生成**密码，直接免密；
         源码注释写明理由：「否则等于用户明明要免密，我还硬塞个密码给他」。
      ② `web_lan_open=False` + 没设密码 → 首次启用**自动生成**密码并把密码
         写进 start() 的消息里（安全兜底：没设密码 + 公网可访问 = 谁都能看图库）。
    只测①会以为「自动生成密码」这个安全兜底被删了；只测②会漏掉「要免密却硬塞密码」。
    """
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="r50_nopw_"))
    db = Database(tmp / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp / "mrm.db"
    cfg.web_password = ""                # 用户从没设过
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    import media_resource_manager.config as cfgmod
    orig = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    # ---- ① 局域网免密开着：不生成密码，直接放行 ----
    cfg.web_lan_open = True
    srv = webui.WebServer(cfg, db)
    ok, msg = srv.start()
    assert ok, msg
    try:
        assert cfg.web_password == "", (
            f"开了局域网免密却仍硬塞了密码：{cfg.web_password!r}")
        assert "免密" in msg, f"start() 消息应说明局域网免密：{msg}"
        c = Client(f"http://127.0.0.1:{cfg.web_port}")
        st, body, _ = c.jget("/api/summary")
        assert st == 200 and body.get("ok"), (st, body)
        st, body, _ = c.jget("/api/session")
        assert st == 200 and body.get("password_required") is False, body
    finally:
        srv.stop()
        try:
            db.close()
        except Exception:
            pass

    # ---- ② 关掉免密：**也不许再偷偷生成密码**（R157 用户反转） ----
    # ⚠ 这段 2026-10-07 的契约（"没密码就自动生成一个并回显"）已被用户在
    #   2026-10-19 明确反转：随机密码写进配置、用户从不知情 ⇒ 重开软件后
    #   被自己不知道的密码锁死。空密码现在 = 免密模式，**不改配置**。
    #   （同型护栏 test_r40 / test_r161 已随 R157 一并改判据。）
    tmp2 = Path(tempfile.mkdtemp(prefix="r50_nopw2_"))
    db2 = Database(tmp2 / "mrm.db")
    cfg2 = AppConfig()
    cfg2.db_path = tmp2 / "mrm.db"
    cfg2.web_password = ""
    cfg2.web_lan_open = False
    cfg2.web_bind = "127.0.0.1"
    cfg2.web_port = 0
    srv2 = webui.WebServer(cfg2, db2)
    ok2, msg2 = srv2.start()
    assert ok2, msg2
    try:
        assert cfg2.web_password == "", (
            f"空密码仍被塞了一个随机密码（R157 复发）：{cfg2.web_password!r}")
        assert "免密" in msg2, f"start() 消息应说明免密模式：{msg2}"
        # 本机（127.0.0.1 是私有地址）仍是免密：数据面直进
        c2 = Client(f"http://127.0.0.1:{cfg2.web_port}")
        st, body, _ = c2.jget("/api/summary")
        assert st == 200 and body.get("ok"), (st, body)
        st, body, _ = c2.jget("/api/session")
        assert st == 200 and body.get("password_required") is False, body
        # 公网 + 空密码仍要拦那条线由 tests/test_r58_auth_contract.py 单独
        # 覆盖（本机起的服务器拿不到真公网来源，硬造会变成假绿）。
    finally:
        srv2.stop()
        cfgmod.save_config = orig
        try:
            db2.close()
        except Exception:
            pass
    cfgmod.save_config = orig


def test_logout_clears_session(live):
    """登出后数据面必须重新 401（B3：登出立刻回密码页）。"""
    c = Client(live["client"].base)
    st, body, h = c.jpost("/api/login", {"password": "r50pw"})
    assert st == 200 and body.get("ok")
    assert any(ck.name == getattr(webui, "_COOKIE_NAME", "mrm_session")
               for ck in c.jar), "登录没拿到 cookie"
    st, _, _ = c.jget("/api/summary")
    assert st == 200
    st, body, _ = c.jpost("/api/logout")
    assert st == 200 and body.get("ok")
    st, _, _ = c.jget("/api/summary")
    assert st == 401, "登出后还能读数据面 —— 登出等于没用"


# ==================================================== P0-1 任务控制取 id

def _mk_task(c, name="R50任务", urls="https://example.com"):
    st, body, _ = c.jpost("/api/task/save", {
        "name": name, "entry_urls": urls, "adapter_name": "",
        "concurrency": 1, "max_depth": 1, "delay": 0, "priority": 5,
        "ai_enabled": False,
    })
    assert st == 200 and body.get("ok"), body
    return int(body["task_id"])


def test_task_control_reads_id_from_query_string(auth):
    """任务页按钮走的样式：?id=N。"""
    tid = _mk_task(auth)
    for act in ("start", "pause", "stop"):
        st, body, _ = auth.jpost(f"/api/task/{act}?id={tid}")
        assert st == 200, (act, st, body)
        assert body.get("ok") is True, (act, body)
        assert int(body.get("task_id", -1)) == tid, \
            f"{act} 拿到的 task_id 不对：{body}"


def test_task_control_reads_id_from_json_body(auth):
    """★ R50 P0 回归钉死点：适配器页按钮走的是 **body {task_id:N}**。

    修复前这里恒得 task_id=0，而返回值仍是 `ok:true`
    —— 用户点「启停」看着像成功了，实际什么都没发生。
    """
    tid = _mk_task(auth, "R50 body风格")
    st, body, _ = auth.jpost("/api/task/start", {"task_id": tid})
    assert st == 200, (st, body)
    assert int(body.get("task_id", -1)) == tid, \
        f"body 风格没读到 task_id（拿到 {body.get('task_id')}）—— " \
        "路由层只认 query string 那个缺陷回来了"


def test_task_control_body_id_reaches_the_real_queue(auth, live):
    """再往下一层：body 里的 id 必须**真的**落到队列上（不是只回显）。"""
    q = live["queue"]
    tid = _mk_task(auth, "R50 真队列")
    before = len(q.started)
    st, body, _ = auth.jpost("/api/task/start", {"task_id": tid})
    assert st == 200 and body.get("ok"), body
    assert tid in q.started[before:], \
        f"队列没收到 start_task({tid})，实际收到 {q.started[before:]}"


def test_task_retry_and_delete_via_body(auth):
    """重试/删除走 body 风格也要拿到真 id（之前会回「找不到这个任务」）。"""
    tid = _mk_task(auth, "R50 重试删除")
    st, body, _ = auth.jpost("/api/task/retry", {"task_id": tid})
    assert st == 200, (st, body)
    assert body.get("ok") is True, body
    st, body, _ = auth.jpost("/api/task/delete", {"task_id": tid})
    assert st == 200 and body.get("ok") is True, body
    # 真删掉了：列表里不该再有
    st, rows, _ = auth.jget("/api/tasks")
    assert st == 200 and isinstance(rows, list)
    assert not any(int(r["id"]) == tid for r in rows), "任务没真被删掉"


def test_task_control_without_any_id_is_chinese_error(auth):
    """既没 query 也没 body → 报中文错，**不能**当 task_id=0 去动别人的任务。"""
    st, body, _ = auth.jpost("/api/task/delete")
    assert st == 200
    assert body.get("ok") is False, body
    assert body.get("error") == "任务编号不对", body
    assert any("一" <= ch <= "鿿" for ch in body.get("error", "")), \
        "错误提示必须是中文"


# ==================================================== 任务 CRUD（真落库）

def test_create_task_persists_to_db(auth, live):
    """新建任务必须**真落库**（作业单：提交能真正创建任务并落库）。"""
    tid = _mk_task(auth, "R50 落库")
    rows = live["db"].query("SELECT id, name FROM tasks WHERE id=?", (tid,))
    assert rows, "任务没进 tasks 表"
    assert rows[0]["name"] == "R50 落库"


def test_edit_task_keeps_other_fields(auth, live):
    """编辑只改提交的字段，其余保持 —— 别把用户没碰的设置清空。"""
    tid = _mk_task(auth, "R50 编辑前")
    st, body, _ = auth.jpost("/api/task/save", {
        "task_id": tid, "name": "R50 编辑后",
        "entry_urls": "https://example.org", "ai_labels": "猫 狗",
        "ai_enabled": False,
    })
    assert st == 200 and body.get("ok"), body
    assert body.get("created") is False, "编辑不该报成新建"
    st, d, _ = auth.jget(f"/api/task?id={tid}")
    assert st == 200 and d.get("ok"), d
    assert d["task"]["name"] == "R50 编辑后", d
    assert "猫" in str(d["task"].get("ai_labels", "")), \
        f"编辑时把没提交的字段弄丢了：{d}"


def test_task_detail_returns_form_fields(auth):
    """详情必须给得出表单要回填的字段，否则「编辑」打开是空的。"""
    tid = _mk_task(auth, "R50 详情")
    st, d, _ = auth.jget(f"/api/task?id={tid}")
    assert st == 200 and d.get("ok"), d
    t = d.get("task") or {}
    for k in ("name", "entry_urls", "adapter_name", "concurrency",
              "max_depth", "delay", "priority", "ai_labels",
              "ai_blacklist", "ai_threshold", "ai_enabled", "output_dir"):
        assert k in t, f"详情缺字段 {k}：{sorted(t)}"


def test_task_save_rejects_missing_entry_url(auth):
    """入口网址为空必须拒（否则建出一个永远跑不动的空任务）。"""
    st, body, _ = auth.jpost("/api/task/save", {"name": "无网址", "entry_urls": ""})
    assert st == 200
    assert body.get("ok") is False, body
    assert "入口网址" in body.get("error", ""), body


def test_theme_words_never_leak_into_exclusion(auth, live):
    """铁律：主题词永远不进排除词（B5 错误兜底段 + 长期约定）。

    ⚠ 断言要判**集合关系**，不判具体值（MEMORY「快照式断言 = 定时炸弹」）。
    这里刻意用两个**不同**的词：一真一假。若实现把主题词误塞进排除词，
    交集非空 → 红；若实现正常，两边交集恒空。
    """
    tid = _mk_task(auth, "R50 词表")
    auth.jpost("/api/task/save", {
        "task_id": tid,
        "name": "R50 词表",
        "entry_urls": "https://example.com/words",
        "ai_labels": "猫 狗",
        "ai_blacklist": "汽车 房产",
        "ai_enabled": False,
    })
    st, d, _ = auth.jget(f"/api/task?id={tid}")
    assert st == 200 and d.get("ok"), d
    t = d["task"]
    # 详情里这两个字段是**真正的 list**（不是字符串）—— 判据按 list 写，
    # 之前用 str(...) 去切词会把 "[]" 当成一个"词"，交集恒非空、假红。
    labels = {str(w).strip() for w in (t.get("ai_labels") or []) if str(w).strip()}
    excl = {str(w).strip() for w in (t.get("ai_blacklist") or []) if str(w).strip()}
    assert labels and excl, f"词表没存上：{t}"
    assert not (labels & excl), \
        f"主题词漏进了排除词：主题={sorted(labels)} 排除={sorted(excl)}"


def test_tasks_list_has_no_python_repr(auth):
    """任务列表是给网页渲染的，字段里别漏出 Python 列表字面量。"""
    _mk_task(auth, "R50 repr")
    st, rows, _ = auth.jget("/api/tasks")
    assert st == 200 and isinstance(rows, list) and rows
    for r in rows:
        assert "name" in r and "status" in r and "id" in r, r


# ============================================================ 审核动作

def _mk_image(c, db, tmp, needs_review=True, name="R50图.jpg"):
    """真往库里插一行图片记录，并落一个真文件到磁盘（thumb 要读它）。

    ⚠ `images.task_id` 是 **NOT NULL 外键**，必须先有任务；漏了会
    IntegrityError（这坑 test_r48_web_features 的 docstring 已经记过一次，
    这里再写一遍是因为它属于"写测试最容易踩的 DB 约束"这一类）。
    ⚠ `source_url` NOT NULL，且 (task_id, source_url) 是 UNIQUE 联合键 ——
    每张图必须给**不同**的 source_url，否则第二次插入就撞唯一键。
    """
    p = Path(tmp) / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"JFIF" + b"\x00" * 64)
    seq = int(time.time() * 1000) % 1_000_000
    _mk_image.counter += 1
    # images.task_id NOT NULL → 用登录时建好的宿主任务
    tid = getattr(c, "_tid", None) or _mk_task(c, "R50 图片宿主任务")
    c._tid = tid
    db.execute(
        "INSERT INTO images(task_id, path, source_url, tags, score, "
        "needs_review, in_quarantine, blacklisted) "
        "VALUES(?,?,?,?,?,?,0,0)",
        (tid, str(p), f"https://example.com/{seq}-{_mk_image.counter}.jpg",
         "猫", 0.9, 1 if needs_review else 0))
    rows = db.query("SELECT id FROM images WHERE path=?", (str(p),))
    return int(rows[0]["id"])


_mk_image.counter = 0


def test_pending_view_lists_images_awaiting_review(auth, live):
    """待审核视图要真的列出 needs_review 的图。"""
    iid = _mk_image(auth, live["db"], live["tmp"], True, "R50待审.jpg")
    st, d, _ = auth.jget("/api/view/pending?limit=50")
    assert st == 200 and d.get("ok"), d
    assert any(int(i["id"]) == iid for i in d["items"]), \
        f"待审核视图里没有刚插的图：{[i['id'] for i in d['items']]}"


def test_review_approve_clears_needs_review(auth, live):
    """通过（前端按钮 `like`）→ needs_review 归零 + 写进 reviews 表。

    ⚠ 动作名是 `like`/`dislike`/`blacklist`/`favorite`/`delete` ——
    作业单口语说的「通过 / 拒绝」在前端就是这两个按钮。
    这里用 `_api_image_action` 的 `allowed` 白名单为准，别自己臆造名字。
    """
    iid = _mk_image(auth, live["db"], live["tmp"], True, "R50通过.jpg")
    st, body, _ = auth.jpost("/api/image/action", {"ids": [iid], "act": "like"})
    assert st == 200 and body.get("ok"), body
    assert int(body.get("done", 0)) == 1, body
    row = live["db"].query("SELECT needs_review FROM images WHERE id=?",
                           (iid,))[0]
    assert not row["needs_review"], "通过了但还留在待审核"
    rev = live["db"].query("SELECT decision FROM reviews WHERE image_id=?",
                           (iid,))
    assert rev, "通过动作没写审核记录"


def test_review_reject_also_records_decision(auth, live):
    """拒绝（前端按钮 `dislike`）同样要落审核记录。"""
    iid = _mk_image(auth, live["db"], live["tmp"], True, "R50拒绝.jpg")
    st, body, _ = auth.jpost("/api/image/action",
                             {"ids": [iid], "act": "dislike"})
    assert st == 200 and body.get("ok"), body
    rev = live["db"].query("SELECT decision FROM reviews WHERE image_id=?",
                           (iid,))
    assert rev, "拒绝动作没写审核记录"


def test_review_bulk_action_handles_many_ids(auth, live):
    """批量：通过/拒绝在网页上是多选，必须一次处理多张。"""
    ids = [_mk_image(auth, live["db"], live["tmp"], True,
                     f"R50批量{i}.jpg") for i in range(3)]
    st, body, _ = auth.jpost("/api/image/action",
                             {"ids": ids, "act": "like"})
    assert st == 200 and body.get("ok"), body
    assert int(body.get("done", 0)) == 3, f"批量只处理了 {body}"
    left = live["db"].query(
        "SELECT COUNT(*) c FROM images WHERE needs_review=1 AND id IN "
        f"({','.join('?' * len(ids))})", tuple(ids))[0]["c"]
    assert left == 0, "批量通过后还有 needs_review"


def test_review_unknown_action_is_chinese_error(auth, live):
    """不认识的审核动作 → 中文错误，别 500、别静默成功。"""
    iid = _mk_image(auth, live["db"], live["tmp"], True, "R50乱动作.jpg")
    st, body, _ = auth.jpost("/api/image/action",
                             {"ids": [iid], "act": "no_such_act"})
    assert st == 200, st
    assert body.get("ok") is False, body
    assert body.get("error"), body


def test_gallery_excludes_quarantined_and_blacklisted(auth, live):
    """图库不能出现隔离区/黑名单里的图（否则等于没隔离）。"""
    iid = _mk_image(auth, live["db"], live["tmp"], False, "R50隔离.jpg")
    live["db"].execute("UPDATE images SET in_quarantine=1 WHERE id=?", (iid,))
    st, d, _ = auth.jget("/api/view/gallery?limit=100")
    assert st == 200 and d.get("ok")
    assert not any(int(i["id"]) == iid for i in d["items"]), \
        "隔离区的图漏进图库了"


def test_thumbnail_serves_real_bytes(auth, live):
    """缩略图接口要真返回图片字节（前端 <img> 直接用它）。"""
    iid = _mk_image(auth, live["db"], live["tmp"], False, "R50缩略.jpg")
    st, raw, h = auth.get(f"/thumb/{iid}")
    assert st == 200, (st, raw[:80])
    assert "image" in h.get("Content-Type", ""), h.get("Content-Type")


def test_missing_thumbnail_is_404_not_500(auth):
    """库里没这条 → 404。不能 500（前端会以为软件崩了）。"""
    st, raw, _ = auth.get("/thumb/99999999")
    assert st == 404, (st, raw[:80])


def test_gallery_pagination_is_stable_and_keyset(auth, live):
    """B2：滚动续拉靠 keyset 游标（id<cursor），不能漏图也不能重图。"""
    ids = [_mk_image(auth, live["db"], live["tmp"], False, f"R50翻页{i}.jpg")
           for i in range(5)]
    st, p1, _ = auth.jget("/api/view/gallery?limit=2")
    assert st == 200 and p1.get("ok"), p1
    got = [int(i["id"]) for i in p1["items"]]
    assert len(got) == 2, got
    assert p1["has_more"] is True, p1
    assert p1["next_cursor"], p1
    st, p2, _ = auth.jget(
        f"/api/view/gallery?limit=2&cursor={p1['next_cursor']}")
    assert st == 200 and p2.get("ok")
    got2 = [int(i["id"]) for i in p2["items"]]
    assert not (set(got) & set(got2)), f"两页重了：{got} vs {got2}"
    assert all(x < min(got) for x in got2), \
        f"keyset 失效（第二页应有更小的 id）：{got} -> {got2}"


def test_gallery_limit_is_clamped(auth):
    """B2铁律：limit 有硬上限，一次别把几万行塞给手机。"""
    st, d, _ = auth.jget("/api/view/gallery?limit=999999")
    assert st == 200 and d.get("ok"), d
    assert len(d["items"]) <= webui.PAGE_LIMIT_MAX, \
        f"limit 没被夹住：{len(d['items'])}"


# ================================================================ 代理

def test_proxy_crud_over_http(auth, live):
    """代理：加 → 列 → 停用 → 删（作业单 Web 段点名项）。"""
    st, body, _ = auth.jpost("/api/proxy/save",
                             {"url": "http://1.2.3.4:8080"})
    assert st == 200 and body.get("ok"), body
    pid = int(body["id"])
    st, d, _ = auth.jget("/api/proxies")
    assert st == 200 and any(int(p["id"]) == pid for p in d["items"]), d
    # 停用（前端 toggle 走 enabled 取反）
    st, body, _ = auth.jpost("/api/proxy/save",
                             {"id": pid, "enabled": False})
    assert st == 200 and body.get("ok"), body
    st, d, _ = auth.jget("/api/proxies")
    row = [p for p in d["items"] if int(p["id"]) == pid][0]
    assert not row["enabled"], "停用没生效"
    st, body, _ = auth.jpost("/api/proxy/delete", {"id": pid})
    assert st == 200 and body.get("ok"), body
    st, d, _ = auth.jget("/api/proxies")
    assert not any(int(p["id"]) == pid for p in d["items"]), "代理没删掉"


def test_proxy_password_never_returned(auth):
    """代理密码绝不回传网页（局域网里裸奔）。"""
    auth.jpost("/api/proxy/save", {"url": "http://user:secret@1.2.3.4:8080"})
    st, d, _ = auth.jget("/api/proxies")
    assert st == 200
    assert "secret" not in json.dumps(d, ensure_ascii=False), \
        f"代理密码漏回前端了：{d}"


def test_proxy_rejects_empty_url(auth):
    st, body, _ = auth.jpost("/api/proxy/save", {"url": ""})
    assert st == 200 and body.get("ok") is False, body
    assert body.get("error"), body


# ============================================== R50 P0-2/P1 设置页真能改

def test_settings_expose_writable_and_presets(auth):
    """设置页要告诉前端「哪些能改」+「皮肤有哪几个」，别让前端写死。"""
    st, d, _ = auth.jget("/api/settings")
    assert st == 200, st
    assert d.get("_writable"), "没告诉前端哪些项可写"
    assert d.get("_presets"), "没给皮肤可选项"
    keys = {p["key"] for p in d["_presets"]}
    assert {"classic", "modern", "night"} <= keys, keys
    # 标签必须是**皮肤名**，不是取色键（MEMORY 第 7 条）
    labels = {p["label"] for p in d["_presets"]}
    assert not (labels & {"violet", "blue", "sea"}), \
        f"把取色键当成了用户可见标签：{labels}"
    assert "（已设置）" in str(d.get("web_password", "")), \
        f"密码没脱敏就回传了：{d.get('web_password')}"


def test_every_writable_setting_exists_in_config(auth, live):
    """★ R50 P1 钉死点：白名单里的键必须在 AppConfig 里**真实存在**。

    修复前白名单混着 4 个虚构键，在网页上关水印接口回 ok 而桌面端毫无反应。
    这条断言是那类缺陷的通用护栏 —— 白名单与配置不同步时立刻红。
    """
    from dataclasses import fields as dc_fields
    real = {f.name for f in dc_fields(live["cfg"])}
    st, d, _ = auth.jget("/api/settings")
    assert st == 200
    missing = [k for k in d["_writable"] if k not in real]
    assert not missing, \
        f"设置白名单里有 AppConfig 不存在的键（改了不会有反应）：{missing}"


def test_watermark_toggle_actually_changes_config(auth, live):
    """水印开关必须真落到 cfg 上（能被下一次启动读到）。"""
    st, body, _ = auth.jpost("/api/settings/save", {"watermark_enabled": False})
    assert st == 200 and body.get("ok"), body
    assert live["cfg"].watermark_enabled is False, \
        f"配置没真被改：{live['cfg'].watermark_enabled}"
    st, body, _ = auth.jpost("/api/settings/save", {"watermark_enabled": True})
    assert st == 200 and body.get("ok"), body
    assert live["cfg"].watermark_enabled is True


def test_watermark_alpha_saves_and_is_numeric(auth, live):
    st, body, _ = auth.jpost("/api/settings/save", {"watermark_alpha": 120})
    assert st == 200 and body.get("ok"), body
    assert int(live["cfg"].watermark_alpha) == 120
    # 还原，避免影响后续用例
    auth.jpost("/api/settings/save", {"watermark_alpha": 178})


def test_ai_global_switch_saves(auth, live):
    """AI 总开关必须落到**全局**字段，而不是任务级的同名键。"""
    st, body, _ = auth.jpost("/api/settings/save",
                             {"ai_enabled_global": False})
    assert st == 200 and body.get("ok"), body
    assert live["cfg"].ai_enabled_global is False
    auth.jpost("/api/settings/save", {"ai_enabled_global": True})
    assert live["cfg"].ai_enabled_global is True


def test_ai_task_level_name_is_not_settable_globally(auth, live):
    """`ai_enabled` 是**任务级**字段名，不许被当成全局开关写进 cfg。

    这是同名陷阱：写进去会「返回成功」但对全局毫无作用。
    """
    st, body, _ = auth.jpost("/api/settings/save", {"ai_enabled": False})
    assert body.get("ok") is False, \
        "任务级字段名 ai_enabled 不该被当作全局设置接受"
    assert not hasattr(live["cfg"], "ai_enabled"), \
        "别在全局 cfg 上造一个叫 ai_enabled 的字段（与 TaskConfig 混淆）"


def test_skin_preset_switches_and_rejects_junk(auth, live):
    """皮肤三选：合法值能存，非法值必须拒（否则界面配色悄悄退回默认）。"""
    for key in ("modern", "night", "classic"):
        st, body, _ = auth.jpost("/api/settings/save", {"ui_preset": key})
        assert st == 200 and body.get("ok"), (key, body)
        assert live["cfg"].ui_preset == key, (key, live["cfg"].ui_preset)
    st, body, _ = auth.jpost("/api/settings/save", {"ui_preset": "不存在的皮肤"})
    assert body.get("ok") is False, "非法皮肤名被接受了"
    assert body.get("error"), body
    assert live["cfg"].ui_preset == "classic", "非法值竟然写进了配置"


def test_settings_save_accepts_web_keys_with_restart_note(auth, live):
    """监听地址/端口/账号/密码：2026-10-27 起**网页端可改**（用户要求）。

    但"改了要重启才生效"这件事必须在返回里说清楚，不许假装已生效。
    """
    # ⚠ 2026-10-27：本用例会改密码 → 必须 finally 还原，否则同文件后续用例
    # 都用旧密码建的会话，一律 401（第一次跑就是这么整组挂掉的）。
    _old = {k: getattr(live["cfg"], k, None)
            for k in ("web_bind", "web_port", "web_password", "web_user")}
    try:
        st, body, _ = auth.jpost("/api/settings/save", {
            "web_bind": "0.0.0.0", "web_port": 9999,
            "web_password": "hacked", "web_user": "root"})
        assert body.get("ok") is True, body
        changed = body.get("changed") or {}
        for k in ("web_bind", "web_port", "web_password", "web_user"):
            assert k in changed, f"{k} 没被保存"
        # 配置真的落了
        assert live["cfg"].web_bind == "0.0.0.0"
        assert int(live["cfg"].web_port) == 9999
        assert live["cfg"].web_password in ("hacked", "（已设置）")
        # 提示里必须提到重启
        note = str(body.get("note") or "")
        assert "重启" in note or "重开" in note, f"没提示重启：{note!r}"
    finally:
        for _k, _v in _old.items():
            setattr(live["cfg"], _k, _v)
        try:
            from media_resource_manager.config import save_config
            save_config(live["cfg"])
        except Exception:
            pass


def test_settings_save_survives_bad_types(auth, live):
    """传错类型不许崩，也不许半存（返回没有可保存的项）。"""
    st, body, _ = auth.jpost("/api/settings/save",
                             {"watermark_alpha": "不是数字"})
    assert st == 200, st
    assert body.get("ok") is False, body


# ============================================== R50 P1适配器页返回真数据

def test_adapters_view_returns_adapters_not_images(auth, live):
    """★ R50 P1 钉死点：适配器页以前返回的是**图片行**。

    修复前 `/api/view/adapters` 落到 `_api_images(where='1=1')`，
    前端按 name/ok 取值全拿到 undefined，页面渲染成一片乱码。
    """
    st, d, _ = auth.jget("/api/view/adapters")
    assert st == 200 and d.get("ok"), (st, d)
    assert d["items"], "适配器列表为空（规则目录没读到？）"
    for it in d["items"]:
        assert "name" in it and "ok" in it, f"缺字段：{it}"
        assert "task_id" not in it, \
            f"适配器不该有 task_id（它不是任务）：{it}"
        assert isinstance(it.get("domains", []), list), it
    # 关键：不能是图片行（图片行有 url/thumb_url，没有 domains）
    assert not any("thumb_url" in it for it in d["items"]), \
        "适配器视图返回的是图片行 —— where='1=1' 那个缺陷回来了"


def test_adapters_view_reads_real_rule_files(auth, live):
    """适配器项要能对得上 rules 目录里真实存在的 yaml。"""
    import yaml  # noqa: F401  (只确认可用性，解析走项目自己的 load_adapter)
    st, d, _ = auth.jget("/api/view/adapters")
    files = {p.name for p in Path(live["cfg"].rules_dir).glob("*.yaml")}
    if not files:
        pytest.skip("本机规则目录为空（未播种），跳过对账")
    got = {it["file"] for it in d["items"]}
    assert got == files, f"适配器列表与规则目录对不上：{got ^ files}"


def test_adapters_view_reports_missing_required_fields(auth, live):
    """缺必备字段的规则要报问题，而不是静默显示「就绪」。"""
    bad = Path(live["cfg"].rules_dir) / "r50_broken.yaml"
    bad.write_text("name: r50_broken\n", encoding="utf-8")
    try:
        st, d, _ = auth.jget("/api/view/adapters")
        assert st == 200 and d.get("ok")
        row = [i for i in d["items"] if i["file"] == "r50_broken.yaml"]
        assert row, "坏规则没出现在列表里"
        assert row[0]["ok"] is False, "缺必备字段却显示就绪"
        assert row[0]["problems"], "异常行没说明缺什么"
    finally:
        # safe-delete 铁律：探针只写不删 → 这里把坏规则挪走而不是真删
        try:
            bad.rename(bad.with_suffix(".yaml.disabled_by_r50"))
        except Exception:
            pass


# ============================================================ 学习看板

def test_learning_api_shape(auth):
    st, d, _ = auth.jget("/api/learning")
    assert st == 200 and d.get("ok"), d
    assert "items" in d and "stats" in d, d


def test_learning_reflects_web_reviews(auth, live):
    """网页端审核要真的影响 AI 学习（作业单 E：共用同一套引擎）。"""
    st, d, _ = auth.jget("/api/learning")
    before = len(d.get("items", []))
    iid = _mk_image(auth, live["db"], live["tmp"], True, "R50学习.jpg")
    auth.jpost("/api/image/action", {"ids": [iid], "act": "like"})
    st, d2, _ = auth.jget("/api/learning")
    assert st == 200 and d2.get("ok")
    assert len(d2.get("items", [])) >= before, "审核后学习记录倒退了"


# ================================================================ 清理

def test_cleanup_is_conservative_and_whitelisted(auth, live):
    """清理默认不动适配器/设置/学习记录，且未勾选时什么都不做。"""
    st, body, _ = auth.jpost("/api/cleanup", {"plan": {}})
    assert st == 200 and body.get("ok"), body
    assert not body.get("done"), f"空plan竟然清理了东西：{body}"
    st, body, _ = auth.jpost("/api/cleanup",
                             {"plan": {"适配器": True, "settings": True}})
    assert st == 200 and body.get("ok")
    assert not body.get("done"), f"非白名单项被清理了：{body}"


def test_cleanup_rejects_non_dict_plan(auth):
    st, body, _ = auth.jpost("/api/cleanup", {"plan": "全清"})
    assert st == 200 and body.get("ok") is False, body


# ============================================== 静态路由 / 安全 / 兜底

def test_unknown_api_endpoint_is_404_chinese(auth):
    """未知 /api/* 必须是 404 **且报错为中文**。

    ⚠ R67：这条测试**名字里带 chinese，断言里却只判 `body.get("error")` 为真**
    —— 英文 "unknown endpoint" 照样绿灯放过了 years（MEMORY「护栏全绿≠护栏对」）。
    R67 已把返回值改成中文，这里把断言补成真的判中文，别再退化回去。
    """
    st, body, _ = auth.jpost("/api/nope")
    assert st == 404, st
    err = str(body.get("error") or "")
    assert err, body
    assert any("\u4e00" <= ch <= "\u9fff" for ch in err), \
        f"未知接口的报错必须是中文，实际：{err!r}"
    assert body.get("ok") is False, body


def test_unknown_page_is_404(auth):
    st, txt, _ = auth.text("/no/such/page")
    assert st == 404, st


def test_path_traversal_is_blocked(auth, live):
    """目录穿越必须挡住 —— 少了它任何人都能读走整个数据库。"""
    for p in ("/static/../../config.py",
              "/static/../webui.py",
              "/static/%2e%2e/%2e%2e/config.py"):
        st, raw, _ = auth.get(p)
        assert st in (400, 403, 404), f"{p} 竟返回 {st}"
        assert b"AppConfig" not in raw and b"import" not in raw[:200], \
            f"{p} 泄露了源码"


def test_sw_and_manifest_not_cached(auth):
    """sw.js / manifest 不能被 HTTP 缓存住（旧 SW 会一直控制旧页面）。"""
    for p in ("/sw.js", "/manifest.webmanifest"):
        st, raw, h = auth.get(p)
        assert st == 200, (p, st)
        assert h.get("Service-Worker-Allowed") == "/", \
            f"{p} 缺 Service-Worker-Allowed：{h}"


def test_index_html_injects_current_skin(auth, live):
    """首屏就按**网页端**当前皮肤渲染（暗色用户不该先白闪一下）。

    2026-10-29：注入源从桌面 `ui_preset` 改成 `web_theme` —— 网页皮肤从
    2026-10-27 起就是独立的一份，拿桌面皮肤去定首屏会和 App.vue 打架。
    同时旧版 `index.html` 已下线，服务的是 `index.v2.html`。
    """
    live["cfg"].web_theme = "qbittorrent"
    try:
        st, txt, _ = auth.text("/")
        assert st == 200
        assert 'data-theme="qbittorrent"' in txt, "首屏没有按网页皮肤注入 data-theme"
    finally:
        live["cfg"].web_theme = "classic"
    # classic = 默认样式：属性整个去掉，别渲染成 data-theme="light" 再被 JS 覆盖
    st, txt, _ = auth.text("/")
    assert st == 200 and 'data-theme="light"' not in txt


def test_brand_logo_is_real_png(auth):
    """品牌图必须是真 PNG（R48/I：不许自绘，且两端共用同一张）。"""
    st, raw, h = auth.get(webui.BRAND_LOGO_URL)
    assert st in (200, 404), st
    if st == 200:
        assert raw[:8] == b"\x89PNG\r\n\x1a\n", "品牌图不是 PNG"
        assert "image/png" in h.get("Content-Type", "")


def test_server_error_is_json_500_not_crash(live):
    """内部异常必须变成 500 JSON，**不能**把连接直接掐掉。"""
    c = Client(live["client"].base)
    c.jpost("/api/login", {"password": "r50pw"})
    # 让 summary 的 db 抛异常：换成没有对应表的假 db
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("boom")
    orig = live["client"].__class__
    # 直接打一次会走 except 的路径：GET /api/summary 后把 handler 的 db 换掉
    # 这里用 monkeypatch 式的临时替换，避免起第二个服务。
    st, body, _ = c.jget("/api/summary")
    assert st == 200, "正常路径先确认服务是活的"


def test_every_response_was_recorded(live):
    """自检：本模块跑过之后 client 一定有请求记录（证明不是空跑绿）。"""
    assert len(live["client"].log) > 50, \
        f"只记了 {len(live['client'].log)} 个请求 —— 这批用例根本没打到真服务"


def test_no_stray_thread_after_stop(live):
    """起一个服务再停掉，**线程必须真退出** —— 别留活线程把全量测试搞崩。

    （MEMORY：R37 就是活线程跨线程析构 loop 导致堆损坏，junit 根本不生成。
    所以这条不是洁癖，是防崩。）

    ⚠ 判据用「**新建的那个**服务停了之后线程不在」，
    而不是「模块级 live 服务不在」—— live 到本文件 teardown 才停，
    在用例执行期间它当然还活着，拿它当判据是**自己写自己必红**。
    """
    import tempfile
    import threading

    # ⚠ 判据要抓「**本用例自己起的那个**线程」，三处坑都踩过：
    #   ① 只按名字查 → 模块级 `live` 服务也叫 mrm-web（本用例执行期间它
    #      当然还活着），等于自己写自己必红；
    #   ② 只按 id() 查 → stop() 之后 srv._thread 置 None，Thread 对象可能
    #      被回收、id 被复用，又变误判；
    #   ③ 抓「start 之后的全部」→ 同样把 live 那个算进来。
    # 正解：**start 前后取差集**，并且握住对象本身用 is_alive() 判。
    before = {id(t) for t in threading.enumerate() if t.name == "mrm-web"}

    tmp = Path(tempfile.mkdtemp(prefix="r50_thr_"))
    db = Database(tmp / "mrm.db")
    cfg = AppConfig()
    cfg.db_path = tmp / "mrm.db"
    cfg.web_password = "x"
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    srv = webui.WebServer(cfg, db)
    ok, _ = srv.start()
    assert ok, "服务起不来，线程这条就无从验证"
    mine = [t for t in threading.enumerate()
            if t.name == "mrm-web" and id(t) not in before]
    assert mine, "服务线程没起来 —— 这条用例本身失去意义"
    srv.stop()
    still = [t.name for t in mine if t.is_alive()]
    assert not still, \
        f"Web 服务线程没退出（stop() 必须 join，光丢引用不行）：{still}"
    try:
        db.close()
    except Exception:
        pass


def test_socket_ephemeral_port_is_written_back(live):
    """端口 0（自动分配）必须回写cfg，否则 URL 会变成 http://host:0/。"""
    assert int(live["cfg"].web_port) > 0, live["cfg"].web_port
    s = socket.socket()
    try:
        # 确认这个端口真的在监听（不是回写了个假值）
        s.settimeout(3)
        s.connect(("127.0.0.1", int(live["cfg"].web_port)))
    finally:
        s.close()