# V0.0.63 护栏：图库「已识别」标识（用户拍板方案 A+D）
#
# A = 右上角紫色 ✓ 徽章（只标已识别；待识别不标徽章，满屏灰问号太吵）
# D = 文字行胶囊（已识别=紫 / 待识别=灰），小图档文字行被裁时靠悬停 tooltip
#
# 数据链路：后端 /api/view 列表必须带 ai 字段（0=待识别 / 1=已识别），
# 前端卡片按 it.ai 渲染。任何一环断了卡片就全显示成「待识别」或全亮。
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_api_images_returns_ai_field():
    """列表 SQL 两条分支都要 SELECT ai_state，_img_item 要映射成 ai。"""
    src = _read("media_resource_manager/webui.py")
    # V0.0.70 起又多了「向上翻页」分支（before 反向 SQL），合法第三处。
    # 钉的是「每条列表分支都要带 ai_state」，不钉死具体条数。
    assert src.count("review_reason, ai_state FROM images") >= 2, \
        "图库列表 SQL 缺 ai_state（每条分支都要带上）"
    assert '"ai": int(' in src, "_img_item 没把 ai_state 映射成 ai 字段"


def test_card_has_badge_and_chip():
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    # 方案A：徽章只在已识别时出现
    assert 'v-if="it.ai === 1" class="ai-mark"' in grid, \
        "右上角徽章必须只标已识别（待识别不标）"
    # 方案D：文字行胶囊两种状态都显示
    assert "已识别" in grid and "待识别" in grid
    assert '.ai-chip.none' in grid, "待识别灰胶囊样式缺失"
    assert '.ai-mark {' in grid, "徽章样式缺失"


def test_badge_does_not_collide_with_tick():
    """勾选圈在 right:6px（宽22px），徽章必须让位（right ≥ 30px）不重叠。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    import re
    m = re.search(r"\.ai-mark \{[^}]*right:\s*(\d+)px", grid)
    assert m, ".ai-mark 没定义 right"
    assert int(m.group(1)) >= 30, "徽章会压住右上角勾选圈"
