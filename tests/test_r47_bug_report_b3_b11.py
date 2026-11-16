"""R47 / BUG_REPORT_ORDER B3~B11：真机 11 条 bug 的红→绿自动化验收。

来源：台式机 WorkBuddy 2026-10-08 23:3x 下发，exe 14891518 真机实测。
本文件只收「offscreen 能客观判定」的那些：控件存在性、互斥性、几何不重叠、
字段清空、端到端 HTTP 状态、像素可见性。

⚠ 反复出现的两条教训，写在这里防止以后重犯：
1. **断言关系，不写死数值**（水印该多靠上 / chip 该显示几个，都随字体和
   DPI 变）。写死坐标 = 自己给自己埋快照式定时炸弹。
2. **offscreen 不能验命中测试**（`QTest.mouseClick` 投过去的事件不派发给
   任何子控件，连点按钮自己都是空）。所以"点击穿透"只能验属性，
   拿它当证据等于自欺欺人。
"""
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():  # noqa: D103
    import sys

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def win(qapp):  # noqa: D103
    """真 MainWindow（不 exec 任何模态框）。

    ⚠⚠⚠ 必须是 **module 粒度**（R48-h 实测的全量挂死）：
       本文件 23 个用例，是全项目建窗最多的测试文件。function 粒度 ⇒
       23 个 `MainWindow`（构造约 9s）在一个文件内堆积，而 conftest 的
       `_reap_top_level_widgets` 是 **module 粒度**回收 —— 文件内部一个都不收。
       它后面紧跟 `fill_all_history`(12) + `output_row_layout`(11)，
       三处叠加 ⇒ **全量跑到那里必挂死**（`-v` 实测停在 output_row_layout
       第 10 个用例，日志不增长、CPU 归零、junit 不生成；单跑各文件全过）。
       症状极像「测试坏了/产品有 bug」，实际是累积 —— 别去回退产品代码。
    """
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    w._ensure_task_form()
    for _ in range(4):
        qapp.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass
    qapp.setStyleSheet("")


@pytest.fixture(autouse=True)
def _reset_shared_state(win):
    """共享窗口**逐用例复位**（module 粒度的必要配套）。

    本文件多个用例会写表单字段（f_entry/f_name/f_out/f_cookie/f_sched…）、
    改 `cfg.last_output_dir`、切 `f_ai_*` 文本。不复位就会顺着用例顺序漏下去
    ⇒ 「单跑绿、合跑红」，且看着像真回归。
    """
    fields = ("f_entry", "f_ai_labels", "f_ai_l2", "f_ai_l3",
              "f_ai_excl", "f_out", "f_cookie", "f_sched")
    last_dir = getattr(win.cfg, "last_output_dir", "")
    try:
        yield
    finally:
        for name in fields:
            w = getattr(win, name, None)
            if w is not None:
                try:
                    w.setText("")
                except Exception:
                    pass
        win.cfg.last_output_dir = last_dir
        cb = getattr(win, "_cb_reuse_last", None)
        if cb is not None:
            cb.setChecked(False)


# =========================================================== B3 保存到行

def test_output_row_controls_do_not_overlap(win, qapp):
    """B3：「保存到」这一行控件互不重叠、且都在该行容器内。

    ⚠⚠ R58 重写判据（原来这条是**假红**，不是回归）。
    原判据用 `mapTo(row, QPoint(0,0))` 算相对坐标再比 `row.rect()`。
    探针（tools/_r58_probe_row_layout.py）实测证明它结构上不可靠：
      · `_cb_reuse_last` 的父容器**不是** `f_out.parentWidget()`（那个 row），
        祖先链实测为 `QWidget ← QWidget ← MainWindow` —— 它是 row 的**兄弟**；
      · `row.layout()` 只有 2 项（QLineEdit + QPushButton），**不含 cb**；
      · offscreen 下它 geom=(98,0,310,16)，宽 310 恰是「未 layout」的默认尺寸；
      · 跨层 `mapTo` + 未 layout ⇒ 返回 (109,107,...) ⇒ 必然被判「跑出行外」。
    触发变更：main_window.py 现在是
        `add_row(half_cell("沿用上次目录", self._cb_reuse_last), QWidget())`
    **独立成行**（原先两层已合并成一层），而 `f_out` 在 `out_row` 里。
    也就是说：**控件真的不重叠，是断言在量一个不成立的关系。**

    改成断言**关系**而不是数值（MEMORY 铁律）：
      ① `f_out` 与 `cb` 必须落在**不同的 add_row** 上（不同行 ⇒ 不可能压字），
         判据用 AST 读源码 —— offscreen 几何不可信，字面量结构可信
         （与 R37/R52 同源的做法）；
      ② 两个控件必须在**建面板时**就建好，不许靠 _open_task_form 动态 add
         （这是原用例真正要守的东西）；
      ③ `_lbl_last_out` 必须 `setVisible(False)` —— 它现在是兼容占位对象，
         一旦可见就会真的多占一行，那才是 B3 意义上的压字。
    """
    import ast
    import inspect
    from pathlib import Path

    win._ensure_task_form()

    # ① 结构：f_out 与 cb 不同行
    src = Path(inspect.getsourcefile(type(win))).read_text(encoding="utf-8")
    tree = ast.parse(src)
    rows = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "add_row":
            continue
        try:
            rows.append(ast.get_source_segment(src, node) or "")
        except Exception:
            rows.append(ast.dump(node))
    row_f_out = [r for r in rows if "out_row" in r or "self.f_out" in r]
    row_cb = [r for r in rows if "_cb_reuse_last" in r]
    assert row_f_out, "源码里找不到含 f_out/out_row 的 add_row —— 结构调整了？"
    assert row_cb, "源码里找不到含 _cb_reuse_last 的 add_row —— 勾选框被移走了"
    # ⚠⚠ R58 自我修正：判据必须是「**没有任何一行同时含两者**」，
    #   而不是 `row_f_out != row_cb`（两个列表不相等）。
    #   后者有个真实漏洞：把 cb 挪到**另一行**（标签换成别的字）时，
    #   两个列表依然不同 ⇒ 断言照样绿，可那仍然可能是压字
    #   （真压字= 两者被塞进**同一个 add_row 的两个格子**）。
    #   反向验证 M1 就是这么骗过第一版的（我第一版写得太松，自己抓到了）。
    for r in rows:
        has_out = ("out_row" in r) or ("self.f_out" in r)
        has_cb = "_cb_reuse_last" in r
        assert not (has_out and has_cb), (
            f"f_out 与「沿用上次目录」勾选框出现在**同一行** —— 会压字（B3）：{r[:90]}")

    # ② ③ 控件存在性 + 兼容标签必须不可见（运行期判据，不依赖几何）
    cb = getattr(win, "_cb_reuse_last", None)
    row_lbl = getattr(win, "_lbl_last_out", None)
    assert cb is not None and row_lbl is not None, (
        "「沿用上次目录」勾选框/提示必须在**建面板时**就建好，"
        "不能靠 _open_task_form 动态 addWidget（B3）")
    assert row_lbl.isHidden(), (
        f"_lbl_last_out 现在是兼容占位，必须 setVisible(False)；"
        f"实际 isHidden={row_lbl.isHidden()} —— 它一旦可见就会多占一行（B3 压字）")
    # ⚠ 用 isHidden() 不用 isVisible()（MEMORY R50 铁律）：
    #   offscreen 下 isVisible() 要求**整条祖先链都 visible**，
    #   而表单面板在测试里从未真正 show ⇒ isVisible() 恒False，
    #   拿它判「控件该显示」就是永久假红（同族坑：R52 的 mapTo 几何）。
    assert not cb.isHidden(), "勾选框被隐藏了 —— 用户看不到就没法用（B3）"

    # 不同行 ⇒ 两行的行容器必然不同，这是不压字的结构性保证
    assert win.f_out.parentWidget() is not cb.parentWidget(), (
        "f_out 与勾选框竟在同一容器里 —— 会压字（B3）")


def test_opening_new_task_twice_does_not_stack_widgets(win, qapp):
    """B3 关键回归：反复「新建任务」不许把控件越叠越多。

    真机就是「点一次糊一点」。这里直接数该行容器里的子控件数量，
    调三次 `_refresh/rebuild` 之后必须**一样多**。
    """
    from PySide6.QtWidgets import QWidget

    win._ensure_task_form()
    panel = win._form_panel
    panel.resize(840, 700)
    panel.show()
    for _ in range(4):
        qapp.processEvents()

    row = win.f_out.parentWidget()

    def _count():
        return len([c for c in row.children()
                    if c.isWidgetType() and not c.isWindow()])

    win.cfg.ai_label_history = ["white stockings", "black pantyhose"]
    before = None
    for _ in range(3):
        win._refresh_hist_chips()
        win._reset_form_for_new()
        for _ in range(2):
            qapp.processEvents()
        n = _count()
        if before is None:
            before = n
        assert n == before, (
            f"「保存到」行子控件数从 {before} 变成 {n} —— 控件在累积（B3）")


# =========================================================== B9 新建清空

def test_new_task_clears_all_fields(win):
    """B9：新建任务必须**全字段清空**，不能残留上一个任务。

    真机：「开新建任务对话框还带着上次任务填的内容」。真因：r39 起表单
    面板是复用的，`self.f_*` 一直指着同一批控件，没人负责重置。
    """
    win._ensure_task_form()
    win.f_entry.setText("https://example.com/上次任务")
    win.f_ai_labels.setText("white stockings")
    win.f_ai_l2.setText("jk 制服")
    win.f_ai_l3.setText("黑长直")
    win.f_out.setText("C:/some/old/dir")
    win.f_cookie.setText("cookie=xxx")
    win.f_sched.setText("03:00")

    win._reset_form_for_new()

    for name in ("f_entry", "f_ai_labels", "f_ai_l2", "f_ai_l3",
                 "f_out", "f_cookie", "f_sched"):
        w_ = getattr(win, name)
        assert not w_.text().strip(), (
            f"{name} 残留了上一个任务的内容：{w_.text()!r}（B9）")


def test_new_task_keeps_sane_defaults_not_zero(win):
    """B9 的反面：新建成效值必须是**合理默认**，不是一律清零。

    ⚠ 这是本条最容易写坏的地方：一律 `setValue(0)` 会把「AI 阈值 0」
    当成「什么都不要」，那是另一个更隐蔽的 bug。所以钉住关系：
    阈值仍是 0.35、并发/深度仍是正数。
    """
    win._ensure_task_form()
    win._reset_form_for_new()
    assert abs(float(win.f_ai_thr.value()) - 0.35) < 1e-6, (
        f"AI 阈值被重置成 {win.f_ai_thr.value()}，应为默认 0.35")
    assert int(win.f_conc.value()) >= 1, "并发被重置成 0，新建任务没法跑"
    assert int(win.f_depth.value()) >= 1, "深度被重置成 0"


def test_reuse_last_checkbox_is_opt_in(win):
    """B9/P1-2：「沿用上次目录」默认**不勾**，勾上才回填。"""
    win._ensure_task_form()
    win.cfg.last_output_dir = "C:/上次/目录"
    cb = getattr(win, "_cb_reuse_last", None)
    assert cb is not None, "缺少「沿用上次目录」勾选框"

    cb.setChecked(False)
    win._on_reuse_last_toggled(False)
    assert not win.f_out.text().strip(), "没勾却回填了目录"

    cb.setChecked(True)
    win._on_reuse_last_toggled(True)
    assert win.f_out.text().strip() == "C:/上次/目录", (
        f"勾了却没回填：{win.f_out.text()!r}")


# =========================================================== B11 常用词

def test_word_chips_use_wrapping_layout(win, qapp):
    """B11：常用词那一行必须用**可换行**布局，且 chip 不越界。

    真机：「常用词列表盖住旁边内容，很难看」+「文字后面有多余字符」。
    """
    from media_resource_manager.gui.main_window import _FlowLayout

    win._ensure_task_form()
    win.cfg.ai_label_history = [f"word{i}" for i in range(30)]
    win._refresh_hist_chips()
    for _ in range(4):
        qapp.processEvents()

    hw = getattr(win, "_hist_chips_ai_label_history", None)
    assert hw is not None, "缺少常用词容器"
    assert isinstance(hw._box.layout(), _FlowLayout), (
        f"常用词仍是 {type(hw._box.layout()).__name__}（不换行 → 会盖住旁边内容，B11）")


def test_word_chips_stay_inside_container(win, qapp):
    """B11：chip 的矩形必须**全部落在容器内**（不许压到相邻控件）。"""
    from PySide6.QtCore import QPoint, QRect
    from PySide6.QtWidgets import QPushButton

    win._ensure_task_form()
    panel = win._form_panel
    panel.resize(840, 700)
    panel.show()
    for _ in range(4):
        qapp.processEvents()

    win.cfg.ai_label_history = [f"verylongword{i}" for i in range(24)]
    win._refresh_hist_chips()
    for _ in range(6):
        qapp.processEvents()

    hw = getattr(win, "_hist_chips_ai_label_history", None)
    box = hw._box
    for c in box.findChildren(QPushButton):
        if not c.isVisible():
            continue
        tl = c.mapTo(box, QPoint(0, 0))
        r = QRect(tl, c.size())
        assert box.rect().contains(r), (
            f"chip {c.text()!r} 越界：{r.getRect()} vs 容器 {box.rect().getRect()}（B11）")


def test_word_chip_text_is_elided(win, qapp):
    """B11：超长词要**省略**，不许把 chip 撑成一整行。"""
    from PySide6.QtWidgets import QPushButton

    win._ensure_task_form()
    panel = win._form_panel
    panel.resize(840, 700)
    panel.show()                 # 不 show 的话 chip 的 isVisible() 恒 False
    for _ in range(4):
        qapp.processEvents()

    win.cfg.ai_label_history = ["x" * 200]
    win._refresh_hist_chips()
    for _ in range(4):
        qapp.processEvents()

    hw = getattr(win, "_hist_chips_ai_label_history", None)
    chips = [c for c in hw._box.findChildren(QPushButton) if c.isVisible()]
    assert chips, "没有生成 chip"
    # ⚠⚠ 限宽判据**只能作用于词 chip**，不能把「更多(N)…」算进去（R48-h 实测）：
    #   `_refresh_hist_chips` 在词数超可见额度时额外加一个
    #   `mb = QPushButton(f"更多({n})…")`，它是 `setFlat(True)` 的纯文字入口，
    #   **刻意不设限宽**（限了会把「更多」两个字截断，反而看不懂）。
    #   原代码在下一行就算出了 `real`，但**限宽那一步漏了过滤** ⇒ 把「更多」也当
    #   词 chip ⇒ 读到 `maximumWidth() == 16777215`（QWT_SIZE_MAX）⇒ **假红**。
    #   症状极像「B11 回归」，其实在测一个不存在的要求。
    #   （为什么之前没炸：function 粒度下 `avail` 够大、没触发「更多」分支；
    #     改 module 粒度后前面用例留下了历史词，才第一次露出这个判据漏洞。）
    real = [c for c in chips if not c.text().startswith("更多")]
    assert real, "只剩「更多」按钮，没有真正的 chip"
    for c in real:
        assert c.maximumWidth() <= 200, (
            f"chip 没限宽（{c.maximumWidth()}），长词会顶爆整行（B11）")
        assert len(c.text()) < 200, (
            f"超长词没省略，还是 {len(c.text())} 字：{c.text()[:40]!r}…（B11）")


# =========================================================== B4/B5 代理

def test_proxy_type_is_mutually_exclusive_radio(qapp):
    """B4：代理「类型」必须是互斥单选（QRadioButton），不是下拉。"""
    from PySide6.QtWidgets import QRadioButton
    from media_resource_manager.gui.proxy_dialog import ProxyDialog

    class _Pool:
        def list(self, *a, **k):
            return []

        def save(self, e):
            pass

    dlg = ProxyDialog(_Pool())
    try:
        from media_resource_manager.gui.proxy_dialog import PROXY_TYPES

        assert len(PROXY_TYPES) >= 3, "类型选项太少"
        rbs = list(dlg.rb_by_scheme.values())
        assert len(rbs) == len(PROXY_TYPES)
        for rb in rbs:
            assert isinstance(rb, QRadioButton), (
                f"{type(rb).__name__} 不是 QRadioButton（B4）")
        assert dlg.rb_group.exclusive(), "单选组不互斥（B4）"

        # 真点一下：勾第 2 个，第 1 个必须自动取消
        rbs[0].setChecked(True)
        rbs[2].setChecked(True)
        assert rbs[2].isChecked()
        assert not rbs[0].isChecked(), "勾了一个另一个还留着 —— 不互斥（B4）"
        assert dlg._selected_scheme() == PROXY_TYPES[2][1]
    finally:
        dlg.deleteLater()


def test_proxy_dialog_has_no_test_no_list_no_priority(qapp):
    """B5：代理对话框**不得有**测试按钮 / 已保存列表 / 优先级控件。

    用户原话：只要一条代理，不要列表、不要优先级、不要测试。
    """
    from PySide6.QtWidgets import (QListWidget, QPushButton, QSpinBox,
                                   QTableWidget)
    from media_resource_manager.gui.proxy_dialog import ProxyDialog

    class _Pool:
        def list(self, *a, **k):
            return []

        def save(self, e):
            pass

    dlg = ProxyDialog(_Pool())
    try:
        texts = [b.text() for b in dlg.findChildren(QPushButton)]
        for bad in ("测试选中", "全部测试", "全部取消", "新增/保存", "删除"):
            assert not any(bad in t for t in texts), (
                f"仍有「{bad}」按钮（B5）：{texts}")
        assert dlg.findChildren(QListWidget) == [], "仍有已保存代理列表（B5）"
        assert dlg.findChildren(QTableWidget) == [], "仍有代理表格（B5）"
        assert not hasattr(dlg, "e_pri"), "仍有优先级控件 e_pri（B5）"
        assert not hasattr(dlg, "lst"), "仍有代理列表 lst（B5）"
        # 必须有「确定」和「关闭」
        assert any("确定" in t for t in texts), "缺少「确定」"
        assert any("关闭" in t for t in texts), "缺少「关闭」"
    finally:
        dlg.deleteLater()


def test_proxy_stores_single_entry_overwrite(qapp):
    """B5：只存**一条**、覆盖写（不累加）。"""
    from media_resource_manager.gui.proxy_dialog import ProxyDialog, SINGLE_PROXY_NAME

    saved = []

    class _Pool:
        def __init__(self):
            self.rows = []

        def list(self, *a, **k):
            return list(self.rows)

        def save(self, e):
            saved.append(e)
            self.rows = [e]        # 覆盖写：库里永远只有一条
            return e.id

    pool = _Pool()
    dlg = ProxyDialog(pool)
    try:
        dlg.rb_by_scheme["http"].setChecked(True)
        dlg.e_host.setText("10.0.0.1")
        dlg.e_port.setValue(3128)
        dlg.save_and_close()

        assert len(saved) == 1, "第一次保存没落库"
        assert saved[0].name == SINGLE_PROXY_NAME
        assert saved[0].host == "10.0.0.1"
        assert saved[0].priority == 0, "还有优先级概念（B5）"

        # 再存一次不同的 → 库里仍然只有一条
        dlg2 = ProxyDialog(pool)
        try:
            dlg2.rb_by_scheme["socks5"].setChecked(True)
            dlg2.e_host.setText("10.0.0.2")
            dlg2.e_port.setValue(1080)
            dlg2.save_and_close()
        finally:
            dlg2.deleteLater()
        assert len(pool.rows) == 1, (
            f"存了两次后库里有 {len(pool.rows)} 条 —— 应恒为 1 条（B5）")
        assert pool.rows[0].host == "10.0.0.2", "第二次没覆盖第一条"
    finally:
        dlg.deleteLater()


def test_proxy_direct_mode_disables_entry(qapp):
    """B4/B5：选「不使用代理（直连）」→ 不要求填地址即可保存。"""
    from media_resource_manager.gui.proxy_dialog import ProxyDialog

    saved = []

    class _Pool:
        def __init__(self):
            self.rows = []

        def list(self, *a, **k):
            return list(self.rows)

        def save(self, e):
            saved.append(e)
            self.rows = [e]
            return e.id

    pool = _Pool()
    dlg = ProxyDialog(pool)
    try:
        dlg.rb_by_scheme[""].setChecked(True)     # 直连
        dlg.e_host.clear()
        dlg.e_port.setValue(0)
        dlg.save_and_close()
        assert saved, "直连模式没能保存"
        assert not saved[0].enabled, "直连却标成 enabled（会被 resolve 选中）"
    finally:
        dlg.deleteLater()


# =========================================================== B8 学习看板

def test_learning_board_refresh_keeps_window_open(qapp):
    """B8：点「刷新」**绝不能**关窗口（真机：一点刷新窗口就没了）。

    真因：`_build()` 里给「刷新」按钮多连了一句 `.connect(self.accept)`。

    ⚵ 必须先 `show()`：offscreen 下没显示的窗口 `isVisible()` 恒为 False，
    那样这条断言对"窗口还在不在"完全没有分辨力（会永远红或永远绿）。
    """
    from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPushButton
    from media_resource_manager.gui.learning_board import LearningDashboard

    class _DB:
        def query(self, *a, **k):
            return []

    app = QApplication.instance()
    dlg = LearningDashboard(_DB())
    try:
        dlg.show()
        for _ in range(4):
            app.processEvents()
        assert dlg.isVisible(), "前提不成立：看板没显示出来"

        bb = dlg.findChild(QDialogButtonBox)
        btn = next(b for b in bb.findChildren(QPushButton)
                   if "刷新" in b.text())
        btn.click()
        for _ in range(4):
            app.processEvents()

        assert dlg.isVisible(), "点「刷新」后看板被关掉了（B8）"
        assert not dlg.result(), (
            f"对话框 result={dlg.result()}（Accepted=1 说明被 accept 了，B8）")
    finally:
        dlg.deleteLater()


def test_learning_board_has_no_close_on_refresh_wiring(qapp):
    """B8 静态护栏：「刷新」按钮不许连 accept/reject/close。

    ⚠ 判据必须**只盯刷新按钮那几行**，不能对整个 `_build` 裸搜
    `self.accept`/`self.reject` —— `bb.rejected.connect(self.reject)` 是
    「关闭」按钮的**正确**接线（点了就该关），全文搜会把它一起判红。
    （本项目反复踩的同一个坑：注释/无关代码里 quoting 同一个词。）
    """
    import ast
    import inspect
    import textwrap
    from media_resource_manager.gui import learning_board

    src = textwrap.dedent(inspect.getsource(learning_board.LearningDashboard._build))
    tree = ast.parse(src)

    def _const_str(node):
        return node.value if isinstance(node, ast.Constant) and \
            isinstance(node.value, str) else None

    offenders = []
    for node in ast.walk(tree):
        # 形如  b_reload.clicked.connect(X)
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"):
            continue
        recv = node.func.value
        # 绑定名：b_reload / b_x 之类与「刷新」有关的局部变量
        name = recv.id if isinstance(recv, ast.Name) else (
            recv.attr if isinstance(recv, ast.Attribute) else "")
        if "reload" not in name.lower() and "刷新" not in name:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute) and arg.attr in (
                    "accept", "reject", "close", "deleteLater"):
                offenders.append(f"{name}.connect(self.{arg.attr})")
    assert not offenders, (
        f"「刷新」按钮不许关窗口（B8）：{offenders}")


def test_learning_board_refresh_button_label_present(qapp):
    """前提自检：确实存在一个「刷新」按钮（否则上面那条护栏是空转）。"""
    from PySide6.QtWidgets import QDialogButtonBox, QPushButton
    from media_resource_manager.gui.learning_board import LearningDashboard

    class _DB:
        def query(self, *a, **k):
            return []

    dlg = LearningDashboard(_DB())
    try:
        bb = dlg.findChild(QDialogButtonBox)
        labels = [b.text() for b in bb.findChildren(QPushButton)]
        assert any("刷新" in t for t in labels), f"没有「刷新」按钮：{labels}"
    finally:
        dlg.deleteLater()


# =========================================================== B7 Web 白屏

@pytest.fixture
def webserver(qapp, tmp_path):
    """起一个真 WebServer（随机端口），用完即停。"""
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    cfg = AppConfig()
    cfg.isolate_dir = str(tmp_path)
    cfg.library_dir = str(tmp_path / "lib")
    cfg.web_enable = True
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0
    # ⚠⚠ R58：必须显式**关掉**局域网免密。
    #   `web_lan_open` 默认 True，而这里绑的是 127.0.0.1 ⇒ 回环属私有地址
    #   ⇒ `_client_allowed_by_ip()` 恒 True ⇒ 数据面恒免密。
    #   那样 `test_web_api_still_requires_login` 的「未登录必须 401」
    #   结构上永远不成立（实测返回 200），B7 的安全红线等于没测。
    #   关掉后 127.0.0.1 不再享受免密，红线才真的被测到。
    cfg.web_lan_open = False
    db = Database(tmp_path / "library.db")
    srv = webui.WebServer(cfg, db)
    ok, msg = srv.start()
    if not ok:
        pytest.skip(f"Web 起不来：{msg}")
    yield srv
    try:
        srv.stop()
    except Exception:
        pass


def _get(srv, path):
    import urllib.request
    url = f"http://127.0.0.1:{srv.port}{path}"
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()

def test_web_manifest_and_sw_are_not_html(webserver):
    """B7：manifest / sw.js 同样不能被登录壳顶替。"""
    from media_resource_manager import webui

    root = webui.web_assets_dir()
    for url_path, rel, ctype_want in (
            ("/manifest.webmanifest", "manifest.webmanifest", "json"),
            ("/sw.js", "sw.js", "javascript")):
        status, ctype, body = _get(webserver, url_path)
        assert status == 200, f"{url_path} HTTP {status}"
        assert ctype_want in ctype.lower(), (
            f"{url_path} Content-Type={ctype}，应含 {ctype_want}")
        assert body == (root / rel).read_bytes(), f"{url_path} 内容不是真资源（B7）"


def _code_of(url):
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_web_api_still_requires_login(webserver):
    """B7 的安全红线：静态资源放行**不等于**数据面放行。

    放开 `/static/*` 是为了登录页能渲染；`/api/*` 与 `/thumb/` 必须
    仍然鉴权，否则就是白送给未登录的人。

    ⚠ **R157 之后这里必须显式设密码才测得动**（2026-10-20 修）：
    空密码 = 免密模式（R141 定的契约，R157 把后端两处统一过来），
    本机 / 局域网客户端直接放行 ⇒ 不设密码时「未登录必须 401」这条断言
    **结构上不可能成立**（实测 200），安全红线等于没测。
    所以这里临时给 cfg 挂一个真密码，测完还原，不污染同夹具的其它用例。
    """
    orig = webserver.cfg.web_password
    webserver.cfg.web_password = "r47-line-secret"
    try:
        for path in ("/api/summary", "/api/tasks", "/api/gallery"):
            code = _code_of(f"http://127.0.0.1:{webserver.port}{path}")
            assert code == 401, (
                f"未登录访问 {path} 返回 {code}，应为 401 —— B7 修复放宽过头了")
    finally:
        webserver.cfg.web_password = orig


def test_web_api_open_when_no_password_is_set(webserver):
    """R157 反向钉住：空密码 + 本机 = **免密**，不是"放宽过头"。

    与上一条互为对照，两条都在才叫守住边界：
      · 设了密码 → 必须登录（上一条）；
      · 没设密码 → 不该要密码（这一条，R141/R157）。
    谁把哪一侧改坏，另一条立刻红。
    ⚠ 公网 + 空密码仍必须拦 ⇒ 由 tests/test_r157_guards.py 钉住
    （真实 socket 造不出公网来源 IP，只能在那边的 handler 替身上验）。
    """
    orig = webserver.cfg.web_password
    webserver.cfg.web_password = ""
    try:
        for path in ("/api/summary", "/api/tasks", "/api/gallery"):
            code = _code_of(f"http://127.0.0.1:{webserver.port}{path}")
            assert code == 200, (
                f"空密码 + 本机访问 {path} 返回 {code}，应为 200 —— "
                f"R157 免密模式没生效（用户会被锁在门外）")
    finally:
        webserver.cfg.web_password = orig


# =========================================================== B6 CLIP

def test_pick_backend_falls_back_to_bundled_models(monkeypatch, tmp_path):
    """B6b：用户数据目录没有 CLIP 时，必须回退到**包内 bundled_models**。

    真机：「本地 AI 处于降级状态」。修法 = 依次尝试多个候选目录，
    任一成功即用（作业单 B6b 原文：优先用 bundled_models 回退根除）。
    """
    from media_resource_manager.classify import clip_model

    tried = []

    class _FakeClip:
        def __init__(self, model, tok, threads=0, gpu=True):
            self.model, self.tok = model, tok

    def _fake_try(self, md):
        tried.append(str(md))
        # 只有「包内 bundled_models」这一条能成功
        if "bundled_models" in str(md):
            self._clip = _FakeClip("m", "t")
            return True
        return False

    monkeypatch.setattr(clip_model.LocalClassifier, "_try_clip_dir", _fake_try)
    c = clip_model.LocalClassifier()
    assert c.backend == "clip", (
        f"backend={c.backend}（应为 clip）—— 没回退到 bundled_models（B6b）")
    assert any("bundled_models" in t for t in tried), (
        f"根本没试 bundled_models，候选={tried}")


def test_pick_backend_falls_back_to_stub_only_after_all_fail(monkeypatch):
    """B6b：所有候选都失败才落 stub（不能一失败就 return）。"""
    from media_resource_manager.classify import clip_model

    monkeypatch.setattr(clip_model.LocalClassifier, "_try_clip_dir",
                        lambda self, md: False)
    c = clip_model.LocalClassifier()
    assert c.backend == "stub", f"应为 stub，实际 {c.backend}"


def test_bundled_clip_dir_is_actually_usable():
    """B6b 端到端前提：包内 bundled CLIP 目录必须**齐全**。

    少了 tokenizer.json 之类，`_try_clip_dir` 会静默返回 False，
    于是又变回「明明随软件带了模型却一直降级」。
    """
    from media_resource_manager.models_lib import bundled_models_dir

    d = bundled_models_dir() / "clip-vit-b32"
    if not d.is_dir():
        pytest.skip("本机没有 bundled CLIP（源码树未放模型）")
    assert (d / "model_quantized.onnx").exists(), "缺 model_quantized.onnx"
    assert (d / "tokenizer.json").exists(), (
        "缺 tokenizer.json —— 这正是 onedir 部署下降级的直接原因")


# =========================================================== B6a 完成判定

def test_zero_result_task_gets_explanatory_note(tmp_path, monkeypatch):
    """B6a + R188：「0 进度即完成」必须说清楚为什么没结果，且**判失败**。

    真机：任务一启动就显示「完成」，进度全 0。根因是 `hard_error` 要求
    `bool(rep.errors)`，站点侧 403/JS 挑战常常一条 error 都不记。

    B6a 当时只把「完成」改成「带说明的完成」；R188（用户又报"任务直接
    完成了"）升级为：0 图一律判 ERROR 并登记进 failed（重试可用）——
    「站点确实没图」与「站点被拦」从报告里分不出来，但两者都**不该
    假装成功**。
    """
    from media_resource_manager.engine import RunReport
    from media_resource_manager.models import TaskStatus
    from tests.test_c1_p11_failed_retry import _drive, _mk_queue

    q = _mk_queue(str(tmp_path))
    rep = RunReport(task_id=0, images_found=0, failed=0, downloaded=0,
                    errors=[])
    tid = _drive(q, monkeypatch, rep)

    from media_resource_manager.db import Database as _D
    from media_resource_manager.repo import get_task
    db = _D(q.cfg.db_path)
    try:
        t = get_task(db, tid)
    finally:
        db.close()

    assert t.status is TaskStatus.ERROR, (
        "R188：0 结果必须判 ERROR，不许再标「完成」")
    assert t.last_error and ("未获取到" in t.last_error or "没有发现" in t.last_error), (
        f"0 结果却没有任何说明：{t.last_error!r}（B6a）")
    assert tid in q.failed, "R188：0 结果任务要进 failed，重试菜单才摸得到"


# =========================================================== B10 原生目录框

def test_dir_pick_uses_native_dialog(win, monkeypatch):
    """B10：目录选择器必须恢复**原生**后端。

    真机：「输出目录浏览现在用的是自绘对话框，用户要的是系统原生」。
    实测矩阵里「原生 + exec()」这一格本来就写着安全（139 只发生在
    「原生 + open() 被 Qt 侧主动关」），所以恢复原生不需要退回自绘。
    """
    from PySide6.QtWidgets import QFileDialog

    seen = {}

    class _FakeDlg:
        def __init__(self, *a, **k):
            self.opts = {}

        def setOption(self, k, v=True):
            self.opts[k] = v
            if k == QFileDialog.Option.DontUseNativeDialog:
                seen["dont_use_native"] = v

        def setFileMode(self, m):
            seen["file_mode"] = m

        def setDirectory(self, d):
            seen["start"] = d

        def exec(self):
            seen["used_exec"] = True
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Accepted

        def selectedFiles(self):
            return ["C:/选中的目录"]

    monkeypatch.setattr(QFileDialog, "__init__",
                        lambda self, *a, **k: _FakeDlg.__init__(self, *a, **k))
    monkeypatch.setattr(QFileDialog, "setOption",
                        lambda self, k, v=True: _FakeDlg.setOption(self, k, v))
    monkeypatch.setattr(QFileDialog, "setFileMode",
                        lambda self, m: _FakeDlg.setFileMode(self, m))
    monkeypatch.setattr(QFileDialog, "setDirectory",
                        lambda self, d: _FakeDlg.setDirectory(self, d))
    monkeypatch.setattr(QFileDialog, "exec",
                        lambda self: _FakeDlg.exec(self))
    monkeypatch.setattr(QFileDialog, "selectedFiles",
                        lambda self: _FakeDlg.selectedFiles(self))

    out = win._dir_pick("选择输出目录", start="C:/起点")
    assert out == "C:/选中的目录", f"返回值不对：{out!r}"
    assert seen.get("used_exec"), "必须用 exec()（open() + 原生 = SIGSEGV）"
    assert not seen.get("dont_use_native"), (
        "目录选择仍在禁用原生后端（B10）")
    assert seen.get("file_mode") == QFileDialog.FileMode.Directory
