"""R203 护栏：R179 白/黑名单词横排自适应换行（**去掉「更多」**）。

背景（订单原话）：词多时区域显示「更多」按钮省略、不换行；用户要的是
「词多就横排铺过去、自动换行、随窗口宽度自适应，不要更多」。

⚠ 为什么必须显式撑高（`fit_flow_height`）：
  `_FlowLayout` 早就有 `heightForWidth`，但 R189 实测容器高度被父布局按
  **单行**给死（29px）⇒ 第二行直接被裁到容器外，表现就是「常用词盖住旁边
  内容」（B11 老毛病）。所以不能指望 HFW 自动往上传播：算完直接
  `setMinimumHeight`，数字握在自己手里。

判据口径：行为优先（真 QApplication + 真布局数学），源码判据只钉「不许再
把词切掉 / 不许再生出「更多」」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MW = ROOT / "media_resource_manager" / "gui" / "main_window.py"
SRC = MW.read_text(encoding="utf-8")

pytest.importorskip("PySide6")
from PySide6.QtCore import QPoint, QRect                   # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from media_resource_manager.gui import main_window as mw    # noqa: E402


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _chip(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setMaximumWidth(150)
    return b


def _box_with_chips(n: int = 8, width: int = 250):
    """一个装了 n 个 chip 的 FlowLayout 容器（宽度 width）。"""
    _app()
    box = QWidget()
    lay = mw._FlowLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    chips = [_chip(f"keyword{i:02d}") for i in range(n)]
    for c in chips:
        lay.addWidget(c)
    box.resize(width, 40)
    return box, lay, chips


# ───────────────────────────────────────── 行为：多行高度与「不越界」
def test_flow_layout_reports_multi_row_height():
    box, lay, chips = _box_with_chips(8, 250)
    _app().processEvents()
    one = chips[0].sizeHint().height()
    h = lay.heightForWidth(250)
    assert h > one + 4, (
        f"8 个 chip 在 250px 宽下必须换成多行，实测高度 {h}（单行 {one}）")


def test_fit_flow_height_pushes_minimum_height():
    """容器必须被**真的撑高**（这就是 R189「第二行被裁」的根因）。"""
    box, lay, _ = _box_with_chips(8, 250)
    _app().processEvents()
    want = int(lay.heightForWidth(250))
    got = mw.fit_flow_height(box, lay)
    assert got >= want
    assert box.minimumHeight() == got
    assert box.minimumHeight() > 24, "没撑到多行的高度，第二行会被裁掉"


def test_all_chips_stay_inside_container_after_wrap():
    """B11 不变式（多行版）：每个 chip 都必须在容器矩形内，一个都不许越界。"""
    box, lay, chips = _box_with_chips(10, 300)
    _app().processEvents()
    mw.fit_flow_height(box, lay)
    box.resize(300, box.minimumHeight())
    lay.setGeometry(box.rect())
    _app().processEvents()
    for c in chips:
        r = QRect(c.mapTo(box, QPoint(0, 0)), c.size())
        assert box.rect().contains(r), (
            f"chip {c.text()!r} 越界：{r.getRect()} vs {box.rect().getRect()}")


def test_refit_on_resize():
    """拖窗口变宽/变窄：容器高度要跟着重算（订单「随窗口宽度自适应」）。"""
    box, lay, _ = _box_with_chips(8, 400)
    _app().processEvents()
    mw.fit_flow_height(box, lay)
    wide = box.minimumHeight()
    filt = mw._FlowRefit(box, lay)
    box.resize(160, box.height())
    _app().processEvents()
    try:
        assert box.minimumHeight() > wide, (
            "窗口变窄后行数变多，容器却没变高 ⇒ 必然裁掉下面几行")
    finally:
        box.removeEventFilter(filt)


def test_fit_flow_height_is_robust():
    """宽 0 / 异常布局都不能把 GUI 槽炸掉（只要求返回个正数）。"""
    box = QWidget()
    lay = mw._FlowLayout(box)
    assert mw.fit_flow_height(box, lay) >= 24


# ───────────────────────────────────────── 源码：不许再把词切掉 / 再生「更多」
def _refresh_fn_src() -> str:
    """取 `_refresh_hist_chips` 的函数体（不含后面的别的方法）。"""
    body = SRC.split("    def _refresh_hist_chips(", 1)[1]
    return body.split("\n    def ", 1)[0]


def test_no_more_button_is_created_anymore():
    body = _refresh_fn_src()
    assert "更多(" not in body, "R179：不许再有「更多(N)…」按钮"
    assert "shown" not in body, "R179：不许再按可见额度把词切掉"
    assert "_chip_w" not in body, "试算宽度的死代码要一起删掉"


def test_every_word_gets_a_chip():
    body = _refresh_fn_src()
    assert "for wd in words:" in body, "必须遍历**全部**词，不再是 shown 子集"


def test_refresh_calls_fit_and_box_has_refit_filter():
    body = _refresh_fn_src()
    assert "fit_flow_height(box, lay)" in body, "重建后必须重算容器高度"
    assert "_FlowRefit(box, bl)" in SRC, "容器没装 resize 重算过滤器"
