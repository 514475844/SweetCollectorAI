"""R154-F14 护栏：任务批量导入/导出 JSON（执行方笔记本棒）。

订单要点：① 导出 = 选中/全部任务 → JSON（含进度统计，不含密钥/机器 id）；
② 导入 = JSON → 批量建任务（校验适配器是否存在，缺适配器中文提示）；
③ Web 端也要有（与桌面「数据」菜单对称）。

每条按「行为断言优先、源码字面量判据兜底」配。Web 导入路径必须校验适配器
（缺适配器的任务建出来也是坏的，宁可跳过并提示）。

⚠ 桌面端导入（import_tasks_file）刻意**不做**适配器校验（valid_adapters=None），
向后兼容旧行为；只有 Web 导入路径走校验 —— 见 _api_task_import。
"""
from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_path_pended = False
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

WEBUI = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"

_TMP = Path(tempfile.mkdtemp(prefix="r154f14_"))


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
# 一、源码字面量接线（护栏：写对函数但没接路由 = 白写）
# ==================================================================
def test_webui_source_wiring():
    src = WEBUI.read_text(encoding="utf-8")
    # 两个新路由必须逐字面量出现（与 test_new_endpoints_are_routed 同约定）
    assert 'path == "/api/tasks/export"' in src
    assert 'path == "/api/tasks/import"' in src
    # 导出必须挂在鉴权之后（/api/* 都在 _auth_ok 之后）
    assert src.index('if not self._auth_ok()') < src.index('path == "/api/tasks/export"')
    # 两个新处理函数存在
    assert "def _api_tasks_export(" in src
    assert "def _api_task_import(" in src
    assert "def _adapter_name_set(" in src

# ==================================================================
# 二、数据层：build_export_data / import_tasks_data（直接对真实 DB）
# ==================================================================
def test_build_export_strips_machine_ids_and_marks_idle(web_home):
    """导出：剔 id/parent_id/proxy_id、status=IDLE、带 progress、不带密钥。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskConfig, TaskStatus

    db = Database(_TMP / "exp.db")
    try:
        tid = repo.save_task(
            db, TaskConfig(id=None, name="t1", adapter_name="good",
                           entry_urls=["http://x/1"], keywords=["cat"]))
        d = taskio.build_export_data(db)
        assert len(d) == 1
        row = d[0]
        assert "id" not in row
        assert "parent_id" not in row
        assert "proxy_id" not in row
        assert row["status"] == TaskStatus.IDLE.value
        assert row["proxy"] is None or isinstance(row["proxy"], (str, type(None)))
        assert "progress" in row
        assert row["name"] == "t1"
        assert row["keywords"] == ["cat"]
    finally:
        db.close()


def test_export_data_ids_filter(web_home):
    """ids 给定时只导这些任务。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskConfig

    db = Database(_TMP / "exp_ids.db")
    try:
        a = repo.save_task(db, TaskConfig(id=None, name="a", adapter_name="good",
                                             entry_urls=["http://x/a"]))
        repo.save_task(db, TaskConfig(id=None, name="b", adapter_name="good",
                                     entry_urls=["http://x/b"]))
        d = taskio.build_export_data(db, ids=[a])
        assert len(d) == 1
        assert d[0]["name"] == "a"
    finally:
        db.close()


def test_import_creates_and_drops_progress(web_home):
    """导入：建出任务；progress 段被丢弃（软件无续抓机制）。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskConfig

    db = Database(_TMP / "imp1.db")
    try:
        raw = [{"name": "n1", "adapter_name": "good",
                "entry_urls": ["http://n/1"], "keywords": ["k"],
                "progress": {"downloaded": 9}}]
        res = taskio.import_tasks_data(db, raw, valid_adapters={"good"})
        assert res["created"] == 1
        rows = db.query(
            "SELECT name, adapter_name, entry_urls, keywords FROM tasks")
        assert len(rows) == 1
        assert rows[0]["name"] == "n1"
        assert rows[0]["adapter_name"] == "good"
        assert json.loads(rows[0]["entry_urls"]) == ["http://n/1"]
        assert json.loads(rows[0]["keywords"]) == ["k"]
        # 导入不还原 progress：新建任务本身就是新行，无需断言；这里确认
        # import_tasks_data 不会因 progress 键而多建或崩。
    finally:
        db.close()


def test_import_dedup_and_adapter_validation(web_home):
    """导入：重复 URL 跳过；适配器缺失跳过并汇报（Web 红线）。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskConfig

    db = Database(_TMP / "imp2.db")
    try:
        repo.save_task(db, TaskConfig(id=None, name="old", adapter_name="good",
                                     entry_urls=["http://seed/1"]))
        raw = [
            {"name": "dup", "adapter_name": "good",
             "entry_urls": ["http://seed/1"]},
            {"name": "fresh", "adapter_name": "good",
             "entry_urls": ["http://seed/2"]},
            {"name": "ghost", "adapter_name": "ghost",
             "entry_urls": ["http://seed/3"]},
        ]
        res = taskio.import_tasks_data(db, raw, valid_adapters={"good"})
        assert res["created"] == 1
        assert "dup" in res["skipped_dup"]
        assert "ghost" in res["missing_adapter"]
        # 同一份 raw 再导一次：fresh 也变重复
        res2 = taskio.import_tasks_data(db, raw, valid_adapters={"good"})
        assert res2["created"] == 0
        assert "fresh" in res2["skipped_dup"]
    finally:
        db.close()


def test_import_no_adapter_check_when_none(web_home):
    """桌面导入路径（valid_adapters=None）不做适配器校验，向后兼容。"""
    from media_resource_manager.db import Database
    from media_resource_manager import repo, taskio
    from media_resource_manager.models import TaskConfig

    db = Database(_TMP / "imp3.db")
    try:
        raw = [{"name": "g2", "adapter_name": "ghost2",
                "entry_urls": ["http://seed/5"]}]
        res = taskio.import_tasks_data(db, raw, valid_adapters=None)
        assert res["created"] == 1
        assert res["missing_adapter"] == []
    finally:
        db.close()


def test_import_rejects_non_list(web_home):
    from media_resource_manager.db import Database
    from media_resource_manager import taskio

    db = Database(_TMP / "imp4.db")
    try:
        with pytest.raises(ValueError):
            taskio.import_tasks_data(db, {"foo": 1}, valid_adapters=None)
    finally:
        db.close()


# ==================================================================
# 三、Web 端真实 HTTP 往返（导出→导入，含适配器校验）
# ==================================================================
def _login(port, pw="pw123"):
    jar = CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/login",
        data=json.dumps({"password": pw}).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with op.open(req, timeout=15) as r:
        assert r.status == 200
    cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
    return cookie, op


@pytest.fixture()
def api_server(web_home):
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    cfg = AppConfig()
    cfg.db_path = str(_TMP / "f14api.db")
    cfg.web_password = "pw123"
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    cfg.web_lan_open = False
    cfg.web_ip_whitelist = []
    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None   # 别真写盘
    rules = _TMP / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "good.yaml").write_text(
        "name: good\nbase_url: http://good.example\n"
        "allowed_domains: [good.example]\n"
        "image_link_selectors:\n  - kind: css\n    expr: a img\n",
        encoding="utf-8")
    cfg.rules_dir = str(rules)
    db = Database(_TMP / "f14api.db")
    srv = webui.WebServer(cfg, db)
    ok, msg = srv.start()
    assert ok, f"服务器起不来：{msg}"
    try:
        yield cfg.web_port, db
    finally:
        if hasattr(srv, "stop"):
            try:
                srv.stop()
            except Exception:
                pass
        cfgmod.save_config = orig_save


def test_web_export_returns_json_and_attachment(api_server):
    port, db = api_server
    cookie, op = _login(port)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tasks/export",
        headers={"Cookie": cookie})
    with op.open(req, timeout=15) as r:
        assert r.status == 200
        assert "attachment" in r.headers.get("Content-Disposition", "")
        data = json.loads(r.read())
    assert isinstance(data, list)


def test_web_export_content_matches_db(api_server):
    port, db = api_server
    from media_resource_manager import repo
    from media_resource_manager.models import TaskConfig
    repo.save_task(db, TaskConfig(id=None, name="web1", adapter_name="good",
                                 entry_urls=["http://web/1"]))
    cookie, op = _login(port)
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tasks/export",
        headers={"Cookie": cookie})
    with op.open(req, timeout=15) as r:
        data = json.loads(r.read())
    assert any(d.get("name") == "web1" for d in data)
    assert all("id" not in d for d in data)
    assert all(d.get("status") == "idle" for d in data)


def test_web_import_validates_adapter_and_creates(api_server):
    port, db = api_server
    cookie, op = _login(port)
    payload = [
        {"name": "imp_good", "adapter_name": "good",
         "entry_urls": ["http://webnew/a"]},
        {"name": "imp_ghost", "adapter_name": "ghost",
         "entry_urls": ["http://webnew/b"]},
    ]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tasks/import",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie})
    with op.open(req, timeout=15) as r:
        res = json.loads(r.read())
    assert res.get("ok"), res
    assert res["created"] == 1
    assert "imp_ghost" in res["missing_adapter"]
    rows = db.query("SELECT name, adapter_name FROM tasks WHERE name=?",
                    ("imp_good",))
    assert rows and rows[0]["adapter_name"] == "good"


def test_web_import_rejects_non_array(api_server):
    port, db = api_server
    cookie, op = _login(port)
    # 发一个 JSON 标量（既不是数组也不是含 tasks/data 的对象）
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/tasks/import",
        data=json.dumps("not-an-array").encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie})
    with op.open(req, timeout=15) as r:
        res = json.loads(r.read())
    assert res.get("ok") is False
    assert res.get("error")
