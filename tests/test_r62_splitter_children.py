"""R62：`_vsplit` 三个孩子的 stretch/sizes 必须跟着孩子数走。

—— 一个 R61 漏掉、被 R62 到岗时发现的**真产品 bug**（不是测试问题）。

## 现象
R61 给 `self._vsplit` 加了第 3 个孩子 `task_table`（§2 的 qB 式表格），
但 `setStretchFactor` / `setSizes` 还是**按 2 个孩子**写的：

    setStretchFactor(0, 1)      # 当时 0=task_list, 1=bottom
    setStretchFactor(1, 0)      # bottom 不拉伸
    setSizes([400, 300])

`QSplitter.setStretchFactor/setSizes` 是**按下标**定位的。加了一个孩子之后，
**下标 1 从 bottom 变成了 task_table** ⇒ `stretch(1)=0` 落到了表格上，
而 bottom 拿默认的 0 ⇒ **三者 stretch 全是 0**
⇒ Qt 把**全部**高度分配给了当时可见的那一个
⇒ **底部「日志统计」面板被挤成 `height=0`（彻底消失）**，
   任务列表/表格占满整页。

## 为什么这条护栏值得写死
「加孩子忘了改 stretch」是一类**静默**回归：不抛异常、界面不崩，
只是某个面板悄悄不见了；而且它**只在特定预设下可见**
（经典预设走表格，另两个预设走卡片 ⇒ 用户可能几个月都碰不到）。
更糟的是它**极难复现**：面板在代码里明明被 `addWidget` 加进去了，
`isVisible()` 还返回 True（隐藏 ≠ 高度为 0）——
只有量 `height()` 才看得见（本文件用的就是 `height()`）。

## 本文件钉的判据（全部是**关系**，不是写死的数值 —— MEMORY 铁律）
1. bottom 面板高度 **> 0**（不论当前是卡片还是表格布局）
2. 主区高度 **>** bottom 高度（主区才是主角，不能被次要面板压过去）
3. 隐藏的那套任务视图高度 **== 0**（不得白占位，把主区挤掉）
4. **两套布局都要成立**（cards / table 各测一遍）
   —— 只测一种布局，就是让另一种布局可以悄悄坏掉

## 反向验证
`tools/r62_reverse_splitter.py` 把本文件对应的产品码还原成 R61 出错形态，
实测：bottom 从 300 掉到 **0** ⇒ 本护栏**抓得到**这个 bug。
（R62 实测：还原形态下探针 2 项转红，还原后 md5 校验一致。）
"""
import pytest

pytest.importorskip("PySide6")

BOT = "bottom_panel"


@pytest.fixture(scope="module")
def _boot():
    """建一个真 MainWindow，并把 main_window 的单实例锁桩掉。

    ⚠ 必须 import 顺序与仓库其它 Qt 用例一致（先 `main_window` 再 `theme`），
    否则 import 期就会走单实例检查弹框。
    """
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    import media_resource_manager.gui.main_window as mw

    mw._ensure_single_instance = lambda *a, **k: True
    from media_resource_manager.gui import theme

    theme.apply_theme(app, False)
    return app, mw


def _win(app, mw, want_table):
    w = mw.MainWindow()
    w.resize(1382, 899)
    w.show()
    for _ in range(6):
        app.processEvents()
    # V0.0.9：开机默认页改成学习看板（stack 索引 0），任务页（1）藏着时
    # 其内部 splitter 不参与真实高度分配（量出来全是初始 86px 假值）。
    # 本文件量的就是任务页的分屏比例 ⇒ 必须切到任务页再量（用户也是
    # 切过来才看到这些高度）。改的是测试前提，不是产品。
    w.stack.setCurrentIndex(w.stack.indexOf(w._task_page))
    for _ in range(6):
        app.processEvents()
    # 与产品同一件事：产品是在换肤函数里 inline 切显隐的（没有 _set_task_view），
    # 这里复刻的正是那段代码做的**同一件事**（显隐 + 补刷），不引入产品没有的路径。
    w.task_table.setVisible(want_table)
    w.task_list.setVisible(not want_table)
    for _ in range(8):
        app.processEvents()
    return w


@pytest.mark.parametrize("want_table,tag", [(True, "table"), (False, "cards")])
def test_bottom_panel_is_not_squeezed_to_zero(_boot, want_table, tag):
    """底部「日志统计」必须有真实高度 —— 症状是「面板不见了」。"""
    app, mw = _boot
    w = _win(app, mw, want_table)
    try:
        bottom = getattr(w, f"_{BOT}", None)
        assert bottom is not None, "找不到 _bottom_panel（属性名变了？）"
        assert bottom.height() > 0, (
            f"[{tag}] bottom(日志统计) 高度被挤成 0 ⇒ 面板不可见。"
            "根因通常是给 QSplitter 加了孩子却没同步 setStretchFactor/setSizes "
            "（它们是按**下标**定位的，加一个孩子就整体错位）。")
    finally:
        w.close()
        for _ in range(3):
            app.processEvents()


@pytest.mark.parametrize("want_table,tag", [(True, "table"), (False, "cards")])
def test_main_area_outweighs_bottom(_boot, want_table, tag):
    """主区（任务区）必须比 bottom 高 —— 断言关系，不断言具体数值。"""
    app, mw = _boot
    w = _win(app, mw, want_table)
    try:
        main = w.task_table if want_table else w.task_list
        bottom = getattr(w, f"_{BOT}")
        assert main.height() > bottom.height(), (
            f"[{tag}] 主区 {main.height()} 未高于 bottom {bottom.height()}"
            " ⇒ 布局权重写错了（stretch 全为 0 时 Qt 会把高度全给可见的那个）。")
    finally:
        w.close()
        for _ in range(3):
            app.processEvents()


@pytest.mark.parametrize("want_table,tag", [(True, "table"), (False, "cards")])
def test_hidden_task_view_occupies_nothing(_boot, want_table, tag):
    """隐藏的那套不得白占高度。

    有了两套并存后，「隐藏」不等于「不占位」：若 splitter 仍给它留高度，
    主区就会被压小 —— 这是 §2 特有的新坑，卡片时代不存在。
    """
    app, mw = _boot
    w = _win(app, mw, want_table)
    try:
        other = w.task_list if want_table else w.task_table
        assert other.isHidden(), f"[{tag}] 对照组前提不成立：另一套应当是隐藏的"
        assert other.height() == 0, (
            f"[{tag}] 隐藏的那套仍占 {other.height()}px 高度 ⇒ 白占位，"
            "主区被压小（用户看到的是任务区比预期矮一截）。")
    finally:
        w.close()
        for _ in range(3):
            app.processEvents()


def test_splitter_stretch_factors_follow_child_count(_boot):
    """结构性钉死：stretch 的下标必须覆盖**每一个**孩子。

    这条是前三条的**根因判据**：只要有一个孩子没被显式给过 stretch，
    Qt 就按默认 0 处理，全局塌成「不拉伸」。
    用「读孩子实际高度」验证，断言关系（main > bottom），
    而不是写死 `setStretchFactor(2, 0)` 这种形状 ——
    形状会变，关系不会。
    """
    app, mw = _boot
    w = _win(app, mw, True)
    try:
        sp = w._vsplit
        kids = [sp.widget(i) for i in range(sp.count())]
        assert len(kids) == 3, (
            f"_vsplit 现在有 {len(kids)} 个孩子（预期 3：task_list/task_table/"
            f"bottom）。**孩子数变了就必须同步复核 setStretchFactor/setSizes**，"
            "它们按下标定位。")
        heights = [k.height() for k in kids]
        vis = [not k.isHidden() for k in kids]
        # ⚠ 隐藏的那套**本来就该**是 0（这是 §2 的设计：同一时刻只显示一套，
        #   另一套不占位）。我第一版写成 `all(h > 0)`，结果被自己的护栏打脸 ——
        #   它与上面 test_hidden_task_view_occupies_nothing 直接矛盾。
        #   ⇒ 判据必须是「**可见的孩子都有高度**」，不是「所有孩子都有高度」。
        assert all(h > 0 for h, v in zip(heights, vis) if v), (
            f"可见孩子里有高度为 0 的：heights={heights} visible={vis}"
            " ⇒ 至少有一个 stretch 没配对。")
        assert sum(1 for h, v in zip(heights, vis) if v and h == 0) == 0, (
            f"可见孩子高度为 0：heights={heights} visible={vis}")
        visible = [k for k in kids if not k.isHidden()]
        assert len(visible) == 2, (
            f"同时可见的孩子数 = {len(visible)}（预期 2：一张任务表 + bottom）")
        # 关系判据：可见的主区必须高于 bottom（这是「权重配对对了」的真正体现）
        main = w.task_table if vis[1] else w.task_list
        bottom = kids[2]
        assert main.height() > bottom.height(), (
            f"主区 {main.height()} 未高于 bottom {bottom.height()}：heights={heights}")
    finally:
        w.close()
        for _ in range(3):
            app.processEvents()
