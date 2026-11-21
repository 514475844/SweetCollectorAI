"""V0.0.70 护栏：进度恢复后能往上翻回之前看过的图。

用户原话「都说了，现在是测试阶段，随时结束，随时打包」—— 响应上一轮
"前面的图也在、能往上翻是另一套做法、改动大以后再定"，本轮把它做掉。
后端加 before 反向游标，前端滚到顶时 unshift 一批并保持视口。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _grid() -> str:
    return (ROOT / "media_resource_manager/webapp/src/components/ImageGrid.vue").read_text(encoding="utf-8")


def _webui() -> str:
    return (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")


def test_backend_parses_before():
    w = _webui()
    assert 'q.get("before")' in w, "后端没解析 before 参数"
    assert "before=before" in w, "before 没传给 _api_images"


def test_backend_upward_query():
    w = _webui()
    assert "before: int = 0" in w, "_api_images 缺 before 形参"
    assert "AND id>?" in w, "向上翻要查 id>before"
    assert "reversed(rows)" in w, "向上翻结果要反转成 DESC 与列表同序"


def test_frontend_has_load_prev():
    g = _grid()
    assert "async function loadPrev" in g, "前端缺向上加载函数"
    fn = g[g.find("async function loadPrev"):]
    fn = fn[:fn.find("\n}")]
    assert "before: firstId" in fn, "loadPrev 没传 before 游标"
    assert "items.value = [...fresh, ...items.value]" in fn, "新批次要 unshift 到列表头"
    assert "main.scrollTop = oldTop" in fn, "向上加载后要保持视口（补偿 scrollTop）"


def test_frontend_triggers_on_scroll_top():
    g = _grid()
    fn = g[g.find("function onScrollProgress"):]
    fn = fn[:fn.find("\n}")]
    assert "scrollTop < 240" in fn and "loadPrev()" in fn, "滚到顶没触发向上加载"


def test_frontend_resets_no_more_prev():
    g = _grid()
    assert "noMorePrev.value = false" in g, "重新加载/直跳后没重置向上翻状态"
    assert "const noMorePrev = ref(false)" in g, "缺 noMorePrev 状态"


def test_frontend_guards_upward_load():
    g = _grid()
    fn = g[g.find("async function loadPrev"):]
    fn = fn[:fn.find("\n}")]
    assert "sort.value !== 'time'" in fn, "非 time 排序不能向上翻（走 offset）"
    assert "restoringView" in fn, "恢复期间不能向上翻（会打乱定位）"


def test_keeps_prev_features():
    """不破坏上一轮的深位置直跳和启动画面。"""
    g = _grid()
    assert "async function loadAtAnchor" in g and "cursor: Number(p.id) + 1" in g
    src = (ROOT / "media_resource_manager/gui/web_shell.py").read_text(encoding="utf-8")
    assert "_C_SPLASH_BORDER" in src and "_C_SPLASH_FROST" in src
