"""Executable entry point.

Double-click  -> launches the desktop GUI (no console window).
With arguments -> runs the CLI exactly like `python -m media_resource_manager`.
"""
import os
import sys
import traceback
from pathlib import Path

try:
    import asyncio
except Exception:      # pragma: no cover - 任何 Python 3 都该有
    asyncio = None


def _crash_log() -> Path:
    try:
        from media_resource_manager.config import user_data_dir
        d = user_data_dir() / "logs"
    except Exception:
        d = Path.home() / "AppData" / "Roaming" / "media-resource-manager" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "gui-crash.log"


def _log_crash(exc: BaseException):
    try:
        with _crash_log().open("a", encoding="utf-8") as f:
            f.write(f"\n=== {time_now()} ===\n")
            f.write(traceback.format_exc())
    except Exception:
        pass


def time_now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _install_crash_forensics():
    """任何崩溃都留下证据（2026-09-30 删除任务闪退排查后加装）。

    * faulthandler: 原生崩溃(访问冲突等)时把所有线程的 Python 调用栈写进
      logs/gui-native.log —— PySide6/Qt 层的 use-after-free 也能抓到栈。
    * sys.excepthook / threading.excepthook: 未捕获异常（含 Qt 槽里被打印到
      不可见 stderr 的那些）追加到 logs/gui-crash.log。
    安装失败绝不影响正常启动。
    """
    try:
        import faulthandler
        import sys as _sys
        import threading as _threading
        d = _crash_log().parent
        d.mkdir(parents=True, exist_ok=True)

        try:
            f = open(d / "gui-native.log", "a", encoding="utf-8")
            f.write(f"\n=== session {time_now()} ===\n")
            f.flush()
            faulthandler.enable(file=f)
            # 挂起 10 秒以上疑似卡死时自动 dump 全部线程栈（不退出）
            faulthandler.dump_traceback_later(600, repeat=True, file=f)
        except Exception:
            pass

        def _log(kind, tb: str):
            try:
                with (d / "gui-crash.log").open("a", encoding="utf-8") as f:
                    f.write(f"\n=== {kind} {time_now()} ===\n{tb}\n")
            except Exception:
                pass

        prev_hook = _sys.excepthook

        def _exc_hook(t, v, tb):
            _log("exception", "".join(
                __import__("traceback").format_exception(t, v, tb)))
            # 未捕获异常也给用户一个**中文**提示，不裸奔英文 traceback
            # （用户反复要求"所有报错/所有文字都中文"）。详情已由 _log 落
            # gui-crash.log；这里只给一句人话 + 错误类型中文名。
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                if QApplication.instance() is not None:
                    QMessageBox.critical(
                        None, "出现了一个错误",
                        f"软件遇到一个意外错误，已记录到日志文件。\n"
                        f"错误类型：{getattr(t, '__name__', t)}\n\n"
                        f"如果它反复出现，请把数据目录下的 logs 文件夹发给开发者。")
            except Exception:
                pass
            try:
                prev_hook(t, v, tb)
            except Exception:
                pass
        _sys.excepthook = _exc_hook

        def _thread_hook(args):
            import traceback as _tb2
            _log("thread-exception", "".join(
                _tb2.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)))
        _threading.excepthook = _thread_hook
    except Exception:
        pass


def _force_selector_policy():
    """全局把 asyncio 切成 Selector 事件循环，别让任何子进程再去开 IOCP。

    r38：默认 policy 在 Windows 上给的是 `WindowsProactorEventLoopPolicy`，
    IOCP 的等待线程要走 COM，子线程里偶发 `0x8001010d` 直接 fatal（见
    scheduler/queue.py::make_loop）。这里在入口一次切掉，scheduler 的
    `asyncio.run()`、browser_cookie 的采集也一起受益。全项目不用
    `asyncio.create_subprocess_*`，Selector 的短板用不到。
    """
    if sys.platform != "win32":
        return
    try:
        # Python 3.13 里 asyncio 顶层没有 WindowsSelectorEventPolicy，
        # 它在 asyncio.windows_events 下（asyncio.__init__ 的 __all__ 没导出它）。
        from asyncio import windows_events
        asyncio.set_event_loop_policy(
            windows_events.WindowsSelectorEventPolicy())
    except Exception:
        pass


def main():
    _install_crash_forensics()
    _force_selector_policy()
    if len(sys.argv) <= 1:
        # No args -> GUI mode (friendlier for double-clicking the .exe)
        try:
            from media_resource_manager.gui import launch
            launch()
            # r39d：**收尾不交回解释器做 finalization**。
            # closeEvent 里已经把该做的都做了（存 UI 状态 / 停三路后台 / 等
            # 8 个后台 QThread 收工 / 退 Engine 线程池 / 关库）。但只要收尾阶段
            # 还有后台线程在执行 Python（engine 自持线程池的 worker、或者任何一个
            # 后台 QThread），解释器 finalization 就会以 0xC0000409
            # （STATUS_STACK_BUFFER_OVERRUN，退出码 3221226505）无声带走整个进程，
            # gui-crash.log 一个字都不写 —— 真机按钮扫描复现过 5 次。
            # 宁可跳过 atexit，也不能让解释器在收尾期炸。
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)
        except BaseException as e:                 # noqa: BLE001 - keep evidence
            _log_crash(e)
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication.instance() or QApplication([])
                QMessageBox.critical(None, "启动失败", f"{type(e).__name__}: {e}")
            except Exception:
                print(f"GUI failed: {e}")
        return
    from media_resource_manager.__main__ import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
