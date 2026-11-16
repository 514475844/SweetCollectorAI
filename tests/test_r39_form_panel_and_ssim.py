"""r39b：锁死「任务表单面板被 GC → 悬空控件」+「SSIM 堵死主线程」两条回归。

真机现场（r39 前台真窗口按钮全扫描，脚本看门狗 + 主线程栈 dump 抓到）：

1. ``RuntimeError: libshiboken: Internal C++ object (QLineEdit) already deleted``
   打在 ``add_task -> self.f_entry.text()``，紧接着
   ``_tick -> refresh -> _reload_adapters -> self.f_adapter.clear()`` 又炸一条。
   根因：`_build_task_form_panel()` 造出来的面板是**裸 QWidget**（没有 Qt 父节点），
   函数一返回 Python 侧就再没人引用它 → PySide6 立刻销毁整棵面板，`self.f_*`
   全成悬空包装 → 之后每次 0.5s 的 refresh 都刷一条 traceback。

2. ``qt_loop=STALL 295s last='查找相似(SSIM)'``，主线程栈：
       dedup/ssim.py:26 ssim_pair  <- 每对都 cv2.imread
       dedup/ssim.py:53 cluster   <- O(n²)
       gui/ssim_dialog.py:22 find_similar_groups
       gui/main_window.py find_similar
   150 张图 ≈ 22000 次解码，界面直接假死 5 分钟。

修法（本文件逐条锁死）：
  1. 任务表单面板 `setParent(self)` 常驻主窗口 + `_open_task_form` 复用同一套
     面板/对话框（`_ensure_task_form`）；
  2. `_alive()` 统一判活，别再到处兜 `except RuntimeError`；
  3. `_reload_adapters` 先 disconnect 再 connect（原来每 0.5s 攒一条连接）；
  4. SSIM 走自持线程池，结果用 `bg_result` 信号 ev 回主线程再建对话框；
  5. `ssim.cluster` 先把每张图解码成 256×256 灰度缓存，pair 只做 matchTemplate。
"""
import time

import pytest

pytest.importorskip("PySide6")

MW_SRC = __import__("pathlib").Path("media_resource_manager/gui/main_window.py")
SSIM_SRC = __import__("pathlib").Path("media_resource_manager/dedup/ssim.py")


@pytest.fixture(autouse=True)
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def home(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", str(d))
    monkeypatch.setattr("media_resource_manager.config.user_data_dir",
                        lambda: d)
    return d


@pytest.fixture()
def win(qapp, home, monkeypatch):
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
    w = mw.MainWindow()
    w.show()
    app.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass


# ------------------------------------------------------------------ 1 表单面板

def test_form_panel_is_reparented(win):
    """面板必须挂到主窗口下：裸 QWidget 会被 GC 连子控件一起销毁。"""
    win._form_panel = None
    win._ensure_task_form()
    panel = win._form_panel
    assert panel is not None
    assert panel.parent() is win
    assert win._alive(win.f_entry)
    assert win._alive(win.f_adapter)


def test_ensure_task_form_is_idempotent(win):
    """连调两次不许重建面板（重建就会把上一套 f_* 变成悬空包装）。"""
    win._form_panel = None
    built = []

    def _fake():
        from PySide6.QtWidgets import QWidget
        p = QWidget(win)
        built.append(p)
        win.f_entry = p   # 占位，只为证明不重建
        return p

    win._build_task_form_panel = _fake
    win._ensure_task_form()
    first = win._form_panel
    win._ensure_task_form()
    assert win._form_panel is first
    assert len(built) == 1


def test_alive_returns_false_for_none(win):
    assert win._alive(None) is False


def test_form_is_reused_across_opens(win, monkeypatch):
    """_open_task_form 复用面板：不再每次新建一套控件。"""
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec", lambda self: None)
    calls = []
    real = win._build_task_form_panel

    def _counted():
        calls.append(1)
        return real()

    win._build_task_form_panel = _counted
    win._form_panel = None
    win._form_dlg = None
    win._open_task_form()
    n_after_first = len(calls)
    win._open_task_form()
    assert len(calls) == n_after_first, "第二次开对话框又重建了整套控件"
    assert isinstance(win._form_dlg, QDialog)


def test_reload_adapters_connects_once(win):
    """refresh 每 tick 都调 _reload_adapters，直接 connect 会攒连接。"""
    win._ensure_task_form()
    hits = []
    # 注意：PySide6 的 receivers() 统计不到「连到 Python callable」的连接，
    # 所以这里挂个探针，看真触发一次 activated 会被回调几次。
    win.f_adapter.activated.connect(lambda *a: hits.append(1))
    for _ in range(5):
        win._reload_adapters()
    win.f_adapter.activated.emit(0)
    assert len(hits) == 1, (
        f"activated 被回调了 {len(hits)} 次（每 0.5s 攒一条连接）")


# ------------------------------------------------------------------ 2 SSIM

def test_find_similar_runs_in_background(win, monkeypatch, qapp):
    """find_similar 必须立刻返回（不堵主线程），结果 ev 回主线程才弹窗。"""
    import media_resource_manager.gui.main_window as mw
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(mw, "find_similar_groups", lambda db: [])
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    win._ssim_busy = False

    t0 = time.time()
    win.find_similar()
    dt = time.time() - t0
    assert dt < 1.0, f"find_similar 同步堵了 {dt:.2f}s"
    assert win._ssim_busy is True

    # 等后台算完 + QueuedConnection 派发到主线程
    for _ in range(80):
        qapp.processEvents()
        if not win._ssim_busy:
            break
        time.sleep(0.02)
    assert win._ssim_busy is False


def test_find_similar_is_guarded_while_busy(win, monkeypatch):
    """算的过程中再点一次要直接返回，别叠一个后台任务。"""
    import media_resource_manager.gui.main_window as mw

    monkeypatch.setattr(mw, "find_similar_groups", lambda db: [])
    win._ssim_busy = True
    before = getattr(win, "_bg_pool", None)
    win.find_similar()
    assert getattr(win, "_bg_pool", None) is before


def test_bg_result_is_emitted_not_called_inline(win):
    """结果必须走 bg_result 信号：Qt 对象只能在 GUI 线程碰。"""
    src = MW_SRC.read_text(encoding="utf-8")
    assert "self.bg_result.emit((cb, res, err))" in src
    # 必须显式的 QueuedConnection：PySide6 连到 Python callable 默认是直连，
    # 从工作线程 emit 会在工作线程里跑回调 → 建对话框就炸
    assert "bg_result.connect(self._on_bg_result," in src
    assert "Qt.ConnectionType.QueuedConnection" in src


def test_ssim_cluster_decodes_each_image_once(win, tmp_path, monkeypatch):
    """cluster 只解码 N 次（每张一次），不是每对两次。"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from media_resource_manager.dedup import ssim

    paths = []
    for i in range(6):
        img = (np.full((60, 60), 120, np.uint8) if i % 2 == 0
               else np.full((60, 60), 125, np.uint8))
        cv2.imwrite(str(tmp_path / f"p{i}.png"), img)
        paths.append(tmp_path / f"p{i}.png")

    calls = []
    real = cv2.imread

    def _counted(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(cv2, "imread", _counted)
    groups = ssim.cluster(paths)
    assert len(calls) <= len(paths), (
        f"解码了 {len(calls)} 次（图只有 {len(paths)} 张）＝ 又退化成每对解码")


def test_ssim_groups_carry_best(win, tmp_path, monkeypatch):
    """find_similar_groups 返回 [(group, best)]，best 在后台一次算好。"""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from media_resource_manager.gui import ssim_dialog

    class _FakeDB:
        def query(self, sql, params=()):
            rows = []
            for i in range(4):
                img = np.full((80, 80), 100 + i, np.uint8)
                p = tmp_path / f"s{i}.png"
                cv2.imwrite(str(p), img)
                rows.append({"path": str(p)})
            return rows

    out = ssim_dialog.find_similar_groups(_FakeDB(), limit=4)
    for group, best in out:
        assert best in group
        assert group[0] is best or len(group) >= 1


def test_ssim_dialog_init_does_not_decode(win, monkeypatch, tmp_path):
    """建对话框时不再解码算『保留哪张』（那等于又啃一遍原图）。"""
    from media_resource_manager.gui import ssim_dialog

    p = tmp_path / "x.png"
    p.write_bytes(b"")
    hits = []
    monkeypatch.setattr(ssim_dialog, "image_dimensions",
                        lambda q: hits.append(q) or (10, 10))
    dlg = ssim_dialog.SimilarDialog([([p], p)], tmp_path, on_done=lambda: None)
    assert not hits, f"建对话框又解码了 {len(hits)} 张图"


# ------------------------------------------------------------------ 3 静态

def test_alive_helper_exists_and_is_used():
    src = MW_SRC.read_text(encoding="utf-8")
    assert "def _alive(o):" in src
    assert "if not self._alive(getattr(self, \"f_adapter\", None)):" in src
