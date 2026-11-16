"""新增小功能的回归测试：AI 词「曾用词」历史 + 内置灯箱翻页。

这些逻辑以前没有测试覆盖，而它们一旦写错会直接让人用不了
（历史顺序反了 / 重复塞词 / 灯箱翻页越界），所以补上。
"""
import pytest

pytest.importorskip("PySide6")

from media_resource_manager.config import AppConfig  # noqa: E402


class _Line:
    """够用的 QLineEdit 替身（只需 text / setText）。"""

    def __init__(self, text=""):
        self._t = text

    def text(self):
        return self._t

    def setText(self, s):
        self._t = s


def _stub(cfg):
    from types import SimpleNamespace
    return SimpleNamespace(cfg=cfg)


def test_remember_words_newest_first_and_dedup():
    from media_resource_manager.gui.main_window import MainWindow
    cfg = AppConfig()
    s = _stub(cfg)
    MainWindow._remember_words(s, ["白丝", "cat"], "ai_label_history")
    # 最后用到的排最前
    assert cfg.ai_label_history == ["cat", "白丝"]
    # 再次用到旧词：提到最前，且不产生重复项
    MainWindow._remember_words(s, ["白丝"], "ai_label_history")
    assert cfg.ai_label_history == ["白丝", "cat"]


def test_remember_words_ignores_blank_and_caps_at_30():
    from media_resource_manager.gui.main_window import MainWindow
    cfg = AppConfig()
    s = _stub(cfg)
    MainWindow._remember_words(s, ["  ", "", "ok"], "ai_excl_history")
    assert cfg.ai_excl_history == ["ok"]
    MainWindow._remember_words(s, [f"w{i}" for i in range(40)],
                               "ai_excl_history")
    assert len(cfg.ai_excl_history) == 30
    assert cfg.ai_excl_history[0] == "w39"      # 最新的在最前


def test_append_word_space_separated_and_no_duplicate():
    from media_resource_manager.gui.main_window import MainWindow
    line = _Line("白丝")
    s = _stub(AppConfig())
    MainWindow._append_word(s, line, "cat")
    assert line.text() == "白丝 cat"
    MainWindow._append_word(s, line, "cat")     # 已存在 → 不重复添加
    assert line.text() == "白丝 cat"


def test_two_histories_do_not_mix():
    """主题词历史和排除词历史必须各存各的。"""
    from media_resource_manager.gui.main_window import MainWindow
    cfg = AppConfig()
    s = _stub(cfg)
    MainWindow._remember_words(s, ["keep"], "ai_label_history")
    MainWindow._remember_words(s, ["drop"], "ai_excl_history")
    assert cfg.ai_label_history == ["keep"]
    assert cfg.ai_excl_history == ["drop"]


def test_lightbox_wraps_around_and_tolerates_missing_file():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from media_resource_manager.gui.lightbox import Lightbox

    rows = [
        {"path": "/definitely/not/here/a.jpg", "width": 1, "height": 2,
         "source_url": "u1", "tags": "t1", "score": 0.5},
        {"path": "/definitely/not/here/b.jpg", "width": 3, "height": 4,
         "source_url": "u2", "tags": "t2", "score": 0.6},
    ]
    lb = Lightbox(rows, 0)
    assert lb.i == 0
    lb.step(1)
    assert lb.i == 1
    lb.step(1)          # 到末尾再往后 → 回到第一张（循环）
    assert lb.i == 0
    lb.step(-1)         # 第一张往前 → 到最后一张
    assert lb.i == 1
    lb.close()

    # 起始下标越界也要能夹回来，不能崩
    lb2 = Lightbox(rows, 99)
    assert lb2.i == 1
    lb2.close()


def test_lightbox_empty_rows_is_safe():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from media_resource_manager.gui.lightbox import Lightbox
    lb = Lightbox([], 0)
    lb.step(1)          # 没有图时翻页不应报错
    lb.close()
