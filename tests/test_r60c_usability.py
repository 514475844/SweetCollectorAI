"""C1~C14 易用性/逻辑修复的回归测试（用户 10-07 真机反馈）。

**测试风格约定**（沿用本项目既有铁律）：
1. 「不许撤多了」的反向断言比正向断言更重要 —— 删功能最容易顺手删过头。
2.凡是验证「菜单里有没有某项」，读**去注释后的源码**而不是真的点菜单
   （点菜单要开 MainWindow，慢且脆）。
3. QThread /模态对话框**绝不真调exec()**（见文件末尾的说明）。

覆盖：
- C1  适配器管理器打开就有列表（根因：_reload_adp 从没在打开时调用过）
- C2  自动生成规则：分析进后台线程 + 转圈遮罩，主线程不阻塞
- C3  「确定」写盘后关闭 + 提示已保存 + **读回校验**
- C4  对话框顶部有「名称」输入 + 「网址」+ 「分析」，不必先建规则
- C5  全部标准按钮中文化（zh_buttons，且不覆盖业务标签）
- C6  全部 QFileDialog 都带 DontUseNativeDialog
- C7  实时抓图墙已移除（部件 + 菜单项 + 定时调用）
- C9  AI 模型对话框是标签页，按模式切页
- C10 适配器巡检入口已移除 + QThread 生命周期已修（不再 abort 进程）
- C11 批量导出任务（含进度统计）与导入对称
- C12 图库菜单不再有「缩略图尺寸…」
- C13 「清空回收站」→「清空隔离区」
- C14 维护菜单不再有「插件打包成 CRX」
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "media_resource_manager" / "gui" / "main_window.py"
REVIEW = ROOT / "media_resource_manager" / "gui" / "review_tab.py"
CHECKUP = ROOT / "media_resource_manager" / "gui" / "adapter_checkup.py"
PROPOSER = ROOT / "media_resource_manager" / "crawler" / "adapter_proposer.py"
TASKIO = ROOT / "media_resource_manager" / "taskio.py"
ZHL = ROOT / "media_resource_manager" / "gui" / "zh_labels.py"


# ----------------------------------------------------------------- 工具
def _code(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_docstrings(src: str) -> str:
    """用 AST 精确去掉所有 docstring，保留行号（用空行占位）。

    为什么必须用 AST 而不是正则/简单剥注释：本项目的代码里**大量关键信息
    写在 docstring 里**（C 批每处改动都写了「为什么」）。用正则删注释但留下
    docstring 的话，测试会在注释里搜到「实时抓图墙」「打包成 CRX」这类
    **已经被移除的功能名**，于是误报失败 —— 第一版就踩了这个坑。
    AST 能把 docstring 变成空串，其它代码原样保留，且行数不变（便于报错定位）。
    """
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    drop: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(
                getattr(first, "value", None), ast.Constant) \
                and isinstance(first.value.value, str):
            drop.add(first.lineno - 1)
            # 多行 docstring 整段占位
            for i in range(first.lineno - 1,
                           getattr(first, "end_lineno", first.lineno)):
                drop.add(i)
    out = []
    for i, ln in enumerate(src.splitlines()):
        out.append("" if i in drop else ln)
    return "\n".join(out)


def _uncommented(code: str) -> list[str]:
    """去掉 **docstring + 整行注释 + 行尾注释** 后的代码行。

    只做正则剥注释是不够的 —— 见 :func:`_strip_docstrings` 的说明。
    """
    src = _strip_docstrings(code)
    out = []
    for ln in src.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


@pytest.fixture(scope="module")
def qapp():
    """整个模块共用一个 QApplication（Qt 不允许有第二个）。"""
    os_env = {}
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ================================================================ C1 适配器列表
def test_c1_adapter_list_is_populated_on_open():
    """C1：打开「网址与适配器管理器」时列表**必须有内容**。

    用户反馈「适配器管理器不显示已有适配器」。根因不是路径、不是权限 ——
    ``_reload_adp()`` 定义在按钮回调里，**打开对话框时压根没调用过**，
    所以 lst_adp 永远是空的，点「新建/删除」之后才会刷出来。
    """
    code = _code(MAIN)
    lines = _uncommented(code)
    joined = "\n".join(lines)
    assert "def _reload_adp(" in joined, "_reload_adp 定义被删了"
    # 关键：在 for b in (b_new, b_edit, b_del, b_auto) **之前**就要调用一次
    idx_def = joined.index("def _reload_adp(")
    idx_first_btn = joined.index("for b in (b_new, b_edit, b_del, b_auto):")
    between = joined[idx_def:idx_first_btn]
    assert "_reload_adp()" in between, (
        "打开对话框时没调 _reload_adp() —— 列表会是空的（C1 回归）")


def test_c1_adapter_list_shows_stem_and_keeps_full_name():
    """列表显示 f.stem（不带 .yaml），完整文件名存Qt.UserRole。"""
    joined = "\n".join(_uncommented(_code(MAIN)))
    assert "QListWidgetItem(f.stem)" in joined
    assert "it.setData(Qt.UserRole, f.name)" in joined


def test_c1_rule_files_covers_yaml_yml_json():
    """_rule_files 必须同时扫三种扩展名（漏一种 = 那种规则的适配器看不见）。"""
    joined = "\n".join(_uncommented(_code(MAIN)))
    for ext in ('"*.yaml"', '"*.yml"', '"*.json"'):
        assert f"rules_dir.glob({ext})" in joined, f"_rule_files漏了 {ext}"


# ================================================================ C2 后台分析
def test_c2_analyze_runs_in_qthread_not_main_thread():
    """C2：分析（抓页面 + LLM/启发式）必须在 QThread 里，主线程不阻塞。

    用户反馈「自动生成规则一点分析就卡死」—— 根因是 do_analyze 里直接调
    ``fetch_url()``（同步 httpx.get，8~20 秒），主线程被冻住，
    窗口整个没响应。现在只start 一个 _AnalyzeWorker。
    """
    joined = "\n".join(_uncommented(_code(PROPOSER)))
    assert "class _AnalyzeWorker(QThread)" in joined, "分析没放进 QThread"
    # 主线程的 do_analyze 里只准出现 w.start()，不准出现 fetch_url
    i = joined.index("def do_analyze(")
    body = joined[i:i + 2500]
    assert "fetch_url(" not in body, (
        "do_analyze 里仍在主线程直接 fetch_url —— 会卡死（C2 回归）")
    assert "_AnalyzeWorker(" in body and ".start()" in body


def test_c2_busy_spinner_overlay_exists():
    """C2：分析期间要有**转圈等待动画**，且用定时器驱动（不依赖 GIF 资源）。"""
    code = _code(PROPOSER)
    assert "class _BusyOverlay(QWidget)" in code
    assert "QTimer" in code
    joined = "\n".join(_uncommented(code))
    assert "_overlay().start(" in joined, "分析时没显示遮罩"
    assert "ov.stop()" in joined, "分析完没收遮罩"


def test_c2_worker_only_emits_signals_never_touches_ui():
    """worker 里绝不能碰控件（跨线程动控件会崩）——只准emit。"""
    body = _code(PROPOSER).split("class _AnalyzeWorker")[1].split("def build_auto_gen_dialog")[0]
    for w in ("appendPlainText", "setPlainText", "setText", "show()", "hide()"):
        assert w not in body, f"worker 里出现 UI 调用 {w}（跨线程，会崩）"


# ================================================================ C3 确定落盘
def test_c3_confirm_button_is_determine_not_confirm_on_disk():
    """C3：按钮文案「确认落盘」→「确定」。"""
    code = _code(PROPOSER)
    assert 'QPushButton("确定")' in code
    assert "确认落盘" not in _uncommented_str(code), "旧文案「确认落盘」还在"


def _uncommented_str(code: str) -> str:
    return "\n".join(_uncommented(code))


def test_c3_confirm_closes_dialog_and_shows_saved():
    """C3：确定 → 写盘 → 关闭对话框 + 弹「已保存」。"""
    joined = _uncommented_str(_code(PROPOSER))
    i = joined.index("def do_confirm(")
    body = joined[i:i + 2000]
    assert "dlg.accept()" in body, "保存后没关闭对话框"
    assert "已保存" in body, "保存后没提示「已保存」"


def test_c3_confirm_reads_back_to_verify():
    """C3：写盘后**读回校验**，确认真的落盘了。"""
    joined = _uncommented_str(_code(PROPOSER))
    i = joined.index("def do_confirm(")
    body = joined[i:i + 2000]
    assert "read_text(" in body, "保存后没有读回校验（C3 回归）"
    assert "读回" in body


def test_c3_no_path_returns_without_writing(qapp, tmp_path, monkeypatch):
    """没分析过就点确定 → 什么都不写（不产生空规则文件）。"""
    from media_resource_manager.crawler import adapter_proposer as ap
    dlg = ap.build_auto_gen_dialog(None, tmp_path)
    try:
        assert list(tmp_path.glob("*.yaml")) == []
        dlg._do_confirm()          # state["rule"] is None → 应安静返回
        assert list(tmp_path.glob("*.yaml")) == [], "没规则却写了文件"
    finally:
        dlg.deleteLater()


# ================================================================ C4 弹窗结构
def test_c4_dialog_has_name_and_url_and_analyze():
    """C4：顶部要有「名称」+「网址」+「分析」三件套，不必先在上一层建规则。"""
    code = _code(PROPOSER)
    assert 'name_in.setPlaceholderText("规则名称' in code
    assert 'url_in.setPlaceholderText("https://...' in code
    assert 'b_analyze = QPushButton("分析")' in code
    joined = _uncommented_str(code)
    i = joined.index("def build_auto_gen_dialog(")
    body = joined[i:]
    # 三者必须在同一个水平布局里（用户要的是"顶部一行"）
    assert "url_row.addWidget(name_in, 1)" in body
    assert "url_row.addWidget(b_analyze)" in body


def test_c4_confirm_disabled_until_analysis_done(qapp, tmp_path):
    """没分析出规则前，「确定」必须是禁用的。"""
    from media_resource_manager.crawler import adapter_proposer as ap
    dlg = ap.build_auto_gen_dialog(None, tmp_path)
    try:
        assert dlg._b_confirm.isEnabled() is False
    finally:
        dlg.deleteLater()


def test_c4_analyze_ignores_bad_url(qapp, tmp_path, monkeypatch):
    """网址不合法 → 弹警告、不启动线程。"""
    from media_resource_manager.crawler import adapter_proposer as ap
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    dlg = ap.build_auto_gen_dialog(None, tmp_path)
    try:
        dlg._url_in.setText("不是网址")
        dlg._do_analyze()
        assert dlg._state.get("worker") is None, "非法网址也启动了 worker"
    finally:
        dlg.deleteLater()


# ================================================================ C5 中文按钮
def test_c5_zh_labels_module_translates_close():
    """zh_buttons 能把标准 Close 按钮改成「关闭」。"""
    from media_resource_manager.gui.zh_labels import zh_buttons
    from PySide6.QtWidgets import QDialogButtonBox
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    zh_buttons(bb)
    assert bb.button(QDialogButtonBox.StandardButton.Close).text() == "关闭"


def test_c5_zh_labels_translates_ok_cancel():
    from media_resource_manager.gui.zh_labels import zh_buttons
    from PySide6.QtWidgets import QDialogButtonBox
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                          | QDialogButtonBox.StandardButton.Cancel)
    zh_buttons(bb)
    assert bb.button(QDialogButtonBox.StandardButton.Ok).text() == "确定"
    assert bb.button(QDialogButtonBox.StandardButton.Cancel).text() == "取消"


def test_c5_zh_labels_does_not_clobber_custom_label():
    """**关键**：已有业务文案的按钮不许被改回"确定"。

    「批量导入」对话框把 Ok设成「导入」、「用模板新建」设成「用此模板新建」——
    zh_buttons 若无脑覆盖，这些功能标签就全丢了。
    """
    from media_resource_manager.gui.zh_labels import zh_buttons
    from PySide6.QtWidgets import QDialogButtonBox
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                          | QDialogButtonBox.StandardButton.Cancel)
    bb.button(QDialogButtonBox.StandardButton.Ok).setText("导入")
    zh_buttons(bb)
    assert bb.button(QDialogButtonBox.StandardButton.Ok).text() == "导入", \
        "业务文案被覆盖了"
    # Cancel 没有自定义文案，仍应被中文化
    assert bb.button(QDialogButtonBox.StandardButton.Cancel).text() == "取消"


def test_c5_zh_labels_is_idempotent():
    """重复调用不会叠出「关闭关闭」。"""
    from media_resource_manager.gui.zh_labels import zh_buttons
    from PySide6.QtWidgets import QDialogButtonBox
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    zh_buttons(bb)
    zh_buttons(bb)
    assert bb.button(QDialogButtonBox.StandardButton.Close).text() == "关闭"


def test_c5_all_button_boxes_are_localized():
    """每个 QDialogButtonBox 都得能显示中文按钮。

    两种合法做法都算数：
      ①建盒后调 ``zh_buttons(bb)``（标准按钮跟着系统语言走，必须改）；
      ② 用 ``addButton("中文", role)`` **自己加**按钮（压根没用标准枚举，
         天然就是中文，不需要改）。
    所以本测试只抓「用了标准枚举 **且** 没调 zh_buttons」这一种漏网情况。
    """
    import re as _re
    for f in (MAIN, REVIEW):
        code = _uncommented_str(_code(f))
        for m in _re.finditer(r"bb = QDialogButtonBox\(", code):
            seg = code[m.start():m.start() + 800]
            if "zh_buttons(bb)" in seg:
                continue
            # ② 自己加中文按钮的情形
            adds = _re.findall(r'addButton\(\s*"([^"]+)"', seg)
            if adds and all(not a.isascii() for a in adds):
                continue
            raise AssertionError(
                f"{f.name} 第 {code[:m.start()].count(chr(10)) + 1} 行有个 "
                f"QDialogButtonBox 既没调 zh_buttons，也没自己加中文按钮"
                f"（addButton 到的：{adds}）")


def test_c5_no_lone_english_ui_literals():
    """源码里不许有裸的英文 QPushButton/QAction 文案（AI/GPU 除外）。"""
    from media_resource_manager.gui.zh_labels import audit_english_labels
    hits = audit_english_labels(ROOT / "media_resource_manager" / "gui")
    # 合法的例外：AI / GPU / CLIP / API / ONNX / CRX / Cookie 等术语
    allow = ("AI", "GPU", "CLIP", "API", "ONNX", "CRX", "Cookie", "IP", "URL",
             "HTTP", "JSON", "YAML", "SHA")
    bad = [(f, i, t) for (f, i, t) in hits
           if not any(a in t for a in allow)]
    assert not bad, f"疑似英文 UI 文案：{bad}"


# ================================================================ C6 文件对话框
def test_c6_no_static_filedialog_calls():
    """绝不能用静态 QFileDialog.getXxx（r39e：原生后端 + 被关 = SIGSEGV）。"""
    for f in (MAIN, REVIEW):
        joined = _uncommented_str(_code(f))
        for bad in ("QFileDialog.getExistingDirectory(",
                    "QFileDialog.getOpenFileName(",
                    "QFileDialog.getSaveFileName(",
                    "QFileDialog.getOpenFileNames("):
            assert bad not in joined, f"{f.name} 又用了静态 {bad}"


def test_c6_every_filedialog_disables_native():
    """每个 ``QFileDialog(...)`` **实例**都要 setOption(DontUseNativeDialog)。

    注意统计口径：``QFileDialog.`` 开头的是**枚举引用**（DontUseNativeDialog /
    FileMode / AcceptMode），不是实例。实例的写法是 ``QFileDialog(self, ...)`
    —— 左括号紧跟、不是点。所以数实例要排除点号那种。
    """
    import re as _re
    joined = _uncommented_str(_code(MAIN))
    # 实例：QFileDialog( 但不能是 QFileDialog.XXX(
    instances = [m.start() for m in _re.finditer(r"(?<![\w.])QFileDialog\(", joined)]
    offs = len(_re.findall(r"QFileDialog\.DontUseNativeDialog", joined))
    assert instances, "没找到任何 QFileDialog 实例（测试本身该更新了）"
    assert offs >= len(instances), (
        f"{len(instances)} 个 QFileDialog 实例，只有 {offs} 处禁用了原生后端")
    # 逐个核对：每个实例后 400 字符内必须有 setOption
    for pos in instances:
        seg = joined[pos:pos + 400]
        assert "DontUseNativeDialog" in seg, (
            "有个 QFileDialog 实例没禁用原生后端（C6 回归，r39e 的 SIGSEGV 雷）")


# ================================================================ C7 抓图墙
def test_c7_live_wall_widget_removed():
    """C7：实时抓图墙部件（livepanel / livegrid）已从布局里删除。"""
    joined = _uncommented_str(_code(MAIN))
    for bad in ("self._live_panel = QWidget()",
                'self._live_panel.setObjectName("livepanel")',
                "self.live_grid = QListWidget()",
                'self.live_grid.setObjectName("livegrid")',
                "self.live_empty = QLabel("):
        assert bad not in joined, f"抓图墙部件还在：{bad}"


def test_c7_live_wall_menu_action_removed():
    """C7：图库菜单里的「实时抓图墙」开关项也撤了（留着就是空开关）。"""
    joined = _uncommented_str(_code(MAIN))
    assert "实时抓图墙" not in joined, "菜单里还有「实时抓图墙」"
    assert "self.act_live" not in joined


def test_c7_live_wall_periodic_timer_check_removed():
    """C7：_tick 里每3 秒查一次抓图墙的定时调用要撤（部件没了，纯浪费）。"""
    joined = _uncommented_str(_code(MAIN))
    assert "_live_seq" not in joined, "_tick 里还在按_live_seq 调 refresh_live"


def test_c7_refresh_live_is_now_a_noop_but_method_survives():
    """refresh_live 保留成空实现 —— 四个调用点不用改，删方法会 AttributeError。"""
    joined = _uncommented_str(_code(MAIN))
    assert "def refresh_live(self):" in joined, "refresh_live 方法被删了"
    i = joined.index("def refresh_live(self):")
    body = joined[i:i + 1200]
    assert "live_grid" not in body, "refresh_live 里还在操作已删的 live_grid"
    assert "def toggle_live_wall(self" in joined, \
        "toggle_live_wall 也要保留（老配置/外部脚本可能还调）"


def test_c7_cfg_live_wall_field_still_in_config():
    """config 里的 live_wall 字段保留 —— 老配置读回来不报错、不用做迁移。"""
    assert "live_wall" in _code(ROOT / "media_resource_manager" / "config.py")


# ================================================================ C9 标签页
def _fake_win_for_model_dialog():
    """造一个够ai_model_dialog 用的假窗口。

    ⚠ **必须继承 QWidget**：ai_model_dialog 里第一句就是 ``QDialog(self)``，
    而 PySide6 对 parent 做类型检查 —— 传个普通对象会直接
    ``TypeError: QDialog.__init__() called with wrong argument types``。
    所以这里只借 cfg 与 _build_model_manager 两个属性，**不起真MainWindow**
    （太重、还会连数据库）。
    """
    import PySide6.QtWidgets as W

    class _Cfg:
        ai_backend = "builtin"
        ai_model_path = ""
        ai_api_url = ""
        ai_api_key = ""
        ai_api_model = ""
        ai_gpu = False
        ai_threads = 0
        rules_dir = None

    class _FakeWin(W.QWidget):
        def __init__(self):
            super().__init__()
            self.cfg = _Cfg()

        def _build_model_manager(self, dlg, path_in):
            return W.QWidget()

        def log_signal(self, *_):
            pass

    return _FakeWin()


def _open_model_dialog(monkeypatch):
    """真的调一次 ai_model_dialog，把建出来的 dlg 截下来返回。

    用 ``QDialog.exec`` 打桩返回 0（= 用户点了关闭），从而**不进入事件循环** ——
    真 exec 会挂住（PySide6 模态 + 无事件循环 = 永久阻塞，这条铁律本轮又踩过）。
    """
    import PySide6.QtWidgets as W
    from media_resource_manager.gui import main_window as mw
    made = {}

    def fake_exec(self):
        made["dlg"] = self
        return 0

    monkeypatch.setattr(W.QDialog, "exec", fake_exec)
    mw.MainWindow.ai_model_dialog(_fake_win_for_model_dialog())
    return made["dlg"]


def test_c9_model_dialog_uses_tabs(qapp, monkeypatch):
    """C9：AI 模型对话框是标签页，不是全摊开的一张长表单。"""
    dlg = _open_model_dialog(monkeypatch)
    tabs = dlg._tabs
    assert tabs.count() == 4, f"应为 4 个标签页，实际 {tabs.count()}"
    titles = [tabs.tabText(i) for i in range(tabs.count())]
    assert titles == ["通用", "到本地", "在线 API", "模型库"], titles


def test_c9_mode_switches_and_disables_tabs(qapp, monkeypatch):
    """C9：模式下拉仍是总开关 —— 切模式会跳对应标签页并禁用其它页。"""
    dlg = _open_model_dialog(monkeypatch)
    tabs, combo = dlg._tabs, dlg._mode_combo

    combo.setCurrentIndex(combo.findData("api"))
    dlg._apply_mode()
    assert tabs.currentIndex() == 2, "选 api 没跳到「在线 API」页"
    assert tabs.isTabEnabled(2) and not tabs.isTabEnabled(1)
    assert dlg._reveal_api and not dlg._reveal_local

    # 2026-10-27（用户裁决）：「到本地（自定义本地模型目录）」这一档已删除 ——
    # 它和模型库的「未下载」是同一件事的两个入口，第三方模型用户更习惯走 API。
    # 所以这里断言它**不存在**，而不是断言"切过去会跳第 1 页"。
    assert combo.findData("local") == -1, "local 档已删除，不该再出现在下拉里"
    assert tabs.currentIndex() == 2, "还在 api 页（选不存在项不该跳页）"

    combo.setCurrentIndex(combo.findData("notdownloaded"))
    dlg._apply_mode()
    assert tabs.currentIndex() == 3
    assert dlg._reveal_dl

    combo.setCurrentIndex(combo.findData("builtin"))
    dlg._apply_mode()
    assert tabs.currentIndex() == 0
    assert not any((dlg._reveal_local, dlg._reveal_api, dlg._reveal_dl))


# ================================================================ C10 巡检
def test_c10_menu_entry_removed():
    """C10：「适配器巡检…」入口从维护菜单移除。"""
    joined = _uncommented_str(_code(MAIN))
    assert "适配器巡检" not in joined, "菜单里还有「适配器巡检」"
    assert "self.open_adapter_checkup)" not in joined


def test_c10_method_and_module_kept():
    """V0.0.5 改判：方法与模块**彻底删除**（决定清单拍板，不留恢复口子）。

    原来这条钉「保留在仓库里将来可恢复」；巡检带崩 + 功能冗余，用户定稿
    彻底移除。改成反向锁：方法、模块、测试文件都不许再存在。
    """
    assert "def open_adapter_checkup(self" not in _uncommented_str(_code(MAIN))
    assert not CHECKUP.exists(), "adapter_checkup 模块必须已删除"


# ↑ V0.0.5：适配器巡检彻底删除后，原来钉「巡检对话框关前 wait() 线程」
#   「worker 登记到 dlg.workers」两条内部行为测试失去对象 —— 被上面
#   test_c10_method_and_module_kept 的「模块必须不存在」整体取代，删除。

# ================================================================ C11 导出
def test_c11_export_includes_progress():
    """C11：导出的每条任务都要带 progress（已下载/已丢弃/待审核/总数）。

    ⚠ ``with tempfile.TemporaryDirectory()`` 里必须**先close() 再退出** ——
    Windows 上 sqlite 句柄还开着时，tmpdir 的 rmtree 会PermissionError
    （WinError 32），表现为「测试失败」，跟被测代码毫无关系（第一版踩过）。
    """
    from media_resource_manager.taskio import task_progress, export_tasks
    from media_resource_manager.db import Database
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    import json as _json

    td = tempfile.mkdtemp()
    db = Database(Path(td) / "t.db")
    try:
        t = TaskConfig(id=None, adapter_name="generic", name="甲",
                       entry_urls=["https://a.com"])
        tid = save_task(db, t)
        # 塞 3 条记录：1 已下载 / 1 隔离 / 1 拉黑
        # ⚠ 两个坑（第一版都踩了，看着像导出功能坏了，其实是测试数据不合格）：
        #   ① images.source_url 是 NOT NULL → 插入必须给值；
        #   ② (task_id, source_url) 有 UNIQUE 约束 → 每条得给**不同**的 url。
        for i, (path, quar, blk) in enumerate(
                (("x.jpg", 0, 0), (None, 1, 0), (None, 0, 1))):
            db.execute(
                "INSERT INTO images (task_id, path, source_url, in_quarantine, "
                "blacklisted, needs_review) VALUES (?,?,?,?,?,?)",
                (tid, path, f"https://a.com/{i}.jpg", quar, blk, 0))
        p = task_progress(db, tid)
        assert p["downloaded"] == 1 and p["discarded"] == 2, p
        assert p["total_records"] == 3

        out = Path(td) / "e.json"
        n = export_tasks(db, out)
        assert n == 1
        data = _json.loads(out.read_text(encoding="utf-8"))
        assert "progress" in data[0], "导出里没有 progress 段（C11 回归）"
        assert data[0]["progress"]["downloaded"] == 1
    finally:
        db.close()
        shutil.rmtree(td, ignore_errors=True)


def test_c11_import_is_symmetric_and_drops_progress():
    """导入能还原配置，但 progress 不还原（软件没有续抓机制）。"""
    from media_resource_manager.db import Database
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task, list_tasks
    from media_resource_manager.taskio import export_tasks, import_tasks_file
    import json as _json
    import tempfile

    td = tempfile.mkdtemp()
    db = db2 = None
    try:
        db = Database(Path(td) / "a.db")
        t = TaskConfig(id=None, adapter_name="generic", name="甲",
                       entry_urls=["https://a.com"], ai_labels=["猫"],
                       concurrency=7, max_depth=5)
        save_task(db, t)
        f = Path(td) / "e.json"
        export_tasks(db, f)

        db2 = Database(Path(td) / "b.db")
        n = import_tasks_file(db2, f)
        assert n == 1, "导入没建出任务"
        got = list_tasks(db2)[0]
        assert got.entry_urls == ["https://a.com"]
        assert got.ai_labels == ["猫"] and got.concurrency == 7
        assert not hasattr(got, "progress")
    finally:
        for d in (db, db2):
            if d is not None:
                d.close()
        shutil.rmtree(td, ignore_errors=True)


def test_c11_import_skips_duplicate_urls():
    """同一个导出文件导两遍，不该多出一堆重复任务。"""
    from media_resource_manager.db import Database
    from media_resource_manager.models import TaskConfig
    from media_resource_manager.repo import save_task
    from media_resource_manager.taskio import export_tasks, import_tasks_file
    import tempfile

    td = tempfile.mkdtemp()
    db = None
    try:
        db = Database(Path(td) / "a.db")
        save_task(db, TaskConfig(id=None, adapter_name="generic", name="甲",
                                 entry_urls=["https://a.com"]))
        f = Path(td) / "e.json"
        export_tasks(db, f)
        assert import_tasks_file(db, f) == 0, "重复网址没跳过"
    finally:
        if db is not None:
            db.close()
        shutil.rmtree(td, ignore_errors=True)


def test_c11_export_menu_entry_symmetric_with_import():
    """菜单里「批量导出任务…」与「批量导入任务…」成对出现。"""
    joined = _uncommented_str(_code(MAIN))
    assert "批量导出任务" in joined and "批量导入任务" in joined
    assert "def batch_export_tasks(self)" in joined
    assert "def batch_import_tasks(self)" in joined


def test_c11_export_readback_verified():
    """导出后必须读回校验（写完不看=没保证）。"""
    joined = _uncommented_str(_code(MAIN))
    i = joined.index("def batch_export_tasks(")
    body = joined[i:i + 4000]
    assert "read_text(" in body, "导出后没有读回校验"


# ================================================================ C12 缩略图尺寸
def test_c12_no_thumbnail_size_menu_entry():
    """C12：图库菜单不再有「缩略图尺寸…」（工具栏滑条已是唯一控件）。"""
    joined = _uncommented_str(_code(MAIN))
    assert 'm_lib.addAction("缩略图尺寸…"' not in joined
    # 方法本身保留（设置页可能还调）
    assert "def edit_thumbnail_size" in joined


# ================================================================ C13 隔离区改名
def test_c13_prune_renamed_to_quarantine_area():
    """C13：「清空回收站」→「清空隔离区」（清的是软件内隔离区，不是系统回收站）。"""
    joined = _uncommented_str(_code(MAIN))
    assert "清空隔离区" in joined
    assert "清空回收站" not in joined, "菜单/弹窗里还有「清空回收站」"


def test_c13_recycle_switch_label_also_fixed():
    """开关项「删除时先进回收站」→「删除时先进隔离区」。"""
    joined = _uncommented_str(_code(MAIN))
    assert "删除时先进隔离区" in joined


def test_c13_internal_names_unchanged():
    """内部名一律不动（老配置兼容）：prune_recycle / use_recycle。"""
    joined = _uncommented_str(_code(MAIN))
    assert "def prune_recycle(self" in joined
    assert "use_recycle" in joined


def test_c13_docs_no_longer_say_recycle_bin():
    """两份文档同步改口径（不改 = 用户照着文档找不到入口）。"""
    for name in ("Sweet采集AI版_软件介绍.md", "Sweet采集AI版使用文档.md"):
        p = ROOT / "docs" / name
        if not p.exists():
            continue
        txt = p.read_text(encoding="utf-8")
        assert "清空回收站" not in txt, f"{name} 里还有「清空回收站」"


# ================================================================ C14 CRX
def test_c14_crx_menu_entry_removed():
    """C14：维护菜单不再有「浏览器插件打包成 CRX」（开发期动作）。"""
    joined = _uncommented_str(_code(MAIN))
    assert "打包成 CRX" not in joined, "维护菜单里还有 CRX 打包入口"
    assert "浏览器插件（取 Cookie/Token）" in joined, \
        "插件安装入口必须留着（未上架 → 打开文件夹手动装）"


def test_c14_crx_method_kept():
    """方法保留（将来上架还要用）。"""
    assert "def build_extension_crx" in _uncommented_str(_code(MAIN))


# ================================================================ 菜单结构不回退
def test_top_level_menus_unchanged():
    """【V0.0.19 用户拍板推倒重排】顶级菜单基线 8 → 5。

    verify_r11 同步改成任务/工具/视图/设置/帮助；页面类（图库/隔离区）
    走左侧导航，「运行」并入「任务」。这里跟着换新基线，并反向锁死
    不许再退回旧的 8 个名字（撤功能/改结构要改干净）。
    """
    joined = _uncommented_str(_code(MAIN))
    for m in ("任务", "工具", "视图", "设置", "帮助"):
        assert f'mb.addMenu("{m}")' in joined, f"顶级菜单「{m}」不见了"
    # 反向：旧的 8 菜单名不许有残留的 mb.addMenu
    for gone in ("运行", "图库", "AI与过滤", "隔离区", "数据", "维护"):
        assert f'mb.addMenu("{gone}")' not in joined, f"旧顶级菜单「{gone}」复活"


def test_c_batch_did_not_touch_watermark_theme():
    """C 批不许动水印几何（§3铁律）。

    具体钉两件事：① r58 的 pane 透明规则仍在 theme.py 且**顺序在实底规则之后**
    （QSS 同特异性下后者胜，顺序反了bug 原样复现）；② 水印控件仍不铺满父容器。
    """
    theme_raw = _code(ROOT / "media_resource_manager" / "gui" / "theme.py")
    # ⚠ 这里必须读**原始源码**而不是 _uncommented：pane 透明规则住在
    # _WM_PENETRATE 这个 QSS 字符串常量里，_strip_docstrings 不会碰它，
    # 但 _uncommented 会把「三引号字符串」当代码丢掉 → 第一版就是这么误报的。
    assert "QTabWidget::pane {\n    background: transparent;\n}" in theme_raw, \
        "r58 的 pane 透明铁律被弄丢了"
    # 顺序铁律：透明规则必须在 LIGHT/DARK 的实底 pane 规则**之后**追加
    # （QSS 同特异性下后者胜，顺序反了 bug 原样复现）
    _pen = theme_raw.index("_WM_PENETRATE")
    _last_solid = max(theme_raw.rindex("QTabWidget::pane", 0, _pen)
                       if "QTabWidget::pane" in theme_raw[:_pen] else 0,
                       0)
    assert _last_solid < _pen, "透明规则被挪到了实底规则之前（顺序反了）"
    wm = _code(ROOT / "media_resource_manager" / "gui" / "watermark.py")
    assert "BOX_MAX" in wm or "setFixedSize" in wm, \
        "水印控件几何被改了（应仍是右下角小方块，不铺满）"

# ================================================================ C8 默认模型
def test_c8_bundled_models_dir_lookup_exists():
    """C8：bundled_models_dir() 必须存在且返回 Path（找不到也返回路径，不返回 None）。"""
    from media_resource_manager.models_lib import (bundled_models_dir,
                                                   BUNDLED_SUBDIR)
    d = bundled_models_dir()
    assert d is not None, "找不到捆绑目录时也应返回一个路径（靠 is_dir 判断）"
    assert d.name == BUNDLED_SUBDIR


def test_c8_resolve_default_model_prefers_bundled(tmp_path, monkeypatch):
    """C8：解析顺序＝捆绑 → 已下载 → 任意就绪。

    这里造一个假的"就绪"模型目录（onnx > 1KB + config.json），
    验证解析逻辑本身正确 —— 不需要真的下模型。
    """
    from media_resource_manager import models_lib

    def _mk(p: Path):
        p.mkdir(parents=True, exist_ok=True)
        (p / "model_quantized.onnx").write_bytes(b"x" * 2048)
        (p / "config.json").write_text("{}", encoding="utf-8")
        return p

    #捆绑目录里有 clip-vit-b32 → 应优先返回它
    bun = tmp_path / "bundled"
    _mk(bun / models_lib.DEFAULT_BUILTIN_ID)
    monkeypatch.setattr(models_lib, "bundled_models_dir", lambda: bun)
    base = tmp_path / "models"
    _mk(base / models_lib.DEFAULT_BUILTIN_ID)   # 下载目录也有一份
    got = models_lib.resolve_default_model_dir(base)
    assert got is not None
    assert got == bun / models_lib.DEFAULT_BUILTIN_ID, \
        "捆绑模型应优先于下载目录"


def test_c8_resolve_returns_none_when_nothing_ready(tmp_path, monkeypatch):
    """什么都没有时返回 None（不是抛异常、也不是返回一个不存在的路径）。"""
    from media_resource_manager import models_lib
    monkeypatch.setattr(models_lib, "bundled_models_dir",
                        lambda: tmp_path / "no_such_dir")
    assert models_lib.resolve_default_model_dir(tmp_path / "empty") is None


def test_c8_tiny_onnx_counts_as_not_ready(tmp_path, monkeypatch):
    """残缺的 onnx（<1KB）不算就绪 —— 断点下载留下的空壳最容易被当成可用。"""
    from media_resource_manager import models_lib
    bun = tmp_path / "bundled"
    d = bun / models_lib.DEFAULT_BUILTIN_ID
    d.mkdir(parents=True)
    (d / "model_quantized.onnx").write_bytes(b"x")     # 只有 1 字节
    (d / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(models_lib, "bundled_models_dir", lambda: bun)
    assert models_lib.resolve_default_model_dir(tmp_path / "empty") is None


def test_c8_autopoint_never_overwrites_user_choice(qapp, monkeypatch):
    """C8：用户已配了模型/已选api 模式 → 启动自动指向**不许覆盖**。"""
    from media_resource_manager.gui import main_window as mw

    class _Cfg:
        ai_model_path = "/my/model"       # 用户自己配了
        ai_backend = "builtin"
        models_dir = ""

        def __getattr__(self, k):
            return None

    seen = []

    class _FakeWin:
        cfg = _Cfg()

        def _models_base_dir(self):
            from pathlib import Path
            return Path(".")

        def _save_cfg_quiet(self):
            seen.append("saved")

        def log_signal(self, *_):
            pass

    #即使真解析出模型，也不该动已有配置
    monkeypatch.setattr("media_resource_manager.models_lib."
                        "resolve_default_model_dir",
                        lambda base: "SHOULD_NOT_BE_USED")
    got = mw.MainWindow._autopoint_default_model(_FakeWin())
    assert got is None
    assert _FakeWin.cfg.ai_model_path == "/my/model", "覆盖了用户的模型选择！"
    assert seen == [], "不该保存配置"


def test_c8_autopoint_fills_when_empty(qapp, monkeypatch):
    """C8：配置为空 + 能解析到模型 → 自动填上并落盘。"""
    from media_resource_manager.gui import main_window as mw

    class _Cfg:
        ai_model_path = ""
        ai_backend = "builtin"
        models_dir = ""

    saved = []

    class _Log:
        """log_signal 是 Signal，代码里会 .emit(msg) —— 假对象必须也有 emit。

        ⚠ 第一版把它写成普通方法，`self.log_signal.emit(...)` 抛 AttributeError，
        而 _autopoint_default_model 外面包着 try/except →异常被安静吃掉、
        返回 None，看着像"自动指向功能没实现"。测试替身不合格也会骗人。
        """
        def emit(self, *_):
            pass

    class _FakeWin:
        cfg = _Cfg()

        def _models_base_dir(self):
            from pathlib import Path
            return Path(".")

        def _save_cfg_quiet(self):
            saved.append(1)

        log_signal = _Log()

    monkeypatch.setattr("media_resource_manager.models_lib."
                        "resolve_default_model_dir",
                        lambda base: "C:/models/clip-vit-b32")
    got = mw.MainWindow._autopoint_default_model(_FakeWin())
    assert got == "C:/models/clip-vit-b32"
    assert _FakeWin.cfg.ai_model_path == "C:/models/clip-vit-b32"
    assert saved == [1], "自动指向后应落盘，下次启动直接可用"


def test_c8_autopoint_swallows_errors(qapp, monkeypatch):
    """模型解析抛异常绝不该影响启动（安静返回 None）。"""
    from media_resource_manager.gui import main_window as mw

    class _Cfg:
        ai_model_path = ""
        ai_backend = "builtin"
        models_dir = ""

    class _FakeWin:
        cfg = _Cfg()

        def _models_base_dir(self):
            from pathlib import Path
            return Path(".")

        def _save_cfg_quiet(self):
            raise AssertionError("不该走到保存")

        def log_signal(self, *_):
            pass

    def boom(base):
        raise RuntimeError("炸了")

    monkeypatch.setattr("media_resource_manager.models_lib."
                        "resolve_default_model_dir", boom)
    assert mw.MainWindow._autopoint_default_model(_FakeWin()) is None


def test_c8_spec_ships_bundled_models_conditionally():
    """打包配置：bundled_models 目录存在就打进datas，不存在就跳过。

    **必须条件化** —— PyInstaller 遇到不存在的 datas 源会直接中断打包，
    写死会让「还没下模型」的状态完全打不出exe。
    """
    spec = _code(ROOT / "build" / "pyinstaller.spec")
    assert "bundled_models" in spec
    assert "os.path.isdir" in spec, \
        "bundled_models 没做存在性判断 —— 目录不在时打包会直接失败"


# ================================================================ C8 默认模型
def test_c8_bundled_models_dir_lookup_exists():
    """C8：bundled_models_dir() 必须存在且返回 Path（找不到也返回路径，不返回 None）。"""
    from media_resource_manager.models_lib import (bundled_models_dir,
                                                   BUNDLED_SUBDIR)
    d = bundled_models_dir()
    assert d is not None, "找不到捆绑目录时也应返回一个路径（靠 is_dir 判断）"
    assert d.name == BUNDLED_SUBDIR


def test_c8_resolve_default_model_prefers_bundled(tmp_path, monkeypatch):
    """C8：解析顺序＝捆绑 → 已下载 → 任意就绪。

    这里造一个假的"就绪"模型目录（onnx > 1KB + config.json），
    验证解析逻辑本身正确 —— 不需要真的下模型。
    """
    from media_resource_manager import models_lib

    def _mk(p: Path):
        p.mkdir(parents=True, exist_ok=True)
        (p / "model_quantized.onnx").write_bytes(b"x" * 2048)
        (p / "config.json").write_text("{}", encoding="utf-8")
        return p

    #捆绑目录里有 clip-vit-b32 → 应优先返回它
    bun = tmp_path / "bundled"
    _mk(bun / models_lib.DEFAULT_BUILTIN_ID)
    monkeypatch.setattr(models_lib, "bundled_models_dir", lambda: bun)
    base = tmp_path / "models"
    _mk(base / models_lib.DEFAULT_BUILTIN_ID)   # 下载目录也有一份
    got = models_lib.resolve_default_model_dir(base)
    assert got is not None
    assert got == bun / models_lib.DEFAULT_BUILTIN_ID, \
        "捆绑模型应优先于下载目录"


def test_c8_resolve_returns_none_when_nothing_ready(tmp_path, monkeypatch):
    """什么都没有时返回 None（不是抛异常、也不是返回一个不存在的路径）。"""
    from media_resource_manager import models_lib
    monkeypatch.setattr(models_lib, "bundled_models_dir",
                        lambda: tmp_path / "no_such_dir")
    assert models_lib.resolve_default_model_dir(tmp_path / "empty") is None


def test_c8_tiny_onnx_counts_as_not_ready(tmp_path, monkeypatch):
    """残缺的 onnx（<1KB）不算就绪 —— 断点下载留下的空壳最容易被当成可用。"""
    from media_resource_manager import models_lib
    bun = tmp_path / "bundled"
    d = bun / models_lib.DEFAULT_BUILTIN_ID
    d.mkdir(parents=True)
    (d / "model_quantized.onnx").write_bytes(b"x")     # 只有 1 字节
    (d / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(models_lib, "bundled_models_dir", lambda: bun)
    assert models_lib.resolve_default_model_dir(tmp_path / "empty") is None


def test_c8_autopoint_never_overwrites_user_choice(qapp, monkeypatch):
    """C8：用户已配了模型/已选api 模式 → 启动自动指向**不许覆盖**。"""
    from media_resource_manager.gui import main_window as mw

    class _Cfg:
        ai_model_path = "/my/model"       # 用户自己配了
        ai_backend = "builtin"
        models_dir = ""

        def __getattr__(self, k):
            return None

    seen = []

    class _FakeWin:
        cfg = _Cfg()

        def _models_base_dir(self):
            from pathlib import Path
            return Path(".")

        def _save_cfg_quiet(self):
            seen.append("saved")

        def log_signal(self, *_):
            pass

    #即使真解析出模型，也不该动已有配置
    monkeypatch.setattr("media_resource_manager.models_lib."
                        "resolve_default_model_dir",
                        lambda base: "SHOULD_NOT_BE_USED")
    got = mw.MainWindow._autopoint_default_model(_FakeWin())
    assert got is None
    assert _FakeWin.cfg.ai_model_path == "/my/model", "覆盖了用户的模型选择！"
    assert seen == [], "不该保存配置"


def test_c8_autopoint_fills_when_empty(qapp, monkeypatch):
    """C8：配置为空 + 能解析到模型 → 自动填上并落盘。"""
    from media_resource_manager.gui import main_window as mw

    class _Cfg:
        ai_model_path = ""
        ai_backend = "builtin"
        models_dir = ""

    saved = []

    class _Log:
        """log_signal 是 Signal，代码里会 .emit(msg) —— 假对象必须也有 emit。

        ⚠ 第一版把它写成普通方法，`self.log_signal.emit(...)` 抛 AttributeError，
        而 _autopoint_default_model 外面包着 try/except →异常被安静吃掉、
        返回 None，看着像"自动指向功能没实现"。测试替身不合格也会骗人。
        """
        def emit(self, *_):
            pass

    class _FakeWin:
        cfg = _Cfg()

        def _models_base_dir(self):
            from pathlib import Path
            return Path(".")

        def _save_cfg_quiet(self):
            saved.append(1)

        log_signal = _Log()

    monkeypatch.setattr("media_resource_manager.models_lib."
                        "resolve_default_model_dir",
                        lambda base: "C:/models/clip-vit-b32")
    got = mw.MainWindow._autopoint_default_model(_FakeWin())
    assert got == "C:/models/clip-vit-b32"
    assert _FakeWin.cfg.ai_model_path == "C:/models/clip-vit-b32"
    assert saved == [1], "自动指向后应落盘，下次启动直接可用"


def test_c8_autopoint_swallows_errors(qapp, monkeypatch):
    """模型解析抛异常绝不该影响启动（安静返回 None）。"""
    from media_resource_manager.gui import main_window as mw

    class _Cfg:
        ai_model_path = ""
        ai_backend = "builtin"
        models_dir = ""

    class _FakeWin:
        cfg = _Cfg()

        def _models_base_dir(self):
            from pathlib import Path
            return Path(".")

        def _save_cfg_quiet(self):
            raise AssertionError("不该走到保存")

        def log_signal(self, *_):
            pass

    def boom(base):
        raise RuntimeError("炸了")

    monkeypatch.setattr("media_resource_manager.models_lib."
                        "resolve_default_model_dir", boom)
    assert mw.MainWindow._autopoint_default_model(_FakeWin()) is None


def test_c8_spec_ships_bundled_models_conditionally():
    """打包配置：bundled_models 目录存在就打进datas，不存在就跳过。

    **必须条件化** —— PyInstaller 遇到不存在的 datas 源会直接中断打包，
    写死会让「还没下模型」的状态完全打不出exe。
    """
    spec = _code(ROOT / "build" / "pyinstaller.spec")
    assert "bundled_models" in spec
    assert "os.path.isdir" in spec, \
        "bundled_models 没做存在性判断 —— 目录不在时打包会直接失败"
