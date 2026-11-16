"""V0.0.38 留下的三件事（V0.0.39 仍成立）。

用户的原始口径：
  · 「我的启动窗口动画呢」→ 启动画面＋主窗口淡入；
  · 「我都已经选中设置了，为什么任务里面的『全部』还能显示是被选中的」；
  · 限速开关同一项存在两处（一处即时保存、一处要按保存本页）。

⚠ 同版里的「自绘标题栏 / 圆角 / Iconify 四键」已在 V0.0.39 被用户否掉，
判据搬到 test_v037（改回系统窗口）与 test_v039（状态栏完全退出）里。
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


# ───────────────── ① 启动画面 + 淡入 ─────────────────
def test_splash_and_fade_in():
    s = _read("media_resource_manager/gui/web_shell.py")
    assert "_show_splash" in s and "_finish_splash" in s
    assert "QSplashScreen" in s and "正在启动" in s
    assert "splash_enabled" in s, "设置里的「显示启动画面」没接进来"
    assert "windowOpacity" in s, "没有淡入动画"
    assert "loadFinished" in s, "启动画面没等页面首帧就收了（会露出白屏）"
    # 进托盘启动时不闪屏（闪完又消失像出错）
    assert "start_in_tray" in s.split("splash = _show_splash", 1)[0].split(
        "splash = None", 1)[1], "进托盘启动时不该弹启动画面"


# ───────────────── ② 侧栏：「全部」只在任务页高亮 ─────────────────
def test_task_subnav_highlights_only_on_tasks_page():
    app = _web("App.vue")
    assert "onTasksPage" in app, "没有区分「是不是任务页」"
    assert "onTasksPage && taskView === v.k" in app, \
        "子项选中态还是只看 status，站在别的页面也会亮"
    assert "route.path === '/tasks'" in app


# ───────────────── ③ 限速开关只留一处 ─────────────────
def test_rate_plan_switch_defined_once():
    vue = _web("views/Settings.vue")
    assert "'rate_plan_enabled'" not in vue, \
        "「采集与保存」里还留着第二个开关（两处状态会打架）"
    assert "planToggleEnabled" in vue
    # 提醒条要能一键启用，而不是让用户自己去找那个勾
    assert "立即启用" in vue


def test_version_bumped():
    import re as _re
    from media_resource_manager import VERSION
    m = _re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 38, f"版本回退：{VERSION}"
    assert f"V{VERSION}" in _read("build/pyinstaller.spec"), \
        "打包注释版本与 __init__.py 不一致"
