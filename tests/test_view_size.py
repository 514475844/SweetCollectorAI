"""显示尺寸的防回归（r30 + r31）：

r30 三件事：
1. **图库网格缩略图不再被列数压小**（以前 6 列 + 900px 视口把 360px 的预览
   钳到 ~143px，而审核页不钳 → 用户反馈"图库的图比审核小"）；
2. **列表模式右侧预览跟着预览框大小走**（以前写死 scaled(240,240) +
   setMinimumSize(240,240)，拖分割条只留白）；
3. **CPU / GPU 加速开关**（cfg.ai_gpu → ClipONNX providers）。

r31 再补三件事：
4. **「每行几张」整个删掉**，尺寸只剩「预览」一个控件（用户实测两个一起用有 BUG）；
   列数纯交给 Qt（Adjust resize mode）自己算；
5. **缩略图改方形居中裁剪**（`square_thumb_pixmap`）——以前等比缩放塞进方格，
   竖图左右空一大片、横图上下空一片，就是用户报的"图四周一圈白"；
6. **二/三级细分词各挂一份「曾用词」**，历史分三个列表各记各的。
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _FakeViewport:
    """只提供 width() 的假视口：让「视口宽」这个输入在测试里可控。"""

    def __init__(self, w: int):
        self._w = int(w)

    def width(self) -> int:
        return self._w


def _boot(monkeypatch, tmp_path):
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtCore import QTimer          # QTimer 在 QtCore，不在 QtWidgets
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
    from media_resource_manager.gui import theme
    theme.apply_theme(app, False)
    # P2-2：`MainWindow()` 构造期的超时守护。
    # 为什么需要：全量 pytest 曾在 ~90% 处崩在**这一行**（0xc0000374 堆损坏）。
    # 崩因是构造期里某个对象横跨 Qt 与 Python 堆被回收，而构造**永不返回**时
    # 整个进程只是静默挂住（offscreen 下没人看见），挂到 CI 超时被杀同样拿不到
    # junit。QTimer.singleShot 只在**事件循环转起来**时才触发，所以放在构造
    # 之后 processEvents 里等一轮——它守的是「构造完成后界面还在跑」的阶段。
    # 真要防「构造本身挂死」得靠 faulthandler 超时（见 conftest 的 _faulthandler_guard）。
    _hung = {"v": False}

    def _on_timeout():
        _hung["v"] = True
        app.quit()

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(_on_timeout)
    watchdog.start(8000)
    try:
        win = mw.MainWindow()
    finally:
        watchdog.stop()
    if _hung["v"]:
        raise AssertionError("MainWindow 构造后界面在 8s 内没能完成事件循环")
    win.show()
    app.processEvents()
    return app, win


def _grid(win, monkeypatch, vw=1000):
    """把图库网格的「视口宽」钉成 vw，返回控件。"""
    monkeypatch.setattr(win.g_grid, "viewport", lambda *a, _vp=_FakeViewport(vw): _vp)
    return win.g_grid


# ------------------------------------- 1) 图库网格：只有「预览」一个控件 ------
def test_thumb_size_is_the_only_input(monkeypatch, tmp_path):
    """缩略图 = 滑块值：360px 就是 360px，不再被列数反压。"""
    app, win = _boot(monkeypatch, tmp_path)
    g = _grid(win, monkeypatch, vw=1000)
    win.g_list_mode = False
    win.cfg.gallery_thumb_size = 360
    win._apply_grid()
    assert g.iconSize().width() == 360
    assert g.gridSize().width() == 360          # 单元格跟图标同宽（共用几何）


def test_narrow_viewport_no_longer_shrinks_the_thumb(monkeypatch, tmp_path):
    """r31：视口再窄也不缩水（以前 6 列 + 窄视口会把图钳到 ~143px）。"""
    app, win = _boot(monkeypatch, tmp_path)
    g = _grid(win, monkeypatch, vw=300)         # 故意给个极窄视口
    win.g_list_mode = False
    win.cfg.gallery_thumb_size = 360
    win._apply_grid()
    assert g.iconSize().width() == 360          # 列数交给 Qt，图不缩水


def test_small_thumb_follows_the_slider(monkeypatch, tmp_path):
    """小预览也照滑块走（128 → 128），列数照样不插手。"""
    app, win = _boot(monkeypatch, tmp_path)
    g = _grid(win, monkeypatch, vw=1000)
    win.g_list_mode = False
    win.cfg.gallery_thumb_size = 128
    win._apply_grid()
    assert g.iconSize().width() == 128
    assert g.gridSize().width() == 128


def test_the_columns_control_is_gone(monkeypatch, tmp_path):
    """r31：用户要求「取消每行几张，只用预览」→ 界面上不能再有这个控件。"""
    app, win = _boot(monkeypatch, tmp_path)
    for dead in ("sb_cols", "l_cols"):
        assert not hasattr(win, dead), f"{dead} 应该已经删掉了"
    import inspect
    from media_resource_manager.gui import main_window
    assert not hasattr(main_window.MainWindow, "set_gallery_columns")


def test_extreme_preview_is_still_clamped(monkeypatch, tmp_path):
    """再大也不能超过 420（避免一张图铺满整屏）。"""
    app, win = _boot(monkeypatch, tmp_path)
    g = _grid(win, monkeypatch, vw=1000)
    win.g_list_mode = False
    win.cfg.gallery_thumb_size = 9999
    win._apply_grid()
    assert 1 <= g.iconSize().width() <= 420


def test_maximized_zooms_in(monkeypatch, tmp_path):
    """最大化时预览跟着放大（×1.8）。"""
    app, win = _boot(monkeypatch, tmp_path)
    monkeypatch.setattr(win, "isMaximized", lambda: True)
    g = _grid(win, monkeypatch, vw=1400)
    win.g_list_mode = False
    win.cfg.gallery_thumb_size = 128
    win._apply_grid()
    assert g.iconSize().width() == 230          # int(128*1.8)


def test_shared_grid_geometry_is_kept(monkeypatch, tmp_path):
    """与审核页共用的几何不能回退：间距 8、单元格高 = 图 + 28。"""
    app, win = _boot(monkeypatch, tmp_path)
    g = _grid(win, monkeypatch, vw=1000)
    win.g_list_mode = False
    win.cfg.gallery_thumb_size = 160
    win._apply_grid()
    assert g.spacing() == 8
    assert g.gridSize().height() == g.iconSize().height() + 28


def test_gallery_and_review_default_alike():
    """两个尺寸默认值一致 —— 默认就不该分「图库小、审核大」。"""
    from media_resource_manager.config import AppConfig
    assert AppConfig.gallery_thumb_size == AppConfig.review_thumb_size


def test_list_mode_still_short_circuits(monkeypatch, tmp_path):
    """列表模式下 _apply_grid 走的是「左缩略图右信息」，不动网格几何。"""
    app, win = _boot(monkeypatch, tmp_path)
    g = _grid(win, monkeypatch, vw=1000)
    win.g_list_mode = True
    win.cfg.gallery_thumb_size = 360
    win._apply_grid()
    assert g.gridSize().height() == 132        # 列表行高，没被网格那套覆盖
    win.g_list_mode = False


def test_slider_drives_either_the_grid_or_the_preview(monkeypatch, tmp_path):
    """r31：列表模式下拖滑块也必须立刻有反应（修「预览 >235px 就没效果」）。"""
    app, win = _boot(monkeypatch, tmp_path)
    win.show()
    try:
        win.stack.setCurrentWidget(win._gallery_tab)
    except Exception:
        pass
    app.processEvents()

    # 网格模式：滑块 → 图标尺寸
    win.g_list_mode = False
    win.cfg.gallery_thumb_size = 240
    win.set_gallery_thumb_size(240)
    assert win.g_grid.iconSize().width() == 240

    # 列表模式：滑块 → 右侧预览窗格变宽（+40，上限 560）
    # （先手动给个真实尺寸：没铺开过的分割条 sizes() 就是 0/0）
    win.g_list_mode = True
    win.g_grid.setVisible(False)
    win.g_split.setVisible(True)
    win.g_split.setSizes([900, 240])
    win.set_gallery_thumb_size(320)
    after = [int(x) for x in win.g_split.sizes()]
    assert after[1] == 360 and after[0] >= 200
    win.g_list_mode = False
    win.set_gallery_thumb_size(160)


# ------------------------------------------- 2) 缩略图：方形居中裁剪 ----------
def _write_img(path, w=400, h=200):
    """渐变图（纯色图会让 dHash/感知去重这些下游逻辑误判，一律用渐变）。"""
    from PIL import Image
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(0, w, 4):
            c = (x * 255 // max(1, w - 1), y * 255 // max(1, h - 1), 128)
            for k in range(4):
                if x + k < w:
                    px[x + k, y] = c
    im.save(str(path), "PNG")
    return path


def test_square_thumb_crops_portrait_and_landscape(tmp_path):
    """竖图横图都要裁成正方形（这是「一圈白」的根治办法）。"""
    from media_resource_manager.gui.table_view import square_thumb_pixmap
    wide = _write_img(tmp_path / "wide.png", 400, 200)
    tall = _write_img(tmp_path / "tall.png", 200, 400)
    for p in (wide, tall):
        pm = square_thumb_pixmap(p, 128)
        assert pm is not None and not pm.isNull()
        assert pm.width() == 128 and pm.height() == 128


def test_square_thumb_uses_a_separate_cache_file(tmp_path):
    """缓存文件名带 _sq：旧缓存（等比版）不会被新代码误读。"""
    from media_resource_manager.gui.table_view import square_thumb_pixmap
    p = _write_img(tmp_path / "c.png", 300, 300)
    cd = tmp_path / "cache"
    pm1 = square_thumb_pixmap(p, 96, cd, "key1")
    assert pm1 is not None and not pm1.isNull()
    # R247：分桶后缩略图在子目录里，必须递归（否则一条都取不到）
    hit = [f for f in cd.rglob("*.png")]
    assert len(hit) == 1 and "_sq96" in hit[0].name
    pm2 = square_thumb_pixmap(p, 96, cd, "key1")
    assert pm2 is not None and not pm2.isNull()


def test_square_thumb_gracefully_fails(tmp_path):
    """坏文件 / 不存在 → 返回 None（让上层的「（找不到）」提示去接住）。"""
    from media_resource_manager.gui.table_view import square_thumb_pixmap
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image at all")
    assert square_thumb_pixmap(bad, 64) is None
    assert square_thumb_pixmap(tmp_path / "missing.png", 64) is None


def test_legacy_thumb_cache_is_swept_once(monkeypatch, tmp_path):
    """r31 老缓存文件名不带 _sq，永远不会被读到 → 第一次进图库时清掉。"""
    app, win = _boot(monkeypatch, tmp_path)
    legacy = win._thumb_dir() / "deadbeef_160.png"
    legacy.write_bytes(b"old")
    keep = win._thumb_dir() / "deadbeef_sq160.png"
    keep.write_bytes(b"new")
    win._thumbs_legacy_swept = False      # 开机时目录还是空的，这里手动重开一次
    win._thumb_dir()
    assert not legacy.exists()
    assert keep.exists()


# -------------------------------------------------- 2) 列表预览自适应 -------
def _preview_label():
    from PySide6.QtWidgets import QApplication
    from media_resource_manager.gui.review_tab import PreviewLabel
    app = QApplication.instance() or QApplication([])
    lab = PreviewLabel("（选中一行看预览）")
    app.processEvents()
    return lab


def test_preview_scales_to_the_current_size():
    from PySide6.QtGui import QPixmap
    lab = _preview_label()
    lab.resize(240, 240)
    lab.setSource(QPixmap(400, 300))
    p = lab.pixmap()
    assert not p.isNull()
    assert p.width() <= 240 and p.height() <= 240


def test_preview_grows_when_the_pane_grows():
    """拖宽分割条后预览框变大 → 图也要跟着变大（这是用户报的 bug）。"""
    from PySide6.QtGui import QPixmap
    lab = _preview_label()
    lab.resize(160, 160)
    lab.setSource(QPixmap(400, 300))
    small = lab.pixmap().width()

    # 拖宽分割条后 Qt 会派发 Resize；这里直接调 _redraw 等价验证
    # 「尺寸变了 → 图重画成新尺寸」，别再写死 240。
    lab.resize(420, 420)
    lab._redraw()
    big = lab.pixmap().width()
    assert big > small
    assert big <= 420


def test_resize_event_triggers_redraw(monkeypatch):
    """resizeEvent 必须调 _redraw（否则拖分割条不会重画）。"""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QResizeEvent
    lab = _preview_label()
    calls = []
    monkeypatch.setattr(lab, "_redraw", lambda: calls.append(1))
    lab.resizeEvent(QResizeEvent(QSize(300, 300), QSize(0, 0)))
    assert calls == [1]


def test_clear_source_resets_to_placeholder():
    from PySide6.QtGui import QPixmap
    lab = _preview_label()
    lab.setSource(QPixmap(10, 10))
    lab.clear_source()
    assert lab.pixmap().isNull()
    assert "选中一行" in lab.text()


def test_preview_no_longer_pinned_to_240(monkeypatch, tmp_path):
    """预览框不再有 240×240 的最小尺寸（否则拖小了也缩不下）。"""
    app, win = _boot(monkeypatch, tmp_path)
    from media_resource_manager.gui.review_tab import ReviewTab
    tab = None
    for ch in win.findChildren(ReviewTab):
        tab = ch
        break
    assert tab is not None
    assert tab.t_prev_img.minimumWidth() < 240
    assert tab.t_prev_img.minimumWidth() >= 1


# ----------------------------------------- 3) 二/三级细分词的「曾用词」 -------
def _form(win):
    """任务表单是「打开对话框时才建」的，测试里先把它建出来。

    必须留住返回的 panel：平时它挂在 QDialog 上所以不会被回收，测试里不挂
    就直接被 GC → 里面的「曾用词」标签变成已删除的 C++ 对象。
    """
    win._test_form_panel = win._build_task_form_panel()
    return win


def test_three_levels_keep_separate_histories():
    """一级/二级/三级各记一份历史（细分词跟主题词串味会填错层）。"""
    from media_resource_manager.config import AppConfig
    cfg = AppConfig()
    for k in ("ai_label_history", "ai_label_history_l2",
              "ai_label_history_l3", "ai_excl_history"):
        assert isinstance(getattr(cfg, k), list)
        assert getattr(cfg, k) == []


def test_level2_and_level3_have_their_own_chips(monkeypatch, tmp_path):
    """任务表单里二三级各挂一排「曾用词」按钮。"""
    app, win = _boot(monkeypatch, tmp_path)
    _form(win)
    for name in ("b_hist_l2", "b_hist_l3"):
        assert getattr(win, name, None) is not None
    assert win.b_hist_l2._key == "ai_label_history_l2"
    assert win.b_hist_l3._key == "ai_label_history_l3"


def test_chips_render_every_level(monkeypatch, tmp_path):
    """记进历史的词要显示成可点的小标签，别藏在下拉框里。"""
    from PySide6.QtWidgets import QPushButton
    app, win = _boot(monkeypatch, tmp_path)
    _form(win)
    win._remember_words(["猫耳", "和服"], "ai_label_history_l2")
    win._remember_words(["猫耳", "和服"], "ai_label_history_l3")
    win._refresh_hist_chips()
    for name in ("b_hist_l2", "b_hist_l3"):
        texts = [b.text() for b in getattr(win, name)._box.findChildren(QPushButton)]
        assert "猫耳" in texts and "和服" in texts


def test_levels_never_mix_up(monkeypatch, tmp_path):
    """只往二级记，一级/三级/排除词三个历史都得保持空。"""
    app, win = _boot(monkeypatch, tmp_path)
    _form(win)
    win._remember_words(["和服"], "ai_label_history")
    win._remember_words(["猫耳"], "ai_label_history_l2")
    assert win.cfg.ai_label_history == ["和服"]
    assert win.cfg.ai_label_history_l2 == ["猫耳"]
    assert win.cfg.ai_label_history_l3 == []
    assert win.cfg.ai_excl_history == []


def test_collect_remembers_all_four_histories():
    """保存任务（add_task / save_selected 都走 _collect）时四条历史一起记。"""
    import inspect
    import pathlib
    from media_resource_manager.gui import main_window
    # r39g：别用 inspect.getsource —— 全量跑时它会退回 linecache 里的陈旧源码
    # 缓存（实测只捞到 `_dot_icon` 收尾那行 `return QIcon(pm)`，真正的 82 行
    # `_collect` 一个字都看不到）→ 假红。单跑过、全量挂，就是这个原因。
    # 直接读磁盘源文件，跟当前代码是不是一致才是我要断言的东西。
    src = pathlib.Path(inspect.getsourcefile(main_window)).read_text(
        encoding="utf-8", errors="ignore")
    for k in ("ai_label_history", "ai_label_history_l2",
              "ai_label_history_l3", "ai_excl_history"):
        assert f'"{k}"' in src


# --------------------------------------------------- 4) CPU / GPU 开关 -----
def test_gpu_default_is_on():
    from media_resource_manager.config import AppConfig
    assert AppConfig.ai_gpu is True


def _fake_ort():
    class _ORT:
        @staticmethod
        def get_available_providers():
            return ["CPUExecutionProvider", "DmlExecutionProvider",
                    "CUDAExecutionProvider"]
    return _ORT


def test_gpu_on_prefers_cuda_then_dml():
    from media_resource_manager.classify.clip_onnx import ClipONNX
    # 2026-10-27：small_model 默认 True（小模型 auto 走 CPU）。本条测的是
    # **provider 优先级顺序**，显式按"大模型"场景取，才验证得到 CUDA>DML>CPU。
    got = ClipONNX._pick_providers(_fake_ort(), True, small_model=False)
    assert got[0] in ("CUDAExecutionProvider", "DmlExecutionProvider")
    assert "CPUExecutionProvider" in got


def test_gpu_off_forces_cpu_only():
    from media_resource_manager.classify.clip_onnx import ClipONNX
    got = ClipONNX._pick_providers(_fake_ort(), False)
    assert got == ["CPUExecutionProvider"]


def test_gpu_off_even_without_cpu_listed():
    """极端情况（provider 列表里没有 CPU）：也返回 CPU，别让会话建不起来。"""

    class _ORT:
        @staticmethod
        def get_available_providers():
            return ["CUDAExecutionProvider"]

    from media_resource_manager.classify.clip_onnx import ClipONNX
    assert ClipONNX._pick_providers(_ORT(), False) == ["CPUExecutionProvider"]


def test_engine_passes_the_configured_device():
    import inspect
    from media_resource_manager import engine
    src = inspect.getsource(engine)
    assert "gpu=_ai_gpu" in src
    assert '_ai_gpu = bool(getattr(self.cfg, "ai_gpu", True))' in src


def test_local_classifier_accepts_gpu():
    import inspect
    from media_resource_manager.classify import clip_model
    assert "gpu: bool" in inspect.getsource(clip_model.LocalClassifier)
    assert "gpu=self._gpu" in inspect.getsource(clip_model.LocalClassifier)


def test_menu_switch_exists_and_persists(monkeypatch, tmp_path):
    """菜单里有「AI 用 GPU 加速」，关掉后写进 cfg 并落盘。"""
    import json
    from pathlib import Path
    app, win = _boot(monkeypatch, tmp_path)
    assert hasattr(win, "act_ai_gpu")
    assert win.act_ai_gpu.isChecked() is True

    win.toggle_ai_gpu(False)
    assert win.cfg.ai_gpu is False
    assert win.act_ai_gpu.isChecked() is False
    home = Path(os.environ.get("MRM_HOME", ""))
    if home and (home / "config.json").exists():
        saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
        assert saved.get("ai_gpu") is False

    win.toggle_ai_gpu(True)
    assert win.cfg.ai_gpu is True
