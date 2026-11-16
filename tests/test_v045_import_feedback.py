"""V0.0.45 护栏：导入的「点下去必须有反应」，以及退出入口的可见性。

用户三条（原话）：
1.「右下角的退出太不明显了。你想办法解决一下。」
2.「导入你自己检查一下，这功能我用着更不明白了，还不如原来的。」
3.「点了开始导入也没有任何的提示，什么都没有吗」
4.「还有右上角的关闭，现在变成彻底关闭了。没有最小化的托盘。」

第 3 条是**真 Bug**，不是体感：
   ``importJob.js`` 的 ``startImport()`` 里调的是 ``_saveForm()``，
   而文件里只导出 ``saveForm()``。那一行在 async 函数体里最先执行，
   抛出的 ReferenceError 变成没人 await 的 rejection —— 于是点「开始导入」
   既不发请求、也不报错、也不出进度条，界面一动不动。校验就看这条：
   文件里出现的每个 ``_xxx(`` 调用都必须是本文件定义过的名字。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager" / "webapp" / "src"


def _job() -> str:
    return (WEB / "importJob.js").read_text(encoding="utf-8")


def _comp() -> str:
    return (WEB / "components" / "ImportFolder.vue").read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """注释里为了讲清历史而写出那个错名字，不该被静态检查当成真调用。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))


# ───────────────── ① 静态检查：不存在的内部函数 ─────────────────
def test_no_undefined_internal_calls_in_import_job():
    """`_saveForm()` 那种「调了个不存在的函数」不许再出现。

    只查下划线开头的内部 helper：外部 API 是 `api.xxx`、公共导出是
    `export function xxx`，都不以下划线开头，不会误报。
    """
    src = _strip_js_comments(_job())
    called = set(re.findall(r"(?<![\w.])(_[A-Za-z]\w*)\s*\(", src))
    defined = set(re.findall(r"function\s+(_[A-Za-z]\w*)\s*\(", src))
    defined |= set(re.findall(r"(?:const|let|var)\s+(_[A-Za-z]\w*)\s*=", src))
    missing = sorted(called - defined)
    assert not missing, f"importJob.js 调了未定义的内部函数：{missing}"


def test_start_import_saves_form_through_public_name():
    src = _strip_js_comments(_job())
    body = src.split("export async function startImport", 1)[1].split("\n}\n", 1)[0]
    assert "saveForm()" in body, "开始导入前没有把表单落盘"
    assert "_saveForm" not in src, "那个不存在的 _saveForm 又回来了"


# ───────────────── ② 点下去立刻有反馈 ─────────────────
def test_start_sets_starting_flag_before_awaiting():
    """请求发出到后端受理之间有几十毫秒到几十秒（WebDAV 要先列目录），
    这段时间界面必须有「正在准备」，不能一声不响。"""
    src = _job()
    body = src.split("export async function startImport", 1)[1].split("\n}\n", 1)[0]
    i_starting = body.index("imp.starting = true")
    i_first_await = body.index("await api.")
    assert i_starting < i_first_await, "starting 不是在发请求之前亮的"
    # 无论成功失败都要把转圈收掉，否则失败后按钮永远卡在「正在准备…」
    assert "finally" in body and "imp.starting = false" in body
    # 任何异常都要落到 imp.err，不许静默吞掉
    assert "imp.err = _humanErr" in body


def test_component_renders_progress_error_and_result_cards():
    s = _comp()
    # 进度卡：点了就出现（v-if="busy"），带阶段名 + 张数 + 进度条
    assert 'class="state run"' in s and 'class="bar"' in s
    assert "phaseLabel" in s and "countText" in s
    assert "busy = computed" in s and "imp.starting" in s
    # 失败卡与结果卡是分开的两种外观，不再把一串结果拼成一句话
    assert 'class="state bad"' in s and 'class="state good"' in s
    assert "去图库查看" in s, "导入完没有下一步入口"
    assert "重试" in s, "失败后没有重试入口"


def test_progress_fields_come_from_backend():
    """张数/剩余时间/当前文件由后端作业字段来，界面不猜。"""
    w = (ROOT / "media_resource_manager" / "webui.py").read_text(encoding="utf-8")
    assert "done=done, total=total, eta=eta" in w
    assert 'name=str(name or "")[:120]' in w, "作业里没带当前文件名"
    job = _job()
    assert "imp.done = Number(j.done || 0)" in job
    assert "imp.total = Number(j.total || 0)" in job
    assert "imp.curName = j.name || ''" in job


def test_import_panel_is_three_steps():
    """用户「更不明白了」：三步标题 + 选项收拢，不再是说明文字平铺。"""
    s = _comp()
    assert s.count('<span class="step">') == 3, "步骤号不是三步"
    for word in ("图片在哪里", "导入选项", "开始导入"):
        assert word in s, f"缺这一步的标题：{word}"
    # 长的括号解释收进 title，正文只留短标签
    assert 'title="勾选：连同所有层级的子文件夹一起导入' in s
    assert "同时复制一份进软件图库" in s and "（占双份磁盘" not in s, \
        "选项标签又把长解释写回正文了（应放 title）"


# ───────────────── ③ 退出：明显 + 关闭收进托盘 ─────────────────
def test_quit_button_always_shows_text():
    app = (WEB / "App.vue").read_text(encoding="utf-8")
    seg = app.split('<div class="sb-right">', 1)[1].split("</footer>", 1)[0]
    assert "退出软件" in seg and "quit-btn" in seg
    assert "<span v-if=\"quitArmed\">" not in seg, "文字还是只在待确认时才出现"
    assert 'html[data-theme="qbittorrent"] .quit-btn' in app, "深色皮肤没换亮红"


def test_window_close_hides_to_tray_not_quit():
    s = (ROOT / "media_resource_manager" / "gui" / "web_shell.py").read_text(
        encoding="utf-8")
    seg = s.split("class _Win(QMainWindow)", 1)[1].split("class _ShellInputFilter", 1)[0]
    assert "_mrm_hide_to_tray" in seg and "event.ignore()" in seg
    # 托盘不可用时必须退回真退出，不能把窗口藏进一个叫不回来的地方
    assert "if tray is not None else None" in s


def test_version_at_least_v045():
    from media_resource_manager import __version__
    parts = tuple(int(x) for x in __version__.split("."))
    assert parts >= (0, 0, 45), __version__
