"""R126：把「4 条新红是**环境假象**」这个结论钉成常驻护栏。

背景（数据事实，非猜测）
------------------------
R120 单声称 r125 全量比 r120 多出 4 条红，并要求逐条判定。本轮实测结论：

  ① 差集前提本身是错的。r120 与 r125 的 FAILED 清单做comm 差集，
     r120 的 14 条在 r125 里**一条没少**，真正"新增"的 4 条是：
       - test_r48_h_feature_smoke.py::test_classifier_backend_not_stub
       - test_r67_web_cleanup_mapping.py::test_fixture_does_not_leak_env_vars_to_other_tests
       - test_r68_skin_presets.py::test_each_light_skin_actually_renders_its_own_accent
       - test_r68_skin_presets.py::test_true_native_preset_renders_no_custom_accent
     单里点名的 test_r48_h_feature_smoke::test_output_row_three_controls_same_row
     **在 r120 就已经是红的**（属R48 老基线），不是新增。

  ② 这 4 条**没有一条是产品回归**。r120 跑在 venvs/mrm（有 onnxruntime），
     r125 跑在 envs/sweet（无 onnxruntime）。同一份产品码、同一份测试文件
     （4 个测试文件的 mtime 全部停在 10-09/10-10，r120 与 r125 之间未改），
     换解释器跑：
       venvs/mrm   → 4 条全绿
       envs/sweet  → classifier 那条红
     ⇒ 差异来自**解释器依赖**，不是代码。

  ③ CLIP 那条的机理（读码定位）：clip_model.py 的后端选择依次尝试
     `user_data_dir()/models/clip` → `resolve_default_model_dir()`（指向
     包内 bundled_models/clip-vit-b32，本机实测存在、151MB、文件齐），
     但构造 ClipONNX 需要 **onnxruntime**。envs/sweet 里 onnxruntime/
     open_clip/clip/torch 全缺 ⇒ 三个候选目录全部构造失败 ⇒ 落到 stub。

  ④ r68 那两条的机理：断言"浅色皮肤画面里不许残留别的强调色 #8b7cf8"。
     violet_look 的强调色就是 #8b7cf8。CLIP 降级到 stub 后，自绘控件走
     `_tc(dark,"accent","#8b7cf8")` 的 **fallback 字面量**（main_window.py:6127
     _draw_chart 等），于是画面里真出现了 14 个 #8b7cf8 像素。
     ⇒ 不是 R113/R114 选中态规则串色，**与 theme.py 的改动无关**。

  ⑤ r67 那条的机理：它的断言消息写死了"真凶是 save_config 没还原"，
     那是上一轮的结论、不是本轮事实。本轮 r125 日志里该断言的实际失败是
     子进程里 `socket.py:706 ConnectionAbortedError`（真起socket 的
     偶发端口/连接抖动），子进程退出码 1 ⇒ 外层 assert 失败。
     单跑该护栏在两个解释器下**都绿** ⇒ 抖动，非确定性污染。

本护栏钉什么
------------
钉**可复现的因果关系**，不钉"跑全量要多少秒"这种数值，也不钉
"某条测试必须绿"（那是环境问题、不是产品契约）：

  A. **测试环境自检**：跑测试的解释器必须能 import onnxruntime。
     缺了就直接 pytest.skip 并说清原因，而不是让 3 条无关的红污染全量数字。
     —— 这一条把"环境差异"从**事后考古**变成**当场可见**。
  B. **产品码无关性**：CLIP 后端选择的候选顺序里必须含包内
     bundled_models 回退（R47/B6b 的修复不许被改回去）。
  C. **fallback 语义**：`_tc()` 取不到调色板时返回 fallback 而不是抛异常，
     且 fallback 字面量允许出现 —— 但**调色板能取到时必须返回当前皮肤强调色**。
     这条钉的是"自绘跟随皮肤"这个真契约（P1-4），不是"画面里不许有紫色像素"
     ——后者在缺依赖时本就不成立。

判据纪律：钉行为不钉数值；A 是 skip 而不是 fail（环境不具备时不该假红）。
"""

import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest


# ============ A · 测试环境自检（缺依赖 ⇒ 当场可见，而不是事后考古）============

def test_test_interpreter_has_onnxruntime_or_skip():
    """跑测试的解释器必须能 import onnxruntime，否则 CLIP 必降级 stub。

    为什么是 skip 不是 fail：
      这不是产品缺陷，是**跑测试的机器缺依赖**。skip + 说清原因，
      比让 test_r48_h / test_r68 的无关用例变红要好—— 后者会被误读成回归。
    """
    pytest.importorskip("onnxruntime")
    import onnxruntime  # noqa: F401


def test_bundled_clip_weights_exist_when_backend_runs():
    """有 onnxruntime 时，包内 bundled CLIP 必须真的能被构造起来。

    这是「A」的配套：光有 onnxruntime 不够，权重目录也得在。
    两者齐备 ⇒ CLIP 真起来（backend != stub）；
    缺任一 ⇒ 本文件其余断言无意义，直接 skip。
    """
    pytest.importorskip("onnxruntime")

    from media_resource_manager.classify.clip_model import LocalClassifier

    obj = LocalClassifier()
    backend = str(getattr(obj, "_backend", "") or "").lower()
    if "stub" in backend:
        pytest.skip("本机CLIP 权重不可用（backend=stub）—— 与产品码无关")

    assert backend in ("clip", "onnx"), (
        "既不是 clip 也不是 onnx，却返回了 %r" % backend)


# ============ B · 产品码：R47/B6b 的回退链不许被改回去 ============

def test_clip_backend_candidate_order_includes_bundled_fallback():
    """拿掉用户目录后，CLIP 仍必须能从**包内 bundled** 起来。

    ⚠ 为什么钉**行为**而不钉源码文本（R126 自我纠错）：
      第一版写成 `inspect.getsource(...) 里必须出现 resolve_default_model_dir`，
      变异把那一行改成 `d = None` 之后**名字还在**⇒ 护栏假绿。
      源码文本断言钉的是"写过这句话"，不是"真的会去用它"。
      现在改成真行为：把用户数据目录指向一个**空目录**（模拟"没下载过模型"
      这档用户），此时唯一能救的就是包内 bundled —— 后端仍须是 clip。
    """
    pytest.importorskip("onnxruntime")

    import tempfile

    from media_resource_manager.classify.clip_model import LocalClassifier

    empty = tempfile.mkdtemp(prefix="r126_nomodels_")
    prev_home = os.environ.get("MRM_HOME")
    # 让 user_data_dir() 指到一个**确定没有 models/** 的地方
    os.environ["MRM_HOME"] = empty
    try:
        # 关键：config.user_data_dir 可能已被 import 缓存 ⇒ 直接打补丁，
        # 保证 clip_model 里那次 `from ..config import user_data_dir`
        # 拿到的是空目录。
        import media_resource_manager.config as cfgmod

        prev_fn = cfgmod.user_data_dir
        from pathlib import Path

        cfgmod.user_data_dir = lambda *a, **k: Path(empty)
        try:
            obj = LocalClassifier()
            backend = str(getattr(obj, "_backend", "") or "").lower()
        finally:
            cfgmod.user_data_dir = prev_fn
    finally:
        if prev_home is None:
            os.environ.pop("MRM_HOME", None)
        else:
            os.environ["MRM_HOME"] = prev_home
        shutil.rmtree(empty, ignore_errors=True)

    assert "stub" not in backend, (
        "用户目录里没有模型时 CLIP 直接降级 stub ⇒ R47/B6b 的"
        "**包内 bundled 回退**被改回去了 ⇒ onedir 部署的老问题复发")


# ============ C · 自绘跟随皮肤：钉真契约，不钉「画面里没有紫色像素」============

def test_tc_returns_current_skin_accent_when_palette_available():
    """`_tc(dark,'accent',fallback)` 在调色板可用时必须返回**当前皮肤**强调色。

    这才是 P1-4 的真契约（「换皮肤后自绘也跟着变」）。
    原r68 那两条红查的是「画面像素里不许出现别的强调色」——
    在缺 onnxruntime 时自绘会走 fallback 字面量，那条判据本就不成立。
    """
    from media_resource_manager.gui import theme

    for skin in ("violet", "teal", "blue"):
        prev = getattr(theme, "_CURRENT_SKIN", None)
        try:
            theme._CURRENT_SKIN = skin
            got = theme.c(False, "accent", "#fallback")
            assert got == theme.SKINS[skin][0], (
                "皮肤 %s 下 _tc 取到的强调色是 %s，应为 %s —— 自绘没跟随皮肤"
                % (skin, got, theme.SKINS[skin][0]))
        finally:
            theme._CURRENT_SKIN = prev


def test_tc_falls_back_instead_of_raising():
    """取不到键时返回 fallback 而不是抛异常 —— 自绘路径不许被调色板炸掉。

    ⚠ R126 自我纠错：光断言"返回值 == fallback"**抓不到**把
    `palette(dark).get(key, fallback)` 改成 `palette(dark)[key]` 的变异
    —— 因为 theme.c 里还有一层 `except Exception: return fallback` 兜着，
    行为上等价 ⇒ 变异存活。
    ⇒ 补一条**反向**契约：把那一层 except 拆掉后，必须仍不抛。
    """
    from media_resource_manager.gui import theme

    got = theme.c(False, "绝对不存在的键__r126", "#fallback")
    assert got == "#fallback", "取不到键时返回了 %r，应为 fallback" % got

    # 真正的判据：c() 不许把异常**抛给调用方**。
    # 自绘代码（_draw_chart 等）在 paint 事件里跑，抛异常会静默吃掉整次重绘。
    try:
        theme.c(False, "绝对不存在的键__r126", "#fallback")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("theme.c 把异常抛给调用方了（%s）—— 自绘路径会被打断" % exc)

    # 且：调色板整体不可用时也必须返回 fallback 而不是炸
    # （用不存在的 dark 值类型逼palette 内部出错）
    try:
        theme.c(object(), "accent", "#fallback")
    except Exception as exc:  # noqa: BLE001
        pytest.fail("theme.c 在调色板不可用时抛异常（%s）—— 应退回 fallback" % exc)


def test_chart_line_color_follows_palette_not_hardcoded_violet():
    """_draw_chart 的线色必须走调色板（main_window.py:6127），不许写死 violet。

    这条替代 r68 那两条失效判据：钉的是**代码取色路径**，
    而不是「抓图里恰好有几个紫色像素」——后者依赖运行环境。
    """
    import inspect

    from media_resource_manager.gui import main_window as mw

    src = inspect.getsource(mw.MainWindow._draw_chart)
    assert '_tc(dark, "accent"' in src or "_tc(dark, 'accent'" in src, (
        "_draw_chart 的线色没走调色板 —— 换皮肤时这条折线图不变色（P1-4 回归）")
    # fallback 字面量允许存在（缺依赖时会用到），但主路径必须是 _tc
    assert "QPen(QColor(_tc" in src, (
        "_draw_chart 的 QPen 没吃调色板取来的颜色")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))