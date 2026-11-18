# -*- coding: utf-8 -*-
"""V0.0.59 护栏：按钮尺寸统一 / 入库百分比口径 / 书面文案。

用户 2026-11-18 22:19 反馈三件事：
  1. AI 模型页「下载」按钮有的大有的小 —— 根因是 .btn.sm 在 12 个页面各写
     一遍、padding 3~5px 不等。统一到全局 style.css，删掉全部局部覆盖。
  2. 入库 35750/100365 显示 7% —— 后端 pct 是整体流程（入库占前 20%），
     前端文案却跟 done/total 并排，口径打架。改为按 done/total 现算。
  3. 「换成这个」口语化 —— 改「设为当前模型」，补进 r240 词表。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "media_resource_manager" / "webapp" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ── ① 按钮尺寸统一到全局 ──────────────────────────────────
def test_global_button_sizes_defined():
    css = _read("style.css")
    assert ".btn.sm {" in css and ".btn.xs {" in css, "全局缺按钮尺寸档位"
    assert "height: 30px" in css and "height: 26px" in css, "按钮高度没定死，仍会高矮不一"


def test_no_local_button_size_overrides():
    """任何页面都不许再写 .btn.sm/.xs 的尺寸覆盖（这是「有的大有的小」的根因）。"""
    import re
    for p in SRC.rglob("*.vue"):
        s = p.read_text(encoding="utf-8")
        # 匹配 .btn.sm { padding ... } 这种局部尺寸定义；允许注释里的说明文字
        for m in re.finditer(r'(?m)^\s*\.btn\.(sm|xs)\s*\{[^}]*padding[^}]*\}', s):
            raise AssertionError(
                f"{p.relative_to(SRC)}: 仍有局部按钮尺寸覆盖，删掉改用全局档位：{m.group(0)[:80]}")


# ── ② 入库百分比按阶段算 ──────────────────────────────────
def test_import_progress_uses_stage_pct():
    vue = _read("components/ImportFolder.vue")
    assert "stagePct" in vue, "缺阶段百分比计算"
    assert "imp.done * 100 / imp.total" in vue, "百分比没按 done/total 现算"
    # 文案和进度条都必须用 stagePct，不能一处用一处不用
    assert "stagePct.value" in vue and "width: Math.max(2, stagePct)" in vue, \
        "百分比文案/进度条没统一到阶段口径"


# ── ③ 书面文案 ────────────────────────────────────────────
def test_model_button_copy_professional():
    vue = _read("views/Models.vue")
    assert "设为当前模型" in vue, "模型切换按钮没改书面语"
    assert "换成这个" not in vue, "口语化文案「换成这个」还在"
    r240 = (ROOT / "tests" / "test_r240_copy_professional.py").read_text(encoding="utf-8")
    assert "换成这个" in r240, "「换成这个」没进口语词表，会回退"


def test_version_bumped():
    import re
    from media_resource_manager import VERSION
    m = re.fullmatch(r"0\.0\.(\d+)", VERSION)
    assert m and int(m.group(1)) >= 59, f"版本回退：{VERSION}"
    assert f"V{VERSION}" in (ROOT / "build" / "pyinstaller.spec").read_text(encoding="utf-8"), \
        "打包注释版本与 __init__.py 不一致"
