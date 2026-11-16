"""R143 护栏：隔离区「Web 端 ↔ 桌面端」口径必须一致，且桌面端不能硬截断。

钉的是**行为**（真 SQLite 上跑同一条 where），不是钉数值：
  · Web 视图口径与桌面端常量**归一化后必须逐字相同** —— 两边各写一份
    SQL 就是 R143 的成因（Web 少了 AND needs_review=0 ⇒ 比桌面端多一批、
    并且与待审核页有交集）；
  · 四态机互斥：进隔离区必须同时清 needs_review（Web 与桌面端都要清）；
  · 桌面端隔离区**不再有写死的 LIMIT 1000**，且带「加载更多」+ 真总数。

反向可变：把 Web 的 where 改回 "in_quarantine=1" → 第 1/2 条必须红。
"""

from __future__ import annotations

import pathlib
import re
import sqlite3

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBUI = ROOT / "media_resource_manager" / "webui.py"
MAINWIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"


# ---------------------------------------------------------------- 取源码口径


def _src(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _web_where(view: str) -> str:
    """从 webui.py 的 where 映射里取出某个视图的 SQL 片段。"""
    src = _src(WEBUI)
    m = re.search(r'"%s"\s*:\s*"([^"]+)"' % re.escape(view), src)
    assert m, f"webui.py 里找不到 {view!r} 视图的 where 片段"
    return m.group(1)


def _desktop_quar_where() -> str:
    src = _src(MAINWIN)
    m = re.search(r'_QUAR_WHERE\s*=\s*"([^"]+)"', src)
    assert m, "main_window.py 里找不到 _QUAR_WHERE 常量（隔离区口径应收敛成常量）"
    return m.group(1)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# ---------------------------------------------------------------- 真库夹具


@pytest.fixture()
def db(tmp_path):
    """一张最小 images 表 + 三类数据（含「两边都置位」的历史脏数据）。"""
    con = sqlite3.connect(str(tmp_path / "mrm.db"))
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE images(id INTEGER PRIMARY KEY, path TEXT, "
        "in_quarantine INTEGER DEFAULT 0, needs_review INTEGER DEFAULT 0, "
        "blacklisted INTEGER DEFAULT 0, phash TEXT)")
    rows = []
    # A：纯隔离区 5 条（用户主动操作进来的回收站）
    for i in range(5):
        rows.append((f"/q/{i}.jpg", 1, 0, 0, None))
    # B：纯待审核 3 条
    for i in range(3):
        rows.append((f"/p/{i}.jpg", 0, 1, 0, None))
    # C：交集 2 条 —— 互斥后不该存在，但历史库里有，口径必须仍把它排除掉
    for i in range(2):
        rows.append((f"/both/{i}.jpg", 1, 1, 0, None))
    # D：图库 2 条
    for i in range(2):
        rows.append((f"/g/{i}.jpg", 0, 0, 0, None))
    con.executemany(
        "INSERT INTO images(path,in_quarantine,needs_review,blacklisted,phash) "
        "VALUES (?,?,?,?,?)", rows)
    con.commit()
    yield con
    con.close()


def _count(con, where: str) -> int:
    return int(con.execute("SELECT COUNT(*) c FROM images WHERE " + where
                           ).fetchone()["c"])


# ---------------------------------------------------------------- 断言


def test_web_and_desktop_quarantine_where_are_identical():
    """★ 主判据：两边口径归一化后必须逐字相同（不许各写一份 SQL）。"""
    assert _norm(_web_where("quarantine")) == _norm(_desktop_quar_where()), (
        "Web 端与桌面端隔离区口径不一致 —— R143 用户实测「数量对不上」"
        f"\n  web     = {_web_where('quarantine')!r}"
        f"\n  desktop = {_desktop_quar_where()!r}")


def test_quarantine_where_excludes_pending_rows():
    """口径必须带 needs_review=0：少了它，隔离区与待审核页会有交集。"""
    assert "needs_review=0" in _norm(_web_where("quarantine"))
    assert "in_quarantine=1" in _norm(_web_where("quarantine"))


def test_web_quarantine_count_equals_desktop_count(db):
    """同一份库上，Web 列表口径与桌面端列表口径数出来的条数必须相等。"""
    assert _count(db, _web_where("quarantine")) == \
        _count(db, _desktop_quar_where()) == 5


def test_quarantine_and_pending_do_not_overlap(db):
    """★ 互斥：两个视图口径**相交**必须为 0 条。

    反向可变：把 Web 的 where 退回 "in_quarantine=1"，这里立刻变 2 条 ⇒ 红。
    历史库里确实存在两边都置位的脏数据（夹具 2 条），口径必须仍把它们
    挡在隔离区之外 —— 这正是用户「web 隔离区和待审核像同一个东西」的成因。
    """
    for name, w in (("web", _web_where), ("desktop", lambda v: _desktop_quar_where())):
        quar = w("quarantine")
        pend = _web_where("pending")
        overlap = _count(db, f"({quar}) AND ({pend})")
        assert overlap == 0, (
            f"{name} 端隔离区与待审核有 {overlap} 条交集 —— 两个页面会看到同一张图")


def test_dirty_rows_with_both_flags_are_still_excluded(db):
    """「两边都置位」的历史脏数据：总数确实存在，但隔离区口径必须排除它。"""
    assert _count(db, "in_quarantine=1 AND needs_review=1") == 2, "夹具应有脏数据"
    assert _count(db, _desktop_quar_where()) == 5, \
        "脏数据被算进隔离区了（应为 5 条纯隔离）"


def _updates_setting_quarantine(src: str) -> list[str]:
    """取所有「送图进隔离区」的 UPDATE 语句（跨行字符串拼接也要收进来）。

    ⚠ 不能写 `[^"]*`：源码里 SQL 是 `"..."` + `"..."` 多行拼接，
    一个 `"` 就把匹配掐断了，会把判据变成假红。
    """
    return re.findall(r"UPDATE images SET in_quarantine=1(.{0,200}?)WHERE id=\?",
                      src, re.S)


def test_delete_action_clears_needs_review_on_both_sides():
    """四态机：进隔离区必须同时清 needs_review，Web 与桌面端都要清。"""
    hits = _updates_setting_quarantine(_src(WEBUI))
    assert hits, "webui.py 里找不到「送进隔离区」的 UPDATE"
    for s in hits:
        assert "needs_review=0" in s, (
            "Web 端送图进隔离区没清 needs_review ⇒ 该图会同时出现在隔离区与待审核")

    desk = _updates_setting_quarantine(_src(MAINWIN))
    assert desk, "main_window.py 里找不到「送进隔离区」的 UPDATE"
    for s in desk:
        assert "needs_review=0" in s, (
            "桌面端送图进隔离区没清 needs_review：两端归属会不一致")


def test_pending_action_clears_quarantine():
    """反向：送进待审核必须清 in_quarantine（四态机另一侧）。"""
    src = _src(WEBUI)
    m = re.search(r"UPDATE images SET needs_review=1(.{0,200}?)WHERE id=\?",
                  src, re.S)
    assert m, "webui.py 里找不到「送进待审核」的 UPDATE（move 分支）"
    assert "in_quarantine=0" in m.group(1), \
        "送进待审核没清 in_quarantine ⇒ 与隔离区不互斥"


def test_desktop_quarantine_has_no_hard_coded_1000():
    """桌面端隔离区不能再有写死的 LIMIT 1000（>1000 条会「后面全看不到」）。"""
    body = _src(MAINWIN)
    start = body.index("def quarantine_manager")
    end = body.index("def open_purged_snapshot", start)
    # ⚠ 只判**代码**，注释里出现的「原来写死 LIMIT 1000」不算数，
    # 否则判据永远假红（自己写的说明文字会把自己钉死）。
    seg = re.sub(r"#[^\n]*", "", body[start:end])
    assert "LIMIT 1000" not in seg, \
        "隔离区取数又硬编码了 LIMIT 1000 —— 超过 1000 条后面的看不到"
    assert "LIMIT ? OFFSET ?" in seg, "隔离区取数应走 分批/翻页（LIMIT ? OFFSET ?）"
    assert "加载更多" in seg, "分批加载要给用户「加载更多」入口"


def test_desktop_quarantine_title_reports_true_total():
    """标题必须显示**真总数**（单独 COUNT），不能把本批条数当总数。"""
    seg = _src(MAINWIN)[_src(MAINWIN).index("def quarantine_manager"):
                        _src(MAINWIN).index("def open_purged_snapshot")]
    assert "SELECT COUNT(*) c FROM images WHERE" in seg, \
        "隔离区缺少独立的 COUNT(*) 总数查询"
    assert "当前已载入" in seg, "标题应区分「共 N 条」与「已载入 x 条」"


def test_quarantine_paging_reaches_the_oldest_row(db):
    """★ 分页真能翻到最旧的一条：1200 条分批取完必须覆盖全部 id。"""
    con = db
    con.executemany(
        "INSERT INTO images(path,in_quarantine,needs_review,blacklisted,phash) "
        "VALUES (?,?,?,?,?)",
        [(f"/old/{i}.jpg", 1, 0, 0, None) for i in range(1200)])
    con.commit()
    total = _count(con, _desktop_quar_where())
    assert total > 1000, "夹具没造够 1000 条以上，测不出硬截断"

    page, off, seen = 500, 0, []
    while True:
        batch = [r["id"] for r in con.execute(
            "SELECT id FROM images WHERE %s ORDER BY id DESC LIMIT ? OFFSET ?"
            % _desktop_quar_where(), (page, off)).fetchall()]
        if not batch:
            break
        seen.extend(batch)
        off += len(batch)
    assert len(seen) == total, "分批加载没覆盖全部隔离记录"
    # 最旧的一条（最小 id）必须在取回的集合里
    oldest = int(con.execute(
        "SELECT MIN(id) m FROM images WHERE " + _desktop_quar_where()
    ).fetchone()["m"])
    assert oldest in seen, "翻不到最旧的一条 —— 硬截断回来了"


def test_bulk_cleanup_covers_all_rows_not_just_current_page():
    """清理缺失 / 清空必须走**全量**记录，不能只处理当前页。"""
    seg = _src(MAINWIN)[_src(MAINWIN).index("def quarantine_manager"):
                        _src(MAINWIN).index("def open_purged_snapshot")]
    assert "def all_quarantine_rows" in seg, "缺少全量隔离记录查询"
    assert "rows = all_quarantine_rows()" in seg
    # 清理/清空两处都必须换成全量
    assert seg.count("all_quarantine_rows()") >= 3, \
        "清理缺失与清空隔离区应都改用全量记录（否则只处理最新一批）"


def test_source_comment_pins_the_parity_contract():
    """两侧源码都要写明「口径必须与对端一致」，否则下次又会各改一边。"""
    src = _src(WEBUI)
    # 契约注释写在视图 where 映射的 `"gallery":` **之前**，从那里往前取一段。
    # ⚠ 必须锚在**那处 SQL 视图映射**上，不能取首个 `"gallery":`：R167 起
    #   文件上方多了统计角标的 `"gallery": 0`（第 307 行那个），取首个会把
    #   窗口落在一段无关代码上，契约注释明明还在却判红（仪器坑）。
    i = src.index('"quarantine": "in_quarantine=1 AND needs_review=0"')
    assert "quarantine_manager" in src[max(0, i - 1500):i], \
        "webui.py 的 where 映射旁应写明与桌面端 quarantine_manager 的口径契约"
    desk = _src(MAINWIN)[_src(MAINWIN).index("def quarantine_manager"):]
    assert "webui" in desk[:4000], \
        "桌面端隔离区旁应写明与 Web 端 webui 的口径契约"
