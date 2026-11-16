"""R117 第9项：图库**列表模式**连按箭头不卡死/不白屏（用户实测最严重项）。

现象：列表模式下用下箭头浏览几张图后，整个图库卡死、没有新图、变白屏；
**刷新 / 切网格再切回 / 切到审核再切回，全都不管用**（状态坏死了）。

本文件钉住「列表模式连续下/上箭头 N 次，预览路径不抛异常、不死循环、
不进入白屏（pixmap 被清空 / 源图丢失）状态」。

⚠ 判据钉**行为**不钉数值：
  · 箭头每次都必须让选中行真的动（不许卡在原地 = 用户看到"没反应"）；
  · 预览控件不许进入「有选中行但源图为空」的白屏态；
  · 上到顶/下到底不许越界报错、不许死循环；
  · 列表↔网格来回切后仍能正常导航。

为什么单独一个文件（R118 口径）：全量 pytest 绝不能被本文件拖挂 ——
所有驱动都走真实信号但不进 exec()，且每个用例都有硬上限。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _mk_images(tmp_path, n=12):
    """造 n 张**本地合成图**（不连外网），尺寸/比例混合。"""
    from PIL import Image
    d = tmp_path / "imgs"
    d.mkdir(parents=True, exist_ok=True)
    specs = [(80, 120), (200, 90), (64, 64), (300, 200), (120, 300)]
    out = []
    for i in range(n):
        w, h = specs[i % len(specs)]
        p = d / f"img_{i:03d}.png"
        Image.new("RGB", (w, h), (i * 7 % 255, 90, 160)).save(str(p))
        out.append(p)
    return out


def _seed_db(db, paths, task_id=1):
    for i, p in enumerate(paths):
        db.execute(
            "INSERT INTO images (task_id, path, source_url, width, height, "
            "bytes, tags, collected_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
            (task_id, str(p), f"http://local/{i}", 80, 120, 4096, "[]"))


def _boot(monkeypatch, tmp_path):
    """起一个 offscreen 的主窗口（不接自动任务、不弹框）。"""
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    monkeypatch.setenv("MRM_NO_QUEUE_LOOP", "1")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    for old in app.topLevelWidgets():
        try:
            for tmr in old.findChildren(QTimer):
                tmr.stop()
            old.close()
        except Exception:
            pass
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app_: True
    return mw, app


def _win_with_images(monkeypatch, tmp_path, n=12):
    """造一个列表模式 + 有图的图库窗口（不进 exec 事件循环）。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from media_resource_manager.db import Database
    db = Database(tmp_path / "r117.db")
    paths = _mk_images(tmp_path, n)
    _seed_db(db, paths)

    # ⚠ 必须用**真 QWidget** 承载：防抖定时器是 `QTimer(self)`，self 必须是
    # QObject 才能当 parent。用 MainWindow.__new__ 绕过 __init__ 的写法会让
    # QTimer(self) 抛错，然后代码退回**同步**老路径 —— 测试看着过了，
    # 其实测的是回退分支，防抖根本没被验证（第一版就踩了这个）。
    from PySide6.QtWidgets import QWidget

    class _Win(QWidget):
        """只挂图库列表模式真正用到的那几个属性，不跑 MainWindow.__init__。

        ⚠ 常量（_GAL_PREVIEW_DEBOUNCE_MS 等）**必须从 MainWindow 继承**。
           早先这里逐个拷方法、没继承类属性，于是 `self._GAL_PREVIEW_DEBOUNCE_MS`
           抛 AttributeError、被 handler 里的 `except Exception` 吞掉 → 静默退回
           **同步**老路径。测试全绿，防抖却压根没被执行（第一版就踩了）。
        """
        _gallery_table_preview = mw.MainWindow._gallery_table_preview
        _gallery_table_preview_now = mw.MainWindow._gallery_table_preview_now
        _gallery_selected_ids = mw.MainWindow._gallery_selected_ids
        _preview_decode_cap = mw.MainWindow._preview_decode_cap
        _preview_pixmap_bounded = mw.MainWindow._preview_pixmap_bounded
        _GAL_PREVIEW_DEBOUNCE_MS = mw.MainWindow._GAL_PREVIEW_DEBOUNCE_MS
        _GAL_PREVIEW_DECODE_CAP = mw.MainWindow._GAL_PREVIEW_DECODE_CAP

    win = _Win()
    win.db = db
    win.g_list_mode = True
    from media_resource_manager.gui.table_view import make_table
    tbl = make_table(None, None, None)
    tbl.resize(700, 400)
    tbl.show()
    app.processEvents()
    win.g_table = tbl
    from media_resource_manager.gui.table_view import PreviewLabel
    prev = PreviewLabel("（选中一行看预览）")
    prev.resize(300, 300)
    prev.show()
    app.processEvents()
    win.g_prev_img = prev
    win.g_prev_txt = type("T", (), {"setText": lambda self, s: None})()
    win._gal_prev_pending = None
    win._gal_prev_timer = None
    win._gal_rows = [{"id": i + 1, "path": str(p)}
                     for i, p in enumerate(paths)]
    # 网格模式的最小替身：切到网格时 _gallery_selected_ids 会读 g_grid.selectedItems()
    win.g_grid = type("G", (), {"selectedItems": lambda self: []})()
    # ⚠ 必须照 __init__ 里那条真线接上：图库就是靠它驱动预览的
    #   （`self.g_table.itemSelectionChanged.connect(self._gallery_table_preview)`）。
    #   漏了它，测的就不是"按箭头"这条路径 —— 防抖压根不会被触发。
    tbl.itemSelectionChanged.connect(win._gallery_table_preview)
    return win, tbl, prev, db, app


def _drain(app, ms=200):
    """把防抖定时器等出来（比 ms 长的都等完）。"""
    import time
    end = time.time() + ms / 1000.0 + 0.15
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def _fill(win, tbl, rows):
    """把行灌进表格（走真实 QTableWidgetItem，选中第一行）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QTableWidgetItem
    from media_resource_manager.gui.table_view import HEADERS
    tbl.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c in range(len(HEADERS)):
            item = QTableWidgetItem(str(row.get("id", "")))
            item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            tbl.setItem(r, c, item)
    tbl.setCurrentCell(0, 0)
    win._gallery_table_preview()
    return tbl


def test_rapid_arrows_coalesce_into_single_decode(monkeypatch, tmp_path):
    """★ 根因护栏：连按 N 次箭头，重活只跑「停手后的最终行」那一次。

    修之前的病根：`_gallery_table_preview` 挂在 itemSelectionChanged 上，
    每按一次箭头就同步「查库 + 整张原图解码 + 缩放」，几十次全堆在 GUI
    线程 → 事件队列塞满 → 白屏且刷新/切模式都排在后面（用户原话）。

    这里把「真正的绘制次数」数出来：连按 30 次 → `_gallery_table_preview_now`
    只该被调用**一次**（画最终选中行），而不是 30 次。
    判据钉行为（合并 + 不丢最终选择），不钉具体毫秒数。
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    win, tbl, prev, db, app = _win_with_images(monkeypatch, tmp_path, n=30)
    try:
        _fill(win, tbl, win._gal_rows)
        _drain(app)

        # 数「真正的重活」= 每次画预览都要查一次库。
        # ⚠ 不能去数 _preview_pixmap_bounded：定时器 connect 时绑的是**原**方法，
        #   事后换实例属性数不到（第一版就这么数出个 0，误判成"防抖失效"）。
        #   db.query 是 _gallery_table_preview_now 内部按 self.db 现取的，能数到。
        calls = {"n": 0}
        real_query = db.query

        def _counting(sql, params=()):
            if "FROM images WHERE id" in str(sql):
                calls["n"] += 1
            return real_query(sql, params)

        db.query = _counting

        n_rows = tbl.rowCount()
        # 真实「连按」= 一串按键事件在事件循环跑起来之前就投递完了。
        # ⚠ 千万别每按一下就 processEvents()：那中间已经过了 60ms 防抖窗口，
        #   每次都当场画出来，数出来是 N 次 —— 那不是连按，是"慢慢点"。
        #   （第一版就这么写，误判成"防抖失效"，实际是压测姿势不对。）
        pending_seen = []
        for _ in range(30):
            tbl.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress,
                                        Qt.Key.Key_Down,
                                        Qt.KeyboardModifier.NoModifier))
            pending_seen.append(getattr(win, "_gal_prev_pending", None))
            app.processEvents()
            pending_seen.append(getattr(win, "_gal_prev_pending", None))

        # 停手前最后一次取值必须是「某个待画 id」，停手后才被清掉
        assert any(p is not None for p in pending_seen), (
            f"连按期间从没记录过待画行（{pending_seen[:6]}…）—— "
            f"防抖把选择丢了，这本身就是新 bug")
        assert calls["n"] <= 1, (
            f"30 次连按期间就画了 {calls['n']} 次 —— "
            f"同步重活没被合并，GUI 线程照样会被塞爆")
        _drain(app)          # 停手，等防抖到期

        assert calls["n"] == 1, (
            f"30 次连按共查库 {calls['n']} 次（应为 1）—— 合并失效，"
            f"这正是 GUI 线程被同步解码塞爆、白屏的路径")

        # 不许丢最终选择：画出来的那张必须是停手时的最后一行
        assert win._gallery_selected_ids(), "停手后必须有选中行"
        shown = getattr(prev, "_src", None)
        assert shown is not None and not shown.isNull(), "停手后必须真的画出图"
        assert win._gal_prev_pending is None, "画完应清空待办，防抖不该重复画"
    finally:
        db.close()
        for w in (tbl, prev, win):
            try:
                w.close()
            except Exception:
                pass


def test_preview_decodes_bounded_not_full_resolution(monkeypatch, tmp_path):
    """根因护栏之二：预览**按尺寸解码**，不再整张原图进内存。

    大图（这里造 3000×2000）整张解码就是几十 MB + 一趟全图缩放；
    预览区最多 560px 宽，整张解码纯属浪费。判据钉「解码后不超上限」
    这个**行为**，不钉具体倍数。
    """
    from PIL import Image
    from PySide6.QtWidgets import QApplication

    win, tbl, prev, db, app = _win_with_images(monkeypatch, tmp_path, n=2)
    try:
        big = tmp_path / "big.png"
        Image.new("RGB", (3000, 2000), (10, 20, 30)).save(str(big))
        pm = win._preview_pixmap_bounded(big)
        assert pm is not None and not pm.isNull(), "大图也必须能解出预览（不许空图）"
        longest = max(pm.width(), pm.height())
        # ⚠ 上界用**预览区物理上限**钉，不读 _preview_decode_cap()：
        #   读被测方法自己的返回值 = 变异把 cap 放到无穷大时判据跟着放大，
        #   测试照样绿（第二版就这么漏了一个变异）。预览区最大 560px 宽
        #   （_resize_gallery_preview 的 clamp），留一倍余量 ⇒ 2000 足够钉死。
        assert 0 < longest <= 2000, (
            f"3000×2000 的图解出边长 {longest} —— 没限尺寸，等于整张原图进内存，"
            f"连按箭头时 GUI 线程照样会被塞爆")
        # 等比护栏（P2-7 红线）：解码不许把图拉变形
        assert abs((pm.width() / pm.height()) - (3000 / 2000)) < 0.02, (
            f"解码后比例 {pm.width()}x{pm.height()} 偏离原图 3:2 —— 变红了")
    finally:
        db.close()
        for w in (tbl, prev, win):
            try:
                w.close()
            except Exception:
                pass


def test_preview_still_shows_final_row_after_burst(monkeypatch, tmp_path):
    """不许「合并过头」：连按一串后停手，最终那一行必须真的显示出来。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    win, tbl, prev, db, app = _win_with_images(monkeypatch, tmp_path, n=10)
    try:
        _fill(win, tbl, win._gal_rows)
        _drain(app)
        for _ in range(7):
            app.processEvents()
            tbl.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress,
                                        Qt.Key.Key_Down,
                                        Qt.KeyboardModifier.NoModifier))
        final_row = tbl.currentRow()
        final_id = win._gal_rows[final_row]["id"]
        _drain(app)
        ids = win._gallery_selected_ids()
        assert ids == [final_id], f"停手后应停在最终行 {final_id}，实际 {ids}"
        pm = getattr(prev, "_src", None)
        assert pm is not None and not pm.isNull(), (
            "停手后最终行必须显示出来 —— 合并过头就成了新 bug")
    finally:
        db.close()
        for w in (tbl, prev, win):
            try:
                w.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 第9项


def test_list_mode_arrow_navigation_no_white_screen(monkeypatch, tmp_path):
    """★ 核心复现：列表模式连按几十次下/上箭头，不卡死、不白屏。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    win, tbl, prev, db, app = _win_with_images(monkeypatch, tmp_path, n=12)
    try:
        _fill(win, tbl, win._gal_rows)
        _drain(app)
        n_rows = tbl.rowCount()
        assert n_rows > 1, "测试前提：至少两行才能测箭头导航"

        # 白屏态定义：有选中行，但预览源图为空 ⇒ 用户看到的正是「没图/白屏」
        def _whited_out():
            ids = win._gallery_selected_ids()
            if not ids:
                return False
            pm = getattr(prev, "_src", None)
            return pm is None or pm.isNull()

        assert not _whited_out(), "填充完第一行就不该是白屏态"

        seen = set()
        steps = 0
        # ↓ 连按到底再↑ 连按回顶：两轮足够覆盖"浏览几张后卡死"的复现路径
        for direction, limit in ((Qt.Key.Key_Down, n_rows * 3),
                                 (Qt.Key.Key_Up, n_rows * 3)):
            for _ in range(limit):
                ev = QKeyEvent(QKeyEvent.Type.KeyPress, direction,
                               Qt.KeyboardModifier.NoModifier)
                app.processEvents()
                tbl.keyPressEvent(ev)
                app.processEvents()          # 让 selectionChanged → 预览跑完
                steps += 1
                row = tbl.currentRow()
                assert 0 <= row < n_rows, f"第 {steps} 步越界：currentRow={row}"
                seen.add(row)
                assert not _whited_out(), (
                    f"第 {steps} 步白屏：还有选中行 {win._gallery_selected_ids()}"
                    f" 但预览源图为空（用户报的就是这个）")

        # 判据：真的翻动了（不是卡在原地）
        assert len(seen) > 1, (
            f"按了 {steps} 次箭头但只停在 {seen} —— 等于卡死/无响应")
    finally:
        db.close()
        for w in (tbl, prev, win):
            try:
                w.close()
            except Exception:
                pass


def test_list_mode_top_and_bottom_no_overflow(monkeypatch, tmp_path):
    """↑ 到顶 / ↓ 到底：不越界、不抛异常（边界单独钉，别混在导航里）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication

    win, tbl, prev, db, app = _win_with_images(monkeypatch, tmp_path, n=6)
    try:
        _fill(win, tbl, win._gal_rows)
        _drain(app)
        n = tbl.rowCount()

        def _press(key):
            app.processEvents()
            tbl.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key,
                                        Qt.KeyboardModifier.NoModifier))
            app.processEvents()

        for _ in range(n * 2 + 5):
            _press(Qt.Key.Key_Up)
        assert tbl.currentRow() == 0, "↑ 到顶后 currentRow 应为 0"
        for _ in range(n * 2 + 5):
            _press(Qt.Key.Key_Down)
        assert tbl.currentRow() == n - 1, "↓ 到底后 currentRow 应为末行"
    finally:
        db.close()
        for w in (tbl, prev, win):
            try:
                w.close()
            except Exception:
                pass


def test_preview_survives_mode_switch_roundtrip(monkeypatch, tmp_path):
    """用户报「切网格再切回 / 切到审核再切回也救不回来」。

    这里钉：列表 →（模拟切走）→ 回列表，预览仍能正常重画，不是坏死态。
    """
    from PySide6.QtWidgets import QApplication

    win, tbl, prev, db, app = _win_with_images(monkeypatch, tmp_path, n=8)
    try:
        _fill(win, tbl, win._gal_rows)
        _drain(app)
        win.g_table.setCurrentCell(3, 0)
        win._gallery_table_preview()
        _drain(app)
        first = win._gallery_selected_ids()
        assert first, "切走前应有选中"

        # 切到网格（win.g_list_mode=False）再切回 —— 预览必须还能重画
        win.g_list_mode = False
        win._gallery_table_preview()
        _drain(app)
        win.g_list_mode = True
        win._gallery_table_preview()
        _drain(app)

        ids = win._gallery_selected_ids()
        assert ids == first, f"切模式来回后选中行变了：{first} → {ids}"
        pm = getattr(prev, "_src", None)
        assert pm is not None and not pm.isNull(), (
            "切模式来回后预览源图丢了 —— 这正是用户说的'切回来也救不回来'")
    finally:
        db.close()
        for w in (tbl, prev, win):
            try:
                w.close()
            except Exception:
                pass
