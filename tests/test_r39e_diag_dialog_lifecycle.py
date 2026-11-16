"""r39e：锁死「导出诊断包」那个保存对话框的生命周期。

真机现场（r39d 那轮扫描 258 步）：

    阻塞式 `QFileDialog.getSaveFileName()` 是真凶之一 —— 它「弹原生对话框 +
    套一层嵌套事件循环」，而扫描的模态应答器正好在那个嵌套循环里去点它的
    按钮 / 销毁它，两边一撞就是 `0xC0000409` 无声死亡（gui-crash.log 空、
    没有 Python 异常）。改成非阻塞 `dlg.open()` + accepted 信号之后，
    258 步不崩了，但**又漏了一整个生命周期**：

1. `export_diagnostic_bundle` 里的 `dlg = QFileDialog(...)` 是**局部变量**，
   从没写过 `self._diag_dlg = dlg`。PySide6 拿不到引用就等 GC，于是下一行
   `self._diag_dlg` 永远是 None —— 取消分支里那一整套
   `reject / close / setParent(None) / deleteLater` **一次都没执行过**。
2. `_on_diag_bundle_picked`（用户点「保存」）从头到尾没碰对话框，
   选完路径就直接打包走了，窗口照样留在那儿。
   ⇒ 每点一次「导出诊断包…」漏一个原生文件对话框，真机扫描里这个动作
   会被点很多遍，窗口一批批堆着不释放。

本文件用假 QFileDialog 把这两条钉死（真 QFileDialog 在 CI 会弹原生窗口，
不好断言）。顺带锁住「不许再退回阻塞式静态函数」。
"""
import pathlib

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

# 假的 exec() 返回值，和 QDialog.DialogCode 对齐
_Accepted = 1
_Rejected = 0


@pytest.fixture(autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class _SignalStub:
    """真 Qt 信号的行为：**摘掉的连接不会在 emit 里被调用**。

    `disconnect` 记一笔 `disconnected`，测试据此断言「关对话框之前
    已经把信号摘掉了」—— 否则 reject() 一发信号就会把已经选好的路径清掉。
    """

    def __init__(self):
        self.cbs = []
        self.disconnected = 0

    def connect(self, cb):
        self.cbs.append(cb)

    def disconnect(self):
        self.disconnected += 1
        self.cbs = []

    def emit(self):
        for cb in list(self.cbs):
            cb()


class _FakeFileDialog:
    """假的 QFileDialog：记录它被怎么开关的，不真弹原生窗口。

    整个类被 monkeypatch 顶掉 `PySide6.QtWidgets.QFileDialog`，所以枚举
    成员得自己补上（主代码里 `QFileDialog.DontUseNativeDialog` 照样写）。
    """

    last = None

    class _AcceptMode:
        AcceptSave = "save"
        AcceptOpen = "open"

    class _FileMode:
        Directory = "dir"

    class _Option:
        ShowDirsOnly = "showdirs"

    AcceptMode = _AcceptMode
    FileMode = _FileMode
    Option = _Option
    DontUseNativeDialog = "dont_use_native"
    # 假对话框每次 new 都从类上取 exec() 的返回值，用例改一次即可影响后续所有实例
    exec_result = _Accepted

    def __init__(self, parent, title, directory, filter_=""):
        self.parent = parent
        self.title = title
        self.directory = directory
        self.filter = filter_
        self.accept_mode = None
        self.files = [directory]
        self.opened = False
        self.closed = False
        self.deleted = False
        self.parent_detached = False
        self.done_code = None
        self.was_exec = False
        self.accepted = _SignalStub()
        self.rejected = _SignalStub()
        self.instances = getattr(self, "_instances", None)
        _FakeFileDialog.last = self

    @classmethod
    def reset_class(cls):
        cls.last = None

    def setOption(self, opt, on=True):
        self.options = getattr(self, "options", set()) | {opt}
        self.option_on = getattr(self, "option_on", True) and on

    def setFileMode(self, mode):
        self.file_mode = mode

    def setAcceptMode(self, mode):
        self.accept_mode = mode

    def setDirectory(self, path):
        self.directory = path

    def selectFile(self, path):
        self.files = [path]

    def exec(self):
        self.was_exec = True
        return type(self).exec_result      # 每次都从类上读，用例改 `fake_fdlg.exec_result` 就生效

    def selectedFiles(self):
        return list(self.files)

    def open(self):
        self.opened = True

    def reject(self):
        self.done_code = 0
        self.rejected.emit()      # 真 QDialog 就是这么干的

    def close(self):
        self.closed = True

    def done(self, code):
        self.done_code = code

    def setParent(self, _p):
        self.parent_detached = True

    def deleteLater(self):
        self.deleted = True


@pytest.fixture
def win(qapp):
    from media_resource_manager.gui.main_window import MainWindow

    w = MainWindow()
    w._ensure_single_instance = lambda *a, **k: True
    w._build_diagnostic_bundle = lambda: None      # 别真去打 zip
    yield w
    try:
        w.close()
    except Exception:
        pass


@pytest.fixture
def fake_fdlg(monkeypatch):
    _FakeFileDialog.reset_class()
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog", _FakeFileDialog)
    return _FakeFileDialog


# ------------------------------------------------------------ 1. 必须自持引用


def test_export_holds_reference_to_dialog(win, fake_fdlg):
    """建完 dialog 必须 `self._diag_dlg = dlg`，否则取消分支整套销毁是空话。"""
    win.export_diagnostic_bundle()
    dlg = getattr(win, "_diag_dlg", None)
    assert dlg is not None, (
        "`self._diag_dlg` 还是 None —— 上一版就是漏了这一句赋值， "
        "导致对话框被 GC / 取消分支永远不释放")
    assert dlg is fake_fdlg.last


def test_export_uses_nonblocking_open_not_static_blocking(win, fake_fdlg, monkeypatch):
    """不许退回阻塞式静态函数（0xC0000409 的直接成因）。"""
    import inspect
    import pathlib

    from media_resource_manager.gui import main_window as mwm
    win.export_diagnostic_bundle()      # 先把对话框弹出来（走的是假 QFileDialog）
    # r39g：同 test_view_size —— inspect.getsource 在全量会话里会拿到陈旧源码
    # 缓存（.open() 明明写得好好地，断言却说找不到），改读磁盘源文件。
    src = pathlib.Path(inspect.getsourcefile(mwm)).read_text(
        encoding="utf-8", errors="ignore")
    # 但读全文会误伤：文件里别的地方（_file_pick 的实现、r39e 的注释
    # docstring）本来就有 `getSaveFileName` 这几个字，一刀切下去必然假红。
    # 所以只切出本函数这一段（到下一个 4 空格缩进的 def 为止）。
    blk = src.split("def export_diagnostic_bundle(")[-1].split("\n    def ")[0]
    code_lines = [ln for ln in blk.splitlines() if not ln.strip().startswith("#")]
    assert not any("getSaveFileName" in ln or "getOpenFileName" in ln
                   for ln in code_lines), "又用回阻塞式 getSaveFileName 了"
    assert any(".open()" in ln for ln in code_lines), (
        "非阻塞对话框应该走 dlg.open()")
    assert win._diag_pending_path, "默认路径要先占好位，accepted 槽才有得用"
    assert win._diag_pending_path.endswith(".zip")


def test_reopen_closes_previous_dialog(win, fake_fdlg):
    """再点一次「导出」时，上一轮的对话框得先被收掉，不能一路堆窗口。"""
    win.export_diagnostic_bundle()
    first = win._diag_dlg
    win.export_diagnostic_bundle()
    second = win._diag_dlg
    assert second is not first
    assert first.deleted or first.parent_detached, (
        "旧的保存对话框没被释放，真机上每点一次漏一个原生窗口")


# ------------------------------------------------------------ 2. 取消要销毁


def test_cancel_releases_dialog(win, fake_fdlg):
    win.export_diagnostic_bundle()
    dlg = win._diag_dlg          # 先留住引用，关掉之后 win 上那个就置 None 了
    win._diag_pending_path = "whatever"
    win._on_diag_bundle_cancel()
    assert win._diag_dlg is None, "取消之后不能还挂着一个对话框引用"
    assert dlg.deleted, "deleteLater 没调"
    assert dlg.parent_detached, "setParent(None) 没调 —— 残留窗口就是这么堆出来的"
    assert dlg.done_code is not None, "reject/close 至少得有一个把窗口关掉"


def test_cancel_clears_pending_path(win, fake_fdlg):
    """取消必须把 _diag_pending_path 清掉，否则下一轮会被当成「已选路径」。"""
    win.export_diagnostic_bundle()
    assert win._diag_pending_path
    win._on_diag_bundle_cancel()
    assert not win._diag_pending_path


def test_cancel_is_idempotent(win, fake_fdlg):
    win.export_diagnostic_bundle()
    win._on_diag_bundle_cancel()
    win._on_diag_bundle_cancel()          # 再来一次不该抛
    assert win._diag_dlg is None


# ------------------------------------------------------------ 3. 接受也要销毁


def test_accepted_path_releases_dialog(win, fake_fdlg):
    """用户点「保存」走的是 accepted 分支 —— 那儿以前根本不碰对话框。"""
    win.export_diagnostic_bundle()
    dlg = win._diag_dlg
    # 用户选了别的路径
    dlg.selectFile(r"D:/c/picked_diag.zip")
    dlg.accepted.emit()

    assert win._diag_dlg is None, "accepted 分支没销毁对话框（照样漏窗口）"
    assert dlg.deleted, "accepted 之后也要 deleteLater"
    assert win._diag_pending_path == "D:/c/picked_diag.zip", "选中的路径没被采纳"
    # 关对话框不能把 rejected 放出去：一放出去 cancel 槽就会把上面这条路径清掉
    assert dlg.rejected.disconnected >= 1, (
        "关之前得先摘信号，否则 reject() 放出的 rejected 会把已选路径抹成 None")


def test_accepted_then_cancel_is_safe(win, fake_fdlg):
    """先 accepted 再 cancel（信号乱序 / 槽里又摸到旧的）不能炸。"""
    win.export_diagnostic_bundle()
    win._diag_dlg.accepted.emit()
    win._on_diag_bundle_cancel()          # 旧引用已置 None，这里应该什么都不做
    assert win._diag_dlg is None


def test_cancel_before_accepted(win, fake_fdlg):
    """反过来：先 cancel 再 accepted，也不能去摸已经删掉的对话框。"""
    win.export_diagnostic_bundle()
    dlg = win._diag_dlg
    win._on_diag_bundle_cancel()
    dlg.accepted.emit()                   # 迟到的信号
    assert win._diag_dlg is None


# ------------------------------------------- 4. 原生后端（这轮的真正真凶）


def test_file_pick_must_disable_native_backend(win, fake_fdlg, monkeypatch):
    """`_file_pick` 必须关掉原生 Win32 文件对话框后端 —— 这是 r39e 的死因。

    实测矩阵（每种跑 3 遍）：
        原生 + open() + reject/accept/close/hide/done → **3/3 SIGSEGV(139)**
        原生 + exec() + reject/点Cancel                → 3/3 安全
        非原生 + open()/exec() + 任意关法             → 3/3 安全
    也就是说雷不在「嵌套事件循环」，而在**原生后端被 open() 出去之后
    又从 Qt 侧主动关掉**。当初把阻塞式 getSaveFileName 换成非阻塞 open()
    只是把雷换了地方，没把它拆了。
    """
    from PySide6.QtWidgets import QFileDialog

    got = win._file_pick("标题", start="x.json", filter_="JSON (*.json)", save=True)
    dlg = fake_fdlg.last
    assert dlg is not None, "_file_pick 没建对话框"
    assert getattr(dlg, "options", None) == {QFileDialog.DontUseNativeDialog}, (
        "没设 DontUseNativeDialog —— 原生文件 dialog 一旦被主动关闭就是 SIGSEGV")
    assert dlg.accept_mode == QFileDialog.AcceptMode.AcceptSave
    assert dlg.files[0] == "x.json", "保存模式该把默认文件名选上"
    assert got, "Accepted 时得返回选中的路径"


def test_file_pick_returns_empty_when_rejected(win, fake_fdlg):
    # 先跑一遍把 last 填出来，再把 exec 的返回值改成 Rejected
    assert win._file_pick("标题", start="pick.json",
                          filter_="JSON (*.json)") == "pick.json"
    fake_fdlg.exec_result = _Rejected      # 改类上的默认值，后面 new 出来的都跟着变
    assert win._file_pick("标题", start="pick.json",
                          filter_="JSON (*.json)") == ""
    assert win._file_pick("标题", start="E:\\", dir_only=True) == ""


def test_file_pick_accept_open_mode(win, fake_fdlg):
    from PySide6.QtWidgets import QFileDialog
    win._file_pick("导入偏好", filter_="JSON (*.json)")
    assert fake_fdlg.last.accept_mode == QFileDialog.AcceptMode.AcceptOpen


def test_dir_pick_uses_directory_mode(win, fake_fdlg):
    from PySide6.QtWidgets import QFileDialog
    win._dir_pick("选择输出目录", start=r"E:\\")
    dlg = fake_fdlg.last
    assert dlg.file_mode == QFileDialog.FileMode.Directory, "目录选择得开 Directory 模式"
    assert dlg.accept_mode is None, "目录模式不该再设 AcceptOpen/AcceptSave"


def test_no_blocking_static_pickers_left():
    """main_window.py 的**代码**里不许再出现阻塞式静态文件选择函数（注释/docstring 不算）。

    走 AST 而不是字符串匹配：源码注释里为了说明死因会提到 `QFileDialog.
    getSaveFileName()`，字符串匹配会把说明文字当代码掐。
    """
    import ast

    p = (pathlib.Path(__file__).resolve().parents[1]
         / "media_resource_manager/gui/main_window.py")
    tree = ast.parse(p.read_text(encoding="utf-8"))
    bad = {"getSaveFileName", "getOpenFileName", "getExistingDirectory"}
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in bad):
            hits.append(f"L{node.lineno} .{node.func.attr}()")
    assert not hits, (
        f"又用回阻塞式静态文件选择 {hits} —— 全项目一律走 _file_pick/_dir_pick，"
        "否则对话框被关闭时 6 种关法 3/3 SIGSEGV")


def test_diag_dialog_disables_native_backend(win, fake_fdlg, monkeypatch):
    """导出诊断包的对话框同样必须关掉原生后端（它走 open() + 主动 destruct）。"""
    from PySide6.QtWidgets import QFileDialog
    win.export_diagnostic_bundle()
    dlg = win._diag_dlg
    assert dlg is not None
    assert getattr(dlg, "options", None) == {QFileDialog.DontUseNativeDialog}, (
        "导出诊断包的对话框没关原生后端 —— 收尾 _diag_dlg_close() 里 reject() 会 139")


# ------------------------------------------------------- 5. 凑两个也别串味儿


def test_both_branches_end_with_no_live_dialog(win, fake_fdlg):
    """无论走哪条分支，一轮结束都不许留下活着的保存对话框。"""
    for emit_cancel in (True, False):
        win.export_diagnostic_bundle()
        (win._diag_dlg.rejected if emit_cancel else win._diag_dlg.accepted).emit()
        assert win._diag_dlg is None, f"分支 cancel={emit_cancel} 留下了活对话框"
    # 两条分支都不该把上一轮的路径留下：cancel 分支必须清空，
    # accepted 分支则被换成这一轮选中的路径（真机上就是默认 zip 名）。
    assert win._diag_pending_path and win._diag_pending_path.endswith(".zip")
