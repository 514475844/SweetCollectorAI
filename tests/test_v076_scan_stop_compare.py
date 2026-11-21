"""V0.0.76 护栏：全库扫描可随时停止 + 成果落盘 + 看板两种学习法数据对比。

用户场景：出门无人值守，让 AI 把全库图扫一遍；回来要能随时暂停/停止，
且已扫出的词不能丢；同时要在看板上看到"标签统计 vs AI 造词"的实在数据差别。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_jobs_cancel_api():
    j = _read("media_resource_manager/jobs.py")
    assert "def cancel(" in j, "作业表缺 cancel()"
    assert "def cancelled(" in j, "作业表缺 cancelled()"


def test_scan_local_supports_stop_and_persist():
    av = _read("media_resource_manager/auto_vocab.py")
    assert "stop_check" in av, "scan_local 未支持停止检查"
    assert "def save_last(" in av, "缺扫描成果落盘函数"
    assert "def last_scan(" in av, "缺上次成果读取函数"
    assert "def compare_stats(" in av, "缺两种学习法对比函数"
    assert 'LAST_KEY = "autovocab.last"' in av, "落盘键名不符"
    # 扫描循环里有取消判断
    body = av[av.index("def scan_local"):av.index("def scan_api")]
    assert "stop_check()" in body, "scan_local 循环未检查取消位"
    # 每 50 张落盘一次
    assert "scanned % 50" in body, "scan_local 未做定期落盘"


def test_cancel_endpoint_registered():
    w = _read("media_resource_manager/webui.py")
    assert '"/api/autovocab/cancel"' in w, "缺取消端点路由"
    assert '"/api/autovocab/last"' in w, "缺上次成果端点路由"
    assert "def _api_autovocab_cancel(" in w, "缺取消实现"
    assert "def _api_autovocab_last(" in w, "缺上次成果实现"
    # 学习看板带出对比数据
    assert '"compare": compare' in w, "看板接口未输出对比数据"


def test_frontend_stop_button_and_last_load():
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    assert "autoVocabCancel" in c, "前端未接停止接口"
    assert "停止（成果保留）" in c, "缺停止按钮文案"
    assert "autoVocabLast" in c, "前端未加载上次成果"
    a = _read("media_resource_manager/webapp/src/api.js")
    assert "/api/autovocab/cancel" in a, "api.js 缺 cancel 封装"
    assert "/api/autovocab/last" in a, "api.js 缺 last 封装"


def test_board_compare_card():
    v = _read("media_resource_manager/webapp/src/views/Learning.vue")
    assert "两种学习方式的数据对比" in v, "看板缺对比卡"
    assert "全新词" in v, "对比卡缺「全新词」指标"
    assert "compare.tag_based.distinct_tags" in v, "对比卡未用后端真实数据"
