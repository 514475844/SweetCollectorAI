"""V0.0.71 护栏：AI 看图自己说词 → 用户勾选 → 写进口味库。

用户原话「让它自己生成词，然后在首页让我来进行筛选，看哪些词是我需要的，
这才是让 AI 学习我自己图库的目的」。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_auto_vocab_module_exists_with_three_methods():
    s = _read("media_resource_manager/auto_vocab.py")
    for fn in ("scan_vocab", "scan_local", "scan_api"):
        assert f"def {fn}" in s, f"缺 {fn} 实现"
    assert "def scan(" in s and "def apply_choices" in s


def test_backend_routes_registered():
    w = _read("media_resource_manager/webui.py")
    for ep in ("/api/autovocab/start", "/api/autovocab/job", "/api/autovocab/apply"):
        assert ep in w, f"后端没注册 {ep}"
    assert "def _api_autovocab_start" in w
    assert "_th.Thread(target=_run, daemon=True, name=\"mrm-autovocab\")" in w, "扫描没走后台线程"


def test_apply_uses_user_origin():
    s = _read("media_resource_manager/auto_vocab.py")
    assert "PREF_ORIGIN_USER" in s, "勾选是用户明确意图，来源该是 user_typed"
    # V0.0.78：写入改走 _set_pref_abs 的设值模式（位置参数 "pos"/"neg"），
    # 修重复勾选权重累加；这里钉行为口径，不钉具体关键字参数写法。
    assert '"pos"' in s and '"neg"' in s, "正/负偏好方向没写对"
    assert "set_preference(db, 0," in s


def test_frontend_api_and_component():
    s = _read("media_resource_manager/webapp/src/api.js")
    for m in ("autoVocabStart", "autoVocabJob", "autoVocabApply"):
        assert m in s, f"api.js 缺 {m}"
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    assert "methods = [" in c and "'vocab'" in c and "'local'" in c and "'api'" in c, "三种方式没做成可选"
    assert "amount = ref('1000')" in c, "扫描量没做成可选"
    assert "autoVocabApply(chosenYes.value, chosenNo.value)" in c, "勾选没接到应用"


def test_home_and_learning_entry():
    """首页即学习看板（/ 重定向 /learning），入口放学习页顶部。"""
    main = _read("media_resource_manager/webapp/src/main.js")
    assert "path: '/', redirect: '/learning'" in main, "首页路由变了，入口位置要重新确认"
    lr = _read("media_resource_manager/webapp/src/views/Learning.vue")
    assert "AutoVocab" in lr and "让 AI 认识我的图库" in lr, "首页/学习页没放入口"
    assert "vocab-entry" in lr, "入口卡片样式缺"


def test_version_and_changelog():
    import re
    v = _read("media_resource_manager/__init__.py")
    m = re.search(r'VERSION = "0\.0\.(\d+)"', v)
    assert m and int(m.group(1)) >= 71, "版本号未升到 0.0.71"
    ch = _read("CHANGELOG.md")
    assert "V0.0.71" in ch, "更新日志缺 V0.0.71"
