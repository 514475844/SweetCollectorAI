"""r39：锁死「审核页缩略图在主线程同步解码 → 界面假死」这条回归。

真机现场（r39 按钮全扫描 soak，前台真窗口）：
    [wd] qt_loop=STALL 13s ... STALL 41s       一次点击都没发生，全程假死
    [wd] === main thread stack ===
        PIL/WebPImagePlugin.py:412 load          <- 卡在这
        PIL/Image.py:1069 convert
        table_view.py:81 square_thumb_pixmap     <- 主线程同步解原图
        review_tab.py:370 reload                 <- 审核页整页重画
        main_window.py:4750 refresh
        main_window.py:4764 refresh_list
        main_window.py:3325 _on_queue_event      <- 队列事件驱动

根因：审核页网格用 ``square_thumb_pixmap(p, 320)``，**cache_dir 为空**＝不落
缓存、每次都拿 Pillow 在 Qt 主线程里原图解码。avif/webp 单张 30~80ms，175 张
待审核就是十几秒到几分钟；reload 又挂在「队列事件 → refresh_list」上，采集一
跑就每隔几秒来一次 → 事件循环被彻底堵死，界面点啥都没反应。

修法三件套（本文件逐条锁死）：
  1. 审核网格走**异步**缩略图：缓存命中同步取，没缓存就占位 + 丢线程池
     （MainWindow.thumb_request_async，复用图库那条 QThreadPool）；
  2. `_on_queue_event` 的列表刷新**节流**（500ms 合并，防事件风暴）；
  3. 审核页**不可见时不重建**（同 r36 对图库的做法，切页时由 _nav_go 自己 reload）。
"""
import ast
import pathlib

import pytest

pytest.importorskip("PySide6")

REVIEW_SRC = pathlib.Path("media_resource_manager/gui/review_tab.py")
MW_SRC = pathlib.Path("media_resource_manager/gui/main_window.py")


@pytest.fixture(autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """数据目录（缩略图缓存也落这儿）—— 别用真实用户数据目录。"""
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", str(d))
    monkeypatch.setattr("media_resource_manager.config.user_data_dir",
                        lambda: d)
    return d


def _calls_to(path, func):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == func):
            out.append(node)
    return out


# ---------------------------------------------------------------- 1 静态

def test_review_grid_does_not_decode_in_main_thread():
    """审核网格里不许再出现「不带缓存的同步 square_thumb_pixmap」。"""
    bad = []
    for c in _calls_to(REVIEW_SRC, "square_thumb_pixmap"):
        first = c.args[0] if c.args else None
        kw = [k.arg for k in c.keywords]
        # 兜底分支里那次（带 cache_dir、且拿的是 self._thumb_dir()）是允许的
        if isinstance(first, ast.Name) and first.id == "p" and "cache_dir" not in kw:
            bad.append(c)
    assert not bad, "仍有没落缓存的同步解图调用（界面会假死）"
    src = REVIEW_SRC.read_text(encoding="utf-8")
    assert "_thumb_icon_async(item, p)" in src


def test_main_window_binds_review_tab():
    """MainWindow 必须把 _win 反向挂给审核页，否则它拿不到线程池。"""
    assert "self._review_tab._win = self" in MW_SRC.read_text(encoding="utf-8")


def test_queue_event_refresh_is_coalesced():
    """队列事件走节流，不再是每个事件都 full refresh。"""
    src = MW_SRC.read_text(encoding="utf-8")
    assert "def _schedule_list_refresh" in src
    assert "self._schedule_list_refresh()" in src


# ---------------------------------------------------------------- 2 真窗口行为

@pytest.fixture()
def win_with_review(qapp, home, monkeypatch):
    """离屏起一个真 MainWindow + 3 张渐变图（纯色图 dHash 全同会误触去重）。"""
    import media_resource_manager.gui.main_window as mw
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme

    mw._ensure_single_instance = lambda a: True
    app = QApplication.instance()
    for old in app.topLevelWidgets():
        try:
            for tmr in old.findChildren(QTimer):
                tmr.stop()
            old.close()
        except Exception:
            pass
    theme.apply_theme(app, False)
    win = mw.MainWindow()
    win.show()
    app.processEvents()

    lib = home / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageDraw
    for i in range(3):
        im = Image.new("RGB", (120 + i * 5, 120), (30, 40, 50))
        d = ImageDraw.Draw(im)
        for y in range(0, 120, 4):
            d.line([(0, y), (120, y)], fill=(y * 2 % 256, 100, 90 - y))
        im.save(str(lib / f"r{i}.jpg"), format="JPEG")

    yield win, win.db, lib
    try:
        win.close()
    except Exception:
        pass


def test_thumb_request_async_uses_thread_pool(win_with_review):
    """thumb_request_async 真把活丢进线程池，并登记槽位。"""
    win, _db, _lib = win_with_review
    from PySide6.QtWidgets import QListWidgetItem

    p = sorted(_lib.glob("*.jpg"))[0]
    it = QListWidgetItem()
    digest = win._thumb_digest("", p)
    mkey = win._thumb_mem_path(digest, 64)
    before = len(win._thumb_slots or {})
    ok = win.thumb_request_async(p, digest, 64, win._thumb_dir(), it, mkey)
    assert ok is True
    assert len(win._thumb_slots) == before + 1


def test_slots_reset_clears_inflight(win_with_review):
    """重建列表前丢掉在飞槽位：结果回来时 item 可能已删，别 setIcon 到野指针上。"""
    win, _db, _lib = win_with_review
    win._thumb_slots = {"t1": (object(), "k", "p", "")}
    win._thumb_pending = [("x", "y", "z")]
    win.thumb_slots_reset()
    assert win._thumb_slots == {}
    assert win._thumb_pending == []


def test_thumb_icon_async_does_not_block_on_uncached(win_with_review, monkeypatch):
    """没缓存时：占位 + 丢线程池，**不许同步解码**（那次假死就是这么来的）。"""
    win, _db, lib = win_with_review
    rt = win._review_tab
    rt._thumb_size = 64
    p = sorted(lib.glob("*.jpg"))[1]

    asked = []
    monkeypatch.setattr(win, "thumb_request_async",
                        lambda *a, **k: asked.append(a) or True)
    monkeypatch.setattr(win, "_thumb_mem_get", lambda m: None)

    def _boom(*a, **k):
        raise AssertionError("没缓存时绝不能在主线程同步解原图")

    monkeypatch.setattr("media_resource_manager.gui.table_view.square_thumb_pixmap",
                        _boom)
    try:
        rt._thumb_icon_async(rt.grid.addItem.__self__ and _dummy_item(), p)
    except AssertionError:
        raise
    except Exception:
        pass
    assert asked, "未缓存时必须走异步请求"


def _dummy_item():
    from PySide6.QtWidgets import QListWidgetItem
    return QListWidgetItem()


def test_refresh_skips_hidden_review_tab(win_with_review, monkeypatch):
    """审核页不可见 → refresh() 不许重建它（否则每次队列事件都重画整页）。"""
    win, _db, _lib = win_with_review
    rt = win._review_tab
    calls = []
    monkeypatch.setattr(rt, "reload", lambda *a, **k: calls.append(1))

    monkeypatch.setattr(type(rt), "isVisible", lambda self: False)
    win.refresh()
    assert calls == [], "审核页不可见时不该重建"

    monkeypatch.setattr(type(rt), "isVisible", lambda self: True)
    win.refresh()
    assert len(calls) == 1, "审核页可见时该重建一次"


def test_schedule_list_refresh_coalesces(win_with_review, monkeypatch):
    """500ms 内的连续事件只刷一次列表。"""
    win, _db, _lib = win_with_review
    calls = []
    monkeypatch.setattr(win, "refresh_list", lambda: calls.append(1))
    for _ in range(10):
        win._schedule_list_refresh()
    assert calls == [], f"节流失效：连来 10 个事件同步刷了 {len(calls)} 次"
    assert win._list_refresh_pending is True
    # 到点：清标志 + 只刷一次（标志不清会「刷一次之后再也不更新」）
    win._list_refresh_timer.timeout.emit()
    assert calls == [1], calls
    assert win._list_refresh_pending is False, "到点后必须复位，否则后续事件全被吞"
    win._schedule_list_refresh()
    assert calls == [1], "新一轮仍在 500ms 窗口内，应继续被合并"
    win._list_refresh_timer.timeout.emit()
    assert calls == [1, 1]
