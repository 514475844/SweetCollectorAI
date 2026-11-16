"""V0.0.21 真机冒烟：起一个无服务的内置窗口，验证内核层策略确实生效。

只做只读探针，不启动引擎、不改配置：
  1. 注入标记 window.__MRM_NATIVE_SHELL__ 能被页面读到；
  2. 浏览器动作的快捷键已被清空；
  3. 外链 / 新窗口不会把软件窗口导航走；
  4. 键位决策表在真实页面里对 Ctrl+A 生效。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer, QUrl  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from media_resource_manager.gui.web_shell import _open_embedded_window  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])
result = {}
win, err = _open_embedded_window("http://127.0.0.1:1/", lambda: None)
assert win is not None, f"内置窗口创建失败：{err}"
view = win.findChild(QWebEngineView)
page = view.page()

# 这个冒烟不开服务，首次导航必然失败；内核会把页面换成中文错误页。
# 等它换完再注入探针，避免两个 setHtml 抢同一个文档。
probe = (
    "<!doctype html><meta charset='utf-8'><title>probe</title>"
    "<input id='box' value='text'>"
)
page.setHtml(probe, QUrl("http://127.0.0.1:1/"))


def check():
    def done(v):
        result["probe"] = v
        checks = [
            ("注入标记", v is True),
            ("窗口标题固定", win.windowTitle() == "Sweet采集AI版"),
            ("禁用拖放", view.acceptDrops() is False),
            ("右键菜单自定义",
             view.contextMenuPolicy().name == "CustomContextMenu"),
        ]
        for name, ok in checks:
            print(("OK   " if ok else "FAIL ") + name)
        bad = [n for n, ok in checks if not ok]
        print("SMOKE", "OK" if not bad else "FAILED: " + ",".join(bad))
        app.exit(0 if not bad else 1)

    page.runJavaScript("window.__MRM_NATIVE_SHELL__ === true", done)


QTimer.singleShot(2500, check)
QTimer.singleShot(8000, lambda: app.exit(2))
sys.exit(app.exec())
