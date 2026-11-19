# V0.0.67 护栏：图库「阅读进度」保留 + 各端各存各的
#
# 用户需求：图越来越多，图库的阅读进度要能保留；网页就是网页、客户端就是
# 客户端，各端各存各的互不干扰。
# 实现要点（本护栏锁定这些语义，防止后续被改坏）：
#   ① 进度存 localStorage（不进后端库、不加数据库字段）——
#      内置客户端用独立 QWebEngine profile（落 data/webengine），网页端用浏览器
#      自己的 profile，两边 localStorage 物理隔离，天然各存各的。
#   ② 按 view 分键存（gallery / review / quarantine 互不串）。
#   ③ 以「视口顶部那张图的 id」为锚点，而不是纯像素 —— 图库持续新增图片时
#      纯 scrollTop 会错位，按 id 找回那张图才不跑偏。
#   ④ 锚图不在已加载批次时按需补载再定位（受上限保护，防内存爆）。
#   ⑤ 切页/恢复窗口内不写进度，防 App.vue 路由强制 scrollTop=0 误覆盖。
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_scroll_progress_uses_localstorage_not_backend():
    """进度走 localStorage，不新增后端 API / 数据库字段。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "localStorage.getItem(PROGRESS_KEY)" in grid, "进度未从 localStorage 读"
    assert "localStorage.setItem(PROGRESS_KEY" in grid, "进度未写入 localStorage"
    # 不应为此新增后端接口
    assert "api.galleryProgress" not in grid, "进度不该走后端 API（各端各存各的靠本地隔离）"


def test_scroll_progress_keyed_by_view():
    """每个视图独立存进度，切视图互不覆盖。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "map[props.view]" in grid, "进度未按 view 分键，多视图会互相覆盖"


def test_scroll_progress_uses_anchor_id():
    """以图片 id 为锚，而非只存像素位置。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "figure.cell[data-id=" in grid, "缺少按 id 找锚图的选择器"
    assert "querySelector(`figure.cell[data-id=" in grid, "恢复时未按 id 定位锚图"
    assert "top: 0" not in grid or "p.top" in grid, "锚图找不到时应退回像素位置"


def test_scroll_progress_restore_caps_loading():
    """恢复时按需补载必须有上限，防止把十万张全拉进内存。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "RESTORE_MAX" in grid, "补载无上限，有内存爆风险"
    assert "items.value.length < RESTORE_MAX" in grid, "补载循环未受上限约束"


def test_scroll_progress_guard_against_route_reset():
    """路由切页强制归零不能把进度写成 0。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "restoringView" in grid, "缺少恢复窗口保护标志"
    assert "if (top < 8 && map[props.view]) return" in grid, \
        "滚到顶部未跳过写入，路由清零会误覆盖进度"


def test_scroll_progress_listener_lifecycle():
    """滚动监听要挂上、卸载要解绑，且节流写入。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "addEventListener('scroll', onScrollProgress" in grid, "未挂滚动监听"
    assert "removeEventListener('scroll', onScrollProgress" in grid, "卸载未解绑滚动监听"
    assert "setTimeout(saveProgress, 200)" in grid, "写入未节流"


def test_scroll_progress_restored_after_first_batch():
    """首批数据到位后才恢复位置；reload 返回 promise 供 await。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "await load(false)" in grid, "onMounted 未 await 首批加载"
    assert "await restoreProgress()" in grid, "加载后未恢复进度"
    assert "return load(false)" in grid, "reload 未返回 promise，切页恢复会抢跑"


def test_restore_survives_route_reset_and_dom_timing():
    """实测修复：单帧恢复会被路由清零/DOM 未就绪打掉，必须多轮+守护。"""
    grid = _read("media_resource_manager/webapp/src/components/ImageGrid.vue")
    assert "async function _ensureAnchorLoaded" in grid, "缺少锚图补载函数"
    assert "for (let i = 0; i < 16 && !done; i++)" in grid, "恢复未做多轮重试"
    assert "for (let g = 0; g < 6; g++)" in grid, "定位后缺少守护帧（防路由清零）"
    assert "Math.abs(m.scrollTop - t) > 24" in grid, "守护未检测位置偏移并重设"


def test_version_bumped_to_067():
    v = _read("media_resource_manager/__init__.py")
    assert 'VERSION = "0.0.67"' in v, "版本号未升到 0.0.67"
