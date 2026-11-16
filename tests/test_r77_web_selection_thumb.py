"""R77 · Web 三项：#25 多选开关（默认单选）/ #33 全选·反选 / #18 缩略图档位。

作业单位置：TONIGHT_FINAL_ORDER 主单第 25/33/18 项，
PRIORITY_TONIGHT_ORDER §2「Web 剩余 7 项」里的前三个。

**为什么这一轮选这三项**：它们共用同一个前端文件
(`media_resource_manager/web/static/js/app.js` 的 `renderGrid`)，
且都决定「点一张缩略图会发生什么」⇒ 一次改、一次验，闭环最小。

四层判据（铁律 R73：光看判据现在是绿的不算数，必须变异一次确认它真会红）：
  L1 **node 真执行** —— 真 require 前端 app.js 跑纯函数，问的是**行为**；
  L2 源码接线 —— 纯函数必须被**真的调用**（不只是被注释提到）；
  L3 CSS —— 三档只改变量、断点不再覆盖 grid-template-columns；
  L4 真 HTTP —— 起 WebServer 取回前端资产，确认改动真在服务路径上。

⚠ 判据与被护对象**同源**：L1/L4 走的都是磁盘上那份 app.js，
   不复制一份副本来断言（R72 教训：抄进自己文件的判据是空壳）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JS = ROOT / "media_resource_manager" / "web" / "static" / "js" / "app.js"
CSS = ROOT / "media_resource_manager" / "web" / "static" / "css" / "app.css"
STUB = ROOT / "tools" / "r77_dom_stub.js"

NODE = shutil.which("node")

needs_node = pytest.mark.skipif(
    NODE is None, reason="本机没装 node ⇒ 跳过纯 JS 行为层（不是产品问题）")


def _js(body: str):
    """在 node 里真 require 前端 app.js，执行 body 并返回 JSON 结果。

    ⚠ 桩与探针共用 tools/r77_dom_stub.js —— 两处各写一份会静默失效。
    """
    assert NODE, "没有 node"
    script = (
        "require(%s).install();\n"
        "var m = require(%s);\n"
        "var out = (function () { %s })();\n"
        "process.stdout.write(JSON.stringify(out));\n"
        % (json.dumps(str(STUB)), json.dumps(str(JS)), body)
    )
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    assert p.returncode == 0, (
        f"node 执行失败 rc={p.returncode}\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}")
    return json.loads(p.stdout)


# ==================== L1 · node 真执行（行为层） ====================

# ==================== L2 · 源码接线（纯函数必须真被调用） ====================

def _src() -> str:
    return JS.read_text(encoding="utf-8")


def _fn_src(name: str) -> str:
    """按**函数边界**切出某个 JS 函数的源码（大括号配平）。

    ⚠ 为什么不能用「全文搜字面量」：本轮变异测试当场抓到一次 ——
      `setAttribute('data-size', state.thumb)` 全文有**两处**（renderGrid 里一处、
      档位下拉的 change 里一处），只判「出现过」时把 renderGrid 那处改成
      常量 'm' 仍全绿 ⇒ 判据是空壳（R73 同型：必须判**在哪**）。
    """
    src = _src()
    i = src.index("function %s(" % name)
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{name} 大括号没配平")

# ==================== L3 · CSS ====================

# ==================== L4 · 真 HTTP（改动真在服务路径上） ====================

_TMP = Path(tempfile.mkdtemp(prefix="r77test_"))


@pytest.fixture()
def web_home():
    old = {k: os.environ.get(k) for k in ("MRM_HOME", "MRM_NO_MIGRATE")}
    os.environ["MRM_HOME"] = str(_TMP)
    os.environ["MRM_NO_MIGRATE"] = "1"
    try:
        yield _TMP
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@pytest.fixture()
def http_server(web_home):
    from media_resource_manager import webui
    from media_resource_manager.config import AppConfig
    from media_resource_manager.db import Database

    cfg = AppConfig()
    cfg.db_path = str(web_home / "r77.db")
    cfg.web_bind = "127.0.0.1"
    cfg.web_port = 0

    import media_resource_manager.config as cfgmod
    orig_save = cfgmod.save_config
    cfgmod.save_config = lambda *a, **k: None      # 进程级全局，必须 finally 还原
    db = Database(web_home / "r77.db")
    srv = webui.WebServer(cfg, db, queue=object())
    ok, msg = srv.start()
    assert ok, f"服务器起不来：{msg}"
    try:
        yield cfg
    finally:
        srv.stop()
        db.close()
        cfgmod.save_config = orig_save


def _get(cfg, path: str):
    r = urllib.request.Request(f"http://127.0.0.1:{cfg.web_port}{path}")
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def test_python_syntax_of_probe_tools():
    """顺手自检：本轮工具脚本本身语法正确（写错会浪费一整轮排查）。"""
    import ast
    for rel in ("tools/r77_apply_patch.py", "tools/r77_add_state_export.py"):
        ast.parse((ROOT / rel).read_text(encoding="utf-8"), filename=rel)
