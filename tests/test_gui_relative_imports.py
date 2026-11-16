"""Regression: single-dot relative imports inside media_resource_manager/gui
must resolve to real gui submodules.

Background (2026-09-30): `_is_structured_query`/`load_gallery` used
`from .search import ...` while search.py lives at the package root -> every
gallery refresh raised ModuleNotFoundError inside a Qt slot (silently
swallowed by PySide6), leaving the gallery permanently empty.
"""
from __future__ import annotations

import ast
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parent.parent / "media_resource_manager" / "gui"


def _single_dot_imports(path: Path):
    """Yield (lineno, module) for `from .mod import ...` (not `from . import`)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            yield node.lineno, node.module


def test_gui_single_dot_imports_resolve():
    """gui/ 里的 `from .x import ...` 必须**真的能解析**。

    ⚠ R160 恢复原判据（R159 那版是**改弱了**，不是修仪器）：
      R159 把判据换成 importlib 导入 `media_resource_manager.<mod>`
      （**两个点**的语义），于是 R149-B 写错的
      `from .classify.preference import is_learning_enabled`
      （learning_board.py:314，**一个点**）被判成「没问题」。
      真相：一个点 = 当前包自己（media_resource_manager.gui），
      classify 是 gui 的**兄弟包**，够到它必须两个点
      （main_window.py 通篇都是 `from ..classify.preference`）。
      实测 `media_resource_manager.gui.classify` 不存在
      （ModuleNotFoundError），运行时 import 必炸，只是被 PySide6 静默吞掉。
      产品码已在 R160 修成两个点；本护栏恢复「按一个点的真实语义查路径」。

    判据：`from .a.b import ...` 的真实目标 = gui/a/b.py 或 gui/a/b/。
    多级路径整条查（只查第一段会漏 `from .a.b` 这种）。
    """
    broken = []
    for py in GUI_DIR.glob("*.py"):
        for lineno, mod in _single_dot_imports(py):
            stem = GUI_DIR / mod.replace(".", "/")
            if not ((stem.parent / (stem.name + ".py")).exists() or stem.is_dir()):
                broken.append(f"{py.name}:{lineno} from .{mod} import ...")
    assert not broken, "broken gui relative imports:\n" + "\n".join(broken)


def test_search_module_is_importable_from_root():
    """The search helpers must live at the package root (media_resource_manager.search)."""
    from media_resource_manager.search import parse_query, search_images
    c = parse_query("tag:丝袜 site:pictoa.com")
    assert "tags_include" in c and "site" in c
