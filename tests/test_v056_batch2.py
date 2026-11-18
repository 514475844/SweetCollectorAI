"""V0.0.56 批2 护栏：审核类别原因 / 影子模式 / 导出可追溯 / 混淆矩阵。

个人自用软件，功能以"能落库、可解释、不误伤"为验收口径。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "media_resource_manager"
WEB = PKG / "webapp" / "src"


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── ① 审核类别原因 ────────────────────────────────────────
def test_review_reason_cat_column_and_write():
    db = _read("media_resource_manager/db.py")
    assert '"reviews": {' in db and 'reason_cat' in db
    ui = _read("media_resource_manager/webui.py")
    # 入参解析 + 落库（带 try 兜底不拖垮删图主流程）
    assert 'reason_cat = [str(w).strip()' in ui
    assert 'INSERT INTO reviews(image_id,decision,task_id,reason_cat)' in ui
    # reason_cat 入参解析与落库都在 _api_image_action 内（下一个方法前）
    seg = ui.split("def _api_image_action", 1)[1]
    next_def = seg.find("\n    def ")
    body = seg[:next_def] if next_def > 0 else seg
    assert "reason_cat" in body
    assert "reason_cat" in body.split("done = 0")[0]      # 入参解析在 done 之前


def test_dislike_modal_has_category_tags():
    s = (WEB / "components/ImageGrid.vue").read_text(encoding="utf-8")
    for cat in ("主题不符", "低质", "重复", "水印", "来源不想要", "稍后再看"):
        assert cat in s
    assert "cats: new Set()" in s
    assert "extra.reason_cat = [...m.cats]" in s
    assert "dmToggle('cats', c)" in s


# ── ② 影子模式 ────────────────────────────────────────────
def test_shadow_default_off_and_consumed():
    cfg = _read("media_resource_manager/config.py")
    assert "feedback_auto_harden: bool = False" in cfg
    ft = _read("media_resource_manager/find_terms.py")
    assert "feedback_auto_harden" in ft
    assert "fb_soft = list(dict.fromkeys(fb_soft + fb_hard))" in ft


def test_shadow_status_exposed_and_ui_toggle():
    ui = _read("media_resource_manager/webui.py")
    assert '"shadow": not bool(getattr(' in ui
    assert '"learn_enabled", "feedback_auto_harden"' in ui
    lv = (WEB / "views/Learning.vue").read_text(encoding="utf-8")
    assert "fbShadow" in lv and "影子模式" in lv
    st = (WEB / "views/Settings.vue").read_text(encoding="utf-8")
    assert "feedback_auto_harden" in st and "回避词自动拦截" in st


# ── ③ 导出可追溯 ──────────────────────────────────────────
def test_export_manifest_has_trace_fields():
    e = _read("media_resource_manager/export_kit.py")
    assert "source_url, collected_at FROM images" in e
    assert '"source_url": _src_url' in e
    assert '"site": _site' in e
    assert '"collected_at"' in e
    assert '"来源网址", "站点", "采集时间"' in e


def test_quarantine_prune_by_days():
    so = _read("media_resource_manager/site_ops.py")
    assert "def prune_quarantine(db, keep_days: int = 0)" in so
    assert "db.quarantine_prune(days)" in so
    ui = _read("media_resource_manager/webui.py")
    assert "keep_days=keep" in ui
    q = (WEB / "views/Quarantine.vue").read_text(encoding="utf-8")
    assert "pruneByDays" in q and "keepDays" in q and "清理超期图" in q
    api = (WEB / "api.js").read_text(encoding="utf-8")
    assert "siteQuarantinePrune: (keepDays)" in api


# ── ④ 混淆矩阵 ────────────────────────────────────────────
def test_confusion_matrix_and_mistakes():
    t = _read("media_resource_manager/threshold_tune.py")
    assert "def _confusion(" in t
    assert '{"tp": tp, "fn": fn, "fp": fp, "tn": tn}' in t
    assert "def _mistakes(" in t
    assert '"confusion": _confusion(pos, neg, best)' in t
    assert '"mistakes": _mistakes(db, best)' in t
    ex = (WEB / "views/Export.vue").read_text(encoding="utf-8")
    assert "tune.confusion.tp" in ex and "tune.confusion.fp" in ex
    assert "误伤的好图" in ex and "漏放的坏图" in ex


def test_confusion_math():
    import sys
    sys.path.insert(0, str(ROOT))
    from media_resource_manager import threshold_tune as tt
    pos = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95]
    neg = [0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    assert tt._confusion(pos, neg, 0.5) == {"tp": 7, "fn": 3, "fp": 4, "tn": 6}


def test_shadow_downgrades_hard_to_soft():
    import sys
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT))
    import media_resource_manager.find_terms as ft
    from media_resource_manager.config import AppConfig
    orig = ft._feedback_words
    ft._feedback_words = lambda db: (["坏词A"], ["观察词C"])
    try:
        task = SimpleNamespace(blacklist=[], ai_blacklist=[], keywords="好词",
                               ai_labels=[], ai_labels_l2=[], ai_labels_l3=[])
        sh = AppConfig(); sh.feedback_auto_harden = False
        au = AppConfig(); au.feedback_auto_harden = True
        rs = ft.collect(None, task, sh)
        ra = ft.collect(None, task, au)
        assert "坏词A" not in rs.avoid and "坏词A" in rs.soft_avoid
        assert "坏词A" in ra.avoid
    finally:
        ft._feedback_words = orig
