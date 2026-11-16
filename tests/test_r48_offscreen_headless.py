"""R48 / THOROUGH_AUDIT_ORDER G：无头环境 offscreen（笔记本「没有屏幕」报错）。

来源：作业单 G（用户第 7 条）—— 笔记本（本身是**带屏笔记本**）在运行/自测时
老报「没有屏幕」。真因：自动化/无头环境没有 DISPLAY，Qt 默认要屏。

修法：`tests/conftest.py` **顶部**、任何 PySide6 import 之前，兜底设
`QT_QPA_PLATFORM=offscreen`。

⚠ 本文件的三条判据都是**「真跑一次不带环境变量的 pytest」** ——
   这正是原 bug 的复现方式。只读 conftest 源码查「有没有那行字符串」
   是**假绿**：那行可能写在 import 之后（太晚）、可能被 `if False` 关掉、
   可能拼错变量名。必须真跑。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PY_EXE = sys.executable


def _run_pytest_without_qpa(args, timeout=300):
    """跑 pytest，**且确保环境里没有 QT_QPA_PLATFORM / DISPLAY**。"""
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env.pop("DISPLAY", None)
    return subprocess.run(
        [PY_EXE, "-m", "pytest", *args, "-q", "--no-header"],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout)


# ============================================================ 复现原 bug

def test_gui_test_passes_without_qpa_platform():
    """G 核心：**不设** QT_QPA_PLATFORM 也能跑 GUI 测试（原来会报没屏幕）。

    这是作业单 G 的判据本身。子进程里显式 `del os.environ[...]`，
    模拟「有人在没有 DISPLAY 的环境里直接 pytest」。
    """
    # 2026-10-29：原来跑 tests/test_r48_web_frontend.py（纯旧 SPA 静态层，
    # 已随旧版网页一起删除）。改为跑一个**真会 import PySide6 并开窗口**的
    # 用例 —— 判据本意就是「不在 import 前设 offscreen 就会报没屏幕」，
    # 换成不碰 Qt 的测试反而测不到东西。
    p = _run_pytest_without_qpa(
        ["tests/test_r48_watermark_left_consistency.py",
         "-k", "sits_at_left_edge"])
    out = (p.stdout or "") + (p.stderr or "")
    assert p.returncode == 0, f"不带 QT_QPA_PLATFORM 时测试失败：\n{out[-1500:]}"
    assert "no screen" not in out.lower(), f"仍报「没有屏幕」：\n{out[-800:]}"
    assert "failed to open display" not in out.lower(), out[-800:]


def test_minimal_qapplication_starts_headless():
    """G：最小 GUI 用例在 offscreen 下能起 QApplication（不依赖任何测试文件）。

    直接起子进程跑一段 PySide6 代码 —— 验证的是「环境本身可用」。
    """
    code = (
        "import os;"
        "os.environ.pop('QT_QPA_PLATFORM', None);"
        "os.environ.pop('DISPLAY', None);"
        "import sys; sys.path.insert(0, r'%s');"
        "from PySide6.QtWidgets import QApplication, QLabel;"
        "app = QApplication([]);"
        "w = QLabel('hi'); w.show();"
        "app.processEvents();"
        "print('QT_QPA_PLATFORM=', os.environ.get('QT_QPA_PLATFORM'));"
        "print('OK')" % ROOT
    )
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env.pop("DISPLAY", None)
    # 模拟 conftest 的兜底（真实路径是 conftest 顶部那三行）
    code = code.replace("from PySide6.QtWidgets",
                        "os.environ.setdefault('QT_QPA_PLATFORM','offscreen');"
                        "from PySide6.QtWidgets")
    p = subprocess.run([PY_EXE, "-c", code], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    assert p.returncode == 0, f"离屏起 QApplication 失败：{p.stderr[-800:]}"
    assert "OK" in (p.stdout or "")


# ============================================================ 结构护栏

def test_conftest_sets_offscreen_before_qt_import():
    """护栏：conftest 顶部、import PySide6 **之前**就设了 offscreen。

    位置是关键：Qt 的 platform plugin 是首次 QApplication 构造时读的，
    写在 import 之后 = 太晚。
    """
    text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    i_set = text.find("QT_QPA_PLATFORM")
    assert i_set > 0, "conftest 里没设 QT_QPA_PLATFORM"
    # PySide6 的任何 import 都要在它之后（conftest 自己不 import PySide6，
    # 所以查 media_resource_manager 那一批业务 import）
    i_biz = text.find("from media_resource_manager.config import")
    assert i_biz == -1 or i_biz > i_set, (
        "offscreen 设置写在业务 import 之后 —— 可能太晚，Qt 已读过环境变量")


def test_conftest_does_not_override_explicit_choice():
    """护栏：用 `setdefault` 而非直接赋值（真机上有人要 xcb/windows 平台）。"""
    text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    # ⚠ 用单引号字面量匹配：conftest 里我写的是
    #   `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`
    #   —— 第一版断言写成双引号转义版，字符串匹配不上 → **假红**
    #   （测试自己的引号风格问题，不是产品问题）。
    assert "os.environ.setdefault(" in text, (
        "必须用 setdefault —— 直接赋值会覆盖真机上显式指定的平台插件")
    assert "QT_QPA_PLATFORM" in text
    # 反向：不能出现无条件赋值
    assert not re.search(r"^\s*os\.environ\[.QT_QPA_PLATFORM.\]\s*=",
                         text, re.M), (
        "发现无条件赋值 QT_QPA_PLATFORM，会覆盖调用方的选择")


def test_explicit_qpa_platform_is_respected():
    """显式设了别的平台时，conftest **不许覆盖**。"""
    text = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert 'os.environ.get("DISPLAY")' in text, (
        "兜底条件应同时看 DISPLAY（有 DISPLAY 的真机不该被强制离屏）")