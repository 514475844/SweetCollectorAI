"""r37：锁死这轮修掉的四件事。

用户报的 6 条 BUG 里，这四组是同一条线上的：

1. 「软件不能正常使用」+「不能设置最高 20 条任务同时跑」
   - 并发上限写死 `setRange(1, 6)` → 现在必须能选到 20；
   - `queue.set_concurrency()` 没判事件循环是否已关闭，关窗口后从 GUI 改并发
     会抛 `RuntimeError: Event loop is closed`（gui-crash 12:12:30 三连）。
2. 「很多格式无法在灯箱中查看」
   - 真机库里 699 webp + 180 avif + 37 jpg，而 Qt 打包的 imageformats
     插件**没有 AVIF** → 灯箱必须能用 Pillow 兜底解码 avif/webp。
3. 「图库灯箱的左右切换不能用」
   - `Lightbox.step()` 用 `% len(self.rows)`，rows 只有 1 条时按 ← → 永远不动；
   - 灯箱是独立顶级窗口，弹出后焦点可能还在主窗口 → ← → 根本进不来。
4. 「图库下一页就卡住 / 900 张就卡」
   - 缩略图同步解码。改成每帧 25ms 预算的分帧回填，翻页必须立刻出格子。
"""
import os

import pytest


@pytest.fixture(autouse=True)
def qapp(monkeypatch):
    """建任何 Qt 控件前都必须有 QApplication，否则 Qt 直接 fatal（一行报错都不打）。"""
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    app = QApplication.instance() or QApplication([])
    # 这些弹窗在自动化里不能真弹（exec 会挂住整个测试）
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.No)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    return app


@pytest.fixture()
def home(monkeypatch, tmp_path):
    """把用户数据目录指到临时目录（别碰真机那 900 张图）。"""
    h = tmp_path / "mrm_home"
    h.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", str(h))
    return h


# ---------------------------------------------------------------- 灯箱 / 格式
def _make_imgs(tmp_path):
    """造 webp + avif + jpg 三个真图文件（纯 Pillow，不联网）。"""
    from PIL import Image
    d = tmp_path / "imgs"
    d.mkdir(parents=True, exist_ok=True)
    outs = {}
    for name, fmt in (("a", "WEBP"), ("b", "AVIF")):
        im = Image.new("RGB", (8, 6))
        for x in range(8):
            for y in range(6):
                im.putpixel((x, y), (x * 30 % 256, y * 40 % 256, 128))
        p = d / f"{name}.img"      # 刻意用假后缀：站点给的扩展名经常不实
        im.save(str(p), format=fmt)
        outs[fmt] = p
    # jpg 用真后缀（下面有「已是 jpg 就不转」的断言）
    jp = d / "c.jpg"
    Image.open(str(outs["WEBP"])).save(str(jp), format="JPEG")
    outs["JPEG"] = jp
    return outs


def test_pillow_fallback_decodes_avif_and_webp(tmp_path, qapp):
    """灯箱对 Qt 读不了的格式（AVIF）必须能靠 Pillow 显示出来。"""
    from media_resource_manager.gui.lightbox import _pixmap_via_pillow
    imgs = _make_imgs(tmp_path)
    assert imgs["AVIF"].exists() and imgs["WEBP"].exists()
    for fmt, p in imgs.items():
        pm = _pixmap_via_pillow(p)
        assert pm is not None, f"{fmt} Pillow 回退解码失败"
        assert not pm.isNull(), f"{fmt} 转出来的 pixmap 是 null"
        assert pm.width() >= 8, f"{fmt} pixmap 尺寸不对: {pm.width()}"


def _rows_with(paths):
    return [{"path": str(p), "id": i, "width": 8, "height": 6,
             "score": 0.5, "source_url": "https://example.com/x", "tags": ""}
            for i, p in enumerate(paths)]


def test_lightbox_left_right_moves_with_many_rows(tmp_path, qapp):
    """多张图时 ← → 必须真的换图（以前只有 1 张时 % len == 不变）。"""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui.lightbox import Lightbox

    imgs = _make_imgs(tmp_path)
    lb = Lightbox(_rows_with(list(imgs.values())), 0, None)
    try:
        assert len(lb.rows) == 3
        lb.show()
        QApplication.processEvents()
        first = lb.i
        # 直接把按键事件发给灯箱（模拟焦点在灯箱上）：→ → ←
        # 期望 1 → 2 → 1（第三次是绕回最后一页后再退一张）
        for key, want in ((Qt.Key.Key_Right, first + 1),
                          (Qt.Key.Key_Right, first + 2),
                          (Qt.Key.Key_Left, first + 1)):
            ev = QKeyEvent(QEvent.Type.KeyPress, int(key),
                           Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(lb, ev)
            assert lb.i == want % 3, f"按键后下标 {lb.i}，期望 {want % 3}"
        # 跳到最后一页再 → ，必须绕回第一张（% len 循环）
        while lb.i != 2:
            lb.step(1)
        lb.step(1)
        assert lb.i == 0, f"循环翻页没绕回开头：{lb.i}"
        # 翻页按钮在只有 1 张时必须禁用（否则按了没反应 = 「不能用」）
        lb2 = Lightbox(_rows_with([list(imgs.values())[0]]), 0, None)
        try:
            assert len(lb2.rows) == 1
            assert lb2.b_prev.isEnabled() is False
            assert lb2.b_next.isEnabled() is False
        finally:
            lb2.close()
    finally:
        lb.close()


def test_lightbox_avif_renders_not_null(tmp_path, qapp):
    """灯箱渲染 avif 时不能落到「无法读取该图片」。"""
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui.lightbox import Lightbox

    imgs = _make_imgs(tmp_path)
    lb = Lightbox(_rows_with([imgs["AVIF"]]), 0, None)
    try:
        lb.show()
        QApplication.processEvents()
        assert lb.i == 0
        assert lb.rows[0]["path"].endswith(".img")
        # render 走的是 Pillow 回退：pixmap 非空 → 不会显示"无法读取"
        pm = lb.img._src
        assert pm is not None and not pm.isNull()
    finally:
        lb.close()


# ------------------------------------------------------- 保存时转 JPG（#142）
def test_convert_file_to_jpg_webp_avif(tmp_path):
    from media_resource_manager.convert_img import convert_file_to_jpg
    imgs = _make_imgs(tmp_path)
    n_webp, n_avif = imgs["WEBP"], imgs["AVIF"]
    p1, p2 = convert_file_to_jpg(n_webp)[0], convert_file_to_jpg(n_avif)[0]
    assert p1.suffix == ".jpg" and p2.suffix == ".jpg"
    assert p1.exists() and p2.exists()
    # 旧后缀文件必须清掉，不能留下 a.img 和 a.jpg 两个
    assert not n_webp.exists() and not n_avif.exists()


def test_convert_file_to_jpg_skips_existing_jpg(tmp_path):
    from media_resource_manager.convert_img import convert_file_to_jpg
    imgs = _make_imgs(tmp_path)
    j = imgs["JPEG"]
    out, conv = convert_file_to_jpg(j)
    assert conv is False
    assert out == j and out.exists()


def test_convert_file_to_jpg_never_raises_on_garbage(tmp_path):
    from media_resource_manager.convert_img import convert_file_to_jpg
    bad = tmp_path / "notanimage.jpg.txt"
    bad.write_bytes(b"hello")
    out, conv = convert_file_to_jpg(bad)
    assert conv is False
    # 垃圾文件必须原样留着（转换失败绝不能把用户文件弄没）
    assert out.exists() and out.read_bytes() == b"hello"


def test_convert_tree_to_jpg(tmp_path):
    from media_resource_manager.convert_img import convert_tree_to_jpg
    imgs = _make_imgs(tmp_path)
    root = tmp_path / "tree"
    root.mkdir()
    import shutil
    for p in imgs.values():
        shutil.copy2(str(p), str(root / p.name))
    n, fail = convert_tree_to_jpg(root)
    assert n >= 2, f"应该至少转 2 张，实际 {n}"
    assert fail == 0
    left = list(root.iterdir())
    assert left
    for x in left:
        assert x.suffix.lower() == ".jpg", f"{x.name} 没转成 jpg"


# ------------------------------ 并发上限（R178：20 → 50）+ Event loop closed 崩溃
def test_concurrency_upper_bound_is_at_least_50():
    """「同时运行任务」必须能选到 **50**（R178 用户明确要求；r37 时只要 20）。

    ⚠ 这条判据在 R178 棒**改判**（不是放宽）：原文断言写死 `setRange(1, 20)`，
    而用户 2026-10-22 要求「最高可设 50」⇒ 上限 50 才是正确行为，那条断言
    从此必假红。它真正要防的是「上限被改小/退回写死值」，所以判据改成
    「≥50 且 UI 与常量同源」——比原来更强（原来允许任何人把常量改成 6 而
    只要源码里还留着 `setRange(1, 20)` 那行就绿）。
    """
    from media_resource_manager.gui import main_window as mw
    assert mw.MAX_PARALLEL_TASKS >= 50, "并发上限被改小了（R178 要求 50）"
    src = open(mw.__file__, encoding="utf-8").read()
    assert f"MAX_PARALLEL_TASKS = {mw.MAX_PARALLEL_TASKS}" in src
    assert "setRange(1, MAX_PARALLEL_TASKS)" in src, "UI 上限没走同一个常量"
    assert "setRange(1, 20)" not in src, "写死的旧上限必须删掉"


def test_set_concurrency_when_loop_closed_is_silent():
    """关库后事件循环已关闭 → set_concurrency 必须静默返回，不能抛。"""
    import asyncio
    from media_resource_manager.scheduler.queue import TaskQueue

    q = TaskQueue(cfg=None, max_concurrent=1)
    q.start()

    async def _run():
        q.set_concurrency(4)
        assert q.max_concurrent == 4

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_run())
    loop.close()
    # 现在伪装成「loop 已关闭 + 队列线程还在」（就是关窗口那一刻的状态）
    q._loop = loop
    q.set_concurrency(20)          # 不许抛 Event loop is closed
    assert q.max_concurrent == 20
    try:
        q.stop()
    except Exception:
        pass


# ------------------------------------------------- 图库分帧缩略图（#141 性能）
@pytest.fixture()
def win_with_gallery(qapp, home):
    """离屏起一个真 MainWindow（数据全在临时目录里）。"""
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
    win._keep_panel = win._build_task_form_panel()
    db = win.db
    lib = home / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    for i in range(6):
        im = Image.new("RGB", (6 + i, 4 + i))
        for x in range(6 + i):
            im.putpixel((x % 6, 0), (x * 20 % 256, 100, 60))
        im.save(str(lib / f"g{i}.jpg"), format="JPEG")
    yield win, db, lib
    try:
        win.close()
    except Exception:
        pass


def test_thumb_cache_key_matches_the_writer(win_with_gallery, tmp_path):
    """锁死「两级 sha1」那个命中判据 bug。

    以前 `_thumb_cache_hit` 算的是 sha1(sha1(sha))[:20]，而真正写缓存的
    `square_thumb_pixmap` 写的是 sha1(sha)[:20] —— 两级 hash 永远对不上，
    缓存命中从不生效，于是**每次翻页都重新解码一遍原图**（avif 单张几十
    ms，200 张十几秒，就是「900 张卡的不行」的元凶）。这条测试保证两边
    用同一套文件名。
    """
    from PIL import Image
    from media_resource_manager.config import user_data_dir
    from media_resource_manager.gui.table_view import square_thumb_pixmap
    win, db, lib = win_with_gallery
    p = lib / "keytest.jpg"
    Image.new("RGB", (20, 10)).save(str(p), format="JPEG")
    sha = "deadbeef" * 8
    dig = win._thumb_digest(sha, p)
    assert len(dig) == 20
    # 判据说命中 → 文件必须在那个路径上
    thumbs = user_data_dir() / "thumbs"
    # 【R117 P2-7】缓存 tag 由 `_sq` 改为 `_fit`：网格缩略图改为**等比完整**
    # （crop=False），文件名必须跟着改，否则 `_thumb_cache_hit` 与写缓存的
    # 两边又对不上（旧 _sq 是方块版，内容已错）。
    # R247：分桶后不能再拼平铺路径，按名字算（cache_path 就是写入端那条规则）
    from media_resource_manager import thumbpath as _tp
    cf = _tp.cache_path(thumbs, f"{dig}_fit128.png")
    assert win._thumb_cache_hit(p, sha, 128, thumbs) is False, \
        "文件还不存在就判成命中共识"
    # 让 square_thumb_pixmap 真的写一份缓存（走 digest 参数，不再二次 hash）
    pm = square_thumb_pixmap(p, 128, thumbs, key=sha, digest=dig, crop=False)
    assert pm is not None and not pm.isNull()
    assert cf.exists(), f"缓存文件名和 _thumb_cache_hit 判据对不上：{cf}"
    assert win._thumb_cache_hit(p, sha, 128, thumbs) is True, "命中判据仍算错"


def test_thumb_memory_lru_is_bounded(win_with_gallery):
    """内存 LRU 必须有上限（百万级图库不能无上限涨内存）。"""
    win, _db, _lib = win_with_gallery
    cap = win._THUMB_MEM_MAX
    assert cap > 0
    for i in range(cap + 50):
        win._thumb_mem_put(f"k{i}", object())     # 用普通对象占位
    assert len(win._thumb_mem) <= cap, f"LRU 没淘汰：{len(win._thumb_mem)}"


def test_pump_dispatches_to_thread_pool(win_with_gallery):
    """没缓存的原图必须丢线程池解，不能在主线程里一张张解（那就几十秒）。"""
    from PIL import Image
    from PySide6.QtWidgets import QApplication
    win, db, lib = win_with_gallery
    assert win._thumb_pool is not None, "缩略图解码线程池没建起来"
    for i in range(30):
        im = Image.new("RGB", (400, 300), (i * 7 % 256, 60, 90))
        n = f"pool{i}.jpg"
        im.save(str(lib / n), format="JPEG")
        db.execute("INSERT INTO images (id,task_id,path,bytes,source_url) "
                   "VALUES (?,1,?,4096,?)", (300 + i, str(lib / n),
                                             "https://example.com/" + n))
    win.g_list_mode = False
    win._gal_sig = None
    win.load_gallery()
    win._pump_thumbs()
    in_flight = len(getattr(win, "_thumb_slots", None) or {})
    assert in_flight > 0, "一张都没丢给线程池（又退回主线程同步解了）"
    # 把在飞的结果收回来。注意：后台 worker 是**真线程**，光 processEvents
    # 转圈不够，得让它真的跑一会儿（真机探针里一次解码约 100ms），所以这里
    # 每次循环真睡 20ms，最多等 10 秒。
    def _icons():
        return sum(1 for i in range(win.g_grid.count())
                   if not win.g_grid.item(i).icon().isNull())

    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        if not getattr(win, "_thumb_pending", None) and _icons() > 0:
            break
        win._pump_thumbs()
        QApplication.processEvents()
        time.sleep(0.02)
    assert not getattr(win, "_thumb_pending", None), "缩略图队列没补完"
    assert win._thumb_timer is None, "补完了定时器没停"
    # fixture 只造了 6 个图片文件（没插库），库里就这 30 行
    assert win.g_grid.count() == 30, f"格子数 {win.g_grid.count()}，应为 30"
    icons = _icons()
    assert icons >= 1, (
        f"一张缩略图都没生成（icons={icons}, 在飞槽位已清空 = 结果被误丢）")


def test_page_turn_stays_fast(win_with_gallery):
    """翻页的**同步**部分必须快（用户感知的「卡」就是这一段）。

    回归前：`load_gallery` 里命中缓存就同步 `QPixmap(缓存小图)`，
    200 张实测 790ms 主线程冻结；基于本轮优化后应当只留建格子。
    """
    import time
    from PIL import Image
    from PySide6.QtWidgets import QApplication
    win, db, lib = win_with_gallery
    for i in range(60):
        im = Image.new("RGB", (600, 400), (i * 3 % 256, 70, 110))
        n = f"fast{i}.jpg"
        im.save(str(lib / n), format="JPEG")
        db.execute("INSERT INTO images (id,task_id,path,bytes,source_url) "
                   "VALUES (?,1,?,4096,?)", (400 + i, str(lib / n),
                                             "https://example.com/" + n))
    win.g_list_mode = False
    win._gal_sig = None
    win.load_gallery()
    for _ in range(300):
        if not getattr(win, "_thumb_pending", None):
            break
        win._pump_thumbs()
        QApplication.processEvents()
    # 第二次翻同一批（现在都进内存/磁盘缓存了）—— 这段就是翻页手感
    win._gal_sig = None
    t0 = time.perf_counter()
    win.load_gallery()
    dt = (time.perf_counter() - t0) * 1000
    assert dt < 500, f"翻页同步耗时 {dt:.0f}ms，还是太卡"


def test_close_event_stops_thumb_pump(win_with_gallery):
    """关窗口必须停掉分帧定时器（否则关库后还在解图）。"""
    win, db, lib = win_with_gallery
    win._stop_thumb_pump()
    win._thumb_pending = [(None, lib / "x.jpg", "")]
    from PySide6.QtWidgets import QApplication
    win.show()
    QApplication.processEvents()
    win.close()
    assert getattr(win, "_thumb_timer", None) is None
