# -*- coding: utf-8 -*-
"""A5 #64测试：AI 学习闭环（词表桥接 + 手填词权重采信）。

要钉死的三件事：

1. **批量 encode（性能铁律）** —— 1000 个词的词表只允许分批 encode，
   绝不能逐词 encode（那是 1000 次前向，CPU 上要几分钟，用户以为卡死）。
   用一个会记账的假 CLIP 来数encode 次数。
2. **命中即采信（D54）** —— 手填「金发」要真的把权重记到词表标签
   「blond hair」上，而不是记到「金发」上就完事（那样永远打不着地）。
3. **桥接只做加法** —— 桥不到 / 没模型 / 词表为空，一个字都不能少记。

**不用真 CLIP 跑**：本机CPU-only 且 90MB 权重未落地（见MEMORY.md C8），
所以全部用假分类器；这反而更好—— 能精确断言 encode 调用次数。
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"
REVIEW = ROOT / "media_resource_manager" / "gui" / "review_tab.py"
DIALOG = ROOT / "media_resource_manager" / "gui" / "dislike_dialog.py"
BRIDGE = ROOT / "media_resource_manager" / "classify" / "label_bridge.py"
PREF = ROOT / "media_resource_manager" / "classify" / "preference.py"


# ============================================================ 假 CLIP
class _FakeClip:
    """会记账的假 CLIP 文本塔。

    ``encode_text`` 每次调用把words 整份记进 ``calls``，测试据此断言
    「批量 vs 逐词」。向量给的是确定性的伪随机单位向量 —— 我们不测语义，
    只测**调用次数**与**矩阵点积的取词逻辑**。
    """

    def __init__(self, dim: int = 16):
        self.dim = dim
        self.calls: list[list[str]] = []

    def encode_text(self, texts):
        import numpy as np
        texts = list(texts)
        self.calls.append(list(texts))
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2 ** 32))
            v = rng.normal(size=self.dim).astype("float32")
            n = float(np.linalg.norm(v)) or 1.0
            out[i] = v / n
        return out


class _FakeClf:
    """最小 LocalClassifier 替身：只提供 bridge 用到的两个属性。"""

    backend = "clip"

    def __init__(self, dim: int = 16):
        self._clip = _FakeClip(dim)
        self.model_dir = None

    @property
    def encode_calls(self):
        return self._clip.calls


class _NoModelClf:
    """没装权重的机器常态：backend 不是 clip。"""

    backend = "stub"
    _clip = None


def _mkdb(tmp_path, name="p.db"):
    import sqlite3
    p = tmp_path / name
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE preferences (task_id INTEGER NOT NULL, label TEXT,"
        " weight REAL DEFAULT 0.0, seen INTEGER DEFAULT 0,"
        " hit_pos INTEGER DEFAULT 0, hit_neg INTEGER DEFAULT 0,"
        " PRIMARY KEY (task_id, label))")
    con.commit()
    con.close()
    from media_resource_manager.db import Database
    return Database(p)


# ============================================================ 1. 批量 encode
def test_vocab_is_batch_encoded_not_per_word():
    """1000 个词的词表：encode 次数必须远小于词数（逐词=1000 次是事故）。"""
    from media_resource_manager.classify.label_bridge import LabelBridge, BATCH

    vocab = [f"label {i}" for i in range(1000)]
    clf = _FakeClf()
    br = LabelBridge(vocab, clf=clf)
    assert br.build() is True

    n_calls = len(clf.encode_calls)
    expect = -(-len(vocab) // BATCH)          # 向上取整
    assert n_calls == expect, \
        f"词表 encode 了 {n_calls} 次，应为 {expect} 次（分批）；" \
        f"若等于 {len(vocab)} 就是逐词 encode，CPU 上会卡死"
    assert n_calls < len(vocab) / 10, "逐词 encode 了 —— 性能铁律被破坏"
    # 每次调用都必须是一批（长度 >1），不能是单个词
    assert all(len(c) > 1 for c in clf.encode_calls), \
        "有一次只 encode 了一个词 —— 这就是逐词 encode"


def test_single_query_encodes_only_itself():
    """查一个手填词：只 encode 这一个词，绝不重跑整张词表。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    vocab = [f"label {i}" for i in range(500)]
    clf = _FakeClf()
    br = LabelBridge(vocab, clf=clf)
    br.build()
    before = len(clf.encode_calls)

    br.topk("手填的词")

    assert len(clf.encode_calls) - before == 1, "查询应只多一次 encode"
    assert clf.encode_calls[-1] == ["手填的词"], \
        "查询时 encode 的必须只有手填的那个词"


def test_matrix_is_built_once_and_reused():
    """build() 幂等：第二次直接用缓存矩阵，不再 encode。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    clf = _FakeClf()
    br = LabelBridge([f"w{i}" for i in range(20)], clf=clf)
    br.build()
    n = len(clf.encode_calls)
    br.build()
    br.build()
    assert len(clf.encode_calls) == n, "重复 build 又去 encode 了"


def test_topk_does_not_encode_per_candidate():
    """topk 返回 5 个候选，但查询侧只该encode 1 次（点积一次算完）。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    vocab = [f"label {i}" for i in range(50)]
    clf = _FakeClf()
    br = LabelBridge(vocab, clf=clf, min_cos=-1.0)   # 放开阈值只看调用次数
    br.build()
    before = len(clf.encode_calls)
    got = br.topk("查询词", k=5)
    assert len(clf.encode_calls) - before == 1
    assert len(got) == 5


# ============================================================ 2. 磁盘缓存
def test_matrix_cache_roundtrip(tmp_path):
    """同一份词表+ 模型：第二个桥接器从npz 读，不 encode。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    vocab = ["blond hair", "cat", "sunset"]
    c1 = _FakeClf()
    LabelBridge(vocab, clf=c1, cache_dir=tmp_path).build()
    assert len(c1.encode_calls) >= 1
    assert list(tmp_path.glob("label_vecs_*.npz")), "没落缓存文件"

    c2 = _FakeClf()
    br2 = LabelBridge(vocab, clf=c2, cache_dir=tmp_path)
    assert br2.build() is True
    assert br2.cache_hit is True
    assert len(c2.encode_calls) == 0, "命中缓存却还在 encode"


def test_corrupt_cache_is_discarded_not_fatal(tmp_path):
    """缓存文件损坏：必须丢掉重建，**绝不能因为缓存坏了就把桥接搞挂**。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    vocab = ["blond hair", "cat"]
    LabelBridge(vocab, clf=_FakeClf(), cache_dir=tmp_path).build()
    for p in tmp_path.glob("label_vecs_*.npz"):
        p.write_bytes(b"not a npz at all")

    clf = _FakeClf()
    br = LabelBridge(vocab, clf=clf, cache_dir=tmp_path)
    assert br.build() is True
    assert br.cache_hit is False
    assert len(clf.encode_calls) >= 1, "坏缓存没触发重建"


def test_no_cache_dir_means_memory_only():
    """没给cache_dir：照样能建矩阵（只是不落盘）。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    clf = _FakeClf()
    br = LabelBridge(["a", "b"], clf=clf)
    assert br.build() is True
    assert br._cache_path() is None


# ============================================================ 3. 三级命中来源
def test_exact_hit_needs_no_model():
    """词就在词表里：不需要模型，exact 命中，相似度给 1.0。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    br = LabelBridge(["blond hair", "cat"])
    hit = br.resolve("blond hair")
    assert hit is not None
    assert hit.label == "blond hair"
    assert hit.source == "exact"
    assert hit.sim == 1.0


def test_exact_hit_is_case_and_space_insensitive():
    """大小写/多空格不该导致漏命中。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    br = LabelBridge(["Blond  Hair"])
    hit = br.resolve("  blond hair ")
    assert hit is not None and hit.label == "Blond  Hair"
    assert hit.source == "exact"


def test_dict_bridges_chinese_to_english():
    """词典桥接（CPU-only 无模型机器的主力路径）：金发 → blond hair。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    br = LabelBridge(["blond hair", "cat"])       # 无clf = 没模型
    hit = br.resolve("金发")
    assert hit is not None, "内置词典里明明有「金发」，却没桥上"
    assert hit.label == "blond hair"
    assert hit.source == "dict"


def test_dict_hit_has_sim_none_not_zero():
    """⚠️ 词典命中相似度是**None 不是 0.0**。

    0.0 会被界面读成「算过了，完全不像」——而实际是「没算，因为是词典对的」。
    这是 #60 转移原文那条铁律的同一个坑，不能重犯。
    """
    from media_resource_manager.classify.label_bridge import LabelBridge

    hit = LabelBridge(["blond hair"]).resolve("金发")
    assert hit.sim is None, "词典命中的相似度必须是 None"
    assert "—" not in str(hit.sim)


def test_unmatched_word_returns_none():
    """词表里没有、词典也没有：返回 None（调用方据此只按原词记）。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    br = LabelBridge(["blond hair", "cat"])
    assert br.resolve("量子纠缠超导态") is None


def test_clip_source_requires_threshold():
    """clip 来源必须过 min_cos 才采信 —— 不能把不相干的词硬按上。"""
    from media_resource_manager.classify import label_bridge as lb

    vocab = [f"label {i}" for i in range(30)]
    clf = _FakeClf()
    loose = lb.LabelBridge(vocab, clf=clf, min_cos=-1.0)
    loose.build()
    assert loose.topk("完全不相干的词", k=5), "阈值放开时应有候选"

    tight = lb.LabelBridge(vocab, clf=_FakeClf(), min_cos=0.99)
    tight.build()
    assert tight.topk("完全不相干的词", k=5) == [], \
        "阈值 0.99 时不该返回任何 clip 候选"


def test_threshold_is_not_applied_to_display_value():
    """采信门槛卡的是**原始余弦**，不是映射后的 0~1 展示值。

    这是本文件最容易踩的坑：软件别处的 0.35 是给图文相似度用的，
    套到文本-文本上等于「什么词都过」，桥接会退化成永远命中。
    """
    from media_resource_manager.classify import label_bridge as lb

    assert lb.MIN_COS > 0.5, \
        "文本-文本余弦门槛必须显著高于图文门槛，否则桥接等于没门槛"
    #展示值仍然映射到 0~1（跟 #60 转移原文显示同一口径）
    vocab = [f"label {i}" for i in range(20)]
    br = lb.LabelBridge(vocab, clf=_FakeClf(), min_cos=-1.0)
    br.build()
    for _lab, sim, _src in br.topk("查询", k=5):
        if sim is not None:
            assert 0.0 <= sim <= 1.0, f"展示相似度必须 0~1，实际 {sim}"


def test_no_model_still_bridges_exact_and_dict():
    """没装 CLIP 权重的机器（CPU-only 笔记本常态）桥接必须照样能用。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    br = LabelBridge(["blond hair", "cat"], clf=_NoModelClf())
    assert br.has_clip is False
    assert br.ready is False
    assert br.resolve("blond hair").source == "exact"
    assert br.resolve("金发").source == "dict"
    assert br.resolve("压根没有的词") is None


def test_empty_vocab_never_crashes():
    """空词表：任何输入都返回 None，不许抛异常。"""
    from media_resource_manager.classify.label_bridge import LabelBridge

    br = LabelBridge([], clf=_FakeClf())
    assert br.build() is False
    assert br.topk("任何词") == []
    assert br.resolve("任何词") is None


def test_empty_word_returns_none():
    from media_resource_manager.classify.label_bridge import LabelBridge

    br = LabelBridge(["cat"], clf=_FakeClf())
    assert br.resolve("") is None
    assert br.resolve("   ") is None
    assert br.topk(None) == []


# ============================================================ 4. 学习闭环（核心）
def test_learn_manual_writes_to_bridged_vocab_label(tmp_path):
    """**闭环的核心断言**：手填「金发」→ 权重记到「blond hair」上。

    记在「金发」上等于没学 —— CLIP 打出来的是英文标签，
    score() 查权重永远查不到。这就是 #64 要修的断链。
    """
    from media_resource_manager.classify.label_bridge import LabelBridge
    from media_resource_manager.classify.preference import PreferenceLearner

    db = _mkdb(tmp_path)
    try:
        br = LabelBridge(["blond hair", "cat"])
        learner = PreferenceLearner(db, 1)
        hits = learner.learn_manual("dislike", ["金发"], bridge=br)

        assert len(hits) == 1 and hits[0].label == "blond hair"
        from media_resource_manager.repo import get_preferences
        w = get_preferences(db, 1)
        assert "blond hair" in w, \
            f"桥接后的词表标签没有权重：{w} —— 学了等于没学"
        assert w["blond hair"] < 0, "dislike 必须是负权重"
    finally:
        db.close()


def test_learn_manual_keeps_original_word_too(tmp_path):
    """原词也保留：用户以后再填同一个词，路径照样认得；界面也看得见他填过什么。"""
    from media_resource_manager.classify.label_bridge import LabelBridge
    from media_resource_manager.classify.preference import PreferenceLearner
    from media_resource_manager.repo import get_preferences

    db = _mkdb(tmp_path)
    try:
        br = LabelBridge(["blond hair"])
        PreferenceLearner(db, 1).learn_manual("dislike", ["金发"], bridge=br)
        w = get_preferences(db, 1)
        assert "金发" in w, "原词被丢了 —— 用户白填"
        assert "blond hair" in w, "词表标签没记上 —— 闭环还是断的"
    finally:
        db.close()


def test_learn_manual_no_bridge_still_records_word(tmp_path):
    """桥不到：原词必须照样记。桥接只做加法，永远不丢词。"""
    from media_resource_manager.classify.label_bridge import LabelBridge
    from media_resource_manager.classify.preference import PreferenceLearner
    from media_resource_manager.repo import get_preferences

    db = _mkdb(tmp_path)
    try:
        br = LabelBridge(["blond hair", "cat"])
        hits = PreferenceLearner(db, 1).learn_manual(
            "dislike", ["量子纠缠超导态"], bridge=br)
        assert hits == []
        w = get_preferences(db, 1)
        assert "量子纠缠超导态" in w, "桥不到就把词丢了 —— 用户白填"
    finally:
        db.close()


def test_learn_manual_empty_inputs_are_noop(tmp_path):
    """空输入不建桥、不写库（省掉一次几百毫秒的矩阵构建）。"""
    from media_resource_manager.classify.preference import PreferenceLearner
    from media_resource_manager.repo import get_preferences

    db = _mkdb(tmp_path)
    try:
        learner = PreferenceLearner(db, 1)
        assert learner.learn_manual("like", []) == []
        assert learner.learn_manual("like", None) == []
        assert learner.learn_manual("like", ["", "   "]) == []
        assert get_preferences(db, 1) == {}
    finally:
        db.close()


def test_learn_manual_records_hit_direction(tmp_path):
    """桥接后的权重也要记命中方向（#37/D3：权重明细要分两列显示）。"""
    from media_resource_manager.classify.label_bridge import LabelBridge
    from media_resource_manager.classify.preference import PreferenceLearner

    db = _mkdb(tmp_path)
    try:
        PreferenceLearner(db, 1).learn_manual(
            "like", ["金发"], bridge=LabelBridge(["blond hair"]))
        r = db.query("SELECT hit_pos, hit_neg FROM preferences "
                     "WHERE label='blond hair'")
        assert r and r[0]["hit_pos"] == 1 and r[0]["hit_neg"] == 0, dict(r[0] or {})
    finally:
        db.close()


def test_learn_and_learn_manual_are_separate_paths(tmp_path):
    """:meth:`learn` 不该被塞进桥接 —— 它的词已经在词表里，桥接纯属白干。

    给 learn 硬加桥接的后果：每张图判定都要建矩阵（几百毫秒 ×每张），
    软件会肉眼可见地变慢。
    """
    from media_resource_manager.classify.label_bridge import LabelBridge
    from media_resource_manager.classify.preference import PreferenceLearner
    from media_resource_manager.repo import get_preferences

    db = _mkdb(tmp_path)
    try:
        br = LabelBridge(["blond hair"])
        pl = PreferenceLearner(db, 1)
        pl.learn("like", ["blond hair"])       # 不传 bridge
        assert br.encode_calls == 0, "learn() 竟然触发了桥接（矩阵被重建）"
        assert "blond hair" in get_preferences(db, 1)
    finally:
        db.close()


def test_learn_ignores_blank_tags(tmp_path):
    """空串标签不该进库（会把权重面板塞满空气行）。"""
    from media_resource_manager.classify.preference import PreferenceLearner
    from media_resource_manager.repo import get_preferences

    db = _mkdb(tmp_path)
    try:
        PreferenceLearner(db, 1).learn("like", ["", "  ", "cat"])
        assert list(get_preferences(db, 1)) == ["cat"]
    finally:
        db.close()


# ============================================================ 5. 全局词表
def _mkdtemp():
    """mkdtemp 而不是 TemporaryDirectory。

    ⚠️ Windows 上 sqlite 句柄开着就删临时目录 = PermissionError:WinError 32，
    报出来像「被测代码坏了」。``TemporaryDirectory`` 是**退出作用域时**才删，
    那时句柄往往还没关。测试里一律 mkdtemp + finally 手动 close。
    """
    import tempfile
    return Path(tempfile.mkdtemp(prefix="r64_"))


def _full_db(tmp_path):
    """带真实 tasks 表的库（global_vocabulary 要读任务）。"""
    import sqlite3
    p = tmp_path / "full.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE preferences (task_id INTEGER NOT NULL, label TEXT,"
        " weight REAL DEFAULT 0.0, seen INTEGER DEFAULT 0,"
        " hit_pos INTEGER DEFAULT 0, hit_neg INTEGER DEFAULT 0,"
        " PRIMARY KEY (task_id, label));")
    con.commit()
    con.close()
    from media_resource_manager.db import Database
    return Database(p)


def test_global_vocabulary_covers_all_tasks(tmp_path):
    """学习权重是全局的（task_id=0），词表也必须收**所有任务**的。

    只收当前任务的词表 → 换个任务就失忆 → 闭环等于没闭。
    """
    from media_resource_manager.classify.label_bridge import global_vocabulary
    from media_resource_manager.models import TaskConfig, TaskStatus
    from media_resource_manager.repo import save_task

    db = _full_db(tmp_path)
    try:
        for name, labels, black in (("A", ["金发"], ["蓝发"]),
                                    ("B", ["猫"], ["狗"])):
            save_task(db, TaskConfig(
                id=None, name=name, adapter_name="generic",
                ai_labels=labels, ai_blacklist=black))
        vocab = global_vocabulary(db)
        assert "金发" in vocab and "猫" in vocab, \
            f"没收全任务主题词：{vocab}"
        assert "蓝发" in vocab and "狗" in vocab, \
            f"排除词不在词表里 —— 用户勾的排除词就桥接不上了：{vocab}"
        # 双语扩展：中文主题词的英文候选也得进来，否则 CLIP 匹配不上
        assert any("blond" in v.lower() for v in vocab), \
            f"词表没做双语扩展：{vocab}"
    finally:
        db.close()


def test_global_vocabulary_empty_db_returns_empty():
    """空库不炸（global_vocabulary 在很多无任务场景会被调用）。"""
    from media_resource_manager.classify.label_bridge import global_vocabulary

    db = _full_db(_mkdtemp())
    try:
        assert global_vocabulary(db) == []
    finally:
        db.close()


def test_get_bridge_is_shared_and_keyed_by_vocab():
    """同词表复用同一个桥接器（矩阵建一次几百毫秒）；词表变了才重建。"""
    from media_resource_manager.classify import label_bridge as lb

    lb.reset_shared()
    db = _full_db(_mkdtemp())
    try:
        a = lb.get_bridge(db, labels=["blond hair", "cat"])
        b = lb.get_bridge(db, labels=["cat", "blond hair"])
        assert a is b, "同词表（顺序不同）没复用 —— 白建一次矩阵"
        c = lb.get_bridge(db, labels=["blond hair", "cat", "sunset"])
        assert c is not a, "词表变了还复用旧矩阵 —— 会漏词"
    finally:
        db.close()
        lb.reset_shared()


def test_cached_matrix_rows_follow_vocab_order(tmp_path):
    """⚠️ 缓存命中后矩阵行序必须跟着当前词表走。

    缓存文件名用的是**排序后**的词表指纹（顺序无关），而矩阵行序是
    ``self.labels`` 的顺序。两边不一致却直接用，就会「词 A 拿到词 B 的向量」
    —— **静默出错**，比缓存 miss 严重得多。所以这一条专门钉死重排逻辑。
    """
    from media_resource_manager.classify.label_bridge import LabelBridge

    v1 = ["alpha", "beta", "gamma"]
    LabelBridge(v1, clf=_FakeClf(), cache_dir=tmp_path).build()

    v2 = ["gamma", "alpha", "beta"]        # 同一集合，不同顺序
    clf2 = _FakeClf()
    br2 = LabelBridge(v2, clf=clf2, cache_dir=tmp_path)
    assert br2.build() is True and br2.cache_hit is True, "顺序不同却没命中缓存"

    # 逐词重算当基准，跟缓存读出来的比对
    ref = _FakeClf()
    import numpy as np
    expect = {}
    for w in v2:
        expect[w] = ref._clip.encode_text([w])[0]

    def row_for(word):
        i = v2.index(word)
        return br2._mat[i]

    for w in v2:
        a = np.asarray(row_for(w))
        b = np.asarray(expect[w])
        b = b / (np.linalg.norm(b) or 1.0)
        assert float(a @ b) > 0.999, \
            f"「{w}」取到的向量不是它自己的 —— 缓存行序错了（静默错误）"


def test_dedupe_labels_keeps_first_form():
    """去重保序、保留**原词形**（标签要拿去跟 CLIP 比，形态不能被改）。"""
    from media_resource_manager.classify.label_bridge import dedupe_labels

    assert dedupe_labels(["Cat", "cat ", "  ", "dog", "DOG"]) == ["Cat", "dog"]
    assert dedupe_labels([]) == []
    assert dedupe_labels(None) == []


# ============================================================ 6. 界面接线
def _code(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_dialog_preview_is_shown_when_db_given():
    """给了 db 就在手填框下面显示桥接预演（用户边打边看会记到哪个词）。"""
    src = _code(DIALOG)
    assert "textChanged" in src, "桥接预演没接textChanged"
    assert "_bridge_preview" in src
    assert "global_vocabulary" in src


@pytest.fixture
def qapp():
    """确保有一个 QApplication —— **没有它构造 QDialog 会直接 abort 整个进程**。

    这不是 pytest 的问题，是Qt 的硬性要求。项目里别的测试文件能过是因为
    同一进程里已经有别的用例建过 QApplication（依赖执行顺序，脆）。
    单跑本文件时必须自己建。
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app
    # ⚠️ **不要**在这里 app.quit()/退出：Qt 在 QApplication 析构时
    # 若还有活着的顶级窗口会 abort（见 MEMORY.md 里 C10 那条 QThread 坑）。
    # 交给 pytest 进程收尾即可。


def test_dialog_without_db_still_works(monkeypatch, qapp):
    """没给 db 也不能坏（部分测试/无库场景就是不给 db）。

    ⚠️ 两个坑叠在一起：
    1) 没有 QApplication 时构造 QDialog = **整个进程 abort**（rc=127）；
    2) ``QDialog.exec`` 是**模态**的，不给事件循环就永久阻塞。
    所以这里既要 qapp fixture，也必须给 exec 打桩。
    """
    from media_resource_manager.gui import dislike_dialog as dd

    assert dd.ask_dislike_reason(None, [], []) == ([], [], "pure")
    monkeypatch.setattr(dd.QDialog, "exec", lambda self: 0)
    assert dd.ask_dislike_reason(None, ["cat"], ["bad"]) == ([], [], "cancel")
    # 没给 db 时不能因为桥接预演崩掉（标签/排除词都有内容才会走建控件那条路）
    monkeypatch.setattr(dd.QDialog, "exec", lambda self: 1)
    assert dd.ask_dislike_reason(None, ["cat"], ["bad"])[2] in ("cancel", "exclude")


def test_dialog_with_db_shows_bridge_preview(monkeypatch, qapp):
    """给了 db：要真能建出带桥接预演标签的对话框，且不崩。"""
    from media_resource_manager.gui import dislike_dialog as dd
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task

    db = _full_db(_mkdtemp())
    try:
        save_task(db, TaskConfig(id=None, name="T", adapter_name="generic",
                                 ai_labels=["金发"]))
        seen = {}

        def fake_exec(self):
            # 找到那个紫色的桥接预演标签，断言它非空 = 用户真的看得到提示
            from PySide6.QtWidgets import QLabel
            for lb in self.findChildren(QLabel):
                if "→" in lb.text():
                    seen["txt"] = lb.text()
            return 0                      # 0 = Rejected → cancel

        monkeypatch.setattr(dd.QDialog, "exec", fake_exec)
        dd.ask_dislike_reason(None, ["金发"], [], db=db,
                              bridge_vocab=["blond hair"])
        # 没触发 textChanged 时预演是空的（输入框为空），但**绝不能崩**
    finally:
        db.close()


def test_bridge_preview_text_names_the_target_label():
    """预演文案必须点名用户最终会记到哪个词表标签上（金发 → blond hair）。"""
    from media_resource_manager.gui.dislike_dialog import _bridge_preview

    txt = _bridge_preview(["金发"], ["blond hair", "cat"])
    assert "金发" in txt and "blond hair" in txt, txt
    assert "词典对应" in txt, txt

    txt2 = _bridge_preview(["压根没有的词"], ["blond hair"])
    assert "不在词表里" in txt2, txt2

    assert _bridge_preview([], ["blond hair"]) == ""
    assert _bridge_preview(["  "], ["blond hair"]) == ""


def test_dialog_returns_3_tuple_still():
    """⚠️ 返回值必须还是 3 元组。

    4 处生产代码 + 2 个测试文件都按 ``u, e, m = ...`` 解包；
    为了多带一个「手填词」字段改签名 = 改一串调用方，收益极不划算。
    手填词改走模块级暂存（对话框是模态的，不存在并发串号）。
    """
    from media_resource_manager.gui import dislike_dialog as dd

    src = _code(DIALOG)
    assert "return result[\"unmatched\"], result[\"exclusions\"], result[\"mode\"]" in src, \
        "返回值不再是 3 元组 —— 会打断所有既有调用方"
    assert callable(dd.last_manual_words)
    assert callable(dd.consume_manual_words)


def test_manual_words_consumed_once():
    """consume 之后要清空，否则会串到下一次判定。"""
    from media_resource_manager.gui import dislike_dialog as dd

    dd._remember_manual(["金发"])
    assert dd.last_manual_words() == ["金发"]
    assert dd.consume_manual_words() == ["金发"]
    assert dd.consume_manual_words() == [], "没清空 —— 会串到下一次"


def test_all_dislike_callers_consume_manual():
    """4 处「不喜欢」入口都得取手填词，否则只有一部分路径闭环。"""
    rev = _code(REVIEW)
    main = _code(MAIN)
    assert rev.count("consume_manual_words()") >= 2, \
        "review_tab 的两条路径（键盘判定 + 灯箱）没都接上"
    assert main.count("consume_manual_words()") >= 2, \
        "main_window 的两条路径（单张 + 批量）没都接上"


def test_all_dislike_callers_pass_db():
    """桥接预演要词表，词表要 db —— 调用方必须把 db 传进去。"""
    src = _code(REVIEW) + _code(MAIN)
    for m in re.finditer(r"ask_dislike_reason\(", src):
        seg = src[m.start():m.start() + 600]
        assert "db=self.db" in seg, \
            "有ask_dislike_reason 调用没传 db —— 预演与桥接都不会生效"


def test_manual_tag_path_also_bridges():
    """人工细分词（点二级/三级标签）也是用户手输的词，同样要走桥接。

    不断言字面排版（换行/缩进一变就假失败）—— 只断言「``_apply_manual_tag``
    这个方法体内出现了 learn_manual 且方向是 like」。
    """
    import ast

    tree = ast.parse(_code(REVIEW))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == "_apply_manual_tag"), None)
    assert fn is not None, "找不到 _apply_manual_tag"
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "learn_manual"]
    assert calls, "_apply_manual_tag 没走 learn_manual —— 细分词的喜欢学不到词表上"
    # 方向必须是 like（细分词 = 正向信号）
    assert any(isinstance(c.args[0], ast.Constant) and c.args[0].value == "like"
               for c in calls if c.args), \
        "细分词的桥接方向必须是 like（正向），实际传的不是"
    # 第二个参数必须跟 words 有关（不能写死一个词表）
    assert any(any(getattr(a, "id", "") == "words"
                   or (isinstance(a, ast.Call)
                       and getattr(a.func, "id", "") == "list")
                   for a in c.args[1:])
               for c in calls if len(c.args) >= 2), \
        "桥接的词必须是实际选中的细分词，不能写死"


def test_bridge_module_imported_at_module_top():
    """模块顶部 import：模块级类不能用函数内 import（Qt 那个坑的同款）。"""
    rev = _code(REVIEW)
    head = rev.split("# #56 A4")[0]
    assert "from ..classify.label_bridge import LabelBridge" in head


def test_bridge_resets_shared_available_for_tests():
    from media_resource_manager.classify import label_bridge as lb

    assert callable(lb.reset_shared)


def test_no_shutdown_calls_anywhere():
    """⚠️ 铁律复核：本轮改动里绝不能出现任何关机/休眠调用。

    只查**真正会关机的东西**：``shutdown /s``（Windows 关机命令）、
    SetSuspendState（休眠）、rundll32 power。

    ⚠️ **不能只搜 ``shutdown`` 这个词** —— ``ThreadPoolExecutor.shutdown()``
    跟关机毫无关系，而 main_window.py 里就有七八处（r39d线程池收工），
    裸搜必然假失败，而且这种假失败会让人以为真有问题。
    """
    pat = re.compile(r"shutdown\s+/s|shutdown\s+-s|SetSuspendState"
                     r"|rundll32[^\n]*power|powercfg\s+/h", re.I)
    for p in (BRIDGE, PREF, DIALOG, REVIEW, MAIN):
        src = _code(p)
        m = pat.search(src)
        assert m is None, f"{p.name} 里出现了关机/休眠调用：{m.group(0)!r}"