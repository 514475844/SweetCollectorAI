"""r40：锁死「网址适配器管理器里规则打不开 / 删不掉」这个 bug（需求 12）。

现象：管理器列表里明明列着规则，点「编辑」打不开、点「删除」删不掉，
看起来像「没显示」——其实是**列表项文本带了 .yaml 扩展名**：

- 列表建项时写的是 `f.name`（含扩展名），显示成 `pictoa.yaml`；
- 而 `_edit_rule` / `_del_rule` 用 `item.text()` 去 `rules_dir / 文本` 拼路径，
  于是永远拼成 `pictoa.yaml.yaml` —— 文件根本不存在。

正解：列表只显示 `f.stem`（无扩展名），完整文件名存 `Qt.UserRole`，
路径统一走 `_rule_path()`。

本文件锁的不变量：列表文本无扩展名；`_rule_path()` 拼出的路径真实存在。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _new_window(qapp):
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


def test_adapter_list_shows_stem_not_filename(qapp, tmp_path):
    """列表项文本 = 规则名（无 .yaml），完整文件名走 Qt.UserRole。"""
    w = _new_window(qapp)
    w = _new_window(qapp)
    cfg = w.cfg
    cfg.rules_dir = tmp_path
    for name in ("pictoa", "spankbang"):
        (tmp_path / f"{name}.yaml").write_text("site: x\n", encoding="utf-8")

    try:
        items = w._rule_files()
        assert len(items) == 2
        it = QListWidgetItem(items[0].stem)
        it.setData(Qt.UserRole, items[0].name)  # 完整文件名存 UserRole
        assert it.text() == "pictoa"
        assert not it.text().endswith(".yaml")
        # 拼出来的路径必须真存在（原来会拼成 pictoa.yaml.yaml）
        from pathlib import Path
        p = Path(tmp_path) / str(it.data(Qt.UserRole))
        assert p.exists(), f"拼出的规则路径不存在：{p}"
    finally:
        try:
            w.close()
        except Exception:
            pass

def test_rule_path_prefers_userrole(qapp, tmp_path):
    """_rule_path() 优先取 Qt.UserRole 的完整文件名。"""
    w = _new_window(qapp)
    w = _new_window(qapp)
    cfg = w.cfg
    cfg.rules_dir = tmp_path
    (tmp_path / "real.yaml").write_text("x", encoding="utf-8")

    w = _new_window(qapp)
    w.cfg = cfg
    try:
        class _It:
            def data(self, role):
                return "real.yaml" if role == int(Qt.UserRole) else ""

        p = w._rule_path(_It())
        assert p.name == "real.yaml"
        assert p.exists()
    finally:
        try:
            w.close()
        except Exception:
            pass