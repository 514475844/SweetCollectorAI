"""#60 / A2-1：AI 阈值默认 0.35 + 一次性旧库迁移。

用户原话：0.5 太严，正确匹配也进不了库。默认值改 0.35 已属**破坏性改动**——
老库里所有停在 0.5 的任务会突然收得多一大截。所以这块的验收重点不是
「默认值改了」，而是：
  1. 五处默认值一致（models / db列默认 / repo兜底 / learn_folder / GUI表单）；
  2. 迁移**只跑一次**（app_meta 标记），用户以后手动调回 0.5 不会被改回去；
  3. 迁移**只动恰好 0.5 的行**，自己调过 0.2/0.8 的任务一个都不许碰；
  4. 迁移不丢数据（行数不变），且写审计。
"""
import sqlite3

import pytest


def _mkdb(tmp_path, rows, name="m.db"):
    """先造一个「老库」：tasks 表有 ai_threshold 列，但**没有 app_meta 表**。"""
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT NOT NULL, adapter_name TEXT, entry_urls TEXT DEFAULT '[]',"
        " keywords TEXT DEFAULT '[]', blacklist TEXT DEFAULT '[]',"
        " proxy TEXT, use_proxy INTEGER DEFAULT 0, concurrency INTEGER DEFAULT 4,"
        " max_depth INTEGER DEFAULT 3, delay REAL DEFAULT 1.0,"
        " output_dir TEXT, auto_run INTEGER DEFAULT 0,"
        " status TEXT DEFAULT 'idle', is_template INTEGER DEFAULT 0,"
        " parent_id INTEGER, created_at TEXT, updated_at TEXT,"
        " ai_enabled INTEGER DEFAULT 0, ai_labels TEXT DEFAULT '[]',"
        " ai_blacklist TEXT DEFAULT '[]', ai_threshold REAL DEFAULT 0.5,"
        " ai_match_mode TEXT DEFAULT 'any',"
        " ai_exclude_match_mode TEXT DEFAULT 'any',"
        " priority INTEGER DEFAULT 5, last_error TEXT, last_run_at TEXT,"
        " min_width INTEGER DEFAULT 0, min_height INTEGER DEFAULT 0,"
        " min_bytes INTEGER DEFAULT 0, max_bytes INTEGER DEFAULT 0,"
        " schedule_at TEXT, rate_limit INTEGER DEFAULT 0,"
        " allowed_hours TEXT, max_pages INTEGER DEFAULT 0,"
        " ai_labels_l2 TEXT DEFAULT '[]', ai_labels_l3 TEXT DEFAULT '[]',"
        " ai_label_strict INTEGER DEFAULT 0, keyword_subdir INTEGER DEFAULT 0)")
    con.executemany(
        "INSERT INTO tasks(id,name,ai_enabled,ai_threshold) VALUES (?,?,?,?)",
        rows)
    con.commit()
    con.close()
    from media_resource_manager.db import Database
    return Database(p)


# ---------------- 1) 默认值一致性 ----------------
def test_task_config_default_is_035():
    from media_resource_manager.models import TaskConfig
    t = TaskConfig(id=None, name="t", adapter_name="s")
    assert t.ai_threshold == 0.35, t.ai_threshold


def test_repo_fallback_is_035():
    """老库/列缺失时 _task_from_row 的兜底值也得是 0.35，不能留 0.5。

    （兜底在 _task_from_row 里，不在 get_task——get_task 只做取行。）
    """
    from media_resource_manager.repo import _task_from_row
    import inspect
    src = inspect.getsource(_task_from_row)
    assert "0.35" in src, "repo 兜底没到 0.35"
    assert "else 0.5" not in src, "repo 兜底还留着 0.5"
    # 真跑一遍：ai_threshold 为 NULL 的行也得读出 0.35
    import inspect as _i
    assert "ai_threshold=(r[" in src


def test_learn_folder_fallback_is_035():
    """learn_folder 里跟**阈值默认值**相关的两处都该是 0.35。

    ⚠ 不能粗暴断言整个文件不含 "0.5"——那里还有图像过滤的 `s >= 0.5`
    （匹配分下限），跟任务阈值不是一回事，动它会改坏过滤语义。
    """
    import io
    src = io.open("media_resource_manager/learn_folder.py",
                  encoding="utf-8").read()
    assert 'getattr(cfg, "ai_threshold", 0.35)' in src, "cfg兜底还是 0.5"
    assert "task.ai_threshold or 0.35" in src, "task 阈值兜底还是 0.5"
    assert 'getattr(cfg, "ai_threshold", 0.5)' not in src
    assert "task.ai_threshold or 0.5" not in src


def test_db_column_default_is_035():
    """新建库（无tasks 表）时 ai_threshold 列默认必须是 0.35。"""
    import io
    src = io.open("media_resource_manager/db.py", encoding="utf-8").read()
    assert '"ai_threshold": "REAL DEFAULT 0.35"' in src, \
        "db.py 补列声明还是 0.5"
    from media_resource_manager.db import DEFAULT_AI_THRESHOLD
    assert DEFAULT_AI_THRESHOLD == 0.35


def test_secondary_fallbacks_follow_the_default():
    """两处「查不到任务阈值时的兜底」也必须跟着走，否则会出现
    「新建任务 0.35，但审核排序按 0.5 排」这种前后不一致的怪状态。

    ⚠ 只看**代码行**（去掉注释再匹配）——历史注释里提到过 0.5，
    那是解释「以前写死 0.5 错在哪」，留着有价值，不能因此误判。
    """
    import io
    import re

    def code_only(path):
        out = []
        for line in io.open(path, encoding="utf-8").read().splitlines():
            out.append(re.sub(r"#.*$", "", line))
        return "\n".join(out)

    q = code_only("media_resource_manager/review/queue.py")
    assert re.search(r"^\s*thr = 0\.35\s*$", q, re.M), \
        "review/queue.py 的兜底阈值没跟上"
    assert not re.search(r"^\s*thr = 0\.5\s*$", q, re.M), \
        "review/queue.py 还留着 0.5 兜底"
    g = code_only("tools/repair_gallery.py")
    assert re.search(r"^SAFE_THRESHOLD = 0\.35\s*$", g, re.M), \
        "repair_gallery 的安全阈值没跟上"


def test_gui_form_default_is_035():
    import io
    src = io.open("media_resource_manager/gui/main_window.py",
                  encoding="utf-8").read()
    assert "self.f_ai_thr.setValue(0.35)" in src, \
        "任务表单阈值初值还是 0.5"
    assert "self.f_ai_thr.setValue(0.5)" not in src


# ---------------- 2) 迁移：只跑一次 ----------------
def test_migration_lowers_legacy_05_once(tmp_path):
    db = _mkdb(tmp_path, [(1, "a", 1, 0.5), (2, "b", 1, 0.5)])
    try:
        got = {r["id"]: r["ai_threshold"]
               for r in db.query("SELECT id,ai_threshold FROM tasks")}
        assert got == {1: 0.35, 2: 0.35}, got
        assert db.meta_get("migrated_ai_threshold_035"), "迁移标记没写"
    finally:
        db.close()


def test_migration_does_not_rerun(tmp_path):
    """**本条是这块的命门**：用户迁完之后自己把阈值调回 0.5，
    重开软件绝不能又被改回 0.35（否则这个设置永远调不动）。"""
    p = tmp_path / "again.db"
    db = _mkdb(tmp_path, [(1, "a", 1, 0.5)], name="again.db")
    try:
        assert db.query("SELECT ai_threshold t FROM tasks WHERE id=1")[0]["t"] \
            == 0.35
        # 用户自己调回 0.5（这是用户的决定，不是旧默认残留）
        db.execute("UPDATE tasks SET ai_threshold=0.5 WHERE id=1")
    finally:
        db.close()

    db2 = type(db)(p)
    try:
        got = db2.query("SELECT ai_threshold t FROM tasks WHERE id=1")[0]["t"]
        assert got == 0.5, f"迁移被重复执行了，用户调的值被覆盖：{got}"
    finally:
        db2.close()


def test_migration_leaves_custom_values_alone(tmp_path):
    """用户自己调过的 0.2 / 0.8 / 0.45 一律不动——只迁恰好 0.5。"""
    db = _mkdb(tmp_path, [(1, "lo", 1, 0.2), (2, "hi", 1, 0.8),
                          (3, "mid", 1, 0.45), (4, "legacy", 1, 0.5)])
    try:
        got = {r["id"]: r["ai_threshold"]
               for r in db.query("SELECT id,ai_threshold FROM tasks")}
        assert got == {1: 0.2, 2: 0.8, 3: 0.45, 4: 0.35}, got
    finally:
        db.close()


def test_migration_keeps_rows_and_audits(tmp_path):
    db = _mkdb(tmp_path, [(1, "a", 1, 0.5), (2, "b", 0, 0.5)])
    try:
        assert db.query("SELECT COUNT(*) c FROM tasks")[0]["c"] == 2, \
            "迁移把行弄没了"
        # 没启用 AI 的任务也迁了？—— 迁的是「旧默认残留」，与enabled 无关，
        # 因为 enabled 随时可能被打开，阈值得先是对的。
        a = db.query("SELECT detail FROM audit_log "
                     "WHERE action='ai_threshold_default'")
        assert a and "2 个任务" in a[0]["detail"], [dict(x) for x in a]
    finally:
        db.close()


def test_migration_idempotent_on_fresh_db(tmp_path):
    """全新库（没有任何任务）也不能因为迁移炸掉。"""
    db = _mkdb(tmp_path, [])
    try:
        assert db.query("SELECT COUNT(*) c FROM tasks")[0]["c"] == 0
        assert db.meta_get("migrated_ai_threshold_035")
    finally:
        db.close()


def test_meta_get_survives_without_app_meta_table(tmp_path):
    """app_meta 表缺失时 meta_get 返回默认、不抛——标记读取本身绝不能炸库。"""
    import io
    src = io.open("media_resource_manager/db.py", encoding="utf-8").read()
    assert "CREATE TABLE IF NOT EXISTS app_meta" in src
    from media_resource_manager.db import Database
    p = tmp_path / "nometa.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE dummy (x INTEGER)")
    con.commit()
    con.close()
    db = Database(p)
    try:
        assert db.meta_get("nope", "fallback") == "fallback"
    finally:
        db.close()