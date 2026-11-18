# -*- coding: utf-8 -*-
"""V0.0.55 护栏：打标断点续跑修复 + 批1 各项。

背景（用户 2026-11-18 报 bug）：打标中断后点「继续打标/重新导入」，
已打好的标签全丢、从头再打。根因是 _write_batch 的 UPSERT 里带着
``ai_state=excluded.ai_state``——force 重导把整个任务的行统统重置回
待打标。断点续打的权威状态就是 ai_state 本身：老行保留、新行才置位。
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "media_resource_manager"


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_upsert_keeps_ai_state_of_existing_rows():
    s = (PKG / "import_folder.py").read_text(encoding="utf-8")
    seg = s.split("def _write_batch", 1)[1].split("def ", 2)[1]
    assert "ai_state=excluded.ai_state" not in seg, \
        "force 重导又会重置全任务打标进度（断点 bug 回归）"
    assert "ON CONFLICT(task_id, source_url) DO UPDATE SET" in s


def test_upsert_sql_semantics_resume_from_checkpoint():
    """真 SQL 实测：打完标 → force 重导 → ai_state/tags 保留、元数据更新。"""
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE images (task_id int, source_url text, sha256 text,"
        " width int, height int, bytes int, path text, tags text,"
        " score real, blacklisted int, in_quarantine int, ai_state int,"
        " PRIMARY KEY(task_id, source_url))")
    import re
    s = (PKG / "import_folder.py").read_text(encoding="utf-8")
    m = re.search(r'"""INSERT INTO images.*?"""', s, re.S)
    assert m, "找不到 _write_batch 的 UPSERT 语句"
    sql = m.group(0).strip('"')
    sql = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    db.execute(sql, (1, "file://a", "old", 1, 1, 10, "p", 1))
    db.execute("UPDATE images SET ai_state=1, tags='[\"猫\"]' WHERE source_url='file://a'")
    db.execute(sql, (1, "file://a", "new", 2, 2, 20, "p", 1))   # force 重导
    r = db.execute("SELECT ai_state, tags, sha256 FROM images").fetchone()
    assert r == (1, '["猫"]', "new"), f"断点未保持：{r}"
    db.execute(sql, (1, "file://b", "n2", 1, 1, 5, "p2", 0))    # 新行按参数置待打标
    assert db.execute(
        "SELECT ai_state FROM images WHERE source_url='file://b'").fetchone()[0] == 0


def test_tag_stage_orders_by_id_and_only_pending():
    """打标主循环必须只捞 ai_state=0 并按 id 排序 —— 断点续打的前提。"""
    s = (PKG / "import_folder.py").read_text(encoding="utf-8")
    assert "WHERE task_id=? AND ai_state=0 " in s
    assert "ORDER BY id LIMIT ?" in s


# ── 批1：SSRF 自保 ─────────────────────────────────────────
def test_ssrf_blocks_metadata_allows_home_nets():
    from media_resource_manager.ssrf import check_url, SsrfBlocked
    for u in ("http://192.168.1.100:8080/x", "http://127.0.0.1:8080",
              "http://10.0.0.5/a", "http://172.20.3.4/b"):
        check_url(u)                    # 自家 NAS/路由器：必须放行
    for u in ("http://169.254.169.254/latest/meta-data",
              "http://[fe80::1]/x", "http://0.0.0.0/y"):
        try:
            check_url(u)
            raise AssertionError(f"漏拦 {u}")
        except SsrfBlocked:
            pass


def test_ssrf_wired_into_fetcher_both_layers():
    """首查（_check_ip_filter）与逐跳钩子（event_hooks）都要接上。"""
    s = (PKG / "crawler/fetcher.py").read_text(encoding="utf-8")
    assert "from ..ssrf import check_url, SsrfBlocked" in s
    assert 'event_hooks={"request": [_ssrf_hook]}' in s
