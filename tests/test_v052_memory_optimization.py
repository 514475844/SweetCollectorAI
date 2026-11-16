"""V0.0.52 护栏：QtWebEngineProcess.exe 内存占用优化。

图库以前把十万张缩略图全塞进 DOM 并解码，渲染进程十来分钟就吃几个 GB。
现在：只装载视口附近的图片、离屏即换成透明图、自动加载有上限、
内核 HTTP 缓存封顶落盘。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_grid_offscreen_images_are_released():
    s = (WEB / "components/ImageGrid.vue").read_text(encoding="utf-8")
    assert "const BLANK = 'data:image/gif;base64," in s
    # 真实地址只放 data-src，src 由观察器按视口控制
    assert ':data-src="thumbSrc(it)"' in s
    assert ':src="thumbSrc(it)"' not in s
    assert "img.src = BLANK" in s
    assert "img.getAttribute('src') !== real" in s
    # 用真正的滚动容器当 root，别用默认视口
    assert "closest('.main')" in s
    assert "rootMargin: '900px 0px'" in s
    assert "imgIO.disconnect()" in s


def test_auto_load_is_capped():
    s = (WEB / "components/ImageGrid.vue").read_text(encoding="utf-8")
    assert "const AUTO_LIMIT = 600" in s
    assert "if (more && items.value.length >= autoLimit.value) return" in s
    assert "function loadMoreManually()" in s
    assert '@click="loadMoreManually"' in s
    # 换新视图/搜索后必须把上限复位
    assert "autoLimit.value = AUTO_LIMIT" in s


def test_shell_cache_is_capped():
    s = _read("media_resource_manager/gui/web_shell.py")
    assert "QWebEngineProfile.HttpCacheType.DiskHttpCache" in s
    assert "setHttpCacheMaximumSize(256 * 1024 * 1024)" in s


def test_version_bumped():
    assert 'VERSION = "0.0.52"' in _read("media_resource_manager/__init__.py")
