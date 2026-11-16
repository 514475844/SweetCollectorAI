"""R154-F17 图库标签批量管理护栏（2026-10-22 R215 棒）。

订单原文（R154_FEATURES_ALL.md §F17）：
  · 多选图 → 批量加/删标签（写 images.tags，去重不覆盖已有）；
  · 图库筛选栏加标签筛选（点击标签即筛）；与现有 tag: 搜索语法互通。

本文件分三层钉契约：

  L1 纯函数（node 之外不需要 GUI）：
    - _read_tags 保留原始大小写（写回用途，不给 split_tags 的小写口径）
    - _merge_tags 去重不覆盖、保序、老库逗号格式归一
    - _strip_tags 只给确实少标签的行返回、保序、空删除返回空

  L2 源码/AST（防"功能被顺手删"）：
    - 右键菜单「删标签…」接着「加标签…」（两者对称，缺一不可）
    - 图库工具栏建了 g_tag_filter 且首项是「全部标签」
    - load_gallery 用 g_tag_filter 的选中值拼 ` tag:xxx`（互通不双轨）

  L3 真 MainWindow 行为（离屏真起窗口 + 真库）：
    - 选标签 → 图库只剩带该标签的图（点标签即筛）
    - 批量加标签不覆盖已有；加完新标签立刻进筛选下拉
    - 批量删标签：勾选清单=选中图标签并集，只删勾中的
    - 标签筛选与「只看收藏」叠加（AND）

变异自检（R73 纪律：判据必须变异确认会红）见
`.workbuddy/_r215_mut_f17.py`：
  - _strip_tags 恒返回 []        → L1 删标签断言红
  - _merge_tags 改成覆盖写        → L1 加标签断言红
  - load_gallery 忽略 tagf        → L3 筛选断言红
  - 删菜单行                      → L2 菜单断言红
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from media_resource_manager.db import Database
from media_resource_manager.gui import main_window as mw_mod

MW_SRC = (os.path.join(_ROOT, "media_resource_manager", "gui",
                       "main_window.py")).replace("\\", "/")
MW_SRC = open(MW_SRC, encoding="utf-8").read()


# ═══════════════════════════ 工具 ═══════════════════════════

def _code_only(blk: str) -> str:
    """去掉注释行再断言 —— 修复说明里常含被修掉的关键字，不剥注释会
    「自己写的文档自己红」（本仓实测踩过三次，R69/R73/R84）。"""
    return "\n".join(l for l in blk.splitlines()
                     if not l.strip().startswith("#"))


def _block(src: str, name: str) -> str:
    """取类中某个方法的源码块（到下一个同级 def 为止）。"""
    i = src.index(f"def {name}(")
    j = src.find("\n    def ", i + 1)
    return src[i:j if j > 0 else len(src)]


def _mk_image(db, task_id, url, tags, path="/tmp/noexist.png",
              favorite=0):
    cur = db.execute(
        "INSERT INTO images (task_id, source_url, path, tags, favorite) "
        "VALUES (?,?,?,?,?)",
        (task_id, url, path, tags, favorite))
    return int(cur.lastrowid)


# ═══════════════════════ L1 · 纯函数 ═══════════════════════

def test_read_tags_keeps_original_case():
    """写回用途必须保大小写：用户手敲的 'White Stockings' 不能被打成
    小写再落库（界面显示会变）。split_tags 的小写口径只归学习/相似度。"""
    got = mw_mod.MainWindow._read_tags('["White Stockings", "cute"]')
    assert got == ["White Stockings", "cute"], got


def test_read_tags_accepts_legacy_comma_format():
    """老库 tags 可能是逗号分隔裸串（split_tags 同款兼容），不能炸。"""
    assert mw_mod.MainWindow._read_tags("白丝,黑丝") == ["白丝", "黑丝"]
    assert mw_mod.MainWindow._read_tags("") == []
    assert mw_mod.MainWindow._read_tags(None) == []
    assert mw_mod.MainWindow._read_tags(["a", "b"]) == ["a", "b"]


def test_merge_tags_dedup_and_keep_order():
    """去重不覆盖：已有该标签的行不返回；没加的保序追加。"""
    rows = [
        {"id": 1, "tags": '["a", "b"]'},     # 已有 a → 跳过
        {"id": 2, "tags": '["b"]'},          # 追加
        {"id": 3, "tags": None},             # 空 → 只留新标签
    ]
    got = mw_mod.MainWindow._merge_tags(rows, "a")
    assert got == [(2, '["b", "a"]'), (3, '["a"]')], got


def test_merge_tags_empty_tag_returns_nothing():
    assert mw_mod.MainWindow._merge_tags(
        [{"id": 1, "tags": '["a"]'}], "   ") == []


def test_strip_tags_only_returns_affected_rows():
    """只给确实少了一个标签的行返回 —— 没命中的行不写库。"""
    rows = [
        {"id": 1, "tags": '["a", "b"]'},     # 删 a → 留 b
        {"id": 2, "tags": '["x"]'},          # 没 a → 不返回
        {"id": 3, "tags": "a,b"},            # 老格式归一 + 删 a
        {"id": 4, "tags": None},             # 空 → 不返回
    ]
    got = mw_mod.MainWindow._strip_tags(rows, ["a"])
    assert got == [(1, '["b"]'), (3, '["b"]')], got


def test_strip_tags_keeps_case_and_order_of_survivors():
    rows = [{"id": 1, "tags": '["White Stockings", "cute", "b"]'}]
    got = mw_mod.MainWindow._strip_tags(rows, ["CUTE"])  # 大小写不敏感匹配由 strip 后精确比
    assert got == [], "大小写不同的词不该被误删（精确匹配）"
    got = mw_mod.MainWindow._strip_tags(rows, ["cute"])
    assert got == [(1, '["White Stockings", "b"]')], got


def test_strip_tags_empty_remove_returns_nothing():
    assert mw_mod.MainWindow._strip_tags(
        [{"id": 1, "tags": '["a"]'}], []) == []
    assert mw_mod.MainWindow._strip_tags(
        [{"id": 1, "tags": '["a"]'}], ["  "]) == []


# ═══════════════════════ L2 · 源码/AST ═══════════════════════

def test_menu_has_remove_tag_next_to_add_tag():
    """右键菜单「删标签…」必须紧接「加标签…」（F17 要求加/删对称）。"""
    src = _code_only(MW_SRC)
    i = src.index('menu.addAction("加标签…", self.gallery_add_tag)')
    j = src.index('menu.addAction("编辑标签/评分…"', i)
    seg = src[i:j]
    assert 'menu.addAction("删标签…", self.gallery_remove_tag)' in seg, (
        "「加标签…」与「编辑标签/评分…」之间没有「删标签…」")


def test_gallery_toolbar_builds_tag_filter_combo():
    """图库筛选栏要有标签筛选下拉：建控件 + 挂进筛选行，一个不能少。"""
    src = _code_only(MW_SRC)
    assert "self.g_tag_filter = QComboBox()" in src, "没建标签筛选下拉"
    i = src.index("self.g_tag_filter = QComboBox()")
    assert "rh.addWidget(self.g_tag_filter)" in src[i:], (
        "标签下拉没挂进图库筛选行（建了不挂=看不见）")
    # 首项「全部标签」在懒铺函数里加（data=None = 不筛）
    fill = _code_only(_block(MW_SRC, "_fill_gallery_tag_combo"))
    assert 'combo.addItem("全部标签", None)' in fill, (
        "标签下拉首项不是「全部标签」（data=None）")


def test_load_gallery_appends_tag_token_to_query():
    """选中标签 = 往查询串拼 ` tag:xxx` —— 与手敲 tag: 语法互通（不双轨）。"""
    blk = _code_only(_block(MW_SRC, "load_gallery"))
    assert "self.g_tag_filter.currentData()" in blk, "load_gallery 没读标签下拉"
    assert 'f"{q} tag:{tagf}"' in blk, "选中标签没拼成 tag: token"


def test_refresh_button_repopulates_tag_combo():
    """刷新必须置脏重铺标签下拉（加/删标签后新标签要立刻选得到）。"""
    blk = _code_only(_block(MW_SRC, "gallery_refresh"))
    assert "_gal_tag_combo_filled = False" in blk
    assert "self.load_gallery()" in blk


# ═══════════════════════ L3 · 真 MainWindow 行为 ═══════════════════════

def _boot(monkeypatch, tmp_path):
    """起一个离屏 MainWindow（照 tests/test_r36_copy_and_refresh.py 套路）。"""
    home = os.path.join(str(tmp_path), "mrm_home")
    os.makedirs(home, exist_ok=True)
    monkeypatch.setenv("MRM_HOME", home)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("MRM_NO_MIGRATE", "1")
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])
    mw_mod._ensure_single_instance = lambda a: True
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QDialog.exec = lambda self: QDialog.DialogCode.Accepted
    from media_resource_manager.gui import theme
    theme.apply_theme(app, False)
    win = mw_mod.MainWindow()
    win.show()
    app.processEvents()
    return app, win


@pytest.fixture()
def win_env(tmp_path, monkeypatch):
    app, win = _boot(monkeypatch, tmp_path)
    yield win
    try:
        win.close()
    except Exception:
        pass


def _seed(win, monkeypatch):
    """灌 3 张图：id1=[白丝,风景] id2=[风景] id3=[白丝]（另建一个任务）。"""
    tid = int(win.db.execute(
        "INSERT INTO tasks (name, entry_urls, output_dir, status) "
        "VALUES (?,?,?,?)",
        ("t1", json.dumps(["https://x.com/1"]), str(win.cfg.library_dir),
         "idle")).lastrowid)
    a = _mk_image(win.db, tid, "https://x.com/a.jpg", '["白丝", "风景"]')
    b = _mk_image(win.db, tid, "https://x.com/b.jpg", '["风景"]')
    c = _mk_image(win.db, tid, "https://x.com/c.jpg", '["白丝"]')
    return tid, a, b, c


def test_tag_filter_shows_only_images_with_that_tag(win_env):
    """★ 点击标签即筛：选「白丝」→ 只剩带白丝的图（含老格式归一后的）。"""
    tid, a, b, c = _seed(win_env, None)
    win = win_env
    win.load_gallery()                       # 触发懒铺标签下拉
    combo = win.g_tag_filter
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels[0] == "全部标签", labels
    assert any(t.startswith("白丝") for t in labels), f"下拉里没有白丝：{labels}"
    # 选中「白丝」
    idx = next(i for i in range(combo.count())
               if combo.itemData(i) == "白丝")
    combo.setCurrentIndex(idx)
    win.load_gallery()
    got = sorted(r["id"] for r in win._gal_rows)
    assert got == sorted([a, c]), f"标签筛选没生效：{got}"
    # 切回「全部标签」→ 三张都回来
    combo.setCurrentIndex(0)
    win.load_gallery()
    got = sorted(r["id"] for r in win._gal_rows)
    assert got == sorted([a, b, c]), f"取消筛选没还原：{got}"


def test_tag_filter_stacks_with_fav_filter(win_env):
    """标签筛选与「只看收藏」叠加是 AND（结构化分支统一处理）。"""
    tid, a, b, c = _seed(win_env, None)
    win = win_env
    win.db.execute("UPDATE images SET favorite=1 WHERE id=?", (c,))
    win.load_gallery()
    combo = win.g_tag_filter
    idx = next(i for i in range(combo.count())
               if combo.itemData(i) == "白丝")
    combo.setCurrentIndex(idx)
    win.g_filter.setCurrentIndex(1)           # 「只看收藏」
    win.load_gallery()
    got = sorted(r["id"] for r in win._gal_rows)
    assert got == [c], f"标签×收藏叠加错：{got}"


def test_add_tag_appends_without_overwrite(win_env, monkeypatch):
    """批量加标签：给 id2 加「白丝」不动它已有的「风景」；加完下拉立刻有新标签。"""
    tid, a, b, c = _seed(win_env, None)
    win = win_env
    from PySide6.QtWidgets import QInputDialog
    monkeypatch.setattr(QInputDialog, "getText",
                        lambda *a, **k: ("白丝", True))
    win._gallery_selected_ids = lambda: [b]
    win.gallery_add_tag()
    row = win.db.query("SELECT tags FROM images WHERE id=?", (b,))[0]
    assert json.loads(row["tags"]) == ["风景", "白丝"], row["tags"]
    # 新标签要立刻进筛选下拉（加标签后置脏重铺）
    win.load_gallery()
    datas = [win.g_tag_filter.itemData(i)
             for i in range(win.g_tag_filter.count())]
    assert "白丝" in datas, f"加完标签下拉里没有它：{datas}"


def test_remove_tag_dialog_removes_only_checked(win_env, monkeypatch):
    """★ 批量删标签：勾选清单=选中图标签并集；只删勾中的，没勾的保留。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QDialog, QListWidget
    tid, a, b, c = _seed(win_env, None)
    win = win_env
    win._gallery_selected_ids = lambda: [a, b]   # 并集 = 白丝/风景

    def _accept_all(self):
        lst = self.findChildren(QListWidget)[0]
        assert lst.count() == 2, f"勾选清单不是并集：{lst.count()}"
        # 只勾「风景」
        for i in range(lst.count()):
            it = lst.item(i)
            it.setCheckState(Qt.CheckState.Checked if it.text() == "风景"
                             else Qt.CheckState.Unchecked)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", _accept_all)
    win.gallery_remove_tag()
    ra = json.loads(win.db.query(
        "SELECT tags FROM images WHERE id=?", (a,))[0]["tags"])
    rb = json.loads(win.db.query(
        "SELECT tags FROM images WHERE id=?", (b,))[0]["tags"])
    assert ra == ["白丝"], f"id1 只该删风景：{ra}"
    assert rb == [], f"id2 只剩风景删完应为空：{rb}"
    # 删完标签从下拉消失（置脏重铺）
    win.load_gallery()
    datas = [win.g_tag_filter.itemData(i)
             for i in range(win.g_tag_filter.count())]
    assert "风景" not in datas, f"删完的标签还在下拉里：{datas}"
    assert "白丝" in datas, "没删的白丝不该从下拉消失"
