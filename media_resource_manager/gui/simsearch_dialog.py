"""「查找类似图片」的结果窗口（需求 30）。

左边是种子图，右边是按评分降序的推荐网格。每张卡片显示：
  · 缩略图（方形裁切，够看清构图）
  · 总评分 + 三路分量（主题词 / 色调 / 结构），让人知道「为什么像」
  · 标签（走 i18n，中文显示；库里存的是英文，不改数据）

点任意结果 → 换种子（即时重排），双击 → 在图库里定位到那张图。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QWidget, QGridLayout, QFrame, QApplication,
)

from ..i18n import translate_tags
from .theme import style as _tstyle      # P1-4：行内样式也要跟随皮肤

THUMB = 132
MAX_COLS = 5


def _tags_cn(tags) -> str:
    """库里的英文标签 → 中文显示。

    必须走 simsearch.split_tags（它会先剥 JSON 数组），别在这里自己再切一遍：
    tags 实际存的是 ``["white stockings", "cute"]``，直接 split 会把整串当成
    一个词，界面就会显示方括号和引号，而且 translate_tags 完全不生效。
    """
    from ..simsearch import split_tags
    words = split_tags(tags)
    if not words:
        return ""
    return " · ".join(translate_tags(words))


def _thumb(path, size=THUMB) -> QPixmap:
    pm = QPixmap()
    p = Path(path) if path else None
    if p and p.exists():
        pm.load(str(p))
    if pm.isNull():
        return pm
    # 方形缩略图：先缩再中心裁，绝不拉伸变形
    w, h = pm.width(), pm.height()
    if w <= 0 or h <= 0:
        return pm
    side = min(w, h)
    return pm.copy((w - side) // 2, (h - side) // 2, side, side)\
             .scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)


class _Card(QFrame):
    """一张推荐图：缩略图 + 评分 + 三路分量 + 中文标签。"""

    def __init__(self, item, on_pick, on_open, parent=None):
        super().__init__(parent)
        self.item = item
        self.setFrameShape(QFrame.Shape.StyledPanel)
        # P1-4：卡片底/描边/hover 态原先写死（#fff/#e2e6ef/#5b3fd6/#f6f3ff），
        # 其中 #5b3fd6 是 violet 皮肤专属 → 换皮肤后 hover 边框明显不属于
        # 当前主题。改走 _tstyle 模板，跟随皮肤与深浅。
        _tstyle(self,
                "QFrame{background:{surface};border:1px solid {border};"
                "border-radius:8px;}"
                "QFrame:hover{border:2px solid {accent};"
                "background:{row_hover};}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        self.img = QLabel()
        self.img.setFixedSize(THUMB, THUMB)
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(self.img, "background:{surface_alt};border-radius:6px;")
        self.img.setPixmap(_thumb(item.get("path")))
        self.img.setToolTip(str(item.get("path") or ""))
        lay.addWidget(self.img, 0, Qt.AlignmentFlag.AlignHCenter)

        total = item.get("score", 0.0)
        head = QLabel(f"<b>{total:.2f}</b>")
        _tstyle(head, "color:{accent_deep};font-size:13px;")
        lay.addWidget(head, 0, Qt.AlignmentFlag.AlignHCenter)

        # 三路分量条：一眼看出是「词像」还是「色像」还是「构图像」
        bars = QWidget()
        bl = QHBoxLayout(bars)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(3)
        for key, tip in (("w_word", "主题词"), ("w_color", "色调"),
                         ("w_hash", "结构")):
            v = float(item.get(key, 0.0) or 0.0)
            col = QLabel(f"{tip} {v:.2f}")
            _tstyle(col,
                    "color:{text_dim};font-size:11px;"
                    "background:{surface_alt};border-radius:4px;padding:1px 4px;}")
            col.setToolTip(f"{tip}得分 {v:.2f}")
            bl.addWidget(col)
        lay.addWidget(bars)

        tg = QLabel(_tags_cn(item.get("tags")) or "（无标签）")
        tg.setWordWrap(True)
        _tstyle(tg, "color:{text};font-size:11px;")
        tg.setToolTip("库里存的原始标签：" + str(item.get("tags") or ""))
        lay.addWidget(tg)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(
            f"总评分 {total:.3f}\\n"
            f"· 主题词 {item.get('w_word', 0):.2f}（画面主体是否同类）\\n"
            f"· 色调 {item.get('w_color', 0):.2f}（整图/九宫格主色接近度）\\n"
            f"· 结构 {item.get('w_hash', 0):.2f}（pHash 汉明距离）\\n\\n"
            "单击：换这张当种子重排\\n双击：在图库中定位")
        self._on_pick = on_pick
        self._on_open = on_open

    def mousePressEvent(self, e):
        # 单击换种子：这是这个功能最核心的交互（demo 里验证过）
        self._on_pick(self.item)
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        self._on_open(self.item)
        super().mouseDoubleClickEvent(e)


class SimilarImagesDialog(QDialog):
    """查找类似图片的结果窗。``parent`` 是 MainWindow（用来在图库里定位）。"""

    def __init__(self, seed, hits, parent=None):
        super().__init__(parent)
        self.seed = seed
        self.hits = list(hits)
        self.parent_win = parent
        self.setWindowTitle("查找类似图片")
        self.resize(980, 720)

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # ---- 顶部：种子图 + 它的标签 ----
        top = QHBoxLayout()
        sl = QLabel()
        sl.setFixedSize(96, 96)
        sl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _tstyle(sl, "background:{surface_alt};border-radius:8px;")
        sl.setPixmap(_thumb(seed.get("path"), 96))
        top.addWidget(sl)
        info = QVBoxLayout()
        t1 = QLabel("<b>以这张图为种子</b>")
        _tstyle(t1, "font-size:14px;color:{text};")
        info.addWidget(t1)
        t2 = QLabel(_tags_cn(seed.get("tags")) or "（这张没有 AI 标签，"
                                               "推荐会偏色调/结构）")
        t2.setWordWrap(True)
        _tstyle(t2, "color:{text_dim};font-size:12px;")
        info.addWidget(t2)
        t3 = QLabel("推荐 %d 张 · 单击任意结果换种子重排 · 双击在图库中定位"
                    % len(self.hits))
        _tstyle(t3, "color:{text_dim};font-size:11px;")
        info.addWidget(t3)
        info.addStretch()
        top.addLayout(info, 1)
        v.addLayout(top)

        # ---- 中部：结果网格（放进滚动区，数量不定） ----
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(self.grid_host)
        v.addWidget(area, 1)

        self._rebuild()

    # ---- 换种子重排（本地已有全部三路分量，直接重排不重新解码） ----
    def _drop_cards(self):
        """把旧卡片**立刻**真正销毁，不留在待删队列里。

        只 deleteLater() 是不够的：那批 C++ 对象会一直挂到解释器退出才回收，
        撞上「Qt 已卸载 / Python 还在 GC」就是 access violation。
        r40 实测：pytest 退出时的 Garbage-collecting 阶段必崩（139）。
        换种子是高频操作，卡片一次几十个，攒着不删迟早出事。
        """
        from PySide6.QtCore import QCoreApplication, QEvent
        while self.grid.count():
            it = self.grid.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        QCoreApplication.sendPostedEvents(None,
                                          QEvent.Type.DeferredDelete)

    def _rebuild(self):
        self._drop_cards()
        for i, item in enumerate(self.hits):
            card = _Card(item, self._on_pick, self._on_open, self.grid_host)
            self.grid.addWidget(card, i // MAX_COLS, i % MAX_COLS)
        QApplication.processEvents()

    def closeEvent(self, e):
        """关窗时同样立刻销毁整棵卡片树，别等 GC。"""
        try:
            self._drop_cards()
        except Exception:
            pass
        super().closeEvent(e)

    def _on_pick(self, item):
        """点结果 → 它变成新种子，**重新算一遍**相似度再排。

        不能只拿旧分数重排：三路分量都是「相对原种子」算出来的，
        换了种子就得重新比一次（demo 里点任意结果即换种子就是这个行为，
        用户验收过）。好在每个结果都带着自己的 tags/phash/特征，
        直接调 rank_similar 即可，不用重新解码图片。
        """
        from ..simsearch import rank_similar
        pool = [h for h in self.hits if h.get("id") != item.get("id")]
        new_seed = dict(item)
        # 只有拿得到视觉特征才能真重算（rank_similar 要 color）。
        # 万一某个结果没带 color（不该发生，重建一次就有），退化成按旧分重排，
        # 绝不能让结果整个清空。
        if new_seed.get("color") is not None and \
                all(h.get("color") is not None for h in pool):
            self.hits = rank_similar(new_seed, pool, limit=len(pool))
        else:
            self.hits = pool + [new_seed]
            self.hits.sort(key=lambda d: d.get("score", 0.0), reverse=True)
        self.seed = new_seed
        self._rebuild()

    def _on_open(self, item):
        """双击 → 回图库并选中那张图。"""
        w = self.parent_win
        if w is None:
            return
        try:
            iid = item.get("id")
            w._select_gallery_by_id(iid)
            self.close()
        except Exception as e:
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.information(self, "定位失败", str(e))
            except Exception:
                pass
