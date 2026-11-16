"""r40 需求 30：「查找类似图片」端到端。

锁三件事：
1. **算法**：主题词/色调/pHash 三路加权，主题词权重最高；通用词（可爱/高清）
   不参与 Jaccard；phash 缺失按最不像算而不是崩。
2. **菜单接线**：SSIM 那个入口必须消失、右键和顶栏都要接到新方法上。
3. **结果窗**：能建起来、标签是中文（i18n 生效）、点结果换种子会重排。
"""
from __future__ import annotations

import pytest

from media_resource_manager import simsearch as ss


# ----------------------------- 算法 -----------------------------

def test_weights_follow_demo():
    """三路权重必须还是 demo 验收过的那组（0.42 / 0.33 / 0.25）。"""
    assert ss.W_WORD == 0.42
    assert ss.W_COLOR == 0.33
    assert ss.W_HASH == 0.25
    assert abs(ss.W_WORD + ss.W_COLOR + ss.W_HASH - 1.0) < 1e-9


def test_jaccard_ignores_generic_words():
    # 只共享通用词 → 不算相似（否则「都带 cute」会把全库连成一片）
    assert ss.jaccard(["cute", "高清"], ["cute", "高清"]) == 0.0
    assert ss.jaccard(["cute", "高清"], ["cute", "高清", "nsfw"]) == 0.0
    # 有实义词才计分：交集1 并集2
    assert ss.jaccard(["stockings", "cute"], ["stockings", "pantyhose"]) == 0.5
    assert ss.jaccard(["stockings"], ["stockings"]) == 1.0
    # 通用词不进入分母：加了 cute 不该拉低得分
    assert ss.jaccard(["stockings", "cute"], ["stockings", "pantyhose", "cute"]) == 0.5


def test_hamming_and_hash_score():
    assert ss.hamming64("0000000000000000", "0000000000000000") == 0
    assert ss.hamming64("0000000000000000", "000000000000000f") == 4
    assert ss.hash_score("0" * 16, "0" * 16) == 1.0
    assert ss.hash_score("0" * 16, "f" * 16) == 0.0
    # phash 缺失不能崩，按最不像算
    assert ss.hamming64("", "") == 64
    assert ss.hamming64(None, "abc") == 64


def test_color_score_identical_is_one():
    cells = [[10, 20, 30]] * 9
    avg = [1, 2, 3]
    assert ss.color_score((cells, avg), (cells, avg)) == pytest.approx(1.0)
    far = [[250, 250, 250]] * 9
    assert ss.color_score((cells, avg), (far, [255, 255, 255])) < 0.6
    assert ss.color_score(None, (cells, avg)) == 0.0


def test_grid3x3_uses_module_level_box():
    """回归：曾经写成 im.BOX → AttributeError → 整条链静默返回 None。

    真图跑一遍，特征必须是 9 格 + 整图色，且不能是 None。
    """
    tmp = pytest.importorskip("tmp_path")
    from PIL import Image
    p = tmp / "g.png"
    Image.new("RGB", (300, 300), (10, 200, 30)).save(p)
    feat = ss.features_for_image(str(p))
    assert feat is not None, "features_for_image 返回 None（Pillow 常量用错？）"
    cells, avg = feat
    assert len(cells) == 9
    assert avg == [10, 200, 30]
    # 纯色图的九宫格必须都等于整图色
    assert all(c == [10, 200, 30] for c in cells)


def test_features_for_image_missing_returns_none():
    assert ss.features_for_image(r"D:\definitely\not\here.jpg") is None


def test_rank_similar_orders_by_total_score():
    seed = {"id": 1, "path": None, "tags": ["stockings", "pantyhose"],
            "phash": "0000000000000000", "color": ([[10, 10, 10]] * 9, [10, 10, 10])}
    cands = [
        {"id": 2, "path": None, "tags": ["stockings", "pantyhose"],
         "phash": "0000000000000000",
         "color": ([[10, 10, 10]] * 9, [10, 10, 10])},          # 三路全中
        {"id": 3, "path": None, "tags": ["stockings"],
         "phash": "000000000000000f",
         "color": ([[200, 200, 200]] * 9, [200, 200, 200])},   # 只中一半
        {"id": 4, "path": None, "tags": ["unrelated"],
         "phash": "ffffffffffffffff",
         "color": ([[255, 255, 255]] * 9, [255, 255, 255])},   # 全不中
    ]
    out = ss.rank_similar(seed, cands)
    assert [c["id"] for c in out] == [2, 3, 4]
    assert out[0]["score"] > out[1]["score"] > out[2]["score"]
    # 种子自己不能出现在结果里
    assert all(c["id"] != 1 for c in out)
    # 每个结果都要带三路分量，界面要显示
    for c in out:
        assert {"w_word", "w_color", "w_hash"} <= set(c)


# ----------------------------- 菜单接线 -----------------------------

def test_ssim_entry_replaced_in_menus():
    """SSIM 入口必须从两处菜单消失，新入口接上。"""
    import io
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "media_resource_manager" / "gui" / "main_window.py"
    txt = src.read_text(encoding="utf-8")
    # 菜单行（不是方法定义、不是注释）里不能再有 SSIM
    assert 'addAction("查找相似(SSIM)"' not in txt
    assert txt.count('addAction("查找类似图片…"') >= 2, "顶栏+右键两处都要接上"
    assert "def gallery_find_similar_images" in txt
    assert "def find_similar" in txt, "find_similar 方法本体保留（只是菜单不挂了）"


# ----------------------------- 结果窗 -----------------------------

def test_result_dialog_builds_and_reorders():
    """结果窗能建起来、标签中文、点结果换种子会重排。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.simsearch_dialog import SimilarImagesDialog

    # 生产路径下每个结果都带 color（rank_similar 用 **c 透传），
    # 这里照样带上，测的才是真实行为。
    dark = ([[10, 10, 10]] * 9, [10, 10, 10])
    light = ([[240, 240, 240]] * 9, [240, 240, 240])
    seed = {"id": 1, "path": None, "tags": ["white stockings"],
            "phash": "0" * 16, "score": 1.0, "color": dark,
            "w_word": 1.0, "w_color": 1.0, "w_hash": 1.0}
    hits = [
        {"id": 2, "path": None, "tags": ["white pantyhose"],
         "phash": "0" * 16, "score": 0.9, "color": dark,
         "w_word": 0.5, "w_color": 0.9, "w_hash": 1.0},
        {"id": 3, "path": None, "tags": ["stockings"],
         "phash": "f" * 16, "score": 0.3, "color": light,
         "w_word": 0.5, "w_color": 0.2, "w_hash": 0.1},
    ]
    dlg = SimilarImagesDialog(seed, hits, None)
    try:
        assert dlg.hits[0]["id"] == 2
        # 点 id=3 当新种子：分数是「相对新种子」重算的，顺序按新分数排
        dlg._on_pick(dlg.hits[1])
        assert dlg.seed["id"] == 3
        # 新种子成了种子，就不该出现在自己的推荐列表里（rank_similar 会跳过自身）
        assert [h["id"] for h in dlg.hits] == [2]
        assert all("w_word" in h for h in dlg.hits)
    finally:
        dlg.close()


def test_dialog_tags_render_in_chinese():
    """结果卡片上的标签必须中文，原始英文只留在 tooltip 里。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.simsearch_dialog import _tags_cn

    txt = _tags_cn('["white stockings", "pantyhose", "cute"]')
    assert "丝" in txt and "连" in txt, txt
    assert "stockings" not in txt, f"界面上不该出现英文原词：{txt}"
    assert _tags_cn("") == ""
    assert _tags_cn(None) == ""


def test_row_of_id_lookup():
    """需求 30 双击回图库要靠它把 id 翻成行号。"""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

    app = QApplication.instance() or QApplication([])
    from media_resource_manager.gui.table_view import row_of_id

    tbl = QTableWidget(3, 1)
    for r, iid in enumerate((7, 8, 9)):
        it = QTableWidgetItem(str(iid))
        it.setData(Qt.ItemDataRole.UserRole, iid)
        tbl.setItem(r, 0, it)
    assert row_of_id(tbl, 8) == 1
    assert row_of_id(tbl, 7) == 0
    assert row_of_id(tbl, 999) is None
