"""r240 护栏：界面文案必须专业书面（禁口语）。

用户 2026-10-28 要求：「检查一下软件内所有的文字，不要有口语化，要专业的。」

这一条最容易回退 —— 写新功能时顺手一句「点一下试试」「随便起个名」，
没有任何测试会红，等用户看到才发现。所以把**词表**钉在这里。

只查**用户可见**的字符串：
- 前端：`<template>` 文本节点、JS 字符串字面量、title/placeholder/alt；
- 桌面端与后端：Python 字符串字面量，且**剥掉 docstring 与 # 注释**
  （注释是给开发者看的，不受此约束）。

⚠ 中文没有词边界，所以「整理 / 整站 / 整张图 / 完整 / 整体」这类正常词
会命中「整」。这里用 EXEMPT 先剥掉再匹配，避免天天误报导致测试被无视。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "media_resource_manager/webapp/src"

# 口语特征词（按类分组，便于报错时看懂属于哪一类问题）
BAD_WORDS = {
    "第二人称": ["你", "您", "咱", "俺"],
    "语气词": ["吧", "呢", "啊", "呀", "哈", "呗", "嘛", "喔"],
    "口语动词": ["搞", "弄", "咋", "啥"],
    "夸张夸张": ["千万", "绝对", "简直", "超级", "无敌", "翻车", "砸站", "手滑"],
    "对话式": ["随便", "记住", "别忘", "一下", "点儿", "顺手", "顺便",
               "其实", "反正", "大概", "差不多", "偷懒", "神器", "一键"],
    "比喻口语": ["白费", "瞎", "乱杀", "骗人", "吓人"],
    # V0.0.48：这轮文案审计新清掉的口语（原词表漏网，被用户当场指出过）。
    # 只收**精确短语**，不收单字「点/更/最/什么」——那些在正常书面语里也用。
    "指令口语": ["点「", "点开", "点一下", "点选", "选「", "试试", "试着",
               "换个词", "问原因", "只管", "不管筛", "拿不准", "不知道选",
               "什么都不用动", "默认就是这个", "比①", "同上的", "准一点",
               "觉得判错", "真不要了", "怎么写", "怎么分", "做什么的"],
    "夸张门槛": ["最低 GPU", "最低 CPU", "基本跑不动", "必须有独显",
               "GTX 10", "RTX 30", "i3-4160", "i5-4460", "i7-4770"],
}

# 正常技术词，先剥掉再匹配（否则「整理」「整站」「整张图」天天红）
EXEMPT = ["整理", "整站", "整张", "整体", "整个", "完整", "整数", "调整",
          "整齐", "整除", "一次性", "同一", "统一", "唯一",
          # 技术词，含"哈"但不是语气词（误报过一次）
          "哈希"]

CJK = re.compile("[\u4e00-\u9fa5]")


def _scan_text(text: str) -> list[tuple[str, str]]:
    """返回 [(类别, 命中片段)]；EXEMPT 里的词先剔除。"""
    t = text
    for w in EXEMPT:
        t = t.replace(w, "")
    out = []
    for cat, words in BAD_WORDS.items():
        for w in words:
            if w in t:
                i = t.index(w)
                out.append((cat, w))
    return out


def _strip_js_comments(t: str) -> str:
    t = re.sub(r"<!--[\s\S]*?-->", "", t)
    t = re.sub(r"/\*[\s\S]*?\*/", "", t)
    t = re.sub(r"^\s*//.*$", "", t, flags=re.M)
    return t


def _js_user_strings(raw: str) -> list[str]:
    """前端里用户能看到的中文片段。"""
    t = _strip_js_comments(raw)
    tpl = "".join(re.findall(r"<template>([\s\S]*?)</template>", t))
    scr = "".join(re.findall(r"<script[^>]*>([\s\S]*?)</script>", t)) or t
    out = []
    for m in re.finditer(r">([^<>{}\n]{2,300})<", tpl):
        s = re.sub(r"\{\{[\s\S]*?\}\}", "", m.group(1)).strip()
        if s:
            out.append(s)
    for m in re.finditer(r'(?:title|placeholder|alt|aria-label)="([^"]{2,300})"', tpl):
        out.append(m.group(1))
    for src in (tpl, scr):
        for m in re.finditer(r"""['"`]([^'"`\n]{2,300})['"`]""", src):
            out.append(m.group(1))
    return [s for s in out if CJK.search(s)]


def _py_user_strings(raw: str) -> list[str]:
    """Python 里用户能看到的中文片段（剥 docstring 与整行注释）。"""
    t = re.sub(r'"""[\s\S]*?"""', '""', raw)
    t = re.sub(r"'''[\s\S]*?'''", "''", t)
    t = "\n".join(ln for ln in t.splitlines()
                  if not ln.lstrip().startswith("#"))
    out = []
    for m in re.finditer(r"""["']([^"'\n]{2,300})["']""", t):
        s = m.group(1)
        if CJK.search(s):
            out.append(s)
    return out


# --------------------------------------------------------------- 前端

def test_web_copy_has_no_colloquial_words():
    bad = []
    for f in sorted(list(WEB.rglob("*.vue")) + list(WEB.rglob("*.js"))):
        if f.name.startswith("icons"):          # SVG 数据，无文案
            continue
        for s in _js_user_strings(f.read_text(encoding="utf-8")):
            hits = _scan_text(s)
            if hits:
                bad.append((str(f.relative_to(WEB)), s[:90], hits[:3]))
    assert not bad, "前端出现口语化文案：\n" + "\n".join(
        f"  {f}: {s}  ← {h}" for f, s, h in bad)


# --------------------------------------------------------------- 桌面端

GUI_FILES = ("main_window.py", "dislike_dialog.py", "learning_board.py",
             "review_tab.py", "hotkey_dialog.py", "lightbox.py")


def test_desktop_gui_copy_has_no_colloquial_words():
    bad = []
    for name in GUI_FILES:
        f = ROOT / "media_resource_manager/gui" / name
        if not f.exists():
            continue
        for s in _py_user_strings(f.read_text(encoding="utf-8")):
            hits = _scan_text(s)
            if hits:
                bad.append((name, s[:90], hits[:3]))
    assert not bad, "桌面端出现口语化文案：\n" + "\n".join(
        f"  {f}: {s}  ← {h}" for f, s, h in bad)


# --------------------------------------------------------------- 后端（会显示在网页上）

BACKEND_FILES = ("webui.py", "engine.py", "subscribe.py", "proxy.py",
                 "models_lib.py", "tls.py", "nets.py", "ipfilter.py",
                 "cleanup.py", "watchfolder.py")


def test_backend_user_facing_copy_has_no_colloquial_words():
    bad = []
    for name in BACKEND_FILES:
        f = ROOT / "media_resource_manager" / name
        if not f.exists():
            continue
        for s in _py_user_strings(f.read_text(encoding="utf-8")):
            hits = _scan_text(s)
            if hits:
                bad.append((name, s[:90], hits[:3]))
    assert not bad, "后端用户可见文案出现口语化：\n" + "\n".join(
        f"  {f}: {s}  ← {h}" for f, s, h in bad)


# --------------------------------------------------------------- 风格一致性

def test_error_messages_use_professional_phrases():
    """几个高频句式：用「未 / 无 / 请」而不是「没 / 别 / 点一下」。"""
    web = (ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
    # 空状态与错误提示里不许出现「还没有」
    assert "还没有常用网址" not in web
    assert "还没有任务" not in web


def test_placeholder_uses_professional_tone():
    """占位文字不许出现「随便」「比如」这种聊天语气。"""
    t = (WEB / "views/Proxies.vue").read_text(encoding="utf-8")
    assert "随便起个名" not in t
    assert "名称（例如" in t


def test_empty_states_say_zan_wu():
    """空状态统一用「暂无 …」，不用「还没有 …」。"""
    files = ["views/Tasks.vue", "views/Urls.vue", "views/Logs.vue",
             "views/Learning.vue", "views/Adapters.vue"]
    for rel in files:
        t = (WEB / rel).read_text(encoding="utf-8")
        assert "还没有" not in t, f"{rel} 仍在用「还没有」，应改为「暂无」"


# --------------------------------------------------------------- 内部单号

#: 内部单号格式：R174 / R48-D1 / #42（后跟中文标点或中文，说明是说明性文字）
_INTERNAL_ID = re.compile(r"(?<![A-Za-z0-9])R\d{2,3}(?![\d])"
                          r"|#\d{1,3}\s*(?:[，。、）)】]|[\u4e00-\u9fa5])")


def test_user_facing_copy_has_no_internal_ids():
    """界面文字里不许出现内部单号（用户看不懂，也没有意义）。

    ⚠ 业务编号不受影响：「任务编号 #12」里的 #12 是数据，不是单号。
    这里是启发式：`#12 ` 后紧跟中文或中文标点时才算违规。
    """
    bad = []
    for name in GUI_FILES:
        f = ROOT / "media_resource_manager/gui" / name
        if not f.exists():
            continue
        for s in _py_user_strings(f.read_text(encoding="utf-8")):
            m = _INTERNAL_ID.search(s)
            if m:
                bad.append((name, s[:90], m.group(0)))
    for f in sorted(WEB.rglob("*.vue")):
        for s in _js_user_strings(f.read_text(encoding="utf-8")):
            m = _INTERNAL_ID.search(s)
            if m:
                bad.append((f.name, s[:90], m.group(0)))
    assert not bad, "界面文案里出现内部单号：\n" + "\n".join(
        f"  {f}: {s}  ← {i}" for f, s, i in bad)


def test_watermark_tooltip_is_professional():
    """水印滑条提示曾带「#42 临时滑条…拖到多少就是多清楚」这种口语+单号。"""
    t = (ROOT / "media_resource_manager/gui/main_window.py").read_text(encoding="utf-8")
    assert "拖到多少就是多清楚" not in t
    assert "#42 临时滑条" not in t
