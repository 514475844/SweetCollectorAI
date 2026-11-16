"""R148 图片旋转护栏（桌面端灯箱 + 列表预览，旋转只改显示不写原文件）。

钉住：
  · 灯箱点旋转一次 → 角度 +90°（rotation()==90），且显示 pixmap 真的转了
    （120×40 源图旋转前横向 w>h，转 90° 后纵向 w<h）；
  · 连点 4 次回到 0°；
  · 翻页（step）后角度复位回 0°（不把上一张角度带过来）；
  · 列表预览 PreviewLabel 同样 rotate_cw 一次 = +90°，且原文件/pixmap 未被改。
反向变异：把 rotate_cw 改回 no-op 或显示图不旋转，会让"转角后变纵向"断言
失败 ⇒ 护栏变红。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt

app = QApplication.instance() or QApplication([])

from media_resource_manager.gui.lightbox import Lightbox
from media_resource_manager.gui.table_view import PreviewLabel


def _mk_non_square(tmp_path):
    """造一张 120×40 的非正方形本地 PNG（转 90° 后宽高应对调）。"""
    from PIL import Image
    p = tmp_path / "rot_test.png"
    Image.new("RGB", (120, 40), (120, 90, 200)).save(str(p))
    return p


def test_lightbox_rotate_cw_90_and_orientation_flips(tmp_path):
    p = _mk_non_square(tmp_path)
    lb = Lightbox([{"path": str(p)}], index=0)
    lb.img.resize(200, 200)
    lb.render()
    assert lb.img.rotation() == 0
    pm0 = lb.img.pixmap()
    assert pm0.width() > pm0.height()           # 原始 120×40 横向
    lb.rotate()
    assert lb.img.rotation() == 90
    pm1 = lb.img.pixmap()
    assert pm1.width() < pm1.height()           # 转 90° 后纵向


def test_lightbox_rotate_four_times_back_to_zero(tmp_path):
    p = _mk_non_square(tmp_path)
    lb = Lightbox([{"path": str(p)}], index=0)
    lb.img.resize(200, 200)
    lb.render()
    for _ in range(4):
        lb.rotate()
    assert lb.img.rotation() == 0


def test_lightbox_rotate_resets_on_page_turn(tmp_path):
    p = _mk_non_square(tmp_path)
    from PIL import Image
    q = tmp_path / "rot_test2.png"
    Image.new("RGB", (120, 40), (10, 200, 90)).save(str(q))
    lb = Lightbox([{"path": str(p)}, {"path": str(q)}], index=0)
    lb.img.resize(200, 200)
    lb.render()
    lb.rotate()
    assert lb.img.rotation() == 90
    lb.step(1)                                  # 翻页 → 角度复位
    assert lb.img.rotation() == 0


def test_preview_label_rotate_cw(tmp_path):
    p = _mk_non_square(tmp_path)
    lab = PreviewLabel("x")
    lab.resize(200, 200)
    pm = QPixmap(str(p))
    lab.setSource(pm)
    pm0 = lab.pixmap()
    assert pm0.width() > pm0.height()           # 原始横向
    lab.rotate_cw()
    assert lab.rotation() == 90
    pm1 = lab.pixmap()
    assert pm1.width() < pm1.height()           # 转 90° 后纵向
    # 只改显示：原 pixmap 不能被旋转污染
    assert pm.width() == 120 and pm.height() == 40
