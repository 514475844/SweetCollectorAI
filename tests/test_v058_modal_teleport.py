# -*- coding: utf-8 -*-
"""V0.0.58 护栏：所有模态弹窗必须 Teleport 到 body。

背景（用户报「标题头又成了高亮了」）：
  弹窗就地渲染在 .page 里，dlg 过渡给 .modal 加了 transform，
  于是 fixed 遮罩以 .page 为包含块 → 盖不住 sticky 顶栏（z-index:20），
  顶栏标题「任务」透出来显得还亮着 —— 与 V0.0.46 灯箱同类 bug 复发。
  修法统一：每个全屏遮罩 Teleport 到 body，彻底脱离祖先层叠上下文。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager" / "webapp" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# 每个文件：遮罩 class → 期望至少几个 Teleport（ Tasks 有 5 个弹窗 ）
_CASES = [
    ("views/Tasks.vue", "class=\"mask\"", 5),
    ("components/Onboarding.vue", "class=\"mask\"", 1),
    ("components/ShortcutsDialog.vue", "class=\"mask\"", 1),
    ("components/FolderPicker.vue", "class=\"fp-mask\"", 1),
    ("App.vue", "class=\"quit-mask\"", 1),
]


def test_every_modal_teleported_to_body():
    for rel, mask, min_n in _CASES:
        s = _read(rel)
        n = s.count('<Teleport to="body">')
        assert n >= min_n, f"{rel}: Teleport 只有 {n} 处，期望 ≥{min_n}"
        assert s.count("</Teleport>") == n, f"{rel}: Teleport 开闭不配平"


def test_modal_wraps_the_mask():
    """遮罩根元素必须紧跟在 Teleport 里，不能只在文件别处有个 Teleport 充数。"""
    for rel, mask, _n in _CASES:
        s = _read(rel)
        # Teleport 起始后 300 字内必须出现遮罩 class（弹窗根 div 就在里面）
        import re
        for m in re.finditer(r'<Teleport to="body">', s):
            seg = s[m.end():m.end() + 300]
            assert mask in seg, f"{rel}: 有个 Teleport 没包住遮罩 {mask}"


def test_transition_still_inside_teleport():
    """Tasks 的 dlg 过渡要留在 Teleport 内（顺序 Teleport > Transition）。"""
    s = _read("views/Tasks.vue")
    assert '<Teleport to="body">\n    <Transition name="dlg">' in s, \
        "弹窗过渡没在 Teleport 内，遮罩仍会被 transform 困住"


def test_version_bumped():
    import re
    from media_resource_manager import VERSION
    m = re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 58, f"版本回退：{VERSION}"
    assert f"V{VERSION}" in (ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8"), \
        "打包注释版本与 __init__.py 不一致"
