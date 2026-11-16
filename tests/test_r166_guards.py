"""R166 护栏：所有判断动作都参与学习。

用户原话（2026-10-20 11:19）：「我要求全部进行学习。」—— 不再只有
「不喜欢 + 原因词」才学习。图库 / 审核 / 隔离区 / 待审核里用户做出的
**每一种判断**都要成为 AI 校准的样本。

本文件钉住四件事（对应订单四条）：
  ① 每种动作都有极性（正/负/强负），且**权重方向不许搞反**；
  ② 「主题词永不进排除词」这条硬规则不被新路径绕过；
  ③ 来源可区分（gallery / review / quarantine / web_*），统计数字对得上；
  ④ 「彻底删除」会删掉 reviews 行，样本来源必须另有 append-only 账本。

⚠ 反例警示（本文件特意钉住"别改回去"）：
  · 原来审核页 `decide()` 里显式学一遍、`_batch_decide_ids` 的 factory
    又学一遍 ⇒ **同一次判断权重双计**。判据是「审核路径只有一个学习点」。
  · 原来图库 `_apply_like` / `_apply_dislike` 用裸 ``json.loads(tags)``——
    老库里「逗号分隔」格式的标签会抛异常被 except 吞掉 = 那些图永远学不到。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MM = ROOT / "media_resource_manager"
PREF = MM / "classify" / "preference.py"
STATS = MM / "classify" / "learning_stats.py"
DB = MM / "db.py"
MW = MM / "gui" / "main_window.py"
RT = MM / "gui" / "review_tab.py"
WEB = MM / "webui.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _code(s: str) -> str:
    """剥掉 Python 注释与字符串字面量再判据。

    ⚠ 本项目踩过太多次「护栏查字符串、自己的说明注释里也写着那个词」的假红
    （R170 的「克隆任务」、R165 的 `var(--text)` 都是同型）。凡是判
    「某个符号有没有被用到」的断言，一律先把注释剥掉再查。
    """
    return re.sub(r"#[^\n]*|'''.*?'''|\"\"\".*?\"\"\"", "", s, flags=re.S)


def _web_env(tmp_path, monkeypatch):
    """建一份隔离的配置+库，并返回一个「不走真 socket」的 Handler 工厂。

    与 R164 护栏同思路：只测图片动作分支，请求体由测试直接塞进去，
    不启真 HTTP 服务（起服务会让单测慢且偶发端口冲突）。
    """
    import json as _json
    monkeypatch.setenv("MRM_HOME", str(tmp_path / "home"))
    from media_resource_manager.config import load_config
    from media_resource_manager.db import Database
    from media_resource_manager import webui

    class _H(webui._Handler):
        # ⚠ 必须**重写 cfg/db 两个 property**：基类里它们是
        #   ``return self.server.db`` —— 这个假 Handler 没有 server，
        #   不重写会在第一次取 db 时就 AttributeError（R164 护栏踩过同款）。
        _cfg = None
        _db = None
        _payload: dict = {}

        def __init__(self, cfg, db, payload=None):
            self._cfg = cfg
            self._db = db
            self._payload = payload or {}
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
            return self._send(
                code, _json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                "application/json", extra)

        def _body(self):
            return self._payload

    cfg = load_config()
    cfg.web_password = ""
    cfg.web_lan_open = True
    cfg.web_bind = "127.0.0.1"
    cfg.ensure_dirs()
    return cfg, Database(cfg.db_path), _H


# =============================================== ① 动作 → 极性

def test_every_judgement_action_has_a_weight():
    """订单第 1 条：每一种判断动作都要有学习权重（不是只有 dislike）。"""
    from media_resource_manager.classify import preference as P
    for act in ("like", "dislike", "blacklist", "restore", "favorite", "purge"):
        assert act in P._DECISION_W, f"动作 {act} 没进 _DECISION_W —— 它不会参与学习"
        assert act in P._DECISION_KIND, f"动作 {act} 没进 _DECISION_KIND —— 命中方向记不上"


def test_polarities_are_correct_and_not_inverted():
    """★ 方向不许搞反：通过/还原/收藏 = 正；不喜欢 = 负；拉黑/彻底删除 = 强负。"""
    from media_resource_manager.classify import preference as P
    pos = ("like", "restore", "favorite")
    neg = ("dislike",)
    strong = ("blacklist", "purge")
    for a in pos:
        assert P._DECISION_W[a] > 0, f"{a} 该是正样本，实际 {P._DECISION_W[a]}"
        assert P._DECISION_KIND[a] == "pos", f"{a} 的命中方向该是 pos"
    for a in neg:
        assert P._DECISION_W[a] < 0, f"{a} 该是负样本，实际 {P._DECISION_W[a]}"
        assert P._DECISION_KIND[a] == "neg", f"{a} 的命中方向该是 neg"
    for a in strong:
        assert P._DECISION_W[a] <= P.DISLIKE, \
            f"{a} 是强负样本，权重不该高于普通 dislike（{P._DECISION_W[a]}）"
        assert P._DECISION_KIND[a] == "neg", f"{a} 的命中方向该是 neg"


def test_strong_negative_is_heavier_than_plain_negative():
    """「彻底删除」要比「不喜欢」更重 —— 用户明确不要，且不可还原。"""
    from media_resource_manager.classify import preference as P
    assert abs(P._DECISION_W["purge"]) >= abs(P._DECISION_W["dislike"]), \
        "彻底删除的惩罚不比不喜欢重 —— 订单要求它是强负样本"


def test_skip_stays_neutral():
    """「跳过」= 这页先不看，不是表态 —— 不许被顺手改成样本。"""
    from media_resource_manager.classify import preference as P
    assert P._DECISION_W.get("skip") == 0.0, "跳过被赋了非零权重"


def test_learning_board_lists_every_source():
    """订单第 4 条：看板要能区分全部样本来源（含新增的还原/收藏/彻底删除）。"""
    from media_resource_manager.classify import learning_stats as LS
    for act in ("like", "restore", "favorite", "dislike", "blacklist",
                "purge", "skip"):
        assert act in LS.DECISION_ZH, f"看板没有 {act} 这个来源的中文名"
        assert act in LS.DECISION_POLARITY or act == "skip", \
            f"看板缺 {act} 的极性标注"


def test_legacy_weights_unchanged():
    """★ 反向护栏：新增键**不改任何既有动作的权重**（老学习结果不受影响）。"""
    from media_resource_manager.classify import preference as P
    assert P._DECISION_W["like"] == P.LIKE == 1.0
    assert P._DECISION_W["dislike"] == P.DISLIKE == -1.0
    assert P._DECISION_W["blacklist"] == P.BLACKLIST == -2.0


# =============================================== ② 主题词永不进排除词

def test_theme_words_never_reach_exclusion_list():
    """★ 硬规则：负样本只影响权重，自动写进 ai_blacklist 的只有弹窗勾的词。"""
    src = _src(PREF)
    # learn_action 全程不许碰 ai_blacklist / 任务表
    i = src.index("def learn_action")
    body = src[i:src.index("\n    # =================", i)]
    assert "ai_blacklist" not in body, \
        "learn_action 碰了 ai_blacklist —— 会把整个主题误杀"
    assert "UPDATE tasks" not in body, "learn_action 改任务表了（学习只该动 preferences）"


def test_all_three_tiers_still_guarded():
    """三条主路径都必须保留「左栏只学习、右栏才写排除词」的注释与判据。"""
    for p, name in ((MW, "main_window"), (RT, "review_tab"), (WEB, "webui")):
        s = _src(p)
        assert "ai_blacklist" in s, f"{name} 里排除词机制不见了"


# =============================================== ③ 来源可区分

def test_learn_action_records_its_source():
    """订单第 4 条：每次学习要留下「从哪来的」—— 看板才能分组。"""
    src = _src(PREF)
    i = src.index("def learn_action")
    body = src[i:src.index("\n    # =================", i)]
    assert "record_learn_sample" in body, "learn_action 没落来源留痕"
    assert "source=source" in body, "没把 source 传进留痕"


def test_db_has_append_only_sample_ledger():
    """订单第 4 条（关键）：彻底删除会清 reviews 行，来源必须另有账本。"""
    s = _src(DB)
    assert "CREATE TABLE IF NOT EXISTS learn_samples" in s, \
        "没有 learn_samples 账本 —— 彻底删除的样本会当场消失"
    i = s.index("CREATE TABLE IF NOT EXISTS learn_samples")
    blk = s[i:s.index(");", i)]
    for col in ("act", "polarity", "weight", "source", "labels", "ts"):
        assert col in blk, f"learn_samples 缺列 {col}"


def test_sample_ledger_really_exists_in_a_live_db(tmp_path):
    """★ 变异测试逼出来的判据：光「源码里有这句 SQL」不够 ——
    必须真建出一张能用、能写、能读回的表（改名/漏执行的变异要能被抓住）。"""
    from media_resource_manager.db import Database
    db = Database(tmp_path / "r166_ledger.db")
    try:
        cols = {r["name"] for r in db.query("PRAGMA table_info(learn_samples)")}
        assert {"act", "polarity", "weight", "source", "labels", "ts"} <= cols, \
            f"learn_samples 表没真建出来 / 缺列：{cols}"
        db.record_learn_sample(act="like", polarity="pos", weight=1.0,
                               image_id=1, task_id=0, source="gallery",
                               labels=["cat", "dog"])
        got = db.learn_sample_counts()
        assert got.get("like") == 1, f"写进去读不回来：{got}"
        assert db.learn_sample_sources().get("gallery", {}).get("like") == 1, \
            "来源读不回来"
    finally:
        db.close()


def test_record_sample_persists_labels(tmp_path):
    """账本要能还原「这次判断针对哪些标签」（否则事后无法复盘）。"""
    import json as _json
    from media_resource_manager.db import Database
    db = Database(tmp_path / "r166_labels.db")
    try:
        db.record_learn_sample(act="purge", polarity="strong_neg",
                               weight=-2.0, image_id=7, task_id=3,
                               source="quarantine", labels=["猫", "狗"])
        r = db.query("SELECT image_id, task_id, labels FROM learn_samples")[0]
        assert int(r["image_id"]) == 7 and int(r["task_id"]) == 3, dict(r)
        assert _json.loads(r["labels"]) == ["猫", "狗"], r["labels"]
    finally:
        db.close()


def test_record_sample_swallows_broken_input(tmp_path):
    """★ 留痕失败不许炸：传进去取不出来的东西也只能静默退化成 []。"""
    from media_resource_manager.db import Database
    db = Database(tmp_path / "r166_bad.db")
    try:
        class _Boom:
            def __iter__(self):
                raise RuntimeError("boom")
        db.record_learn_sample(act="like", labels=_Boom())   # 不许抛
        assert db.learn_sample_counts().get("like") == 1
    finally:
        db.close()


def test_sample_ledger_is_append_only():
    """账本只能追加：不许出现 UPDATE/DELETE learn_samples。"""
    for p in (DB, MW, RT, WEB, MM / "cleanup.py", MM / "tidy.py"):
        if not p.exists():
            continue
        s = _src(p)
        assert not re.search(r"DELETE\s+FROM\s+learn_samples", s, re.I), \
            f"{p.name} 在删样本账本 —— 历史不该被抹"
        assert not re.search(r"UPDATE\s+learn_samples", s, re.I), \
            f"{p.name} 在改样本账本 —— append-only 被破坏"


def test_writer_does_not_double_count_weights():
    """★ 账本只记来源，权重仍只走 preferences（防"两份账各算一次"）。"""
    src = _code(_src(DB))
    i = src.index("def record_learn_sample")
    body = src[i:src.index("def learn_sample_counts", i)]
    assert "set_preference" not in body, "record_learn_sample 里又去加权重了（会双计）"
    assert "preferences" not in body, "record_learn_sample 碰了 preferences 表"


def test_web_quarantine_and_gallery_sources_distinguished():
    """网页端隔离区/图库两类来源必须分开记。"""
    s = _src(WEB)
    assert "web_quarantine" in s and "web_gallery" in s, \
        "网页端没区分隔离区/图库来源 —— 看板上两条会混成一条"


def test_web_quarantine_delete_learns_before_erasing(tmp_path, monkeypatch):
    """★ 隔离区彻底删除必须**先学再删**（删完就取不到 tags 了）。

    真行为断言：调真接口，看样本账本里有没有留下一条 purge，
    **且图片行已经没了** —— 证明学习发生在删除之前而不是被删除带走。
    """
    from media_resource_manager.db import Database
    from media_resource_manager import webui as W

    cfg, db, _H = _web_env(tmp_path, monkeypatch)
    f = tmp_path / "q_purge.jpg"
    f.write_bytes(b"fakeimage")
    db.execute("INSERT INTO tasks (name, ai_labels) VALUES ('r166q', '[]')")
    db.execute(
        "INSERT INTO images (task_id, source_url, path, tags,"
        " needs_review, in_quarantine, blacklisted) VALUES (1,?,?,?,0,1,0)",
        ("http://example.com/qp.jpg", str(f), '["cat", "dog"]'))
    iid = db.query("SELECT id FROM images WHERE source_url=?",
                   ("http://example.com/qp.jpg",))[0]["id"]

    r = _H(cfg, db)._api_image_action({"ids": [iid], "act": "delete"})
    assert r["ok"] and r["done"] == 1, f"隔离区删除未成功：{r}"

    assert not db.query("SELECT * FROM images WHERE id=?", (iid,)), \
        "隔离区删除没删图片行"
    acts = db.learn_sample_counts()
    assert acts.get("purge") == 1, \
        f"彻底删除没留下强负样本（学习丢在删除里了）：{acts}"
    srcs = db.learn_sample_sources()
    assert "web_quarantine" in srcs, f"来源没标成网页隔离区：{srcs}"
    assert srcs["web_quarantine"].get("purge") == 1, srcs
    # 权重要落在传入的标签上，且是强负
    w = {r["label"]: float(r["weight"]) for r in db.query(
        "SELECT label, weight FROM preferences WHERE task_id=0")}
    assert w.get("cat", 0) < 0 and w.get("dog", 0) < 0, \
        f"彻底删除没给标签负权重：{w}"
    db.close()


def test_web_quarantine_like_counts_as_restore(tmp_path, monkeypatch):
    """★ 隔离区点「审核通过」实为**还原**，来源与动作要分开记。"""
    from media_resource_manager.db import Database
    from media_resource_manager import webui as W

    cfg, db, _H = _web_env(tmp_path, monkeypatch)
    db.execute("INSERT INTO tasks (name, ai_labels) VALUES ('r166r', '[]')")
    db.execute(
        "INSERT INTO images (task_id, source_url, path, tags,"
        " needs_review, in_quarantine, blacklisted) VALUES (1,?,?,?,1,1,0)",
        ("http://example.com/qr.jpg", str(tmp_path / "qr.jpg"), '["cat"]'))
    iid = db.query("SELECT id FROM images WHERE source_url=?",
                   ("http://example.com/qr.jpg",))[0]["id"]

    r = _H(cfg, db)._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] and r["done"] == 1, r

    srcs = db.learn_sample_sources()
    assert srcs.get("web_quarantine", {}).get("restore") == 1, \
        f"隔离区还原没记成 restore：{srcs}"
    assert "like" not in srcs.get("web_quarantine", {}), \
        "隔离区还原被错记成 like（看板上两条会混）"
    w = {r["label"]: float(r["weight"]) for r in db.query(
        "SELECT label, weight FROM preferences WHERE task_id=0")}
    assert w.get("cat", 0) > 0, f"还原应该是正样本：{w}"
    db.close()


def test_web_gallery_like_keeps_like_semantics(tmp_path, monkeypatch):
    """★ 反向护栏：图库点「通过」仍是 like（不该被还原口径改坏）。"""
    from media_resource_manager import webui as W

    cfg, db, _H = _web_env(tmp_path, monkeypatch)
    db.execute("INSERT INTO tasks (name, ai_labels) VALUES ('r166g', '[]')")
    db.execute(
        "INSERT INTO images (task_id, source_url, path, tags,"
        " needs_review, in_quarantine, blacklisted) VALUES (1,?,?,?,0,0,0)",
        ("http://example.com/gl.jpg", str(tmp_path / "gl.jpg"), '["cat"]'))
    iid = db.query("SELECT id FROM images WHERE source_url=?",
                   ("http://example.com/gl.jpg",))[0]["id"]

    r = _H(cfg, db)._api_image_action({"ids": [iid], "act": "like"})
    assert r["ok"] and r["done"] == 1, r
    srcs = db.learn_sample_sources()
    assert srcs.get("web_gallery", {}).get("like") == 1, \
        f"图库通过没记成 like：{srcs}"
    db.close()


def test_web_favorite_only_learns_on_adding(tmp_path, monkeypatch):
    """★ 收藏只在「收」时学；取消收藏不学（不是"这张不要"）。"""
    from media_resource_manager import webui as W

    cfg, db, _H = _web_env(tmp_path, monkeypatch)
    db.execute("INSERT INTO tasks (name, ai_labels) VALUES ('r166f', '[]')")
    db.execute(
        "INSERT INTO images (task_id, source_url, path, tags,"
        " needs_review, in_quarantine, blacklisted) VALUES (1,?,?,?,0,0,0)",
        ("http://example.com/fv.jpg", str(tmp_path / "fv.jpg"), '["cat"]'))
    iid = db.query("SELECT id FROM images WHERE source_url=?",
                   ("http://example.com/fv.jpg",))[0]["id"]
    h = _H(cfg, db)

    h._api_image_action({"ids": [iid], "act": "favorite"})   # 收
    assert db.learn_sample_counts().get("favorite") == 1, "收藏没学"
    h._api_image_action({"ids": [iid], "act": "favorite"})   # 取消
    assert db.learn_sample_counts().get("favorite") == 1, \
        "取消收藏又学了一次 —— 会把用户没意见的图打成负样本"
    db.close()


def test_desktop_sources_present():
    """桌面端图库/审核/隔离区三类来源都要有。"""
    assert 'source="gallery"' in _src(MW), "桌面图库没记来源"
    assert 'source="quarantine"' in _src(MW), "桌面隔离区没记来源"
    assert 'source="review"' in _src(RT), "桌面审核页没记来源"


# =============================================== ④ 覆盖每个动作

def test_web_favorite_participates_in_learning():
    """图库收藏 = 正样本，网页端也要学。"""
    s = _src(WEB)
    i = s.index('if act == "favorite":')
    blk = s[i:i + 1200]
    assert "favorite" in blk and "_learn_web_action" in blk, \
        "网页端收藏没参与学习"


def test_web_quarantine_delete_learns_before_erasing():
    """★ 隔离区彻底删除必须**先学再删**（删完就取不到 tags 了）。"""
    s = _src(WEB)
    i = s.index("self._hard_delete_image(iid)")
    before = s[max(0, i - 900):i]
    assert "_learn_web_action" in before, \
        "彻底删除前没学习 —— 图片行删掉后 tags 就没了，样本永久丢失"
    assert before.index("_learn_web_action") < len(before), "顺序不对"


def test_desktop_purge_learns_before_deleting():
    """桌面端隔离区彻底删除同样「先学再删」。"""
    s = _src(MW)
    i = s.index('"purge", self._tags_of_row(row[0])')
    j = s.index('DELETE FROM images WHERE id=?', i)
    assert i < j, "桌面端 purge 在删行之后才学 —— tags 已取不到"


def test_desktop_restore_participates_in_learning():
    """隔离区还原回图库 = 正样本。"""
    s = _src(MW)
    assert '"restore", list(getattr(rec, "tags", None) or [])' in s, \
        "桌面端还原没参与学习"


def test_desktop_favorite_participates_in_learning():
    s = _src(MW)
    i = s.index("def toggle_favorite")
    blk = s[i:i + 1600]
    assert "learn_action" in blk and '"favorite"' in blk, \
        "桌面端收藏没参与学习"


# =============================================== ⑤ 审核路径不许双计

def test_review_decide_has_single_learning_point():
    """★ 反例护栏：审核路径只能有一个学习点（曾经 decide + factory 双计）。"""
    s = _src(RT)
    i = s.index("def decide(")
    j = s.index("def _batch_decide(", i)
    body = s[i:j]
    n = len(re.findall(r"PreferenceLearner\(", body))
    # 允许弹窗勾选/手填词那几处（它们学的是**词**，不是整图 tags）。
    # 但不许再出现对 rec.tags 的整图 learn/learn_action。
    whole = re.findall(r"PreferenceLearner\([^)]*\)\.learn(?:_action)?\(",
                       body)
    assert len(whole) <= 3, \
        f"decide() 里整图学习点有 {len(whole)} 处 —— 疑似与 factory 双计：{whole}"
    # 强判据：不许对 recs 循环做整图 learn
    assert not re.search(r"for r in recs:[\s\S]{0,200}?learn_action", body), \
        "decide() 仍在循环 recs 学整图 tags —— 与 factory 双计（R166 已统一到 factory）"
    assert n >= 0  # 占位，保持变量被用


def test_factory_is_the_review_learning_point():
    """审核页的学习应该发生在 factory 里（撤销栈包得住的位置）。"""
    s = _src(RT)
    i = s.index("def factory(rec):")
    blk = s[i:i + 900]
    assert "learn_action" in blk, "factory 没走统一学习入口"
    assert 'source="review"' in blk, "factory 没标来源"


# =============================================== ⑥ 老格式标签不被静默丢

def test_gallery_uses_split_tags_not_raw_json():
    """★ 反例护栏：老库里「逗号分隔」的标签不许因为 json.loads 抛异常而学不到。"""
    s = _code(_src(MW))
    i = s.index("def _apply_like")
    blk = s[i:i + 2200]
    assert "json.loads" not in blk, \
        "_apply_like 还在裸 json.loads —— 老格式标签会被 except 吞掉（静默不学）"
    assert "_tags_of_row" in blk, "_apply_like 没走统一取标签"


def test_tags_helper_exists_and_is_shared():
    """桌面端与网页端各有一份取标签的辅助（同口径，都用 split_tags）。"""
    assert "def _tags_of_row" in _src(MW), "桌面端缺取标签辅助"
    assert "def _tags_of" in _src(WEB), "网页端缺取标签辅助"
    assert "split_tags" in _src(MW) and "split_tags" in _src(WEB), \
        "取标签没走 simsearch.split_tags（老格式会漏）"


# =============================================== ⑦ 学习开关与失败隔离

def test_learning_switch_respected():
    """R149-B 语义不变：开关关掉时不记权重、也不落留痕（没有权重就没有对照）。"""
    from media_resource_manager.classify import preference as P
    src = _src(PREF)
    i = src.index("def learn_action")
    body = src[i:src.index("\n    # =================", i)]
    assert "is_learning_enabled()" in body, "learn_action 没尊重全局学习开关"
    assert body.index("is_learning_enabled()") < body.index("record_learn_sample"), \
        "开关判断必须在写库之前"


def test_learning_failure_never_breaks_the_action():
    """★ 学习失败不许让审核/删除本身失败（try/except 兜住）。"""
    for p, fn in ((PREF, "learn_action"), (DB, "record_learn_sample")):
        s = _src(p)
        i = s.index(f"def {fn}")
        blk = s[i:i + 3000]
        assert "except Exception" in blk, f"{fn} 没有兜异常 —— 学习失败会掀翻主线动作"


def test_learn_action_writes_the_actual_labels(tmp_path):
    """★ 真行为判据：学习必须落在**传进来那些标签**上，不许被替换/丢弃。

    （变异测试逼出来的：把 ``clean`` 写死成别的值，光靠源码字符串匹配
    是查不出来的 —— 必须真跑一遍看权重落在谁头上。）
    """
    from media_resource_manager.db import Database
    from media_resource_manager.classify.preference import PreferenceLearner
    db = Database(tmp_path / "r166_labels_learn.db")
    try:
        ok = PreferenceLearner(db, 0).learn_action(
            "dislike", ["cat", "dog"], source="review", image_id=1)
        assert ok is True, "learn_action 没记成"
        got = {r["label"]: float(r["weight"]) for r in db.query(
            "SELECT label, weight FROM preferences WHERE task_id=0")}
        assert got.get("cat") == -1.0 and got.get("dog") == -1.0, \
            f"权重没落在传入的标签上：{got}"
        assert set(got) == {"cat", "dog"}, f"多出没传的标签：{set(got)}"
    finally:
        db.close()


def test_learn_action_skips_blank_labels(tmp_path):
    """空/纯空白标签不许产生权重行（否则看板多出莫名其妙的空标签）。"""
    from media_resource_manager.db import Database
    from media_resource_manager.classify.preference import PreferenceLearner
    db = Database(tmp_path / "r166_blank.db")
    try:
        L = PreferenceLearner(db, 0)
        assert L.learn_action("like", ["", "   "], source="gallery") is False
        assert L.learn_action("like", [], source="gallery") is False
        assert not db.query("SELECT * FROM preferences"), "空白标签进了权重表"
    finally:
        db.close()


def test_learn_action_returns_false_when_switch_off(tmp_path):
    """学习开关关掉 → 不记权重也不记留痕（没有权重就没有对照）。"""
    from media_resource_manager.db import Database
    from media_resource_manager.classify import preference as P
    db = Database(tmp_path / "r166_off.db")
    old = P.is_learning_enabled()
    try:
        P.set_learning_enabled(False)
        ok = P.PreferenceLearner(db, 0).learn_action(
            "like", ["cat"], source="gallery")
        assert ok is False, "开关关着还返回记成了"
        assert not db.query("SELECT * FROM preferences"), "开关关着却记了权重"
        assert not db.query("SELECT * FROM learn_samples"), "开关关着却记了留痕"
    finally:
        P.set_learning_enabled(old)
        db.close()


# =============================================== ⑧ 语法与结构自检

def test_sample_line_reports_counts_and_sources():
    """★ 看板的「样本来源」一行要能把每种判断与来源讲清楚（订单第 4 条）。"""
    from media_resource_manager.classify import learning_stats as LS
    st = {
        "samples": {
            "like": {"zh": "喜欢", "count": 2, "polarity": "pos"},
            "favorite": {"zh": "收藏", "count": 1, "polarity": "pos"},
            "dislike": {"zh": "不喜欢", "count": 0, "polarity": "neg"},
            "purge": {"zh": "彻底删除", "count": 3, "polarity": "strong_neg"},
        },
        "sample_total": 6,
        "sources": {"gallery": {"like": 2},
                    "quarantine": {"purge": 3},
                    "web_gallery": {"favorite": 1}},
    }
    line = LS.sample_line(st)
    for frag in ("喜欢 2", "收藏 1", "彻底删除 3", "6 个判断样本",
                 "图库 2", "隔离区 3", "网页图库 1"):
        assert frag in line, f"样本来源行缺「{frag}」：{line}"
    # 0 值的来源不该出现（不然一行挤满没用的 0）
    assert "不喜欢 0" not in line, f"0 值来源也列了：{line}"


def test_sample_line_empty_is_guiding_not_blank():
    """空样本时给引导语，不留空白（空白会被读成"界面坏了"）。"""
    from media_resource_manager.classify import learning_stats as LS
    line = LS.sample_line({"samples": {}, "sample_total": 0, "sources": {}})
    assert line.strip(), "空样本时给了一行空白"
    assert "还没" in line, f"没有引导语：{line}"

def test_module_syntax_ok():
    for p in (PREF, STATS, DB):
        ast.parse(_src(p))


def test_stats_collect_returns_sample_keys():
    """看板 collect 的空库返回值必须带 R166 新键（否则前端取不到会 undefined）。"""
    from media_resource_manager.classify import learning_stats as LS
    import inspect
    body = inspect.getsource(LS.collect)
    for k in ('"samples"', '"sample_total"', '"sources"'):
        assert k in body, f"collect 返回值缺 {k}"


def test_stats_keeps_legacy_keys():
    """★ 反向护栏：老键一个不许少（桌面/网页看板都在读它们）。"""
    from media_resource_manager.classify import learning_stats as LS
    import inspect
    body = inspect.getsource(LS.collect)
    for k in ('"decisions"', '"reviews_total"', '"daily"', '"daily_max"',
              '"top_pos"', '"top_neg"', '"labels_total"'):
        assert k in body, f"collect 丢了老键 {k}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
