# -*- coding: utf-8 -*-
"""R152-A10 死代码清理护栏（2026-10-23，R226 轮）。

A10 作业单原文：live_wall(:3171)、_gal_timer(:3185)、web_settings_dialog(:3575
无调用点)、不可达代码(:10873-10879)、黑名单追溯死代码(:7967-7970)——删或注释
写明为何留。

本轮（R226）处置口径：
- web_settings_dialog：早前批次已整删（全仓 0 引用）；
- live_wall / _gal_timer：按「注释写明为何留」保留（老配置兼容，不再启动）；
- 不可达代码 ×2：engine._first_in_text 尾部重复实现、
  main_window._hide_to_tray_on_close 的 R138 前旧实现 —— 本轮删除；
- 黑名单追溯死代码：CrawlEngine._to_quarantine（追溯改「审核替代隔离」后
  全仓零调用，含 tests/tools、含 getattr 动态引用）—— 本轮删除。

R73 口径：绿的不算数。每条判据都要能被对应变异打死：
- M1 任意函数 return/raise/break/continue 后再放语句 → test_no_unreachable 红；
- M2 把 _to_quarantine 加回 CrawlEngine → test_to_quarantine_absent 红；
- M3 在 _hide_to_tray_on_close 的 return 后补旧实现 → test_hide_to_tray 红；
- M4 删掉 toggle_live_wall（老配置兼容空实现）→ test_live_wall_compat 红。
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "media_resource_manager"
ENGINE = PKG / "engine.py"
MAIN_WINDOW = PKG / "gui" / "main_window.py"

TERM = (ast.Return, ast.Raise, ast.Break, ast.Continue)


# ---------------------------------------------------------------- 工具 --
def _scan_block(stmts, out):
    """块内出现「终结语句后还有语句」即记入 out（每块只记第一处）。"""
    seen_term = False
    for i, s in enumerate(stmts):
        if seen_term:
            out.append(getattr(s, "lineno", 0))
            seen_term = False
        if isinstance(s, TERM) and i + 1 < len(stmts):
            seen_term = True
        for blk in _child_blocks(s):
            _scan_block(blk, out)


def _child_blocks(s):
    out = []
    for attr in ("body", "orelse", "finalbody"):
        blk = getattr(s, attr, None)
        if isinstance(blk, list):
            out.append(blk)
    for h in getattr(s, "handlers", []) or []:
        out.append(h.body)
    for case in getattr(s, "cases", []) or []:
        out.append(case.body)
    return out


def _func_src(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    return None


# ---------------------------------------------------------------- L2 AST --
def test_no_unreachable_statements_in_package():
    """全包任何 .py 里不许有「终结语句之后还有语句」。

    变异 M1：在任意函数 return 后补一行语句，本判据立即红。
    """
    bad = []
    for p in sorted(PKG.rglob("*.py")):
        if "__pycache__" in str(p):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        out: list = []
        _scan_block(tree.body, out)
        for ln in out:
            bad.append(f"{p.relative_to(ROOT)}:{ln}")
    assert not bad, f"存在不可达语句: {bad}"


def test_to_quarantine_absent():
    """CrawlEngine._to_quarantine（黑名单追溯死代码）必须保持缺席。

    变异 M2：把方法加回 Engine，本判据红。
    （子串判据只认 ``def _to_quarantine``——engine.py 的历史注记注释里
    允许出现方法名本身，R73 教训：别让判据被注释骗到。）
    """
    src = ENGINE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names = {n.name for n in node.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert "_to_quarantine" not in names, \
                f"{node.name} 里不应再有 _to_quarantine"
    assert "def _to_quarantine" not in src, \
        "engine.py 里不应再定义 _to_quarantine"


def test_hide_to_tray_on_close_is_single_return():
    """_hide_to_tray_on_close 除 docstring 外只许一条 return（委托模块级函数）。

    变异 M3：在 return 后补回 event.ignore 旧实现，本判据红。
    """
    tree = ast.parse(MAIN_WINDOW.read_text(encoding="utf-8"))
    fn = _func_src(tree, "_hide_to_tray_on_close")
    assert fn is not None, "_hide_to_tray_on_close 不应被删"
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant):
        body = body[1:]                      # 去掉 docstring
    assert len(body) == 1 and isinstance(body[0], ast.Return), \
        "_hide_to_tray_on_close 除 docstring 外应只剩一条 return 委托语句"
    # 委托目标必须是模块级 _tray_hide_on_close（R117 替身测试的契约）
    call = body[0].value
    assert isinstance(call, ast.Call)
    assert getattr(call.func, "id", "") == "_tray_hide_on_close"


def test_live_wall_compat_kept():
    """toggle_live_wall 空实现必须保留且 docstring 说明为何留（老配置兼容）。

    变异 M4：删掉方法或清空 docstring 的「保留」说明，本判据红。
    """
    tree = ast.parse(MAIN_WINDOW.read_text(encoding="utf-8"))
    fn = _func_src(tree, "toggle_live_wall")
    assert fn is not None, "toggle_live_wall 不应被删（老配置/外部脚本兼容）"
    doc = ast.get_docstring(fn) or ""
    assert "保留" in doc, "docstring 必须写明这是故意保留的兼容空实现"
    # 行为契约：只记配置、不抛错
    assert len(fn.body) >= 1


def test_first_in_text_behavior():
    """删掉重复实现后 _first_in_text 行为不变：按词表顺序返回首个命中。

    变异 M1'：把函数改坏（如恒返回空）→ 本判据红。
    """
    from media_resource_manager.engine import Engine
    f = Engine._first_in_text
    assert f(["cat", "dog"], "a cat runs") == "cat"
    assert f(["cat", "dog"], "hotdog stand") == "dog"
    assert f(["cat"], "nothing here") == ""
    assert f([], "cat") == ""


def test_engine_neighbours_intact():
    """邻接方法不许被顺手删掉（防「删多了」回归）。"""
    tree = ast.parse(ENGINE.read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Engine")
    names = {n.name for n in cls.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for keep in ("_purge_to_trash", "_stage_to_review",
                 "_first_in_text", "_matched_keyword", "_blacklist_rules"):
        assert keep in names, f"Engine 缺少邻接方法 {keep}"
    mod_names = {n.name for n in tree.body
                 if isinstance(n, ast.FunctionDef)}
    assert "apply_blacklist_existing" in mod_names, \
        "模块级 apply_blacklist_existing 不应被删"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
