"""R48 / THOROUGH_AUDIT_ORDER E：Web 功能完整化（后端 API）。

来源：作业单 E（用户第 6 条前半）—— 「Web 要拥有软件上的所有功能」，
当前 Web 只能进页面、不能新建任务，功能残缺。

本文件只测**后端**（作业单要求「每条功能都要有后端测试」）。前端能渲染/
能触发由静态断言 + 人工真机复测兜底。

⚠ 贯穿全文件的铁律（作业单 E 原文）：**共用同一套业务逻辑/引擎，
   不要另写一套**。所以测试里刻意用「桌面端也用的函数」当判据
   （`repo.get_task` / `ProxyPool.list` / `get_preferences`），
   而不是去查 webui 自己的表 —— 如果哪天有人在 webui 里另写一套 SQL，
   这些测试就会红，那正是我们要的。
"""
import json

import pytest

from media_resource_manager import webui
from media_resource_manager.db import Database


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
    """只测业务方法：不走真 socket。"""

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

    # 设置/历史落盘一律拦住：测试不该写用户真实 config.json
    import media_resource_manager.config as cfgmod
    monkeypatch.setattr(cfgmod, "save_config", lambda *a, **k: None)

    h = H(cfg, db)
    yield h, cfg, db
    try:
        db.close()
    except Exception:
        pass


def _mk_task(h, **kw):
    body = {"name": "t1", "entry_urls": ["https://example.com"],
            "adapter_name": ""}
    body.update(kw)
    return h._api_task_save(body)


# ================================================== E-1 新建 / 编辑任务

def test_create_task(env):
    """E-1：Web 能**真正创建**任务（落库，桌面端能列出来）。"""
    from media_resource_manager.repo import list_tasks

    h, _cfg, db = env
    r = _mk_task(h)
    assert r["ok"] is True, f"建任务失败：{r}"
    assert r["task_id"]
    tasks = list_tasks(db)
    assert any(t.name == "t1" for t in tasks), "任务没落库"


def test_create_task_requires_entry_url(env):
    """E-1：没有入口网址 → 明确中文报错，不建空任务。"""
    h, _cfg, db = env
    r = h._api_task_save({"name": "空任务", "entry_urls": []})
    assert r["ok"] is False
    assert "入口" in r["error"]
    from media_resource_manager.repo import list_tasks
    assert not [t for t in list_tasks(db) if t.name == "空任务"]


def test_create_task_accepts_url_string(env):
    """表单把多个网址写成一个字符串（空格/逗号分隔）也要能拆开。"""
    from media_resource_manager.repo import get_task

    h, _cfg, _db = env
    r = h._api_task_save({"entry_urls": "https://a.com https://b.com"})
    assert r["ok"] is True, r
    from media_resource_manager.repo import get_task as gt
    t = gt(_db, r["task_id"])
    assert len(t.entry_urls) == 2, f"没拆开：{t.entry_urls}"


def test_edit_task_keeps_unspecified_fields(env):
    """E-1 ⚠ 编辑时**没传的字段必须保持原值**（不能被清成默认）。

    红→绿：从空白开始填会把这个字段清掉 —— 用户改个名字把整份配置丢了。
    """
    from media_resource_manager.repo import get_task

    h, _cfg, db = env
    r = _mk_task(h, concurrency=7, ai_labels=["nude"], priority=2)
    tid = r["task_id"]
    r2 = h._api_task_save({"task_id": tid, "name": "改个名"})
    assert r2["ok"] is True, r2
    t = get_task(db, tid)
    assert t.name == "改个名"
    assert t.concurrency == 7, f"并发被清成 {t.concurrency}"
    assert t.ai_labels == ["nude"], f"主题词被清成 {t.ai_labels}"
    assert t.priority == 2


def test_edit_missing_task_reports_chinese(env):
    h, _cfg, _db = env
    r = h._api_task_save({"task_id": 99999, "entry_urls": ["https://a.com"]})
    assert r["ok"] is False
    assert "未找到" in r["error"]


def test_task_detail_returns_all_form_fields(env):
    """E-1：编辑表单要能取回全部字段（否则打开编辑框是空的）。"""
    from media_resource_manager.repo import get_task

    h, _cfg, db = env
    r = _mk_task(h, ai_labels=["nude"], ai_labels_l2=["asian"],
                 ai_threshold=0.55, concurrency=6)
    d = h._api_task_detail(r["task_id"])
    assert d["ok"] is True
    t = d["task"]
    assert t["ai_labels"] == ["nude"]
    assert t["ai_labels_l2"] == ["asian"]
    assert abs(t["ai_threshold"] - 0.55) < 1e-6
    assert t["concurrency"] == 6
    # 字段名必须与 TaskConfig 对得上（前端按这些 key 填表）
    from media_resource_manager.repo import get_task as gt
    real = gt(db, r["task_id"])
    assert set(t) >= set(H._TASK_FORM_FIELDS.values()) - {"id"}


def test_web_words_join_desktop_history(env):
    """E-1：Web 建的词要进**桌面端同一个**历史池（两端互通）。"""
    h, cfg, _db = env
    _mk_task(h, ai_labels=["nude", "naked"],
             ai_blacklist=["watermark"])
    assert "nude" in list(cfg.ai_label_history)
    assert "watermark" in list(cfg.ai_excl_history)


def test_bad_number_field_does_not_crash(env):
    """E-1：数字字段填了非数字 → 忽略该字段，**不 500**。"""
    h, _cfg, _db = env
    r = _mk_task(h, concurrency="不是数字")
    assert r["ok"] is True, r


def test_duplicate_words_are_deduped(env):
    h, _cfg, _db = env
    r = _mk_task(h, ai_labels="nude nude  naked nude")
    from media_resource_manager.repo import get_task
    t = get_task(_db, r["task_id"])
    assert t.ai_labels == ["nude", "naked"], f"没去重：{t.ai_labels}"


# ================================================== E-2 启停 / 删除 / 重试

def test_task_start_and_stop_use_the_real_queue(env):
    """E-2：启停走**真队列对象**，不是自己维护一份状态。"""
    h, _cfg, _db = env
    tid = _mk_task(h)["task_id"]
    h._api_task_ctl(tid, "start")
    h._api_task_ctl(tid, "stop")
    assert h.server.queue.started == [tid]
    assert h.server.queue.stopped == [tid]


def test_task_delete_removes_row(env):
    """E-2：删除任务真删库（与桌面端同一句 SQL）。"""
    from media_resource_manager.repo import list_tasks

    h, _cfg, db = env
    tid = _mk_task(h)["task_id"]
    r = h._api_task_ctl(tid, "delete")
    assert r["ok"] is True, r
    assert all(t.id != tid for t in list_tasks(db))


def test_task_delete_missing_is_chinese_error(env):
    h, _cfg, _db = env
    r = h._api_task_ctl(99999, "delete")
    assert r["ok"] is False
    assert "未找到" in r["error"]


def test_task_retry_clears_error_and_starts(env):
    """E-2：重试 = 清错误 + 重新启动。"""
    h, _cfg, db = env
    tid = _mk_task(h)["task_id"]
    db.execute("UPDATE tasks SET status='error', last_error='炸了' WHERE id=?",
               (tid,))
    r = h._api_task_ctl(tid, "retry")
    assert r["ok"] is True, r
    from media_resource_manager.repo import get_task
    t = get_task(db, tid)
    assert t.status.value != "error", "重试后状态还是 error"
    assert not (t.last_error or ""), "重试后错误没清"
    assert h.server.queue.started == [tid], "重试没重新入队"


def test_task_delete_works_without_queue(env):
    """⚠ 没有队列时删除仍要能用（后台模式没队列是常态）。"""
    h, _cfg, db = env
    tid = _mk_task(h)["task_id"]
    h.server.queue = None
    r = h._api_task_ctl(tid, "delete")
    assert r["ok"] is True, r


# ================================================== E-3 审核

def _mk_image(h, **kw):
    """插一条图片。⚠ images.task_id 是 NOT NULL —— 必须先有任务，
    插 NULL 会 IntegrityError（第一次写测试就踩了）。"""
    if not getattr(h, "_tid", None):
        h._tid = _mk_task(h)["task_id"]
    # ⚠ images 的 NOT NULL 列就两个：task_id + source_url。漏哪个都 IntegrityError
    #   （实测先后踩了 task_id 和 source_url 两次，别再漏）。
    # ⚠ images 的 NOT NULL 列就两个：task_id + source_url；且
    #   (task_id, source_url) 是 UNIQUE 联合键 —— 同一任务下 source_url
    #   不能重复（实测为此踩了第三次 IntegrityError，别再漏）。
    seq = getattr(h, "_seq", 0) + 1
    h._seq = seq
    cols = {"path": kw.pop("path", "x.jpg"), "task_id": h._tid,
            "source_url": f"https://example.com/a{seq}.jpg",
            "needs_review": 1, "in_quarantine": 0}
    cols.update(kw)
    keys = ",".join(cols)
    marks = ",".join("?" * len(cols))
    h.db.execute(f"INSERT INTO images({keys}) VALUES({marks})",
                 tuple(cols.values()))
    return h.db.query("SELECT id FROM images ORDER BY id DESC LIMIT 1")[0]["id"]


def test_review_actions_are_available(env):
    """E-3：审核动作走已有 `_api_image_action`（不另写一套）。"""
    h, _cfg, _db = env
    iid = _mk_image(h, tags="nude")
    r = h._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] is True, r
    assert r["done"] == 1
    row = h.db.query(
        "SELECT needs_review FROM images WHERE id=?", (iid,))[0]
    assert row["needs_review"] == 0, "审核后 needs_review 没清"


def test_review_rejects_unknown_action(env):
    h, _cfg, _db = env
    r = h._api_image_action({"ids": [1], "act": "格式化"})
    assert r["ok"] is False
    assert "不支持" in r["error"]


# ================================================== E-4 图库

def test_gallery_excludes_quarantined(env):
    """E-4：图库不含隔离区图片。"""
    h, _cfg, _db = env
    a = _mk_image(h, path="a.jpg", in_quarantine=0)
    b = _mk_image(h, path="b.jpg", in_quarantine=1)
    items = h._api_gallery()
    ids = [i["id"] for i in items]
    assert a in ids and b not in ids, f"隔离区图片漏进图库：{items}"


def test_gallery_has_thumb_url(env):
    h, _cfg, _db = env
    _mk_image(h, path="a.jpg", in_quarantine=0)
    items = h._api_gallery()
    assert items[0]["url"].startswith("/thumb/"), items


# ================================================== E-5 代理管理

def test_proxy_create_list_toggle_delete(env):
    """E-5：单条代理「确定 / 关闭 / 删除」全链路（复用 ProxyPool）。"""
    from media_resource_manager.proxy import ProxyPool

    h, _cfg, db = env
    r = h._api_proxy_save({"url": "socks5://127.0.0.1:1080", "name": "p1"})
    assert r["ok"] is True, r
    pid = r["id"]

    lst = h._api_proxy_list()
    assert lst["ok"] and any(i["id"] == pid for i in lst["items"])

    # 关闭 = 停用但**不删条目**
    r2 = h._api_proxy_save({"id": pid, "enabled": False})
    assert r2["ok"] is True and r2["enabled"] is False
    assert ProxyPool(db).get(pid).enabled is False, "关闭没生效"
    # 关闭 = 停用，仍在库里（能再打开），只是不进「可用代理」列表
    assert ProxyPool(db, ).list(only_enabled=False), "关闭后条目被删了（应为停用）"

    r3 = h._api_proxy_delete({"id": pid})
    assert r3["ok"] is True
    assert ProxyPool(db).get(pid) is None


def test_proxy_password_is_masked_in_list(env):
    """E-5：列表里密码打码（作业单铁律：不外发账号标识）。"""
    h, _cfg, _db = env
    h._api_proxy_save({"url": "http://user:secret@1.2.3.4:8080"})
    items = h._api_proxy_list()["items"]
    assert items
    assert "secret" not in items[0]["url"], f"密码没打码：{items[0]['url']}"


def test_proxy_rejects_empty_url(env):
    h, _cfg, _db = env
    r = h._api_proxy_save({"url": ""})
    assert r["ok"] is False
    assert "代理" in r["error"]


def test_proxy_socks_default_port(env):
    """socks5 不写端口时默认 1080（写错端口就永远连不上）。"""
    h, _cfg, db = env
    r = h._api_proxy_save({"url": "socks5://127.0.0.1"})
    from media_resource_manager.proxy import ProxyPool
    e = ProxyPool(db).get(r["id"])
    assert e.port == 1080, f"默认端口应 1080，实际 {e.port}"


# ================================================== E-6 设置

def test_settings_save_only_whitelisted(env):
    """E-6：设置**白名单制** —— 非白名单键不许通过 Web 改。

    2026-10-27：用户要求网页端也能改监听地址/端口/账号/密码，所以
    web_bind/web_port/web_user/web_password 已进白名单；本条改成用
    **真·白名单外**的键（dsn/secret_key 之类）验证白名单机制本身仍然有效。
    """
    h, cfg, _db = env
    r = h._api_settings_save({"watermark_alpha": 120,
                              "dsn": "hack",
                              "secret_key": "hack",
                              "not_a_real_setting": "hack"})
    assert r["ok"] is True, r
    assert cfg.watermark_alpha == 120
    for bad in ("dsn", "secret_key", "not_a_real_setting"):
        assert bad not in r["changed"], f"{bad} 是白名单外的键，却被写进配置了"
    assert not hasattr(cfg, "dsn") or getattr(cfg, "dsn", "") != "hack", \
        "白名单外的键被改了！"


def test_settings_save_type_coerced(env):
    h, cfg, _db = env
    h._api_settings_save({"watermark_alpha": "200"})
    assert cfg.watermark_alpha == 200
    assert isinstance(cfg.watermark_alpha, int)


def test_settings_save_rejects_all_unknown(env):
    h, cfg, _db = env
    r = h._api_settings_save({"nonexistent_key": 1})
    assert r["ok"] is False
    assert "没有" in r["error"]


# ================================================== E-7 学习看板

def test_learning_api_reads_shared_preferences(env):
    """E-7：看板读**桌面端同一份** preference（get_preferences）。"""
    from media_resource_manager.repo import set_preference, get_preferences

    h, _cfg, db = env
    set_preference(db, 0, "nude", 0.8)
    set_preference(db, 0, "watermark", -0.6)
    r = h._api_learning()
    assert r["ok"] is True, r
    names = [i["name"] for i in r["items"]]
    assert "nude" in names and "watermark" in names
    # 看板与桌面端读到的必须一致
    assert set(names) == set(get_preferences(db).keys())


def test_learning_items_sorted_by_weight(env):
    """按权重绝对值排序（最强的学习信号排最前）。"""
    from media_resource_manager.repo import set_preference

    h, _cfg, db = env
    set_preference(db, 0, "small", 0.1)
    set_preference(db, 0, "big", 0.9)
    r = h._api_learning()
    assert r["items"][0]["name"] == "big"


def test_learning_handles_empty_table(env):
    """没有学习记录时不能崩（看板要能显示「还没数据」）。"""
    h, _cfg, _db = env
    r = h._api_learning()
    assert r["ok"] is True
    assert r["items"] == []


# ================================================== 路由与错误中文

def test_new_endpoints_are_routed(env):
    """E：新增接口都挂在真实路由上（写对了函数但没接路由 = 白写）。"""
    from pathlib import Path

    src = Path(webui.__file__).read_text(encoding="utf-8")
    for ep in ("/api/task/save", "/api/task/delete", "/api/task/retry",
               "/api/task/pause", "/api/proxy", "/api/proxy/save",
               "/api/proxy/delete", "/api/settings/save", "/api/learning",
               "/api/task"):
        assert ep in src, f"{ep} 没接路由"


def test_all_error_messages_are_chinese(env):
    """H 段铁律：所有用户可见错误必须中文。"""
    h, _cfg, _db = env
    cases = [
        h._api_task_save({"entry_urls": []}),
        h._api_task_detail(99999),
        h._api_task_ctl(99999, "delete"),
        h._api_proxy_save({"url": ""}),
        h._api_proxy_delete({"id": 0}),
        h._api_settings_save({"zzz": 1}),
        h._api_image_action({"ids": [], "act": "like"}),
        h._api_image_action({"ids": [1], "act": "x"}),
    ]
    for r in cases:
        if r.get("ok") is False:
            err = r.get("error") or ""
            assert err, f"失败但没给提示：{r}"
            assert any("一" <= ch <= "鿿" for ch in err), f"提示不是中文：{err!r}"


def test_errors_are_captured_as_500_not_crash(env):
    """异常路径返回结构化 JSON，不把 handler 掀翻。"""
    h, _cfg, _db = env

    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("db exploded")

        def execute(self, *a, **k):
            raise RuntimeError("db exploded")

    h._db = _Boom()
    for call in (lambda: h._api_proxy_list(),
                 lambda: h._api_learning(),
                 lambda: h._api_proxy_save({"url": "http://1.2.3.4:8080"})):
        r = call()
        assert r["ok"] is False
        assert r.get("error")


def test_task_list_includes_error_for_display(env):
    """任务列表带上错误文案（Web 上要能看到失败原因）。"""
    h, _cfg, db = env
    tid = _mk_task(h)["task_id"]
    db.execute("UPDATE tasks SET last_error='连接失败' WHERE id=?", (tid,))
    items = h._api_tasks()
    row = [i for i in items if i["id"] == tid][0]
    assert row["error"] == "连接失败"

def test_ai_api_key_writable_but_never_echoed(env):
    """2026-10-27 #18：在线 API 的 key 要能在网页上改，但**读接口绝不能回显**。

    Web 页在局域网是裸奔的，回显一个付费 key 等于把它摊给每个连上来的人。
    所以：写进去能生效，读出来只能是「（已设置）」+ 一个布尔标记。
    """
    h, cfg, _db = env
    r = h._api_settings_save({"ai_api_key": "sk-very-secret"})
    assert r["ok"] is True, r
    assert cfg.ai_api_key == "sk-very-secret", "写不进去 = 网页改 key 的功能是假的"
    out = h._api_settings()
    assert out.get("ai_api_key") == "（已设置）", f"回显了明文：{out.get('ai_api_key')!r}"
    assert out.get("ai_api_key_set") is True, out.get("ai_api_key_set")
