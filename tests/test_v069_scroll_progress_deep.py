"""V0.0.69 护栏：看到 2 万张后切回来，滚动进度要能恢复。

用户原话「我已经看到2万多张了，切到审核再切回图库的时候，它需要重新
点好多次继续加载。继续加载需要我拉到底部，手动点它也不会跳转」。
根因：旧恢复逻辑写死补载上限 1200，锚图在 2 万张处永远进不了 DOM。
修法：time 排序深位置用锚图 id 当 cursor 直跳，一次请求到锚图那批。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _grid() -> str:
    return (ROOT / "media_resource_manager/webapp/src/components/ImageGrid.vue").read_text(encoding="utf-8")


def test_progress_records_sort():
    g = _grid()
    assert "sort: sort.value" in g, "进度没记排序，排序变了直跳会跳错"


def test_has_cursor_jump():
    g = _grid()
    assert "async function loadAtAnchor" in g, "缺锚图 cursor 直跳"
    assert "cursor: Number(p.id) + 1" in g, "直跳 cursor 要设为锚图 id+1（id<cursor 含锚图）"
    assert "sort.value !== 'time'" in g, "只能对 time 排序直跳（其它排序走 offset）"


def test_jump_replaces_list_and_checks_anchor():
    g = _grid()
    fn = g[g.find("async function loadAtAnchor"):]
    fn = fn[:fn.find("\n}")]
    assert "items.value = rows" in fn, "直跳后列表要换成锚图那批"
    assert "some((r) => Number(r.id) === Number(p.id))" in fn, "必须确认锚图真在返回批次里"
    assert "autoLimit.value = items.value.length" in fn, "直跳后要同步 autoLimit，继续加载按钮状态才正常"


def test_deep_position_uses_jump():
    g = _grid()
    assert "JUMP_MIN_IDX" in g, "缺深位置阈值"
    fn = g[g.find("async function restoreProgress"):]
    fn = fn[:fn.find("\n}")]
    assert "loadAtAnchor(p)" in fn and "JUMP_MIN_IDX" in fn, "深位置没走直跳"


def test_no_hard_1200_cap():
    g = _grid()
    # _ensureAnchorLoaded 里不能再用 `items.value.length < RESTORE_MAX` 这种硬卡
    fn = g[g.find("async function _ensureAnchorLoaded"):]
    fn = fn[:fn.find("\n}")]
    assert "items.value.length < RESTORE_MAX" not in fn, "补载还卡在 1200 上限"
    assert "want + RESTORE_MAX" in fn, "补载上限要按锚点 idx 动态给"


def test_restore_locks_writes_and_settles():
    g = _grid()
    fn = g[g.find("async function restoreProgress"):]
    fn = fn[:fn.find("\n}")]
    assert "restoringView = true" in fn, "恢复期间没锁进度写入"
    assert "_settleAtAnchor(p)" in fn, "定位后没守护"


def test_keeps_logo_spinner_guard_intact():
    """不破坏启动画面护栏（V0.0.68）。"""
    src = (ROOT / "media_resource_manager/gui/web_shell.py").read_text(encoding="utf-8")
    assert "_C_SPLASH_BORDER" in src and "_C_SPLASH_FROST" in src
