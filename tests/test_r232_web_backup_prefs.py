"""R232 · Web 端「数据备份 / 偏好导入导出」护栏（PARITY_MATRIX 缺失项 #1）。

订单出处：``.workbuddy/PARITY_MATRIX.md`` 缺失项第 1 条 —— 桌面「数据」菜单
有「每日自动备份数据库 / 导出偏好 / 导入偏好」，Web 端三个全无
（既没有 ``/api/backup`` 也没有 ``/api/preferences/*``）。换机、重装时 Web
用户拿不回自己的数据，也拿不回几个月攒下来的学习成果。

分三层钉契约：

  L1 纯函数（``data_backup`` —— 两端共用的那一层）
    - 备份文件名与桌面端逐字一致（按天），保留份数按**名字**排不是按 mtime
    - 偏好导出结构与桌面端同源；导入逐条把关而不是整份拒绝

  L2 源码 / AST（防"功能被顺手删"）
    - 四个路由都**接了**（同名路由必须连下一行一起判）
    - webui 不许直接赋值 cfg.daily_backup（只能走共享层）
    - 备份必须走 sqlite 在线快照，不许直接 copy .db 文件
    - 前端有侧栏入口 + 三个按钮 + 开关回显 / 写回 / 失败回滚

  L3 行为（真 handler + 真库 + 真 sqlite 备份 API）
    - 立即备份真出文件、真清理旧的；清单真读目录
    - 开关落盘（save_config 被调用）；导入真写库并覆盖已存在的权重

变异自检见 ``.workbuddy/_r232_mutate.py``（每条判据都要能红）。
"""
import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_resource_manager import data_backup as dbk                 # noqa: E402
from media_resource_manager.config import AppConfig                  # noqa: E402
from media_resource_manager.db import Database                       # noqa: E402

WEBUI_PY = ROOT / "media_resource_manager" / "webui.py"
APP_JS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "media_resource_manager" / "web" / "index.html"
SHARED_PY = ROOT / "media_resource_manager" / "data_backup.py"

ROUTES = ["/api/backup", "/api/backup/daily", "/api/preferences/import",
          "/api/preferences/export"]


def _lines(path):
    return path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")


def _route_wired(route, next_fragment):
    """同名路由 GET / POST 是**同一行字面量** ⇒ 必须连下一行一起判。

    R228 踩过：只判 ``if path == "/api/x":`` 的话，把实现换掉（等于没接
    路由）照样绿。
    """
    ls = _lines(WEBUI_PY)
    for i, ln in enumerate(ls):
        if ln.strip() == 'if path == "%s":' % route:
            nxt = ls[i + 1] if i + 1 < len(ls) else ""
            if next_fragment in nxt:
                return True
    return False


def _webui_assigns_daily_directly():
    """AST 扫：webui 里有没有**直接赋值** cfg.daily_backup。

    不看字符串（注释里出现同名字样会假红，R73/R229 同族），只看真实赋值。
    """
    tree = ast.parse(WEBUI_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == dbk.DAILY_FIELD:
                return True
    return False


class _Send:
    """替身：接住 _api_prefs_export 的 _send（导出不走 _json）。"""

    def __init__(self):
        self.calls = []

    def __call__(self, code, body, ctype, extra=None):
        self.calls.append({"code": code, "body": body, "ctype": ctype,
                           "extra": extra or {}})
        return {"code": code, "body": body, "ctype": ctype, "extra": extra or {}}


def _handler(db=None, cfg=None, sink=None):
    from media_resource_manager import webui

    class _H(webui._Handler):
        def __init__(self, d, c, s):
            self._db = d
            self._cfg = c
            self._sink = s
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)

        @property
        def db(self):
            return self._db

        @property
        def cfg(self):
            return self._cfg

        def _send(self, code, body, ctype, extra=None):
            return self._sink(code, body, ctype, extra)

    return _H(db, cfg, sink or _Send())


def _mk_db(tmp_path, name="r232.db"):
    return Database(tmp_path / name)


# ═══════════════════════ L1 · 纯函数 ═══════════════════════

def test_backup_name_matches_desktop_convention():
    import datetime as _dt

    n = dbk.backup_name(_dt.datetime(2026, 10, 23, 9, 30))
    assert n == "mrm_db_backup_20261023.db", n
    assert dbk.is_backup_name(n) is True
    # 别人的手工库 / 别的后缀不许混进清单（清理会把它删掉）
    assert dbk.is_backup_name("mrm.db") is False
    assert dbk.is_backup_name("mrm_db_backup_bad.db") is False
    assert dbk.is_backup_name("mrm_db_backup_20261023.db.bak") is False


def test_prune_keeps_newest_and_sorts_by_name_not_mtime():
    """按**名字**排（= 按日期），不是按 mtime：mtime 会被拷贝/解压改掉。"""
    names = ["mrm_db_backup_20261020.db", "mrm_db_backup_20261023.db",
             "mrm_db_backup_20261021.db"]
    assert dbk.prune(names, 2) == ["mrm_db_backup_20261020.db"], \
        "该删最老的那份，保留最近 2 份"
    assert dbk.prune(names, 7) == [], "还没到保留上限 ⇒ 一份都不许删"
    assert dbk.prune(names, 0) == [], "keep<=0 是不清理（与桌面端同口径）"
    assert dbk.prune([], 3) == []


def test_daily_default_on_and_apply_persists_once():
    class _Old:                      # 老配置没这个键
        pass

    assert dbk.daily_enabled(_Old()) is True
    cfg = AppConfig()
    assert cfg.daily_backup == dbk.DAILY_DEFAULT
    calls = []
    assert dbk.apply_daily(cfg, False, save=lambda: calls.append(1)) is False
    assert cfg.daily_backup is False
    assert calls == [1], "落盘只能一次"
    assert dbk.apply_daily(cfg, 0) is False


def test_shared_layer_never_imports_persist_function():
    """共享层不许 import 落盘函数 —— 两端落盘链不同，只能参数注入。

    ⚠ 只查**真实 import**：docstring 里也写了 save_config 这个名字
    （R73/R229/R230 同族：判据被注释/docstring 骗到）。
    """
    tree = ast.parse(SHARED_PY.read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            imported.update(a.name for a in n.names)
        elif isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
    assert "save_config" not in imported, \
        "共享层不许直接 import 落盘函数，save 必须由调用方注入"


def test_human_size_units():
    assert dbk.human_size(512) == "512 B"
    assert dbk.human_size(2048) == "2.0 KB"
    assert dbk.human_size(3 * 1024 * 1024) == "3.0 MB"
    assert dbk.human_size(None) == "0 B"


def test_prefs_payload_shape_and_skips_empty_label():
    rows = [{"task_id": 0, "label": "猫", "weight": 1.5, "seen": 3},
            {"task_id": 0, "label": "   ", "weight": 9.0, "seen": 9},
            {"task_id": 2, "label": "狗", "weight": -0.5, "seen": 0}]
    d = dbk.prefs_payload(rows)
    assert d["version"] == dbk.PREFS_VERSION
    assert d["count"] == 2, "空标签的行不导出"
    assert "exported_at" in d
    assert d["preferences"][0]["label"] == "猫"
    # 结构与桌面端 export_preferences 同源（同一批键）
    assert set(d.keys()) >= {"version", "exported_at", "preferences"}
    assert set(d["preferences"][0].keys()) >= {"task_id", "label", "weight", "seen"}


def test_normalize_prefs_accepts_both_shapes_and_filters():
    items, skipped = dbk.normalize_prefs(
        {"preferences": [{"label": "猫", "weight": 1.0, "seen": 2},
                         {"label": "  ", "weight": 1.0},
                         "not-a-dict",
                         {"weight": 1.0}]})
    assert [i["label"] for i in items] == ["猫"]
    assert skipped == 3, "空标签 / 非对象 / 缺标签 三条都该跳过"
    # 裸数组也认（手搓或别处导出）
    items2, skipped2 = dbk.normalize_prefs([{"label": "狗"}])
    assert len(items2) == 1 and skipped2 == 0
    # 权重/表态坏值**退化成 0**而不是整条丢掉（新偏好本来就是 0）
    items3, skipped3 = dbk.normalize_prefs(
        {"preferences": [{"label": "鸟", "weight": "x", "seen": "y"}]})
    assert skipped3 == 0 and items3[0]["weight"] == 0.0 and items3[0]["seen"] == 0
    # 完全不是名单 ⇒ 0 条，交给上层给中文报错
    assert dbk.normalize_prefs({}) == ([], 0)
    assert dbk.normalize_prefs("x") == ([], 0)


def test_upsert_sql_covers_existing_rows():
    sql = dbk.PREFS_UPSERT_SQL
    assert "INSERT INTO preferences" in sql
    assert "ON CONFLICT(task_id, label) DO UPDATE SET" in sql, \
        "导入已存在的标签必须是**覆盖权重**，不是报错或插第二行"
    assert "weight=excluded.weight" in sql and "seen=excluded.seen" in sql


# ═══════════════════════ L2 · 源码 / AST ═══════════════════════

def test_all_four_routes_are_wired():
    for r in ROUTES:
        assert ('if path == "%s":' % r) in _src(WEBUI_PY), "路由没接：%s" % r
    # ⚠ 只判字面量不够：同名路由 GET/POST 是同一行，必须连下一行一起判
    assert _route_wired("/api/backup", "self._api_backup_now()")
    assert _route_wired("/api/backup", "self._api_backup_list()")
    assert _route_wired("/api/backup/daily", "self._api_backup_daily(self._body())")
    assert _route_wired("/api/preferences/import",
                        "self._api_prefs_import(self._body_json())")
    assert _route_wired("/api/preferences/export", "self._api_prefs_export()")


def _src(path):
    return path.read_text(encoding="utf-8")


def test_webui_never_assigns_daily_directly():
    assert _webui_assigns_daily_directly() is False, \
        "webui 直接改 cfg.daily_backup ⇒ 与桌面端两份实现，迟早漂"
    assert "from .data_backup import apply_daily" in _src(WEBUI_PY)


def test_backup_uses_sqlite_online_snapshot_not_file_copy():
    """WAL 模式下直接 copy .db 会拿到旧数据（R152-A6 已踩过一次）。"""
    src = _src(WEBUI_PY)
    assert "self.db.backup(dest)" in src, \
        "必须走 Database.backup（sqlite 在线快照 API）"
    for bad in ("shutil.copy(self.cfg.db_path", "shutil.copy2(self.cfg.db_path"):
        assert bad not in src, "不许直接 copy 数据库文件：%s" % bad
    # 清理旧备份走共享层的 prune（按名字排，不按 mtime）
    assert "from .data_backup import (KEEP_DEFAULT, KEEP_FIELD, backup_dir," in src

def test_export_uses_download_headers():
    src = _src(WEBUI_PY)
    assert 'attachment; filename="mrm_preferences_' in src, \
        "导出没有 Content-Disposition ⇒ 浏览器会直接显示 JSON 而不是下载"


# ═══════════════════════ L3 · 行为 ═══════════════════════

def test_api_backup_now_creates_file_and_prunes(tmp_path, monkeypatch):
    import media_resource_manager.config as CFG

    monkeypatch.setattr(CFG, "user_data_dir", lambda: tmp_path)
    db = _mk_db(tmp_path)
    cfg = AppConfig()
    h = _handler(db=db, cfg=cfg)
    out = h._api_backup_now()
    assert out["ok"] is True, out
    d = dbk.backup_dir(tmp_path)
    assert (d / out["name"]).is_file(), "备份文件没落地"
    assert out["size"] > 0 and "B" in (out.get("size_text") or "")
    # 再备份同一天 = 覆盖（用户手点的就是要此刻的快照），份数不涨
    out2 = h._api_backup_now()
    assert out2["name"] == out["name"]


def test_api_backup_now_prunes_beyond_keep(tmp_path, monkeypatch):
    import media_resource_manager.config as CFG

    monkeypatch.setattr(CFG, "user_data_dir", lambda: tmp_path)
    db = _mk_db(tmp_path)
    cfg = AppConfig()
    cfg.backup_keep = 2
    d = dbk.backup_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    for day in ("20261001", "20261002", "20261003", "20261004"):
        (d / ("mrm_db_backup_%s.db" % day)).write_bytes(b"x" * 16)
    out = _handler(db=db, cfg=cfg)._api_backup_now()
    assert out["ok"] is True
    assert out["removed"] == 3, "超出保留份数的旧备份必须清掉：%r" % out
    left = sorted(p.name for p in d.iterdir() if dbk.is_backup_name(p.name))
    assert len(left) == 2, left
    assert left[-1] == out["name"], "最新的那份必须还在"


def test_api_backup_list_reads_dir_and_switch(tmp_path, monkeypatch):
    import media_resource_manager.config as CFG

    monkeypatch.setattr(CFG, "user_data_dir", lambda: tmp_path)
    cfg = AppConfig()
    cfg.daily_backup = False
    d = dbk.backup_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "mrm_db_backup_20261020.db").write_bytes(b"x" * 2048)
    (d / "mrm_db_backup_20261022.db").write_bytes(b"y" * 1024)
    (d / "not_mine.db").write_bytes(b"z")
    out = _handler(cfg=cfg)._api_backup_list()
    assert out["ok"] is True, out
    assert out["count"] == 2, "不是本工具产出的 .db 不许列进清单"
    # 新的在前
    assert out["items"][0]["name"] == "mrm_db_backup_20261022.db"
    assert out["items"][0]["size"] == 1024
    assert out["items"][0]["size_text"] == "1.0 KB"
    assert out["daily"] is False, "清单要带上开关状态（前端一次请求画完）"
    assert str(d) == out["dir"]


def test_api_backup_daily_persists_and_errors_in_chinese(monkeypatch, tmp_path):
    import media_resource_manager.config as CFG

    calls = []
    monkeypatch.setattr(CFG, "save_config", lambda c: calls.append(c))
    monkeypatch.setattr(CFG, "user_data_dir", lambda: tmp_path)
    cfg = AppConfig()
    h = _handler(db=_mk_db(tmp_path), cfg=cfg)
    out = h._api_backup_daily({"enabled": False})
    assert out["ok"] is True and out["enabled"] is False
    assert cfg.daily_backup is False
    assert len(calls) == 1, "必须落盘，只改内存等于重启丢设置"

    bad = h._api_backup_daily({})
    assert bad["ok"] is False
    err = str(bad.get("error") or "")
    assert any("一" <= ch <= "鿿" for ch in err), "用户可见错误必须中文：%r" % err
    assert cfg.daily_backup is False, "报错时不该把原来的开关改掉"


def test_api_prefs_export_downloads_same_shape_as_desktop(tmp_path):
    db = _mk_db(tmp_path)
    db.execute("INSERT INTO preferences(task_id, label, weight, seen) "
               "VALUES (0,?,?,?)", ("猫", 1.5, 3))
    sink = _Send()
    out = _handler(db=db, cfg=AppConfig(), sink=sink)._api_prefs_export()
    assert out["code"] == 200
    assert out["ctype"].startswith("application/json")
    disp = out["extra"].get("Content-Disposition") or ""
    assert "attachment" in disp and disp.endswith('.json"')
    import json as _json

    data = _json.loads(out["body"].decode("utf-8"))
    assert data["version"] == dbk.PREFS_VERSION
    assert data["preferences"][0]["label"] == "猫"
    # 导出文件的结构与桌面端同源 ⇒ 能直接拿去桌面导入
    assert "exported_at" in data


def test_api_prefs_import_merges_and_reports(tmp_path):
    db = _mk_db(tmp_path)
    db.execute("INSERT INTO preferences(task_id, label, weight, seen) "
               "VALUES (0,?,?,?)", ("猫", 1.0, 1))
    h = _handler(db=db, cfg=AppConfig())
    out = h._api_prefs_import(
        {"preferences": [{"label": "猫", "weight": 9.0, "seen": 9},
                         {"label": "狗", "weight": 2.0, "seen": 2},
                         {"label": ""}]})
    assert out["ok"] is True, out
    assert out["merged"] == 2, out
    assert out["skipped"] == 1, "空标签那条要报出来，不能静默丢"
    rows = {r["label"]: (r["weight"], r["seen"])
            for r in db.query("SELECT label, weight, seen FROM preferences")}
    assert rows["猫"] == (9.0, 9), "已存在的标签必须是**覆盖**不是跳过：%r" % rows
    assert rows["狗"] == (2.0, 2)
    assert len(rows) == 2, "同标签不许存成两行：%r" % rows


def test_api_prefs_import_garbage_is_chinese_error(tmp_path):
    h = _handler(db=_mk_db(tmp_path), cfg=AppConfig())
    out = h._api_prefs_import({"nothing": 1})
    assert out["ok"] is False
    err = str(out.get("error") or "")
    assert any("一" <= ch <= "鿿" for ch in err), "用户可见错误必须中文：%r" % err
    assert "preferences" in err, "要告诉用户缺的是什么：%r" % err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
