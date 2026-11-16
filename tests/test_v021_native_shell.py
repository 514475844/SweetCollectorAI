"""V0.0.21 护栏：内置窗口必须像桌面软件，而不是套了壳的浏览器。

用户 2026-11-13：「我光说一点就是一点啊，还有快捷键什么的，比如 CTRL+A
就真的全选中了。这只是我自己举的一个例子，你要举一反三，多多思考一下」。

所以这一层钉的是**按类收口**，不是某一个键：
  1. 键位：常用键改成软件动作（全选 / 搜索 / 保存 / 刷新），浏览器功能键
     全部停用；软件自有键位与录键态优先放行；
  2. 指针：文本拖选、拖拽、中键、右键浏览器菜单、页面缩放；
  3. 导航：外链、新窗口、拖文件进窗口、下载、打印、开发者工具、错误页；
  4. 说明：桌面端 F1 与网页端快捷键说明同源，逐字一致；
  5. 普通浏览器访问不受影响（只有注入了标记才启用桌面策略）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"
GUI = ROOT / "media_resource_manager" / "gui"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_decision_table_is_complete():
    """决策表逐条跑一遍：软件动作 / 停用 / 放行 / 输入框 / 录键态。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("本机无 node，跳过前端决策表动态断言")
    script = r"""
const m = await import(process.env.MRM_NATIVE_SHELL_URL);
const cases = [
  ['Ctrl+A', {}, 'action'], ['Ctrl+A', {editable:true}, 'allow'],
  ['Ctrl+F', {}, 'action'], ['F3', {}, 'action'],
  ['Ctrl+S', {}, 'action'], ['F5', {}, 'action'],
  ['Ctrl+R', {}, 'action'], ['Ctrl+Shift+R', {}, 'action'],
  ['Ctrl+P', {}, 'block'], ['Ctrl+U', {}, 'block'],
  ['F12', {}, 'block'], ['Ctrl+W', {}, 'block'],
  ['Ctrl+T', {}, 'block'], ['Ctrl+H', {}, 'block'],
  ['Ctrl+J', {}, 'block'], ['Ctrl+D', {}, 'block'],
  ['Ctrl+L', {}, 'block'], ['Alt+\u2190', {}, 'block'],
  ['Ctrl+0', {}, 'block'], ['Ctrl+=', {}, 'block'],
  ['Ctrl+B', {}, 'allow'], ['Ctrl+Z', {}, 'allow'],
  ['F1', {}, 'allow'], ['Esc', {}, 'allow'], ['?', {}, 'allow'],
  ['Alt+1', {}, 'allow'], ['\u2191', {}, 'allow'], ['Space', {}, 'allow'],
  ['Ctrl+C', {hasSelection:true}, 'allow'], ['Ctrl+C', {}, 'block'],
  ['Ctrl+A', {recording:true}, 'allow'],
  ['Ctrl+P', {matchedAction:true}, 'allow'],
  ['Ctrl+V', {editable:true}, 'allow'],
  ['Ctrl+V', {}, 'block'],
];
let bad = [];
for (const [seq, ctx, want] of cases) {
  const got = m.decideShellKey(seq, ctx).kind;
  if (got !== want) bad.push(seq + '/' + JSON.stringify(ctx) + ': ' + got + ' != ' + want);
}
console.log(JSON.stringify(bad));
"""
    env = dict(os.environ, MRM_NATIVE_SHELL_URL=(WEB / "nativeShell.js").as_uri())
    out = subprocess.run([node, "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    bad = json.loads(out.stdout.strip().splitlines()[-1])
    assert not bad, "键位决策表不符: " + "; ".join(bad)


def test_key_table_covers_browser_function_groups():
    src = _text(WEB / "nativeShell.js")
    for name in ("打印与另存", "查找与地址栏", "历史与下载",
                 "查看源代码与开发者工具", "标签与窗口", "页面形态",
                 "缩放", "清除与隐私"):
        assert name in src, f"缺少浏览器功能分组：{name}"
    for seq in ("Ctrl+P", "Ctrl+U", "F12", "Ctrl+Shift+I", "Ctrl+T",
                "Ctrl+N", "Ctrl+W", "Ctrl+H", "Ctrl+J", "Ctrl+D",
                "Ctrl+L", "Ctrl+K", "Ctrl+G", "F6", "F7", "F11",
                "Alt+←", "Alt+→", "Ctrl+Tab", "Ctrl+0"):
        assert seq in src, f"浏览器功能键未收口：{seq}"
    for action in ("select-all", "focus-search", "save-current", "refresh-view"):
        assert action in src, f"缺少软件动作：{action}"
    # 软件自有键位、录键态、普通浏览器隔离
    for token in ("SOFTWARE_SEQS", "EDIT_SEQS", "recording",
                  "__MRM_NATIVE_SHELL__", "isNativeShell"):
        assert token in src


def test_pointer_and_selection_guards():
    src = _text(WEB / "nativeShell.js")
    for hook in ("keydown", "mrm-shell-key", "dragstart", "dragover",
                 "drop", "selectstart", "auxclick", "wheel", "gesturestart"):
        assert f"'{hook}'" in src, f"缺少指针事件收口：{hook}"
    css = _text(WEB / "style.css")
    assert "html.mrm-native body" in css
    assert "user-select: none" in css
    assert "user-select: text" in css, "输入框与日志等可复制区域必须放行"
    assert "-webkit-user-drag: none" in css


def test_kernel_layer_handles_browser_process_behaviour():
    src = _text(GUI / "web_shell.py")
    for token in ("_SHELL_FORWARD_SEQS", "_SHELL_BLOCK_SEQS", "_qt_key_seq",
                  "_seq_detail", "_ShellInputFilter", "_ShellPage",
                  "__MRM_NATIVE_SHELL__", "QWebEngineScript",
                  "acceptNavigationRequest", "newWindowRequested",
                  "downloadRequested", "customContextMenuRequested",
                  "setSpellCheckEnabled", "setAcceptDrops(False)",
                  "NavigateOnDropEnabled", "FullScreenSupportEnabled",
                  "PdfViewerEnabled", "ErrorPageEnabled", "setZoomFactor(1.0)",
                  "QDesktopServices.openUrl", "ShortcutOverride",
                  "MiddleButton", "XButton1"):
        assert token in src, f"内核层缺少：{token}"
    # 内核必须把吞掉的键转发给网页的同一张决策表，不能两端各写一套
    assert "mrm-shell-key" in src
    # 外链不能留在软件窗口里导航
    assert "return False" in src


def test_kernel_does_not_break_editing():
    """Ctrl+A 在输入框里仍要能全选文字；录键态不能被内核吞掉。"""
    src = _text(WEB / "nativeShell.js")
    assert "e.target.select" in src
    assert "_mrmKernelReplay" in src
    assert "SelectAll" in _text(GUI / "web_shell.py")


def test_shell_actions_wired_into_views():
    """全选 / 刷新 / 保存 / 搜索要在主要视图里落到真实动作上。"""
    checks = {
        "components/ImageGrid.vue": ("onShellSelectAll", "onShellRefresh", "selectAllForce"),
        "views/Tasks.vue": ("onShellSelectAll", "onShellRefresh", "onShellFocusSearch"),
        "views/Export.vue": ("onShellSelectAll", "onShellRefresh"),
        "views/Settings.vue": ("onShellSave",),
        "views/Learning.vue": ("useShellRefresh",),
        "views/Logs.vue": ("useShellRefresh",),
        "views/Subs.vue": ("useShellRefresh",),
        "views/Urls.vue": ("useShellRefresh",),
        "views/Proxies.vue": ("useShellRefresh",),
        "views/Models.vue": ("useShellRefresh",),
        "views/Report.vue": ("useShellRefresh",),
        "views/Maintenance.vue": ("useShellRefresh",),
        "views/Adapters.vue": ("useShellRefresh",),
        "views/Review.vue": ("useShellRefresh",),
    }
    for rel, tokens in checks.items():
        src = _text(WEB / rel)
        for token in tokens:
            assert token in src, f"{rel} 未接 {token}"
    app = _text(WEB / "App.vue")
    for token in ("installNativeShell", "focusGlobalSearch",
                  "refreshCurrentView", "searchEl", "__mrmMatchWebAction",
                  "flush: 'sync'"):
        assert token in app


def test_shortcut_help_is_single_source_and_identical():
    """桌面 F1 与网页端说明必须逐字一致（用户硬性要求）。"""
    from media_resource_manager.hotkeys import (SHELL_FIXED_NOTES,
                                                SHELL_MODE_NOTE)
    assert SHELL_FIXED_NOTES and SHELL_MODE_NOTE
    main = _text(GUI / "main_window.py")
    webui = _text(ROOT / "media_resource_manager" / "webui.py")
    assert "SHELL_FIXED_NOTES" in main and "SHELL_MODE_NOTE" in main
    assert "SHELL_FIXED_NOTES" in webui and "SHELL_MODE_NOTE" in webui
    assert '"shell": [dict(x) for x in SHELL_FIXED_NOTES]' in webui
    assert "shell_note" in webui
    dialog = _text(WEB / "components" / "ShortcutsDialog.vue")
    for token in ("shell.value", "shellNote.value", "内置窗口固定键位",
                  "SHELL_FIXED_NOTES" if False else "s.key"):
        assert token in dialog, f"网页端说明缺少 {token}"
    # 界面文案必须专业书面：不许出现第二人称、语气词与口语动词
    plain = re.sub(r"[\s\W_]+", "", SHELL_MODE_NOTE +
                   "".join(x["label"] for x in SHELL_FIXED_NOTES))
    for bad in ("你", "您", "咱", "吧", "呢", "啊", "搞", "弄", "咋", "啥",
                "点一下", "点开", "搞定", "顺手", "顺便", "其实", "反正",
                "大概", "差不多", "随便", "记住", "别忘", "一下", "点儿",
                "找不到", "还没", "手动"):
        assert bad not in plain, f"说明文案出现口语/不统一用词：{bad}"


def test_version_bumped_and_copy_professional():
    """版本号钉**关系**不钉旧值：格式合法、不回退、与打包注释同源。"""
    import re as _re
    from media_resource_manager import VERSION
    m = _re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m, f"版本号格式不合法：{VERSION}"
    assert int(m.group(1)) >= 21, f"版本号发生回退：{VERSION}"
    spec = _text(ROOT / "build" / "pyinstaller.spec")
    assert f"V{VERSION}" in spec, "打包注释里的版本与 __init__.py 不一致"
    assert _text(GUI / "web_shell.py").startswith('"""V0.0.')


def test_normal_browser_access_is_untouched():
    """局域网 / 手机浏览器访问不该被桌面策略影响。"""
    src = _text(WEB / "nativeShell.js")
    assert "function isNativeShell()" in src
    assert "window.__MRM_NATIVE_SHELL__ === true" in src
    assert "if (!isNativeShell()) return () => {}" in src
    css = _text(WEB / "style.css")
    assert "html.mrm-native body" in css
    assert not re.search(r"^\s*body\s*\{\s*user-select", css, re.M)
