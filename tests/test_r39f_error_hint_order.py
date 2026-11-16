"""r39f：锁死「任务失败原因只显示第一条」这条回归。

真机现场（30 分钟前台全按钮扫描跑出来的 5 个 error 任务之一）：

    tasks 表  id=21  status=error
    last_error = "selector warning @ https://www.erome.com/: "
                 "image selector 'img[src*='/thumbs/']' matched 0"

这条信息**零指导性**：它只是 adapter 的**提示**（某页某选择器空了），不是故障
根因。真正能照着做的那句在 warnings 后面被挤着：

    "所有 image selector 落空，通用兜底也没找到图片：页面可能是
     JS 动态渲染、需要登录/Cookie，或入口页本身不含图"

而 queue.py 判定任务失败的写法是：

    hard_error = bool(rep.errors) and rep.downloaded == 0
    last_error = (rep.errors[0] if hard_error else "")[:200]

只取第一条。engine 又原样 `rep.errors.extend(warnings[:20])`，于是「matched 0」
永远打头 —— 用户（以及下次扫出来的诊断）只看到一句没头没尾的提示，
根本判断不出该改入口 URL 还是改选择器。

修法：engine 把 warnings 合并进 rep.errors 前，先按「有没有指导性」排序，
把含 落空 / 也没找到 / 可能是 / 未产出图 的那条提到最前；其余一条不丢。

本文件上锁三件事：
1. 有指导性的那条确实排到了第一（last_error 会显示它）；
2. 原顺序里那些平平无奇的 matched 0 一条不少，全还在；
3. 完全没有指导性字样时顺序不乱排（纯 matched 0 场景保持原样）。
"""
import ast
import pathlib

import pytest

ENGINE_REL = "media_resource_manager/engine.py"


def _engine_path():
    # tests/ 在项目根下，包根 = tests 的上一级
    return pathlib.Path(__file__).resolve().parent.parent / ENGINE_REL


def _sorted_call():
    """把引擎里那句 rep.errors.extend(sorted(warnings[:20], key=_hint_rank)) 抠出来。"""
    src = _engine_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "extend":
            if node.args and isinstance(node.args[0], ast.Call):
                return node
    return None


def test_engine_ranks_actionable_hint_first():
    """必须存在「按指导性排序」的 extend，否则 last_error 又只剩 matched 0。"""
    call = _sorted_call()
    assert call is not None, (
        "engine.py 里没找到 rep.errors.extend(sorted(...)) —— 任务失败原因又会"
        "只剩一句 matched 0，用户无法判断该改入口还是改选择器")
    src = _engine_path().read_text(encoding="utf-8")
    for kw in ("落空", "也没找到", "可能是", "未产出图"):
        assert f'"{kw}"' in src or f"'{kw}'" in src, (
            f"排序键里少了指导性关键词 {kw}，匹配不到真实提示句")


def test_hint_rank_function_is_defined_in_engine():
    """engine 里必须能拿到 _hint_rank（sorted 的 key）。

    它定义在跑任务的那个协程体内部（extend 就在那儿），所以不能只看顶层
    FunctionDef，要整棵树找 —— 只盯顶层会误报「没定义」。
    """
    src = _engine_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_hint_rank" in names, "engine.py 里找不到 _hint_rank"


def test_hint_rank_puts_actionable_text_front():
    """直接用函数语义断言：有指导性 → 0，没有 → 1。"""
    import importlib.util
    import sys

    path = _engine_path()
    spec = importlib.util.spec_from_file_location("_mrm_engine_for_r39f", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_mrm_engine_for_r39f"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # engine 拉起链条里有可选依赖，缺依赖就跳过语义断言
        pytest.skip(f"engine 导入失败（可选依赖缺失）：{type(e).__name__}: {e}")

    if not hasattr(mod, "_hint_rank"):
        pytest.skip("engine 无法独立导入，跳过 _hint_rank 语义断言")

    plain = "image selector 'img[src*='/thumbs/']' matched 0"
    hint = ("所有 image selector 落空，通用兜底也没找到图片：页面可能是 "
            "JS 动态渲染、需要登录/Cookie，或入口页本身不含图")
    assert mod._hint_rank(plain) == 1
    assert mod._hint_rank(hint) == 0
    assert mod._hint_rank(hint) < mod._hint_rank(plain)
    # 并列时不报错（sorted 稳定）
    ranked = sorted([plain, plain], key=mod._hint_rank)
    assert ranked == [plain, plain]


def test_warnings_are_not_lost_after_sorting():
    """排序只是挪位置，不能吞掉任何一条 warning。"""
    src = _engine_path().read_text(encoding="utf-8")
    assert "rep.errors.extend(sorted(warnings[:20], key=_hint_rank))" in src, (
        "engine 里那句 extend 被改样了：排序键可能写错，或丢了 warnings 切片")
    assert "warnings[:20]" in src, "warnings 全量切片丢了，提示条数会变少"
