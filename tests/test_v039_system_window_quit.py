"""V0.0.39：自绘窗口退回系统窗口，右下角两个小于号换成「完全退出」。

用户原话：
  · 「算了，好丑，不要自绘窗口了，改回原来的窗口。」
  · 「把右下角的那两个小于号缩进，改成完全退出，至于怎么个按键法，你自己考虑。」

设计决定（写下来免得以后被"优化"回去）：
  · 系统标题栏的 ✕ 一开始是「彻底退出」（V0.0.36 口径）；V0.0.45 用户看到实物后
    要求改回「收进托盘」（见 test_v037 的 test_window_close_hides_to_tray），
    「缩小到托盘」不再只藏在托盘菜单里。
  · 状态栏那个按钮是**两步确认**：点一下变红显示「确认退出」，3 秒内再点
    一次才真退。状态栏是最容易误点的地方，一步直退迟早会关掉正在跑的采集。
  · 退出请求走 HTTP → 只 set 一个 threading.Event，Qt 侧 300ms 轮询后
    走同一条退出链。HTTP 线程里直接碰 Qt 会偶发崩溃/退不干净。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager"
WEB = SRC / "webapp" / "src"


def _read(rel: str) -> str:
    p = ROOT / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


def _web(rel: str) -> str:
    p = WEB / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


# ───────────────── ① 退回系统窗口 ─────────────────
def test_window_is_system_framed_again():
    s = _read("media_resource_manager/gui/web_shell.py")
    assert "FramelessWindowHint" not in s, "还是无边框自绘窗口"
    assert "setMask" not in s, "窗口遮罩（圆角）该一起撤掉"
    assert "class _TitleBar" not in s and "class _IconButton" not in s, \
        "自绘标题栏的类还留着"
    # 网页区直接铺满客户区，不再套标题栏的竖排容器
    assert "win.setCentralWidget(view)" in s
    # 图标还在（系统标题栏左上角/任务栏用它）
    assert "setWindowIcon" in s


def test_shell_icons_module_removed():
    """自绘标题栏没了，那套 SVG 图标模块不该留成死代码。"""
    assert not (SRC / "gui" / "shell_icons.py").exists()


# ───────────────── ② 状态栏「完全退出」 ─────────────────
def test_status_bar_has_quit_button_instead_of_collapse():
    app = _web("App.vue")
    seg = app.split('<div class="sb-right">', 1)[1].split("</footer>", 1)[0]
    assert "askQuit" in seg, "状态栏没有完全退出按钮"
    assert "确认退出" in seg, "没有二次确认的提示"
    assert "chevrons-left" not in seg, "两个小于号（收起侧栏）还在"
    # 两步确认：先 arm，再在时限内第二次点击才发请求
    script = app.split("async function askQuit", 1)[1].split("\n}\n", 1)[0]
    assert "quitArmed.value = true" in script and "setTimeout" in script
    assert "api.appQuit()" in script
    # 侧栏折叠还有别的入口（左上角按钮 + Ctrl+B），功能没丢
    assert "toggleSide" in app and "side-btn" in app


def test_quit_api_and_hook():
    w = _read("media_resource_manager/webui.py")
    assert '"/api/app/quit"' in w, "后端没有退出路由"
    assert "def _api_app_quit" in w
    assert "self.quit_hook = None" in w
    assert "_mrm_webserver" in w.split("def _api_app_quit", 1)[1].split("def ", 1)[0], \
        "退出接口没从 server 上找钩子"
    # 只设事件，不在 HTTP 线程碰 Qt（注释与说明文字不算）
    import re as _re
    body = w.split("def _api_app_quit", 1)[1].split("\n    def ", 1)[0]
    code = _re.sub(r'""".*?"""', "", body, flags=_re.S)
    code = "\n".join(l for l in code.splitlines()
                     if not l.strip().startswith("#"))
    assert "app.quit" not in code and ".close()" not in code

    s = _read("media_resource_manager/gui/web_shell.py")
    assert "quit_hook = quit_evt.set" in s, "壳没把退出钩子挂到服务上"
    assert "_quit_poll" in s and "is_set()" in s, "Qt 侧没有轮询退出事件"
    assert "import threading" in s


def test_api_client_has_app_quit():
    js = _web("api.js")
    assert "appQuit" in js and "/api/app/quit" in js


def test_version_bumped():
    import re as _re
    from media_resource_manager import VERSION
    m = _re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 39, f"版本回退：{VERSION}"
    assert f"V{VERSION}" in _read("build/pyinstaller.spec"), \
        "打包注释版本与 __init__.py 不一致"
