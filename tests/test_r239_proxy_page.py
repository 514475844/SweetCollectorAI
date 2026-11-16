"""r239 护栏：网页端代理管理页（列表 / 增删改 / 停用 / 测速）。

为什么值得单独锁：
1. **打码回填**是最容易犯的错 —— 列表里地址是 `user:***@host`，顺手塞进编辑框，
   用户一点保存密码就真变成三个星号，而且**当场看不出来**（列表还是显示 ***），
   要等下一次任务全 407 才发现。
2. **密码留空 = 不改**：编辑时不能把空密码写回去。
3. 测速是同步阻塞调用，**批量必须有上限**，否则用户以为网页卡死。
4. `_zh_error` 只匹配 str(ex) 不匹配类名时，httpx 的 ConnectError 会落到兜底文案
   「代理不可用（ConnectError）」，等于没告诉用户任何信息。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager/webapp/src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 页面与路由

def test_proxies_page_exists_and_is_wired():
    assert (SRC / "views/Proxies.vue").exists(), "代理管理页不存在"
    main = _read("main.js")
    assert "views/Proxies.vue" in main
    assert "path: '/proxies'" in main
    app = _read("App.vue")
    assert "label: '代理管理'" in app
    assert "path: '/proxies'" in app


def test_proxies_page_uses_real_endpoints():
    api = _read("api.js")
    assert "proxyCheck:" in api and "/api/proxy/check" in api
    t = _read("views/Proxies.vue")
    assert "api.proxies()" in t
    assert "api.proxySave(" in t and "api.proxyDelete(" in t and "api.proxyCheck(" in t


def test_edit_form_never_prefills_masked_url():
    """编辑回填**逐字段**（类型/主机/端口/账号），绝不直接用 p.url（含 ***）。

    2026-10-29（用户第 7 条）：表单拆成「类型单选 + 主机 + 端口 + 账号 + 密码」，
    所以回填也不再拼一个 url 字符串 —— 但仍必须避开 p.url。
    """
    t = _read("views/Proxies.vue")
    seg = t.split("function openEdit", 1)[1].split("async function save", 1)[0]
    assert "p.url" not in seg.replace("p.url 是", ""), \
        "openEdit 里用了 p.url —— 那是打码地址，保存会把密码写成 ***"
    for k in ("p.scheme", "p.host", "p.port"):
        assert k in seg, f"编辑回填缺 {k}"


def test_password_blank_means_keep():
    """留空密码 = 不修改；填了才拼进 userinfo。

    2026-10-29 改法：不再用 withPassword 去改字符串，而是在拼 url 时决定
    要不要带 `user:pass@` —— 账号留空则整段 userinfo 都不带（后端 urlsplit
    拿不到 username 就保留原值），账号在、密码空则只带 `user@`。
    """
    t = _read("views/Proxies.vue")
    assert "form.password ? `${user}:${form.password}@` : `${user}@`" in t
    # 旧实现已删除：字符串补丁式改密码太容易把 *** 写回去
    assert "function withPassword" not in t


def test_check_buttons_show_progress():
    """测速是阻塞的：按钮上必须有进行中的文案，否则用户会连点到卡死。"""
    t = _read("views/Proxies.vue")
    assert "正在测速…" in t and "测试中…" in t
    assert "anim=\"busy === 'check' ? 'spin'" in t or "anim=\"busy === 'check:'" in t


# ---------------------------------------------------------------- 后端

def test_proxy_check_route_registered():
    from media_resource_manager import webui
    src = Path(webui.__file__).read_text(encoding="utf-8")
    assert '"/api/proxy/check"' in src
    assert "def _api_proxy_check" in src


def test_proxy_check_rejects_bad_id(tmp_path):
    """编号非法 / 不存在都要给中文原因 —— 不能抛给前端一个 500。"""
    from types import SimpleNamespace
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    # ⚠ 必须给真库：ProxyPool(db) 构造时就会查表，db=None 会先炸在
    #   "代理池不可用"，测不到编号校验（第一次就写成 SimpleNamespace(db=None)）。
    db = Database(tmp_path / "mrm.db")
    stub = SimpleNamespace(db=db)
    r = webui._Handler._api_proxy_check(stub, {"id": "abc"})
    assert r["ok"] is False and "编号" in r["error"], r

    r2 = webui._Handler._api_proxy_check(stub, {"id": 987654})
    assert r2["ok"] is False and "不存在" in r2["error"], r2


def test_proxy_check_caps_batch_size():
    """批量最多 8 个（同步阻塞，全测会让用户以为卡死）。"""
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    seg = src.split("def _api_proxy_check", 1)[1].split("def _api_learning", 1)[0]
    assert "[:8]" in seg, "批量测速没有上限"
    assert "只测了前 8 个" in seg, "截断了却没告诉用户"


def test_proxy_save_updates_credentials_on_url_change():
    """改地址时账号密码也要跟着变（否则旧密码会一直留在条目里）。"""
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    seg = src.split("def _api_proxy_save", 1)[1].split("def _api_proxy_delete", 1)[0]
    assert "entry.username = u.username" in seg
    assert "entry.password = u.password" in seg


def test_zh_error_matches_exception_class_name():
    """httpx 的 `ConnectError` 消息里没有 connect 这个词，只有类名有。"""
    from media_resource_manager.proxy import ProxyPool

    class ConnectError(Exception):
        pass

    msg = ProxyPool._zh_error(ConnectError("[WinError 10061] 目标计算机积极拒绝"))
    assert "无法连接代理" in msg, msg


def test_proxy_list_shows_disabled_entries():
    """停用的代理必须在列表里（否则用户停用一次就再也找不回来）。"""
    src = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    seg = src.split("def _api_proxy_list", 1)[1].split("def _api_proxy_save", 1)[0]
    assert "only_enabled=False" in seg


# ------------------------------------------------- 底栏剩余空间（同批交付）

def test_stats_reports_disk_space(tmp_path):
    """底栏「剩余 49.1 GB」的数据来源：/api/stats 的 disk_* 字段。"""
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.last_output_dir = tmp_path          # 测临时目录所在的盘
    cfg.min_free_mb = 1                     # 1MB，正常不会触发

    class Stub:
        def __init__(self):
            self.db, self.cfg = db, cfg

    r = webui._Handler._api_stats(Stub())
    assert r["ok"] is True, r
    assert isinstance(r.get("disk_free"), int) and r["disk_free"] > 0, r
    assert isinstance(r.get("disk_total"), int) and r["disk_total"] >= r["disk_free"], r
    assert r.get("disk_text"), "没算出人类可读的剩余空间"
    assert r.get("disk_low") is False, r
    assert str(tmp_path) in (r.get("disk_path") or ""), r


def test_stats_marks_low_disk(tmp_path):
    """低于 min_free_mb 时必须标出来 —— 底栏靠这个变红。"""
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.last_output_dir = tmp_path
    cfg.min_free_mb = 10 ** 12              # 1TB，任何机器都会判"低"

    class Stub:
        def __init__(self):
            self.db, self.cfg = db, cfg

    r = webui._Handler._api_stats(Stub())
    assert r["ok"] is True
    assert r["disk_low"] is True, r


def test_disk_fields_absent_when_unmeasurable(tmp_path):
    """测不到就**整组字段都不出现**，不能回 0 —— 0 会被前端显示成「剩余 0 B」，
    比不显示更吓人（用户以为盘满了）。"""
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    db = Database(tmp_path / "mrm.db")
    cfg = AppConfig()
    cfg.last_output_dir = tmp_path / "不存在的目录" / "更深一层"
    cfg.min_free_mb = 200

    class Stub:
        def __init__(self):
            self.db, self.cfg = db, cfg

    r = webui._Handler._api_stats(Stub())
    assert r["ok"] is True
    # Windows 上不存在的路径仍能取到所在卷的剩余空间，所以这里只断言
    # "要么没有字段，要么是真的数字"，不允许 0 冒充
    if "disk_free" in r:
        assert r["disk_free"] > 0, r


def test_statusbar_renders_disk_with_warning():
    a = _read("App.vue")
    assert 'st.disk_text' in a
    assert 'name="hard-drive"' in a
    assert 'bad: st.disk_low' in a
    # 悬停要能看出测的是哪个路径（用户常把图存到别的盘）
    assert "st.disk_path" in a
