"""R48-B 曾用词「全部填满」守卫 —— 2026-10-27 按 R52/R53b 新设计重写。

历史沿革（为什么这文件整体换血）：
    R51：全局唯一「全部填满」按钮（btnFillAllHistory），点一次填四行。
    R52/R53b：**设计变更**——按用户「分级全部填满」诉求，改为**每个级别
    各自一个**（objectName = btnFillOne_<cfg_key>），点哪个只填哪一级；
    全局按钮被移除。旧断言按旧结构找按钮 → 集体假红（11 条）。

本文件现在守的是新设计的四条语义（都能测，别靠肉眼）：
    ① 四个级别（一/二/三级白名单 + 黑名单）各有自己的填充按钮；
    ② 只追加不覆盖、重复词不重复加（幂等）；
    ③ 绝不跨级串词（一级词不串进二级桶，黑名单词不串进白名单）；
    ④ 主题词永不进排除词（全局铁律）。
"""
import sys

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QPushButton  # noqa: E402

from media_resource_manager.gui import theme  # noqa: E402
from media_resource_manager.gui.main_window import MainWindow  # noqa: E402

KEYS = (
    "ai_label_history",
    "ai_label_history_l2",
    "ai_label_history_l3",
    "ai_excl_history",
)
FIELDS = {
    "ai_label_history": "f_ai_labels",
    "ai_label_history_l2": "f_ai_l2",
    "ai_label_history_l3": "f_ai_l3",
    "ai_excl_history": "f_ai_excl",
}


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def win(qapp):
    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    w._ensure_task_form()
    for _ in range(3):
        qapp.processEvents()
    saved = {k: list(getattr(w.cfg, k, []) or []) for k in KEYS}
    yield w
    for k, v in saved.items():
        setattr(w.cfg, k, v)
    try:
        w.close()
    except Exception:
        pass
    qapp.setStyleSheet("")


@pytest.fixture(autouse=True)
def _reset_shared_state(win):
    """共享窗口逐用例复位（module 粒度的必要配套，防「单跑绿合跑红」）。"""
    saved = {k: list(getattr(win.cfg, k, []) or []) for k in KEYS}
    fields = tuple(FIELDS.values())
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(win.cfg, k, list(v))
        for name in fields:
            w = getattr(win, name, None)
            if w is not None:
                w.setText("")


def _btn(win, key):
    btns = win.findChildren(QPushButton, "btnFillOne_" + key)
    assert btns, f"找不到该级别的填充按钮 btnFillOne_{key}"
    return btns[0]


# ----------------------------------------------------------- ① 结构：四级各有按钮

def test_each_level_has_its_own_fill_button(win):
    for key in KEYS:
        b = _btn(win, key)
        assert not b.isHidden(), f"btnFillOne_{key} 存在但被隐藏"


def test_global_fill_all_button_is_gone(win):
    """R52/R53b 设计变更：全局按钮被移除，不许它阴魂不散地回来。"""
    assert win.findChildren(QPushButton, "btnFillAllHistory") == []


# ----------------------------------------------------------- ② 语义：填自己的那级

def test_fill_one_level_fills_its_own_field(win):
    win.cfg.ai_label_history = ["丝袜", "美腿"]
    _btn(win, "ai_label_history").click()
    assert win.f_ai_labels.text().split() == ["丝袜", "美腿"]


def test_exclusions_fill_only_exclusions(win):
    win.cfg.ai_excl_history = ["黑词A", "黑词B"]
    _btn(win, "ai_excl_history").click()
    assert win.f_ai_excl.text().split() == ["黑词A", "黑词B"]
    # 白名单三个框必须纹丝不动
    assert win.f_ai_labels.text() == ""
    assert win.f_ai_l2.text() == ""
    assert win.f_ai_l3.text() == ""


# ----------------------------------------------------------- ③ 追加 / 幂等

def test_fill_is_append_not_overwrite(win):
    win.cfg.ai_label_history = ["丝袜", "美腿"]
    win.f_ai_labels.setText("已有词")
    _btn(win, "ai_label_history").click()
    words = win.f_ai_labels.text().split()
    assert words[0] == "已有词", "先填的内容被覆盖 = 丢用户输入"
    assert set(words[1:]) == {"丝袜", "美腿"}


def test_fill_is_idempotent(win):
    win.cfg.ai_label_history = ["丝袜", "美腿"]
    _btn(win, "ai_label_history").click()
    once = win.f_ai_labels.text()
    _btn(win, "ai_label_history").click()
    assert win.f_ai_labels.text() == once, "点两次出现重复词"


# ----------------------------------------------------------- ④ 跨级 / 铁律

def test_no_cross_level_contamination(win):
    win.cfg.ai_label_history_l2 = ["二级词"]
    win.cfg.ai_label_history_l3 = ["三级词"]
    _btn(win, "ai_label_history").click()          # 只点一级
    assert win.f_ai_l2.text() == "", "一级填充把词串进了二级"
    assert win.f_ai_l3.text() == "", "一级填充把词串进了三级"


def test_theme_words_never_into_exclusions(win):
    """全局铁律：主题词无论怎么填，都不能出现在排除词框里。"""
    win.cfg.ai_label_history = ["主题词X"]
    win.cfg.ai_label_history_l2 = ["主题词Y"]
    _btn(win, "ai_label_history").click()
    _btn(win, "ai_label_history_l2").click()
    assert win.f_ai_excl.text() == "", "主题词串进了排除词（全局铁律被破）"


# ----------------------------------------------------------- ⑤ 空历史安全

def test_empty_history_keeps_field_untouched(win):
    win.cfg.ai_label_history = []
    win.f_ai_labels.setText("手填词")
    _btn(win, "ai_label_history").click()
    assert win.f_ai_labels.text() == "手填词", "空历史不该清空已有内容"
