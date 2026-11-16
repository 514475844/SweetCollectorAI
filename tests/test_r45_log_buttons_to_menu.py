"""r45（#45）：日志统计页的 5 个按钮全部搬进顶部菜单，页面不再出现按钮。

用户诉求：「日志页别摆按钮」——底栏那页只要看日志，5 个按钮摆在那儿占地方、
还跟底栏打架。做法：清空日志 / 导出日志… / 刷新统计 / 导出报告… / 导入任务…
一个不少地收进顶部菜单「维护 → 日志统计」，页面只留日志正文。

这里钉的不变量（每条都对应一种「搬漏了/ 搬重了 / 搬坏了」）：
1. 页面里**一个 QPushButton 都没有**（真删，不是 setHidden —— 藏在布局里的
   按钮仍然是死代码，而且用户看到的还是「有个按钮在那儿」）。
2. 菜单「维护 → 日志统计」子菜单里 5 个动作都在，标题一字不差。
3. 功能等价：每个动作的槽接的还是原来那个方法（导出日志→export_log、
   导出报告→export_report、导入任务→import_tasks、清空日志→清 log_view）。
4. 「刷新统计」必须带 force=True：refresh_stats 有节流，漏了 force 点菜单
   会被节流挡掉、看不到新数（跟 r39f 的按钮版断言同一条铁律，搬家后继续钉）。
5. 清空日志走方法而不是直连 log_view.clear：菜单比日志页先建，连接那一刻
   self.log_view 还不存在，直连会 AttributeError。
6. 顶级菜单数量/顺序不变（还是 8 个）——#45 是「收进现有菜单」，不是新增顶级
   菜单；顺带守住 verify_r11 的基线。
"""
import inspect

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMenu, QPushButton  # noqa: E402


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(scope="module")
def _cache():
    return {}


def _win():
    from media_resource_manager.gui.main_window import MainWindow
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    try:
        w.hide()
    except Exception:
        pass
    return w


def _log_submenu(w):
    """从菜单树里找出「工具 → 日志统计」子菜单（V0.0.19 父级改名）。"""
    for act in w.menuBar().actions():
        m = act.menu()
        if m is None or act.text() != "工具":
            continue
        for sub in m.actions():
            sm = sub.menu()
            if sm is not None and sub.text() == "日志统计":
                return sm
    return None


# ------------------------------------------------------------------ 1 页面无按钮
def test_log_page_has_no_buttons(qapp):
    w = _win()
    try:
        page = w.findChild(type(w._stats_tab), "logstats")
        assert page is not None, "找不到 objectName=logstats 的日志页"
        btns = page.findChildren(QPushButton)
        assert btns == [], (
            "日志页里还留着按钮："
            + ", ".join(b.text() for b in btns)
            + " —— #45 要求页面只留日志正文，按钮全在「维护 → 日志统计」菜单里")
        # 页面的直接子控件里也不该再有装按钮的容器行
        from PySide6.QtWidgets import QHBoxLayout
        for lay in page.findChildren(QHBoxLayout):
            assert lay.count() == 0, (
                f"日志页里还有个剩 {lay.count()} 个控件的空按钮行布局，"
                "撤按钮时没撤干净")
    finally:
        w.close()


# ------------------------------------------------------------------ 2 菜单可达
def test_five_actions_in_menu(qapp):
    w = _win()
    try:
        sm = _log_submenu(w)
        assert sm is not None, (
            "「维护」菜单下没有「日志统计」子菜单 —— 5 个按钮被撤出页面后就没地儿放了")
        titles = [a.text() for a in sm.actions() if not a.isSeparator()]
        assert titles == ["清空日志", "导出日志…", "刷新统计", "导出报告…", "导入任务…"], (
            f"菜单里的动作与预期不符：{titles}")
    finally:
        w.close()


def test_top_level_menus_unchanged(qapp):
    """#45 不得新增顶级菜单（仍是 8 个，顺序不变）—— verify_r11 的硬基线。"""
    w = _win()
    try:
        top = [a.text() for a in w.menuBar().actions()]
        # 【V0.0.19 改锁】5 个：任务/工具/视图/设置/帮助
        assert top == ["任务", "工具", "视图", "设置",
                       "帮助"], f"顶级菜单被改了：{top}"
    finally:
        w.close()


# ------------------------------------------------------------------ 3 功能等价
def test_slots_still_point_at_original_methods(qapp):
    """每个动作接的槽必须还是原来那个方法，不能搬着搬着接错。"""
    from media_resource_manager.gui import main_window as mw
    src = inspect.getsource(mw.MainWindow._build_ui)
    # 清空日志走 _clear_log_view（延迟取 log_view），不是直连 .clear
    assert "self.act_clear_log.triggered.connect(self._clear_log_view)" in src
    assert "self.act_clear_log.triggered.connect(self.log_view.clear)" not in src, (
        "清空日志又直连 log_view.clear 了 —— 菜单比日志页先建，连接时属性还不存在")
    for name, meth in (("act_export_log", "self.export_log"),
                       ("act_refresh_stats", None),
                       ("act_export_report", "self.export_report"),
                       ("act_import_tasks", "self.import_tasks")):
        if meth is not None:
            assert f"self.{name}.triggered.connect({meth})" in src, \
                f"{name} 没接原来的 {meth}"
    # 刷新统计：lambda 包一层，必须保留 force=True
    assert "self.act_refresh_stats.triggered.connect(\n            lambda: self.refresh_stats(force=True))" in src, (
        "刷新统计丢了 force=True —— refresh_stats 有节流，点了会被挡掉、看不到新数")


def test_clear_log_view_works_and_tolerates_missing(qapp):
    w = _win()
    try:
        w.log_view.setPlainText("这一行待清掉")
        assert w.log_view.toPlainText().strip() != ""
        w._clear_log_view()
        assert w.log_view.toPlainText().strip() == "", "点了清空日志但面板没清"
        # 日志页还没建（极端顺序）时也不能炸
        saved, w.log_view = w.log_view, None
        try:
            w._clear_log_view()          # 不抛异常即通过
        finally:
            w.log_view = saved
    finally:
        w.close()


def test_refresh_stats_action_forces_refresh(qapp, monkeypatch):
    """真触发一次菜单动作：必须以 force=True 调到 refresh_stats。"""
    w = _win()
    try:
        seen = []
        monkeypatch.setattr(
            type(w), "refresh_stats",
            lambda self, force=False: seen.append(force))
        w.act_refresh_stats.trigger()
        assert seen == [True], f"刷新统计菜单动作没走 force=True：{seen}"
    finally:
        w.close()


# ------------------------------------------------------------------ 4 不回退
def test_watermark_and_penetrate_untouched(qapp):
    """#45 只动日志页/菜单，不许碰水印几何与 hover 穿透。

    真正的水印铁律由 test_r57_watermark_overlay_geometry 与
    test_r58_watermark_pane_clip（含 QSS 顺序 + 像素 bbox）钉着，
    这里只做**回归提醒**：确认那些规则仍在原处、#45 没顺手挪动插入点。
    """
    from media_resource_manager.gui import theme

    for name in ("LIGHT_QSS", "DARK_QSS"):
        qss = getattr(theme, name)
        assert "QTabWidget::pane" in qss, f"{name} 缺 QTabWidget::pane 规则"
        idx = qss.rfind("QTabWidget::pane")
        assert "transparent" in qss[idx:idx + 200], (
            f"{name} 最后一条 pane 规则不是透明的：水印 S 会被裁成 'weet'")
        # 顺序铁律：透明那条必须在实底 pane 规则**之后**（同特异性后者胜）
        first_pane = qss.index("QTabWidget::pane")
        assert idx > first_pane, (
            f"{name} 的 pane 透明规则跑到了实底规则前面 —— 会被覆盖，r58 bug 复现")
