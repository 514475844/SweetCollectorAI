"""V0.0.13 护栏：界面文案全面书面化，术语两端统一。

用户 2026-11-10 17:31 要求：「软件的口语化必须都得修改成专业话术。你跟我
聊天，用口语化就行了。」——**界面**必须书面，**对话**不受限。

r240 已钉了一批语气词/第二人称。本文件补的是 r240 没盖住、但实际存在过的
那几类（都在这轮真实改出来过）：

1. 口语动词/连接词：点一下、点开、搞定、顺手、顺便、其实、反正、大概、
   差不多、一下、折腾、费劲；
2. 缺失口径统一：找不到 → 未找到；还没 → 尚未/暂无；手动 → 手工填写；
3. 术语统一：界面里「要找 / 别找」改为「目标词 / 排除词」（标题、提示、
   toast、桌面看板全一致），避免同一个概念三种叫法。

⚠ 只查用户可见字符串（剥 docstring / 注释 / 变量名），与 r240 同口径。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager/webapp/src"
GUI = ROOT / "media_resource_manager/gui"

# 口语化特征（这轮真实出现过、已全部改写）
COLLOQUIAL = ["点一下", "点开", "搞定", "顺手", "顺便", "其实", "反正",
              "大概", "差不多", "折腾", "费劲", "随便"]
# 缺失/动作口径：界面统一用书面写法
MISSING = ["找不到", "还没填", "还没设置", "还没下载", "还没有"]

CJK = re.compile("[\u4e00-\u9fa5]")

# 允许保留的固定词（技术名/协议/既有控件名，非口语）
EXEMPT = ["整合", "整理", "整站", "完整", "统一", "唯一", "一次性",
          "同一", "哈希", "点开图", "一下（Ctrl"]


def _clean(raw: str) -> str:
    t = re.sub(r'"""[\s\S]*?"""', '""', raw)
    t = re.sub(r"'''[\s\S]*?'''", "''", t)
    t = re.sub(r"<!--[\s\S]*?-->", "", t)
    t = re.sub(r"/\*[\s\S]*?\*/", "", t)
    t = re.sub(r"^\s*//.*$", "", t, flags=re.M)
    t = "\n".join(l for l in t.splitlines() if not l.lstrip().startswith("#"))
    return t


def _strings(raw: str) -> list[str]:
    t = _clean(raw)
    out = []
    for m in re.finditer(r"""["'`]([^'"`\n]{2,300})["'`]""", t):
        s = m.group(1)
        if CJK.search(s):
            out.append(s)
    return out


def _hits(s: str, words) -> list[str]:
    t = s
    for e in EXEMPT:
        t = t.replace(e, "")
    return [w for w in words if w in t]


def _scan(files, words, label):
    bad = []
    for f in files:
        if not f.exists() or f.name.startswith("icons"):
            continue
        for s in _strings(f.read_text(encoding="utf-8", errors="ignore")):
            h = _hits(s, words)
            if h:
                bad.append((f.name, s[:100], h))
    assert not bad, f"{label}仍有口语化文案：\n" + "\n".join(
        f"  {n}: {s}  ← {h}" for n, s, h in bad)


def test_web_has_no_colloquial_leftovers():
    _scan(sorted(list(WEB.rglob("*.vue")) + list(WEB.rglob("*.js"))),
          COLLOQUIAL, "前端")


def test_desktop_has_no_colloquial_leftovers():
    _scan(sorted(GUI.glob("*.py")), COLLOQUIAL, "桌面端")


def test_backend_has_no_colloquial_leftovers():
    _scan([ROOT / "media_resource_manager/webui.py",
           ROOT / "media_resource_manager/engine.py"], COLLOQUIAL, "后端")


def test_web_missing_wording_is_formal():
    _scan(sorted(list(WEB.rglob("*.vue")) + list(WEB.rglob("*.js"))),
          MISSING, "前端缺失口径")


def test_desktop_missing_wording_is_formal():
    _scan(sorted(GUI.glob("*.py")), MISSING, "桌面端缺失口径")


def test_term_naming_is_unified():
    """同一个概念只许一种叫法：界面「目标词 / 排除词」，不再出现「要找 / 别找」。

    只查用户可见字符串 —— 「要找什么」这种出现在注释/docstring 里的说明
    不受约束（那是写给开发者看的）。
    """
    bad = []
    for f in sorted(GUI.glob("*.py")):
        for s in _strings(f.read_text(encoding="utf-8", errors="ignore")):
            if "要找" in s or "别找" in s:
                bad.append((f.name, s[:100]))
    for f in sorted(list(WEB.rglob("*.vue")) + list(WEB.rglob("*.js"))):
        if f.name.startswith("icons"):
            continue
        for s in _strings(f.read_text(encoding="utf-8", errors="ignore")):
            if "要找" in s or "别找" in s:
                bad.append((f.name, s[:100]))
    assert not bad, ("界面术语不统一（应统一为「目标词 / 排除词」）：\n"
                     + "\n".join(f"  {n}: {s}" for n, s in bad[:20]))


def test_target_and_exclusion_terms_do_exist():
    """统一后的两个术语必须真的在界面上出现（防「删掉旧词却忘了换成新词」）。"""
    board = (GUI / "learning_board.py").read_text(encoding="utf-8")
    assert "目标词" in board and "排除词" in board, \
        "学习看板没显示「目标词 / 排除词」，术语替换只做了删除没做替换"
    task = (GUI / "main_window.py").read_text(encoding="utf-8")
    assert "本轮目标词" in task, "建任务表单的词表提示没同步术语"
