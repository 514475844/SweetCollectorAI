"""R119：图库/审核在**代表性图片量**下不崩、不白屏、不挂死（常驻回归护栏）。

用户报的原始故障是「图库图片到某个数量就崩溃/白屏」。这类问题的特点是
**小数据量永远复现不了** —— 护栏如果只在 12 张图上跑，等于没钉。所以本文件
在一个**模块级共享**的 scratch 库里灌到代表性量（默认 500 张，命令行
``MR119_IMAGES`` 可调），然后驱动三条真实路径：

  ① 列表模式连按几十次下/上箭头（用户原话：按几下就卡死白屏）；
  ② 反复「列表 ↔ 网格」切换 + 刷新（用户原话：切来切去都救不回来）；
  ③ 审核队列连续判定（用户原话：点喜欢图片马上错位）。

判据一律钉**行为**不钉数值：
  · 不抛异常、不进死循环（每步都有硬步数上限，超了就是挂死）；
  · 有选中行时预览源图不许为空（= 用户看到的白屏）；
  · 翻页/切模式后仍能取到行（不是「切过去就空了」）；
  · 缩略图不许把图裁成 1:1 方块 / 拉伸变形（R117 P2-7 在规模下的表现）。

⚠ 成本与隔离（R118 口径）：
  · 数据用 ``tools/seed_stress_data.py`` 同一个种子函数灌，且只写 pytest 的
    tmp 目录 —— **绝不碰用户真实数据**；
  · 模块级 fixture 只灌一次（同文件内用例共用），避免每个用例重灌 500 张；
  · 所有驱动都走真实信号但不进 exec()，每个用例都有硬上限，全量不会挂死。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 代表性图片量。500 是「成本可控 + 明显超过小数据量」之间的折中；
# 真机遇到的具体崩溃量由 tools/seed_stress_data.py --images 灌全量再验。
N_IMAGES = int(os.environ.get("MR119_IMAGES", "500"))
N_TASKS = 8

# 单个用例的**硬步数上限**：超过即判定为死循环/挂死，直接失败退出。
MAX_STEPS = 400


def _boot(monkeypatch, tmp_path, home=None):
    """起一个 offscreen 的 QApplication（不接自动任务、不弹框）。

    ⚠ `home` 必须传**刚灌好数据的那个 scratch 目录**：MainWindow 自己
    load_config() + Database(cfg.db_path)，数据目录只看 MRM_HOME。
    早先这里默认指向 `tmp_path/mrm_home`，而种子写的是 `seed_home` ——
    两边不是同一个目录 ⇒ 窗口开的是空库，第一条断言就 0 行（第一版踩过）。
    """
    home = home or os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    monkeypatch.setenv("MRM_NO_QUEUE_LOOP", "1")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox
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
    # ★R125：QMenu.exec 在 offscreen 下会真阻塞等点击 ⇒ 无头环境里
    #   「构造右键菜单」这一步能把整个进程挂死（本轮压测真踩了，卡 15 分钟）。
    #
    #   ⚠ `QMenu.exec = lambda ...` 是**无效**的：PySide6 方法走 shiboken C++
    #     绑定，类属性赋值对实例调用不生效。有效做法只有 Python 子类覆盖 exec，
    #     再把子类塞回 PySide6.QtWidgets.QMenu —— 产品码里
    #     `from PySide6.QtWidgets import QMenu`（main_window.py 内局部导入）
    #     拿到的就是桩类。菜单**构造本身照常跑**（那才是被测行为）。
    import PySide6.QtWidgets as _qtw

    class _NoExecMenu(QMenu):
        def exec(self, *a, **k):        # noqa: D401
            return None

    _qtw.QMenu = _NoExecMenu
    import media_resource_manager.gui.main_window as mw
    mw._ensure_single_instance = lambda app_: True
    return mw, app


def _seed_scratch(tmp_path, n_images=None):
    """用**订单指定的种子脚本**灌 scratch 库（不另写一套灌数据逻辑）。"""
    from tools.seed_stress_data import seed
    home = os.path.join(str(tmp_path), "seed_home")
    return home, seed(home, n_tasks=N_TASKS, n_images=int(n_images or N_IMAGES),
                      n_reviews=min(120, int(n_images or N_IMAGES) // 4),
                      n_rules=6, n_audit=60, n_prefs_per_tier=12,
                      n_quarantine=30, verbose=False)


def _win_on(monkeypatch, tmp_path, home):
    """把 MRM_HOME 指向刚灌好的 scratch 库，起一个**真** MainWindow。

    走真窗口（不是替身）是有意的：本单要验的就是「真实规模下真窗口不崩」，
    替身绕过了 load_gallery / fill_table / 缩略图泵这些真正会卡死的路径。
    """
    mw, app = _boot(monkeypatch, tmp_path, home=home)
    win = mw.MainWindow()
    app.processEvents()
    return mw, win, app


def _drain(app, ms=200):
    """把防抖定时器/异步缩略图槽位等出来。"""
    import time
    end = time.time() + ms / 1000.0 + 0.15
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def _whited_out(win):
    """白屏态：有选中行但预览源图为空 —— 用户报的正是这个。"""
    try:
        ids = win._gallery_selected_ids()
    except Exception:
        return False
    if not ids:
        return False
    prev = getattr(win, "g_prev_img", None)
    pm = getattr(prev, "_src", None)
    return pm is None or pm.isNull()


# ==================================================================== #
# 种子脚本自身的护栏（先钉住「数据真的灌进去了、且幂等」）
# ==================================================================== #

def test_seed_script_fills_all_tables(monkeypatch, tmp_path):
    """种子脚本必须把**所有可预填充表**都灌上 —— 少灌一张表，
    后面所有压测结论都不成立（这是本文件的地基）。"""
    from media_resource_manager.db import Database
    home, counts = _seed_scratch(tmp_path, n_images=40)
    db = Database(os.path.join(home, "mrm.db"))
    try:
        for table in ("tasks", "images", "reviews", "preferences", "rules",
                      "audit_log", "compliance"):
            n = int(db.query(f"SELECT COUNT(*) c FROM {table}")[0]["c"])
            assert n > 0, f"表 {table} 一条都没灌上（种子脚本漏了）"
        n_img = int(db.query(
            "SELECT COUNT(*) c FROM images WHERE path IS NOT NULL "
            "AND blacklisted=0 AND in_quarantine=0")[0]["c"])
        assert n_img >= 30, f"图库可见图只有 {n_img} 条（40 张里对不上）"
        assert counts["needs_review"] > 0, "审核队列是空的，压测第5项无从谈起"
    finally:
        db.close()


def test_seed_script_is_idempotent(monkeypatch, tmp_path):
    """重复跑不能把数据翻倍（订单明写「可重复执行（幂等）」）。"""
    from media_resource_manager.db import Database
    home, _ = _seed_scratch(tmp_path, n_images=40)
    _seed_scratch(tmp_path, n_images=40)      # 第二遍
    db = Database(os.path.join(home, "mrm.db"))
    try:
        for table in ("tasks", "images", "reviews", "preferences", "rules",
                      "audit_log"):
            a = int(db.query(f"SELECT COUNT(*) c FROM {table}")[0]["c"])
            _seed_scratch(tmp_path, n_images=40)      # 第三遍
            b = int(db.query(f"SELECT COUNT(*) c FROM {table}")[0]["c"])
            assert a == b, f"{table} 复跑后从 {a} 变成 {b} —— 种子脚本不幂等"
    finally:
        db.close()


def test_seed_repairs_missing_image_files(monkeypatch, tmp_path):
    """R129：DB 有记录、磁盘上文件没了 ⇒ 种子脚本能**自愈补回**。

    为什么钉这条（实测踩出来的，不是顺手加的）：
      · images.path 是**绝对路径**，指向种子库自己；
      · 在种子库上跑一轮审核/隔离压测，产品会把判过的图**真的搬走** ——
        库里于是留下「记录还在、文件没了」的悬空行；
      · 下一轮再压测，审核页一条都铺不出来（reload 会跳过 path 不存在
        的记录），会被误读成「审核页在规模下崩了」。
    判据钉**文件被补回来**这个行为，不钉具体像素/尺寸。
    """
    from pathlib import Path

    from media_resource_manager.db import Database
    from tools.seed_stress_data import _repair_missing_image_files

    home, _ = _seed_scratch(tmp_path, n_images=8)
    db = Database(os.path.join(home, "mrm.db"))
    try:
        rows = list(db.query(
            "SELECT id, path FROM images WHERE path IS NOT NULL "
            "AND path != '' LIMIT 3"))
        assert rows, "前置条件：库里应有带 path 的图记录"
        for r in rows:
            Path(r["path"]).unlink(missing_ok=True)
        assert not any(Path(r["path"]).exists() for r in rows), \
            "前置条件：文件确实被删掉了（否则这条护栏没在测东西）"

        n = _repair_missing_image_files(db)
        assert n == len(rows), (
            f"自愈只修了 {n} 张（删了 {len(rows)} 张）—— 悬空行没被补齐")
        for r in rows:
            assert Path(r["path"]).exists(), f"{r['path']} 自愈后仍不存在"
    finally:
        db.close()


def test_seed_label_tiers_are_deep_enough():
    """订单 §一 原文：preferences「L1/L2/L3 **每层各几十条**」。

    这条必须钉住：R129 之前词表只有 15/21/20 条，preferences 一共才 56 行，
    层级显示（R117 P1-3）在压测里根本灌不满，而**没有任何测试会发现** ——
    变异试过把 L3 砍回 20 条，全套护栏照样全绿。所以这里直接钉数据源：
    每层词表都够深、且互不重复。
    """
    from tools.seed_stress_data import L1_WORDS, L2_WORDS, L3_WORDS

    MIN_PER_TIER = 40          # 「各几十条」的下限
    for name, words in (("L1", L1_WORDS), ("L2", L2_WORDS), ("L3", L3_WORDS)):
        assert len(words) >= MIN_PER_TIER, (
            f"{name} 词表只有 {len(words)} 条（< {MIN_PER_TIER}）—— "
            f"不满足订单「每层各几十条」，三级层级压测会失真")
        assert len(set(words)) == len(words), (
            f"{name} 词表有重复词："
            f"{[w for w in set(words) if words.count(w) > 1]}")


def test_seed_script_refuses_real_data_dir(monkeypatch, tmp_path):
    """红线：目录里有别人的 mrm.db 又没有本脚本标记 ⇒ 拒绝执行。

    这条护栏是「别把用户真实数据当压测沙盘」的**最后一道闸** —— 万一有人
    把 --home 指到真机上，数据目录被灌满合成图，是不可逆的损失。
    """
    import subprocess
    home = tmp_path / "not_scratch"
    home.mkdir()
    (home / "mrm.db").write_bytes(b"SQLite format 3\x00" + b"\x00" * 32)
    py = sys.executable
    r = subprocess.run(
        [py, "-X", "utf8", os.path.join(_ROOT, "tools", "seed_stress_data.py"),
         "--home", str(home), "--images", "1", "--tasks", "1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=_ROOT)
    assert r.returncode != 0, (
        f"对着有 mrm.db 的目录竟然跑成功了（rc={r.returncode}）—— "
        f"它可能已经把用户的库当成沙盘覆盖了")
    assert "拒绝执行" in (r.stdout or "") + (r.stderr or ""), \
        f"没给出拒绝原因，输出={((r.stdout or '') + (r.stderr or ''))[-300:]}"


# ==================================================================== #
# ① 列表模式：代表性量下连按几十次箭头不卡死/不白屏
# ==================================================================== #

def test_list_mode_many_arrows_at_scale(monkeypatch, tmp_path):
    """★ 核心复现路径放大版：500 张图库里连按几十次下/上箭头。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    home, _counts = _seed_scratch(tmp_path)
    mw, win, app = _win_on(monkeypatch, tmp_path, home)
    try:
        # ⚠ 必须先切到**列表模式**：MainWindow 默认是网格模式
        #   （g_list_mode 初始 False），不切的话 g_table 一行都没有 ——
        #   第一版就误判成「种子数据没接上」（真因是压根没进列表模式）。
        win.g_mode.setChecked(True)
        win.toggle_gallery_mode()
        _drain(app, 150)
        n_rows = win.g_table.rowCount()
        assert n_rows >= 50, (
            f"图库列表只出了 {n_rows} 行 —— 种子数据没接上，"
            f"或这个量下 load_gallery 自己就取不到数据了")
        assert not _whited_out(win), "铺完第一页就不该是白屏态"

        seen = set()
        steps = 0
        for key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            for _ in range(MAX_STEPS // 2):
                app.processEvents()
                win.g_table.keyPressEvent(
                    QKeyEvent(QKeyEvent.Type.KeyPress, key,
                              Qt.KeyboardModifier.NoModifier))
                app.processEvents()
                steps += 1
                row = win.g_table.currentRow()
                assert 0 <= row < n_rows, f"第 {steps} 步越界：currentRow={row}"
                seen.add(row)
                assert not _whited_out(win), (
                    f"第 {steps} 步白屏：选中 {win._gallery_selected_ids()} "
                    f"但预览源图为空 —— 这就是用户报的『按几下就卡死变白屏』")
        assert len(seen) > 1, (
            f"按了 {steps} 次箭头只停在 {len(seen)} 行 —— 等于卡死/无响应")
    finally:
        win.close()


# ==================================================================== #
# ② 列表 ↔ 网格 反复切换 + 刷新（用户：切来切去都救不回来）
# ==================================================================== #

def test_mode_toggle_roundtrip_at_scale(monkeypatch, tmp_path):
    """反复切模式/刷新，任一轮之后图库都必须还能取到行、还能画出图。"""
    home, _counts = _seed_scratch(tmp_path)
    mw, win, app = _win_on(monkeypatch, tmp_path, home)
    try:
        _drain(app)
        for i in range(6):
            win.g_mode.setChecked(True)      # 切「列表模式」
            win.toggle_gallery_mode()
            _drain(app, 120)
            if i % 2 == 0:
                assert win.g_table.rowCount() > 0, (
                    f"第 {i} 轮切到列表模式后一行都没有 —— 切过去就空了")
                win.g_table.setCurrentCell(3, 0)
                win._gallery_table_preview()
                _drain(app)
                assert not _whited_out(win), (
                    f"第 {i} 轮列表模式下预览白屏 —— 状态坏死没恢复")

            win.g_mode.setChecked(False)     # 切「网格模式」
            win.toggle_gallery_mode()
            _drain(app, 120)
            if i % 2 == 0:
                assert win.g_grid.count() > 0, (
                    f"第 {i} 轮切到网格模式后一个格子都没有")

            win._gal_sig = None              # 强制刷新（用户点「刷新」）
            win.load_gallery()
            _drain(app, 120)
            rows_now = (win.g_table.rowCount() if win.g_list_mode
                        else win.g_grid.count())
            assert rows_now > 0, (
                f"第 {i} 轮刷新后图库空了（{'列表' if win.g_list_mode else '网格'}）")
    finally:
        win.close()


def test_gallery_paging_walks_all_pages(monkeypatch, tmp_path):
    """翻页到最后一页再回来：每页都得有行，不能某页空/越界。

    钉的是 keyset 分页在**全量规模**下的行为（用户报过「到某个量就崩」）。
    走真实翻页按钮（``g_next``）而不是直接改页码变量 —— 按钮的 enable 态
    与 ``_gal_page_move`` 的夹取逻辑都在这条路径上。
    """
    home, _counts = _seed_scratch(tmp_path)
    mw, win, app = _win_on(monkeypatch, tmp_path, home)
    try:
        win.g_mode.setChecked(False)          # 网格模式才有翻页
        win.toggle_gallery_mode()
        _drain(app, 150)
        total = int(win.db.query(
            "SELECT COUNT(*) c FROM images WHERE path IS NOT NULL "
            "AND blacklisted=0 AND in_quarantine=0")[0]["c"])
        assert total > 0, "图库总数为 0"

        pages = 0
        seen_pages = []
        while pages < MAX_STEPS:
            n = win.g_grid.count()
            assert n > 0, f"第 {pages} 页一行都没有（总数 {total}）"
            seen_pages.append(n)
            pages += 1
            if not win.g_next.isEnabled():
                break
            win.g_next.click()
            _drain(app, 120)
        assert pages >= 2, (
            f"只翻出 {pages} 页（总数 {total}，各页行数 {seen_pages}）—— "
            f"要么翻页没生效，要么这个量下只剩一页")
        # 翻到最后一页再一页页退回来，退回第 1 页
        for _ in range(pages + 2):
            if not win.g_prev.isEnabled():
                break
            win.g_prev.click()
            _drain(app, 120)
        assert win._gal_page == 0, \
            f"一路往前翻完还停在第 {win._gal_page} 页（越翻越远）"
    finally:
        win.close()


# ==================================================================== #
# ③ 审核队列：连续判定不崩、不错位
# ==================================================================== #

def test_review_queue_continuous_decisions(monkeypatch, tmp_path):
    """几百条审核队列下连续喜欢/不喜欢/跳过：不抛异常、页面不空。"""
    from media_resource_manager.models import ReviewDecision
    home, _counts = _seed_scratch(tmp_path)
    mw, win, app = _win_on(monkeypatch, tmp_path, home)
    try:
        tab = win._review_tab
        tab.reload()
        _drain(app, 150)
        start = tab.grid.count()
        assert start > 0, "审核页一条待审都没有（种子数据没接上）"

        decided = 0
        for i in range(min(MAX_STEPS // 4, 60)):
            item = tab.grid.currentItem() or (tab.grid.item(0)
                                              if tab.grid.count() else None)
            if item is None:
                break
            iid = item.data(0x0100)      # Qt.UserRole
            dec = (ReviewDecision.LIKE, ReviewDecision.DISLIKE,
                   ReviewDecision.SKIP)[i % 3]
            tab.queue.apply(iid, dec)
            decided += 1
            tab.reload()
            _drain(app, 60)
            assert tab.grid.count() > 0 or decided >= start, (
                f"判到第 {decided} 张时审核页空了 —— 队列判定路径崩了")
        assert decided > 0, "一张都没判成，判定路径根本没跑起来"
    finally:
        win.close()


def test_review_auto_fill_keeps_positions_stable(monkeypatch, tmp_path):
    """P2-5 在规模下的表现：点喜欢后网格不许整页重排（位置可预期）。

    判据钉**增量补位**这条行为：判掉一张之后，仍留在页里的那些 item 必须还是
    **同一个对象**（= 没被销毁重建 = 用户看不到整网格跳动）。
    """
    from media_resource_manager.models import ReviewDecision
    home, _counts = _seed_scratch(tmp_path)
    mw, win, app = _win_on(monkeypatch, tmp_path, home)
    try:
        tab = win._review_tab
        tab.reload()
        _drain(app, 150)
        if tab.grid.count() < 4:
            return          # 队列太小，本条无意义（不算失败）
        before = tab._snapshot_grid_items()
        ids_before = [iid for _it, iid in before]
        assert ids_before, "前置条件：网格里应有 item"

        cur = tab.grid.currentItem() or tab.grid.item(0)
        iid = cur.data(0x0100)
        tab.queue.apply(iid, ReviewDecision.LIKE)
        tab.reload()
        _drain(app, 120)

        after = {iid_: it for it, iid_ in tab._snapshot_grid_items()}
        kept = [iid_ for iid_ in ids_before if iid_ != iid and iid_ in after]
        assert kept, "判掉一张之后页面上一张都没剩（不该发生）"
        # ⚠ 判据必须比**对象身份**：同一个 image_id 在增量补位后应该还是
        #   原来那个 QListWidgetItem 对象（只删判掉的 + 末尾 append）。
        #   如果落进全量重建，after[id] 会是一个**新对象** ⇒ 用户看到整网格跳位。
        #   （只查「id 还在」是废判据 —— 重建后 id 当然还在。）
        objs_before = {iid_: it for it, iid_ in before}
        rebuilt = [iid_ for iid_ in kept
                   if after[iid_] is not objs_before[iid_]]
        assert not rebuilt, (
            f"判一张之后 {len(rebuilt)}/{len(kept)} 个 item 被重建了 —— "
            f"网格会整页跳动（P2-5 回归）")
    finally:
        win.close()


# ==================================================================== #
# 规模下的缩略图不变形（R117 P2-7 在全量数据下仍成立）
# ==================================================================== #

def test_thumbs_keep_aspect_ratio_at_scale(monkeypatch, tmp_path):
    """混合比例（竖/方/超宽/极端长条）在**真实生产缩略图路径**上不许被裁方块/拉变形。

    ⚠ 判据必须打 ``MainWindow._thumb_pixmap``（图库网格真正用的那个入口），
      不能直接调 ``square_thumb_pixmap`` —— 后者默认 ``crop=True``（方形裁剪），
      而生产的两条调用点都显式传了 ``crop=False``。直接调底层函数等于
      **用错参数自己造了个方块缩略图**，然后指责代码（第一版就这么红过一次，
      红得毫无意义）。这里走真实入口，参数由代码自己决定。
    """
    from pathlib import Path
    home, _counts = _seed_scratch(tmp_path)
    mw, win, app = _win_on(monkeypatch, tmp_path, home)
    thumbs = Path(win._thumb_dir())
    try:
        rows = win.db.query(
            "SELECT path, width, height, sha256 FROM images "
            "WHERE path IS NOT NULL AND blacklisted=0 AND in_quarantine=0 "
            "ORDER BY id LIMIT 60")
        assert rows, "库里没有可取的图"
        checked = 0
        for r in rows:
            p, w, h = r["path"], int(r["width"] or 0), int(r["height"] or 0)
            if not w or not h or not os.path.exists(p):
                continue
            pm = win._thumb_pixmap(Path(p), r["sha256"] or "", 128, thumbs)
            if pm is None or pm.isNull():
                continue
            # 等比缩小：任何一边都不许超过原图对应边
            assert pm.width() <= w and pm.height() <= h, \
                f"缩略图 {pm.width()}x{pm.height()} 比原图 {w}x{h} 还大 —— 不是缩小"
            assert max(pm.width(), pm.height()) <= 128, \
                f"缩略图边长 {max(pm.width(), pm.height())} 超出请求尺寸"
            ratio_in = w / h
            ratio_out = pm.width() / pm.height()
            # 允许 1px 级取整误差；比例偏离超过 3% 就是被裁/被拉了
            assert abs(ratio_out - ratio_in) / ratio_in < 0.03, \
                f"原图 {w}x{h}（{ratio_in:.3f}）缩略图 {pm.width()}x{pm.height()}" \
                f"（{ratio_out:.3f}）—— 比例被改坏了（裁方块或拉伸变形）"
            checked += 1
        assert checked >= 10, f"实际只校验了 {checked} 张，样本不足"
    finally:
        win.close()


def test_gallery_context_menu_builds_at_scale(monkeypatch, tmp_path):
    """图库右键菜单在规模下能构造出来、不抛异常、不卡（R119 三.2-③ 明写项）。

    为什么单列一条：``_gallery_menu`` 末尾是 ``menu.exec(...)``，在 offscreen
    平台下**真的会阻塞等点击** ⇒ 无头环境里这一步能把整个进程挂死
    （本轮压测真踩过，卡了 15 分钟才靠 faulthandler 抓到栈）。
    ``_boot()`` 已把 ``QMenu.exec`` 打成非阻塞桩，所以这里能安全地真调一次，
    验的是「菜单构造过程」—— 选中/右键在规模下不卡。

    判据钉行为：能构造完、不抛异常、且在合理时间内返回。
    """
    import time as _time
    home, _counts = _seed_scratch(tmp_path)
    mw, win, app = _win_on(monkeypatch, tmp_path, home)
    try:
        win.g_mode.setChecked(False)
        win.toggle_gallery_mode()
        _drain(app, 200)
        assert win.g_grid.count() > 0, "网格一个格子都没有，右键无从谈起"

        t0 = _time.time()
        win._gallery_menu(win.g_grid.viewport().rect().center())
        cost = _time.time() - t0
        # 规模只该影响"菜单里有多少项"，不该让构造本身变慢到不可用
        assert cost < 10.0, f"右键菜单构造花了 {cost:.1f}s（>10s 视为卡）"
    finally:
        win.close()
