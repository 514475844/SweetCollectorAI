"""R202 护栏：R184（不弹黑窗）/ R185（立即彻底退出）/ R178（并发上限 50）
/ R180（全部开始如实提示）/ R182（全局快捷键审计）。

行为断言优先，源码判据只做兜底（本项目踩过多次「只搜字符串＝空壳断言」）。

⚠ 两个仪器要点（本棒实证）：
  1. 判据**必须盯被调用**，不能盯「源码里出现这个字符串」——`subprocess.Popen`
     的隐藏窗口参数可以写在任何一层，只搜串会被同义写法骗过。
  2. `force_quit()` 生产路径是 `os._exit(0)`（不返回）⇒ 测试一律**注入**
     `exit_fn`，绝不在测试里真退（真退会把整个 pytest 进程带走）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_resource_manager import childproc                     # noqa: E402
from media_resource_manager.backends import gallery_dl as gd     # noqa: E402
from media_resource_manager.gui import lifecycle                 # noqa: E402
from media_resource_manager.gui.main_window import (             # noqa: E402
    MAX_PARALLEL_TASKS, format_start_all_message)

PKG = ROOT / "media_resource_manager"


def _src(rel: str) -> str:
    return (PKG / rel).read_text(encoding="utf-8")


def _all_pkg_sources() -> list[tuple[str, str]]:
    out = []
    for p in sorted(PKG.rglob("*.py")):
        if ".bak" in p.name or "__pycache__" in p.parts:
            continue
        try:
            out.append((str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


class _FakeProc:
    """够用的 Popen 替身：记录 terminate/kill，stdout 可喂行。"""

    def __init__(self, lines=()):
        self.stdout = iter(list(lines))
        self.terminated = False
        self.killed = False
        self.waited = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self.waited = True
        self._alive = False
        return 0


class _StubbornProc(_FakeProc):
    """「terminate 收不到」的替身：模拟正在下载、不给信号的 gallery-dl。

    ⚠ 用它是必须的：普通替身的 terminate 会立刻置死，那样永远测不到
    「宽限期后补 kill」这条路径，护栏就变成空壳断言。
    """

    def terminate(self):
        self.terminated = True          # 信号发了，但进程还活着

    def wait(self, timeout=None):       # 等不到
        raise subprocess.TimeoutExpired("cmd", timeout)


# ═══════════════════════════════════════════════════ R184：不弹黑控制台窗口
@pytest.mark.parametrize("url,expected", [
    ("https://example.com/", True),
    ("https://example.com", True),
    ("https://example.com/index.html", True),
    ("https://example.com/INDEX.HTML", True),
    ("https://example.com/?page=2", True),          # 路径仍是根
    ("https://example.com/post/show/123", False),   # 具体图集/帖子
    ("https://danbooru.donmai.us/posts/1", False),
    ("https://example.com/galleries/abc/", False),  # 列表页（末尾斜杠也是路径）
    ("", True),
    (None, True),
])
def test_bare_homepage_judgement(url, expected):
    """宁松勿紧：只有路径真的是根才算首页，列表页照旧交给 gallery-dl。"""
    assert gd.is_bare_homepage(url) is expected


@pytest.mark.skipif(os.name != "nt", reason="Windows 专属的隐藏窗口参数")
def test_no_window_kwargs_has_both_mechanisms():
    kw = childproc.no_window_kwargs()
    assert kw.get("creationflags") == 0x08000000
    si = kw.get("startupinfo")
    assert si is not None, "creationflags 对「父进程自己是控制台程序」的场景无效"
    assert si.dwFlags & int(subprocess.STARTF_USESHOWWINDOW)
    assert si.wShowWindow == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows 专属的隐藏窗口参数")
def test_download_iter_spawns_hidden_and_registers(monkeypatch, tmp_path):
    """真跑 download_iter：spawn 参数必须带隐藏窗口标志。

    盯**实际传给 Popen 的 kwargs**，不盯源码字符串（同义写法骗不过这个）。
    """
    childproc.clear_children()
    records: list[dict] = []
    proc = _FakeProc([])
    monkeypatch.setattr(childproc.subprocess, "Popen",
                        lambda cmd, **kw: (records.append(kw), proc)[1])
    monkeypatch.setattr(gd, "_exe", lambda: "dummy-gallery-dl.exe")
    be = gd.GalleryDLBackend(dest_root=tmp_path)
    list(be.download_iter("https://example.com/galleries/abc/"))

    assert records, "download_iter 竟然没起子进程"
    assert records[0].get("creationflags") == 0x08000000
    assert records[0].get("startupinfo") is not None
    # 跑完必须自己摘掉登记，否则表会随采集时长无限增长
    assert childproc.live_children() == []


def test_engine_skips_bare_homepage_before_spawn():
    """快路径必须在 spawn 之前就把裸首页挑掉（否则每喂一次弹一次黑窗）。"""
    src = _src("engine.py")
    assert "is_bare_homepage(url)" in src
    assert "跳过裸首页入口" in src
    # 全被跳过时要给用户一句中文解释，不能静默「成功但 0 张图」
    assert "入口为站点首页" in src


def test_download_also_hides_window():
    """同步下载那条路径（subprocess.run）同样要带隐藏参数（源码兜底）。

    这条只能静态判：download() 是同步等 180s 的调用，真跑要真装 gallery-dl。
    """
    src = _src("backends/gallery_dl.py")
    assert "**childproc.no_window_kwargs()" in src
    # 两个 spawn 点都在：run 分支与 Popen 分支
    assert src.count("childproc.no_window_kwargs()") >= 1
    assert "childproc.spawn_hidden(" in src


def test_stop_kills_when_terminate_ignored():
    """R185：任务停止时只 terminate 不够，宽限期后必须补 kill。"""
    proc = _FakeProc([])
    proc._alive = True
    be = gd.GalleryDLBackend.__new__(gd.GalleryDLBackend)
    be._proc = proc
    be.stop()
    assert proc.terminated
    # 「不理信号」的替身：只 terminate 不够，必须补 kill
    stub = _StubbornProc([])
    stub._alive = True
    be._proc = stub
    be.stop()
    assert stub.killed, "terminate 无效时必须补 kill，否则黑窗永远关不掉"


# ═══════════════════════════════════════════════════ R185：立即彻底退出
def test_child_registry_roundtrip():
    childproc.clear_children()
    p = _FakeProc([])
    childproc.register_child(p)
    childproc.register_child(p)                 # 幂等：不能重复登记
    assert childproc.live_children() == [p]
    childproc.unregister_child(p)
    assert childproc.live_children() == []


def test_kill_children_terminates_then_kills():
    childproc.clear_children()
    stub = _StubbornProc([])
    stub._alive = True
    childproc.register_child(stub)
    n = childproc.kill_children(grace=0.05)
    assert n == 1
    assert stub.terminated and stub.killed
    assert childproc.live_children() == []


def test_force_quit_kills_children_stops_queue_and_exits():
    """force_quit 四步：杀子进程 → 停队列 → 关库 → 硬退（注入 exit_fn）。"""
    childproc.clear_children()
    p = _StubbornProc([])
    p._alive = True
    childproc.register_child(p)

    calls: list[str] = []
    q = types.SimpleNamespace(
        stop_all=lambda: calls.append("stop_all"),
        stop=lambda: calls.append("stop"),
        _thread=types.SimpleNamespace(is_alive=lambda: False))
    db = types.SimpleNamespace(close=lambda: calls.append("db_close"))
    obj = types.SimpleNamespace(_queue=q, db=db,
                                _wait_bg_qthreads=lambda budget: calls.append(
                                    f"qthreads:{budget:.2f}"))
    exits: list[int] = []
    lifecycle.force_quit(obj, budget=1.0, exit_fn=exits.append, sweep=False)

    assert p.killed, "子进程必须先死，否则退出不叫「彻底」"
    assert "stop_all" in calls and "stop" in calls
    assert "db_close" in calls
    assert any(c.startswith("qthreads:") for c in calls)
    assert exits == [0], "最后必须硬退（os._exit 的注入替身被调用）"


def test_spawn_hidden_registers_the_child(monkeypatch):
    """登记必须在 spawn 那一刻就做（否则退出时根本没有东西可杀）。

    ⚠ 这条是 M18 的补漏：只验「跑完登记表是空的」恒真，等于没验。
    """
    childproc.clear_children()
    proc = _FakeProc([])
    monkeypatch.setattr(childproc.subprocess, "Popen",
                        lambda cmd, **kw: proc)
    got = childproc.spawn_hidden(["dummy.exe", "-c", "x"])
    assert got is proc
    assert childproc.live_children() == [proc], "spawn 必须登记，否则杀不掉"
    childproc.clear_children()


def test_force_quit_hard_exits_when_no_exit_fn(monkeypatch):
    """生产路径（不注入 exit_fn）必须真的 `os._exit(0)`。

    ⚠ 这条是 M10 的补漏：只注入 exit_fn 的话，「把 os._exit 换成空操作」
    这种变异永远绿 —— 注入点自己把被测对象顶掉了。
    """
    childproc.clear_children()
    exits: list[int] = []
    monkeypatch.setattr(lifecycle.os, "_exit", exits.append)
    lifecycle.force_quit(types.SimpleNamespace(), budget=0.4, sweep=False)
    assert exits == [0], "不传 exit_fn 时必须走 os._exit(0)"


def test_force_quit_survives_objects_without_queue():
    """替身/无 GUI 环境：什么都不该炸，且仍然硬退。"""
    childproc.clear_children()
    exits: list[int] = []
    lifecycle.force_quit(types.SimpleNamespace(), budget=0.5,
                         exit_fn=exits.append, sweep=False)
    assert exits == [0]


def test_force_quit_sweep_uses_taskkill_tree(monkeypatch):
    """兜底清扫必须带 /T（杀进程树），只杀父进程等于没杀。"""
    cmds: list[list[str]] = []
    monkeypatch.setattr(childproc.subprocess, "run",
                        lambda cmd, **kw: cmds.append(list(cmd)) or
                        types.SimpleNamespace(returncode=0))
    if os.name == "nt":
        assert childproc.taskkill_cmd("gallery-dl.exe") == [
            "taskkill", "/F", "/T", "/IM", "gallery-dl.exe"]
        childproc.sweep_named(timeout=0.5)
        assert cmds and cmds[0][:3] == ["taskkill", "/F", "/T"]
    else:
        assert childproc.taskkill_cmd("gallery-dl.exe") is None
        assert childproc.sweep_named() == 0


def test_request_quit_goes_through_force_quit(monkeypatch):
    """托盘「退出」与菜单「退出」共用同一出口：request_quit 必须调 force_quit。

    ⚠ 用**未绑定调用** + 纯 Python 替身（MainWindow 建不起来），这与 R141 的
    仪器教训同源：判据函数要能在没有真实窗口的对象上跑。
    """
    from media_resource_manager.gui import main_window as mw

    seen: list[object] = []
    monkeypatch.setattr(lifecycle, "force_quit",
                        lambda obj=None, **kw: seen.append(obj))
    win = types.SimpleNamespace(_explicit_quit=False, closed=0, _queue=None)
    win.close = lambda: setattr(win, "closed", win.closed + 1)

    mw.MainWindow.request_quit(win)
    assert win._explicit_quit is True, "不置这个标志会被「缩托盘」分支接管"
    assert win.closed == 1
    assert seen and seen[0] is win, "request_quit 必须把窗口交给 force_quit"


def test_explicit_quit_not_hijacked_by_tray(monkeypatch):
    """_explicit_quit=True 时缩托盘分支必须让位（否则点了退出只藏窗口）。"""
    from media_resource_manager.gui import main_window as mw

    obj = types.SimpleNamespace(_explicit_quit=True, tray=object())
    assert mw._tray_can_reside(obj) is False
    assert mw._tray_hide_on_close(obj, None) is False


# ═══════════════════════════════════════════════════ R178：并发上限 50
def test_parallel_upper_bound_is_50():
    assert MAX_PARALLEL_TASKS == 50
    src = _src("gui/main_window.py")
    assert "setRange(1, MAX_PARALLEL_TASKS)" in src
    assert "setRange(1, 20)" not in src, "写死的旧上限必须删掉"


def test_queue_accepts_50_without_clamp():
    """配置层允许 50：set_concurrency 不许把它 clamp 回 20。"""
    from media_resource_manager.scheduler.queue import TaskQueue

    q = TaskQueue.__new__(TaskQueue)
    q._loop = None
    q.max_concurrent = 1
    q.set_concurrency(MAX_PARALLEL_TASKS)
    assert q.max_concurrent == 50
    q.set_concurrency(999)          # 超出 UI 上限也不许崩，只按原样收
    assert q.max_concurrent >= 50


# ═══════════════════════════════════════════════════ R180：全部开始如实提示
def test_start_all_message_reports_limit_and_queue():
    msg = format_start_all_message(22, 20, 0)
    assert "22" in msg and "20" in msg
    assert "排队 2" in msg, "与并发上限矛盾的老文案必须消失"
    assert "22 个任务都开始" not in msg


def test_start_all_message_all_fit():
    msg = format_start_all_message(3, 20, 0)
    assert "全部立即开始" in msg
    assert "排队" not in msg


def test_start_all_message_counts_already_running():
    msg = format_start_all_message(5, 20, 18)
    assert "当前运行 20" in msg
    assert "排队 3" in msg


def test_start_all_message_empty_and_robust():
    assert "没有可运行" in format_start_all_message(0, 20)
    # 脏输入（None / 字符串 / 负数）不许把 GUI 槽炸掉
    assert isinstance(format_start_all_message(None, None), str)
    assert isinstance(format_start_all_message("5", "10"), str)
    assert isinstance(format_start_all_message(-3, -1), str)


def test_run_all_uses_the_honest_message():
    src = _src("gui/main_window.py")
    assert "format_start_all_message(" in src
    assert "已入队 {len(items)} 个任务（全部开始）" not in src


# ═══════════════════════════════════════════════════ R182：全局快捷键审计
_FORBIDDEN = (
    "SetWindowsHookEx", "LowLevelKeyboard", "WH_KEYBOARD",
    "pynput", "import keyboard", "globalShortcut",
    "ApplicationShortcut",
    # ⚠ grabKeyboard 不在此列：Qt widget 级键盘抓取（只在控件有焦点时
    #   生效，不影响其他程序），与全局 OS 钩子性质完全不同。F19 热键
    #   录制对话框合法使用它。
)


def test_no_global_keyboard_hooks_anywhere():
    """R182 元凶排查结论：全仓零全局键盘钩子（本棒逐仓 grep 实证）。

    这条护栏盯的是**真的不能有**——以后谁要是引入 `pynput` / `SetWindowsHookEx`
    / `Qt.ApplicationShortcut`，这条立刻红。
    """
    for name, text in _all_pkg_sources():
        # childproc/gallery_dl 的注释里会提到这些词？——不许；所以注释也不写。
        for tok in _FORBIDDEN:
            assert tok not in text, f"{name} 引入了全局键盘相关代码：{tok}"


def test_shortcuts_are_widget_or_window_scoped():
    """存在的 QShortcut 必须显式声明非全局上下文（F1/Ctrl+A/Ctrl+Z）。"""
    for name, text in _all_pkg_sources():
        if "QShortcut" not in text:
            continue
        assert "ApplicationShortcut" not in text, f"{name} 用了应用级快捷键"
        if "setContext" in text:
            for ctx in ("WidgetShortcut", "WindowShortcut",
                        "WidgetWithChildrenShortcut"):
                if ctx in text:
                    break
            else:                                    # pragma: no cover
                pytest.fail(f"{name} 的 setContext 用了未知上下文")


def test_no_application_wide_event_filter():
    """不许在 QApplication 上装全局事件过滤器（那会吞掉所有按键）。"""
    for name, text in _all_pkg_sources():
        assert "instance().installEventFilter" not in text, name
        assert "qApp.installEventFilter" not in text, name
