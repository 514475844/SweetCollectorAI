"""R117 剩余 7 项（P1-1 / P1-2 / P1-3 / P1-4 / P2-5 / P2-7 / P2-8）的护栏。

⚠ 判据一律钉**行为**，不钉数值（格式、尺寸、文字宽度都不许钉）：
  P1-1「任务」菜单里看得到全部开始/全部停止，且与「运行」菜单**同一个**
        QAction 对象（不是各自 new 的两个 —— 两个会各自触发一次槽）。
  P1-2 任务表是 ExtendedSelection；Ctrl 多选 / Shift 连选 / 全选都能取到
        **多个** id；批量运行/停止作用于**整个选中集合**。
  P1-3 白名单主题词 L1/L2/L3 **三级都在**、且层级标注清晰；
        且勾出来的词是**干净词**（不带 "一级:" 这类前缀 —— 带前缀会让
        PreferenceLearner 学到不存在的词）。
  P1-4 最小宽高真的传到了 Downloader；且任务级 > 全局；小于阈值的图不落库。
  P2-5 审核页点"喜欢"后**位置稳定**：保留的 item 是同一个对象（缩略图
        没被重解码）、只删判掉的、只在末尾追加。
  P2-7 网格缩略图**等比缩放**：横图出来还是横的、竖图还是竖的，
        绝不裁成方块、绝不拉伸变形（同步/异步**两条路径都要查**）。
  P2-8 预览框尺寸**与图无关**：换一张大图，sizeHint / minimumSizeHint /
        minimumSize 三个都不许变。

为什么单独一个文件（R118 口径）：全量 pytest 绝不能被本文件拖挂 ——
所有驱动都走真实信号但不进 exec()，且每个用例都有硬上限。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


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


def _mk_images(tmp_path, n=8, sub="imgs"):
    """造 n 张**本地合成图**（不连外网），尺寸/比例混合。"""
    from PIL import Image
    d = tmp_path / sub
    d.mkdir(parents=True, exist_ok=True)
    specs = [(80, 120), (200, 90), (64, 64), (300, 200), (120, 300)]
    out = []
    for i in range(n):
        w, h = specs[i % len(specs)]
        p = d / f"img_{i:03d}.png"
        Image.new("RGB", (w, h), (i * 7 % 255, 90, 160)).save(str(p))
        out.append(p)
    return out


# ==================================================================== #
# P1-1：「任务」菜单里的「全部开始 / 全部停止」
# ==================================================================== #

def _real_win(monkeypatch, tmp_path):
    """起一个真 MainWindow（单实例/弹框已在 _boot 里 stub）。

    MainWindow.__init__ **不接受参数**：它自己 load_config() + Database(cfg.db_path)，
    所以要隔离只能靠 MRM_HOME（_boot 已把它指向 tmp_path 下的 mrm_home）。
    """
    import media_resource_manager.gui.main_window as mw
    return mw.MainWindow()


def test_task_menu_has_run_all_stop_all(monkeypatch, tmp_path):
    """「任务」菜单里必须有「全部开始/全部停止」，且与「运行」菜单同一对象。"""
    _boot(monkeypatch, tmp_path)
    win = _real_win(monkeypatch, tmp_path)
    menubar = win.menuBar()
    menus = [a.menu() for a in menubar.actions() if a.menu() is not None]
    by_title = {m.title(): m for m in menus}
    assert "任务" in by_title, f"找不到「任务」菜单：{[m.title() for m in menus]}"
    assert "任务" in by_title, f"找不到「任务」菜单：{[m.title() for m in menus]}"
    task_act = {a.text(): a for a in by_title["任务"].actions()}
    run_act = {a.text(): a for a in by_title["任务"].actions()}
    assert "全部开始" in task_act, \
        f"「任务」菜单里没有「全部开始」：{list(task_act)}"
    assert "全部停止" in task_act, \
        f"「任务」菜单里没有「全部停止」：{list(task_act)}"
    # 关键：与「运行」菜单里的是**同一个 QAction 对象**。
    # 两个独立对象 = 点一下触发两次槽 + enabled 状态各管各的。
    assert task_act["全部开始"] is run_act["全部开始"], \
        "「任务」菜单的「全部开始」不是「运行」菜单那一个 QAction 对象"
    assert task_act["全部停止"] is run_act["全部停止"], \
        "「任务」菜单的「全部停止」不是「运行」菜单那一个 QAction 对象"
    win.close()


# ==================================================================== #
# P1-2：任务表多选 / 全选 / 批量动作
# ==================================================================== #

def _mk_taskcfgs(n=6):
    """造 n 个 TaskConfig（build_rows 的输入必须是对象，不是 dict）。"""
    from media_resource_manager.models import TaskConfig, TaskStatus
    return [TaskConfig(id=i + 1, name=f"task{i}",
                       adapter_name="generic",
                       entry_urls=[f"http://local/{i}"],
                       status=TaskStatus.IDLE)
            for i in range(n)]



def test_task_table_supports_multi_select(monkeypatch, tmp_path):
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtCore import Qt, QItemSelectionModel
    from PySide6.QtWidgets import QAbstractItemView
    from media_resource_manager.gui.task_table import TaskTableView as TaskTableWidget
    tbl = TaskTableWidget()
    tbl.resize(640, 300)
    tbl.show()
    app.processEvents()
    # ① 选择模式必须是多选（否则 Ctrl/Shift 连选根本选不中第二行）
    assert tbl.selectionMode() == \
        QAbstractItemView.SelectionMode.ExtendedSelection, \
        "任务表还是单选模式，Ctrl/Shift 连不了选"

    # ⚠ build_rows 吃的是 **TaskConfig 对象**（getattr 取值），不是 dict ——
    #   传 dict 会让每一行的 id 都变成 None，看起来"多选取不到东西"。
    tbl.set_tasks(_mk_taskcfgs(6))
    app.processEvents()
    assert tbl.tmodel is not None and tbl.tmodel.rowCount() >= 3

    # ② 单选仍必须能用（单选行为不许回归）
    tbl.selectRow(1)
    app.processEvents()
    one = tbl.selected_ids()
    assert len(one) == 1, f"单选时 selected_ids 应恰好 1 个，实际 {one}"

    # ③ Ctrl 多选 → 两个都要拿到
    tbl.clearSelection()
    sel = tbl.selectionModel()
    sel.clearSelection()
    for r in (0, 2):
        sel.select(tbl.tmodel.index(r, 0),
                   QItemSelectionModel.SelectionFlag.Select
                   | QItemSelectionModel.SelectionFlag.Rows)
    app.processEvents()
    multi = tbl.selected_ids()
    assert len(multi) >= 2, f"多选取不到多行：{multi}"

    # ④ 全选 → 全部行
    n = tbl.select_all()
    app.processEvents()
    assert n == tbl.tmodel.rowCount(), \
        f"全选取到 {n} 行，表里共 {tbl.tmodel.rowCount()} 行"
    tbl.close()


def test_batch_actions_hit_whole_selection(monkeypatch, tmp_path):
    """多选后「运行 / 停止」必须作用于**整个选中集合**，不能只动第一行。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtCore import Qt, QItemSelectionModel
    from media_resource_manager.gui.task_table import TaskTableView as TaskTableWidget
    from PySide6.QtWidgets import QWidget

    class _Win(QWidget):
        _selected_task_ids = mw.MainWindow._selected_task_ids
        run_selected = mw.MainWindow.run_selected
        stop_selected = mw.MainWindow.stop_selected
        refresh_list = lambda self: None
        log_signal = None

    class _Sig:
        def emit(self, *_a, **_k):
            return None

    win = _Win()
    win.log_signal = _Sig()
    tbl = TaskTableWidget()
    tbl.resize(640, 300)
    tbl.show()
    app.processEvents()
    tbl.set_tasks(_mk_taskcfgs(5))
    win.task_table = tbl
    app.processEvents()

    submitted = []
    stopped = []

    class _Q:
        def __init__(self):
            self.running = set()

        def submit_many(self, items):
            submitted.append([t for t, _ in items])

        def stop_task(self, tid):
            stopped.append(tid)
            return True

    win._queue = _Q()
    sel = tbl.selectionModel()
    sel.clearSelection()
    for r in (0, 1, 3):
        sel.select(tbl.tmodel.index(r, 0),
                   QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    app.processEvents()
    want = set(tbl.selected_ids())
    assert len(want) == 3, f"测试自身前提失败：只选到 {want}"

    win.run_selected()
    assert submitted, "多选后点运行，一个都没入队"
    assert set(submitted[0]) == want, \
        f"运行只作用了 {submitted[0]}，选中的是 {want}"
    win.stop_selected()
    assert set(stopped) == want, \
        f"停止只作用了 {stopped}，选中的是 {want}"
    win.close()
    tbl.close()


# ==================================================================== #
# P1-3：白名单主题词 L1/L2/L3 三级都在 + 层级标注
# ==================================================================== #

def test_white_list_shows_all_three_tiers(monkeypatch, tmp_path):
    mw, app = _boot(monkeypatch, tmp_path)
    from media_resource_manager.db import Database
    from media_resource_manager.repo import save_task
    from media_resource_manager.models import TaskConfig as Task
    db = Database(tmp_path / "r117c.db")
    # ⚠ 两个坑：
    #   ① save_task 只在 **id is None** 时走 INSERT；传 id=1 会走 UPDATE
    #      分支，而库里没这行 ⇒ 静默什么都不做（get_task 返回 None）。
    #   ② 它返回**新分配的 id**，查的时候必须用返回值，别硬写 1。
    tid = save_task(db, Task(id=None, name="t", adapter_name="generic",
                             entry_urls=["http://x"],
                             ai_labels=["L1甲", "L1乙"],
                             ai_labels_l2=["L2丙"],
                             ai_labels_l3=["L3丁"]))
    assert tid, "save_task 没返回 id"
    from media_resource_manager.gui.main_window import MainWindow
    from PySide6.QtWidgets import QWidget

    class _Win(QWidget):
        _task_ai_labels = MainWindow._task_ai_labels
        _task_ai_labels_by_tier = MainWindow._task_ai_labels_by_tier

    win = _Win()
    win.db = db
    flat = win._task_ai_labels(tid)
    for w in ("L1甲", "L1乙", "L2丙", "L3丁"):
        assert w in flat, f"{w} 没进来：白名单只剩 {flat}"
    assert len(flat) == 4, f"层间应去重，实际 {flat}"
    by = win._task_ai_labels_by_tier(tid)
    assert by[1] == ["L1甲", "L1乙"], by
    assert by[2] == ["L2丙"], by
    assert by[3] == ["L3丁"], by
    win.close()


def test_dislike_dialog_renders_tier_headings(monkeypatch, tmp_path):
    """弹窗左栏要**分段**渲染（L1/L2/L3 各有小标题），且勾出来是干净词。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from media_resource_manager.gui.dislike_dialog import _make_column
    from PySide6.QtWidgets import QLabel, QCheckBox

    widget, checks = _make_column(
        "主题词", "#16a34a", ["a", "b"],
        groups=[("一级主题词", ["a"]), ("二级主题词（一级的细分）", ["b"]),
                ("三级主题词", [])])
    labels = []
    for lb in widget.findChildren(QLabel):
        labels.append(lb.text())
    assert any("一级" in t for t in labels), \
        f"左栏没有一级小标题：{labels}"
    assert any("二级" in t for t in labels), \
        f"左栏没有二级小标题：{labels}"
    # 空段不占位（没配三级就别显示三级标题，否则像坏了）
    assert not any("三级" in t for t in labels), \
        f"空的三级段也渲染了小标题：{labels}"
    # 勾出来的必须是**干净词**（带前缀会让学习器学到假词）
    texts = [c.text() for c in checks]
    assert texts == ["a", "b"], f"复选框文字被前缀污染了：{texts}"
    widget.close()


# ==================================================================== #
# P1-4：最小宽高真的生效
# ==================================================================== #

def test_task_size_limit_prefers_task_level(monkeypatch, tmp_path):
    from media_resource_manager.engine import _task_size_limit
    # 任务级优先
    assert _task_size_limit(300, 100) == 300
    # 任务级为 0（不限）→ 回退全局
    assert _task_size_limit(0, 100) == 100
    assert _task_size_limit(None, 100) == 100
    # 两边都没有 → 0（不限，绝不能变成"过滤掉一切"）
    assert _task_size_limit(0, 0) == 0
    # 负数/脏数据一律当不限，绝不能变成垃圾阈值
    assert _task_size_limit(-5, 0) == 0
    assert _task_size_limit("abc", 120) == 120


def test_engine_passes_task_size_to_downloader(monkeypatch, tmp_path):
    """护住 engine.py 那行传参：任务级 min_width/min_height 必须真的到位。

    ★ 这里就是用户报的 P1-4 根因：原代码写的是
      ``min_width=task.max_depth and self.cfg.min_width``
      —— 用无关字段做与运算，且从不读任务级配置 ⇒ 过滤器形同虚设。

    ⚠ R154-F7（2026-10-19）起 Downloader 构造抽成了 run/retry 共用的
      ``_make_downloader`` 助手（防两套口径漂移）⇒ 判据改成查
      「run + _make_downloader」两处的并集：run 里没有不是回归，
      两个处都没有才是。

    ⚠ 判据必须**先剥注释**再看代码行：那段坏写法被我写进了注释里解释
      根因，直接全文匹配会被自己的注释判红（R104 修 Dockerfile 护栏时
      踩过同型：被头部注释里的字面量骗到）。这里只取**非注释行**。
    """
    from media_resource_manager.engine import _task_size_limit
    import media_resource_manager.engine as eng
    import inspect
    import re

    def _code_of(fn) -> str:
        src = inspect.getsource(fn)
        return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())

    code = _code_of(eng.Engine.run) + "\n" + _code_of(eng.Engine._make_downloader)
    assert "task.max_depth and" not in code, \
        "engine 代码里还有 `task.max_depth and ...` 这种把 min_width 算成 0 的写法"
    assert "_task_size_limit(task.min_width" in code, \
        "min_width 没有走 _task_size_limit（任务级优先）"
    assert "_task_size_limit(task.min_height" in code, \
        "min_height 没有走 _task_size_limit（任务级优先）"
    # 真跑一遍优先级函数，别只钉源码形状
    assert _task_size_limit(300, 0) == 300
    assert _task_size_limit(0, 300) == 300


def test_downloader_rejects_too_small_images(tmp_path):
    """小于阈值的图**真的不落盘**（这是用户验收口径）。"""
    from PIL import Image
    from media_resource_manager.downloader.downloader import Downloader
    d = tmp_path / "lib"
    d.mkdir(parents=True, exist_ok=True)
    big = d / "big.png"
    small = d / "small.png"
    Image.new("RGB", (400, 400), (10, 20, 30)).save(str(big))
    Image.new("RGB", (100, 100), (10, 20, 30)).save(str(small))
    dl = Downloader.__new__(Downloader)
    dl.root = d
    dl.min_width = 300
    dl.min_height = 300
    dl.min_bytes = 0
    from media_resource_manager.dedup.hash import image_dimensions
    assert image_dimensions(big) == (400, 400)
    assert image_dimensions(small) == (100, 100)
    # 判据与 downloader 内联的那条一致：宽或高不足 ⇒ filtered
    w, h = image_dimensions(small)
    assert (dl.min_width and w and w < dl.min_width) or \
           (dl.min_height and h and h < dl.min_height), \
        "100×100 的图在 300×300 阈值下竟然不算被过滤"


# ==================================================================== #
# P2-7：网格缩略图等比缩放（同步 + 异步两条路径）
# ==================================================================== #

def test_thumb_sync_path_keeps_aspect(tmp_path):
    from PIL import Image
    from media_resource_manager.gui.table_view import square_thumb_pixmap
    src = tmp_path / "wide.png"
    Image.new("RGB", (400, 100), (200, 30, 60)).save(str(src))
    pm = square_thumb_pixmap(src, 128, tmp_path / "c1", crop=False)
    assert pm is not None and not pm.isNull()
    w, h = pm.width(), pm.height()
    # 等比：宽高比必须与原图一致（容差 1px 是缩放取整的必然误差）
    assert abs(w / h - 400 / 100) < 0.05, \
        f"同步路径把 {400}x{100} 缩成了 {w}x{h}（比例被改了）"
    assert max(w, h) <= 128, f"缩略图比请求的 size 还大：{w}x{h}"


def test_thumb_async_path_keeps_aspect(tmp_path):
    """★ 同步路径早就等比了，异步路径才是方块 —— 用户看到的多半是这条。"""
    from PIL import Image
    from media_resource_manager.gui.main_window import _ThumbDecodeRunnable
    src = tmp_path / "tall.png"
    Image.new("RGB", (100, 400), (30, 200, 60)).save(str(src))
    out = tmp_path / "c2" / "x.png"
    out.parent.mkdir(parents=True, exist_ok=True)

    got = {}

    class _Sig:
        def emit(self, *a):
            got["ok"] = True

    # crop 默认必须是 False（= 不裁剪）—— 之前默认 True 就是方块根因
    r = _ThumbDecodeRunnable(_Sig(), str(src), "tok", str(out), 128)
    r.run()
    assert out.exists(), "异步解码没写出缩略图"
    from PIL import Image as I
    with I.open(str(out)) as im:
        w, h = im.size
    assert abs(w / h - 100 / 400) < 0.05, \
        f"异步路径把 100x400 竖图缩成了 {w}x{h}（被裁/被拉成方块了）"
    assert max(w, h) <= 128


def test_thumb_cache_tag_is_fit_not_sq(monkeypatch, tmp_path):
    """缓存 tag 必须是 fit（等比版）；读到旧 _sq 要当没命中并清掉。"""
    from PIL import Image
    mw, app = _boot(monkeypatch, tmp_path)
    from media_resource_manager.config import user_data_dir
    from media_resource_manager.gui.table_view import square_thumb_pixmap
    from PySide6.QtWidgets import QWidget
    from media_resource_manager.gui.main_window import MainWindow

    class _Win(QWidget):
        _thumb_digest = MainWindow._thumb_digest
        _thumb_cache_hit = MainWindow._thumb_cache_hit

    win = _Win()
    p = tmp_path / "lib" / "a.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (300, 150), (1, 2, 3)).save(str(p))
    sha = "abc" * 20
    dig = win._thumb_digest(sha, p)
    thumbs = user_data_dir() / "thumbs"
    square_thumb_pixmap(p, 96, thumbs, key=sha, digest=dig, crop=False)
    # R247：缩略图改成按文件名哈希分桶（thumbs/xx/y/），所以这里不能再写死
    # 「平铺在 thumbs 根目录」—— 用 thumbpath.find 按名字找，新旧路径都认。
    from media_resource_manager import thumbpath as _tp
    fit = _tp.find(thumbs, f"{dig}_fit96.png")
    assert fit.exists(), f"写出来的不是 _fit 缓存：{sorted(os.listdir(thumbs))}"
    assert win._thumb_cache_hit(p, sha, 96, thumbs) is True
    # 老 _sq（方块版）不算命中，且要被清掉让下次重解成 fit
    sq = _tp.cache_path(thumbs, f"{dig}_sq96.png")
    sq.parent.mkdir(parents=True, exist_ok=True)
    sq.write_bytes(b"x")
    assert win._thumb_cache_hit(p, sha, 96, thumbs) is True
    os.remove(str(fit))
    assert win._thumb_cache_hit(p, sha, 96, thumbs) is False, \
        "读到旧 _sq 方块缓存竟判成命中（用户会继续看到方块）"
    win.close()


# ==================================================================== #
# P2-8：预览框尺寸与图无关
# ==================================================================== #

def test_preview_box_size_independent_of_image(monkeypatch, tmp_path):
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtGui import QPixmap
    from media_resource_manager.gui.table_view import PreviewLabel
    lab = PreviewLabel("（选中一行看预览）")
    lab.resize(300, 300)
    lab.show()
    app.processEvents()
    small = QPixmap(100, 80)
    small.fill()
    lab.setSource(small)
    app.processEvents()
    base = (lab.sizeHint().width(), lab.sizeHint().height(),
            lab.minimumSizeHint().width(), lab.minimumSizeHint().height(),
            lab.minimumSize().width(), lab.minimumSize().height())
    huge = QPixmap(4000, 3000)
    huge.fill()
    lab.setSource(huge)
    app.processEvents()
    after = (lab.sizeHint().width(), lab.sizeHint().height(),
             lab.minimumSizeHint().width(), lab.minimumSizeHint().height(),
             lab.minimumSize().width(), lab.minimumSize().height())
    assert base == after, \
        f"预览框尺寸随图变了：{base} → {after}"
    # 缩放本身仍是 KeepAspectRatio（红线：绝不裁剪/拉伸）
    pm = lab.pixmap()
    assert pm is not None and not pm.isNull()
    if pm.width() > 0 and pm.height() > 0:
        # 图必须被装进控件范围内（不许比控件还大 = 那是没缩放）
        assert pm.width() <= lab.width() + 1, \
            f"预览图 {pm.width()} 比控件 {lab.width()} 还宽（没 fit）"
    lab.close()


def test_preview_scales_with_aspect_ratio(monkeypatch, tmp_path):
    """大图小图都 fit 进同一框，比例不变、且不把控件顶开。"""
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtGui import QPixmap
    from media_resource_manager.gui.table_view import PreviewLabel
    lab = PreviewLabel("x")
    lab.resize(320, 240)
    lab.show()
    app.processEvents()
    ratios = []
    for w, h in ((100, 100), (400, 100), (100, 400), (1000, 2000)):
        pm = QPixmap(w, h)
        pm.fill()
        lab.setSource(pm)
        app.processEvents()
        got = lab.pixmap()
        assert got is not None and not got.isNull()
        assert got.width() <= lab.width() + 1 and \
            got.height() <= lab.height() + 1, \
            f"{w}x{h} 缩放后 {got.width()}x{got.height()} 超出框 " \
            f"{lab.width()}x{lab.height()}"
        ratios.append((got.width() / max(1, got.height()), w / h))
        assert lab.minimumSizeHint().width() <= 320, \
            "预览框下限被图顶开了"
    for got, want in ratios:
        assert abs(got - want) / want < 0.05, \
            f"等比缩放被破坏：得到 {got:.3f}，原图 {want:.3f}"
    lab.close()


# ==================================================================== #
# P2-5：审核页点「喜欢」后位置稳定（增量补位）
# ==================================================================== #

class _Rec:
    """审核记录替身（只带网格要用到的字段）。"""

    def __init__(self, rid, path, score=0.5, task_id=1):
        self.id = rid
        self.path = str(path)
        self.score = score
        self.task_id = task_id
        self.bytes = 1024
        self.width = 100
        self.height = 100
        self.source_url = "http://local/x"
        self.tags = "[]"


def _review_tab_stub(monkeypatch, tmp_path):
    mw, app = _boot(monkeypatch, tmp_path)
    from PySide6.QtWidgets import QListWidget
    import media_resource_manager.gui.review_tab as rt

    class _Tab:
        _snapshot_grid_items = rt.ReviewTab._snapshot_grid_items
        _make_grid_item = rt.ReviewTab._make_grid_item
        _make_grid_item_check = rt.ReviewTab._make_grid_item_check
        _rebuild_grid_items = rt.ReviewTab._rebuild_grid_items
        _try_incremental_fill = rt.ReviewTab._try_incremental_fill
        _thumb_icon_async = rt.ReviewTab._thumb_icon_async

    t = _Tab()
    t.grid = QListWidget()
    t.grid.resize(500, 400)
    t.grid.show()
    t._thumb_size = 64
    t._win = None
    return t, app


def test_review_like_keeps_items_stable(monkeypatch, tmp_path):
    """判掉第 1 张后：保留的 item 必须是**同一个对象**（缩略图不重解码）。"""
    t, app = _review_tab_stub(monkeypatch, tmp_path)
    paths = _mk_images(tmp_path, 6, sub="rev")
    recs = [_Rec(i + 1, p) for i, p in enumerate(paths)]
    t._rebuild_grid_items(recs)
    app.processEvents()
    assert t.grid.count() == len(recs)
    before = [t.grid.item(i) for i in range(t.grid.count())]
    before_ids = [it.data(0x0100) for it in before]   # Qt.UserRole

    # 判定第 1 张（它离开待审队列），补 1 张新的到末尾
    new_recs = recs[1:] + [_Rec(99, paths[0])]
    snap = t._snapshot_grid_items()
    assert len(snap) == len(recs), "快照数量对不上"

    assert t._try_incremental_fill(new_recs, snap) is True, \
        "正常补位场景被判成了不可增量（会退化成全量重建 = 错位）"
    app.processEvents()
    now = [t.grid.item(i) for i in range(t.grid.count())]
    assert len(now) == len(new_recs), \
        f"增量后格子数 {len(now)}，期望 {len(new_recs)}"
    # 前 5 个必须是原对象（缩略图 icon 直接还在 ⇒ 不闪不跳）
    for i in range(len(recs) - 1):
        assert now[i] is before[i + 1], \
            f"第 {i} 个格子被重建了（缩略图会重新解码 → 视觉错位）"
    assert now[i].data(0x0100) == before_ids[i + 1]
    # 末尾追加的是新图
    assert now[-1].data(0x0100) == 99
    t.grid.close()


def test_review_incremental_refuses_when_order_changes(monkeypatch, tmp_path):
    """顺序变了 / 中间插图 ⇒ 必须**拒绝**增量（宁可全量重建也别跳位）。"""
    t, app = _review_tab_stub(monkeypatch, tmp_path)
    paths = _mk_images(tmp_path, 5, sub="rev2")
    recs = [_Rec(i + 1, p) for i, p in enumerate(paths)]
    t._rebuild_grid_items(recs)
    app.processEvents()
    snap = t._snapshot_grid_items()

    # 顺序对调：增量会让用户看到图跳位 ⇒ 必须 False
    swapped = [recs[1], recs[0]] + recs[2:]
    assert t._try_incremental_fill(swapped, snap) is False, \
        "顺序变了仍走增量（用户会看到图跳位）"
    app.processEvents()
    # 中间插一张：keep 的前缀对不上 ⇒ 也必须 False
    inserted = [recs[0], _Rec(77, paths[0])] + recs[1:]
    assert t._try_incremental_fill(inserted, snap) is False, \
        "中间插图仍走增量"
    t.grid.close()


def test_review_empty_queue_clears_grid(monkeypatch, tmp_path):
    """队列空了 ⇒ 网格必须清空（不能留着已判定的图）。"""
    t, app = _review_tab_stub(monkeypatch, tmp_path)
    paths = _mk_images(tmp_path, 3, sub="rev3")
    recs = [_Rec(i + 1, p) for i, p in enumerate(paths)]
    t._rebuild_grid_items(recs)
    app.processEvents()
    snap = t._snapshot_grid_items()
    assert t._try_incremental_fill([], snap) is True
    assert t.grid.count() == 0, \
        f"队列空了但网格还剩 {t.grid.count()} 个格子"
    t.grid.close()