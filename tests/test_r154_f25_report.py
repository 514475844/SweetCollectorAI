"""R154-F25 护栏：采集周报 / 月报（执行方笔记本棒）。

行为断言优先、源码判据兜底；变异自检 .workbuddy/_r211_mutate.py。

覆盖（与 R154-F25 订单逐条对齐）：
- period_range：周=周一~周日 / 月=1号~月末 / offset 往前翻 / **负 offset 钳回 0**
  / 非法 kind 回落 week；
- collect_period 真库：采集张数、收藏、隔离、判定分布、**通过率有分母才算**、
  无判定给 None（不给 0.0）、逐日**补零**、任务排行按采集量降序且带上
  喜欢/不喜欢、不喜欢 top 词来自 learning_stats；
- 导出：Markdown 含四大块 + **「全局口径」说明**；CSV 带 BOM + **逗号/引号转义**；
- 桌面：真建 QDialog 驱动（切周期 / 翻期 / 当期禁用下一期 / 导出文本非空）；
- Web：/api/report **接了路由** + 真调返回同一份 reporting 口径；
- 前端：reportRateText 把 null 翻成中文（不是 0%）。
"""
from __future__ import annotations

import ast
import datetime as _dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager.db import Database
from media_resource_manager.reporting import (
    PERIOD_MONTH, PERIOD_WEEK, collect_period, headline, pass_rate_text,
    period_range, to_csv, to_markdown,
)

REPORTING_PY = ROOT / "media_resource_manager" / "reporting.py"
WEBUI_PY = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
INDEX_HTML = ROOT / "media_resource_manager" / "web" / "index.html"


def _py_func_src(name: str) -> str:
    src = REPORTING_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    raise AssertionError(f"reporting.py 里没有函数 {name}")


# ==================== 共用：造一个真库 ====================

def _mk_db(tmp_path):
    db = Database(tmp_path / "f25.db")
    return db


def _add_task(db, name: str) -> int:
    cur = db.execute("INSERT INTO tasks(name) VALUES (?)", (name,))
    return int(cur.lastrowid)


def _add_image(db, task_id: int, url: str, day: str,
               favorite: int = 0, quarantine: int = 0) -> int:
    cur = db.execute(
        "INSERT INTO images(task_id, source_url, collected_at, favorite,"
        " in_quarantine) VALUES (?,?,?,?,?)",
        (task_id, url, day + " 10:00:00", favorite, quarantine))
    return int(cur.lastrowid)


def _add_review(db, image_id: int, task_id: int, decision: str, day: str):
    db.execute(
        "INSERT INTO reviews(image_id, decision, task_id, created_at) "
        "VALUES (?,?,?,?)", (image_id, decision, task_id, day + " 11:00:00"))


# ==================== period_range ====================

def test_week_starts_on_monday_and_ends_on_sunday():
    """周报必须是周一~周日（ISO 周），不是「往前数 7 天」。"""
    # 2026-10-22 是周四
    p = period_range(PERIOD_WEEK, _dt.date(2026, 10, 22))
    assert p["start"] == "2026-10-19", p
    assert p["end"] == "2026-10-25", p
    assert _dt.date.fromisoformat(p["start"]).weekday() == 0
    assert _dt.date.fromisoformat(p["end"]).weekday() == 6


def test_month_covers_first_to_last_day():
    p = period_range(PERIOD_MONTH, _dt.date(2026, 2, 17))
    assert p["start"] == "2026-02-01", p
    assert p["end"] == "2026-02-28", p          # 2026 非闰年


def test_offset_goes_back_one_period():
    w1 = period_range(PERIOD_WEEK, _dt.date(2026, 10, 22), 1)
    assert w1["start"] == "2026-10-12" and w1["end"] == "2026-10-18"
    m1 = period_range(PERIOD_MONTH, _dt.date(2026, 10, 22), 1)
    assert m1["start"] == "2026-09-01" and m1["end"] == "2026-09-30"
    m3 = period_range(PERIOD_MONTH, _dt.date(2026, 1, 5), 3)
    assert m3["start"] == "2025-10-01", m3      # 跨年回退


def test_negative_offset_is_clamped_to_current():
    """未来期一律钳回 0 —— 未来没数据，翻过去只会看到空表。"""
    p = period_range(PERIOD_WEEK, _dt.date(2026, 10, 22), -3)
    assert p["offset"] == 0
    assert p["start"] == "2026-10-19", p


def test_unknown_kind_falls_back_to_week():
    assert period_range("bogus", _dt.date(2026, 10, 22))["kind"] == PERIOD_WEEK
    assert period_range("", _dt.date(2026, 10, 22))["kind"] == PERIOD_WEEK
    # 非法 offset 不能把整个函数炸掉
    assert period_range(PERIOD_WEEK, _dt.date(2026, 10, 22), "x")["offset"] == 0


# ==================== collect_period（真库） ====================

def test_collect_counts_and_pass_rate(tmp_path):
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    t2 = _add_task(db, "人像")
    # 三张在窗口内，一张在窗口外
    i1 = _add_image(db, t1, "u1", "2026-10-19")
    i2 = _add_image(db, t1, "u2", "2026-10-20", favorite=1)
    i3 = _add_image(db, t2, "u3", "2026-10-21", quarantine=1)
    _add_image(db, t1, "u-out", "2026-10-01")     # 窗口外，不该算进来
    _add_review(db, i1, t1, "like", "2026-10-19")
    _add_review(db, i2, t1, "like", "2026-10-20")
    _add_review(db, i3, t2, "dislike", "2026-10-21")

    rep = collect_period(db, "2026-10-19", "2026-10-25")
    assert rep["collected"] == 3, rep
    assert rep["favorites"] == 1
    assert rep["quarantined"] == 1
    assert rep["reviewed"] == 3, rep
    assert rep["pos_count"] == 2 and rep["neg_count"] == 1
    assert abs(rep["pass_rate"] - 2 / 3) < 1e-9, rep["pass_rate"]
    assert rep["has_data"] is True


def test_pass_rate_is_none_without_any_decision(tmp_path):
    """★ 没有分母必须给 None，不是 0.0 —— 0% 会被读成「判了全不喜欢」。"""
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    _add_image(db, t1, "u1", "2026-10-19")
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    assert rep["pass_rate"] is None, rep
    assert rep["reviewed"] == 0
    assert rep["collected"] == 1
    assert pass_rate_text(None) == "本期还没有判定"


def test_skip_does_not_count_as_a_decision(tmp_path):
    """skip 不表态（DECISION_POLARITY 里没有它），不能进通过率分母。"""
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    i1 = _add_image(db, t1, "u1", "2026-10-19")
    _add_review(db, i1, t1, "skip", "2026-10-19")
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    assert rep["reviewed"] == 0, rep
    assert rep["pass_rate"] is None


def test_daily_series_is_zero_filled(tmp_path):
    """★ 哪天没采也要有一行 —— 跳过那天看起来像「软件没工作」。"""
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    _add_image(db, t1, "u1", "2026-10-19")
    _add_image(db, t1, "u2", "2026-10-19")
    _add_image(db, t1, "u3", "2026-10-22")
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    days = [d["date"] for d in rep["daily"]]
    assert days == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22",
                    "2026-10-23", "2026-10-24", "2026-10-25"], days
    got = {d["date"]: d["count"] for d in rep["daily"]}
    assert got["2026-10-19"] == 2 and got["2026-10-21"] == 0
    assert got["2026-10-22"] == 1
    assert rep["daily_max"] == 2


def test_task_rank_is_ordered_and_carries_verdicts(tmp_path):
    """★ 排行必须按采集量降序。

    ⚠ 刻意让**插入顺序 ≠ 期望顺序**（1 / 3 / 2 张）：插入顺序正好等于
    期望顺序时，「不排序」的缺陷会假绿（本轮变异 M9 就是这么漏的）。
    """
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")          # 1 张
    t2 = _add_task(db, "人像")          # 3 张
    t3 = _add_task(db, "建筑")          # 2 张
    _add_image(db, t1, "a0", "2026-10-19")
    for i in range(3):
        _add_image(db, t2, f"b{i}", "2026-10-19")
    for i in range(2):
        _add_image(db, t3, f"c{i}", "2026-10-19")
    i_b = db.query("SELECT id FROM images WHERE task_id=? LIMIT 1", (t2,))[0]["id"]
    _add_review(db, int(i_b), t2, "like", "2026-10-19")
    i_c = db.query("SELECT id FROM images WHERE task_id=? LIMIT 1", (t3,))[0]["id"]
    _add_review(db, int(i_c), t3, "dislike", "2026-10-19")

    rep = collect_period(db, "2026-10-19", "2026-10-25")
    rank = rep["task_rank"]
    assert [r["task_id"] for r in rank] == [t2, t3, t1], rank
    assert [r["images"] for r in rank] == [3, 2, 1], rank
    assert rank[0]["name"] == "人像"
    assert rank[1]["liked"] == 0 and rank[1]["disliked"] == 1
    assert rank[1]["pass_rate"] == 0.0
    assert rank[2]["pass_rate"] is None        # 采了但没判


def test_top_dislike_comes_from_learning_stats(tmp_path):
    """★ 复用 learning_stats 口径 —— 另写一套就是「两个数字」。

    判据落到**真调用**上：只判 import 出现过是空壳（import 那行就带着它）。
    """
    db = _mk_db(tmp_path)
    db.execute("INSERT INTO preferences(task_id,label,weight) VALUES (0,?,?)",
               ("模糊", -2.5))
    db.execute("INSERT INTO preferences(task_id,label,weight) VALUES (0,?,?)",
               ("清晰", 1.5))
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    labels = [t["label"] for t in rep["top_dislike"]]
    assert labels == ["模糊"], rep["top_dislike"]

    src = _py_func_src("collect_period")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_ls_collect"]
    assert calls, "collect_period 没真调 learning_stats.collect（另写口径 = 两个数字）"


def test_empty_db_still_has_every_key(tmp_path):
    db = _mk_db(tmp_path)
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    for k in ("collected", "favorites", "quarantined", "decisions", "reviewed",
              "pos_count", "neg_count", "pass_rate", "daily", "daily_max",
              "top_dislike", "task_rank", "has_data"):
        assert k in rep, k
    assert rep["has_data"] is False
    assert rep["pass_rate"] is None


def test_swapped_range_is_tolerated(tmp_path):
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    _add_image(db, t1, "u1", "2026-10-19")
    rep = collect_period(db, "2026-10-25", "2026-10-19")
    assert rep["collected"] == 1


# ==================== 导出 ====================

def test_markdown_has_all_four_blocks_and_the_global_caveat(tmp_path):
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    i1 = _add_image(db, t1, "u1", "2026-10-19")
    _add_review(db, i1, t1, "like", "2026-10-19")
    db.execute("INSERT INTO preferences(task_id,label,weight) VALUES (0,?,?)",
               ("模糊", -2.5))
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    md = to_markdown(rep, PERIOD_WEEK)
    assert "# 采集周报" in md
    assert "采集张数" in md and "通过率" in md
    assert "判定分布" in md and "每日采集" in md
    assert "不喜欢 Top 词" in md and "任务排行" in md
    assert "风景" in md
    # ★ top 词是全局口径，必须写明 —— 不写就是误导
    assert "全局" in md, "Markdown 没写明 top 词是全局口径（会读成「本期新增」）"


def test_csv_has_bom_and_escapes_commas_and_quotes(tmp_path):
    db = _mk_db(tmp_path)
    t1 = _add_task(db, '风景, "高清"')
    _add_image(db, t1, "u1", "2026-10-19")
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    csv = to_csv(rep)
    assert csv.startswith("\ufeff"), "CSV 没有 BOM（Excel 打开是乱码）"
    assert csv.splitlines()[0].endswith("通过率")
    # 任务名里的逗号和引号都必须转义，否则 Excel 会把一行切成两列
    assert '"风景, ""高清"""' in csv, csv


def test_markdown_of_empty_period_still_renders():
    md = to_markdown(collect_period(_FakeDb(), "2026-10-19", "2026-10-25"))
    assert "本期还没有判定" in md


class _FakeDb:
    """query 全炸的库（老库缺表 / 桩）—— 报告不许崩。"""

    def query(self, *a, **k):
        raise RuntimeError("boom")


def test_broken_db_does_not_crash_the_report():
    rep = collect_period(_FakeDb(), "2026-10-19", "2026-10-25")
    assert rep["collected"] == 0
    assert rep["pass_rate"] is None
    assert isinstance(to_markdown(rep), str)


def test_pass_rate_text_clamps_and_formats():
    assert pass_rate_text(0.5) == "50.0%"
    assert pass_rate_text(0.0) == "0.0%"
    assert pass_rate_text(1.0) == "100.0%"
    assert pass_rate_text(-1) == "0.0%"        # 越界钳制
    assert pass_rate_text(9) == "100.0%"


def test_headline_is_one_chinese_line(tmp_path):
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    _add_image(db, t1, "u1", "2026-10-19")
    rep = collect_period(db, "2026-10-19", "2026-10-25")
    h = headline(rep)
    assert "采集 1 张" in h and "本期还没有判定" in h, h


# ==================== 桌面端对话框（真建） ====================

def test_desktop_dialog_drives_period_switch_and_paging(tmp_path):
    from PySide6.QtWidgets import QApplication

    from media_resource_manager.gui.report_dialog import ReportDialog
    app = QApplication.instance() or QApplication([])
    assert app is not None

    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    # ⚠ 2026-11-09 修：原来写死 "2026-10-19" —— ReportDialog 默认打开
    # 「本期」，跑测日期一旦越过那一周，表里就是空的（rowCount 0）必红，
    # 和被测代码无关。改成今天，永远落在本期里。
    _add_image(db, t1, "u1", _dt.date.today().isoformat())

    dlg = ReportDialog(None, db)
    try:
        rep0 = dlg.report()
        assert rep0["kind"] == PERIOD_WEEK
        # 当期「下一期」必须是禁用的（未来没有数据）
        assert dlg.btn_next.isEnabled() is False

        # 切到月报：区间变成整月
        dlg.cmb_kind.setCurrentIndex(1)
        rep1 = dlg.report()
        assert rep1["kind"] == PERIOD_MONTH
        assert rep1["start"].endswith("-01"), rep1

        # 翻到上一期后「下一期」可用，再点回来又回到当期
        dlg.btn_prev.click()
        assert dlg.btn_next.isEnabled() is True
        dlg.btn_next.click()
        assert dlg.btn_next.isEnabled() is False

        assert "采集" in dlg.markdown_text()
        assert dlg.csv_text().startswith("\ufeff")
        assert dlg.tbl.rowCount() == 1
        assert dlg.tbl.item(0, 0).text() == "风景"
    finally:
        dlg.close()


def test_export_writes_utf8_not_the_system_codepage():
    """★ 导出必须显式 utf-8：Windows 默认编码是 GBK，中文任务名会变乱码。

    判据落在**写文件那一行**：这是极少数只能靠源码判的地方（真跑要弹
    QFileDialog），但仍要判到"open(..., encoding=)"这个具体参数上，
    不能只判函数里出现过 "utf-8" 字样（注释里也可能有）。
    """
    src = (ROOT / "media_resource_manager" / "gui" / "report_dialog.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_export":
            fn = node
    assert fn is not None, "report_dialog.py 里没有 _export"
    hits = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "open":
            hits.append({kw.arg: getattr(kw.value, "value", None)
                         for kw in n.keywords})
    assert hits, "_export 没有 open()"
    assert any(h.get("encoding") == "utf-8" for h in hits), \
        f"导出没有显式 encoding='utf-8'（Windows 上会按 GBK 写 ⇒ 乱码）：{hits}"


def test_main_window_menu_and_opener_are_wired():
    mw = (ROOT / "media_resource_manager" / "gui" / "main_window.py").read_text(
        encoding="utf-8")
    assert 'self.act_report.triggered.connect(self.show_collection_report)' in mw
    assert 'def show_collection_report(self):' in mw


# ==================== Web ====================

def test_report_endpoint_is_routed():
    """★ 判「整条分支在不在」，不是判字面量出现过（/api/report_x 也含它）。"""
    src = WEBUI_PY.read_text(encoding="utf-8")
    assert 'if path == "/api/report":' in src, "/api/report 没接路由（写了函数不接 = 白写）"
    assert "self._api_report(u)" in src


def test_web_report_serves_the_same_pure_layer():
    """★ Web 不许自己写 SQL —— 必须调 reporting.collect_period。"""
    src = WEBUI_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_api_report":
            fn = node
    assert fn is not None, "webui.py 里没有 _api_report"
    # ⚠ 两种调用形态都要认：`reporting.collect_period(...)`（Attribute）
    #   与 `from .reporting import collect_period` 之后的裸名调用（Name）。
    #   只认一种 = 换种写法就假红（R84「判据不许只认一种形态」的同型）。
    calls = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            calls.add(f.attr)
        elif isinstance(f, ast.Name):
            calls.add(f.id)
    assert "collect_period" in calls, "_api_report 没调 reporting.collect_period"
    assert "period_range" in calls
    assert "to_markdown" in calls and "to_csv" in calls
    # 反向：不许出现裸 SELECT（自己写 SQL 就是第二套口径）
    seg = ast.get_source_segment(src, fn) or ""
    assert "SELECT COUNT(*)" not in seg.upper(), "_api_report 自己写了 SQL"


def test_web_report_returns_one_payload(tmp_path):
    """2026-10-27：原来硬编码 2026-10-19 造数据 —— 这测试有**时间炸弹**：
    跨到下一周（今天 10-27）当期报表里就查不到那张图 ⇒ collected=0 假红。
    改为按"今天"造数据，任何日期跑都落在当期。"""
    import datetime
    from urllib.parse import urlparse

    from media_resource_manager import webui

    _today = datetime.date.today().isoformat()
    db = _mk_db(tmp_path)
    t1 = _add_task(db, "风景")
    i1 = _add_image(db, t1, "u1", _today)
    _add_review(db, i1, t1, "like", _today)

    class _H(webui._Handler):
        def __init__(self, db):
            self._db = db
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)

        @property
        def db(self):
            return self._db

    h = _H(db)
    out = h._api_report(urlparse("/api/report?kind=week&offset=0"))
    assert out["ok"] is True, out
    assert out["report"]["collected"] == 1
    assert out["report"]["kind"] == PERIOD_WEEK
    assert "采集" in out["headline"]
    assert "# 采集周报" in out["markdown"]
    assert out["csv"].startswith("\ufeff")
    # 期数往前翻：这一期的数据不该串到上一期
    out2 = h._api_report(urlparse("/api/report?kind=week&offset=1"))
    assert out2["report"]["start"] != out["report"]["start"]
    assert out2["report"]["collected"] == 0
    # 月报要能切
    out3 = h._api_report(urlparse("/api/report?kind=month&offset=0"))
    assert out3["report"]["kind"] == PERIOD_MONTH
    # 坏参数不许 500
    out4 = h._api_report(urlparse("/api/report?kind=zzz&offset=abc"))
    assert out4["ok"] is True and out4["report"]["kind"] == PERIOD_WEEK
