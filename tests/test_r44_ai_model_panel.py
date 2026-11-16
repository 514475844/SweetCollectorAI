"""r44（#44）：AI 模型面板重做。

锁几条不变量（旧代码全不满足）：
1. 面板有「模式」下拉，4 个选项：内置 CLIP / 未下载 / 到本地 / 在线 API；
2. 按所选模式条件显隐：选「到本地」才显本地目录、选「在线API」才显 API 配置、
   选「未下载」才显模型库下载面板；其余情况这些区域都隐藏（#44 核心契约）；
3. 没有英文 OK/Cancel 按钮（禁 OK/Cancel），改为全中文「保存 / 关闭」；
   推理设备（CPU/GPU）与推理线程数都收进本面板。
另外验证 do_save 的 backend 映射（内置→builtin，未下载/到本地→local，在线→api）。
"""
import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                               QPushButton, QLineEdit)  # noqa: E402


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _win():
    from media_resource_manager.gui.main_window import MainWindow
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


def _rec_exec(monkeypatch):
    """把 QDialog.exec 换成探针：记录被 exec 的对话框并直接返回 Rejected。"""
    seen = []
    monkeypatch.setattr(
        QDialog, "exec",
        lambda self, *a, **k: (seen.append(self)
                               or QDialog.DialogCode.Rejected))
    return seen


def test_mode_dropdown_has_three_modes(qapp, monkeypatch):
    """2026-10-27 用户要求：删掉「到本地（自定义本地模型目录）」这一档。

    理由：它和下面模型库的「未下载/已就绪」是同一件事的两个入口，
    第三方模型用户更习惯走在线 API。现在只剩 内置 / 未下载 / 在线 API。
    """
    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        w.ai_model_dialog()
        dlg = seen[0]
        combo = dlg._mode_combo
        datas = [combo.itemData(i) for i in range(combo.count())]
        assert datas == ["builtin", "notdownloaded", "api"], datas
        assert "local" not in datas, "「到本地」档已按用户要求删除"
        assert combo.currentData() == "builtin"   # 默认内置 CLIP
    finally:
        w.close()


def test_visibility_by_mode(qapp, monkeypatch):
    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        w.ai_model_dialog()
        dlg = seen[0]
        combo = dlg._mode_combo

        assert (dlg._reveal_local, dlg._reveal_api, dlg._reveal_dl) == (False, False, False)

        # 「到本地」档已删除（2026-10-27），findData 返回 -1，索引不会真的切走
        assert combo.findData("local") == -1, "不该再有 local 档"

        combo.setCurrentIndex(combo.findData("api"))
        dlg._apply_mode()
        assert (dlg._reveal_local, dlg._reveal_api, dlg._reveal_dl) == (False, True, False)

        combo.setCurrentIndex(combo.findData("notdownloaded"))
        dlg._apply_mode()
        assert (dlg._reveal_local, dlg._reveal_api, dlg._reveal_dl) == (False, False, True)

        combo.setCurrentIndex(combo.findData("builtin"))
        dlg._apply_mode()
        assert (dlg._reveal_local, dlg._reveal_api, dlg._reveal_dl) == (False, False, False)
    finally:
        w.close()


def test_no_ok_cancel_chinese_buttons(qapp, monkeypatch):
    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        w.ai_model_dialog()
        dlg = seen[0]
        assert dlg.findChildren(QDialogButtonBox) == [], "出现 OK/Cancel 按钮框"
        texts = [b.text() for b in dlg.findChildren(QPushButton)]
        assert "保存" in texts and "关闭" in texts, texts
    finally:
        w.close()


def test_device_and_threads_in_panel(qapp, monkeypatch):
    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        w.ai_model_dialog()
        dlg = seen[0]
        dev = dlg._device_combo
        # 2026-11-11：三档 → 四档（自动 / 强制显卡 / 强制 NPU / 强制 CPU）。
        # 顺序与桌面「AI与过滤 → 推理设备」菜单一致；联动处一律按 findData
        # 定位，不能写死索引（加了 NPU 后写死索引会把 CPU 指到 NPU）。
        assert [dev.itemData(i) for i in range(dev.count())] == [
            "auto", "gpu", "npu", "cpu"]
        spin = dlg._threads_spin
        assert spin.minimum() == 0 and spin.maximum() == 4
    finally:
        w.close()


def test_save_maps_backend(qapp, monkeypatch):
    import media_resource_manager.config as cfgmod
    saved = []
    monkeypatch.setattr(cfgmod, "save_config", lambda c: saved.append(c))

    seen = _rec_exec(monkeypatch)
    w = _win()
    try:
        w.ai_model_dialog()
        dlg = seen[0]
        combo = dlg._mode_combo

        # 选在线 API，按 placeholder 给三个 API 框填值，调保存
        combo.setCurrentIndex(combo.findData("api"))
        dlg._apply_mode()
        for le in dlg.findChildren(QLineEdit):
            ph = le.placeholderText() or ""
            if "api.openai.com" in ph:
                le.setText("https://example.com/v1")
            elif "sk-" in ph:
                le.setText("sk-test")
            elif "gpt-4o-mini" in ph:
                le.setText("gpt-4o-mini")
        dlg._do_save()
        assert saved, "save_config 没被调用"
        cfg = saved[-1]
        assert cfg.ai_backend == "api"
        assert cfg.ai_api_url == "https://example.com/v1"
        assert cfg.ai_api_key == "sk-test"
        assert cfg.ai_api_model == "gpt-4o-mini"
    finally:
        w.close()
