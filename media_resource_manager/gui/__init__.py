"""GUI package (PySide6). Launch via `mrm gui` or media_resource_manager.gui.launch().

⚠⚠ R112（DOCKER_X86_ORDER §A1）：**本文件不许在模块层 import main_window**。

原因（实测取证，见 .workbuddy/r112_pyside_import_chain.txt）：
`webui.py` 在**请求路径**上会 `from .gui import theme` / `from .gui.theme import ...`
来取皮肤调色板（Web 端要跟着桌面皮肤换色）。而 Python 导入 `pkg.sub` 必定先执行
`pkg/__init__.py` —— 只要本文件在模块层 import main_window，
**无头链路（headless → webui → gui → main_window）就会把 PySide6 拖进进程**。

后果有两层：
  1. 容器镜像按 §C 不装 PySide6 ⇒ ImportError（webui 那几处 try/except 会兜住，
     于是变成**静默降级**：Web 端只剩 3 个硬编码皮肤、强调色为空）—— 比崩更难查；
  2. 即便装了 PySide6，无头进程也白占一份 Qt 内存（挂机功耗预算 45W）。

改成 PEP 562 惰性属性：真正要 `launch` / `MainWindow` 时才导入 main_window，
`from .gui import launch`（`__main__.py:100` 的用法）照旧可用。
"""

from __future__ import annotations

__all__ = ["launch", "MainWindow"]


def __getattr__(name: str):
    """PEP 562：把 `launch` / `MainWindow` 推迟到真正被取用时才导入。"""
    if name in ("launch", "MainWindow"):
        from .main_window import MainWindow as _MainWindow
        from .main_window import launch as _launch

        globals()["MainWindow"] = _MainWindow
        globals()["launch"] = _launch
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
