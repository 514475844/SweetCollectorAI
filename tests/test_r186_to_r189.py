"""R186-R189 护栏。

作业单：`.workbuddy/R186_COMPLIANCE_REMOVE.md`（合规层 off 档 + 删 UI）、
`.workbuddy/R188_ZERO_IMAGE_NOT_DONE.md`（0 图必判失败不判完成）、
`.workbuddy/R189_BROWSER_UA_COOKIE.md`（gallery-dl 快路径带浏览器 UA/Cookie）、
`.workbuddy/R187_WEB_STARTUP_RACE.md`（Web 启动竞态：bootSession 退避重试）。

判据风格（本仓铁律）：
* 行为断言优先（真跑队列 / 真跑 node），文本断言只做辅助且**先剥注释**；
* AST 判据锚**代码结构**（函数名 / 关键字参数），docstring 与注释骗不到；
* 断言「关系」而不是「数值」。
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SRC = ROOT / "media_resource_manager"
APPJS = SRC / "web" / "static" / "js" / "app.js"
MAINWIN = SRC / "gui" / "main_window.py"


# ══════════════════════════════════════════════════ R188：0 图必判失败
def _mk_queue(tmpdir):
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager.scheduler.queue import TaskQueue

    cfg = AppConfig()
    cfg.db_path = os.path.join(str(tmpdir), "t.db")
    Database(cfg.db_path).close()
    return TaskQueue(cfg)


def _mk_task(q, name="任务"):
    from media_resource_manager.db import Database
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    t = TaskConfig(id=None, name=name, adapter_name="generic-gallery",
                   entry_urls=["https://e.com/"], max_depth=1)
    db = Database(q.cfg.db_path)
    try:
        return save_task(db, t)
    finally:
        db.close()


class _StubEngine:
    def __init__(self, report):
        self._report = report

    async def run(self, task_id):
        from media_resource_manager.engine import RunReport
        rep = self._report
        if rep is None:
            rep = RunReport(task_id=task_id)
        else:
            rep.task_id = task_id
        return rep

    def shutdown(self):
        pass


def _drive(q, monkeypatch, report):
    import media_resource_manager.scheduler.queue as qmod
    monkeypatch.setattr(qmod, "Engine", lambda cfg, db: _StubEngine(report))
    tid = _mk_task(q)
    asyncio.run(q._run_one(tid))
    return tid


def _task_status(q, tid):
    from media_resource_manager.db import Database
    from media_resource_manager.repo import get_task
    db = Database(q.cfg.db_path)
    try:
        return get_task(db, tid)
    finally:
        db.close()


def test_r188_zero_images_clean_exit_is_error(tmp_path, monkeypatch):
    """【R188 核心】gallery-dl 被站点拦截（403/JS 挑战）时干净退出：
    found=0 / downloaded=0 / errors=[] —— 旧逻辑判 DONE（用户看到
    "任务直接完成了"），现在必须判 ERROR 并登记进 failed 让重试可用。"""
    from media_resource_manager.engine import RunReport
    from media_resource_manager.models import TaskStatus
    q = _mk_queue(tmp_path)
    rep = RunReport(task_id=0, images_found=0, failed=0, downloaded=0,
                    errors=[])
    tid = _drive(q, monkeypatch, rep)
    t = _task_status(q, tid)
    assert t.status is TaskStatus.ERROR, (
        f"0 图干净退出必须判 error，实际 {t.status}（R188：不再假绿）")
    assert tid in q.failed, "0 图任务没进 self.failed —— 重试菜单摸不到它"
    note = q.failed.get(tid) or ""
    assert "未获取到任何结果" in note, f"状态说明应写明没下到图，实际 {note!r}"


def test_r188_zero_images_with_pages_also_error(tmp_path, monkeypatch):
    """抓到页面但 0 图（选择器未命中 / 拦截页）同样判失败。"""
    from media_resource_manager.engine import RunReport
    from media_resource_manager.models import TaskStatus
    q = _mk_queue(tmp_path)
    rep = RunReport(task_id=0, images_found=0, failed=0, downloaded=0,
                    errors=[], pages=2)
    tid = _drive(q, monkeypatch, rep)
    t = _task_status(q, tid)
    assert t.status is TaskStatus.ERROR, (
        f"抓到页面但 0 图也必须判 error，实际 {t.status}")


def test_r188_downloaded_still_done(tmp_path, monkeypatch):
    """正常下到图的站点不受影响：downloaded>0 仍判 DONE（回归保护）。"""
    from media_resource_manager.engine import RunReport
    from media_resource_manager.models import TaskStatus
    q = _mk_queue(tmp_path)
    rep = RunReport(task_id=0, images_found=15, failed=0, downloaded=15,
                    errors=[])
    tid = _drive(q, monkeypatch, rep)
    t = _task_status(q, tid)
    assert t.status is TaskStatus.DONE, (
        f"下到 15 张应判 done，实际 {t.status}（R188 不该误伤正常任务）")
    assert tid not in q.failed


# ══════════════════════════════════════════════════ R186：合规 off 档
def test_r186_off_level_in_config():
    from media_resource_manager.config import COMPLIANCE_LEVELS
    assert "off" in COMPLIANCE_LEVELS, "R186：config 必须有 off 档"
    # off 档预填排除词为空（default_exclusions 对未知档兜底 []）
    from media_resource_manager.config import default_exclusions
    assert default_exclusions("off") == []


def test_r186_off_level_skips_gate(tmp_config, tmp_db, fake_fetcher):
    """off 档：没有授权记录、没有 basis，任务照跑、compliance 表零写入。"""
    PAGES = {
        "https://site.com/": '<a class="album" href="/a1">A1</a>',
        "https://site.com/a1": ('<a class="photo" href="/a1/p1">p1</a>'),
        "https://site.com/a1/p1": '<img src="/img/1.jpg">',
    }
    rule = {
        "name": "site", "base_url": "https://site.com",
        "allowed_domains": ["site.com"],
        "list_page_selectors": {"albums": {"kind": "css", "expr": "a.album",
                                           "attr": "href"}},
        "album_link_selectors": {"photos": {"kind": "css",
                                            "expr": "a.photo", "attr": "href"}},
        "image_link_selectors": [{"kind": "css", "expr": "img", "attr": "src"}],
        "max_depth": 3,
    }
    (tmp_config.rules_dir / "site.json").write_text(json.dumps(rule),
                                                    encoding="utf-8")
    tmp_config.compliance_level = "off"
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    t = TaskConfig(id=None, name="t", adapter_name="site",
                   entry_urls=["https://site.com/"],
                   output_dir=str(tmp_config.library_dir))
    tid = save_task(tmp_db, t)

    def factory(cfg, proxy):
        return fake_fetcher(PAGES)

    eng = Engine(tmp_config, tmp_db, fetcher_factory=factory)
    rep = asyncio.run(eng.run(tid))
    gate_errs = [e for e in rep.errors
                 if "compliance" in str(e) or "授权" in str(e)]
    assert not gate_errs, f"off 档不应有合规报错，实际 {gate_errs}"
    n = tmp_db.query("SELECT COUNT(*) c FROM compliance")[0]["c"]
    assert int(n or 0) == 0, f"off 档跑完 compliance 表必须仍是 0 行，实际 {n}"


def test_r186_standard_no_longer_blocks(tmp_config, tmp_db, fake_fetcher):
    """2026-10-26：合规门禁整层删除——standard 档也不再拦，任务照跑。
    旧断言"standard 仍拦"随门禁一起退役（守卫测试
    test_compliance_level.py 钉死门禁不存在）。"""
    tmp_config.compliance_level = "standard"
    rule = {
        "name": "site2", "base_url": "https://site.com",
        "allowed_domains": ["site.com"],
        "list_page_selectors": {"albums": {"kind": "css", "expr": "a.album",
                                           "attr": "href"}},
        "image_link_selectors": [{"kind": "css", "expr": "img", "attr": "src"}],
        "max_depth": 3,
    }
    (tmp_config.rules_dir / "site2.json").write_text(json.dumps(rule),
                                                     encoding="utf-8")
    from media_resource_manager.engine import Engine
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    t = TaskConfig(id=None, name="t", adapter_name="site2",
                   entry_urls=["https://site.com/"],
                   output_dir=str(tmp_config.library_dir))
    tid = save_task(tmp_db, t)

    def factory(cfg, proxy):
        return fake_fetcher({})

    eng = Engine(tmp_config, tmp_db, fetcher_factory=factory)
    rep = asyncio.run(eng.run(tid))
    assert not any("compliance" in str(e) for e in rep.errors), rep.errors


def _strip_py_comments(src: str) -> str:
    """剥掉 Python 源里的注释行与行尾注释（保 docstring：它是字符串字面量，
    不影响 AST 判据；这里只为让人工读输出时干净）。"""
    return "\n".join(re.sub(r"#(?![!']).*$", "", ln) for ln in src.splitlines())


def test_r186_gui_compliance_ui_removed():
    """AST 判据：main_window 里不得再有授权菜单/授权流程/合规模板切换的
    **代码结构**（docstring/注释里提到这些词不算）。"""
    tree = ast.parse(MAINWIN.read_text(encoding="utf-8"))

    func_names: set[str] = set()
    self_attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_names.add(node.name)
        if isinstance(node, ast.Attribute) and \
                isinstance(node.value, ast.Name) and node.value.id == "self":
            self_attrs.add(node.attr)

    banned_funcs = {"toggle_auto_authorize", "set_compliance_level",
                    "auth_manager", "authorize_selected", "_authorize_flow",
                    "_do_authorize"}
    leaked = banned_funcs & func_names
    assert not leaked, f"R186：这些合规流程函数必须删除，仍在源码结构里: {leaked}"

    banned_attrs = {"act_autoauth", "act_authorize", "act_authmgr",
                    "_comp_actions"}
    leaked_attrs = banned_attrs & self_attrs
    assert not leaked_attrs, f"R186：授权菜单 QAction 仍被赋值: {leaked_attrs}"


def test_r186_engine_no_auto_insert_row():
    """AST 判据：engine.py 里不得再有「INSERT INTO compliance」字符串字面量
    （auto_authorize 自动插行的唯一写入路径已删）。"""
    tree = ast.parse((SRC / "engine.py").read_text(encoding="utf-8"))
    hits = [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "INSERT INTO compliance" in n.value]
    assert not hits, f"R186：engine 仍在自动插授权行: {hits}"


# ══════════════════════════════════════════════════ R189：UA + Cookie
def test_r189_default_ua_is_real_browser():
    from media_resource_manager.config import AppConfig
    ua = AppConfig().user_agent
    assert ua.startswith("Mozilla/5.0"), (
        f"默认 UA 必须是真实浏览器 UA（站点按 UA 挡机器人），实际 {ua!r}")
    assert "MediaResourceManager" not in ua, (
        "默认 UA 不得再带机器人标识")


def test_r189_cookie_browser_default():
    from media_resource_manager.config import AppConfig
    assert AppConfig().cookie_browser == "chrome"


def test_r189_backend_config_carries_ua_and_cookies(tmp_path):
    """_make_config 把 UA 放**全局**段、cookie-browser 放 **extractor** 段
    （gallery-dl 的选项层级如此），且空值不写键。"""
    from media_resource_manager.backends.gallery_dl import GalleryDLBackend
    be = GalleryDLBackend(dest_root=tmp_path, user_agent="UA-X",
                          cookie_browser="firefox")
    cfg = be._make_config()
    assert cfg["user-agent"] == "UA-X"
    assert cfg["extractor"]["cookies-from-browser"] == "firefox"
    # 关系断言：UA 不该被塞进 extractor 段（gallery-dl extractor 段不认它）
    assert "user-agent" not in cfg["extractor"]

    be2 = GalleryDLBackend(dest_root=tmp_path, user_agent="", cookie_browser="")
    cfg2 = be2._make_config()
    assert "user-agent" not in cfg2
    assert "cookies-from-browser" not in cfg2["extractor"]


def test_r189_engine_wires_ua_and_cookie():
    """AST 判据（防 keyword 整体缺失）：engine._run_via_gallery_dl 构造
    GalleryDLBackend 时必须显式带 user_agent / cookie_browser 关键字。"""
    tree = ast.parse((SRC / "engine.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_run_via_gallery_dl")
    kw_names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and \
                isinstance(node.func, ast.Name) and \
                node.func.id == "GalleryDLBackend":
            kw_names |= {k.arg for k in node.keywords}
    assert {"user_agent", "cookie_browser"} <= kw_names, (
        f"GalleryDLBackend(...) 缺关键字参数，现有: {sorted(kw_names)}")


def test_r189_engine_passes_cfg_values_to_backend(tmp_config, tmp_db,
                                                  monkeypatch):
    """行为级判据（防「keyword 在、值没接上」——变异测试抓到的空壳）：
    真跑 _run_via_gallery_dl，断言 GalleryDLBackend 收到的就是 cfg 里的
    user_agent / cookie_browser 值。"""
    import media_resource_manager.backends.gallery_dl as gmod
    from media_resource_manager.engine import Engine, RunReport
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task

    tmp_config.user_agent = "UA-FROM-CFG"
    tmp_config.cookie_browser = "firefox"
    captured: dict = {}

    class FakeBE:
        def __init__(self, *a, **kw):
            captured.update(kw)

        def download_iter(self, url, *a, **k):
            return iter(())

        def stop(self):
            pass

    monkeypatch.setattr(gmod, "GalleryDLBackend", FakeBE)
    t = TaskConfig(id=None, name="t", adapter_name="generic-gallery",
                   entry_urls=["https://e.com/a1"], delay=0,
                   output_dir=str(tmp_config.library_dir))
    tid = save_task(tmp_db, t)
    task = __import__("media_resource_manager.repo", fromlist=["get_task"]) \
        .get_task(tmp_db, tid)
    eng = Engine(tmp_config, tmp_db,
                 fetcher_factory=lambda cfg, proxy: None)
    rep = RunReport(task_id=tid)
    asyncio.run(eng._run_via_gallery_dl(task, None,
                                        ["https://e.com/a1"], rep))
    assert captured.get("user_agent") == "UA-FROM-CFG", (
        f"engine 没把 cfg.user_agent 传进 backend: {captured}")
    assert captured.get("cookie_browser") == "firefox", (
        f"engine 没把 cfg.cookie_browser 传进 backend: {captured}")


# ══════════════════════════════════════════════════ R187：bootSession 重试
def _extract_boot_session_js() -> str:
    """抽 app.js 里 bootSession IIFE 的函数体并**剥掉注释**（R104 铁律：
    注释里会复述旧实现，字面量判据必须先剥注释再匹配）。"""
    src = APPJS.read_text(encoding="utf-8")
    m = re.search(r"\(function bootSession\(\) \{", src)
    assert m, "app.js 找不到 bootSession IIFE"
    i = m.end()
    j = src.index("\n})();", i)
    body = src[i:j]
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
    return body


def _node_boot_probe(fetch_mode: str) -> dict:
    """node 真跑 bootSession：stub fetch 按场景返回，setTimeout 立即同步
    触发（不等真实 10s），统计 showLogin / 顶部提示 / 重试次数。"""
    probe = """
var __calls = {login: 0, note: 0, timers: 0, session: 0, delays: []};
function showLogin(msg) { __calls.login++; }
function showApp() {}
function boot() {}
var state = {};
function enterAppFromSession(s) { __calls.session++; }
function api(url, opt) {
  if (FETCH_MODE === 'ok-nopw') { return Promise.resolve(); }
  return Promise.reject(new Error('net'));
}
var document = {
  createElement: function () {
    return {style: {}, textContent: '', parentNode: null};
  },
  body: {appendChild: function () { __calls.note++; },
         removeChild: function () {}}
};
setTimeout = function (cb, ms) {
  __calls.timers++; __calls.delays.push(ms); cb();
};
var FETCH_MODE = %s;
var fetch = function () {
  if (FETCH_MODE === 'reject') {
    return Promise.reject(new TypeError('Failed to fetch'));
  }
  if (FETCH_MODE === 'ok-nopw') {
    return Promise.resolve({ok: true, json: function () {
      return Promise.resolve({password_required: false});}});
  }
  return Promise.resolve({ok: false, json: function () {
    return Promise.resolve({password_required: true});}});
};
%s
/* bootSession 全链路是 Promise 微任务 + stub setTimeout 回调 —— 必须等
   事件循环排空（beforeExit）再报计数，否则 stringify 时链还没跑完。 */
process.on('beforeExit', function () {
  console.log(JSON.stringify(__calls));
});
""" % (json.dumps(fetch_mode), _extract_boot_session_js())
    p = subprocess.run(
        ["node", "-e", probe], capture_output=True, text=True,
        timeout=60, encoding="utf-8", errors="replace",
        cwd=str(ROOT))
    assert p.returncode == 0, f"node 跑 bootSession 失败：{p.stderr}"
    return json.loads((p.stdout or "").strip().splitlines()[-1])
