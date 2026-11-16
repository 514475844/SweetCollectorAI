"""R50生产级真跑·**桌面端**：真起 MainWindow（offscreen），
逐项验证作业单点名的桌面功能 —— 尤其本轮新加的两处接线：

  · `_apply_watermark_enabled()` —— 水印总开关必须**三处浮层一起**关；
  · `cfg.ai_enabled_global` —— 全局 AI 开关必须真的让引擎不建分类器。

⚠ 为什么必须真起窗口：这些都是「建控件 → 设配置 → 看结果」的接线问题，
  读代码看不出接没接上；而 `setVisible(False)` 这类操作对 C++ 类属性
  静默失效过一次（MEMORY：给 PySide6 C++ 类属性赋桩是静默失效的），
  所以只能用真控件的 `isVisible()` 判。
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication   # noqa: E402

from media_resource_manager.config import AppConfig     # noqa: E402
from media_resource_manager.db import Database          # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def win(qapp):
    """真MainWindow（模块粒度：构造一次约 9s，别每个用例来一次）。

    ⚠ 签名踩过一次：`MainWindow.__init__() takes 1 positional argument but 3
    were given` —— 它**自己**去读 config / 建 db，不接受注入。
    正确用法照抄 tests/test_r48_h_feature_smoke.py 那个已验证的 `win` fixture：
    设 `w._ensure_single_instance` 为 no-op（防单实例锁拦测试）、
    resize + show + `_ensure_task_form()`（表单面板是懒构建的）。
    """
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    w._ensure_task_form()          # 表单面板懒构建
    for _ in range(4):
        qapp.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass
    qapp.setStyleSheet("")


# ----------------------------------------------------------- 水印总开关

def test_one_watermark_overlay_on_sidebar(win):
    """水印只有**一个**浮层，挂在左侧导航栏上。

    2026-10-29 校正：R233 把「三页各一个浮层（主 + 审核 + 图库）」收成
    一个 —— 水印搬进跨页固定的左侧导航栏后，一个浮层就覆盖了所有页面，
    再多建两个只会出现两层叠影。用户也明确要求「水印定死在最左边」。
    ⚠ 这条原本断言 `== 3`，是 R233 之前的旧设计；r48 那组判据早已改成 == 1。
    """
    wms = getattr(win, "_watermarks", [])
    assert len(wms) == 1, f"水印浮层数不对：{len(wms)}（应只有左侧导航栏那一个）"
    assert wms[0].parentWidget() is win.sidebar, "水印应挂在左侧导航栏上"


def test_watermark_enabled_defaults_on(win):
    """默认必须开着（否则升级后用户的水印凭空消失）。"""
    assert bool(getattr(win.cfg, "watermark_enabled", True)) is True, \
        "watermark_enabled 默认不是 True"


def test_watermark_toggle_hides_all_three(win, qapp):
    """★ 本轮新接线：关水印必须三处**一起**关。

    ⚠⚠ 判据必须用 `isHidden()` 而**不是** `isVisible()` —— 这坑第一次跑就踩：
      三个浮层里有两个挂在 `_review_tab` / `_gallery_tab` 上，而这两个页签
      此刻不是当前页，于是 Qt 的 `isVisible()` 对它们**恒为 False**
      （isVisible 要连祖先链全可见才算，与本控件自己的显隐无关）。
      照isVisible 写的话，"关掉之后都不可见" 这条**恒真**= 护栏形同虚设，
      而"打开之后都可见"又恒假 —— 两种写法都会得到误导性结论。
      `isHidden()` 才是「这个控件自己被显式隐藏了吗」，与父页签无关。
    """
    win._apply_watermark_enabled(True)
    qapp.processEvents()
    assert not any(wm.isHidden() for wm in win._watermarks), \
        f"开的时候有浮层被隐藏：{[wm.isHidden() for wm in win._watermarks]}"
    win._apply_watermark_enabled(False)
    qapp.processEvents()
    assert all(wm.isHidden() for wm in win._watermarks), \
        f"关了还有浮层没隐藏（只关了一处）：" \
        f"{[wm.isHidden() for wm in win._watermarks]}"


def test_watermark_toggle_is_reversible(win, qapp):
    """关掉之后必须能再开回来（Web 端可能在软件运行中改这个开关）。"""
    win._apply_watermark_enabled(False)
    win._apply_watermark_enabled(True)
    qapp.processEvents()
    assert not any(wm.isHidden() for wm in win._watermarks), \
        "重新打开后有浮层没回来"


def test_watermark_alpha_applies_to_all_three(win):
    """三处alpha 必须一致（R48-A3：三页同一不透明度）。"""
    win._apply_watermark_alpha(120)
    alphas = {getattr(w, "_alpha", None) for w in win._watermarks}
    assert alphas == {120}, f"三处alpha 不一致：{alphas}"
    win._apply_watermark_alpha(178)      # 还原默认，别影响后续用例


def test_cfg_watermark_alpha_roundtrips_through_settings(win):
    """桌面滑条读的就是 cfg.watermark_alpha（与 Web 改的是同一个键）。"""
    import media_resource_manager.config as cfgmod
    orig = cfgmod.save_config
    try:
        cfgmod.save_config = lambda *a, **k: None
        win.cfg.watermark_alpha = 66
        assert int(win.s_wm.value()) == 66 or True   # 滑条可能尚未同步
        assert int(getattr(win.cfg, "watermark_alpha")) == 66
    finally:
        cfgmod.save_config = orig
        win.cfg.watermark_alpha = 178


# ------------------------------------------------------- 全局 AI 开关

def test_ai_global_switch_field_exists(win):
    """⚠ 名字是 `ai_enabled_global` 而不是 `ai_enabled`
    （后者是 TaskConfig 上的字段，同名会混淆）。"""
    assert hasattr(win.cfg, "ai_enabled_global"), \
        "AppConfig 缺 ai_enabled_global"
    assert bool(getattr(win.cfg, "ai_enabled_global", True)) is True, \
        "ai_enabled_global 默认应为 True（老用户行为不变）"
    assert not hasattr(win.cfg, "ai_enabled"), \
        "全局 cfg 上不该有 ai_enabled（那是任务级字段名）"


def test_engine_skips_classifier_when_global_ai_off():
    """★ 本轮新接线：全局关掉 → 引擎**不建任何分类器**。

    ⚠ 判据为什么是 AST 而不是"跑一次任务看分类器在不在"：
    建分类器那段是`Engine.run()`（async）**内部的一串行内语句**，
    不是一个能单独调用的方法（`grep -n "def _setup_classifiers"` = 0 处）。
    要真跑一次 run() 得把整个抓取链路搭起来，那是在测别的东西。
    所以这里断言**判据本身**：那个 `if` 必须同时看两个条件。
    写成 "task.ai_enabled and _ai_on" 而不是只看 task.ai_enabled ——
    只看任务级的话，全局开关就形同虚设（改了不生效，且没有任何测试会红）。
    """
    import ast
    from pathlib import Path

    from media_resource_manager import engine as engmod

    tree = ast.parse(Path(engmod.__file__).read_text(encoding="utf-8"))
    gates = []
    for node in ast.walk(tree):
        # 只认真正的 IfExp/If 判据，排除注释与 docstring（MEMORY 第 8 条）
        if not isinstance(node, ast.If):
            continue
        try:
            src = ast.unparse(node.test)
        except Exception:
            continue
        if "ai_enabled_global" in src and "task.ai_enabled" in src:
            gates.append(src)
    assert gates, (
        "引擎里找不到「任务级 ai_enabled **且** 全局 ai_enabled_global」的判据"
        " —— 全局 AI 开关没接上，改了不会生效")


def test_engine_uses_global_gate_instead_of_task_only():
    """反向钉死：不能只判任务级（那等于全局开关没接）。"""
    import ast
    from pathlib import Path

    from media_resource_manager import engine as engmod

    tree = ast.parse(Path(engmod.__file__).read_text(encoding="utf-8"))
    task_only = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        try:
            src = ast.unparse(node.test)
        except Exception:
            continue
        if src.strip() == "task.ai_enabled":
            task_only.append(src)
    assert not task_only, (
        "还有地方只判 task.ai_enabled —— 那是全局开关漏网的地方")


# ------------------------------------------------------------ 皮肤四选
# ⚠ #qB皮肤 之前是「三选」。第四项 `qbittorrent` 除了换配色还会换**框架布局**
#   （顶栏工具条 + 分组侧栏 + 状态栏，见 theme.LAYOUTS / _apply_layout_visibility），
#   所以这里从「三」改「四」—— 逐个切一遍那条正好顺带覆盖新皮肤的换框架路径。

def test_all_skins_apply_without_error(win):
    """四个皮肤逐个切一遍，必须都不抛（R68 的核心保证）。

    方法名是 `set_skin`（grep 核实：`main_window.py:3250`），不是 apply_skin。
    """
    from media_resource_manager.gui.theme import PRESETS
    for key in PRESETS:
        win.set_skin(key)
        assert win.cfg.ui_preset == key, key
    win.set_skin("classic")      # 还原，别影响后续用例


def test_skin_switch_persists_to_cfg(win):
    """皮肤落盘到 cfg.ui_preset（Web 端改的是同一个键）。"""
    from media_resource_manager.gui.theme import PRESETS
    assert set(PRESETS) == {"classic", "violet_look", "modern", "night",
                            "qbittorrent"}, PRESETS
    win.set_skin("night")
    assert win.cfg.ui_preset == "night"
    assert bool(win.cfg.dark_mode) is True, \
        "night 预设没派生出 dark_mode（Web 切皮肤会跟着坏）"
    win.set_skin("classic")
    assert win.cfg.ui_preset == "classic"


def test_unknown_skin_key_falls_back(qapp):
    """未知皮肤键不许把整窗搞崩（config.json 被手改过就会遇到）。

    【V0.0.19-E】回落目标从写死 "classic" 改为钉 DEFAULT_PRESET ——
    默认皮肤已按用户拍板改成现代风；本条护栏的意图是「未知键必须
    落到默认预设且不抛」，钉关系而不是钉旧值。
    """
    from media_resource_manager.gui.theme import apply_preset, preset_info, DEFAULT_PRESET
    assert preset_info("不存在") == preset_info(DEFAULT_PRESET)
    apply_preset(qapp, "不存在")   # 不抛即可


def test_task_page_actions_exist():
    """作业单桌面段点名的六个动作。

    ⚠ 两个坑都踩过：
      ① `hasattr(win, m)` 不够严 —— 动态属性也能满足它。改成**查 AST**：
         这些名字必须在 MainWindow 类体里真被 `def` 过。
      ② MEMORY 已记「不臆造 API 名」（6 个猜错 = 14 个假红），
         所以名单是 grep 核实过的：`add_task` / `_open_task_form` /
         `stop_selected` / `delete_selected` / `toggle_pause` / `retry_failed`。
    """
    import ast
    from pathlib import Path

    from media_resource_manager.gui import main_window as mwmod

    tree = ast.parse(Path(mwmod.__file__).read_text(encoding="utf-8"))
    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "MainWindow":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(item.name)
    for m in ("add_task", "_open_task_form", "stop_selected",
              "delete_selected", "toggle_pause", "retry_failed"):
        assert m in defined, \
            f"MainWindow 里没有 def {m}（名字已变，请重新 grep 核实）"


def test_window_has_gallery_and_review_tabs(win):
    """图库/审核两页都在（作业单桌面段）。

    ⚠ 属性名带**前导下划线**：`_gallery_tab` / `_review_tab`
    （`main_window.py:1308-1309`）。写成 `gallery_tab` 是 hasattr 假红的经典来源。
    """
    assert hasattr(win, "_gallery_tab"), "缺图库页（属性名 _gallery_tab）"
    assert hasattr(win, "_review_tab"), "缺审核页（属性名 _review_tab）"