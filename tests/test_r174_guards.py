"""R174 护栏：学习权重带「来源」，硬黑名单只收用户手填词（执行方笔记本棒）。

用户原话（2026-10-20）：「这几个单词其实是我想要的图片，只是机器认得不准，
所以我删除了或者点了不喜欢图片。这难道不是一个 bug 吗？」

根因：「用户对某张图的负面反馈」被错误等价于「用户不喜欢模型给这张图打的
那个标签」。模型标签是**假设**不是事实，误标时会把用户真正想要的类目一起
拉进硬黑名单（engine.py 的 ai_blacklist 命中即排除）。

本文件钉住五件事：
  ① preferences 有 source 列，且**老库升级后存量行被标成 model_auto**
     （不标就没法一键清 —— 用户手上现在全是这种脏数据，这条最关键）；
  ② 模型自动标签 learning 后 = model_auto；手填词 = user_typed，且**手填
     语义是粘的**（先自动后手填、或反过来，都算用户词）；
  ③ 「应用学到的排除词」只写 user_typed，**model_auto 一条都不许进
     ai_blacklist**（订单第 1 条，本单的核心）；
  ④ 一键「清模型自动词」只清**负权**的 model_auto（正权是"用户喜欢"，
     清掉损失更大），并顺带把这些词从任务 ai_blacklist 剔除；
  ⑤ 单标签权重有上限（误标不该一次霸榜），两端都要能看见来源。

⚠ 反例警示（后人别改回去）：
  · 老库存量若标成 user_typed，一键清除对当前脏数据**完全无效** ⇒ 订单
    等于没做。所以迁移必须回灌 model_auto。
  · 清词若把正权一起删，等于把用户教了几个月的"喜欢"也清了 —— 清负权即可。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MM = ROOT / "media_resource_manager"
PREF = MM / "classify" / "preference.py"
REPO = MM / "repo.py"
DBPY = MM / "db.py"
RT = MM / "gui" / "review_tab.py"
BOARD = MM / "gui" / "learning_board.py"
WEB = MM / "webui.py"
APPJS = MM / "web" / "static" / "js" / "app.js"

MODEL = "model_auto"
USER = "user_typed"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _code(s: str) -> str:
    """剥掉注释与字符串字面量再判据（R166 同款，防自己的说明文字造成假红）。"""
    return re.sub(r"#[^\n]*|'''.*?'''|\"\"\".*?\"\"\"", "", s, flags=re.S)


def _db(tmp_path, name="t.db"):
    from media_resource_manager.db import Database
    return Database(tmp_path / name)


def _cols(db, table="preferences"):
    return {r["name"] for r in db._conn.execute(f"PRAGMA table_info({table})")}


def _row(db, label):
    rows = db.query("SELECT label, weight, source FROM preferences "
                    "WHERE task_id=0 AND label=?", (label,))
    return rows[0] if rows else None


# ============================================ ① 迁移与来源标记

def test_fresh_db_has_source_column(tmp_path):
    db = _db(tmp_path)
    assert "source" in _cols(db)
    db.close()


def test_legacy_rows_are_backfilled_as_model_auto(tmp_path):
    """★ 最关键的一条：存量权重必须可被一键清除。

    老库里那批脏词（红丝 / 美丽 / 可爱）全是 R174 之前的代码写的，那时
    根本没有来源概念。若迁移给它们打上默认的 user_typed，用户点「清模型
    自动词」会一条都清不掉 —— 订单等于没做。所以**新列一落地就把存量
    回灌成 model_auto**：那批词本来绝大多数就是模型标签，且清掉它们正是
    用户当下要的结果。
    """
    p = tmp_path / "legacy.db"
    db = _db(tmp_path, "legacy.db")
    db.execute("INSERT INTO preferences(task_id,label,weight) VALUES(0,'红丝',-9)")
    db.close()

    # 模拟「升级前的老库」：把这列 DROP 掉（比手写旧 SCHEMA 更贴近真升级路径）
    import sqlite3
    con = sqlite3.connect(str(p))
    con.execute("ALTER TABLE preferences DROP COLUMN source")
    con.commit()
    con.close()

    from media_resource_manager.db import Database
    db2 = Database(p)
    try:
        assert "source" in _cols(db2), "迁移没把 source 列补回来"
        r = _row(db2, "红丝")
        assert r is not None, "存量行丢了"
        assert r["source"] == MODEL, (
            f"存量行被标成 {r['source']} —— 用户点「清模型自动词」会一条都清不掉")
    finally:
        db2.close()


def test_new_rows_are_not_backfilled(tmp_path):
    """反向护栏：回灌只能发生在**刚建列**那一次，不能每次开库都洗一遍。"""
    from media_resource_manager.repo import set_preference
    db = _db(tmp_path)
    set_preference(db, 0, "手填词", -1.0, kind="neg", origin=USER)
    assert _row(db, "手填词")["source"] == USER
    db.close()
    # 重开：不能被回灌成 model_auto
    from media_resource_manager.db import Database
    db2 = Database(tmp_path / "t.db")
    try:
        assert _row(db2, "手填词")["source"] == USER, "重开库把手填词洗成了 model_auto"
    finally:
        db2.close()


# ============================================ ② 学习路径的来源

def test_model_tags_learn_as_model_auto(tmp_path):
    from media_resource_manager.classify.preference import PreferenceLearner
    db = _db(tmp_path)
    PreferenceLearner(db, 0).learn("dislike", ["红丝"])
    assert _row(db, "红丝")["source"] == MODEL, \
        "模型标签学完没标 model_auto —— 清污清不到它"
    db.close()


def test_manual_words_learn_as_user_typed(tmp_path):
    """手填词 = 用户亲手写的，是**唯一**允许进硬黑名单的来源。"""
    from media_resource_manager.classify.preference import PreferenceLearner

    class _NoBridge:
        def resolve(self, _w):
            return None

    db = _db(tmp_path)
    PreferenceLearner(db, 0).learn_manual("dislike", ["手填词"],
                                          bridge=_NoBridge())
    assert _row(db, "手填词")["source"] == USER, "手填词没被标成 user_typed"
    db.close()


def test_user_typed_is_sticky(tmp_path):
    """★ 语义裁决：只要用户亲手填过，这个词就永远是「用户词」。

    顺序两种都测：先自动后手填、先手填后自动。若按"最后一次"记，用户
    手填完再被模型误标一次就会退化成 model_auto，然后被一键清除掉 ——
    用户亲手教的词凭什么被清掉？所以取"最强来源"。
    """
    from media_resource_manager.repo import set_preference
    db = _db(tmp_path)
    set_preference(db, 0, "甲", -1.0, kind="neg", origin=MODEL)
    set_preference(db, 0, "甲", -1.0, kind="neg", origin=USER)
    set_preference(db, 0, "乙", -1.0, kind="neg", origin=USER)
    set_preference(db, 0, "乙", -1.0, kind="neg", origin=MODEL)
    assert _row(db, "甲")["source"] == USER
    assert _row(db, "乙")["source"] == USER, \
        "手填过的词被后来的模型样本洗成 model_auto（会被一键清掉）"
    db.close()


def test_default_origin_is_model_auto_not_user_typed(tmp_path):
    """★ 反向护栏：默认来源必须是 model_auto。

    set_preference 的默认参数若给成 user_typed，所有没显式传来源的老调用
    点（以后新增的也算）都会悄悄变成"用户手填"，然后**合法地**流进硬黑名单
    —— 本单想堵的洞会换个地方继续漏。默认取保守的那一端。
    """
    from media_resource_manager.repo import set_preference
    db = _db(tmp_path)
    set_preference(db, 0, "丙", -1.0, kind="neg")
    assert _row(db, "丙")["source"] == MODEL, \
        "set_preference 默认来源不是 model_auto —— 漏传参数就会污染黑名单"
    db.close()


# ============================================ ③ 权重封顶

def test_weight_is_capped(tmp_path):
    """误标不该一次霸榜：单标签权重封顶（R169 第 3 条并入）。"""
    from media_resource_manager.classify import preference as P
    from media_resource_manager.classify.preference import PreferenceLearner
    cap = getattr(P, "WEIGHT_CAP", None)
    assert cap is not None and cap > 0, "没有权重上限常量"
    db = _db(tmp_path)
    for _ in range(int(cap) + 30):
        PreferenceLearner(db, 0).learn("blacklist", ["红丝"])
    w = float(_row(db, "红丝")["weight"])
    assert abs(w) <= cap + 1e-9, f"权重 {w} 冲破上限 {cap}（误标会永久霸榜）"
    assert w < 0, "方向错了：blacklist 该是负权"
    db.close()


def test_positive_weight_is_capped_too(tmp_path):
    from media_resource_manager.classify import preference as P
    from media_resource_manager.classify.preference import PreferenceLearner
    cap = P.WEIGHT_CAP
    db = _db(tmp_path)
    for _ in range(int(cap) + 30):
        PreferenceLearner(db, 0).learn("like", ["金发"])
    assert abs(float(_row(db, "金发")["weight"])) <= cap + 1e-9
    db.close()


# ============================================ ④ 硬黑名单只收手填词

def test_negative_user_typed_labels_excludes_model_auto(tmp_path):
    from media_resource_manager.repo import (
        set_preference, negative_user_typed_labels)
    db = _db(tmp_path)
    set_preference(db, 0, "模型误标", -9.0, kind="neg", origin=MODEL)
    set_preference(db, 0, "用户真不要", -2.0, kind="neg", origin=USER)
    got = negative_user_typed_labels(db)
    assert "用户真不要" in got
    assert "模型误标" not in got, "模型自动词混进了「用户手填」名单"
    db.close()


def test_apply_learned_exclusions_writes_only_user_typed(tmp_path, monkeypatch):
    """★ 订单第 1 条（本单核心）：点「应用学到的排除词」不许把模型标签写进
    硬黑名单。行为断言 —— 真跑 apply_learned_exclusions，看任务落了什么。
    """
    import media_resource_manager.gui.review_tab as rt
    from media_resource_manager.repo import set_preference, get_task

    db = _db(tmp_path)
    db.execute("INSERT INTO tasks(name) VALUES('t')")
    tid = int(db.query("SELECT last_insert_rowid() AS id")[0]["id"])
    set_preference(db, 0, "红丝", -108.0, kind="neg", origin=MODEL)
    set_preference(db, 0, "美丽", -44.0, kind="neg", origin=MODEL)
    set_preference(db, 0, "手填不要", -3.0, kind="neg", origin=USER)

    shown = []
    monkeypatch.setattr(rt.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(str(a[-1]))))

    class _Combo:
        @staticmethod
        def currentData():
            return None

    class _Tab:
        apply_learned_exclusions = rt.ReviewTab.apply_learned_exclusions
        # ⚠ 替身上的钩子一律写成 `*a, **k`：写成 `lambda self, ...` 在
        #   staticmethod 下收不到 self，零参调用（_show_prefs()）直接 TypeError
        #   —— 那种报错会被 Qt/调用方吞成"点了没反应"。
        log = staticmethod(lambda *a, **k: None)
        # 写完之后会顺手刷一下偏好面板；纯替身上没有那个方法，补个空实现
        _show_prefs = staticmethod(lambda *a, **k: None)

    t = _Tab()
    t.db = db
    t.task_combo = _Combo()
    t.apply_learned_exclusions()

    bl = list(get_task(db, tid).ai_blacklist or [])
    assert "手填不要" in bl, "用户手填的排除词没写进去（正常功能被误伤）"
    assert "红丝" not in bl, "★ 模型自动词被写进硬黑名单 —— 正是本单要修的 bug"
    assert "美丽" not in bl, "★ 模型自动词被写进硬黑名单 —— 正是本单要修的 bug"
    db.close()


def test_apply_learned_exclusions_explains_when_nothing_to_add(tmp_path,
                                                               monkeypatch):
    """只有模型自动词时必须**讲清楚为什么一条都没写**。

    不讲就是「点了没反应」，用户只会以为功能坏了。
    """
    import media_resource_manager.gui.review_tab as rt
    from media_resource_manager.repo import set_preference

    db = _db(tmp_path)
    db.execute("INSERT INTO tasks(name) VALUES('t')")
    set_preference(db, 0, "红丝", -108.0, kind="neg", origin=MODEL)

    shown = []
    monkeypatch.setattr(rt.QMessageBox, "information",
                        staticmethod(lambda *a, **k: shown.append(str(a[-1]))))

    class _Combo:
        @staticmethod
        def currentData():
            return None

    class _Tab:
        apply_learned_exclusions = rt.ReviewTab.apply_learned_exclusions
        # ⚠ 替身上的钩子一律写成 `*a, **k`：写成 `lambda self, ...` 在
        #   staticmethod 下收不到 self，零参调用（_show_prefs()）直接 TypeError
        #   —— 那种报错会被 Qt/调用方吞成"点了没反应"。
        log = staticmethod(lambda *a, **k: None)
        # 写完之后会顺手刷一下偏好面板；纯替身上没有那个方法，补个空实现
        _show_prefs = staticmethod(lambda *a, **k: None)

    t = _Tab()
    t.db = db
    t.task_combo = _Combo()
    t.apply_learned_exclusions()

    assert shown, "一条都没写却什么都没告诉用户（点了没反应）"
    assert "模型" in shown[-1], f"没说明「模型自动词不进黑名单」的原因：{shown[-1]}"
    db.close()


# ============================================ ⑤ 一键清模型自动词

def test_clear_model_auto_negatives_keeps_positives_and_user_words(tmp_path):
    from media_resource_manager.repo import (
        set_preference, clear_model_auto_negatives)
    db = _db(tmp_path)
    set_preference(db, 0, "误标负", -9.0, kind="neg", origin=MODEL)
    set_preference(db, 0, "自动正", 5.0, kind="pos", origin=MODEL)
    set_preference(db, 0, "用户负", -2.0, kind="neg", origin=USER)
    n = clear_model_auto_negatives(db)
    assert n == 1, f"清掉了 {n} 条，期望只清 1 条（负权 + model_auto）"
    assert _row(db, "误标负") is None, "负权的模型自动词没清掉"
    assert _row(db, "自动正") is not None, \
        "正权被一起清了 —— 那是「喜欢」方向，清掉损失更大"
    assert _row(db, "用户负") is not None, "用户手填词被清了"
    db.close()


def test_clear_model_auto_strips_labels_from_task_blacklist(tmp_path):
    """清完还要把词从任务 ai_blacklist 里摘掉，否则引擎照旧屏蔽（治标不治本）。"""
    import json
    from media_resource_manager.repo import (
        set_preference, clear_model_auto_negatives)
    db = _db(tmp_path)
    db.execute("INSERT INTO tasks(name, ai_blacklist) VALUES('t', ?)",
               (json.dumps(["红丝", "手填不要"], ensure_ascii=False),))
    set_preference(db, 0, "红丝", -108.0, kind="neg", origin=MODEL)
    set_preference(db, 0, "手填不要", -2.0, kind="neg", origin=USER)

    clear_model_auto_negatives(db)
    cur = db.query("SELECT ai_blacklist FROM tasks")[0]["ai_blacklist"]
    bl = json.loads(cur or "[]")
    assert "红丝" not in bl, "词清了但任务黑名单里还留着 —— 引擎照旧屏蔽"
    assert "手填不要" in bl, "用户手填的排除词被顺手摘掉了"
    db.close()


def test_clear_model_auto_returns_zero_when_clean(tmp_path):
    from media_resource_manager.repo import clear_model_auto_negatives
    db = _db(tmp_path)
    assert clear_model_auto_negatives(db) == 0
    db.close()


# ============================================ ⑥ 两端可见

def test_learning_stats_exposes_origin_counts(tmp_path):
    from media_resource_manager.classify import learning_stats as LS
    from media_resource_manager.repo import set_preference
    db = _db(tmp_path)
    set_preference(db, 0, "a", -1.0, kind="neg", origin=MODEL)
    set_preference(db, 0, "b", 1.0, kind="pos", origin=MODEL)
    set_preference(db, 0, "c", -1.0, kind="neg", origin=USER)
    got = LS.preference_origins(db)
    assert got.get("model_auto") == 2, f"model_auto 计数 {got}"
    assert got.get("user_typed") == 1, f"user_typed 计数 {got}"
    assert got.get("model_auto_neg") == 1, f"负权 model_auto 计数 {got}"
    db.close()


def test_desktop_board_has_clear_model_auto_button():
    """桌面看板必须有「仅清模型自动词」入口（订单第 3 条）。"""
    s = _code(_src(BOARD))
    assert "clear_model_auto_negatives" in s, "看板没接清除函数"
    assert "仅清模型自动词" in _src(BOARD), "看板没有这个按钮文案"


def test_web_learning_samples_carry_source(tmp_path, monkeypatch):
    """网页样本表要能看出哪条是模型自动写的（用户才有得可清）。"""
    from media_resource_manager.repo import set_preference
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    from media_resource_manager.config import load_config
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    cfg = load_config()
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    set_preference(db, 0, "红丝", -9.0, kind="neg", origin=MODEL)

    class _H(webui._Handler):
        def __init__(self, cfg, db):
            self._cfg, self._db = cfg, db
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)

        @property
        def cfg(self):
            return self._cfg

        @property
        def db(self):
            return self._db

        def _send(self, code, body, ctype, extra=None):
            return (code, body, ctype, extra or {})

        def _json(self, obj, code=200, extra=None):
            import json as _j
            return self._send(code, _j.dumps(obj).encode("utf-8"),
                              "application/json", extra)

    out = _H(cfg, db)._api_learning()
    assert out.get("ok"), out
    srcs = {s["label"]: s.get("source") for s in out.get("samples") or []}
    assert srcs.get("红丝") == MODEL, f"样本没带来源：{srcs}"
    db.close()


def test_web_clear_model_auto_needs_confirm(tmp_path, monkeypatch):
    """清除是删除 ⇒ 后端必须有确认闸（与 /api/learn/reset 同口径）。"""
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    from media_resource_manager.config import load_config
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    cfg = load_config()
    cfg.ensure_dirs()
    db = Database(cfg.db_path)

    class _H(webui._Handler):
        def __init__(self, cfg, db, payload=None):
            self._cfg, self._db, self._p = cfg, db, payload
            self.headers = {}
            self.client_address = ("127.0.0.1", 0)

        @property
        def cfg(self):
            return self._cfg

        @property
        def db(self):
            return self._db

        def _send(self, code, body, ctype, extra=None):
            return (code, body, ctype, extra or {})

        def _json(self, obj, code=200, extra=None):
            import json as _j
            return self._send(code, _j.dumps(obj).encode("utf-8"),
                              "application/json", extra)

    out = _H(cfg, db)._api_learning_clear_model_auto({})
    assert out.get("ok") is False, "不带 confirm 也放行 —— 少点一次就清光学习"
    out = _H(cfg, db)._api_learning_clear_model_auto({"confirm": True})
    assert out.get("ok") is True, out
    db.close()


def test_web_route_is_registered():
    s = _code(_src(WEB))
    assert "/api/learn/clear-model-auto" in s, "路由没注册"
    assert "_api_learning_clear_model_auto" in s
