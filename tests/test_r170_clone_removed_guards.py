"""R170 护栏：移除「克隆任务」（2026-10-20 11:40 用户拍板）。

用户担忧原话（订单背景）：克隆后忘改配置 → 两个相同任务同时跑 → 重复采集。
拍板：**克隆功能直接删除**；「存为模板 / 用模板新建」暂保留。

本单最容易犯的错：把 `taskio.clone_task` 一起删掉 —— 它是「用模板新建」的
底层实现，删了会把模板功能打死。所以护栏**同时钉两个方向**：
  · 克隆的**用户入口**（桌面菜单 / 方法 / Web 按钮 / API 路由）必须消失；
  · 模板能力与库层 `clone_task` 必须还在。
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
WEBUI = ROOT / "media_resource_manager" / "webui.py"


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_js_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", s)


def _strip_py_comments(s: str) -> str:
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in s.splitlines())


# ---------------- ① 桌面端：入口消失 --------------------------------


def test_desktop_clone_menu_and_method_gone():
    """桌面右键菜单「克隆任务」与 _clone_task 方法必须都已移除。"""
    src = _src(MAIN)
    code = _strip_py_comments(src)      # 注释里提「已移除」是对的，不算违规
    assert 'menu.addAction("克隆任务"' not in code, \
        "右键菜单「克隆任务」还在 —— R170 已删除（用户拍板）"
    assert "def _clone_task(" not in code, \
        "_clone_task 方法还在 —— R170 已删除"
    assert "_clone_task(" not in code, \
        "还有地方在调 _clone_task —— R170 已删除"


def test_desktop_other_menu_items_kept():
    """其余菜单项一个不少（删除要精准，别顺手砍了别的）。"""
    src = _src(MAIN)
    for lit in ('menu.addAction("存为模板…"',
                'menu.addAction("失败网址清单…"',
                "self.act_run", "self.act_stop", "self.act_edit",
                "self.act_del", "self.act_refresh"):
        assert lit in src, f"菜单项 {lit} 不见了 —— 删克隆时误伤"


def test_desktop_cloned_task_data_not_touched():
    """用户已有的「（副本）」任务**不许**被自动删除（那是用户数据）。

    只检查源码里没有"删副本任务"的批量清理逻辑。
    """
    src = _strip_py_comments(_src(MAIN))
    bad = re.search(r"（副本）[^\n]{0,40}(DELETE|删除)|"
                    r"(DELETE|删除)[^\n]{0,40}（副本）", src)
    assert bad is None, \
        f"源码里出现自动删「（副本）」任务的逻辑 —— 用户数据不可动：{bad}"


# ---------------- ② 网页端：按钮与接口消失 --------------------------

def test_backend_clone_route_and_handler_gone():
    """`/api/task/clone` 路由与 `_api_task_clone` 处理器必须移除（不留死代码）。"""
    code = _strip_py_comments(_src(WEBUI))
    assert 'path == "/api/task/clone"' not in code, \
        "/api/task/clone 路由还在 —— R170 已删除"
    assert "def _api_task_clone(" not in code, \
        "_api_task_clone 处理器还在 —— R170 已删除"


def test_backend_clone_route_really_404s():
    """行为级：真起服务打 /api/task/clone → 不许返回 ok:True。"""
    import os
    import socket
    import json
    import tempfile
    import urllib.error
    import urllib.request
    from http.cookiejar import CookieJar

    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    if "media_resource_manager" not in __import__("sys").path:
        pass

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r170_"))
    old = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    os.environ["MRM_HOME"] = str(tmp)
    os.environ["MRM_NO_MIGRATE"] = "1"

    def _free_port():
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        return p

    cfg = AppConfig()
    cfg.web_port = _free_port()
    cfg.web_password = "pw"
    cfg.web_lan_open = False
    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None
    db = Database(tmp / "r170.db")
    srv = webui.WebServer(cfg, db)
    ok, msg = srv.start()
    assert ok, msg
    try:
        jar = CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        r = opener.open(urllib.request.Request(
            f"http://127.0.0.1:{cfg.web_port}/api/login",
            data=json.dumps({"password": "pw"}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10)
        r.read()
        req = urllib.request.Request(
            f"http://127.0.0.1:{cfg.web_port}/api/task/clone",
            data=json.dumps({"id": 1}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
        assert '"ok": true' not in body.replace('"ok":true', '"ok": true'), \
            f"/api/task/clone 还能克隆 —— R170 删除未生效：{body[:200]}"
    finally:
        srv.stop()
        db.close()
        cfgmod.save_config = orig_save
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------- ④ 模板能力必须保留 --------------------------------

def test_library_clone_task_kept_for_templates():
    """库层 clone_task 必须保留 —— 它是「用模板新建」的实现，删了模板一起死。

    行为级：真用它从模板建一个真任务。
    """
    import tempfile
    from media_resource_manager.db import Database
    from media_resource_manager import taskio, repo
    from media_resource_manager.models import TaskConfig

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r170lib_"))
    db = Database(tmp / "t.db")
    try:
        tid = repo.save_task(db, TaskConfig(
            id=None, name="站点X", adapter_name="good",
            entry_urls=["http://x/1"], keywords=["猫"]))
        tpl = taskio.save_as_template(db, tid, "我的模板")
        assert tpl is not None
        new_id = taskio.clone_task(db, tpl)          # 模板 → 真任务
        t = repo.get_task(db, new_id)
        assert t.is_template is False, "从模板建出来的必须是真任务"
        assert t.name.startswith("我的模板")
        assert t.id != tpl and t.id != tid
    finally:
        db.close()

def test_dup_url_guard_is_lightweight_no_switch():
    """订单要求「轻量防呆，不加开关」——不许引入新的配置开关。"""
    m = _src(MAIN)
    k = m.index("def _warn_dup_url_on_save(")
    blk = m[k:k + 3000]
    assert "self.cfg." not in blk, "防呆里读了 cfg 开关（订单要求不加开关）"
