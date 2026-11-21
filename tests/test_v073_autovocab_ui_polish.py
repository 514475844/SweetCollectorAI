"""V0.0.73 护栏：AutoVocab 候选词列表的对齐/空开/溢出修复。

界面体检确认：三张方式卡等高、标签不压字、无横向溢出（这些本来就对）。
真问题是词名区缺少收缩与省略保护，长词会贴住 ✓/✕ 按钮；本轮只修这一处。
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_word_name_has_ellipsis_and_spacing():
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    # 词卡与按钮之间留固定空开
    word_line = [x for x in c.splitlines() if x.strip().startswith(".word {")]
    assert word_line and "gap: 10px" in word_line[0], "词名与按钮之间缺空开"
    # 词名区可收缩，超长词省略而非撑破
    assert ".winfo { min-width: 0; flex: 1; }" in c, "词名区未设收缩，长词会挤按钮"
    b_line = [x for x in c.splitlines() if ".winfo b {" in x]
    assert b_line and all(k in b_line[0] for k in ("overflow: hidden", "text-overflow: ellipsis", "white-space: nowrap")), \
        "词名缺省略号保护"


def test_result_header_spacing():
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    rh = [x for x in c.splitlines() if x.strip().startswith(".res-head {")]
    assert rh and "gap: 10px" in rh[0], "结果标题与计数缺间距保护"


def test_method_cards_still_equal_grid():
    """方式卡仍用等宽 grid（等高对齐），别被这轮改动破坏。"""
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    assert ".methods { display: grid; grid-template-columns: repeat(3, 1fr);" in c


def test_version_and_changelog():
    v = _read("media_resource_manager/__init__.py")
    m = re.search(r'VERSION = "0\.0\.(\d+)"', v)
    assert m and int(m.group(1)) >= 73, "版本号未升到 0.0.73"
    assert "V0.0.73" in _read("CHANGELOG.md"), "更新日志缺 V0.0.73"
