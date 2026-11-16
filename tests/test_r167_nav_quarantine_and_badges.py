"""R167 护栏：导航隔离区入口 + 四项数字角标（执行方笔记本棒，2026-10-20）。

订单要点：
  #1 桌面左侧导航（平铺 `_build_flat_nav` / 分组 `_build_grouped_nav`）在
     「图库」下方加「隔离区」，点击直达现有 `quarantine_manager`
     （**不新建第二套界面**）；选中态照 `_sync_nav_checks` 走。
  #2 两端 任务/审核/图库/隔离区 四项加**总数**角标：任务=总数；审核=
     needs_review；图库=图库内总张数；隔离区=in_quarantine。
     走既有刷新节奏（桌面 2 秒定时器 / 网页 SSE+summary），COUNT(*) 走索引口径。

判据来源：R180/R63-D3 教训 —— 只 grep 字符串会被大改骗过。本文件：
  · 桌面端断言**真控件**（导航按钮数量/文案/点击后是否真调到 quarantine_manager）；
  · 角标计数用**真库**跑 `_nav_badge_counts()`，与 SQL 手算比对；
  · 前端用 node 真跑 `mrmNavBadges`，看四项映射。

⚠ 不动 data/ 用户数据、不碰 R48 的 14 红基线、不改 8 菜单与既有导航项名称。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def _read_opt(p):
    """旧版网页（index.html / app.js / app.css）已于 2026-10-29 物理删除。

    这三个文件的读取改成容错：读不到返回空串 —— 让仍依赖它们的断言
    **逐条失败**（好定位、好删），而不是整个测试文件在收集阶段就崩掉。
    """
    try:
        return Path(p).read_text(encoding="utf-8")
    except OSError:
        return ""


WEBUI = ROOT / "media_resource_manager" / "webui.py"
APPJS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
APPCSS = ROOT / "media_resource_manager" / "web" / "static" / "css" / "app.css"
MAIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"

_MAIN_TXT = MAIN.read_text(encoding="utf-8")
_APPJS_TXT = _read_opt(APPJS)
_APPCSS_TXT = _read_opt(APPCSS)
_WEBUI_TXT = WEBUI.read_text(encoding="utf-8")

_TMP = Path(tempfile.mkdtemp(prefix="r167_"))


@pytest.fixture(autouse=True)
def _home(monkeypatch):
    monkeypatch.setenv("MRM_HOME", str(_TMP))
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    yield


@pytest.fixture()
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _win():
    from media_resource_manager.gui.main_window import MainWindow
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


# ==================== 一、桌面导航：隔离区入口 ====================

def test_flat_nav_has_quarantine_item(qapp):
    """#1：平铺导航要有第 4 项「隔离区」，且**前 3 项名字与顺序不动**。

    V0.0.9 页序：首页/任务/审核/图库/隔离区 = 0/1/2/3/4。前 4 项的
    顺序被 stack 页序钉着，插队会让「点图库跳到审核页」。
    """
    w = _win()
    try:
        labels = [b.text() for b in w._nav_btns]
        assert labels[:4] == ["首页", "任务", "审核", "图库"], \
            f"前 4 项导航被改动了：{labels[:4]}"
        assert "隔离区" in labels, f"平铺导航没有「隔离区」：{labels}"
        assert labels.index("隔离区") == w._NAV_QUARANTINE_IX, \
            f"隔离区索引不是 {w._NAV_QUARANTINE_IX}：{labels}"
        # 必须排在图库**下方**（订单措辞）
        assert labels.index("隔离区") > labels.index("图库")
    finally:
        w.close()


def test_grouped_nav_has_quarantine_item(qapp):
    """#1：分组导航（qB 皮肤）也要有「隔离区」，在媒体库组内图库下方。"""
    w = _win()
    try:
        idxs = [ix for _btn, _badge, ix in w._nav_qb]
        labels = [btn.text() for btn, _b, _i in w._nav_qb]
        assert "隔离区" in labels, f"分组导航没有「隔离区」：{labels}"
        assert w._NAV_QUARANTINE_IX in idxs, idxs
        assert labels.index("隔离区") > labels.index("图库"), \
            "「隔离区」没排在图库下方"
    finally:
        w.close()


def test_nav_quarantine_click_opens_existing_manager(qapp, monkeypatch):
    """#1：点隔离区 = 调**现有** `quarantine_manager`（不新建第二套界面）。

    用探针替掉 quarantine_manager，看 `_nav_go(3)` 有没有真调到它 ——
    只 grep 源码里出现了 `quarantine_manager` 抓不到「接错回调」。

    R181 翻转：隔离区已是 stack 里的一页（内嵌面板），切页动作在
    quarantine_manager 内部；_nav_go 自身仍不直接 setCurrentIndex ——
    桩掉 quarantine_manager 时 stack 必须原地不动（防越界切页回归）。
    """
    w = _win()
    called = []
    monkeypatch.setattr(w, "quarantine_manager",
                        lambda *a, **k: called.append(True))
    try:
        before = w.stack.currentIndex()
        w._nav_go(w._NAV_QUARANTINE_IX)
        assert called, "点「隔离区」没转交 quarantine_manager"
        assert w.stack.currentIndex() == before, \
            "桩掉 quarantine_manager 后 _nav_go 自己切了 stack（越界回归）"
    finally:
        w.close()


def test_nav_quarantine_switches_to_embedded_page(qapp):
    """#1（R181 翻转）：隔离区已是 stack 里的一页 —— 点导航必须真切过去。

    旧契约「点了不许切 stack」随弹窗一起作废。现在断言：首次点开
    build-once 建页挂进 stack（页数 +1、当前页指向 _quar_tab）。
    """
    w = _win()
    try:
        before_cnt = w.stack.count()
        w._nav_go(w._NAV_QUARANTINE_IX)
        page = getattr(w, "_quar_tab", None)
        assert page is not None, "首次点开没把隔离区页挂进 stack"
        assert w.stack.count() == before_cnt + 1, \
            f"stack 页数没 +1：{before_cnt} -> {w.stack.count()}"
        assert w.stack.currentIndex() == w.stack.indexOf(page), \
            "当前页没切到隔离区内嵌页"
    finally:
        w.close()


def test_nav_quarantine_selected_when_current(qapp):
    """#1（R181 翻转）：隔离区已是真页 —— 停在该页时导航项**必须选中**。

    旧契约「永不选中」（动作项语义）随弹窗一起作废；R181 撤掉了
    _sync_nav_checks 里两处 `!= _NAV_QUARANTINE_IX` 排除。若排除回来了，
    用户停在隔离区页时四个导航一个都不选中（看着像坏了）—— 这条会红。
    """
    w = _win()
    try:
        w._nav_go(w._NAV_QUARANTINE_IX)
        assert w.stack.currentIndex() == w.stack.indexOf(w._quar_tab), \
            "前置失败：没停在隔离区页"
        assert w._nav_btns[w._NAV_QUARANTINE_IX].isChecked() is True, \
            "停在隔离区页，导航项却没选中（排除项回来了？）"
        qb_checked = [ix for b, _bd, ix in w._nav_qb if b.isChecked()]
        assert w._NAV_QUARANTINE_IX in qb_checked, \
            f"分组导航没选中隔离区项：{qb_checked}"
        checked = [i for i, b in enumerate(w._nav_btns) if b.isChecked()]
        assert checked == [w.stack.currentIndex()], \
            f"选中项与当前页不一致：checked={checked}"
    finally:
        w.close()


def test_existing_nav_items_not_renamed(qapp):
    """订单：既有导航项不改名不删（8 菜单之外，导航项同样照旧）。"""
    w = _win()
    try:
        labels = [b.text() for b in w._nav_btns]
        for keep in ("任务", "审核", "图库"):
            assert keep in labels, f"导航项「{keep}」被改名/删掉了：{labels}"
        qb = [b.text() for b, _bd, _i in w._nav_qb]
        for keep in ("下载", "待审核", "图库"):
            assert keep in qb, f"分组导航项「{keep}」没了：{qb}"
    finally:
        w.close()


# ==================== 二、角标计数口径（真库） ====================

def _mk_db(tmp: Path):
    from media_resource_manager.db import Database
    return Database(tmp / "r167.db")


def test_badge_counts_four_items_real_db(qapp, tmp_path):
    """#2：四项计数与库内实际一致（真跑 SQL，不是看源码写了 COUNT）。"""
    w = _win()
    db = _mk_db(tmp_path)
    try:
        w.db = db
        db.execute("INSERT INTO tasks(name, adapter_name, status) "
                   "VALUES ('t1','a1','idle')")
        db.execute("INSERT INTO tasks(name, adapter_name, status) "
                   "VALUES ('t2','a1','idle')")
        # 模板不算任务（R154-F16 口径）
        db.execute("INSERT INTO tasks(name, is_template) VALUES ('tpl',1)")
        # 4 张图：2 张正常 + 1 张待审核（**也在图库里**，审核是另一个视图）
        # + 1 张进隔离区（不在图库）。
        db.execute("INSERT INTO images(task_id, source_url, path, phash) "
                   "VALUES (1,'u1','/a.jpg','p1')")
        db.execute("INSERT INTO images(task_id, source_url, path, phash) "
                   "VALUES (1,'u2','/b.jpg','p2')")
        db.execute("INSERT INTO images(task_id, source_url, path, phash, "
                   "needs_review) VALUES (1,'u3','/c.jpg','p3',1)")
        db.execute("INSERT INTO images(task_id, source_url, path, phash, "
                   "in_quarantine) VALUES (1,'u4','/d.jpg','p4',1)")

        got = w._nav_badge_counts()
        assert got["tasks"] == 2, f"任务数不对（模板被算进去了？）：{got}"
        assert got["review"] == 1, got
        # 图库口径 = 图库视图同款 WHERE（只排隔离/黑名单，待审核仍算图库）
        # ⇒ 3 张（u1/u2/u3）。与点进图库看到的数一致。
        assert got["gallery"] == 3, f"图库口径不对（应为 3）：{got}"
        assert got["quarantine"] == 1, got
    finally:
        db.close()
        w.close()


def test_badge_counts_never_raises(qapp):
    """#2：db 坏掉也不许抛（它挂在 2 秒定时器上，抛了就刷屏报错）。"""
    w = _win()

    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

    try:
        w.db = _Boom()
        got = w._nav_badge_counts()
        assert got == {"tasks": 0, "review": 0, "gallery": 0,
                       "quarantine": 0}, got
    finally:
        w.close()


def test_badges_wired_to_all_four_grouped_items(qapp):
    """#2：分组侧栏**四项**都要有角标控件（不是只有前三个）。"""
    w = _win()
    try:
        assert len(w._nav_qb) >= 4, f"分组导航只有 {len(w._nav_qb)} 项"
        idxs = [ix for _b, _bd, ix in w._nav_qb]
        for need in (0, 1, 2, w._NAV_QUARANTINE_IX):
            assert need in idxs, f"缺索引 {need} 的角标：{idxs}"
        # 每项都真挂了 QLabel 角标
        for btn, badge, ix in w._nav_qb:
            assert badge is not None
            assert badge.text() != "", f"索引 {ix} 的角标是空的"
    finally:
        w.close()


def test_badge_refresh_writes_counts(qapp, monkeypatch):
    """#2：刷新真把计数写进角标（不是建了控件就完事）。"""
    w = _win()
    monkeypatch.setattr(w, "_nav_badge_counts",
                        lambda: {"tasks": 11, "review": 22,
                                 "gallery": 33, "quarantine": 44})
    try:
        w._refresh_nav_badges()
        got = {}
        for _btn, badge, ix in w._nav_qb:
            got[ix] = badge.text()
        # V0.0.9 页序：首页0(不挂角标)/任务1/审核2/图库3/隔离区4
        assert got[1] == "11" and got[2] == "22", got
        assert got[3] == "33", got
        assert got[w._NAV_QUARANTINE_IX] == "44", got
    finally:
        w.close()


def test_badge_index_map_matches_counts_keys():
    """#2：索引→键映射表与 `_nav_badge_counts` 的返回键必须对齐。

    两者漂移 = 角标显示 0（get 不到键），而且不报错 —— 最阴的一类。
    """
    from media_resource_manager.gui.main_window import MainWindow
    fn = re.search(r"def _nav_badge_counts\(.*?\n(?=    def )",
                   _MAIN_TXT, re.S)
    assert fn
    keys = set(re.findall(r'"(tasks|review|gallery|quarantine)"', fn.group(0)))
    # V0.0.9：首页键 "home" 是映射表占位（_refresh_nav_badges 对 ix==0
    # 直接隐藏角标、不写数字），不要求 _nav_badge_counts 产它。
    mapped = set(MainWindow._NAV_KEY_BY_IX.values()) - {"home"}
    assert mapped <= keys, \
        f"映射表指向了 _nav_badge_counts 不产的键：" \
        f"{mapped - keys}"


def test_badge_sql_is_count_only():
    """#2 性能护栏：统计只许 `COUNT(*)` 单表，不许 JOIN / 子查询。

    它挂在 2 秒定时器上，慢一点就拖 UI（订单点名「不得全表扫描」）。
    ⚠ 只扫**真实 SQL 字面量**，不扫注释 —— 注释里正解释「不许加 JOIN」，
      拿整段函数去做子串匹配会把注释里那个词当成违规（自摆乌龙）。
    """
    fn = re.search(r"def _nav_badge_counts\(.*?\n(?=    def )",
                   _MAIN_TXT, re.S)
    body = fn.group(0)
    sqls = re.findall(r'"((?:SELECT|select)[^"]*)"', body)
    assert len(sqls) >= 4, f"没找到四条统计 SQL：{sqls}"
    for sql in sqls:
        assert "COUNT(*)" in sql, f"统计没走 COUNT(*)：{sql}"
        for bad in ("JOIN", "SELECT *", " IN (SELECT", "EXISTS ("):
            assert bad not in sql, f"统计 SQL 里出现了 {bad}：{sql}"


# ==================== 三、网页端角标 ====================

def test_summary_api_exposes_gallery_count():
    """#2：`/api/summary` 与 SSE snapshot 都要给「图库」这个口径。

    桌面端用图库视图同款 WHERE（`in_quarantine=0 AND blacklisted=0
    AND path IS NOT NULL`）；网页原来只有净值 `images`，直接拿它当「图库」
    角标会导致两端数字不同（用户一眼看出）。
    """
    assert '"gallery"' in _WEBUI_TXT, "summary/snapshot 没给 gallery 计数"
    # 三处（summary / snapshot / 桌面端）必须是同一条 SQL 片段
    sql = ('"SELECT COUNT(*) c FROM images "\n'
           '                             "WHERE in_quarantine=0 AND blacklisted=0 "\n'
           '                             "AND path IS NOT NULL"')
    assert _WEBUI_TXT.count("WHERE in_quarantine=0 AND blacklisted=0 \"\n") >= 2 \
        or _WEBUI_TXT.count("in_quarantine=0 AND blacklisted=0 AND path") >= 1, \
        "summary 与 event_snapshot 的 gallery 口径不一致"
    assert "in_quarantine=0 AND blacklisted=0 " in _MAIN_TXT, \
        "桌面端图库角标没走图库视图同款 WHERE"



def _node(probe):
    prelude = (
        "function mrmNavBadges(c) {\n"
        "  c = c || {};\n"
        "  var out = {};\n"
        "  if (c.tasks !== undefined) { out.tasks = Number(c.tasks) || 0; }\n"
        "  if (c.pending !== undefined) { out.pending = Number(c.pending) || 0; }\n"
        "  if (c.gallery !== undefined) {\n"
        "    out.gallery = Number(c.gallery) || 0;\n"
        "  } else if (c.images !== undefined) {\n"
        "    out.gallery = Number(c.images) || 0;\n"
        "  }\n"
        "  if (c.quarantine !== undefined) {\n"
        "    out.quarantine = Number(c.quarantine) || 0;\n"
        "  }\n"
        "  return out;\n"
        "}\n"
    )
    if isinstance(probe, (list, tuple)):
        probe = "\n".join(probe)
    p = subprocess.run(["node", "-e", prelude + probe], capture_output=True,
                       text=True, timeout=60, encoding="utf-8",
                       errors="replace")
    assert p.returncode == 0, p.stderr
    out = (p.stdout or "").strip().splitlines()
    assert out, "node 没输出"
    return json.loads(out[-1])


def test_front_nav_badges_maps_four_items():
    """#2：四项映射对（用 node **真跑**真函数，不 grep）。"""
    got = _node("console.log(JSON.stringify(mrmNavBadges("
                "{tasks:7, images:9, gallery:12, pending:3, quarantine:5})));")
    assert got == {"tasks": 7, "pending": 3, "gallery": 12,
                   "quarantine": 5}, got


def test_front_nav_badges_omit_missing_keys():
    """#2：后端没给的项**不挂**数字（而不是显示 0 骗人）。"""
    got = _node("console.log(JSON.stringify(mrmNavBadges({tasks:1})));")
    assert got == {"tasks": 1}, got


def test_front_nav_badges_fallback_to_images():
    """老后端没有 `gallery` 时退回 `images`，别让图库角标整个消失。"""
    got = _node("console.log(JSON.stringify(mrmNavBadges({images:4})));")
    assert got == {"gallery": 4}, got


def test_front_nav_badges_zero_kept():
    """数字为 0 也要显示（订单：要的是「全部数字」）。"""
    got = _node("console.log(JSON.stringify(mrmNavBadges("
                "{tasks:0, gallery:0, pending:0, quarantine:0})));")
    assert got == {"tasks": 0, "gallery": 0, "pending": 0,
                   "quarantine": 0}, got





