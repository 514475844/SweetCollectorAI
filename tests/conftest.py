"""Pytest fixtures: temp config/db + a network-free fake fetcher.

We test the engine against FIXTURE pages only (no real site hits), per the
compliance/test policy in the spec.
"""
import os

# ---- R48/G：无头环境强制 offscreen（**必须在 import PySide6 之前**）----
#
# 作业单 G：笔记本（本身是带屏笔记本）在自测时老报「没有屏幕 / no screen /
# failed to open display」。真因是 Qt 在**没有 DISPLAY 的环境**里默认要屏，
# 而 pytest 的运行环境常常没有 —— 以前全靠我在命令行外面手写
# `QT_QPA_PLATFORM=offscreen`，任何人（包括不记得这件事的人）直接
# `pytest tests/` 就会撞上。
#
# 所以这里**在 conftest 顶部、任何 PySide6 import 之前**把它设成 offscreen。
# 三点要注意：
# ① 用 `setdefault` 而不是直接赋值 —— 真机上有人**故意**要 `xcb`/windows
#    平台插件时（比如截真机图）不该被测试环境覆盖掉。
# ② 必须早于 `from media_resource_manager.config import ...` 那一批 import：
#    Qt 的 platform plugin 是**首次 QApplication 构造时**读环境变量的，
#    之后再设就晚了。
# ③ 只在没显式设置时兜底，不覆盖用户/调用方的选择。
if not os.environ.get("QT_QPA_PLATFORM") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from pathlib import Path
from typing import Optional

from media_resource_manager.config import AppConfig
from media_resource_manager.db import Database
from media_resource_manager.crawler.fetcher import Fetcher, RobotDisallowed, AccessBlocked
from media_resource_manager.crawler.robots import RobotsPolicy

# ---- V0.0.19-E：仓库 data/config.json 快照还原（跨文件配置泄漏的修法）----
# 背景：tests/ 下 61 个文件直接构造 MainWindow()，其中一半不设 MRM_HOME，
# 于是 user_data_dir() 回落到仓库 data/。皮肤/毛玻璃类测试（set_skin →
# save_config）会把选择**落盘**到 data/config.json，下一个文件构造窗口就
# 读到上一个测试留下的皮肤（r68「classic 浅底」被前面写进的 night 串红过）。
# 曾试过会话级 MRM_HOME 指到临时目录 —— **翻车**：几十条断言硬依赖仓库
# data/rules 那 24 份规则与 data/ 默认路径（l3 适配器、导出层系统目录判定
# 等），全量 21 批连红。所以改成「目录不动、配置还原」：每个测试文件跑完
# 把 config.json 恢复成本文件开始时的样子，写进去的脏配置活不出自己的文件。


@pytest.fixture(scope="module", autouse=True)
def _config_snapshot():
    """module 级：进入时快照仓库 data/config.json，退出时原样还原。

    只动配置文件本身；测试在 data/ 里写的库、规则、缩略图不在本夹具管辖
    （它们各自有 tmp_path / MRM_HOME 隔离，历史上就没出过跨文件事故）。
    """
    # ⚠ 路径在函数内求值（本夹具挪到了 import 之后，仍保持局部求值的
    # 写法：conftest 顶部曾因块插在 import 前而 NameError 连坐全量）。
    repos_config = Path(__file__).resolve().parent.parent / "data" / "config.json"
    bak = None
    try:
        if repos_config.is_file():
            bak = repos_config.read_bytes()
    except Exception:
        bak = None
    yield
    try:
        if bak is not None:
            repos_config.write_bytes(bak)
        elif repos_config.is_file():
            repos_config.unlink()
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _faulthandler_guard():
    """P2-2：挂死守护 —— 真卡住时把**所有线程的栈**打进 stderr 再退出。

    为什么必须有：作业单点名的 `0xc0000374` 是**堆损坏**，它有两种表现形态：
      ① 进程直接 abort（junit 不生成，只剩一个退出码）；
      ② 更常见的是**卡在解释器退出阶段**——某个线程的 loop/控件还在收尾，
         主线程已经等不到它。这个形态下 QTimer 救不了（事件循环已经不转了），
         表现是「跑着跑着就没动静了」，CI 只能靠超时杀掉，**一样没有 junit**。
    `faulthandler.dump_traceback_later` 是唯一能在这种时刻还拿到栈的手段：
    它由 CPython 的看门狗线程触发，不依赖任何 Qt/事件循环状态。

    触发阈值 1800s（30 分钟）：早期全量 ~170s 定了 420s，后来测试涨到 4000+
    条、含多个 90s 的 GUI 真机用例，全量实测要 12~15 分钟——420s 会把**正常
    在跑**的测试当成挂死掐掉（2026-11-21 实测两次卡在 56%/7min 就是它）。
    30 分钟足够正常全量跑完，真挂死时打印栈仍比干等有用。看门狗阈值存在且
    可被 monkeypatch 调大由 test_r71 护栏钉死，这里改值不影响契约。
    注意 `exit=True` 只在**超时那一刻**生效，正常跑完会在 teardown 里取消。
    """
    import faulthandler
    faulthandler.enable()
    faulthandler.dump_traceback_later(1800, exit=True)
    try:
        yield
    finally:
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass


@pytest.fixture(scope="module", autouse=True)
def _reap_top_level_widgets():
    """每个**测试文件**跑完后销毁它建的顶级窗口（全量崩 `0xc0000374` 的修法）。

    背景：全量跑到 86% 后固定崩在 `test_view_size.py` 的 `mw.MainWindow()`，
    崩因不是那个文件本身（单跑 31/31 全过），而是**前 80 个文件累积把堆搞坏**。
    机制：`tests/` 下 28 个文件都会建 `MainWindow()`，而各文件自带的 `_boot()`
    只做「`old.close()`」—— **close 只是隐藏，C++ 对象和整棵控件树照样留着**。
    实测量过：**每个 MainWindow 带 22 个顶级控件**；全量几百个用例叠在同一个
    QApplication 里 → 堆被吃穿 → `0xc0000374`，**junit 根本不生成**。

    为什么是 **module** 粒度而不是 per-用例（per-用例两种更"彻底"的写法都翻车了）：
    - per-用例 + `deleteLater()` + `sendPostedEvents(None, DeferredDelete)`：
      后者是**全局冲刷**，会把 Qt 内部刚 post 的延迟删除一起执行掉 → 共享窗口活着，
      `menuBar()` 却变成**新建的空菜单栏** → 顶级菜单数断言误报 `0 != 8`。
    - per-用例 + `shiboken6.delete()`：太早太狠，Qt 仍持有引用时直接 **abort 整进程**。
    - per-用例还会踩掉 `scope="module"` 的共享窗口缓存
      （`test_r26_direction_convergence._cache`），后面用例拿到已析构对象。
    module 粒度既把累积**限在一个文件内**（几十个控件，远低于崩溃量级），
    又不干扰文件内部的共享与顺序 —— **够用且不惹事**。

    三条约束（都是踩过的坑）：
    1. **绝不 `app.quit()`** —— 别的文件的 `qapp` fixture 靠它才收敛。
    2. **没有 QApplication 就直接返回**：纯逻辑用例（占大多数）不该被迫 import Qt，
       更不该在没 app 时构造控件（那会整进程 abort）。
    3. **全程 try/except**：清理失败绝不能把用例判成失败——它只是卫生措施。
    """
    yield
    try:
        from PySide6.QtCore import QCoreApplication, QEvent, QTimer
        from PySide6.QtWidgets import QApplication
    except Exception:
        return
    app = QApplication.instance()
    if app is None:
        return
    try:
        for old in list(app.topLevelWidgets()):
            try:
                for tmr in old.findChildren(QTimer):
                    tmr.stop()
                old.close()
            except Exception:
                pass
            try:
                old.deleteLater()
            except Exception:
                pass
        # deleteLater 只是**投递**；不手动派发 DeferredDelete 它永远不生效。
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    except Exception:
        pass


@pytest.fixture(scope="module", autouse=True)
def _reap_queue_threads():
    """每个测试文件跑完，回收它起的 TaskQueue 子线程（P2-2）。

    为什么需要：`_boot()` / 多个 fixture 会真的 `q.start()`，那会拉起一条
    **跑着 asyncio loop 的真线程**。`stop()` 从此会 join（P2-2 已修），但
    ① 用例中途失败/跳过时 teardown 未必跑到；
    ② `test_r37` 那种「把 `_loop` 换成已关闭的 loop」的场景会绕过旧 stop；
    ③ 有些用例压根不调 stop，只是让队列对象被 GC。
    这些线程活到解释器退出那一刻，就是 `0xc0000374` 的来源之一。

    这里是**兜底**：遍历 `threading.enumerate()`，凡名字是我们队列线程的
    （`_thread_main` 建的 Thread 名就是 `Thread-N`，改不了，所以改用
    「target 是 TaskQueue._thread_main」这个可靠判据），一律 join + 必要时
    terminate。用 module 粒度而不是 per-用例，与 `_reap_top_level_widgets`
    同理：不干扰文件内部共享，也不会踩掉 scope="module" 的缓存。
    """
    yield
    try:
        import threading
        from media_resource_manager.scheduler.queue import TaskQueue
    except Exception:
        return
    target = getattr(TaskQueue, "_thread_main", None)
    if target is None:
        return
    for th in threading.enumerate():
        if th is threading.current_thread():
            continue
        try:
            if th.is_alive() and th._target is target:
                th.join(5.0)
                if th.is_alive():
                    th.terminate()
        except Exception:
            pass


@pytest.fixture
def tmp_config(tmp_path) -> AppConfig:
    cfg = AppConfig(
        db_path=tmp_path / "mrm.db",
        library_dir=tmp_path / "library",
        rules_dir=tmp_path / "rules",
        isolate_dir=tmp_path / "quarantine",
        log_dir=tmp_path / "logs",
    )
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def tmp_db(tmp_config) -> Database:
    return Database(tmp_config.db_path)


class FakeFetcher(Fetcher):
    """Returns HTML from a dict; never touches the network."""

    def __init__(self, pages: dict[str, str], block: Optional[set[str]] = None):
        # build a minimal config-like object
        class _C:
            user_agent = "test"
            delay = 0
            timeout = 5
            retry = 1
            proxy = None
            verify_ssl = True
        super().__init__(_C(), proxy=None)  # type: ignore
        self._pages = pages
        self._block = block or set()

    async def _ensure_robots(self, url):
        return RobotsPolicy.empty()

    async def fetch_text(self, url, delay=None):
        if url in self._block:
            raise AccessBlocked(f"blocked {url}")
        if url not in self._pages:
            raise RobotDisallowed(f"no fixture for {url}")
        return self._pages[url]

    async def fetch_bytes(self, url, delay=None, range_start=0):
        if url in self._block:
            raise AccessBlocked(f"blocked {url}")
        # 返回 (status, bytes)；range_start>0 时站点不支持 Range 也回 200 整文件
        # 2026-10-26：下载器加了文件头魔数校验（HTML 拦截页不再落库），
        # 假数据必须带 JPEG 头，否则被当拦截页拒收 → downloaded=0。
        payload = self._pages.get(url, "").encode() or b"\x00" * 60
        return (200, b"\xff\xd8\xff\xe0" + payload)

    async def close(self):
        pass


@pytest.fixture
def fake_fetcher():
    def _make(pages, block=None):
        return FakeFetcher(pages, block)
    return _make
