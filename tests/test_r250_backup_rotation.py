"""R250：整库备份自动轮换（份数 / 天数 / 总量三条规则取并集）。

为什么单独一轮做：Docker 化以后是 7×24 挂着跑，每天一份几百 MB 的整包
备份，一个月就是十几 GB —— 而原来**只有手动删除**（网页端连手动清理都
没有，只有列表里逐个删）。
"""
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"

NOW = datetime(2026, 10, 29, 12, 0, 0)


def _names(*, days_ago):
    from media_resource_manager.fullbackup import full_name
    return [full_name(NOW - timedelta(days=d)) for d in days_ago]


# ───────────────────────── 纯策略 ─────────────────────────
def test_keep_keeps_newest_n():
    from media_resource_manager.fullbackup import prune_plan
    names = _names(days_ago=[41, 40, 10, 3, 2, 1, 0])
    drop = prune_plan(names, keep=3, days=0, max_bytes=0, now=NOW)
    assert len(drop) == 4
    assert all(n not in drop for n in names[-3:])


def test_days_drops_old_but_keeps_newest():
    from media_resource_manager.fullbackup import prune_plan
    names = _names(days_ago=[41, 40, 10, 3, 2, 1, 0])
    drop = prune_plan(names, keep=0, days=7, max_bytes=0, now=NOW)
    assert set(drop) == set(names[:3])


def test_total_bytes_cap_drops_oldest_first():
    from media_resource_manager.fullbackup import prune_plan
    names = _names(days_ago=[6, 5, 4, 3, 2, 1, 0])
    entries = [(n, 1000) for n in names]
    drop = prune_plan(entries, keep=0, days=0, max_bytes=3000, now=NOW)
    # 7 份 × 1000B 压到 3000B ⇒ 只留最新 3 份
    assert drop == sorted(names[:4])


def test_never_deletes_the_only_backup():
    """只剩一份、而且已经超期超量时，也必须留着 —— 那是唯一退路。"""
    from media_resource_manager.fullbackup import prune_plan
    names = _names(days_ago=[400])
    assert prune_plan(names, keep=1, days=30, max_bytes=1, now=NOW) == []
    assert prune_plan([(names[0], 10 ** 9)], keep=0, days=0,
                      max_bytes=1, now=NOW) == []


def test_all_rules_zero_means_no_cleanup():
    from media_resource_manager.fullbackup import prune_plan
    names = _names(days_ago=[400, 300, 200])
    assert prune_plan(names, keep=0, days=0, max_bytes=0, now=NOW) == []


def test_ignores_non_full_backup_names():
    """R232 的每日库快照（mrm_db_backup_*.db）不属于整包，绝不能被它删。"""
    from media_resource_manager.fullbackup import prune_plan
    names = _names(days_ago=[40, 30]) + [
        "mrm_db_backup_20260101.db", "mrm_db_backup_20260102.db"]
    drop = prune_plan(names, keep=1, days=0, max_bytes=0, now=NOW)
    assert all(n.startswith("mrm_full_") for n in drop), drop
    assert len(drop) == 1


def test_accepts_plain_name_list():
    """不带 size 也要能算（只有份数/天数规则时用得上）。"""
    from media_resource_manager.fullbackup import prune_plan
    names = _names(days_ago=[3, 2, 1])
    assert prune_plan(names, keep=2) == [sorted(names)[0]]


# ───────────────────────── 真删文件 ─────────────────────────
def test_prune_and_delete_removes_files_and_reports_bytes(tmp_path):
    from media_resource_manager import fullbackup as fb
    names = _names(days_ago=[6, 5, 4, 3, 2, 1, 0])
    for n in names:
        (tmp_path / n).write_bytes(b"x" * 100)
    (tmp_path / "mrm_db_backup_20260101.db").write_bytes(b"y" * 10)
    res = fb.prune_and_delete(tmp_path, keep=3, days=0, max_bytes=0)
    assert len(res["deleted"]) == 4
    assert res["bytes"] == 400
    assert len(list(tmp_path.glob("mrm_full_*.zip"))) == 3
    # 每日快照必须还在
    assert (tmp_path / "mrm_db_backup_20260101.db").exists()


def test_prune_and_delete_on_missing_dir_is_safe(tmp_path):
    from media_resource_manager import fullbackup as fb
    res = fb.prune_and_delete(tmp_path / "nope", keep=3)
    assert res == {"deleted": [], "bytes": 0}


# ───────────────────────── 接线 ─────────────────────────
def test_export_prunes_after_success():
    """清理必须挂在「备份成功之后」，不能在备份失败时把旧备份删了。"""
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    # 取这个方法体（到下一个同级方法为止）—— 注意方法内部还有嵌套的
    # `def _run():`，不能简单按 "def " 切
    rest = src.split("def _api_full_export", 1)[1]
    m = re.search(r"\n    def ", rest)
    seg = rest[: m.start()] if m else rest
    assert "_prune_full_backups()" in seg
    # 必须在 _job_finish(成功) 之前调用
    assert seg.index("_prune_full_backups()") < seg.index('message="备份完成"')


def test_prune_route_and_settings_writable():
    src = (PKG / "webui.py").read_text(encoding="utf-8")
    assert '"/api/backup/full/prune"' in src
    for k in ("full_backup_keep", "full_backup_days", "full_backup_max_gb"):
        assert f'"{k}"' in src, f"可写白名单缺 {k}"


def test_config_has_rotation_fields():
    from media_resource_manager.config import AppConfig
    c = AppConfig()
    assert int(c.full_backup_keep) >= 1
    assert int(c.full_backup_days) >= 1
    assert float(c.full_backup_max_gb) > 0


def test_frontend_wires_rotation():
    st = (PKG / "webapp/src/views/Settings.vue").read_text(encoding="utf-8")
    for k in ("full_backup_keep", "full_backup_days", "full_backup_max_gb",
              "pruneFull"):
        assert k in st, f"设置页缺 {k}"
    api = (PKG / "webapp/src/api.js").read_text(encoding="utf-8")
    assert "fullPrune:" in api


def test_built_bundle_contains_rotation():
    d = PKG / "web/static/app/assets"
    js = "".join(p.read_text(encoding="utf-8", errors="replace")
                 for p in d.glob("*.js"))
    assert "full_backup_keep" in js and "full/prune" in js, "前端没重新构建"
