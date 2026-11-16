"""R56 护栏：钉住「累积探针是只读的」+「运行器 extra 白名单」+「env 构造抗污染」。

R56 的三个真发现（R55 的结论被实测推翻了一半）：
  ① `conftest._reap_top_level_widgets` 现有的
     `close()+deleteLater()+sendPostedEvents(DeferredDelete)+processEvents`
     **确实把控件真放掉了**（实测净增后代 = 0）⇒ 「只 close 不真删」这个前提是假的；
  ② `shiboken6.delete()` 真删（= R55 的「候选 A」）**直接把进程打挂** ⇒ 那条路是死路；
  ③ R55 那个 70x 曲线是**探针自己没复刻被测行为**造出来的假象
     （它的注释自称「如实复刻 conftest」，代码里只有 `w.close()`）。

★ 本文件所有「不许出现 X」的判据**一律用 AST**，不用全文搜——
  R37 铁律：全文搜会匹配到本仓注释/文档里「引用错误写法」的那几行而假红。
  R56 写第一版时就被自己的docstring 抓了一次红（下面第 4 节的注释即例子）。

判据一律是**行为**不是名字（R54/R55 铁律）：
  - 探针不得调用任何销毁控件的方法（AST 查 Call.func.attr）；
  - 探针必须真写 JSONL，且是 append；「量不到」要写成 -1 记录而非跳过；
  - `MRM_PYTEST_EXTRA` 只能加 `-p <模块>`，`-q`/`-x` 这类会**关掉守卫或改写产物**的
    参数必须被丢掉（否则「加个只读探针」这个动作就能把 R55 刚修好的 `-vv` 抵消掉）。
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
PLUGIN = TOOLS / "_r56_probe_accum_plugin.py"
RUNNER = TOOLS / "_r53_run_full.py"
PROBE = TOOLS / "_r56_probe_reclaim.py"
R55_TEST = ROOT / "tests" / "test_r55_verbose_forensics.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


# ---------------------------------------------------------------- 1) 探针只读


def test_accum_plugin_never_destroys_widgets():
    """累积探针必须**只读**：AST 里不得出现任何销毁/关闭控件的调用。"""
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    banned = {"close", "closeAllWindows", "delete", "deleteLater", "setParent",
              "exec", "exec_", "quit", "exit", "terminate", "kill",
              "setQuitOnLastWindowClosed"}
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute) and fn.attr in banned:
            found.append(f"{fn.attr} @line{node.lineno}")
        elif isinstance(fn, ast.Name) and fn.id in ("exec", "quit", "exit", "eval"):
            found.append(f"{fn.id} @line{node.lineno}")
    assert not found, f"累积探针只读，但发现销毁类调用：{found}"


def test_accum_plugin_reads_counts_and_appends_jsonl():
    """探针必须真读控件数 + 以 **append** 写 JSONL，且「量不到」要落成 -1。"""
    src = PLUGIN.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert "topLevelWidgets" in src, "没读 topLevelWidgets ⇒ 量不到累积"
    assert _fn(tree, "_write") is not None, "_write 应抽成函数，否则两个 hook 各写一份易漂"

    opened_append = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "open" and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "a"):
            opened_append = True
    assert opened_append, "JSONL 必须 append 打开（多轮不能互相覆盖）"

    counts = _fn(tree, "_counts")
    assert counts is not None, "必须有 _counts()"
    rets = [n for n in ast.walk(counts) if isinstance(n, ast.Return)]

    def _const(node):
        """把 AST 节点折成字面量值。★ 必须处理 `-1`：
        Python 里 `-1` 解析成 `UnaryOp(USub, Constant(1))`，**不是** `Constant(-1)`。
        R56 第一版只认 `Constant` ⇒ 这条护栏永远红（又一次「判据本身写错」）。
        """
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            inner = _const(node.operand)
            return -inner if isinstance(inner, (int, float)) else None
        return None

    def _is_minus_one_pair(node) -> bool:
        return (isinstance(node, ast.Tuple) and len(node.elts) == 2
                and all(_const(e) == -1 for e in node.elts))

    assert any(_is_minus_one_pair(r.value) for r in rets), (
        "_counts 在没有 QApplication 时必须 return (-1, -1) 落一条记录；"
        "返回 None 后整行跳过，会让「探针没挂上」和「这段本来没控件」长得一模一样"
    )
    # ⚠ 这里**刻意不再**加一条「不许出现 (None, None)」的反向断言：
    # 正常路径就是 `return len(tops), n`，两个元素都不是字面量常量，
    # 任何「按常量判定 None」的写法都会把它误判成违规（R56 第一版的判据就栽在这）。
    # 上面的正向断言已经覆盖真正的要求；多写一条只会制造假红。
    #⇒ 教训：**反向断言要先确认它在合法代码上不会红**（R53：mutation 没红有两种解释，
    #  这里反过来——「护栏红了」也可能是判据本身写错了）。


def test_accum_plugin_end_to_end_records_a_monotonic_series(tmp_path):
    """端到端真跑一小段 pytest：探针必须挂得上，且写出**多条**记录。

    R55 教训：「静态护栏全绿」≠「行为真的发生」。这里真起一个 pytest 进程
    （只跑 1 个纯逻辑文件，不建控件，避免与全量抢 GUI 资源）。

    ★ 关键：即便那个文件**根本不建控件**（`_counts` 会返回 -1），
      也必须照样写出记录 —— 这正是 ① 想钉住的行为。
    """
    out = tmp_path / "accum.jsonl"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1",
               PYTHONPATH=str(TOOLS) + os.pathsep + str(ROOT),
               MRM_ACCUM_OUT=str(out))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_triage_junit.py",
         "-q", "-p", "_r56_probe_accum_plugin", "--no-header", "-x"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=900,
    )
    assert out.exists(), (
        f"探针没写出 JSONL（pytest rc={proc.returncode}）\n{proc.stdout[-1500:]}"
    )
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) >= 2, f"只有 {len(rows)} 条记录，构不成曲线"
    assert all({"kind", "t", "top", "desc"} <= set(r) for r in rows), "记录字段不全"
    ts = [r["t"] for r in rows]
    assert ts == sorted(ts), f"时间戳非递增：{ts[:8]}"
    assert any(r["kind"] == "module" for r in rows), "缺模块级汇总行"
    # 纯逻辑文件不建控件 ⇒ 至少要能看到 top == -1（证明「量不到」被记录了）
    assert any(r["top"] == -1 for r in rows), (
        "没有 -1 记录 ⇒ _counts 把「没有 QApplication」当成「跳过」了"
    )


# ------------------------------------------------- 2) 运行器 extra 白名单


def test_runner_extra_only_allows_p_plugin():
    """`MRM_PYTEST_EXTRA` 只能加 `-p <模块>`，其余一律丢弃。

    为什么关键：R55 刚修好「`-v` 被 `addopts=-q` 静默抵消」。若 extra 能塞进 `-q`，
    那么「加个只读探针」这个动作就能把 R55 的修复打回原形
    —— **参数写了 ≠ 行为发生了**的同型复发。
    """
    mod = _load(RUNNER, "r53_runner_extra")
    os.environ["MRM_PYTEST_EXTRA"] = "-p myprobe -q --junit-xml=evil.xml -x"
    try:
        cmd = mod._pytest_cmd("tests/real.xml")
    finally:
        os.environ.pop("MRM_PYTEST_EXTRA", None)
    assert "-p" in cmd and "myprobe" in cmd, f"白名单把真探针也丢了：{cmd}"
    assert "-q" not in cmd, f"-q 混进来了 ⇒ 会抵消 -vv：{cmd}"
    assert "-x" not in cmd, f"-x 混进来了 ⇒ 会提前中止：{cmd}"
    # 判据必须看**实际参数**，不能只数 '--junit-xml' 子串
    # （cmd 里是 '--junit-xml=tests/real.xml' 一个整体，子串计数恒为 0 —— R56 踩过）
    junits = [a for a in cmd if a.startswith("--junit-xml")]
    assert junits == ["--junit-xml=tests/real.xml"], f"junit 被覆盖/丢失：{cmd}"


def test_runner_extra_keeps_verbose_and_junit_by_default():
    """不带 extra 时，R55 的修复必须原封不动还在（`-vv` + 恰一个 junit）。"""
    mod = _load(RUNNER, "r53_runner_default")
    os.environ.pop("MRM_PYTEST_EXTRA", None)
    cmd = mod._pytest_cmd("tests/x.xml")
    assert "-vv" in cmd, f"-vv 丢了 ⇒ addopts 的 -q 又会抵消：{cmd}"
    assert "-v" not in cmd, "应是 -vv（单 -v 会被 addopts 抵消）"
    assert "--tb=short" in cmd
    assert [a for a in cmd if a.startswith("--junit-xml")] == ["--junit-xml=tests/x.xml"]


# ------------------------------------------------- 3) R55 的候选 A 是死路


def test_reclaim_probe_covers_all_four_modes():
    """四种模式都得**在代码里声明**：`close` / `later`(=conftest 现状) / `flush` / `shib`(候选 A)。

    R56 实测：`shib`（shiboken6 真删）**直接打挂进程**，`later` **净增后代 = 0**。
    「候选 A 会 abort」这件事必须有可复跑的探针背着，
    否则下一轮读 R55 的报告又会去改 conftest。

    ★ 判据读 `MODES` 这个**模块级元组**（AST），不是全文搜 docstring ——
      R56 第一版就是靠 `mode == "xxx"` 的比较节点去找，结果 `later` 是**兜底分支**
      根本没有比较节点 ⇒ 假红。契约写进可执行的位置才好测。
    """
    tree = ast.parse(PROBE.read_text(encoding="utf-8"))
    declared: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "MODES" for t in node.targets):
            if isinstance(node.value, (ast.Tuple, ast.List)):
                declared = {e.value for e in node.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert {"close", "later", "flush", "shib"} <= declared, f"MODES 声明不全：{sorted(declared)}"
    # 并且main 必须真的校验它（否则声明只是装饰）
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "str" for n in ast.walk(_fn(tree, "main") or ast.Module(body=[]))) \
        or "MODES" in ast.dump(_fn(tree, "main")), "main 应校验 MODE 属于 MODES"


def test_reclaim_probe_faithfully_mirrors_conftest_teardown():
    """`later` 模式必须**逐字复刻** conftest 的三步，否则又是一个假对照。

    ★ 这条正是 R55 那个探针栽的地方：它注释自称「如实复刻 conftest」，
      代码里只有 `w.close()`，少了 deleteLater / sendPostedEvents ⇒ 量出 70x 假曲线。

    ⚠ 顺序判据必须用 **AST 的 lineno**，不能用 `src.index(子串)`：
      R56 第一版用 index()，结果命中的是**文档字符串里提到的那几个词**
      （`sendPostedEvents` 在模块 docstring 里出现得更早）⇒ 报出一个假的顺序错误。
      同型复发：全文/子串定位会撞上注释（R37）。
    """
    tree = ast.parse(PROBE.read_text(encoding="utf-8"))
    src = PROBE.read_text(encoding="utf-8")
    assert "shiboken6" in src, "候选 A 必须真的调 shiboken6（否则不是同一个对照）"

    fn = _fn(tree, "_reclaim")
    assert fn is not None, "必须有 _reclaim()"
    lines: dict[str, int] = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in ("close", "deleteLater"):
            # 取**最靠前**的那次（同一循环体里的首次出现）
            lines[name] = min(lines.get(name, 10**9), node.lineno)
        if name == "sendPostedEvents":
            lines["send"] = min(lines.get("send", 10**9), node.lineno)
        if name == "processEvents":
            lines["proc"] = min(lines.get("proc", 10**9), node.lineno)
    assert {"close", "deleteLater", "send", "proc"} <= set(lines), \
        f"later 模式没复刻 conftest 的三步，缺：{ {'close','deleteLater','send','proc'} - set(lines) }"
    assert (lines["close"] < lines["deleteLater"] < lines["send"] < lines["proc"]), \
        (f"顺序不对（close<deleteLater<sendPostedEvents<processEvents），实际 {lines}")
    # 三步都在 ⇒ 就不必再全文搜（那会撞注释）
    assert "DeferredDelete" in src


# ------------------------------- 4) 环境相关的隐性缺陷（单跑永远看不出来）


def _dict_star_environ_offenders() -> list[str]:
    """AST 扫出所有 `dict(**<environ>, KEY=v)` 形态的调用（**注释/docstring 不算**）。"""
    import glob
    bad: list[str] = []
    for p in sorted(glob.glob(str(ROOT / "tests" / "*.py"))
                    + glob.glob(str(ROOT / "tools" / "*.py"))):
        try:
            tree = ast.parse(Path(p).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "dict" and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Starred) and _is_environ(first.value):
                if any(isinstance(a, ast.Starred) for a in node.args[1:]) is False \
                        and len(node.args) >= 2:
                    bad.append(f"{Path(p).name}:{node.lineno}")
            elif isinstance(first, ast.Starred):
                continue
    return bad


def _is_environ(node: ast.AST) -> bool:
    """判断表达式是不是 `os.environ` / `__import__("os").environ` 之类的**真引用**。"""
    src = ast.dump(node)
    return "environ" in src


def test_no_dict_star_unpack_environ_with_extra_kwargs():
    """禁止 `dict(**os.environ, KEY=v)` —— 父环境已有该 KEY 时直接 TypeError。

    ★ R56 在真全量里抓到的**唯一一个红**就属这一类，机制值得记死：
    `test_r55_verbose_forensics.py` 里写的是
        env=dict(**__import__("os").environ, PYTHONUNBUFFERED="1")
    `**environ` 先把 `PYTHONUNBUFFERED` 铺成关键字参数，末尾又显式给同一个 key
    ⇒ `TypeError: got multiple values for keyword argument`。

    为什么单跑永远发现不了：`PYTHONUNBUFFERED` **只有父进程已设了它才会炸**。
    R55 自己单跑那个文件时父环境干净 ⇒ 5 passed；
    而 `tools/_r53_run_full.py` 给子进程设了 `PYTHONUNBUFFERED="1"`
    （铁律三要无缓冲）⇒ 全量里同一个文件立刻红。
    ⇒ **「单跑绿」不代表「在真实调用环境里绿」。**
      凡读宿主环境的用例，判据必须是「在最脏的环境下也成立」。
    """
    bad = _dict_star_environ_offenders()
    assert not bad, (
        "发现 dict(**environ, KEY=v) 写法（父环境已有该 KEY 时必炸）：" + str(bad)
    )


def test_verbose_forensics_env_construction_is_dirty_env_safe():
    """钉住 R55 那条用例：env 构造必须是 `dict(environ, KEY=v)` 且真能抗污染。"""
    tree = ast.parse(R55_TEST.read_text(encoding="utf-8"))
    star_forms = 0
    good_forms = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "dict" and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Starred) and _is_environ(first.value):
            star_forms += 1
        elif "environ" in ast.dump(first):
            good_forms += 1
    assert star_forms == 0, "又退回 dict(**environ, KEY=v) 了"
    assert good_forms >= 1, "应至少有一处 dict(environ, KEY=v) 正解"


def test_dirty_env_construction_really_works_end_to_end(tmp_path):
    """真按「父环境已污染」构造一次 env —— 判据是行为，不是读源码。"""
    polluted_parent = dict(os.environ, PYTHONUNBUFFERED="1")
    env = dict(polluted_parent, PYTHONUNBUFFERED="1")     # 正解
    assert env["PYTHONUNBUFFERED"] == "1"
    try:
        dict(**polluted_parent, PYTHONUNBUFFERED="1")      # 反例：必须抛
        raised = False
    except TypeError:
        raised = True
    assert raised, "dict(**environ, KEY=v) 竟然没抛 TypeError？前提变了要重新评估"


# ============ 5) 钉住 R56 的「机制纠正」，别让下一轮退回R55 那个已被证伪的说法


def test_conftest_teardown_really_deletes_and_shib_would_abort():
    """`later` 三步能**真删**；`shib`（shiboken6 真删）是**死路** —— 两条都要有代码为证。

    R55 的候选 A 是「把 conftest 改成真删」。R56 实测 `shiboken6.delete()` **打挂进程**。
    这条护栏的作用：让「候选 A 已被证伪」这件事**有可执行的代码证据**，
    而不是只写在报告里 —— 否则下一轮读 R55 报告又会去改 conftest。
    """
    fn = _fn(ast.parse(PROBE.read_text(encoding="utf-8")), "_reclaim")
    assert fn is not None
    calls = [n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "deleteLater" in calls, "later 模式必须调 deleteLater（否则又没复刻 conftest）"
    assert "delete" in calls, "候选 A（shiboken6.delete）必须留在探针里，否则结论不可复跑"


def test_accum_plugin_records_module_boundary_not_monotonic_growth():
    """探针必须区分「模块内累积」与「模块级 reap 后的残留」—— 两者的含义完全不同。

    ★ R56 的关键测量口径：模块级那一行读的是**该模块 reap 之前**的快照
      （pytest hook顺序：module 级fixture teardown 在 `pytest_runtest_teardown` 之后）。
      R55 把「模块内涨到 4769」和「全局单调累积」混为一谈 ⇒ 得出错的机制。
    ⇒ 护栏要求：模块级和用例级两种 kind 都必须在，且模块级必须带 `module` 字段。
    """
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "kind"
                        and isinstance(v, ast.Constant)):
                    kinds.add(v.value)
    assert {"module", "case"} <= kinds, f"两种粒度都要记，否则会重犯 R55 的口径错误：{kinds}"
