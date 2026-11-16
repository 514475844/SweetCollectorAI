"""R128：给「R119 全量里 R68 那两条红」钉上**根因级**护栏。

背景（本轮实测，不是推测）
--------------------------
`full_20261015_r125.log` 比 `r120` 多出的 4 条红里，有 2 条是
`test_r68_skin_presets` 的「画面里不该出现别的皮肤强调色 #8b7cf8」。
R126 已用二分法把污染源锁到 `tests/test_r119_gallery_scale.py` 这个文件，
但**机理未定位**。本轮定位到两条独立成因，都在这里钉死：

成因 A（测试隔离）
    `tools/seed_stress_data.py::seed()` 里 `os.environ["MRM_HOME"] = ...`
    是**进程级**写入且从不还原。pytest 全量是单进程 ⇒ 这个种子目录泄漏给
    后面**每一个**测试文件。`test_r68` 的 `MainWindow()` 于是不再打开自己
    的库，而是打开 R119 灌的压测库（里面有 status=done 的任务）。

成因 B（产品配色）
    调色板 `st_done` 写死 `#8b7cf8`，**逐位等于** violet 皮肤的强调色；
    `palette()` 里还额外把它覆盖成 `ac`（当前皮肤强调色）。而状态点
    pixmap 是**建行那一刻**画的、换肤不重建行 ⇒ 换到现代风仍是紫点，
    切到真原生（classic）画面里也仍留一颗自绘强调色点 —— 与
    「原生模式完全交给系统样式」契约直接冲突。

为什么护栏要钉**这两条**而不是"画面里数像素"
------------------------------------------
像 r68 那样抓图数像素，只有"窗口里恰好有 done 任务"时才红；
换个库、换个顺序就绿了 —— 判据依赖数据，等于没判。
这里直接钉**成因**：状态色不许撞强调色、seed 不许改进程环境。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STATUS_KEYS = ("st_running", "st_queued", "st_idle",
               "st_done", "st_error", "st_stopped")


def _accent_hexes():
    """四个/五个预设各自的强调色 hex（判"撞色"的基准）。"""
    from media_resource_manager.gui import theme
    return {theme.preset_accent(k): theme.SKINS[theme.preset_accent(k)][0]
            for k in theme.PRESETS}


# ══════════════════════════════════════════════════════════════════
# 成因 B：状态点颜色不许等于任何皮肤的强调色
# ══════════════════════════════════════════════════════════════════
def test_native_mode_status_dots_never_equal_any_preset_accent():
    """**真原生模式**下，任何状态点色都不许等于某个预设的强调色。

    真原生（classic）的契约是「控件全部交给系统样式画」⇒ 画面里
    **零**自绘强调色，连自己的都不行。早先 `st_done` 无条件跟随皮肤
    （`palette()` 里 `"st_done": ac`），classic 下就是一颗钢蓝点，
    直接违反该契约（R68 那条「真原生画面里出现自绘强调色」的红）。
    """
    from media_resource_manager.gui import theme
    accents = {k: v.lower() for k, v in _accent_hexes().items()}
    old = theme._NATIVE_MODE
    try:
        theme._NATIVE_MODE = True
        for dark in (False, True):
            pal = theme.palette(dark)
            for k in STATUS_KEYS:
                v = (pal.get(k) or "").lower()
                assert v, f"{'dark' if dark else 'light'} 调色板缺 {k}"
                hit = [n for n, hexv in accents.items() if hexv == v]
                assert not hit, (
                    f"原生模式 {'dark' if dark else 'light'} 的 {k}={v} 命中预设"
                    f"强调色 {hit} —— 原生画面里不许出现自绘强调色")
    finally:
        theme._NATIVE_MODE = old


def test_done_dot_follows_skin_outside_native_mode():
    """非原生皮肤下「已完成」点必须跟随皮肤（P14b 契约的补强版）。

    只钉「跟随」不够 —— 跟随还必须**真的画出来**（见下一条重画护栏）。
    这里钉取值层：不同皮肤给出不同值，且等于该皮肤的强调色。
    """
    from media_resource_manager.gui import theme
    old = theme._NATIVE_MODE
    try:
        theme._NATIVE_MODE = False
        for skin, accent in (("violet", "#8b7cf8"), ("blue", "#4a90e2"),
                             ("teal", "#22c3a6")):
            theme._CURRENT_SKIN = skin
            got = theme.palette(False)["st_done"].lower()
            assert got == accent, (
                f"皮肤 {skin} 下 st_done={got}，期望跟随强调色 {accent}")
    finally:
        theme._NATIVE_MODE = old
        theme._CURRENT_SKIN = "violet"


def test_done_dot_is_repainted_when_skin_changes(monkeypatch, tmp_path):
    """★ 本轮真因的一半：状态点 pixmap 换肤必须**真的重画**。

    `TaskRow` 建行时就把颜色烤进 QPixmap，换皮肤时 QSS 的 unpolish/polish
    刷不动位图 ⇒ 不显式重画的话，切到现代风看到的「已完成」仍是上一套
    皮肤的紫罗兰 —— R68「画面里残留别家强调色 #8b7cf8」就是这么来的。

    判据钉**位图里的实际像素**：换肤前后取所有 QLabel 位图的中心色，
    旧强调色必须消失、新强调色必须出现。这条不看代码文本，
    只看用户真正看到的那张图。
    """
    from PySide6.QtWidgets import QApplication, QLabel
    from media_resource_manager.gui import theme
    from tools.seed_stress_data import seed

    home = tmp_path / "repaint_home"
    seed(home, n_tasks=2, n_images=3, n_reviews=2, n_rules=1, n_audit=2,
         n_prefs_per_tier=2, n_quarantine=1, verbose=False)
    monkeypatch.setenv("MRM_HOME", str(home))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    monkeypatch.setenv("MRM_NO_QUEUE_LOOP", "1")
    app = QApplication.instance() or QApplication([])

    from media_resource_manager.db import Database
    from media_resource_manager.gui.main_window import MainWindow
    db = Database(os.path.join(str(home), "mrm.db"))
    try:
        db.execute("UPDATE tasks SET status='done' WHERE id="
                   "(SELECT MIN(id) FROM tasks)")
    finally:
        db.close()

    def _pixmap_colors(win):
        cols = set()
        for lb in win.findChildren(QLabel):
            pm = lb.pixmap()
            if pm is None or pm.isNull():
                continue
            img = pm.toImage()
            p = img.pixel(img.width() // 2, img.height() // 2)
            cols.add("#%02x%02x%02x" % ((p >> 16) & 255,
                                        (p >> 8) & 255, p & 255))
        return cols

    win = MainWindow()
    try:
        win.hide()
        theme.apply_preset(app, "violet_look")
        for _ in range(4):
            app.processEvents()
        before = _pixmap_colors(win)
        theme.apply_preset(app, "modern")
        for _ in range(4):
            app.processEvents()
        after = _pixmap_colors(win)

        violet = theme.SKINS["violet"][0].lower()
        blue = theme.SKINS["blue"][0].lower()
        assert violet in before, (
            f"前置失败：紫罗兰风下位图里没有 violet 点（{sorted(before)}）"
            f" —— 要么库里没 done 任务，要么抓色方式错了")
        assert blue in after, (
            f"换到现代风后位图里没有新强调色 {blue}（{sorted(after)}）"
            f" —— 状态点没跟随皮肤")
        assert violet not in after, (
            f"换到现代风后位图里仍残留旧强调色 {violet}（{sorted(after)}）"
            f" —— 位图没重画（烤死的 pixmap）")
    finally:
        win.close()


def test_done_dot_color_is_readable_on_both_schemes():
    """撞色改掉了，可读性不能跟着掉：浅底/暗底都要看得见。

    用 WCAG 相对亮度算对比度（不是凭感觉），阈值取 3.0 ——
    状态点是 10px 的小圆点，属非文本图形，3:1 是可辨识线。
    """
    from media_resource_manager.gui import theme

    def _lum(h):
        h = h.lstrip("#")
        ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]

        def f(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(ch[0]) + 0.7152 * f(ch[1]) + 0.0722 * f(ch[2])

    def _ct(a, b):
        la, lb = _lum(a), _lum(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    light = theme.palette(False)
    dark = theme.palette(True)
    assert _ct(light["st_done"], light["surface"]) >= 3.0, (
        "浅色下已完成点对卡片底对比度 %.2f < 3.0：%s"
        % (_ct(light["st_done"], light["surface"]), light["st_done"]))
    assert _ct(dark["st_done"], dark["surface"]) >= 3.0, (
        "暗色下已完成点对面板底对比度 %.2f < 3.0：%s"
        % (_ct(dark["st_done"], dark["surface"]), dark["st_done"]))


# ══════════════════════════════════════════════════════════════════
# 成因 A：种子脚本不许把数据目录泄漏进进程环境
# ══════════════════════════════════════════════════════════════════
def test_seed_does_not_leak_home_into_process_env(tmp_path):
    """`seed()` 返回后，`MRM_HOME` / `MRM_NO_MIGRATE` 必须与调用前一致。

    这是 R119 → R68 跨文件污染的**直接成因**：pytest 全量单进程，
    env 一旦被改，后面每个文件的 `MainWindow()` 都开错库。
    判据钉「调用前后一致」，不钉具体值 —— 调用方自己设过 env 的情况
    也必须原样保留（还原成"没有"不等于还原成功，还原成"原值"才算）。
    """
    from tools.seed_stress_data import seed
    saved = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    try:
        os.environ.pop("MRM_HOME", None)
        os.environ.pop("MRM_NO_MIGRATE", None)
        home = tmp_path / "seed_home"
        seed(home, n_tasks=2, n_images=3, n_reviews=2, n_rules=1,
             n_audit=2, n_prefs_per_tier=2, n_quarantine=1, verbose=False)
        assert os.environ.get("MRM_HOME") is None, (
            "seed() 把 MRM_HOME 永久写进了进程环境（=%s）—— "
            "同进程后续测试文件会打开这个压测库（R119→R68 污染的真因）"
            % os.environ.get("MRM_HOME"))
        assert os.environ.get("MRM_NO_MIGRATE") is None, (
            "seed() 把 MRM_NO_MIGRATE 永久写进了进程环境")
        # 调用方**自己**设过 env 时，seed 结束后必须还是调用方那个值
        os.environ["MRM_HOME"] = str(tmp_path / "caller_home")
        seed(home, n_tasks=1, n_images=1, n_reviews=1, n_rules=1,
             n_audit=1, n_prefs_per_tier=1, n_quarantine=1, verbose=False)
        assert os.environ["MRM_HOME"] == str(tmp_path / "caller_home"), (
            "seed() 没把调用方自己设的 MRM_HOME 还原回去（=%s）"
            % os.environ.get("MRM_HOME"))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_seed_still_actually_uses_the_home_it_was_given(tmp_path):
    """反向前置：不污染的前提下，seed 必须**真的**把数据灌进给定目录。

    少了这条，上一条可以靠「seed 什么都不做」轻易变绿。
    """
    from media_resource_manager.db import Database
    from tools.seed_stress_data import seed
    home = tmp_path / "seed_home"
    counts = seed(home, n_tasks=3, n_images=6, n_reviews=3, n_rules=2,
                  n_audit=3, n_prefs_per_tier=3, n_quarantine=2,
                  verbose=False)
    assert counts["images"] >= 6, f"图片没灌够：{counts}"
    db = Database(os.path.join(str(home), "mrm.db"))
    try:
        n = int(db.query("SELECT COUNT(*) c FROM tasks")[0]["c"])
        assert n >= 3, f"给定目录里只有 {n} 个任务 —— seed 没灌对地方"
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════
# 端到端：真的有 done 任务时，换皮肤后画面里仍不许出现别家强调色
# （= r68 那两条的"不怕数据"版本：自己造 done 任务，不靠别的文件的库）
# ══════════════════════════════════════════════════════════════════
def test_done_task_dot_survives_skin_switch_without_accent_leak(
        monkeypatch, tmp_path):
    """库里真有一个「已完成」任务时，切到现代风/真原生，画面不许有强调色。

    这条把成因 A 与 B 合起来端到端复现：以前只有"恰好打开 R119 的压测库"
    才红；现在自己造一个 done 任务，任何环境下都会验证同一条契约。
    """
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui import theme
    from tools.seed_stress_data import seed

    home = tmp_path / "e2e_home"
    seed(home, n_tasks=2, n_images=3, n_reviews=2, n_rules=1, n_audit=2,
         n_prefs_per_tier=2, n_quarantine=1, verbose=False)

    monkeypatch.setenv("MRM_HOME", str(home))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    monkeypatch.setenv("MRM_NO_QUEUE_LOOP", "1")
    app = QApplication.instance() or QApplication([])

    from media_resource_manager.db import Database
    from media_resource_manager.gui.main_window import MainWindow
    db = Database(os.path.join(str(home), "mrm.db"))
    try:
        db.execute("UPDATE tasks SET status='done' WHERE id="
                   "(SELECT MIN(id) FROM tasks)")
    finally:
        db.close()

    win = MainWindow()
    try:
        win.hide()
        for _ in range(4):
            app.processEvents()
        # 前置：这个窗口里**确实**有一个已完成任务（否则本条是空转）
        rows = win.db.query("SELECT COUNT(*) c FROM tasks "
                            "WHERE status='done'")[0]["c"]
        assert int(rows) >= 1, "前置失败：库里没有 done 任务，本条无从谈起"

        accents = _accent_hexes()
        for preset in ("modern", "classic"):
            theme.apply_preset(app, preset)
            for _ in range(4):
                app.processEvents()
            img = win.grab().toImage()
            # modern 允许画**自己**的强调色（那是设计），只禁别家的；
            # classic 是真原生，任何自绘强调色都不许出现。
            mine = theme.preset_accent(preset)
            if theme.preset_is_native(preset):
                banned = dict(accents)
            else:
                banned = {k: v for k, v in accents.items() if k != mine}
            for _name, hexv in banned.items():
                r, g, b = (int(hexv[i:i + 2], 16) for i in (1, 3, 5))
                hits = sum(
                    1 for y in range(0, img.height(), 2)
                    for x in range(0, img.width(), 2)
                    if (((img.pixel(x, y) >> 16) & 255),
                        ((img.pixel(x, y) >> 8) & 255),
                        img.pixel(x, y) & 255) == (r, g, b))
                assert hits == 0, (
                    f"皮肤 {preset} 的画面里出现了强调色 {hexv}（{hits} 个取样点）"
                    f" —— 库里有已完成任务时状态点又把别家/自绘强调色画出来了")
    finally:
        win.close()
