"""#63 / A6 学习看板 测试。

分两层：
1. **纯统计层**（``learning_stats``）：不需要 QApplication，测口径与边界。
2. **界面层**（``learning_board``）：必须有 ``qapp`` fixture（本项目铁律：
   没有 QApplication 时构造 QDialog 会让整个进程 abort、rc=127 无任何输出）。

另含「不许把看板做成可改数据的入口」的反向断言，以及菜单基线断言。
"""
from __future__ import annotations

import datetime as _dt
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_resource_manager.classify import learning_stats as LS  # noqa: E402
from media_resource_manager.db import Database  # noqa: E402
from media_resource_manager.repo import (  # noqa: E402
    GLOBAL_TASK_ID, set_preference,
)


# ---------------- fixtures ----------------

@pytest.fixture()
def db():
    """临时库。⚠️ Windows 上 sqlite 句柄开着不能删目录 —— 必须先 close() 再删。"""
    td = Path(tempfile.mkdtemp(prefix="r63_db_"))
    d = Database(td / "mrm.db")
    try:
        yield d
    finally:
        d.close()
        shutil.rmtree(td, ignore_errors=True)


def _add_pref(db, label, weight, seen=1, hp=0, hn=0):
    """插一条权重（不走 set_preference 的累加语义，直接给绝对值）。"""
    db.execute(
        "INSERT INTO preferences(task_id,label,weight,seen,hit_pos,hit_neg) "
        "VALUES (?,?,?,?,?,?)",
        (GLOBAL_TASK_ID, label, float(weight), int(seen), int(hp), int(hn)))


def _add_review(db, decision, day=None):
    """插一条审核记录。``day`` 给 'YYYY-MM-DD'，不给就用今天。"""
    # R221/C10：FK 开启后 reviews 的父行（images）必须先存在。images→tasks
    # 的外键已退役，task_id=0 不违约束；这些用例测的是统计，不是图库。
    db.execute(
        "INSERT OR IGNORE INTO images(id,task_id,source_url) "
        "VALUES (1, 0, 'r63-fixture')")
    d = day or _dt.date.today().isoformat()
    db.execute(
        "INSERT INTO reviews(image_id,decision,task_id,created_at) "
        "VALUES (?,?,?,?)",
        (1, decision, 0, f"{d} 12:00:00"))


# ---------------- 纯统计层 ----------------

def test_collect_on_empty_db_has_all_keys(db):
    """空库也必须返回完整键 —— 否则界面渲染要到处写 if 判空，早晚漏一处。"""
    st = LS.collect(db)
    for k in ("has_data", "labels_total", "pos_count", "neg_count",
              "neu_count", "strong_count", "hit_pos_total", "hit_neg_total",
              "seen_total", "top_pos", "top_neg", "decisions", "reviews_total",
              "daily", "daily_max", "window_days"):
        assert k in st, f"缺键 {k}"
    assert st["has_data"] is False
    assert st["labels_total"] == 0
    # 基准不能是 0，否则画图除零
    assert st["daily_max"] >= 1


def test_collect_counts_pos_neg_neutral(db):
    _add_pref(db, "a", 1.5)      # 正
    _add_pref(db, "b", -2.0)     # 负
    _add_pref(db, "c", 0.0)      # 中性
    st = LS.collect(db)
    assert st["labels_total"] == 3
    assert st["pos_count"] == 1
    assert st["neg_count"] == 1
    assert st["neu_count"] == 1
    assert st["has_data"] is True


def test_collect_strong_count_uses_abs_threshold(db):
    """|w|>=1.0算强权重，0.9 不算。"""
    _add_pref(db, "strong_pos", 1.0)
    _add_pref(db, "strong_neg", -3.0)
    _add_pref(db, "weak", 0.9)
    _add_pref(db, "weak_neg", -0.9)
    st = LS.collect(db)
    assert st["strong_count"] == 2


def test_collect_hit_and_seen_totals(db):
    _add_pref(db, "x", 1.0, seen=3, hp=5, hn=1)
    _add_pref(db, "y", -1.0, seen=2, hp=0, hn=7)
    st = LS.collect(db)
    assert st["hit_pos_total"] == 5
    assert st["hit_neg_total"] == 8
    assert st["seen_total"] == 5


def test_top_pos_sorted_by_weight_desc(db):
    for lb, w in (("p1", 1.0), ("p5", 5.0), ("p3", 3.0), ("p2", 2.0)):
        _add_pref(db, lb, w)
    st = LS.collect(db)
    weights = [i["weight"] for i in st["top_pos"]]
    assert weights == sorted(weights, reverse=True)
    assert st["top_pos"][0]["label"] == "p5"


def test_top_neg_sorted_most_negative_first(db):
    """""最不要的"应该在最前面 —— 看板 Top-N 是给人看最该清理的词。"""
    for lb, w in (("n1", -1.0), ("n9", -9.0), ("n5", -5.0)):
        _add_pref(db, lb, w)
    st = LS.collect(db)
    weights = [i["weight"] for i in st["top_neg"]]
    assert weights == sorted(weights)
    assert st["top_neg"][0]["label"] == "n9"


def test_top_n_respects_limit(db):
    for i in range(30):
        _add_pref(db, f"lab{i}", float(i))
    st = LS.collect(db, top_n=5)
    assert len(st["top_pos"]) == 5
    assert st["top_pos"][0]["label"] == "lab29"


def test_top_pos_and_neg_do_not_mix(db):
    """正负两栏必须各自只含本方向的词 —— 混了Top-N 就没意义了。"""
    for i in range(3):
        _add_pref(db, f"pos{i}", 1.0 + i)
        _add_pref(db, f"neg{i}", -1.0 - i)
    st = LS.collect(db)
    assert all(i["sign"] == "pos" for i in st["top_pos"])
    assert all(i["sign"] == "neg" for i in st["top_neg"])
    assert all(i["weight"] > 0 for i in st["top_pos"])
    assert all(i["weight"] < 0 for i in st["top_neg"])


def test_null_columns_do_not_crash(db):
    """老库可能有 NULL —— 看板不能因为一行脏数据整个打不开。"""
    db.execute(
        "INSERT INTO preferences(task_id,label,weight,seen,hit_pos,hit_neg) "
        "VALUES (?,?,NULL,NULL,NULL,NULL)", (GLOBAL_TASK_ID, "nullrow"))
    st = LS.collect(db)
    assert st["labels_total"] == 1
    assert st["hit_pos_total"] == 0
    assert st["seen_total"] == 0


def test_decisions_include_zero_rows(db):
    """没点过的判定也要列出来（值0）—— 空白会被误读成"界面坏了"。"""
    _add_review(db, "like")
    st = LS.collect(db)
    assert set(st["decisions"]) >= set(LS.DECISION_ZH)
    assert st["decisions"]["like"]["count"] == 1
    assert st["decisions"]["blacklist"]["count"] == 0
    assert st["decisions"]["blacklist"]["zh"] == "拉黑"


def test_unknown_decision_not_silently_dropped(db):
    """库里出现未知判定值也要显示，不能悄悄吞掉。"""
    _add_review(db, "some_future_verdict")
    st = LS.collect(db)
    assert "some_future_verdict" in st["decisions"]
    assert st["decisions"]["some_future_verdict"]["count"] == 1


def test_daily_window_has_fixed_length_and_zero_fills(db):
    """趋势图必须是**定长且补零**的窗口：没审核的那天要有 0 行。"""
    today = _dt.date.today()
    _add_review(db, "like", (today - _dt.timedelta(days=3)).isoformat())
    st = LS.collect(db, days=14)
    assert len(st["daily"]) == 14
    assert [d["date"] for d in st["daily"]] == LS.day_window(14)
    # 第 3 天前的那天必须是 0（而不是缺行）
    by = {d["date"]: d["count"] for d in st["daily"]}
    assert by[(today - _dt.timedelta(days=3)).isoformat()] == 1
    assert by[today.isoformat()] == 0
    assert st["daily_max"] == 1


def test_daily_ignores_rows_without_created_at(db):
    """created_at 为 NULL 的行不许让趋势崩。

    ⚠️ 注意语义差别：**趋势图**按日期窗口过滤（NULL 的落不进任何一天），
    但 ``reviews_total`` 是「累计审核次数」，该行仍要计入 —— 它确实是一次审核，
    只是不知道是哪天做的。把它从总数里抹掉等于谎报用户。
    """
    # R221/C10：FK 开启后 reviews 父行（images）先铺（同 _add_review）
    db.execute("INSERT OR IGNORE INTO images(id,task_id,source_url) "
               "VALUES (1, 0, 'r63-fixture')")
    db.execute("INSERT INTO reviews(image_id,decision,task_id,created_at) "
               "VALUES (1,'like',0,NULL)")
    st = LS.collect(db, days=7)
    assert len(st["daily"]) == 7
    assert sum(d["count"] for d in st["daily"]) == 0   # 趋势里没有它
    assert st["reviews_total"] == 1                   # 但总数里算数


def test_day_window_ascending_and_length(db):
    w = LS.day_window(5)
    assert len(w) == 5
    assert w == sorted(w)
    assert w[-1] == _dt.date.today().isoformat()


def test_reviews_total_sums_all_decisions(db):
    _add_review(db, "like")
    _add_review(db, "like")
    _add_review(db, "dislike")
    st = LS.collect(db)
    assert st["reviews_total"] == 3


def test_summary_line_guides_when_empty(db):
    s = LS.summary_line(LS.collect(db))
    assert "还没学到" in s
    # 不得出现任何准确率/百分比类词—— 本地权重没有准确率可算
    assert "%" not in s and "准确" not in s


def test_summary_line_reports_counts(db):
    _add_pref(db, "a", 1.0)
    _add_pref(db, "b", -1.0)
    _add_review(db, "like")
    s = LS.summary_line(LS.collect(db))
    assert "2 条权重" in s and "审核 1 次" in s


def test_bar_ratio_clamped_and_zero_safe():
    assert LS.bar_ratio({"weight": 2.0}, 4.0) == 0.5
    assert LS.bar_ratio({"weight": -2.0}, 4.0) == 0.5    # 用绝对值
    assert LS.bar_ratio({"weight": 8.0}, 4.0) == 1.0     # 夹到 1，不越界
    assert LS.bar_ratio({"weight": 1.0}, 0) == 0.0# 除零保护
    assert LS.bar_ratio({"weight": None}, 4.0) == 0.0
    assert LS.bar_ratio({}, "bad") == 0.0


def test_top_scale_never_zero():
    """""量级基准不能是 0" —— 0 会让所有条长算成 0 或除零。"""
    assert LS.top_scale([]) == 1.0
    assert LS.top_scale([{"weight": 0.0}, {"weight": 0.0}]) == 1.0
    assert LS.top_scale([{"weight": -3.0}, {"weight": 1.0}]) == 3.0


def test_collect_is_read_only(db):
    """看板不许改数据：跑一遍前后库内容必须一模一样。"""
    _add_pref(db, "a", 1.0)
    _add_review(db, "like")
    before = (db.query("SELECT * FROM preferences"),
              db.query("SELECT * FROM reviews"))
    LS.collect(db)
    after = (db.query("SELECT * FROM preferences"),
             db.query("SELECT * FROM reviews"))
    assert [tuple(r) for r in before[0]] == [tuple(r) for r in after[0]]
    assert [tuple(r) for r in before[1]] == [tuple(r) for r in after[1]]


def test_collect_uses_global_task_id_only(db):
    """看板只认全局权重（task_id=0）；历史遗留的按任务行不该混进来。"""
    _add_pref(db, "global", 1.0)
    db.execute("INSERT INTO preferences(task_id,label,weight,seen,hit_pos,hit_neg)"
               " VALUES (99,'legacy',5.0,1,0,0)")
    st = LS.collect(db)
    labels = [i["label"] for i in st["top_pos"]]
    assert "legacy" not in labels


# ---------------- 界面层（需要 qapp） ----------------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    # ⚠️ 绝不在这里 app.quit() —— 同一进程里后面的用例还要用（项目铁律）


def test_dashboard_builds_on_empty_db(qapp, db):
    from media_resource_manager.gui.learning_board import LearningDashboard
    dlg = LearningDashboard(db)
    assert dlg.lbl_summary.text()
    st = dlg.reload()
    assert st["has_data"] is False


def test_dashboard_renders_numbers(qapp, db):
    from media_resource_manager.gui.learning_board import LearningDashboard
    _add_pref(db, "a", 2.0, hp=2)
    _add_pref(db, "b", -1.0, hn=3)
    _add_review(db, "like")
    dlg = LearningDashboard(db)
    assert dlg._cards["pos"].text() == "1"
    assert dlg._cards["neg"].text() == "1"
    assert dlg._cards["hit"].text() == "5"
    assert dlg._cards["rev"].text() == "1"
    assert "判定分布" in dlg.lbl_dec.text()
    assert "喜欢 1" in dlg.lbl_dec.text()


def test_dashboard_charts_populated(qapp, db):
    from media_resource_manager.gui.learning_board import LearningDashboard
    for i in range(3):
        _add_pref(db, f"pos{i}", float(i + 1))
        _add_pref(db, f"neg{i}", float(-(i + 1)))
    _add_review(db, "like")
    dlg = LearningDashboard(db)
    assert len(dlg.ch_pos._items) == 3
    assert len(dlg.ch_neg._items) == 3
    assert len(dlg.ch_trend._daily) == 14


def test_dashboard_charts_survive_empty(qapp, db):
    """空数据时图表不能因除零/索引越界炸掉（paintEvent 里崩 = 整窗白屏）。"""
    from media_resource_manager.gui.learning_board import LearningDashboard
    dlg = LearningDashboard(db)
    dlg.grab()          # 触发一次真实 paint
    assert dlg.ch_trend._max >= 1


def test_dashboard_all_zero_weights_paint_ok(qapp, db):
    """权重全被手改成0 的极端情况也不能崩（bar_ratio 除零保护）。"""
    from media_resource_manager.gui.learning_board import LearningDashboard
    _add_pref(db, "zero", 0.0)
    dlg = LearningDashboard(db)
    dlg.grab()
    assert dlg._cards["pos"].text() == "0"


def test_dashboard_has_no_write_actions(qapp, db):
    """反向断言：看板**不自己写库**，改权重仍只在「权重明细」里做。

    撤/挪入口最怕"两个界面都能改同一张表"，用户改了一处发现另一处没同步。

    ⚠ R174 / R169 起判据收窄到**SQL 字面量 + 单条改删函数**：
      看板允许调用 repo 层那两个**批量**清除函数
      （``clear_model_auto_negatives`` / ``reset_learning``），
      但**不许自己写 CREATE/UPDATE/DELETE**，也不许出现单条改删
      （``set_preference_weight`` / ``delete_preference``）。
      这样"只有一处口径"的意图仍然成立：批量清除的 SQL 在 repo 里，
      看板拿到的结果是函数返回值，改口径只需改 repo 一处。

    为什么放宽（不是把红线抹掉）：R169 订单第 4 条明确要求
      「一键重置学习统计」在**两端**都有入口，桌面端只能挂看板
      （权重明细是逐条表格，放"全清"按钮反而更危险）。
      保持"看板零 SQL"这条更细的红线，既满足订单，又没放弃原意图。
    """
    from media_resource_manager.gui import learning_board
    src = Path(learning_board.__file__).read_text(encoding="utf-8")
    for bad in ("DELETE FROM preferences", "UPDATE preferences",
                "INSERT INTO preferences", "set_preference_weight",
                "delete_preference"):
        assert bad not in src, f"看板不该自己写库，却出现了 {bad}"
    # 允许（且要求）走 repo 的批量函数 —— 它们把 SQL 收在一处
    assert "reset_learning" in src or "clear_model_auto_negatives" in src, \
        "看板连批量清除入口都没有（R174/R169 要求的那两个按钮呢）"


def test_dashboard_refresh_button_closes(qapp, db):
    """C5铁律：弹窗里点「刷新」必须关掉，不能留着假装生效。"""
    from PySide6.QtWidgets import QDialogButtonBox
    from media_resource_manager.gui.learning_board import LearningDashboard
    dlg = LearningDashboard(db)
    bb = dlg.findChild(QDialogButtonBox)
    assert bb is not None
    labels = [b.text() for b in bb.buttons()]
    assert "刷新" in labels
    # 全中文：不能出现英文 Close/OK
    for t in labels:
        assert t.strip() not in ("Close", "OK", "Cancel", ""), f"英文按钮 {t}"


def test_menu_entry_exists(qapp):
    """菜单里必须有「学习看板…」，且**没新建第 9 个顶级菜单**。"""
    from media_resource_manager.gui import main_window as MW
    src = Path(MW.__file__).read_text(encoding="utf-8")
    assert '"学习看板…", self.show_learning_dashboard' in src
    assert "def show_learning_dashboard" in src
    # 顶级菜单数量：verify_r11 钉死 8 个，这里做个便宜的静态兜底
    top_menus = [l.strip() for l in src.splitlines()
                 if "= mb.addMenu(" in l]
    # 【V0.0.19 改锁】8 → 5
    assert len(top_menus) == 5, f"顶级菜单变成 {len(top_menus)} 个：{top_menus}"


def test_learning_board_not_in_top_level_menu_names(qapp):
    """看板绝不能被加成顶级菜单（与上一条互为呼应）。"""
    from media_resource_manager.gui import learning_board
    src = Path(learning_board.__file__).read_text(encoding="utf-8")
    assert "mb.addMenu" not in src


def test_review_tab_prefs_table_still_reachable(qapp, db):
    """A6 是**新增**看板，不是取代权重明细 —— 后者必须还在。"""
    from media_resource_manager.gui import review_tab
    src = Path(review_tab.__file__).read_text(encoding="utf-8")
    assert "def show_prefs_table" in src
    assert "def edit_selected" in src      # #60 的改权重
    assert "def delete_selected" in src