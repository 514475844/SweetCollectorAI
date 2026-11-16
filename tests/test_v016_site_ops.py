"""V0.0.16 护栏：网页补齐的维护动作 + 破坏性操作双重确认。

钉死四条：
  1. 五个 POST / 三个 GET 端点都接进路由（护栏按源码字面量判）；
  2. 清空日志、清空隔离区、全面清理三个真删动作后端必须校验 confirm；
  3. 日志导出走加密 .mrmx（需求 19），不是明文 CSV 直接送出；
  4. 「暗夜风」是用户 2026-10-29 明确删掉的网页皮肤，不许被当成欠账加回来。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from media_resource_manager import export_kit as ek
from media_resource_manager import site_ops as so
from media_resource_manager.crypt import decrypt_bytes

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class _DB:
    def __init__(self):
        self._c = sqlite3.connect(":memory:")
        self._c.row_factory = sqlite3.Row
        self._c.executescript(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "ts TEXT, actor TEXT, action TEXT, target TEXT, detail TEXT)")
        self._c.executemany(
            "INSERT INTO audit_log(ts,actor,action,target,detail) VALUES(?,?,?,?,?)",
            [("2026-11-11 10:00:00", "web", "login", "本机", "登录成功"),
             ("2026-11-11 10:01:00", "web", "backfill", "", "补写 3 张")])
        self._c.commit()

    def query(self, sql, params=()):
        return self._c.execute(sql, tuple(params)).fetchall()

    def execute(self, sql, params=()):
        cur = self._c.execute(sql, tuple(params))
        self._c.commit()
        return cur

    def close(self):
        self._c.close()


def test_routes_are_wired():
    text = _read("media_resource_manager/webui.py")
    for p in ("/api/site/data-location", "/api/site/cleanup-items",
              "/api/site/logs/export", "/api/site/backfill",
              "/api/site/backup-sync", "/api/site/logs/clear",
              "/api/site/quarantine/prune", "/api/site/cleanup"):
        assert f'"{p}"' in text, f"端点未接入路由：{p}"


def test_destructive_actions_require_confirm():
    text = _read("media_resource_manager/webui.py")
    for marker in ("def _api_site_logs_clear(self, body):",
                   "def _api_site_quarantine_prune(self, body):",
                   "def _api_site_cleanup(self, body):"):
        i = text.find(marker)
        assert i >= 0, f"缺少方法 {marker}"
        assert "self._confirmed(" in text[i:i + 700], f"{marker} 未校验 confirm"


def test_log_export_is_encrypted():
    text = _read("media_resource_manager/webui.py")
    i = text.find("def _api_site_logs_export(self):")
    assert i >= 0
    block = text[i:i + 900]
    assert "ek.encrypted(" in block
    assert ".mrmx" in block


def test_night_theme_stays_removed():
    """用户明确删过：网页端不许再出现第二个深色皮肤。"""
    text = _read("media_resource_manager/webapp/src/App.vue")
    i = text.find("const THEMES = [")
    assert i >= 0
    assert "key: 'night'" not in text[i:i + 400]


def test_frontend_buttons_present():
    assert "开始回填" in _read("media_resource_manager/webapp/src/views/Maintenance.vue")
    assert "siteLogsClear" in _read("media_resource_manager/webapp/src/views/Logs.vue")
    assert "siteQuarantinePrune" in _read("media_resource_manager/webapp/src/views/Quarantine.vue")
    js = _read("media_resource_manager/webapp/src/api.js")
    for name in ("siteBackfill", "siteBackupSync", "siteCleanup"):
        assert name in js


def test_cleanup_items_come_from_cleanup_module():
    items = so.cleanup_items()
    keys = {x["key"] for x in items}
    assert "library" in keys and "prefs" in keys and "rules" in keys
    assert len(items) == 11
    assert all("label" in x and "desc" in x for x in items)


def test_empty_plan_is_refused():
    r = so.full_cleanup(_DB(), object(), [])
    assert not r["ok"]
    r2 = so.sync_backup(_DB(), object(), "")
    assert not r2["ok"]


def test_audit_log_export_and_clear():
    db = _DB()
    raw = so.audit_log_csv(db)
    assert raw.startswith(b"\xef\xbb\xbf")
    assert "backfill" in raw.decode("utf-8-sig")
    blob = ek.encrypted(raw, "")
    assert blob != raw and decrypt_bytes(blob, "") == raw
    r = so.clear_audit_logs(db)
    assert r["ok"] and r["deleted"] == 2
    assert db.query("SELECT COUNT(*) c FROM audit_log")[0]["c"] == 0
    db.close()


def test_data_location_is_read_only(monkeypatch):
    from media_resource_manager import config as cfgmod

    tmp = Path(tempfile.mkdtemp(prefix="v016_loc_"))
    try:
        monkeypatch.setattr(cfgmod, "user_data_dir", lambda: tmp)
        cfg = type("Cfg", (), {
            "db_path": tmp / "mrm.db", "rules_dir": tmp / "rules",
            "log_dir": tmp / "logs", "library_dir": tmp / "library",
            "isolate_dir": tmp / "isolate",
        })()
        r = so.data_location(cfg)
        assert r["ok"] and r["root"] == str(tmp.resolve())
        assert not (tmp / "rules").exists(), "data_location 不该创建任何目录"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
