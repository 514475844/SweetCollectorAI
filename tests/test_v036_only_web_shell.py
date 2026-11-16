"""V0.0.36：废除旧桌面 exe，全软件只认 SweetWeb.exe（Web 壳）。

用户定稿「从现在开始，废除旧的 exe 改用新版的 web 壳 exe」。
判据钉「唯一入口」这个关系，不钉具体字节数或构建哈希：
  · 打包只出一个 EXE，而且必须是 SweetWeb；
  · 启动链（开机自启 / 快捷方式 / 命令行）全部指向它；
  · 旧 exe 的名字不许再出现在运行时拦截、交付文档和交付目录里。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = "MediaResourceManager.exe"
NEW = "SweetWeb.exe"


def _text(rel: str) -> str:
    p = ROOT / rel
    assert p.is_file(), f"缺文件 {rel}"
    return p.read_text(encoding="utf-8")


# ───────────────────────── 打包：只出一个 exe ─────────────────────────
def test_spec_builds_only_web_shell():
    spec = _text("build/pyinstaller.spec")
    assert "mrm_app.py" not in spec, "还在把旧桌面入口打进包"
    assert "mrm_web_app.py" in spec
    # 只保留一个 EXE() 调用
    assert len(re.findall(r"^\s*exe\w*\s*=\s*EXE\(", spec, re.M)) == 1, \
        "打包定义里不止一个可执行文件"
    exe_block = spec.split("exe_web = EXE(", 1)[1].split("coll = COLLECT(", 1)[0]
    assert 'name="SweetWeb"' in exe_block, "唯一那个 EXE 不叫 SweetWeb"
    assert 'name="MediaResourceManager"' not in exe_block, \
        "EXE 定义里还写着旧名字（COLLECT 的内部目录名不算）"
    # COLLECT 里不许再收旧 exe / 旧 Analysis
    collect = spec.split("coll = COLLECT(", 1)[1]
    assert "exe_web" in collect and "a_web.binaries" in collect
    assert "\n    exe,\n" not in collect and "a.binaries" not in collect


def test_build_driver_expects_web_shell():
    driver = _text("build_nodel.py")
    assert '(nested / "SweetWeb.exe").is_file()' in driver, \
        "拍平判断还在等旧 exe，新包会被当成打包失败"
    assert '_deliver = PathType(distpath) / "MediaResourceManager"' in driver, \
        "COLLECT 目录名没变，部署后处理不该跟着改坏"


# ───────────────────────── 运行时：不再有「桌面版」拦截 ─────────────────────────
def test_web_entry_drops_old_desktop_guard():
    entry = _text("mrm_web_app.py")
    assert OLD not in entry, "还在检测旧 exe 是否同开"
    assert "_desktop_running" not in entry
    assert "网页界面已在运行" not in entry
    assert "--tray" in entry and "start_in_tray" in entry
    assert "cli_main" in entry, "命令行子命令透传不能跟着旧 exe 一起被删"


def test_autostart_always_opens_shell_in_tray():
    a = _text("media_resource_manager/autostart.py")
    cmd = a.split("def _command", 1)[1].split("def startup_dir", 1)[0]
    frozen = cmd.split("if getattr(sys, \"frozen\", False):", 1)[1].split("if exe.name", 1)[0]
    assert '--tray' in frozen and "serve --interval" not in frozen, \
        "冻结态自启还在跑无窗口的 serve（旧 exe 那套）"
    assert "sweetweb" in a.lower(), "注释里应写明唯一 exe 是 SweetWeb"


# ───────────────────────── 交付说明：只教用户点新壳 ─────────────────────────
def test_delivery_docs_point_to_web_shell():
    for rel in ("README.md", "run.bat",
                "docs/Sweet采集AI版_软件介绍.md",
                "docs/Sweet采集AI版使用文档.md",
                "docs/使用步骤说明.md"):
        s = _text(rel)
        assert NEW in s, f"{rel} 没写新壳名"
        assert OLD not in s, f"{rel} 还在教用户双击旧 exe"


def test_desktop_shortcut_points_to_web_shell_if_present():
    """用户机器上真有快捷方式时，必须指向新壳；没有就跳过（保持可移植）。"""
    from media_resource_manager import shortcut
    d = shortcut.desktop_dir()
    if not d:
        return
    lnk = d / f"{shortcut.APP_TITLE}.lnk"
    if not lnk.is_file():
        return
    b = lnk.read_bytes()
    assert NEW.encode("utf-16-le") in b or NEW.encode("ascii") in b, \
        "桌面快捷方式还指向旧 exe"
    assert OLD.encode("utf-16-le") not in b and OLD.encode("ascii") not in b, \
        "桌面快捷方式里还留着旧 exe 的名字"


def test_version_and_single_entry_are_documented():
    from media_resource_manager import VERSION
    assert re.fullmatch(r"0\.0\.(\d+)", VERSION) and int(VERSION.rsplit(".", 1)[1]) >= 36
    spec = _text("build/pyinstaller.spec")
    assert f"V{VERSION}" in spec, "打包注释里的版本要和 __init__.py 一致"
    assert "V0.0.36" in spec and "废除旧桌面 exe" in spec
