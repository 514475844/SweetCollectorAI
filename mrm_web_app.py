"""SweetWeb.exe 入口 —— V0.0.36 起本软件唯一的可执行体。

起数据库、任务队列、订阅监控与 Web 服务，再开一个内置浏览器窗口。
旧桌面 exe 已废除，这里不再需要「检测桌面程序是否同开」那道拦截；
单实例由 web_shell 的 QLocalServer 负责：第二次双击
会把已经开着的窗口叫出来，而不是再起一份服务共写同一份数据。

带参数运行 = 透传给命令行子命令（proxy / queue / serve / autostart 等），
命令行能力没有因为废除旧 exe 而丢失。
"""
import os
import sys
import time
import traceback
from pathlib import Path


def _log_dir() -> Path:
    try:
        from media_resource_manager.config import user_data_dir
        d = user_data_dir() / "logs"
    except Exception:
        d = Path.home() / "AppData" / "Roaming" / "media-resource-manager" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log(msg: str):
    try:
        with (_log_dir() / "webshell-crash.log").open("a", encoding="utf-8") as f:
            f.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n{msg}\n")
    except Exception:
        pass


def _force_selector_policy():
    """与 mrm_app 同款：Windows 上把 asyncio 切 Selector，避 IOCP 崩溃老坑。"""
    if sys.platform != "win32":
        return
    try:
        import asyncio
        from asyncio import windows_events
        asyncio.set_event_loop_policy(
            windows_events.WindowsSelectorEventPolicy())
    except Exception:
        pass


def main():
    _force_selector_policy()
    # V0.0.27：`--tray` 只表示「这次启动直接进托盘」（开机自启那条路用它），
    # 不是给 CLI 的参数。先摘掉它，再判断剩下的算不算 CLI 调用。
    argv = [a for a in sys.argv[1:] if a != "--tray"]
    tray_only = len(argv) != len(sys.argv) - 1
    if argv:
        # 带参数 → CLI 透传（旧桌面 exe 废除后，命令行入口就在这里）
        from media_resource_manager.__main__ import main as cli_main
        cli_main()
        return
    try:
        from media_resource_manager.gui.web_shell import shell_main
        rc = shell_main(start_in_tray=tray_only)
        if rc != 0:
            from media_resource_manager.gui.web_shell import shell_main as _sm
            reason = getattr(_sm, "error", "") or "原因未知，详见日志。"
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "无法启动网页界面", reason)
        sys.exit(rc)
    except SystemExit:
        # 正常退出路径（shell_main 返回后经 sys.exit 抛出）。曾被下面的
        # BaseException 兜住，导致「关窗退出」也弹一次「启动失败 SystemExit: 0」。
        raise
    except BaseException as e:                        # noqa: BLE001
        _log(traceback.format_exc())
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(
                None, "启动失败",
                f"网页界面启动失败：{type(e).__name__}: {e}\n\n"
                "详情已记录到数据目录 logs/webshell-crash.log。")
        except Exception:
            print(f"WebShell failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
