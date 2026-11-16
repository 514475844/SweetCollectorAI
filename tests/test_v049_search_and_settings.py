"""V0.0.49 护栏：搜索框集成语义搜索 + 设置页保存逻辑一致。

用户两条：
  1) 「做飞牛 Seek 那样的功能怎么成了单独按键『按意思找』？我说了直接集成到搜索框。」
     → 删除独立开关，按输入内容自动判断：结构化查询走精确搜索，自然语言走向量搜索。
  2) 「设置里好多改了就直接保存，那『放弃修改』『保存本页』有什么用？」
     → 同页不能两种保存行为。在线模型下拉从即时保存改成草稿制，与同页其它字段一致；
       Web 页本来就是纯即时保存（底部已用说明文字替代按钮），再加标题处说明。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / "media_resource_manager/webapp/src/components/ImageGrid.vue"
SET = ROOT / "media_resource_manager/webapp/src/views/Settings.vue"


# ─────────────── ① 搜索框自动语义 ───────────────
def test_no_separate_semantic_button():
    s = GRID.read_text(encoding="utf-8")
    assert "按意思找" not in s.split("<template>", 1)[1].split("</template>", 1)[0], \
        "界面上还有「按意思找」独立按钮"
    assert "toggleSem" not in s, "手动切换开关的函数还在"


def test_auto_semantic_detection():
    s = GRID.read_text(encoding="utf-8")
    assert "semAuto" in s and "computed" in s, "缺自动判断"
    assert "STRUCT_RE" in s, "缺结构化查询识别"
    # 结构化关键字要能识别
    seg = s.split("const STRUCT_RE =", 1)[1].split("\n", 1)[0]
    for kw in ("tag", "site", "score"):
        assert kw in seg, f"结构化识别漏了 {kw}"


def test_sem_mode_derived_from_query():
    s = GRID.read_text(encoding="utf-8")
    seg = s.split("const semMode = computed", 1)[1].split("\n", 1)[0]
    assert "semAuto" in seg, "semMode 没从输入内容派生"
    assert "if (semMode.value)" in s, "load 里没走语义分支"


def test_placeholder_mentions_both_modes():
    s = GRID.read_text(encoding="utf-8")
    assert "搜索画面特征" in s and "tag:" in s, \
        "搜索框提示没说清既能按特征也能按标签"


# ─────────────── ② 设置页保存一致 ───────────────
def test_online_model_dropdown_uses_draft():
    s = SET.read_text(encoding="utf-8")
    old = "@change=\"save({ ai_api_model: $event.target.value })\""
    assert old not in s, "在线模型下拉还是即时保存，与同页其它字段不一致"
    assert "@change=\"onField({ k: 'ai_api_model'" in s, "下拉没改成草稿制"


def test_web_page_states_instant_save():
    s = SET.read_text(encoding="utf-8")
    assert "本页改动即时保存" in s, "Web 页没说清是即时保存"
    assert "本页每一项都是改完立即保存" in s, "底部即时保存说明丢了"


def test_savebar_buttons_only_on_draft_pages():
    s = SET.read_text(encoding="utf-8")
    assert "instantTab" in s and "INSTANT_TABS" in s
    seg = s.split('v-if="instantTab"', 1)[1].split("</template>", 1)[0]
    assert "放弃修改" not in seg, "即时保存页不该摆「放弃修改」按钮"
