# -*- coding: utf-8 -*-
"""V0.0.19 毛玻璃模块护栏：gui/acrylic.py 静态契约 + 真机无 GUI 冒烟。

钉死四条：
  1. ast 解析通过，三个契约函数 is_supported / apply_glass / remove_glass
     存在且签名符合（apply_glass 带 level="auto" 默认值）；
  2. 只允许标准库 import（ctypes/os/sys/pathlib），禁止任何第三方依赖，
     尤其禁止 PySide6 —— 该模块必须能在无 Qt 环境加载；
  3. 真机调用 is_supported() 返回 dict 且键齐全（ok/os_build/level/reason）；
  4. 无效句柄安全：apply_glass(0) 不抛异常且 ok=False。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACRYLIC = ROOT / "media_resource_manager" / "gui" / "acrylic.py"

ALLOWED_IMPORTS = {"ctypes", "os", "sys", "pathlib", "__future__"}
CONTRACT_FUNCS = {
    "is_supported": ([], []),
    "apply_glass": (["hwnd", "level"], ["auto"]),
    "remove_glass": (["hwnd"], []),
}


def _tree() -> ast.Module:
    return ast.parse(ACRYLIC.read_text(encoding="utf-8"))


def _top_func(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def test_ast_parses():
    assert ACRYLIC.is_file(), "gui/acrylic.py 必须存在"
    _tree()  # SyntaxError 即失败


def test_contract_functions_signatures():
    tree = _tree()
    for name, (params, defaults) in CONTRACT_FUNCS.items():
        fn = _top_func(tree, name)
        assert fn is not None, "缺少契约函数 %s" % name
        got = [a.arg for a in fn.args.args]
        assert got == params, "%s 参数应为 %s，实际 %s" % (name, params, got)
        n_def = len(fn.args.defaults)
        assert n_def == len(defaults), "%s 默认值数量不符" % name
        got_defaults = [ast.literal_eval(d) for d in fn.args.defaults]
        assert got_defaults == defaults, "%s 默认值应为 %s" % (name, defaults)


def test_no_third_party_imports():
    tree = _tree()
    bad = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    bad.append(top)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top not in ALLOWED_IMPORTS:
                    bad.append(top)
    assert not bad, "存在非白名单 import：%s" % sorted(set(bad))


_PROBE = r'''
import json, sys
sys.path.insert(0, r"%s")
from media_resource_manager.gui import acrylic as ac
out = {
    "support": ac.is_supported(),
    "apply0": ac.apply_glass(0),
    "remove0": ac.remove_glass(0),
    "applybad": ac.apply_glass("not-a-handle"),
}
print(json.dumps(out, ensure_ascii=False))
''' % str(ROOT).replace("\\", "/")


def _probe() -> dict:
    """子进程真机探测（无 QApplication），一次覆盖契约 3、4。"""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, "探测子进程异常：%s" % proc.stderr
    return json.loads(proc.stdout.strip())


def test_is_supported_returns_full_dict():
    info = _probe()["support"]
    assert isinstance(info, dict), "is_supported 必须返回 dict"
    for key in ("ok", "os_build", "level", "reason"):
        assert key in info, "is_supported 返回缺少键 %s" % key
    assert isinstance(info["ok"], bool)
    assert isinstance(info["os_build"], int)
    assert info["level"] in ("acrylic", "mica", "transparent", "")
    assert isinstance(info["reason"], str) and info["reason"], "reason 必须是简体中文说明"
    # 本机为 Win10/Win11：ok=True 时 level 必非空，ok=False 时 level 必为空。
    if info["ok"]:
        assert info["level"] != ""
    else:
        assert info["level"] == ""


def test_apply_glass_invalid_hwnd_safe():
    res = _probe()["apply0"]
    assert isinstance(res, dict)
    assert res.get("ok") is False, "无效句柄 0 必须返回 ok=False"
    assert res.get("error"), "无效句柄必须给出中文原因"
    res2 = _probe()["remove0"]
    assert isinstance(res2, dict) and res2.get("ok") is False
    res3 = _probe()["applybad"]
    assert isinstance(res3, dict) and res3.get("ok") is False, "非法类型句柄不得抛异常"
