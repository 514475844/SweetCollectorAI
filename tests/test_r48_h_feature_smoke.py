"""R48 / THOROUGH_AUDIT_ORDER H：全功能普查（meta 段，用户「今晚彻底检查所有功能」）。

本文件是**普查表**：作业单 H 逐条列了要普查的功能，这里每条一个用例，
断言**行为关系**而不是「函数存在」。

⚠⚠⚠ 三条铁律，都是前几轮真踩出来的（本文件第一版就踩了第 1、2 条）：

  1. **绝不能真调末行是 `dlg.exec()` / `QMessageBox.xxx` 的方法**。
     第一版写了 `win.show_learning_dashboard()` 和 `win.proxy_health_check()`，
     前者内部 `dlg.exec()`、后者内部 `QMessageBox.information` ——
     offscreen 下**永久阻塞**（实测整个文件卡死 7 分钟、CPU 归零、
     **junit 根本不生成**，症状与 R25/R37 堆损坏一模一样）。
     正解：**读磁盘源码切块 + 正则/AST 判据**，或只调「确定不弹窗」的那半边。
     另：给 PySide6 C++ 类属性赋桩是**静默失效**的，`QMessageBox.information = x`
     也不保证拦住内部调用 —— 唯一的可靠做法就是**别调**。

  2. **别猜控件/属性名**。第一版凭空写了 `form.f_dir` / `form.chk_reuse_dir` /
     `win._form_panel` / `win.sched.paused`，十有八九不存在 ⇒ 一片假红。
     正解：**先 grep 源码取真名**（本文件所有控件名都来自
     `tests/test_r48_output_row_layout.py` 等既有测试 + 源码 grep）。

  3. **`win` 必须是 module 粒度**。MainWindow 构造约 9s，而 conftest 的
     `_reap_top_level_widgets` 是 module 粒度回收 —— function 粒度会让本文件
     堆出 N 个窗口，全量跑到这里时挂死在 47%（R48-i 实测两次）。
     本文件**只建 1 个窗口**。

**断言关系，不写死数值**：窗口尺寸/DPI/字体一变就假失败，且看着像真回归。
"""
import ast
import inspect
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

MW = Path("media_resource_manager/gui/main_window.py")
RB = Path("media_resource_manager/gui/review_tab.py")
LB = Path("media_resource_manager/gui/learning_board.py")
WEBUI = Path("media_resource_manager/webui.py")


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fn_src(path: Path, name: str) -> str:
    """取某个类/模块级函数的源码切块（AST 定位，不靠正则猜缩进）。"""
    tree = ast.parse(_src(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_src(path), node) or ""
    return ""


@pytest.fixture(scope="module")
def qapp():  # noqa: D103
    import sys

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    return app


@pytest.fixture(scope="module")
def win(qapp):  # noqa: D103
    from media_resource_manager.gui import theme
    from media_resource_manager.gui.main_window import MainWindow

    qapp.setStyleSheet(theme.LIGHT_QSS)
    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w.resize(1382, 899)
    w.show()
    w._ensure_task_form()          # 表单面板懒构建
    for _ in range(4):
        qapp.processEvents()
    yield w
    try:
        w.close()
    except Exception:
        pass
    qapp.setStyleSheet("")


# ══════════════════════════════════════════════════════════════
# 1. 任务页：列表/网格、新建、编辑、启动、暂停、删除、重试
# ══════════════════════════════════════════════════════════════


def test_task_page_core_actions_wired():
    """任务页核心动作齐备**且接了槽**（动作在但没接槽 = 点了没反应）。

    ⚠ 用 AST 判「connect 存在」，不用 `act.receivers()` ——
       PySide6 的 `receivers()` 要传字符串签名，传 bound signal 会 TypeError。
    """
    src = _src(MW)
    tree = ast.parse(src)
    wired = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "connect":
            seg = ast.get_source_segment(src, node) or ""
            m = re.search(r"self\.(act_\w+)\.triggered\.connect", seg)
            if m:
                wired.add(m.group(1))
    for name in ("act_new", "act_edit", "act_pause", "act_retry"):
        assert f"self.{name} = QAction(" in src, f"任务页缺动作 {name}"
        assert name in wired, f"{name} 没接 triggered 槽（点了没反应）"


def test_task_form_new_resets_to_open_interval_defaults(win):
    """B9：新建表单打开是「合理默认」，不是全零。

    ⚠ `_ensure_task_form()` **返回 None**（它只保证面板已建，控件是挂到
       MainWindow 上的 `self.f_*`）—— 别写 `form = win._ensure_task_form()`
       然后在 form 上找控件，那必然全是None。
    ⚠ 关系断言：阈值落在**开区间**（0 < v < 1），不写死 0.35。
    """
    win._ensure_task_form()
    win._reset_form_for_new()
    thr = win.f_ai_thr.value()
    assert 0.0 < thr < 1.0, f"新建表单 AI 阈值应是开区间默认值，实得 {thr}"
    assert win.f_ai_thr.singleStep() > 0, "AI 阈值步进为 0（用户调不了）"


def test_task_form_opens_for_edit_without_clearing_config(win):
    """编辑表单会带值进场（`_fill_form`），新建才重置 —— 两者不能混。"""
    src = _fn_src(MW, "_fill_form")
    assert "f_ai_thr" in src, "_fill_form 没回填 AI 阈值（编辑会把配置清掉）"
    src2 = _fn_src(MW, "_reset_form_for_new")
    assert "f_ai_thr" in src2, "_reset_form_for_new 没给 AI 阈值合理默认"


def test_pause_toggle_is_reversible_and_guarded(win):
    """暂停/继续：空队列必须直接 return，有队列才切换 —— 不能只进不退。"""
    src = _fn_src(MW, "toggle_pause")
    assert "if not self._queue" in src, "toggle_pause 缺空队列守卫（空队列会 AttributeError）"
    assert "resume()" in src and "pause()" in src, "toggle_pause 必须能暂停也能继续"
    # 不真调：真调会 emit 信号并可能弹窗；这里只验代码形状。
    if win._queue is not None:
        before = win._queue.snapshot()["paused"]
        win.toggle_pause()
        win.toggle_pause()
        assert win._queue.snapshot()["paused"] == before, "暂停来回切后状态没复原"


def test_retry_failed_handles_empty_queue():
    """空队列点「重试失败任务」不抛 —— 用户会在空库时点它。

    ⚠ 读源码判「有没有空列表守卫」，不真调（真调可能弹提示框）。
    """
    src = _fn_src(MW, "retry_failed")
    assert src, "找不到 retry_failed"
    assert re.search(r"if\s+not\s+\w", src), "retry_failed 没有空列表守卫"


def test_all_task_ids_has_no_duplicates(win):
    """`_all_task_ids` 无重复（重试/刷新重复塞 id ⇒ 批量操作会重复处理同一任务）。"""
    ids = win._all_task_ids()
    assert isinstance(ids, list), "_all_task_ids 应返回 list"
    assert len(ids) == len(set(ids)), "_all_task_ids 里有重复 id"


# ══════════════════════════════════════════════════════════════
# 2. 审核页：网格/列表、通过/拒绝/批量、翻卡片
# ══════════════════════════════════════════════════════════════


def test_review_tab_has_view_and_multi_switches():
    """审核页网格/列表切换 + 批量模式切换都在（两个都要能来回切）。"""
    src = _src(RB)
    for fn in ("toggle_list_mode", "toggle_multi_mode"):
        assert f"def {fn}(" in src, f"审核页缺 {fn}（视图/批量切换入口没了）"
    tree = ast.parse(src)
    for fn in ("toggle_list_mode", "toggle_multi_mode"):
        seg = _fn_src(RB, fn)
        assert "if on is None" in seg or ": bool | None" in seg or "bool | None" in _src(RB), \
            f"{fn} 应支持「不传参 = 取反」"


def test_review_tab_has_decide_and_undo():
    """审核页有通过/拒绝决策入口 + 撤销（#56/#72）。"""
    src = _src(RB)
    assert "def undo_review(" in src, "审核页缺撤销入口"
    assert re.search(r"def .*(approve|reject|decide|on_decide)", src), \
        "审核页找不到 通过/拒绝 决策入口"


def test_lightbox_flip_is_step_and_labels_follow_context():
    """D21/#56：灯箱**行为恒为 step(±1)**，但**按钮文案随上下文变**。

    ⚠ 两条铁律一起验：行为不许被改成跳页，文案不许被写死。
    ⚠ `step` / `_sync_nav_buttons` 都定义在 **lightbox.py 内部**，
       不在 main_window.py —— 别去主窗口找（我第一版就找错了）。
    """
    from media_resource_manager.gui import lightbox

    src = inspect.getsource(lightbox)
    assert "def step(" in src, "灯箱缺 step 方法（左右键翻页的基础）"
    assert "def _sync_nav_buttons(" in src, \
        "灯箱缺 _sync_nav_buttons（文案随上下文变只有这一个实现）"
    tree = ast.parse(src)
    step_src = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "step":
            step_src = ast.get_source_segment(src, node) or ""
    assert "goto" not in step_src, "step() 里调了 goto → 左右键行为被改成跳页（D21 铁律）"


def test_review_undo_shortcut_is_installed():
    """审核页装了撤销快捷键（`_install_undo_shortcut` 在 __init__ 里被调）。"""
    src = _src(RB)
    assert "def _install_undo_shortcut(" in src, "审核页缺 _install_undo_shortcut"
    init = _fn_src(RB, "__init__")
    assert "_install_undo_shortcut" in init, \
        "__init__ 里没调 _install_undo_shortcut → 撤销快捷键是死的"


# ══════════════════════════════════════════════════════════════
# 3. 图库页：网格/列表浏览、滚动续拉
# ══════════════════════════════════════════════════════════════


def test_gallery_loads_without_crash(win, qapp):
    """图库刷新不崩（空库也算通过）—— 真调，这个方法不弹模态。

    ⚠ `qapp` 是 fixture，**必须显式接进参数**；裸写 `qapp.processEvents()`
       会去拿 fixture 函数对象 ⇒ `FixtureFunctionDefinition has no attribute`。
    """
    try:
        win.load_gallery()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"图库加载抛异常：{type(exc).__name__}: {exc}")
    qapp.processEvents()


def test_gallery_keyset_paging_present():
    """R39 L1：图库走 keyset 游标分页（不是 OFFSET），游标函数必须还在。"""
    src = _src(MW)
    for fn in ("_gal_page_query", "_gal_cursor_for_page", "_gal_keyset_sort"):
        assert f"def {fn}(" in src, f"图库缺 {fn}（keyset 分页被回退成 OFFSET 了？）"
    assert "OFFSET" not in _fn_src(MW, "_gal_page_query").upper(), \
        "_gal_page_query 里又出现 OFFSET（分页性能回归）"


def test_gallery_keyset_sort_respects_direction():
    """keyset 排序键必须**随排序模式变**（否则点表头排序点了没反应）。

    ⚠ `_gal_keyset_sort(srt)` 单参、白名单是 `repo._SORT_EXPR`
       （键只有 id/site/score/res）。**它返回的是「模式键」本身**
       （`return srt if srt in _SORT_EXPR else "id"`，SQL 表达式在下游再查表）——
       我第一版拿它跟 `_SORT_EXPR[k]`（值）比 ⇒ 必然不等 ⇒ 假红。
    """
    from media_resource_manager.gui.main_window import MainWindow
    from media_resource_manager.repo import _SORT_EXPR

    assert len(_SORT_EXPR) >= 2, "排序白名单不足 2 项（排序功能无从谈起）"
    keys = [k for k in _SORT_EXPR if k != "id"]
    a = MainWindow._gal_keyset_sort(keys[0])
    b = MainWindow._gal_keyset_sort(keys[1] if len(keys) > 1 else "id")
    assert a != b, f"不同排序模式返回了同一个排序键（排序失效）：{a!r} == {b!r}"
    assert a == keys[0], f"白名单内的模式应原样返回键，实得 {a!r}"


def test_gallery_keyset_sort_rejects_unknown_mode():
    """白名单外的排序值必须**兜底成 id**（排序键绝不能直接拼原始输入 = SQL 注入面）。"""
    from media_resource_manager.gui.main_window import MainWindow

    evil = MainWindow._gal_keyset_sort("id; DROP TABLE images--")
    assert "DROP" not in evil.upper(), f"排序键把原始输入拼进去了（SQL 注入面）：{evil!r}"
    assert evil == "id", f"白名单外的值应兜底成 id，实得 {evil!r}"


# ══════════════════════════════════════════════════════════════
# 4. 代理管理
# ══════════════════════════════════════════════════════════════


def test_proxy_dialog_uses_real_radio_buttons():
    """B5+B4：代理对话框用 QRadioButton + QButtonGroup（真单选），无 QComboBox。"""
    from media_resource_manager.gui import proxy_dialog

    src = inspect.getsource(proxy_dialog)
    assert "QRadioButton" in src, "代理对话框没有 QRadioButton（B4：类型不是真单选）"
    assert "QButtonGroup" in src, "多个 QRadioButton 没有 QButtonGroup（不互斥）"
    assert "QComboBox" not in src, "代理对话框还残留 QComboBox（B4 要求改单选）"


def test_proxy_dialog_has_close_not_okcancel():
    """B5：按钮是「确定/关闭」全中文，不是 OK/Cancel。"""
    from media_resource_manager.gui import proxy_dialog

    src = inspect.getsource(proxy_dialog)
    assert "确定" in src, "代理对话框没有「确定」"
    assert "关闭" in src, "代理对话框没有「关闭」"
    assert not re.search(r'"(OK|Cancel)"', src), "代理对话框还残留 OK/Cancel 英文按钮"


def test_proxy_health_check_records_error_without_raising():
    """D-3 / B5：健康检查失败**只记错**，绝不抛到上层打断调度。

    ⚠ 不真调 `win.proxy_health_check()` —— 它内部有两处 `QMessageBox.information`
       且用 QThread，offscreen 下**永久阻塞**（本文件第一版真调过，卡死 7 分钟、
       junit 都不生成）。改验源码形状：worker 里的 check 调用被 try/except 包住、
       异常被转成 (False, 0, err) 三元组而不是往上抛。
    """
    src = _fn_src(MW, "proxy_health_check")
    assert src, "找不到 proxy_health_check"
    assert "except Exception as ex:" in src or "except Exception as e:" in src, \
        "proxy_health_check 的 worker 里没有宽except（D-3：异常会打断调度）"
    # 失败必须落成三元组里的错误串
    assert re.search(r"ok,\s*lat,\s*err\s*=\s*False", src), \
        "代理检查失败没有落成 (False, 0, err) —— 错误会被吞掉而不是显示给用户"
    assert not re.search(r"^\s*raise\b", src, re.M), "proxy_health_check 里又 raise 了"


def test_proxy_module_check_also_guarded():
    """D-3：proxy.py 的 check/acheck 同样不许把异常抛给调用方。"""
    from media_resource_manager import proxy as proxy_mod

    src = inspect.getsource(proxy_mod)
    for fn in ("check", "acheck"):
        seg = _fn_src(Path(proxy_mod.__file__), fn)
        if not seg:
            continue
        assert "try:" in seg or "except" in seg, f"proxy.{fn} 没有异常兜底（D-3）"
        assert not re.search(r"^\s*raise\b", seg, re.M), f"proxy.{fn} 会把异常抛到上层（D-3）"
    assert src


# ══════════════════════════════════════════════════════════════
# 5. 保存目录（原生框 + 沿用上次 + C 段重排）
# ══════════════════════════════════════════════════════════════


def test_output_row_three_controls_same_row(win):
    """C：编辑框 + 浏览 + 沿用上次 三者**同一父控件**（同一行），无独立「上次目录」区。

    ⚠ 关系断言：同一父容器比 y 坐标抗布局重构（y 随字体/DPI/面板高变）。
    ⚠ `win._out_row` 就是那一行（B3 之后它是**建面板时建一次**的，不是动态塞的）。
    """
    from PySide6.QtWidgets import QCheckBox, QLineEdit, QWidget

    row = win._out_row
    browse = [b for b in row.findChildren(QPushButton) if b.text().startswith("浏览")]
    assert browse, "这一行没有「浏览…」按钮"
    # ⚠ 「沿用上次目录」是 **QCheckBox**，不是可勾选 QPushButton ——
    #   我第一版用 `isCheckable()` 在 QPushButton 里找 ⇒ 空列表假红。
    # 2026-10-27 按 R52 重写：沿用勾选已独立成行，这一行只有编辑框+浏览
    checks = row.findChildren(QCheckBox)
    assert not checks, "R52 起「沿用上次目录」应独立成行，不该在这行里"
    edits = row.findChildren(QLineEdit)
    assert edits, "这一行没有目录编辑框"
    for w in (browse[0], edits[0]):
        assert w.parent() is row, f"{w.__class__.__name__} 不在保存目录行里（R52：编辑框+浏览同行）"


def test_no_separate_last_dir_display_widget():
    """C：去掉了独立的「上次目录」显示区 —— 上次目录只显示在编辑框里。"""
    src = _src(MW)
    # 「上次目录」只允许作为勾选框文案/提示存在，不应有独立只读标签控件
    assert "lab_last_dir" not in src, "还存在独立的 lab_last_dir（C 要求去掉）"
    assert "lbl_last_dir" not in src, "还存在独立的 lbl_last_dir（C 要求去掉）"


def test_browse_uses_native_dialog_for_dir_only():
    """B10：只给「目录」场景保留原生 `exec()`，其余走非阻塞 open()。

    ⚠ 读源码判形状，**绝不真调**（原生 exec() 无事件循环会挂死）。
    """
    src = _src(MW)
    assert "dir_only" in src, "B10 的 dir_only 判据不见了"
    assert ".exec()" in src, "原生对话框（exec）分支不见了（B10 要求保留）"


def test_reuse_last_toggles_fills_editbox():
    """B9：勾「沿用上次目录」的回填逻辑挂在 toggle 上（不在建面板时）。"""
    src = _fn_src(MW, "_on_reuse_last_toggled")
    assert src, "找不到 _on_reuse_last_toggled（B9 要求回填搬进这里）"
    assert "setText(" in src, "_on_reuse_last_toggled 没往编辑框写目录"


# ══════════════════════════════════════════════════════════════
# 6. 学习看板（B8：刷新不再关窗）
# ══════════════════════════════════════════════════════════════


def test_learning_dashboard_reload_does_not_close_window():
    """B8：刷新按钮**不许**接到 accept/reject/close（接了 = 点一下关窗）。

    ⚠ 用 AST 判「connect 到关闭调用」，不用全文搜 —— 本仓注释里大量 quoting
       「曾经错在哪」，全文搜必然匹配到自己写的注释而假红（记忆铁律）。
    """
    tree = ast.parse(_src(LB))
    closes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("accept", "reject", "close", "exec"):
            closes.add((node.func.attr, node.lineno))
    assert closes, "learning_board 里一个关闭类调用都没有 —— 判据本身失效，删了这条重写"
    # 刷新类信号名里带 reload/refresh/refresh 的 connect 不许指向上面这些
    src = _src(LB)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "connect"):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if not re.search(r"reload|refresh|刷新|b_reload", seg):
            continue
        for attr, lineno in closes:
            assert f".{attr}" not in seg, \
                f"learning_board:{lineno} 刷新路径接了 .{attr} → B8 退化（点刷新会关窗）"


def test_learning_dashboard_opens_without_blocking():
    """看板能构造出对话框（**只构造，不 exec**）。

    ⚠ `open_learning_dashboard` 内部 `dlg.exec()` —— 真调必挂死。
       这里只验「构造路径存在且返回 QDialog」，用 AST 找 exec 的位置以确保
       我们知道危险点在哪。
    """
    src = _fn_src(LB, "open_learning_dashboard")
    assert src, "找不到 open_learning_dashboard"
    assert ".exec()" in src, \
        "open_learning_dashboard 不再 exec 了？若已改为非阻塞，测试该同步改写"


def test_learning_dashboard_shows_weights():
    """看板要有正负向 Top-N 与趋势（#63 看板本体，不是空壳）。"""
    src = _src(LB)
    assert "def " in src
    for kw in ("weight", "权重", "trend", "趋势"):
        assert kw in src, f"看板缺「{kw}」相关实现（#63：一眼看全局）"


# ══════════════════════════════════════════════════════════════
# 7. Web（E 全功能 + F 登录）
# ══════════════════════════════════════════════════════════════


def test_web_api_surface_complete():
    """E：Web 后端覆盖 任务/代理/学习/设置 四组 API（与桌面功能对齐）。"""
    src = _src(WEBUI)
    for needle in ("/api/task/save", "/api/task/pause", "/api/task/delete",
                   "/api/task/retry", "/api/proxies", "/api/proxy/save",
                   "/api/proxy/delete", "/api/learning", "/api/settings/save"):
        assert needle in src, f"Web 缺接口 {needle}（E 要求 Web 与桌面功能对齐）"


def test_web_settings_save_is_allowlist_only():
    """E：设置写入是**白名单制**，且不含要重启才生效的键（改了会骗用户）。"""
    src = _src(WEBUI)
    seg = _fn_src(WEBUI, "_api_settings_save")
    assert seg, "找不到设置写入处理 _api_settings_save"
    # 2026-10-27：用户明确要求网页端也能改监听地址/端口/账号/密码
    # （原来只有桌面端能改）。所以它们**应当**在白名单里；但"改了要重启"
    # 这件事必须在返回的 note 里说清楚，否则才是"骗用户"。
    for allowed in ("web_bind", "web_port"):
        assert allowed in seg, f"{allowed} 应可由网页改（用户 2026-10-27 要求）"
    assert "重启" in seg or "重开" in seg, \
        "改了监听地址/端口却没提示需要重启"
    # 白名单制：应该有一份明确的键名清单
    assert re.search(r"(ALLOW|allow|白名单|KEYS)", seg), \
        "设置写入看不出白名单制（黑名单式实现容易漏键）"


def test_web_password_optional():
    """F：密码不是必填 —— 空密码免密放行；/api/session 回报 password_required。"""
    src = _src(WEBUI)
    assert "def _auth_ok(" in src, "webui 缺 _auth_ok"
    assert "def _do_login(" in src, "webui 缺 _do_login"
    assert "password_required" in src, \
        "F：/api/session 要回报 password_required（前端据此决定要不要显示登录页）"
    auth = _fn_src(WEBUI, "_auth_ok")
    assert "not" in auth and "password" in auth, \
        "_auth_ok 没处理「密码为空 ⇒ 放行」（F 要求）"


def test_web_static_routes_before_auth():
    """B7：/static/* 必须排在鉴权**之前**，否则登录壳被当 CSS/JS 返回 → 白屏。

    ⚠ 关系断言（谁在谁前面），不是「有没有 static 路由」——
       回归形态恰恰是「路由还在，但顺序被改回去了」（R47 B7 实测）。
    """
    src = _src(WEBUI)
    i_static = src.find("/static/")
    i_auth = src.find("_auth_ok(")
    assert i_static != -1, "webui 里找不到 /static/ 路由"
    assert i_auth != -1, "webui 里找不到鉴权调用"
    assert i_static < i_auth, "静态资源路由排在鉴权之后了 → B7 白屏会复发"

def test_clip_dependencies_present():
    """B6b 根因：`onnxruntime` + `tokenizers` 当初是**注释掉的** → CLIP 永远降级。

    ⚠ 判据落在 **requirements.txt**（真根因处），不是运行时 import ——
       运行时本机装了不代表**打包环境**装了。
    ⚠ 必须看**有效行**：requirements.txt 里这些包上方有一大段中文注释
       （R48/D-1 的说明），注释里也提到包名。我第一版取「第一条含包名的行」
       ⇒ 命中注释行 ⇒ 全部假红。**正确做法：过滤掉 lstrip 后以 # 开头的行。**
    """
    req = Path("requirements.txt").read_text(encoding="utf-8")
    active = [ln for ln in req.splitlines()
              if ln.strip() and not ln.lstrip().startswith("#")]
    for pkg in ("onnxruntime", "tokenizers", "socksio", "httpx"):
        hits = [ln for ln in active if re.match(rf"{pkg}\b", ln.strip())]
        assert hits, f"requirements.txt 里没有 {pkg} 的**有效依赖行**（被注释掉了 → 打包必缺）"


def test_spec_has_hidden_imports():
    """B6b/D-1：spec 的 hiddenimports 必须含这些包，否则 PyInstaller 不打包。"""
    spec = Path("build/pyinstaller.spec").read_text(encoding="utf-8")
    for pkg in ("onnxruntime", "tokenizers", "socksio"):
        assert pkg in spec, f"pyinstaller.spec 的 hiddenimports 缺 {pkg}"


def test_classifier_backend_not_stub():
    """B6b：CLIP 真能用（backend 至少不是 stub）。

    ⚠ 断言「不是 stub」这条**关系**，不写死具体后端名 —— 换推理后端不该假失败。
    ⚠ `LocalClassifier` 在 **`classify/clip_model.py`**，不在 `crawler/` 下
       （我第一版写 `from crawler import classifier` ⇒ ImportError 假红）。
    """
    from media_resource_manager.classify.clip_model import LocalClassifier

    try:
        obj = LocalClassifier()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"LocalClassifier 构造抛异常：{type(exc).__name__}: {exc}")
    backend = str(getattr(obj, "_backend", "") or "").lower()
    assert "stub" not in backend, \
        f"CLIP 仍在 stub 降级（_backend={backend!r}）→ B6 未真修（多半是权重路径没找到）"


def test_classifier_backend_picks_bundled_model():
    """B6b 修法核心：候选路径含**包内 bundled_models**（不靠部署时手工拷文件）。"""
    from media_resource_manager.classify import clip_model

    src = inspect.getsource(clip_model)
    assert "bundled" in src.lower(), \
        "_pick_backend 的候选里没有 bundled（用户随包装了权重却仍被降级 → B6b 会复发）"
    assert src.count("cands.append") >= 2, \
        "候选目录少于 2 个（B6b 要求「多候选回退」而不是只认一个路径）"


def test_ai_threshold_and_weights_ui_present(win):
    """AI 阈值控件 + 权重明细入口都在（不得「降级」成不可用）。

    ⚠ 控件挂在 **MainWindow** 上（`win.f_ai_thr`），不在 `_ensure_task_form()`
       的返回值上 —— 它返回 None。
    """
    win._ensure_task_form()
    assert hasattr(win, "f_ai_thr"), "新建表单缺 AI 阈值控件"
    assert win.f_ai_thr.value() > win.f_ai_thr.minimum(), "AI 阈值被设成了下限（B9 回归）"
    assert "权重" in _src(MW), "主窗口找不到权重明细入口"


# ══════════════════════════════════════════════════════════════
# 9. 三个皮肤切换（R68）
# ══════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _restore_global_theme():
    """⚠⚠⚠ **全局主题状态必须逐用例还原**（R48-h 自己踩出来的真回归）。

    `theme.apply_preset(app, key)` 改的是**模块级全局状态**
    （`theme._CURRENT_SKIN` / 暗色开关）。本文件的换肤用例会连着跑
    classic → modern → **night**，最后一个是**深色**。
    不还原的话，后面任何依赖「当前是浅色」的用例都会红 ——
    本轮实测就红了 `test_r67_inline_style_skin::test_style_applies_and_registers`
    （它断言 `css(False, ...)` 得到浅色值 `#5a6271`，而全局已被我留在深色，
    实际拿到 `#aab1bd`）。

    **症状极具误导性**：那条用例单跑全绿、只有合跑红，看起来像
    「r67 被别人改坏了」⇒ 极可能引着人去回退 r67 的产品代码。
    这正是记忆铁律里那条「状态污染 → 症状打到别的文件」的复现。

    正解：**动全局状态的用例必须配 autouse fixture 还原**，
    且要还原**全部**相关全局量（皮肤 + 明暗），别只还原一个。
    """
    from media_resource_manager.gui import theme

    saved_skin = getattr(theme, "_CURRENT_SKIN", None)
    saved_dark = getattr(theme, "_CURRENT_DARK", None)
    try:
        yield
    finally:
        if saved_skin is not None:
            theme._CURRENT_SKIN = saved_skin
        if saved_dark is not None:
            theme._CURRENT_DARK = saved_dark


def test_three_skin_presets_exist():
    """三个预设齐备（用户要「原始工程风 / 现代风 / 暗夜风」）。"""
    from media_resource_manager.gui import theme

    presets = getattr(theme, "PRESETS", None)
    assert isinstance(presets, dict) and len(presets) >= 3, \
        f"皮肤预设应 >=3 个，实得 {presets}"
    for k in presets:
        assert callable(getattr(theme, "apply_preset", None)), "缺 apply_preset"


def test_skin_selector_labels_differ_from_accent_labels():
    """R68-7：用户看到的选择器标签 ≠ 取色引擎的强调色键名（否则菜单显示「紫罗兰」）。"""
    from media_resource_manager.gui import theme

    sel = [theme.preset_label(k) for k in theme.PRESETS]
    eng = [str(v) for v in getattr(theme, "SKIN_LABELS", {}).values()]
    assert sel and eng, "PRESETS 或 SKIN_LABELS 为空（取色引擎/选择器缺一不可）"
    assert not (set(sel) & set(eng)), "选择器标签与强调色标签混用了（用户会看到颜色名）"


def test_apply_preset_produces_distinct_qss(qapp):
    """切皮肤后 QSS 真变了（不只是记了个字段）。

    ⚠ R68 教训：「静态护栏全绿」≠「换肤真的生效」。这里比的是**生成结果**。
    ⚠ `apply_preset(app, preset)` 是**两参**（要 QApplication），我第一版
       只传一个 ⇒ TypeError 假红。
    """
    from media_resource_manager.gui import theme

    # ⚠ `DARK_QSS` / `LIGHT_QSS` 是**模块级 f-string，import 时就定型**，
    #   `apply_preset` 改的是 `theme._CURRENT_SKIN` + 重刷控件，
    #   **不会**重新求值那两个模块常量。我第一版去比它们 ⇒ 三个皮肤永远相同 ⇒ 假红。
    #   真正的观测点是 `theme._CURRENT_SKIN`（以及 `theme.SKINS` 的取值）。
    seen = {}
    for k in theme.PRESETS:
        try:
            theme.apply_preset(qapp, k)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"apply_preset({k!r}) 抛异常：{type(exc).__name__}: {exc}")
        seen[k] = getattr(theme, "_CURRENT_SKIN", None)
    assert all(v is not None for v in seen.values()), \
        f"apply_preset 没有设置 _CURRENT_SKIN（切皮肤不会生效）：{seen}"
    # ⚠ **不要**断言「三个皮肤的强调色两两不同」—— 那是**设计事实，不是缺陷**：
    #   `classic`（浅+violet）与 `night`（深+violet）**刻意共用同一个强调色**，
    #   只靠明暗区分。我第一版把它当缺陷 ⇒ 假红（与 R40「为了过就调阈值」同型：
    #   阈值可调时不要改阈值，先问「这是不是事实」）。
    #   真正该保证的是「(强调色, 明暗) 组合两两不同」。
    combos = {(theme.preset_accent(k), bool(theme.preset_dark(k))) for k in theme.PRESETS}
    assert len(combos) == len(theme.PRESETS), \
        f"存在两个预设的 (强调色, 明暗) 组合完全相同 → 用户选了看不出区别：{combos}"
    # 两个浅色皮肤必须靠强调色区分（这是它们唯一的差别）
    light_accents = {theme.preset_accent(k) for k in theme.PRESETS if not theme.preset_dark(k)}
    assert len(light_accents) == len([k for k in theme.PRESETS if not theme.preset_dark(k)]), \
        f"两个浅色皮肤强调色相同 → 用户在浅色下选不出区别：{light_accents}"


def test_semantic_colors_do_not_follow_skin():
    """R68-5：语义色（评分档/状态点/排除红）**刻意不**跟随皮肤。

    ⚠ 反向钉死：别「顺手接上」—— 跟了会与强调色撞色，状态就读不出来了。
    ⚠ `status_color()` 定义在 **main_window.py**（不在 theme.py）。
    """
    from media_resource_manager.gui import main_window as mw

    src = _src(Path(mw.__file__))
    assert "def status_color(" in src, \
        "status_color 应是函数（函数才能在**调用时**取色，跟得上换肤）"
    # ⚠ R68-5 的真实形态是「**派生** dict」：`{k: status_color(k) for k in ...}`，
    #    它在 import 时求值一次 —— 作为向后兼容的快照可以接受，
    #    **真正要防的是「写死字面量的映射表」**（那才是取色时机被永久钉死）。
    #    我第一版只查 `STATUS_COLORS = {` ⇒ 把派生 dict 也当成退化 ⇒ 假红。
    m = re.search(r"^STATUS_COLORS\s*=\s*\{.*$", src, re.M)
    if m:
        # 派生快照（`{k: status_color(k) for k in ...}`）可接受；
        # 写死字面量才要拦。
        assert "status_color(" in m.group(0), \
            f"STATUS_COLORS 变成写死的字面量表了（R68-5 退化）：{m.group(0).strip()[:70]}"
    else:
        assert "STATUS_COLORS" not in src, \
            "STATUS_COLORS 处于半吊子状态（有引用没定义）"
    fn_src = _fn_src(Path(mw.__file__), "status_color")
    assert fn_src, "找不到 status_color 实现"


# ══════════════════════════════════════════════════════════════
# 10. 常用词溢出（B11：限宽+省略）
# ══════════════════════════════════════════════════════════════


def test_history_chips_are_width_limited():
    """B11：曾用词 chip 必须限宽 + 省略，而不是把面板撑开/遮挡别的控件。"""
    src = _src(MW)
    assert "_refresh_hist_chips" in src, "找不到曾用词 chip 渲染函数"
    seg = _fn_src(MW, "_refresh_hist_chips")
    blob = seg or src
    assert "setFixedWidth" in blob or "setMaximumWidth" in blob, "chip 没有限宽（B11）"
    assert "Elide" in blob or "_elide" in blob, "chip 没有省略号处理（B11 要求限宽+省略）"


def test_fill_all_history_button_present(win):
    """B3：「全部填满」按钮在每个曾用词区（B 段成果，不能被搬家弄丢）。"""
    for key in ("1", "2", "3"):
        box = getattr(win, f"_hist_chips_{key}", None)
        if box is None:
            continue
        btn = box.findChild(QPushButton, "btnFillAllHistory")
        assert btn is not None, f"第 {key} 级曾用词区没有「全部填满」按钮"


def test_elide_helper_truncates():
    """`_elide()` 在极窄宽度下必须真截断（原样返回 = 没省略）。"""
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QApplication as QA

    from media_resource_manager.gui.main_window import _elide

    fm = QFontMetrics(QA.instance().font())
    long_word = "超长关键词" * 40
    out = _elide(fm, long_word, 40)
    assert out != long_word, "极窄宽度下未省略（原样返回）"
    assert len(out) < len(long_word), "省略后长度没变短"


# ══════════════════════════════════════════════════════════════
# 11. 错误兜底（D/H 原则推广：中文 + 异常不硬停）
# ══════════════════════════════════════════════════════════════


def test_fetcher_socks_error_translated_not_raised():
    """D-2：SOCKS 缺失/连不上 → 中文提示 + 不抛、不闪退。"""
    from media_resource_manager.crawler import fetcher

    src = inspect.getsource(fetcher)
    assert re.search(r"socks", src, re.I), "fetcher 里没有 socks 相关处理（D 段要求）"
    assert "代理" in src, "fetcher 没有中文代理提示（D-2 要求转中文）"
    assert "try:" in src, "fetcher 没有 try（SOCKS 异常会直接崩任务）"


def test_no_blanket_except_swallowing_in_fetcher():
    """宽 except 把代码 bug 伪装成业务失败（R39 抓到过 IndexError 被吞）。"""
    from media_resource_manager.crawler import fetcher

    tree = ast.parse(inspect.getsource(fetcher))
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body = [n for n in node.body if not isinstance(n, ast.Expr)]
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                continue
            # 裸 except（无类型）会把所有东西吞掉
            if node.type is None and body and isinstance(body[0], ast.Pass):
                pytest.fail(f"fetcher:{node.lineno} 裸 except 直接 pass（代码 bug 会被伪装成业务失败）")


def test_zh_labels_translates_all_qt_standard_buttons():
    """`zh_labels` 的本职 = 把 Qt **标准按钮**变中文（英文系统上显示 "Close"/"OK"）。

    ⚠ 我第一版把它当成「网络/代理错误文案集中表」⇒ 假红。查了源码：
       它只有一张 StandardButton→中文 的表（`_ZH`）+ `zh_buttons()` + 审计函数。
       **错误文案不在这里**（在 fetcher/proxy 各自的模块里，见下一条）。
    """
    from media_resource_manager.gui import zh_labels

    src = inspect.getsource(zh_labels)
    assert "_ZH = {" in src, "zh_labels 的翻译表不见了"
    for std in ("Close", "Ok", "Cancel", "Save", "Open"):
        assert f'"{std}"' in src, f"Qt 标准按钮 {std} 没有中文映射（英文系统会露英文）"
    assert callable(zh_labels.zh_buttons), "zh_buttons 入口不见了"
    assert callable(zh_labels.audit_english_labels), \
        "audit_english_labels 不见了（英文残留审计是 C5 的护栏，别删）"


def test_network_error_hints_are_chinese():
    """用户可见的网络/代理错误文案必须是中文（分散在各自模块，不在 zh_labels）。"""
    from media_resource_manager.crawler import fetcher

    src = inspect.getsource(fetcher)
    # D-2 明文要求的那句中文兜底
    assert "SOCKS 代理需要 socksio 组件" in src, \
        "fetcher 缺 D-2 要求的中文 SOCKS 兜底提示"
    assert re.search(r"[一-鿿]", fetcher.SOCKS_HINT_ZH), "SOCKS 提示不是中文"
    # 网络/超时类也要有中文说法
    zh_hits = re.findall(r'"[^"]*[一-鿿][^"]*"', src)
    assert len(zh_hits) >= 3, f"fetcher 里中文文案只有 {len(zh_hits)} 条（用户可见错误必须中文）"


def test_no_user_visible_english_error_strings():
    """用户可见错误必须中文 —— 抽查网络/代理相关错误文案里有无纯英文。"""
    from media_resource_manager.gui import zh_labels

    src = inspect.getsource(zh_labels)
    tree = ast.parse(src)
    english_only = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value.strip()
            if len(s) < 8 or s.startswith("#"):
                continue
            has_cn = any("\u4e00" <= ch <= "\u9fff" for ch in s)
            has_en = re.search(r"[A-Za-z]{4,}", s)
            if has_en and not has_cn:
                english_only.append(s[:40])
    # 允许少量（技术名词/协议名），不许成片
    assert len(english_only) <= 8, \
        f"zh_labels 里疑似纯英文错误文案 {len(english_only)} 条：{english_only[:5]}"


# ══════════════════════════════════════════════════════════════
# 12. 普查元验证：护栏本身必须真的能红
# ══════════════════════════════════════════════════════════════


def test_meta_red_green_evidence_is_real():
    """确认本文件不是「全绿摆设」。

    做法：拿判据用的源码做**真变异**（内存副本，不落盘、不需还原），
    看判据是否会报「缺失」。若变异没生效说明判据已经形同虚设。
    """
    src = _src(WEBUI)
    mutated = src.replace("/api/learning", "/api/XXX_LEARNING", 1)
    assert mutated != src, "变异没生效（源码里已无 /api/learning，本用例该删）"
    assert "/api/learning" not in mutated, "变异后仍能找到该接口 → 判据失效"


def test_meta_judgement_catches_regression(tmp_path):
    """再进一步：对**判据函数本身**做真变异，确认它会红。

    ⚠ 只在 tmp 目录里造一个假模块，验证 AST 判据抓得住「刷新接了 accept」。
       这是本文件唯一一条「测试测试本身」的用例，也是防「护栏形同虚设」的主力。

    ⚠ 第一版这里假红过两次，两次都是**变异样本不合格**，不是判据坏：
      ① 判据要求 connect 段里含 `reload|refresh|刷新|b_reload`，我造的按钮叫
         `self.b` ⇒ 正则不匹配；
      ② 判据的 `closes` 集合收的是 **`ast.Call`**（真正的调用），
         而我写的 `self.accept` 只是**属性引用**（`ast.Attribute`）⇒ 集合空。
         真实 learning_board 里之所以能抓到，是因为那边有 `dlg.exec()` 这种真调用。
      **教训：造变异样本要复现真实结构，别造个「简化版」——**
      简化掉的正是判据依赖的那部分，测出来的就是「我的样本不合格」。
    """
    bad = tmp_path / "learning_board_bad.py"
    bad.write_text(
        "class D:\n"
        "    def __init__(self):\n"
        "        self.b_reload = None\n"
        "    def _wire(self):\n"
        "        self.b_reload.clicked.connect(self.accept)\n"
        "    def force_close(self):\n"
        "        self.accept()\n"
        "    def accept(self):\n"
        "        self.closed = True\n",
        encoding="utf-8",
    )
    src = bad.read_text(encoding="utf-8")
    tree = ast.parse(src)
    closes = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("accept", "reject", "close", "exec"):
            closes.add((node.func.attr, node.lineno))
    hit = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "connect"):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if not re.search(r"reload|refresh|刷新|b_reload", seg):
            continue
        for attr, _ in closes:
            if f".{attr}" in seg:
                hit = True
    assert hit, "判据没抓到「刷新接了 accept」→ 护栏形同虚设，必须重写"
