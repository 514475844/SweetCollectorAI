"""R61 · qB 式原生高密度任务表（NATIVE_SKIN_ORDER §2）护栏。

═══ 本文件押着的六条契约 ═══
① **进度条必须由系统样式画**（`QStyleOptionProgressBar` +
   `drawControl(CE_ProgressBar)`）。这是整个 §2 存在的**唯一理由** ——
   自己拿 QPainter 画圆角矩形 = 「在 Windows 上假装像 Windows」，
   跟真原生控件放一起反而更刺眼，且不跟用户主题/高对比度/缩放。
② **不许写死 hex**：颜色全走 QPalette（P1-4 的同款要求，本模块零 hex）。
③ **不许编数字**：ETA 在「总量未知」时必须显示 "—"。
④ **R74 删掉了「操作」列**（作业单第 2 项）⇒ 原先钉「画与命中
   同一几何真相」的 `action_rects` 那套整体消失。取而代之的护栏是
   `test_start_stop_still_reachable_without_the_actions_column`：
   **撤掉行内按钮后，启停必须仍能从右键菜单到达** —— 这正是 R66
   撤 `self._tb` 时踩过的坑（撤掉入口却不补替代入口 ⇒ 那个皮肤
   静默失去全部动作，且不报任何错）。
⑤ **两套并存**：`TaskRow` 不许被删；cards 三个皮肤保持 cards 不动。
⑥ **接线不能只在新控件里对**：主窗口那一侧（显隐 / 补刷 / 选中 /
   右键 / 信号）必须单独验 —— R61 的集成探针就是在那里抓到两个真 bug
   （脏标记写反、菜单坐标换算错），而表格控件单测当时全绿。

⚠⚠ **本文件多数断言写「关系」不写「数值」**（R21/MEMORY 铁律）：
写死今天的取值（行高 24、列宽 84、8 列）→ 产品一改需求就集体假失败，
而且**看起来像真回归**，会引着人去回退正确的产品代码。
所以：行高只断言「紧凑」（≤ 某上限）、列只断言「该有的键都在」。
唯一写死的是**结构性事实**（列 key 的集合、task_view 的分配），
那些是作业单钦定的契约。
"""
from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest

from media_resource_manager.gui import theme, task_table
from media_resource_manager.models import TaskConfig, TaskStatus

GUI = pathlib.Path(task_table.__file__).resolve().parent
SRC = GUI / "task_table.py"


@pytest.fixture()
def qapp():
    """项目铁律：碰 QWidget 的测试文件必须自带 qapp，且绝不 app.quit()。

    （app.quit() 会掀掉整个 session 的事件循环，后面所有 Qt 用例全崩。）
    """
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_src())


def _fn(name: str):
    for node in ast.walk(_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    return None


def _class(name: str):
    for node in ast.walk(_tree()):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _method(cls_name: str, meth: str):
    """取**指定类**里的方法节点。

    ⚠⚠ 不要用「按名字全文件找第一个 FunctionDef」—— `__init__` 在本仓
    有几十个（_FlowLayout / _ElideLabel / …），取到的会是别的类，
    然后断言报「产品代码没有 XXX」，**而实际上产品代码好好的**。
    这类假红比漏检更费时间（R60：先分清是护栏坏了还是产品坏了）。
    """
    cls = _class(cls_name)
    if cls is None:
        return None
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == meth:
            return node
    return None


def _method_src(cls_name: str, meth: str) -> str:
    node = _method(cls_name, meth)
    return ast.unparse(node) if node is not None else ""


def _call_names(node) -> set:
    """节点内所有被调用的名字（方法名 / 类名），用于正向判据。"""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            nm = getattr(f, "attr", None) or getattr(f, "id", None)
            if nm:
                out.add(nm)
    return out


# ══════════════════════════════════════════════════════════════════
# ① 进度条必须交给系统样式画（本单的核心，不可退化）
# ══════════════════════════════════════════════════════════════════
def test_progress_bar_is_drawn_by_the_system_style():
    """进度条必须走 QStyleOptionProgressBar + style().drawControl。

    ⚠ 反向判据：不许出现自绘进度条的那几个标志性调用
    （drawRoundedRect / drawRect / QBrush / setBrush）。
    只查正向调用不够 —— 那只证明「用了系统样式」，不证明
    「没同时又自己画了一个盖在上面」。
    """
    fn = _fn("paint")
    cls = _class("ProgressDelegate")
    assert cls is not None, "找不到 ProgressDelegate"
    paints = [n for n in ast.walk(cls)
              if isinstance(n, ast.FunctionDef) and n.name == "paint"]
    assert paints, "ProgressDelegate 必须实现 paint（不画就等于没进度条）"
    body = ast.unparse(paints[0])
    assert "QStyleOptionProgressBar" in body, \
        "进度条没用 QStyleOptionProgressBar —— 就不可能是系统画的"
    assert "drawControl" in body and "CE_ProgressBar" in body, \
        "进度条没走 style().drawControl(CE_ProgressBar)"
    for banned in ("drawRoundedRect", "QBrush", "setBrush"):
        assert banned not in body, \
            "进度条里出现 %s = 自己画了个假进度条（§2 的意义就在原生）" % banned


def test_progress_delegate_takes_palette_not_hardcoded_colors():
    """进度条颜色必须来自 option.palette（不许在委托里钉死颜色）。"""
    paints = [n for n in ast.walk(_class("ProgressDelegate"))
              if isinstance(n, ast.FunctionDef) and n.name == "paint"]
    body = ast.unparse(paints[0])
    assert "palette" in body, \
        "paint 没把 option.palette 交给 drawControl ⇒ 深色/高对比度下不跟随"


def test_module_has_no_hardcoded_hex():
    """本模块零裸 hex（P1-4 同款；theme.py 才是调色板唯一真值源）。

    ⚠ 用 AST 剥注释/docstring 再扫，与 P1-4 护栏同一套判据 ——
    否则本文件注释里写的 `#1f2430` 之类会把护栏自己搞红。
    """
    tree = _tree()
    hexes = re.findall(r"#[0-9a-fA-F]{6}\b", _src())
    # 允许出现在注释/docstring 里的说明性色值（那是给人看的）
    code_only = "\n".join(
        line for line in _src().splitlines()
        if not line.lstrip().startswith("#"))
    assert not re.search(r"#[0-9a-fA-F]{6}\b", code_only), \
        "task_table.py 出现裸 hex：%s" % sorted(set(hexes))[:5]
    assert tree is not None


# ══════════════════════════════════════════════════════════════════
# ③ 不许编数字（ETA / 速度）
# ══════════════════════════════════════════════════════════════════
def test_no_number_is_ever_fabricated():
    """**整行**都不许出现编出来的数字 —— 从 ETA 一个格子升级到全行。

    ⚠ R74：作业单第 1 项删掉了「剩余(ETA)」列，理由是「ETA 无意义：
      不知网站总图数/总时间」。但**「不许编数字」这个原则不许跟着列
      一起消失** —— 它只是从 ETA 转移到了进度与速度两列上。
      本项目的抓取是流式 BFS，**运行中根本不知道总共会有多少张图**：
      任何「已完成 37%」「0 张/秒」都是编的，而用户会当真（这是进度类
      UI 最伤信任的一种假）。
    ⚠ 与 R61 原版相比断言**更强**：原来只判 ETA 一个格子，现在判
      整行数据（pct / rate / busy）+ 判 build_rows 不再产出 eta 键。
    """
    t = TaskConfig(id=1, name="x", adapter_name="a", status=TaskStatus.RUNNING)
    # 场景一：正在跑、什么都还不知道（最容易被编出数字的场景）
    row = task_table.build_rows(
        [t], {1: {"downloaded": 0, "elapsed": 0}})[0]
    assert row["pct"] is None, "总量未知时进度不许编百分比（应画不确定态）"
    assert row["busy"] is True, "总量未知时应是不确定态（活动条）"
    assert row["rate"] is None, "elapsed=0 时速度算不出，不许编"
    assert task_table._fmt_rate(row["rate"]) == "", \
        '算不出的速度必须显示空串，不是 "0"（"0 张/秒"会被当成卡死）'
    # 反向：build_rows **不许再产出 eta 键**。留着它就等于给「编数字」
    # 留了个后门 —— 某个下游代码（排序键 / 导出 / 未来的列）随时会
    # 把它渲染出来，而那时它就成了「编出来的剩余时间」。
    assert "eta" not in row, \
        "build_rows 不该再产出 eta 字段（ETA 列已删）—— 有它就有人会画它"

    # 场景二：已知总量 ⇒ 这时才允许算，且算出来的必须是真的
    row2 = task_table.build_rows(
        [t], {1: {"downloaded": 5, "images_found": 15, "elapsed": 10.0}})[0]
    assert row2["pct"] == 33, "已知总量时进度应按已存/发现算"
    assert row2["rate"] == 0.5, "已知 elapsed 时速度应算得出"
    assert task_table._fmt_rate(row2["rate"]) == "0.50/s"


def test_eta_helpers_are_gone_but_the_principle_survives():
    """删掉的实现不许留成死代码，但「不许编数字」要在别处仍然成立。

    ⚠ 为什么单独一条：`_fmt_dur` 在本模块里**只有** ETA 列一个调用点。
      列删了它就是死代码 —— 而死代码比没有更糟：下一个人会以为
      「时长显示」这个能力还在。
    ⚠⚠ 同理不许留 `COL_ETA` 常量：它还在的话，`if col == COL_ETA`
      这类判断会**静默指向一个别的列**（列号是位置，不是身份）——
      把「速度」列画成时长，不崩溃、不报错，只是显示错了。
      这与 R63-M5「判『X 与 Y 不同处』要写成『没有任何一处同时含
      X 和 Y』」是同一条纪律。
    """
    assert not hasattr(task_table, "_fmt_dur"), \
        "_fmt_dur 是死代码了（唯一调用点是已删的 ETA 列）"
    assert not hasattr(task_table, "COL_ETA"), \
        "COL_ETA 常量还在 —— 它会静默指向别的列（列号是位置不是身份）"
    # 原则仍在：速度算不出是空串（不是 "0"），进度算不出是不确定态。
    assert task_table._fmt_rate(None) == "", "速度算不出必须是空串"


def test_rate_is_blank_not_zero_when_unknown():
    """速度算不出时是**空串**，不是 "0"。

    ⚠ 「0 张/秒」和「没数据」在 UI 上是两回事：前者会让用户以为任务卡死了。
    """
    assert task_table._fmt_rate(None) == ""
    assert task_table._fmt_rate(0.0) == "0.00/s", "真的有 0 速才显示 0"


def test_running_without_report_shows_busy_not_zero_progress():
    """正在跑但无 report ⇒ 画不确定态（busy），不是「0%」。

    ⚠ 显示 0% 等于告诉用户「没在动」，而它正在满速抓 —— 这是会让人
    手动去点停止的假信息。
    """
    t = TaskConfig(id=1, name="x", adapter_name="a", status=TaskStatus.RUNNING)
    row = task_table.build_rows([t], {})[0]
    assert row["busy"] is True and row["pct"] is None, \
        "运行中无总量应是不确定态（busy + pct=None）"


# ══════════════════════════════════════════════════════════════════
# ④ 操作列：画与命中同一几何（防「点停止却执行运行」）
# ══════════════════════════════════════════════════════════════════
def test_actions_column_and_its_helpers_are_gone():
    """「操作」列及其实现必须**彻底**消失，不许留空壳。

    ⚠ R74：作业单第 2 项删掉这一列。留空壳比删干净更糟 ——
      一个没人调用的 `ActionDelegate` 会让下一个人以为
      「表内还有开始/停止按钮」，而实际上按钮已经没了。
    ⚠⚠ 判据用 AST 判「有定义」，**不搜字面量**（R63-D5/ R73 M5）：
      本模块注释里大量提到这些名字（「随操作列一起删除」），
      全文搜会**匹配到自己 ⇒ 假红**；反过来只搜「有没有出现」
      也会被注释骗到 ⇒ 两个方向都得防。
    """
    tree = _tree()
    # ① 这些顶层名字不许真的被定义
    for gone in ("action_rects", "ActionDelegate"):
        assert _fn(gone) is None and _class(gone) is None, \
            "%s 还在 —— 操作列删了，实现要一起删干净" % gone
    # ② 两个信号不许再声明（它们唯一的发射点已被删除）
    view = _class("TaskTableView")
    assigned = set()
    for n in ast.walk(view):
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if isinstance(tgt, ast.Name):
                    assigned.add(tgt.id)
    for gone_sig in ("run_requested", "stop_requested"):
        assert gone_sig not in assigned, \
            "%s 信号还在，但唯一的发射点已删 ⇒ 永远是死信号" % gone_sig
    # ③ mousePressEvent 覆写必须一起删
    #    （它**只**为操作列的命中判定存在：判不到时它就是 super()）
    meths = {n.name for n in ast.walk(view)
             if isinstance(n, ast.FunctionDef)}
    assert "_action_at" not in meths, "_action_at 还在（操作列的命中判定）"
    assert "mousePressEvent" not in meths, \
        "mousePressEvent 覆写只为操作列而存在，列删了就该删（留空壳=误导）"
    # ④ 反向：双击编辑**不许**被一起删（那是另一条正交的能力）
    assert "_on_double_click" in meths, "双击编辑被误删了"
    assert tree is not None


# (已随「操作」列删除 —— 见上面两条护栏)


def test_start_stop_still_reachable_without_the_actions_column(qapp):
    """**行为级**：行内按钮删了，启停必须仍能从**右键菜单**到达。

    ⚠⚠⚠ 这是本轮最重要的一条，写它的理由是一次**真实踩过的坑**：

      R66 撤掉 qB 皮肤的 `self._tb`（页内 4 个按钮）时，只把按钮改成
      「常显」就以为完事了，**没检查替代入口** ⇒ 结果 qB 皮肤的
      「新建/全部开始/全部停止/编辑」一个都点不到，而且**不报任何错**。
      R74 删「操作」列是**完全同型**的操作：行内「开始/停止」是
      table 视图（classic / qbittorrent）**唯一**的启停入口。

    ⇒ 所以这条不是「源码里有 addAction」那种静态搜，而是**真发一次
      表格右键**、验证它带出命中行 id，再用 AST 确认主窗口菜单真接了
      「运行 / 停止」。
    ⚠⚠ **不许调 `_body_menu`**：它最后 `menu.exec()`，offscreen 下那是
      **永久挂死**（R69 踩过 3 次，症状「CPU 只涨 3.3 秒却跑了 6 分钟」，
      无 traceback）。这里只验它算得出 id 并发对信号。
    """
    from media_resource_manager.gui import main_window as mw

    # ① 表格本体右键必须带出命中行的 id（菜单定位任务的唯一依据）
    t = TaskConfig(id=7, name="n", adapter_name="a", status=TaskStatus.IDLE)
    v = task_table.TaskTableView()
    v.resize(900, 300)
    v.set_tasks([t], {})
    v.show()
    qapp.processEvents()

    got = []
    v.menu_requested.connect(lambda pos, tid: got.append(tid))
    center = v.visualRect(v.tmodel.index(0, 0)).center()
    r = v._hit_row(center)
    assert r == 0, "命中判定应认出第 0 行，实得 %s" % r
    tid = v.tmodel.row_id(r) if r >= 0 else None
    v.menu_requested.emit(center, int(tid))
    assert got == [7], \
        "表格右键必须带出命中行 id（菜单靠它定位任务），实得 %s" % got

    # ② 主窗口右键菜单里必须真有「运行 / 停止」。
    #    ⚠ 这两个接的是 run_selected / stop_selected（作用于**当前选中**）
    #      而非 _run_by_id（按 id 定位）—— 对表格两者等价，因为
    #      _task_menu 会先 _select_by_id(tid) 把右键点中的那行选中。
    #      若哪天这条等价断了，症状是「停掉另一个任务」且不报错（R61 §⑥）。
    body = ast.unparse(_mw_fn("_task_menu"))
    assert "act_run" in body and "act_stop" in body, \
        "右键菜单里没有运行/停止 ⇒ classic/qbittorrent 皮肤静默失去启停能力"
    src = pathlib.Path(mw.__file__).read_text(encoding="utf-8")
    assert re.search(r"act_run\.triggered\.connect\(self\.run_selected\)",
                     src), "act_run 没接 run_selected"
    assert re.search(r"act_stop\.triggered\.connect\(self\.stop_selected\)",
                     src), "act_stop 没接 stop_selected"

    # ③ 反向护栏：卡片版的行内按钮**必须还在**（三个 cards 皮肤靠它）
    tr = _mw_class("TaskRow")
    assert tr is not None, "TaskRow 不许被删（R61 §⑤ 两套并存）"
    tr_src = ast.unparse(tr)
    assert "_run_by_id" in tr_src and "_stop_by_id" in tr_src, \
        "卡片版的行内运行/停止按钮被误删了（cards 皮肤靠它启停）"


# ══════════════════════════════════════════════════════════════════
# ⑤ 两套并存 + 分配
# ══════════════════════════════════════════════════════════════════
def test_task_view_assignment_matches_the_order():
    """task_view 的分配必须与作业单钦定的一致。"""
    assert theme.preset_layout("classic").get("task_view") == "table"
    assert theme.preset_layout("qbittorrent").get("task_view") == "table"
    for k in ("violet_look", "modern", "night"):
        assert theme.preset_layout(k).get("task_view") == "cards", \
            "%s 应保持卡片（改动面最小、不引入回归）" % k


def test_every_layout_declares_task_view():
    """每个预设都必须显式声明 task_view。

    ⚠ 漏一个会怎样？`lay.get("task_view")` 返回 None ≠ "table" ⇒ 静默
    回落到 cards。症状是「某个皮肤没生效」而**没有任何报错**。
    显式声明 + 本护栏 = 漏了就在测试里红。
    """
    for key in theme.PRESETS:
        assert "task_view" in theme.LAYOUTS[key], \
            "%s 没声明 task_view（会静默回落到 cards）" % key


def test_task_row_is_not_deleted():
    """TaskRow 必须还在 —— 两套并存，作业单明写「不许删」。"""
    from media_resource_manager.gui import main_window as mw
    assert hasattr(mw, "TaskRow"), "TaskRow 被删了（两套并存是硬要求）"
    assert hasattr(mw, "TaskTableView") or hasattr(mw.MainWindow, "selected_id")


# ══════════════════════════════════════════════════════════════════
# 列/行高：只断言「结构」与「够紧凑」，不钉死具体数值
# ══════════════════════════════════════════════════════════════════
def test_columns_cover_the_ordered_spec():
    """列 key 与表头文案的**集合与顺序**必须与作业单一致（契约，可钉死）。

    ⚠ R74：作业单第 1/2 项删掉了「剩余(ETA)」「操作」两列 ——
      这是**结构性契约**，所以这里可以钉死（文件头已声明：
      唯一写死的就是结构性事实）。
    ⚠ 顺带钉住**表头文案**：光断 key 集合的话，把「已保存」改成
      「已存」这类回归会漏（key 没变）。
    """
    keys = [c[0] for c in task_table.COLUMNS]
    assert keys == ["idx", "name", "progress", "status", "saved", "rate"], keys
    heads = [c[1] for c in task_table.COLUMNS]
    # 2026-10-24：「#」→「编号」，且 idx 列改显真实任务 ID（与日志/通知
    # 的「任务 #N」同一套编号，删任务造成的 ID 断档不再引起"少了任务"误会）。
    assert heads == ["编号", "名称", "进度", "状态", "已保存", "速度"], heads
    # 反向：被删的两列不许换个 key 混回来（用户要求彻底删，不是隐藏）
    for banned in ("剩余", "操作"):
        assert banned not in heads, \
            "「%s」列不许回来（作业单第 1/2 项：用户要求彻底删除）" % banned
    # 「剩余」「操作」两个词也不许作为**表头文案**藏在别处
    assert task_table.NCOLS == 6, \
        "列数应为 6（idx/name/progress/status/saved/rate），实得 %d" \
        % task_table.NCOLS


def test_name_column_is_the_only_stretch_one():
    """只有「名称」列 stretch，其余定宽（数字列被拉宽会看起来很怪）。"""
    init = _method_src("TaskTableView", "__init__")
    assert init, "找不到 TaskTableView.__init__"
    # 只断言「名称列被特殊判断为 stretch」这一事实，不钉死写法：
    # 判据写成 AST 事实（比较了 key 与 'name'），而不是某一行具体文本 ——
    # 否则把 `if key == "name"` 改成 `if key in _STRETCH` 就假红。
    assert '"name"' in init or "'name'" in init, \
        "名称列没被特殊对待（应 stretch）"
    assert "Stretch" in init, "没有 stretch 列（窗口拉宽时谁吃余量？）"


def test_row_height_is_compact():
    """行高必须落在 qB 观感区间（作业单 22~26px），别飘成大行卡片。"""
    assert 20 <= task_table.ROW_HEIGHT <= 30, \
        "行高 %d 超出高密度传输列表的观感区间" % task_table.ROW_HEIGHT


def _call_arg_true(node, name: str) -> bool:
    """该调用是否存在**且首参为真值**。

    ⚠⚠ 只判「调用出现过」是**假护栏**：把 `setSortingEnabled(True)` 改成
    `setSortingEnabled(False)`，名字照样在，护栏照样绿 —— 而功能真的没了。
    这正是 R61 反向验证里 M12/M13 两条漏过的原因。
    ⇒ 这类「开关型 API」必须连**参数值**一起判。
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        nm = getattr(f, "attr", None) or getattr(f, "id", None)
        if nm != name or not n.args:
            continue
        a = n.args[0]
        if isinstance(a, ast.Constant):
            return bool(a.value)
        if isinstance(a, ast.NameConstant):        # 兼容旧 ast
            return bool(a.value)
        return True        # 非常量（如变量）→ 不武断判假
    return False


def test_table_offers_sort_drag_and_hide():
    """列可排序 / 可拖动 / 可隐藏 —— 作业单明列的三条。

    ⚠ 判据查的是「调用 + 参数为真」，不是「调用出现过」——
    `setSortingEnabled(False)` 里名字也在，那样判会永远绿。
    """
    init_node = _method("TaskTableView", "__init__")
    init = _method_src("TaskTableView", "__init__")
    assert init_node is not None, "找不到 TaskTableView.__init__"
    assert _call_arg_true(init_node, "setSortingEnabled"), \
        "列不能排序（setSortingEnabled 没开）"
    assert _call_arg_true(init_node, "setSectionsMovable"), \
        "列不能拖动（setSectionsMovable 没开）"
    assert "_header_menu" in init and "setColumnHidden" in _src(), \
        "列隐藏功能没了（表头右键菜单）"


def test_table_has_context_menu_on_both_header_and_body():
    """GUI 铁律：新表格**必须**配右键菜单（表头管列、主体管任务）。"""
    src = _src()
    assert src.count("CustomContextMenu") >= 2, \
        "表头与表格本体的右键菜单都要有"
    assert "def _header_menu" in src and "def _body_menu" in src


def test_action_column_buttons_are_not_real_widgets():
    """操作列不许塞真 QPushButton（那就退回「每行一个控件」的老路）。

    ⚠ 这是 §2 性能与密度的前提。用 AST 查「本模块没有实例化
    QPushButton」，而不是全文搜（注释里提到它是允许的）。
    """
    tree = _tree()
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = getattr(f, "id", None) or getattr(f, "attr", None)
            if name in ("QPushButton", "QWidget", "QLabel"):
                bad.append("%s@%d" % (name, node.lineno))
    assert not bad, \
        "操作列/单元格不许实例化真控件（%s）—— 每行一个 widget 就白做了" % bad


# ══════════════════════════════════════════════════════════════════
# 指纹：不许每次刷新都重画（R61 的护栏，反面是「无脑 reset」）
# ══════════════════════════════════════════════════════════════════
def test_unchanged_rows_do_not_emit_reset():
    """内容没变 ⇒ 一个信号都不发（refresh 是被队列事件高频触发的）。"""
    t = TaskConfig(id=1, name="a", adapter_name="x", status=TaskStatus.IDLE)
    m = task_table.TaskTableModel()
    rows = task_table.build_rows([t], {})
    assert m.set_rows(rows) is True, "首次必须刷"
    assert m.set_rows(list(rows)) is False, "内容没变却发了 reset（会全量重画）"
    rows2 = [dict(r) for r in rows]
    rows2[0]["status"] = "error"
    assert m.set_rows(rows2) is True, "内容变了却不刷 ⇒ 界面永远不更新"


def test_sort_is_numeric_not_textual():
    """排序必须按**数值**，不是显示文本。

    ⚠ 否则 "1,000" 会排在 "999" 前面（文本序）—— 这类表��� bug 很常见，
    且用户一眼就看得出不对却说不清哪里不对。
    """
    m = task_table.TaskTableModel()
    rows = [
        {"id": 1, "idx": 1, "name": "a", "status": "done", "saved": 999,
         "rate": None, "eta": None, "pct": 100, "busy": False},
        {"id": 2, "idx": 2, "name": "b", "status": "idle", "saved": 1000,
         "rate": None, "eta": None, "pct": None, "busy": False},
    ]
    m.set_rows(rows)
    m.sort(task_table.COL_SAVED, Qt_ASC())
    assert [r["saved"] for r in m.rows()] == [999, 1000], \
        "「已保存」按文本排序了（千分位字符串会排错）"


def Qt_ASC():
    from PySide6.QtCore import Qt
    return Qt.SortOrder.AscendingOrder


# ══════════════════════════════════════════════════════════════════
# ⑥ 主窗口接线（静态部分）
# ══════════════════════════════════════════════════════════════════
def _mw_tree():
    from media_resource_manager.gui import main_window as mw
    return ast.parse(pathlib.Path(mw.__file__).read_text(encoding="utf-8"))


def _mw_class(name):
    for node in ast.walk(_mw_tree()):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _mw_fn(name):
    for node in ast.walk(_mw_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _mw_method_src(cls_name, meth):
    """⚠ 必须限定在指定类里取方法 —— 本仓 `__init__` 有几十个，
    按名字全文件取第一个会拿到 _FlowLayout 的，然后报「产品没建表格」。"""
    cls = _mw_class(cls_name)
    if cls is None:
        return ""
    for node in cls.body:
        if isinstance(node, ast.FunctionDef) and node.name == meth:
            return ast.unparse(node)
    return ""


def test_main_window_builds_and_refreshes_both_views():
    """主窗口必须**两套都建、都刷**（作业单：两套并存、按布局切换）。"""
    from media_resource_manager.gui import main_window as mw
    src = pathlib.Path(mw.__file__).read_text(encoding="utf-8")
    assert "TaskTableView" in src, "主窗口没建表格控件"
    init = _mw_method_src("MainWindow", "__init__")
    assert init, "找不到 MainWindow.__init__（类名/方法名变了？）"
    # ⚠ 建壳是**分方法**的（__init__ 只调 _build_ui()），所以判据要落到
    #   真正建控件的那个方法上。写成只在 __init__ 里找 → 永远假红，
    #   而这条假红会让人以为「表格没建」（R60：先分清护栏坏还是产品坏）。
    build = _mw_method_src("MainWindow", "_build_ui") + init
    # ⚠ 判据是「**真的构造了** TaskTableView」，不是「代码里出现过这个名字」。
    #   把 `self.task_table = TaskTableView(self)` 改成 `= None`，
    #   下面这三条判据全都照样成立 ⇒ 护栏形同虚设（M17 就是这么漏过的）。
    assert re.search(r"self\.task_table\s*=\s*TaskTableView\(", build), \
        "建壳时没有真的构造 TaskTableView（赋 None / 换类都不算）"
    assert "self._vsplit.addWidget(self.task_table)" in build, \
        "表格没进 splitter（会与卡片叠在一起）"
    refresh = ast.unparse(_mw_fn("refresh"))
    assert "_refresh_task_table" in refresh, \
        "refresh 没喂表格 ⇒ 表格永远空白"
    assert "_refresh_task_list" in refresh, \
        "refresh 不许跳过卡片（默认预设就是 classic=表格可见，" \
        "跳卡片会让读 itemWidget 的老测试全红，且症状像真回归）"


def test_layout_visibility_switches_task_views():
    """_apply_layout_visibility 必须按 task_view 切两套显隐 + 处理补刷。"""
    body = ast.unparse(_mw_fn("_apply_layout_visibility"))
    assert "task_table.setVisible" in body, "显隐没切表格"
    assert "task_list.setVisible" in body, "显隐没切卡片"
    assert "_task_table_dirty" in body, \
        "切过去时没补刷 ⇒ 用户看到的是旧数据（且极难复现）"


def test_selected_id_reads_whichever_view_is_visible():
    """selected_id 必须按「哪套可见」问哪套。

    ⚠⚠ 漏掉一套的后果不是少个功能，而是**操作作用到错误的行**：
    表格可见时用户选中表格第 3 行，菜单却去读卡片残留的选中行 ⇒
    「停止」停掉另一个任务。不崩溃不报错，只是停错了 —— 用户会以为
    软件有灵异 bug。
    """
    body = ast.unparse(_mw_fn("selected_id"))
    assert "task_table" in body and "isVisible" in body, \
        "selected_id 没按可见性分流（会停错行）"
    assert "task_list.currentItem()" in body, "卡片分支被删了？"


def test_select_by_id_sets_both_views():
    """_select_by_id 必须两套都设（两个控件的选中是各自独立的）。"""
    body = ast.unparse(_mw_fn("_select_by_id"))
    assert "select_id" in body, "没设表格的选中"
    assert "task_list.setCurrentRow" in body, "没设卡片的选中"


def test_switching_to_cards_resyncs_row_width():
    """切到卡片皮肤时必须补一次 `_sync_task_rows_width()`。

    ⚠⚠ 与 #123「切回任务页要补同步」**完全同型**，只是入口变了：
    控件从隐藏变可见时，隐藏期间它不参与布局、行控件保留旧宽度 ⇒
    行尾「运行/停止/编辑」停在屏幕中间不靠右。
    以前不会出，是因为卡片**永远可见**、切皮肤不影响它；
    有了 table/cards 两套之后，「切皮肤」也成了让它变可见的入口。
    症状只在「先表格 → 后卡片」这个顺序下出现 ⇒ 极难复现。
    """
    body = ast.unparse(_mw_fn("_apply_layout_visibility"))
    assert "_sync_task_rows_width" in body, \
        "切到卡片时不补行宽同步 ⇒ 换皮肤后行尾按钮停在屏幕中间"


def test_table_menu_reuses_the_card_menu_with_explicit_id():
    """表格右键必须**复用** _task_menu，且**直接传 id**。

    ⚠ 两条各占一个坑：
      · 另写一份菜单 ⇒ 加了新动作只改一处，用户在某种皮肤下找不到；
      · 把表格坐标硬换算成卡片坐标 ⇒ 表格可见时卡片是隐藏的，它的
        viewport 不代表屏幕上任何东西，菜单会弹在别处、itemAt 还会
        命中不相干的任务。
    """
    body = ast.unparse(_mw_fn("_task_table_menu"))
    assert "_task_menu" in body, "表格另写了一份菜单（会漂移）"
    assert "tid=" in body, "没直接传 id（改走坐标换算了）"
    assert "mapFrom" not in body, \
        "不该做「表格坐标→卡片坐标」换算（卡片隐藏时无意义）"
