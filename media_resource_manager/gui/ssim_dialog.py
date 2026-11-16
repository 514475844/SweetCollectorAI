"""SSIM near-duplicate finder wired into the gallery (local, undoable)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QListWidget, QListWidgetItem, QHBoxLayout,
    QPushButton, QMessageBox,
)

from ..db import Database
from ..dedup import ssim
from ..dedup.hash import image_dimensions


def _pick_best(group):
    """一组里挑「保留谁」：分辨率/面积最大的那张（同面积时取文件更大的）。

    r39 改动：这以前写在 ``SimilarDialog.__init__`` 里，等于建对话框时又对每组
    每张图解码一遍 —— 150 张图再啃 150 次解码。现在挪到这里，由**后台线程**在
    聚类时顺手一次算完，主线程建对话框时不再解码。
    """
    def area(p):
        try:
            w, h = image_dimensions(p)
        except Exception:
            w = h = 0
        a = (w or 0) * (h or 0)
        try:
            if a:
                a *= max(1, p.stat().st_size)
        except OSError:
            pass
        return a
    return max(group, key=area)


def find_similar_groups(db: Database, limit: int = 150):
    """返回 ``[(group, best), ...]``。

    **注意（r39）**：这个函数是重活（聚类是 O(n²) 的 cv2.imread），**必须在后台
    线程调用**，主线程直接调会把整个界面卡死几十秒（真机扫描实测 35s+）。
    """
    rows = db.query(
        "SELECT path FROM images WHERE path IS NOT NULL AND in_quarantine=0 "
        "ORDER BY id DESC LIMIT ?", (limit,))
    paths = [Path(r["path"]) for r in rows if Path(r["path"]).exists()]
    return [(g, _pick_best(g)) for g in ssim.cluster(paths)]


class SimilarDialog(QDialog):
    """Shows SSIM clusters; the largest file in each group is kept, the rest
    are moved to the quarantine folder (nothing is deleted)."""

    def __init__(self, groups, isolate_dir: Path,
                 on_done, parent=None, db=None):
        """``groups`` 形如 ``[(group, best), ...]`` —— best 已在后台线程算好，
        这里**不再解码任何图片**（r39）。

        R152-A5：新增 ``db`` —— 隔离时必须回写 images 表
        （``in_quarantine=1, needs_review=0, path=新路径``），否则隔离区
        看不见这些图、图库里全变成「磁盘已缺失」。不传 db 时退化为旧行为
        （只搬文件，供无库场景/测试复用）。
        """
        super().__init__(parent)
        self.groups = groups
        self.isolate_dir = isolate_dir
        self.on_done = on_done
        self.db = db
        self.setWindowTitle(f"相似图片（{len(groups)} 组）")
        self.resize(760, 520)
        v = QVBoxLayout(self)
        v.addWidget(QLabel(
            "每组保留分辨率/体积最大的一张，其余移入隔离区（可恢复，不删除）。"))
        self.lst = QListWidget()
        for i, (g, best) in enumerate(groups, 1):
            item = QListWidgetItem(f"组{i}（{len(g)} 张）→ 保留: {best.name}")
            item.setData(Qt.ItemDataRole.UserRole, (g, best))
            self.lst.addItem(item)
        v.addWidget(self.lst)
        row = QHBoxLayout()
        b_ok = QPushButton("按此方案隔离重复项")
        b_ok.clicked.connect(self.apply)
        b_cancel = QPushButton("取消")
        b_cancel.clicked.connect(self.reject)
        row.addWidget(b_ok); row.addWidget(b_cancel)
        v.addLayout(row)

    def apply(self):
        moved = 0
        self.isolate_dir.mkdir(parents=True, exist_ok=True)
        db = getattr(self, "db", None)
        for g, best in (it.data(Qt.ItemDataRole.UserRole)
                        for it in self._items()):
            for p in g:
                if p.resolve() == best.resolve():
                    continue
                # R152-A5：**唯一名**（对齐 main_window 不喜欢路径的
                # ``stem(n)suffix`` 规则）—— 原来直接 ``isolate_dir / p.name``
                # + replace，同名文件会被**静默覆盖**（隔离区里已有的图没了，
                # 图库记录还指着它 = 数据丢失）。
                dest = self.isolate_dir / p.name
                n = 1
                while dest.exists():
                    dest = self.isolate_dir / f"{p.stem}({n}){p.suffix}"
                    n += 1
                old = str(p)
                try:
                    p.replace(dest)
                    moved += 1
                except Exception:
                    continue
                # R152-A5：回写 images 表 —— 隔离区管理看得见、图库不再显
                # 「磁盘已缺失」；needs_review=0 与 R143 四态机口径一致。
                if db is not None:
                    try:
                        db.execute(
                            "UPDATE images SET in_quarantine=1, "
                            "needs_review=0, path=? WHERE path=?",
                            (str(dest), old))
                    except Exception:
                        pass
        QMessageBox.information(self, "完成", f"已移动 {moved} 张到隔离区")
        self.on_done()
        self.accept()

    def _items(self):
        return [self.lst.item(i) for i in range(self.lst.count())]
