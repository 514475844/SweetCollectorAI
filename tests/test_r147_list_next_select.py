"""R147 列表模式 / R151 网格模式「操作后选中下一张」护栏。

钉住 fill_table 的 select_row 语义（列表模式走 selectRow，网格模式复用同款
"按旧行号选中=下一张"思路，见 main_window.load_gallery 的 _gal_anchor_grid）：

  · 初载（select_row=None）            → 选第一行；
  · 删中间第 k 行 → 重灌后选第 k 行      = 原第 k+1 张（下一张）；
  · 删末行 index=N-1 → 选新末行 N-2     ；
  · 删空表                            → 不选（currentRow==-1）；
  · anchor 越界（>count-1）           → 钳到末行不越界；
  · anchor 为负                       → 钳到首行。

反向变异：把 select_row 分支改回 `tbl.selectRow(0)` 会让"删后选中下一张"断言
失败 ⇒ 护栏变红。这是 R147/R151 不许"每次操作后跳回第一行"的契约钉。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtWidgets import QApplication, QTableWidget
from PySide6.QtCore import Qt

app = QApplication.instance() or QApplication([])

from media_resource_manager.gui.table_view import fill_table


def _rows(n):
    return [{
        "id": i + 1,
        "path": f"/tmp/noexist_{i + 1}.png",
        "task_id": 1, "score": 0.5, "bytes": 1024,
        "width": 80, "height": 80, "source_url": "",
        "favorite": False, "collected_at": None,
    } for i in range(n)]


def _mk_tbl():
    return QTableWidget(0, 8)


def test_initial_selects_first_row():
    t = _mk_tbl()
    fill_table(t, _rows(5), None)
    assert t.currentRow() == 0


def test_delete_middle_selects_next():
    t = _mk_tbl()
    rows = _rows(5)
    after = rows[:2] + rows[3:]          # 删 index 2
    fill_table(t, after, None, select_row=2)
    assert t.currentRow() == 2
    assert t.item(2, 0).data(Qt.ItemDataRole.UserRole) == 4  # 原第 3 张被删，下一张=id4


def test_delete_last_selects_new_last():
    t = _mk_tbl()
    after = _rows(5)[:4]                 # 删末行 index 4
    fill_table(t, after, None, select_row=4)
    assert t.currentRow() == 3
    assert t.item(3, 0).data(Qt.ItemDataRole.UserRole) == 4


def test_delete_empty_no_selection():
    t = _mk_tbl()
    fill_table(t, [], None, select_row=0)
    assert t.rowCount() == 0
    assert t.currentRow() == -1


def test_anchor_out_of_range_clamped():
    t = _mk_tbl()
    fill_table(t, _rows(3), None, select_row=99)
    assert t.currentRow() == 2           # 钳到末行不越界


def test_anchor_negative_clamped_min():
    t = _mk_tbl()
    fill_table(t, _rows(3), None, select_row=-5)
    assert t.currentRow() == 0
