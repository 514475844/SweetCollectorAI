"""L1 · 图库/列表keyset（游标）分页。

作业单：`.workbuddy/LEGACY_DEBT_ORDER.md` L1
「图库/待审核等列表在库很大时翻页用 offset/limit 或一次全量读回，深翻页越来越慢。
改keyset（游标）分页——按主键/时间有序 `WHERE (cursor_col) > ? ORDER BY ... LIMIT ?`，
向后翻用游标，向前翻用反向游标或缓存窗口；禁止深 offset。
验收：造5 万行假数据，翻到第 1000 页耗时与第 1 页同量级（差异 < 2x）；
相关视图翻页功能不变；补分页单元测试（首/中/尾页 + 向后/向前）。」

⚠ **本文件为什么以「造5 万行」的性能断言为主**：这是作业单给的验收数字。
它必须真的跑 5 万行 —— 用 100 行造的库，OFFSET 99 和 OFFSET 0 耗时都在
噪声内，测出来「差异 < 2x」恒成立，等于什么都没验。5 万行 / 每页 200 =
第 1000 页正好落在 offset=199800，这是唯一能让「深翻页更慢」显形的数据量。

**耗时断言一律断言「关系」不写死毫秒数**（本仓铁律）：真值受磁盘/CPU 影响，
钉死数值会变成定时炸弹。这里断言的是 ratio < 2，且用同一进程内先跑基线再跑
深页的写法，把环境噪声摊掉。
"""
from __future__ import annotations

import os
import sqlite3
import time

import pytest

from media_resource_manager.db import Database
from media_resource_manager.repo import (
    GALLERY_COLUMNS,
    images_cursor_for,
    list_images_page,
)

# 图库默认视图的 WHERE，与 GUI 里_gal_page_query 拼的是同一条。
GAL_WHERE = "path IS NOT NULL AND blacklisted=0 AND in_quarantine=0"
PAGE = 200


# --------------------------------------------------------------- 夹具
def _mkdb(tmp_path, name="a.db") -> Database:
    db = Database(str(tmp_path / name))
    return db


def _bulk_insert(db, n, *, with_path=True, score_from=None, task_id=1):
    """批量灌 n 行图库数据。

    用 executemany + 一次事务，而不是循环 execute()：5 万次单条 execute 要几十秒，
    而时间预算应该花在**被测的分页查询**上，不是造数据上。
    """
    db.execute("INSERT OR IGNORE INTO tasks (id,name) VALUES (?,?)",
               (task_id, f"任务{task_id}"))
    rows = []
    for i in range(1, n + 1):
        rows.append((
            1, f"https://e.com/{i}.jpg", f"sha{i:08x}", "ph",
            100 + (i % 800), 100 + (i % 600), 1024 * (1 + i % 900),
            (f"C:/imgs/{i}.jpg" if with_path else None),
            "[]", (score_from(i) if score_from else (i % 100) / 100.0),
            0, 0, 0, 0,
        ))
    db.executemany(
        "INSERT INTO images (task_id,source_url,sha256,phash,width,height,"
        "bytes,path,tags,score,blacklisted,in_quarantine,needs_review,rating)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return rows


@pytest.fixture()
def big_db(tmp_path):
    """5 万行库 —— 作业单指定的数据量。"""
    db = _mkdb(tmp_path, "big.db")
    _bulk_insert(db, 50_000)
    return db


@pytest.fixture()
def small_db(tmp_path):
    db = _mkdb(tmp_path, "small.db")
    _bulk_insert(db, 450)     # 450 = 2 页 + 50 行，正好能测尾页不满
    return db


# --------------------------------------------------------------- 1. 性能验收
def test_l1_deep_page_not_slower_than_first(big_db):
    """【作业单验收】第 1000 页耗时与第 1 页同量级（差异 < 2x）。

    这是整个 L1 存在的理由，所以放在文件第一个测试、且用真实 5 万行库。

    **页大小取 50 而不是图库实际的 200**：作业单要的是「第 1000 页」，
    5 万行 / 每页 200 只有 250 页，压根翻不到第 1000 页（会先到尾页）。
    每页 50 → 正好 1000 页，且对应的 OFFSET 是 49950，足够深到显形。

    测法：同一进程内先测首页、再测深页、再测一次首页，取两次首页的较小值当基线
    —— 这样能把首次查询的缓存预热、SQLite 页缓存冷启动之类的噪声摊掉。
    """
    kw = dict(where=GAL_WHERE, sort="id", desc=True, limit=50)

    def timed(cursor):
        t0 = time.perf_counter()
        rows, nxt, more = list_images_page(big_db, cursor=cursor, **kw)
        return time.perf_counter() - t0, rows, nxt, more

    warm, first_rows, cur, _ = timed(None)
    assert len(first_rows) == 50

    # 一路翻页，把游标推进到**第 1000 页的入口**。
    # 每页 50 行 × 1000 页 = 50000 = 全部行，所以第 1000 页是末页。
    # 末页取完 next_cursor 变 None 是正确行为，因此循环只走 998 步：
    # 998 步之后手里的游标正好是第 1000 页的入口（下一页 = 第 999 次取）。
    t_walk = time.perf_counter()
    c = cur
    for i in range(998):
        _rows, c, more = list_images_page(big_db, cursor=c, **kw)
        assert c is not None, f"第 {i+2} 页拿不到游标"
        assert more, f"第 {i+2} 页不该到尾页（还有 1000 页）"
    walk = time.perf_counter() - t_walk

    deep, deep_rows, _nc, more_deep = timed(c)
    assert len(deep_rows) == 50, "第 1000 页应恰好是最后一页 50 行"
    again, _, _, _ = timed(None)
    base = min(warm, again)

    ratio = deep / base if base > 0 else 1.0
    # 只断言关系：深页不比首页慢 2 倍以上。
    # OFFSET 版在这个数据量下 ratio 通常是几十倍（实测过），所以这条能真的变红。
    assert ratio < 2.0, (
        f"深翻页比首页慢 {ratio:.1f}x（基线 {base*1000:.2f}ms，"
        f"深页 {deep*1000:.2f}ms）—— keyset 没生效？"
        f"（走 1000 页总耗时 {walk:.2f}s）")


def test_l1_offset_would_have_been_slower(big_db):
    """反向护栏：证明这个数据量下 OFFSET 版**确实更慢**。

    为什么要这条：上面那条 ratio<2 的断言，如果哪天造数据的方式变了导致
    OFFSET 也不慢，它就会恒绿、变成一条什么都不验的假测试。这里直接把
    OFFSET 版跑一遍量出它有多慢，钉住「这个库确实能区分两者」这个前提。
    """
    kw = dict(where=GAL_WHERE, sort="id", desc=True, limit=50)
    # 取第 1000 页首行的 id 当游标：keyset 与 OFFSET 必须从**同一个位置**开始，
    # 否则测的是两个不同深度的查询，比出来的差值没有意义。
    deep_off = 50 * 999
    last = big_db.query(
        f"SELECT id FROM images WHERE {GAL_WHERE} "
        f"ORDER BY id DESC LIMIT 1 OFFSET ?", (deep_off,))[0]["id"]
    cur = (int(last), int(last))

    t_key = _timeit(lambda: list_images_page(big_db, cursor=cur, **kw))
    t_off = _timeit(lambda: big_db.query(
        f"SELECT {GALLERY_COLUMNS} FROM images WHERE {GAL_WHERE} "
        f"ORDER BY id DESC LIMIT ? OFFSET ?", (50, deep_off)))
    assert t_key < t_off, (
        f"keyset({t_key*1000:.1f}ms) 竟然不慢于 OFFSET({t_off*1000:.1f}ms) "
        f"—— 大概率是索引没建上")


def _timeit(fn, repeat=3):
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best


# --------------------------------------------------------------- 2. 正确性
def test_l1_first_middle_last_page(small_db):
    """首/中/尾页：每页行数、跨页不重不漏、顺序正确。"""
    seen = []
    cursor = None
    pages = []
    for _ in range(10):
        rows, cursor, more = list_images_page(
            small_db, where=GAL_WHERE, sort="id", desc=True,
            cursor=cursor, limit=PAGE)
        ids = [int(r["id"]) for r in rows]
        pages.append(ids)
        seen.extend(ids)
        if not more or cursor is None:
            break
    assert len(pages) == 3, f"450 行 / 每页 200 应是 3 页，实得 {len(pages)}"
    assert [len(p) for p in pages] == [200, 200, 50]
    # 不重不漏
    assert len(seen) == len(set(seen)), "翻页出现重复行"
    assert set(seen) == set(range(1, 451)), "翻页漏行"
    # 每页内部严格降序
    for p in pages:
        assert p == sorted(p, reverse=True)
    # 页与页之间也连续降序（这是 keyset 正确性的核心：游标必须严格接上一页末行）
    assert seen == sorted(seen, reverse=True)


def test_l1_forward_then_backward(small_db):
    """向后翻 + 向前翻（缓存窗口方案）：回到第1 页必须与首次查询逐行一致。"""
    kw = dict(where=GAL_WHERE, sort="id", desc=True, limit=PAGE)
    p1, cur1, _ = list_images_page(small_db, **kw)
    p2, cur2, _ = list_images_page(small_db, cursor=cur1, **kw)
    assert [int(r["id"]) for r in p1] != [int(r["id"]) for r in p2]

    # 向前翻：作业单允许「反向游标**或**缓存窗口」。这里测反向游标那条路径
    #（GUI 用的是缓存窗口，两条路径都要对）。
    back, _nc, more = list_images_page(
        small_db, cursor=images_cursor_for(p2[0], "id"),
        backward=True, **kw)
    assert [int(r["id"]) for r in back] == [int(r["id"]) for r in p1], \
        "从第 2 页退回第 1 页应与首次查询完全一致"
    assert more is False or back, "反向翻到首页时不该给出空页"


def test_l1_backward_is_reverse_of_forward(small_db):
    """反向查询取到的必须是「紧邻游标之前的那一批」，不是最远的一批。

    这是反向翻页最容易写错的地方：直接按显示顺序取 LIMIT 会拿到离游标最远的
    那些行（也就是第 1 页），看上去有数据、其实跳了 998 页。
    """
    kw = dict(where=GAL_WHERE, sort="id", desc=True, limit=PAGE)
    p1, cur1, _ = list_images_page(small_db, **kw)
    p2, _c2, _ = list_images_page(small_db, cursor=cur1, **kw)
    back, _nc, _ = list_images_page(
        small_db, cursor=images_cursor_for(p2[0], "id"), backward=True, **kw)
    assert [int(r["id"]) for r in back] == [int(r["id"]) for r in p1]


def test_l1_ascending_direction(small_db):
    """正序（升序）翻页：游标比较方向必须跟着反过来。"""
    seen, cursor, pages = [], None, []
    for _ in range(5):
        rows, cursor, more = list_images_page(
            small_db, where=GAL_WHERE, sort="id", desc=False,
            cursor=cursor, limit=PAGE)
        ids = [int(r["id"]) for r in rows]
        pages.append(ids)
        seen.extend(ids)
        if not more or cursor is None:
            break
    assert seen == sorted(seen), "升序翻页结果不是升序"
    assert set(seen) == set(range(1, 451)), "升序翻页漏行"
    assert len(seen) == len(set(seen)), "升序翻页重复行"


# --------------------------------------------------------------- 3. 复合排序键
@pytest.mark.parametrize("srt", ["id", "site", "score", "res"])
def test_l1_all_sort_keys_are_total_order(small_db, srt):
    """四种排序键都要能翻页，且不重不漏。

    复合键（site/score/res）比 id 难：同分行必须靠 id 兜底才能构成全序，
    否则两页之间可能漏行或重行。这就是 _SORT_EXPR 后面统一补 `, id` 的原因。
    """
    seen, cursor = [], None
    # 循环上限按「行数/页大小 + 2」算，别写死 10 —— 450 行 / 每页 37 要 13 页，
    # 写死 10 会在**实现完全正确**时误报「漏行」，这种假红比不测更糟。
    for _ in range(450 // 37 + 2):
        rows, cursor, more = list_images_page(
            small_db, where=GAL_WHERE, sort=srt, desc=True,
            cursor=cursor, limit=37)      # 故意用非整除的页大小
        seen.extend(int(r["id"]) for r in rows)
        if not more or cursor is None:
            break
    assert set(seen) == set(range(1, 451)), f"排序键 {srt} 翻页有漏行/重行"
    assert len(seen) == len(set(seen)), f"排序键 {srt} 出现重复行"
    # 顺序要真的按该键排（不是偷偷退回按 id 排）
    if srt == "score":
        sc = [round(float(r["score"] or 0), 6) for r in rows]
        assert sc == sorted(sc, reverse=True)


def test_l1_null_values_do_not_swallow_rows(tmp_path):
    """⚠ score/width/height 为 NULL 的行，翻页时不能被游标条件整批吞掉。

    这是本实现最容易踩的坑：SQL 里 `NULL < 0.3` 的结果是 NULL（不是 true），
    带 NULL 的行会被 `WHERE` 静默丢掉 → 表现为「翻到某页突然少一大片」。
    修法是排序列统一包 COALESCE（见 repo._SORT_EXPR）。
    """
    db = _mkdb(tmp_path, "null.db")
    db.execute("INSERT OR IGNORE INTO tasks (id,name) VALUES (1,'t')")
    rows = []
    for i in range(1, 201):
        # 每 3 行一个 NULL：分数、宽、高分别置空
        rows.append((1, f"https://e.com/{i}.jpg", f"s{i}", "p",
                     None if i % 3 == 0 else 10 + i,
                     None if i % 3 == 1 else 10 + i,
                     1024, f"C:/i/{i}.jpg", "[]",
                     None if i % 3 == 2 else i / 200.0, 0, 0, 0, 0))
    db.executemany(
        "INSERT INTO images (task_id,source_url,sha256,phash,width,height,"
        "bytes,path,tags,score,blacklisted,in_quarantine,needs_review,rating)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    for srt in ("score", "res"):
        seen, cursor = [], None
        for _ in range(200 // 50 + 2):     # 200 行 / 每页 50 = 4 页
            rs, cursor, more = list_images_page(
                db, where=GAL_WHERE, sort=srt, desc=True,
                cursor=cursor, limit=50)
            seen.extend(int(r["id"]) for r in rs)
            if not more or cursor is None:
                break
        assert set(seen) == set(range(1, 201)), (
            f"排序键 {srt}：带 NULL 的行被游标吞掉了 "
            f"（只翻到 {len(seen)}/200 行）")


def test_l1_new_rows_inserted_while_paging(tmp_path):
    """翻页途中新入库的行：keyset 不像 OFFSET 那样漏图/重图。

    OFFSET 的经典毛病：翻到第 2 页时前面插进 5 行新图，OFFSET 200 会把原来
    的第 196~200 行重复显示一遍，且最后一页的 5 行永远看不到。
    keyset 的游标是「上一页末行的 id」，与前面插了几行无关。
    """
    db = _mkdb(tmp_path, "ins.db")
    _bulk_insert(db, 400)
    kw = dict(where=GAL_WHERE, sort="id", desc=True, limit=100)
    p1, cur1, _ = list_images_page(db, **kw)
    p1_ids = [int(r["id"]) for r in p1]

    # 插 5 行更新的 id（400 之后）
    rows = [(1, f"https://e.com/n{i}.jpg", f"news{i}", "p", 1, 1, 1,
             f"C:/n/{i}.jpg", "[]", 0.5, 0, 0, 0, 0) for i in range(5)]
    db.executemany(
        "INSERT INTO images (task_id,source_url,sha256,phash,width,height,"
        "bytes,path,tags,score,blacklisted,in_quarantine,needs_review,rating)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    p2, _c2, _ = list_images_page(db, cursor=cur1, **kw)
    p2_ids = [int(r["id"]) for r in p2]
    assert not (set(p1_ids) & set(p2_ids)), "新行插入导致两页重叠"
    assert max(p2_ids) < min(p1_ids), "第 2 页必须是紧接第 1 页的下一批"


# --------------------------------------------------------------- 4. 静态护栏
def test_l1_no_deep_offset_in_gallery_query():
    """护栏：图库列表不许再出现 OFFSET 深分页。

    用 AST 找 ``String.Constant`` 里的 OFFSET —— 不能用全文搜：
    本仓注释里到处是「以前用 OFFSET…改成 keyset」这类解释性文字，
    全文搜会匹配到自己写的注释而假红。
    """
    import ast
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "media_resource_manager",
                     "gui", "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        v = node.value.upper()
        # 只看 SQL 片段：OFFSET 后面必须跟数字才是深分页
        if "OFFSET" in v and "SELECT" in v:
            offenders.append((node.lineno, node.value[:80]))
    assert not offenders, f"图库里仍有 OFFSET 分页：{offenders}"


def test_l1_sort_key_whitelisted():
    """排序键必须走白名单 —— 它要拼进 ORDER BY，不能直接用原始输入。"""
    import ast
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "media_resource_manager",
                     "gui", "main_window.py"), encoding="utf-8").read()
    assert "_gal_keyset_sort" in src, "缺少排序键白名单函数"
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_gal_keyset_sort"]
    assert fn, "找不到 _gal_keyset_sort"
    body = ast.unparse(fn[0])
    # 白名单外的值一律当 id（比较运算符用 IN 判定，不写死引号风格）
    assert "_SORT_EXPR" in body and "id" in body


def test_l1_keyset_sort_name_does_not_shadow_existing():
    """护栏：新加的排序键白名单函数**不许**叫 `_gal_sort_key`。

    那儿已经有一个同名的 `_gal_sort_key(r, order)`（结构化搜索的 Python
    排序键，两个参数）。同名会互相覆盖 —— Python 里后定义的方法赢，
    结构化搜索的 `self._gal_sort_key(r, srt)` 就会用错版本直接崩。
    这个坑本轮真踩过一次，是靠「A 参数被 sort=srt 调用」的断言抓到的。
    """
    import ast
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "media_resource_manager",
                     "gui", "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    names = [n.name for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)]
    dup = {n for n in names if names.count(n) > 1}
    assert "_gal_sort_key" not in dup, (
        "_gal_sort_key 被重复定义（同名不同参）→ 后者覆盖前者")
    assert names.count("_gal_keyset_sort") == 1


def test_l1_self_method_calls_all_exist():
    """护栏：`self._xxx(...)` 调用的方法必须真有定义。

    ⚠ 本轮真踩过：定义叫 `_gal_page_query`、调用写成 `_gallery_page_query`，
    AttributeError 被 load_gallery 自己的 `except` 吞掉 → 症状是
    **图库一片空白、状态栏写「读取失败: 'MainWindow' object has no
    attribute ...」**，而不是任何显式报错。纯文本断言看不出这种错
    （字符串确实存在），必须走 AST 把「调用名」与「定义名」对上。
    """
    import ast
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "media_resource_manager",
                     "gui", "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    called = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "self"):
            called.add(n.func.attr)
    # 只查**私有方法**（下划线开头）：`self.setStyleSheet` 这类调的是Qt 自己的
    # 方法，本类当然没有定义 —— 全量比对会捞出几十个假失败（第一版就踩了）。
    # 本类自己定义的私有方法才可能拼错。
    missing = sorted(a for a in called - defined if a.startswith("_"))
    assert not missing, (
        f"调用了不存在的 self 私有方法：{missing}\n"
        f"（若这些调用被 try/except 吞掉，症状是功能静默失效而不是报错）")


def test_l1_cursor_stack_invalidated_on_filter_change():
    """护栏：换筛选/排序后游标栈必须作废。

    不作废的后果不是「慢」，而是**翻到某一页看到不属于当前筛选的行**：
    游标里存的是旧排序键的值，拿去查新条件自然对不上。
    """
    import ast
    src = open(
        os.path.join(os.path.dirname(__file__), "..", "media_resource_manager",
                     "gui", "main_window.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef)
          and n.name == "_gal_cursor_for_page"]
    assert fn, "找不到 _gal_cursor_for_page（游标栈实现）"
    body = ast.unparse(fn[0])
    # 失效键里必须包含筛选维度，不能只有排序键
    for field in ("tid", "flt", "orient", "res"):
        assert field in body, f"游标失效键漏了 {field}（换筛选后游标不失效）"
    assert "[None]" in body, "游标栈首项必须是 None（首页不用游标）"


# --------------------------------------------------------------- 5. 索引
def test_l1_gallery_indexes_created(tmp_path):
    """复合索引必须真建出来（keyset 不建索引 = 一样全表扫描）。"""
    db = _mkdb(tmp_path, "idx.db")
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    for want in ("idx_images_gallery_id", "idx_images_task_id",
                 "idx_images_score_id", "idx_images_review_id"):
        assert want in names, f"缺索引 {want}（keyset 会退化成全表扫描）"


def test_l1_index_migration_is_idempotent_on_old_db(tmp_path):
    """老库（列还没 ALTER 出来）跑迁移不能炸 —— 建不上索引只是慢，不是错。"""
    db = _mkdb(tmp_path, "old.db")
    # needs_review 是后加的列，先删掉索引依赖它的那条，再直接重跑迁移
    db.execute("DROP INDEX IF EXISTS idx_images_review_id")
    db._migrate_gallery_indexes()          # 不抛异常即通过
    db._migrate_gallery_indexes()          # 再跑一次也不抛（幂等）
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_images_gallery_id" in names


def test_l1_keyset_uses_index(big_db):
    """EXPLAIN 里不能出现 SCAN images（那说明游标条件没走索引）。"""
    plan = big_db.query(
        f"EXPLAIN QUERY PLAN SELECT {GALLERY_COLUMNS} FROM images "
        f"WHERE {GAL_WHERE} AND id < ? ORDER BY id DESC LIMIT ?", (999999, 200))
    text = " ".join(str(r["detail"]) for r in plan)
    # 允许 SCAN images USING COVERING INDEX（用了索引，只是名字里带 SCAN）
    assert "USING" in text and "INDEX" in text.upper(), \
        f"keyset 查询没走索引：{text}"
    assert "SCAN images" not in text.replace("SCAN images USING", ""), \
        f"退化成全表扫描了：{text}"


# --------------------------------------------------------------- 6. 边界
def test_l1_empty_result(small_db):
    rows, cur, more = list_images_page(
        small_db, where="path IS NOT NULL AND 0", cursor=None, limit=200)
    assert rows == [] and cur is None and more is False


def test_l1_cursor_past_end_returns_empty(small_db):
    """翻页游标走到最末之后，再翻一次 → 空页 + has_more=False。

    这里要区分两个「没有下一页」：末页**不满**时（450 行 / 每页 200 →末页 50）
    实现返回 ``has_more=False`` 且 ``next_cursor=None``；而末页**刚好满**时
    ``has_more=False`` 但 ``next_cursor`` 仍会给（多取一条没取到 → 无 more）。
    两种都不该再吐出行。
    """
    kw = dict(where=GAL_WHERE, sort="id", desc=True, limit=PAGE)
    rows, cursor, more = list_images_page(small_db, **kw)
    assert more and cursor is not None, "首页之后应该还有下一页"
    # 一路翻到 has_more=False
    for _ in range(10):
        rows, cursor, more = list_images_page(small_db, cursor=cursor, **kw)
        if not more:
            break
    assert not more
    # 无论 next_cursor 是不是 None，再翻一次都不能凭空冒出整页数据
    probe_c = cursor
    out, _nc, _m = list_images_page(small_db, cursor=probe_c, **kw)
    if probe_c is None:
        # cursor=None 语义是「打开第一页」，这是设计如此，不是 bug
        assert len(out) == PAGE
    else:
        assert out == [], "越过末页还取到了数据"


def test_l1_bad_sort_key_falls_back_to_id(small_db):
    """未知排序键不许拼进 SQL（注入面），必须退化成 id。"""
    kw = dict(where=GAL_WHERE, desc=True, limit=10)
    a, _, _ = list_images_page(small_db, sort="id", **kw)
    b, _, _ = list_images_page(small_db, sort="id; DROP TABLE images--",
                               **kw)
    assert [r["id"] for r in a] == [r["id"] for r in b]
    # 表还在
    assert small_db.query("SELECT COUNT(*) AS c FROM images")[0]["c"] == 450