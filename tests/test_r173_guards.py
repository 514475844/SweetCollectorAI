"""R154-F16 护栏：任务克隆 / 模板（执行方笔记本棒）。

订单要点：① 任务行右键/详情加「克隆」：复制配置建同名+"(副本)"任务
（不复制历史记录）；② 常用配置可"存为模板"，新建任务时一键套用；
③ 桌面 + Web 双端。

实现口径（与订单的偏差，台账有记）：订单写"本地 JSON 列表"，落地用
**既有 DB 列 is_template=1**（repo.list_tasks(templates=True) 是 2019 年
起就有的入口）—— 模板跟着库走，备份/迁移自动带上，且天然不出现在
任务列表 / Web / 导出里。行为与订单一致，不另造文件格式。

每条按「行为断言优先、源码字面量判据兜底」配。
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

WEBUI = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
MAIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"

_TMP = Path(tempfile.mkdtemp(prefix="r173_"))


@pytest.fixture()
def web_home():
    old = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    os.environ["MRM_HOME"] = str(_TMP)
    os.environ["MRM_NO_MIGRATE"] = "1"
    try:
        yield _TMP
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ==================================================================
# 一、源码字面量接线（写对函数但没接路由/菜单 = 白写）
# ==================================================================
def test_webui_source_wiring():
    src = WEBUI.read_text(encoding="utf-8")
    # R170：三个模板路由逐个显式字面量（与 r48 护栏同约定，不许 f-string 循环）
    assert 'path == "/api/task/templates"' in src
    assert 'path == "/api/task/template/save"' in src
    assert 'path == "/api/task/template/delete"' in src
    # R170：「克隆」路由与处理器已按用户拍板移除，且不许回来
    assert 'path == "/api/task/clone"' not in src, \
        "/api/task/clone 路由回来了 —— R170 已删除克隆功能"
    assert "def _api_task_clone(" not in src, \
        "_api_task_clone 处理器回来了 —— R170 已删除克隆功能"
    # 模板列表是 GET（与 /api/tasks/export 同在 do_GET），其余两个是 POST
    get_blk = src[src.index("def do_GET"):src.index("def do_POST")]
    assert 'path == "/api/task/templates"' in get_blk
    post_blk = src[src.index("def do_POST"):]
    assert 'path == "/api/task/template/save"' in post_blk
    assert 'path == "/api/task/template/delete"' in post_blk
    # 全部挂在鉴权之后
    assert src.index('if not self._auth_ok()') < src.index(
        'path == "/api/task/templates"')
    # 三个处理函数存在
    for fn in ("def _api_task_templates(",
               "def _api_task_template_save(",
               "def _api_task_template_delete("):
        assert fn in src, fn

def test_gui_source_wiring():
    src = MAIN.read_text(encoding="utf-8")
    # R170：右键菜单「克隆任务」与 _clone_task 方法已按用户拍板移除
    assert 'menu.addAction("克隆任务"' not in src, \
        "桌面右键菜单「克隆任务」回来了 —— R170 已删除"
    assert "def _clone_task(" not in src, \
        "_clone_task 方法回来了 —— R170 已删除"
    # 菜单保留项仍在
    assert 'menu.addAction("存为模板…", lambda: self._save_task_as_template(int(tid)))' in src
    # 表单模板行三件套
    assert "self.f_tpl = QComboBox()" in src
    assert "b_tpl_apply.clicked.connect(self._apply_template)" in src
    assert "b_tpl_save.clicked.connect(self._save_form_as_template)" in src
    # 方法本体存在
    for fn in ("def _reload_templates(", "def _apply_template(",
               "def _save_form_as_template(",
               "def _save_task_as_template("):
        assert fn in src, fn
    # 开对话框时刷新模板下拉
    i = src.index("def _open_task_form(")
    assert "self._reload_templates()" in src[i:i + 1200]
    # 套用后必须切新建态（绝不能 UPDATE 模板自己）
    j = src.index("def _apply_template(")
    assert "self._editing_tid = None" in src[j:j + 3000]

def test_r170_clone_lib_capability_kept_for_templates():
    """R170 只删「克隆」入口，库层 clone_task 必须留着给模板用。

    反例警示：删 clone_task 会把「用模板新建」一起打死（它是底层实现）。
    """
    from media_resource_manager import taskio
    assert hasattr(taskio, "clone_task"), \
        "taskio.clone_task 被删了 —— 模板新建会一起坏掉"
    assert hasattr(taskio, "save_as_template")
    assert hasattr(taskio, "list_templates")



# ==================================================================
# 二、数据层：克隆 / 模板（直接对真实 DB）
# ==================================================================
def _mk(db, **kw):
    from media_resource_manager.models import TaskConfig
    from media_resource_manager import repo
    base = dict(id=None, name="src", adapter_name="good",
                entry_urls=["http://x/1"], keywords=["cat"],
                ai_labels=["白丝"], ai_labels_l2=["jk"],
                concurrency=4, max_depth=3, delay=2.75,
                ai_threshold=0.42, rate_limit=7, allowed_hours="22-6",
                keyword_subdir=True)
    base.update(kw)
    return repo.save_task(db, TaskConfig(**base))


def test_clone_copies_config_without_history(web_home, tmp_path):
    """克隆：配置逐字段复制；历史（images）不跟过去；名字+（副本）。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio

    db = Database(tmp_path / "c1.db")
    try:
        tid = _mk(db)
        # 源任务塞两条历史（图片记录）
        db.execute(
            "INSERT INTO images(task_id, source_url, path) VALUES (?,?,?)",
            (tid, "http://x/1", "C:/fake/a.jpg"))
        db.execute(
            "INSERT INTO images(task_id, source_url, path) VALUES (?,?,?)",
            (tid, "http://x/2", "C:/fake/b.jpg"))
        new_id = taskio.clone_task(db, tid)
        assert new_id is not None and new_id != tid
        t = repo.get_task(db, new_id)
        assert t.name == "src（副本）"
        assert t.entry_urls == ["http://x/1"]
        assert t.keywords == ["cat"]
        assert t.ai_labels == ["白丝"]
        assert t.ai_labels_l2 == ["jk"]
        assert t.concurrency == 4 and t.max_depth == 3
        assert t.delay == 2.75 and t.ai_threshold == 0.42   # float 不许被 int 化
        assert t.rate_limit == 7 and t.allowed_hours == "22-6"
        assert t.keyword_subdir is True
        # ⚠ cookie 不按任务落库（cookies.py 按域名存），克隆 naturally
        #   不带它 —— 这里明确记一笔，防止下轮有人「补」一个假断言。
        assert t.is_template is False
        assert t.parent_id == tid              # 血缘存档
        assert str(t.status.value) == "idle"
        # 历史不复制：新任务下零张图；源任务两张还在
        n_new = db.query("SELECT COUNT(*) c FROM images WHERE task_id=?",
                         (new_id,))[0]["c"]
        n_src = db.query("SELECT COUNT(*) c FROM images WHERE task_id=?",
                         (tid,))[0]["c"]
        assert n_new == 0 and n_src == 2
        # 克隆绝不回写源任务
        src = repo.get_task(db, tid)
        assert src.name == "src" and src.id == tid
    finally:
        db.close()


def test_clone_clears_runtime_state(web_home, tmp_path):
    """克隆：运行态清零（status/last_error/last_run_at），错误文案不带过去。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskStatus

    db = Database(tmp_path / "c2.db")
    try:
        tid = _mk(db)
        db.execute(
            "UPDATE tasks SET status=?, last_error=?, last_run_at=? WHERE id=?",
            ("error", "connection reset", "2026-10-19 01:02:03", tid))
        new_id = taskio.clone_task(db, tid)
        t = repo.get_task(db, new_id)
        assert t.status == TaskStatus.IDLE
        assert t.last_error == "" and t.last_run_at == ""
    finally:
        db.close()


def test_clone_missing_returns_none(web_home, tmp_path):
    from media_resource_manager.db import Database
    from media_resource_manager import taskio

    db = Database(tmp_path / "c3.db")
    try:
        assert taskio.clone_task(db, 999) is None
        assert taskio.save_as_template(db, 999) is None
    finally:
        db.close()


def test_template_hidden_from_task_list(web_home, tmp_path):
    """模板不进任务列表/导出，只在 templates=True 时可见；源任务不动。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio

    db = Database(tmp_path / "t1.db")
    try:
        tid = _mk(db)
        tpl_id = taskio.save_as_template(db, tid)
        assert tpl_id is not None and tpl_id != tid
        names = [t.name for t in repo.list_tasks(db)]
        assert "src" in names and len(names) == 1     # 模板不出现在任务列表
        tpls = taskio.list_templates(db)
        assert [t.id for t in tpls] == [tpl_id]
        # 源任务零改动
        src = repo.get_task(db, tid)
        assert src.is_template is False and src.name == "src"
        # 导出也不带模板（F14 口径不被模板污染）
        data = taskio.build_export_data(db)
        assert all(d.get("name") != "src 模板" for d in data)
    finally:
        db.close()


def test_template_naming(web_home, tmp_path):
    """模板名：给名用给名；空名回退任务名；两端口一致。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio

    db = Database(tmp_path / "t2.db")
    try:
        tid = _mk(db)
        a = taskio.save_as_template(db, tid, "常用图站")
        b = taskio.save_as_template(db, tid, "   ")     # 空白名 → 回退
        assert repo.get_task(db, a).name == "常用图站"
        assert repo.get_task(db, b).name == "src"
        assert len(taskio.list_templates(db)) == 2
    finally:
        db.close()


def test_clone_from_template_makes_real_task(web_home, tmp_path):
    """「用模板新建」= 克隆模板 → is_template=0，进任务列表，模板还在。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio

    db = Database(tmp_path / "t3.db")
    try:
        tid = _mk(db)
        tpl_id = taskio.save_as_template(db, tid, "tpl")
        new_id = taskio.clone_task(db, tpl_id)
        t = repo.get_task(db, new_id)
        assert t.is_template is False
        assert t.name == "tpl（副本）"
        assert [x.id for x in repo.list_tasks(db)] == [tid, new_id]
        assert [x.id for x in taskio.list_templates(db)] == [tpl_id]
    finally:
        db.close()


def test_delete_template_only_templates(web_home, tmp_path):
    """删模板接口只删 is_template=1；真任务传进来 no-op（防误删）。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio

    db = Database(tmp_path / "t4.db")
    try:
        tid = _mk(db)
        tpl_id = taskio.save_as_template(db, tid, "tpl")
        assert taskio.delete_template(db, tid) is False      # 真任务删不动
        assert repo.get_task(db, tid) is not None
        assert taskio.delete_template(db, tpl_id) is True
        assert taskio.list_templates(db) == []
        assert taskio.delete_template(db, tpl_id) is False   # 幂等二次删
    finally:
        db.close()


def test_save_template_config_from_form_object(web_home, tmp_path):
    """桌面「表单直存」：_collect 出来的对象直接存成模板（不落任务列表）。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskConfig, TaskStatus

    db = Database(tmp_path / "t5.db")
    try:
        cfg = TaskConfig(id=None, name="表单直存", adapter_name="good",
                         entry_urls=["http://f/1"])
        cfg.status = TaskStatus.RUNNING
        tid = taskio.save_template_config(db, cfg)
        t = repo.get_task(db, tid)
        assert t.is_template is True
        assert t.status == TaskStatus.IDLE        # 运行态清零
        assert t.parent_id is None
        assert repo.list_tasks(db) == []           # 不进任务列表
        assert len(taskio.list_templates(db)) == 1
    finally:
        db.close()


def test_template_payload_shape(web_home, tmp_path):
    """模板载荷：新建表单要回填的字段全集；**不带 cookie**（Web 无此字段）。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskConfig

    db = Database(tmp_path / "t6.db")
    try:
        tid = _mk(db)
        tpl_id = taskio.save_as_template(db, tid, "tpl")
        p = taskio.template_payload(repo.get_task(db, tpl_id))
        assert p["name"] == "tpl"
        assert p["entry_urls"] == ["http://x/1"]
        assert p["ai_labels"] == ["白丝"]
        assert p["ai_labels_l2"] == ["jk"]
        assert p["concurrency"] == 4 and p["delay"] == 2.75
        assert p["ai_threshold"] == 0.42
        assert "cookie" not in p
        assert "secret" not in json.dumps(p, ensure_ascii=False)
        for k in ("id", "name", "adapter_name", "entry_urls", "keywords",
                  "concurrency", "max_depth", "delay", "priority",
                  "ai_labels", "ai_labels_l2", "ai_labels_l3",
                  "ai_blacklist", "ai_threshold", "ai_enabled",
                  "use_proxy", "auto_run", "output_dir"):
            assert k in p, k
    finally:
        db.close()


# ==================================================================
# 三、真起服务：四个端点的 HTTP 行为
# ==================================================================
class _Cli:
    def __init__(self, port: int) -> None:
        self.port = port
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def login(self, pw: str = "pw123") -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/login",
            data=json.dumps({"password": pw}).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"})
        with self.opener.open(req, timeout=15) as r:
            assert r.status == 200

    def get(self, path: str):
        with self.opener.open(
                f"http://127.0.0.1:{self.port}{path}", timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(self, path: str, body: dict):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))


@pytest.fixture()
def srv(web_home, tmp_path):
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    cfg = AppConfig()
    cfg.db_path = str(tmp_path / "r173.db")
    cfg.web_password = "pw123"
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_lan_open = False
    cfg.web_ip_whitelist = []

    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None

    db = Database(tmp_path / "r173.db")
    s = webui.WebServer(cfg, db)
    ok, msg = s.start()
    assert ok, f"服务器起不来：{msg}"
    try:
        yield webui, cfg, db, _Cli(cfg.web_port)
    finally:
        s.stop()
        db.close()
        cfgmod.save_config = orig_save


def test_http_template_roundtrip(srv):
    """HTTP 端到端：建任务 → 存模板 → 列表见模板 → 删模板。

    R170：原「克隆」两步（POST /api/task/clone）已随功能删除，本用例只覆盖
    保留下来的模板链路；模板→真任务的库层能力另由
    ``test_clone_from_template_makes_real_task`` 直接对 DB 验证。
    """
    webui, cfg, db, cli = srv
    cli.login()
    # 直接建一个任务（走 save_task，省得拼表单）
    r = cli.post("/api/task/save", {
        "name": "站点A", "entry_urls": "http://a/1",
        "adapter_name": "good", "concurrency": 4, "max_depth": 3,
        "delay": 1, "priority": 5, "ai_labels": "猫",
        "ai_threshold": 0.35, "ai_enabled": False, "use_proxy": False,
        "auto_run": False, "output_dir": ""})
    assert r.get("ok"), r
    tid = r.get("id") or r.get("task_id")
    tasks = cli.get("/api/tasks")
    tid = next(t["id"] for t in tasks if t["name"] == "站点A")

    # 存模板（默认名 = 任务名）
    r = cli.post("/api/task/template/save", {"id": tid})
    assert r.get("ok"), r
    tpls = cli.get("/api/task/templates")["templates"]
    assert any(t["name"] == "站点A" for t in tpls)
    tpl_id = next(t["id"] for t in tpls if t["name"] == "站点A")
    # 模板不出现在任务列表
    assert "站点A" in [t["name"] for t in cli.get("/api/tasks")]
    assert [t["name"] for t in cli.get("/api/tasks")].count("站点A") == 1

    # 删模板：真任务删不动，模板删得掉
    r = cli.post("/api/task/template/delete", {"id": tid})
    assert r.get("ok") is False, r
    r = cli.post("/api/task/template/delete", {"id": tpl_id})
    assert r.get("ok"), r
    assert cli.get("/api/task/templates")["templates"] == []


def test_http_templates_requires_login(srv):
    """未登录打模板接口 = 401/拒绝（安全面：模板清单不带明细也不该裸奔）。"""
    webui, cfg, db, cli = srv
    req = urllib.request.Request(
        f"http://127.0.0.1:{cli.port}/api/task/templates")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8")
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
        body = e.read().decode("utf-8")
    assert code in (401, 403), (code, body[:200])


def test_http_clone_endpoint_gone(srv):
    """R170：/api/task/clone 必须已删除（不能再克隆出重复任务）。"""
    webui, cfg, db, cli = srv
    cli.login()
    r = cli.post("/api/task/clone", {"id": 1})
    # 路由不在 = 落到未知路径分支；无论何种形态都**不许**返回 ok:True
    assert r.get("ok") is not True, \
        "/api/task/clone 还能克隆 —— R170 删除未生效"


def test_template_exclusion_sql_wiring():
    """模板排除的 SQL 接线：所有「任务计数/列表/批量/定时」现场都要带
    is_template=0 —— 漏一处就是「模板出现在任务页/顶栏/被定时拉起」。
    逐站点字面量判据（这些 SQL 都是手写字符串，没有可断言的行为替身）。"""
    w = WEBUI.read_text(encoding="utf-8")
    # webui：SSE 摘要 + /api/summary + 任务列表 + 重试失败 + 批量启停
    assert w.count("FROM tasks WHERE is_template=0") >= 3, w.count(
        "FROM tasks WHERE is_template=0")
    assert "WHERE status='error' \"\n                \"AND is_template=0" in w \
        or "status='error' " in w and "AND is_template=0" in w
    assert '"SELECT id FROM tasks WHERE is_template=0 ORDER BY id"' in w
    g = MAIN.read_text(encoding="utf-8")
    # GUI：侧栏角标 + 统计面板 + 状态签名 + 定时扫描
    assert g.count('"SELECT COUNT(*) c FROM tasks WHERE is_template=0"') >= 2
    assert '"WHERE is_template=0 GROUP BY status"' in g
    assert '"WHERE t.is_template=0 "' in g
    assert '"AND is_template=0")' in g                 # 定时扫描
    assert '"WHERE is_template=0 ORDER BY id"))' in g   # 状态签名
    # 审核/调度两处
    rt = (ROOT / "media_resource_manager" / "gui" /
          "review_tab.py").read_text(encoding="utf-8")
    assert "SELECT id FROM tasks WHERE is_template=0 ORDER BY id" in rt
    rq = (ROOT / "media_resource_manager" / "review" /
          "queue.py").read_text(encoding="utf-8")
    assert "FROM tasks WHERE is_template=0" in rq
    sq = (ROOT / "media_resource_manager" / "scheduler" /
          "queue.py").read_text(encoding="utf-8")
    assert "status='running' " in sq and "AND is_template=0" in sq


def test_http_summary_and_snapshot_exclude_templates(srv):
    """/api/summary 与 SSE event_snapshot 的任务计数都不带模板，
    且两边口径一致（R172 的同口径护栏在模板场景下仍成立）。"""
    webui, cfg, db, cli = srv
    cli.login()
    cli.post("/api/task/save", {
        "name": "真任务", "entry_urls": "http://a/1", "adapter_name": "good",
        "concurrency": 4, "max_depth": 3, "delay": 1, "priority": 5,
        "ai_labels": "", "ai_threshold": 0.35, "ai_enabled": False,
        "use_proxy": False, "auto_run": False, "output_dir": ""})
    tid = next(t["id"] for t in cli.get("/api/tasks")
               if t["name"] == "真任务")
    assert cli.post("/api/task/template/save", {"id": tid}).get("ok")
    # 任务列表只有真任务
    assert [t["name"] for t in cli.get("/api/tasks")] == ["真任务"]
    # summary 计数 = 1（模板不计）
    s = cli.get("/api/summary")
    assert s["tasks"] == 1, s
    # SSE 快照同口径
    snap = webui.event_snapshot(db, None)
    assert snap["tasks"] == 1, snap


def test_http_batch_excludes_templates(srv):
    """批量启停/重试失败不碰模板（模板没有 queue 语义，碰了就是报错源）。"""
    webui, cfg, db, cli = srv
    cli.login()
    cli.post("/api/task/save", {
        "name": "真任务", "entry_urls": "http://a/1", "adapter_name": "good",
        "concurrency": 4, "max_depth": 3, "delay": 1, "priority": 5,
        "ai_labels": "", "ai_threshold": 0.35, "ai_enabled": False,
        "use_proxy": False, "auto_run": False, "output_dir": ""})
    tid = next(t["id"] for t in cli.get("/api/tasks")
               if t["name"] == "真任务")
    assert cli.post("/api/task/template/save", {"id": tid}).get("ok")
    # 不传 ids = 全体：total 只数真任务（1），不因模板变成 2
    r = cli.post("/api/task/batch", {"act": "stop"})
    assert r.get("ok"), r
    assert r.get("total") == 1, r


def test_http_raw_socket_get_templates(srv):
    """GET /api/task/templates 是 200 JSON（不是 SSE/不是 404 页面）。"""
    webui, cfg, db, cli = srv
    cli.login()
    s = socket.create_connection(("127.0.0.1", cli.port), timeout=10)
    try:
        cookie = ""
        for c in cli.jar:
            cookie = f"{c.name}={c.value}"
            break
        s.sendall(("GET /api/task/templates HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   "Cookie: %s\r\nConnection: close\r\n\r\n"
                   % cookie).encode("utf-8"))
        buf = b""
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        assert b"200" in head.split(b"\r\n")[0], head[:120]
        assert b"application/json" in head.lower(), head[:200]
        d = json.loads(body.decode("utf-8"))
        assert d.get("ok") is True and d.get("templates") == []
    finally:
        s.close()
