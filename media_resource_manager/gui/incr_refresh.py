"""任务列表的**增量刷新**基元（L2）。

作业单：`.workbuddy/LEGACY_DEBT_ORDER.md` L2
「刷新时整体重建 model/表格（refresh() 全量重建），库大时主线程卡死、界面无响应。
改增量刷新——只 diff 变化行（新增/状态变更/删除）做 beginInsertRows/removeRows/
dataChanged，不整体 clear+refill。」

⚠ **为什么是 widget 级 diff 而不是 beginInsertRows**
本项目**没有任何 Qt Model/View 架构**：`QAbstractTableModel`/`QStandardItemModel`
全项目 0 处，列表一律是 `QListWidget`+`setItemWidget`。`beginInsertRows` 那套
只对 model/view 有效，硬套不上；真要上就得把 `QListWidget` 换成 `QListView`
并把 `TaskRow` 改成 delegate —— 那是重写整个任务列表 UI，不在「消除卡顿」
的范围内，且风险远大于收益（PySide6 那边已经踩过 setItemWidget 悬空指针的坑）。

所以这里走**同构的 widget 级 diff**：语义与 model 版一一对应
（新增 → insertItem、删除 → takeItem、状态变更 → 只换那一个行控件），
效果一样是「不整体 clear+refill」，但不动控件类型。

**diff 的键是任务 id**（UserRole），不是行号 —— 行号会随增删整体平移，
按行号比会把「插在第 3 行」误判成「第 3~N 行全变了」。
"""
from __future__ import annotations

from typing import Callable


class ListDiff:
    """一次 diff 的结果，供测试与调用方核对。

    刻意**只写不删**（作业单铁律）：这里不碰文件系统，纯内存数据结构。
    """

    __slots__ = ("kept", "updated", "added", "removed", "order_changed")

    def __init__(self, kept, updated, added, removed, order_changed):
        self.kept = kept            # 没动的（保留原行控件）
        self.updated = updated      # id 变了需要换行控件的
        self.added = added          # 新增的 id
        self.removed = removed      # 消失的 id
        self.order_changed = order_changed

    @property
    def touched(self) -> int:
        """真正需要动控件的行数 —— 增量刷新的核心指标，应远小于总数。"""
        return len(self.updated) + len(self.added) + len(self.removed)

    def __repr__(self):
        return (f"ListDiff(kept={len(self.kept)}, updated={len(self.updated)}, "
                f"added={len(self.added)}, removed={len(self.removed)}, "
                f"reordered={self.order_changed})")


def diff_by_id(old_ids: list[int], new_ids: list[int],
               changed: Callable[[int], bool] | None = None) -> ListDiff:
    """按 id diff 两份列表（**保序**），返回增/删/改。

    ``changed(id)`` 返回 True 表示该 id 对应的内容变了（状态/进度变了），
    需要换掉行控件。为 None 时只看增删。

    ⚠ **顺序也要单独报出来**：列表顺序变了（按分数排、按时间排）时，
    即使 id 集合一模一样，也得重排 —— 否则界面顺序会停在旧排序上。
    只报 ``order_changed`` 不逐个报，因为重排要么整体做要么不做。
    """
    old_list = list(old_ids or [])
    new_list = list(new_ids or [])
    old_set, new_set = set(old_list), set(new_list)

    added = [i for i in new_list if i not in old_set]
    removed = [i for i in old_list if i not in new_set]
    common = [i for i in new_list if i in old_set]
    if changed is None:
        updated = []
    else:
        updated = [i for i in common if changed(i)]

    # 顺序变了 = 相同 id 的相对次序变了。判据不能是 `old_list != new_list`
    #（那会把「纯新增」也算成重排），要比「公共 id 构成的序列」。
    old_common = [i for i in old_list if i in new_set]
    order_changed = (old_common != common)
    return ListDiff(kept=common, updated=updated, added=added,
                    removed=removed, order_changed=order_changed)


def plan_incremental(old_ids: list[int], new_ids: list[int],
                     changed: Callable[[int], bool] | None = None,
                     max_touch_ratio: float = 0.5):
    """在 diff 与「全量重建」之间选一个，返回 ``(用增量, ListDiff)``。

    为什么要这个开关：diff 本身也有成本（两次建集合+ 逐项比较），
    而当**大部分行都变了**时，全量重建反而更快。所以按「改动比例」分界。

    ⚠ ``max_touch_ratio`` 刻意可调而不是写死：比例阈值不是常量，
    真实取值要靠实测（改动少时 diff 赢、大面积变动时重建赢）。
    宁可调错成「多重建几次」也不要调错成「该重建时硬 diff」——
    后者的症状是界面卡住不动，比慢更难查。
    """
    d = diff_by_id(old_ids, new_ids, changed)
    total = max(1, len(new_ids))
    if (d.touched + (len(new_ids) if d.order_changed else 0)) > \
            max_touch_ratio * total:
        return False, d
    return True, d