# -*- coding: utf-8 -*-
"""test_r54_forensics —— 给「挂死自动取证器」兜底（R54）。

背景：R51~R53 三轮都撞上「全量 pytest 间歇性挂死」，三轮共同的困惑是
**挂死时不产生任何可诊断输出**：无 traceback、无 junit、无 Windows 事件日志、
连conftest 的 420s 看门狗都一声不吭。R53 因此写了 `tools/_r53_run_full.py`
自动抓栈，声称"下一次复现就能定位"。

R54 实测发现：**那个工具自己就抓不到东西**。
venv 的 `Scripts/python.exe` 只有 274KB，是个转发器；R53 版 runner 把
`proc.pid`（=转发器）交给 py-spy，实测结果是

    rc=1  stdout 长度 0  stderr="Failed to find python version from target process"

而对转发器 spawn 出来的真解释器抓栈则 rc=0、栈里有真实帧。
⇒ **真挂死时取证器写下的是一份空文件**，这正好就是「无输出」谜团的成因之一。

本测试用**真实子进程 + 真实 py-spy** 端到端钉死这个行为，而不是靠读源码字面量
（教训同R50：「静态护栏全绿」≠「功能真的生效」）。
"""
from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "_r53_run_full.py"

# 睡着的子进程：栈里必须出现这个函数名，用来证明「抓到的是真栈而非空壳」。
_SLEEPER = (
    "import time\n"
    "def R54_TEST_RAYSLEEP():\n"
    "    time.sleep(45)\n"
    "R54_TEST_RAYSLEEP()\n"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("_r53_run_full_undertest", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _descendants(root: int) -> list[int]:
    """独立实现一遍「取子孙 pid」，用来交叉验证被测函数。

    ⚠ 刻意**不复用** `_r53_run_full._pyspy_targets` 的内部实现：
    复用等于让被测代码自己证明自己（M1 那次「没抓红」正是这个原因）。
    """
    import ctypes
    from ctypes import wintypes

    class PE32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    pairs: dict[int, int] = {}
    try:
        pe = PE32W()
        pe.dwSize = ctypes.sizeof(PE32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                pairs[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        k32.CloseHandle(snap)

    kids: dict[int, list[int]] = {}
    for pid, ppid in pairs.items():
        kids.setdefault(ppid, []).append(pid)
    out, stack = [], [root]
    while stack:
        cur = stack.pop()
        for k in kids.get(cur, ()):
            out.append(k)
            stack.append(k)
    return out


# ── 1. 静态层：工具必须存在且可解析 ───────────────────────────────
def test_tool_exists_and_parses():
    assert TOOL.exists(), "R53 的取证器 _r53_run_full.py 不见了"
    ast.parse(TOOL.read_text(encoding="utf-8"))


# ── 2. 静态层：禁止把「只有转发器」的 pid 直接喂给 py-spy ─────────
def test_dump_stacks_targets_more_than_the_launcher():
    """反向钉死：抓栈函数不能只认launcher 一个 target。

    判据用 AST 找 `--pid` 实参的来源函数，而不是全文搜 `proc.pid`
    —— 本仓注释里大量 quoting「曾经错在哪」，全文搜会匹配到注释而假红
    （R37铁律）。

    ⚠ R54 修正记录：第一版这条只断言「`_pyspy_targets` 这个名字出现过」，
    结果把被测函数改成「只返回 launcher」它**照样绿** ——
    名字在、行为坏。⇒ 「护栏查了名字」≠「护栏查了行为」，
    行为必须由上面的端到端用例真起进程来验。
    """
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_pyspy_targets"),
              None)
    assert fn is not None, "_pyspy_targets 不见了 —— 取证又退回只抓 launcher"

    fn_dump = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "_dump_stacks"),
                   None)
    assert fn_dump is not None, "_dump_stacks 不见了"

    called = {n.func.id for n in ast.walk(fn_dump)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_pyspy_targets" in called, (
        "_dump_stacks 没有走 _pyspy_targets，等于又只抓转发器 → "
        "真挂死时取证会是空文件（R54 抓到的真缺陷）")

    # _pyspy_targets 的返回值必须真的**拼进了子孙列表**，而不是硬编码单元素
    rets = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert any(isinstance(r.value, ast.BinOp) for r in rets), (
        "_pyspy_targets 没有任何一处把 tree 与 root_pid 合并返回 —— "
        "它很可能只返回了 launcher")


# ── 3. 端到端层：真起子进程 + 真 py-spy，必须抓到**非空栈** ────────
def test_real_pyspy_dump_actually_returns_a_stack():
    """真跑一次：断言工具**自带的那个** py-spy 真的能抓到非空栈。

    这条是本轮的核心断言。若哪天 venv 结构变了导致抓不到，
    它会红，那正是我们希望早知道的。

    ⚠ 两个 R54 踩坑点：
    ① 必须用**工具里的 PYSPY 常量**，不能自己在测试里拼路径 ——
       否则把工具的 PYSPY 改坏，测试照样绿（第一版就栽在这）。
    ② 「PYSPY 不存在」要**判失败**而不是 skip：路径配错是配置缺陷，
       skip 会把红信号变成沉默。
    """
    mod = _load_tool()
    pyspy = Path(mod.PYSPY)
    if not pyspy.exists():
        # V0.0.7：本机安全软件会把 py-spy.exe（这类工具要注入别的进程读
        # 内存）直接清掉，pip 重装完也会被清掉。这**不是**「路径配错」，
        # 是环境策略：明确跳过并写清原因，别让红信号变成沉默；而在正常
        # 机器上「包装清单里声明过、文件却没了」以外的路径错误仍会判失败。
        dist_info = next(
            (Path(sys.prefix) / "Lib" / "site-packages").glob(
                "py_spy-*.dist-info/RECORD"), None)
        declared = (dist_info is not None and dist_info.exists()
                    and "Scripts/py-spy.exe" in dist_info.read_text(
                        encoding="utf-8", errors="replace"))
        if declared and not shutil.which("py-spy"):
            pytest.skip(
                "本机安全策略清理了 py-spy.exe（包装清单里声明过、文件被外部删除）；"
                "这不是工具路径配错。需要真抓栈时请临时放行该程序再跑本条")
        if not declared and not shutil.which("py-spy"):
            # 这个解释器根本没装 py-spy（无 dist-info、PATH 里也没有）——
            # 是取证工具未安装，不是被测产品 nor 工具路径配错。挂死取证是
            # 可选调试能力，缺依赖时跳过；需要时 pip install py-spy 即可。
            pytest.skip(
                "当前 venv 未安装 py-spy（可选取证工具），跳过真抓栈；"
                "装上 py-spy 后本条会自动真跑")
        assert pyspy.exists(), (
            f"工具配置的 py-spy 不存在：{pyspy} —— 真挂死时取证必然抓不到东西")

    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], cwd=str(ROOT))
    try:
        time.sleep(4)
        targets = mod._pyspy_targets(proc.pid)
        assert targets, "没找到任何 py-spy target"
        assert proc.pid in targets, "launcher 自己都不在 target 列表里"

        #★ 行为层断言：target 列表必须**真的包含子孙进程**。
        # 只测「列表里有 launcher」会漏掉 M1（把 _pyspy_targets 改回只返
        # launcher）—— 那是本轮修的真缺陷，护栏必须能抓它。
        # 独立用Toolhelp32 取一次子孙做交叉验证，不复用被测函数的内部实现。
        real_kids = _descendants(proc.pid)
        for k in real_kids:
            assert k in targets, (
                f"子孙 pid={k}（真解释器）不在 target 列表 {targets} 里 —— "
                "这会让真挂死时抓到空文件，正是 R54 修的那个缺陷")

        ok_pid = None
        denied: list[str] = []
        for pid in targets:
            try:
                r = subprocess.run([str(pyspy), "dump", "--pid", str(pid), "--nonblocking"],
                                   capture_output=True, text=True, timeout=90)
            except (PermissionError, OSError) as e:
                # 安全软件可能不删文件、只拒绝执行（WinError 5）。同样
                # 是环境策略，不是取证器定位错了，说清楚再跳过。
                denied.append(f"pid={pid}: {type(e).__name__}: {e}")
                continue
            if r.returncode == 0 and "R54_TEST_RAYSLEEP" in (r.stdout or ""):
                ok_pid = pid
                break
        if ok_pid is None and denied and len(denied) == len(targets):
            pytest.skip("本机安全策略拒绝执行 py-spy（" + "；".join(denied[:2])
                        + "）；需要真抓栈时请临时放行该程序再跑本条")
        assert ok_pid is not None, (
            f"所有 target {targets} 都抓不到真实栈 —— 取证器仍不可用。"
            "venv 转发器结构可能变了，必须重新定位真解释器 pid")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


# ── 4. 端到端层：_dump_stacks 必须**落盘留痕**，抓不到也要写文件 ──
def test_dump_stacks_always_writes_evidence_files():
    """抓不到栈 ≠ 可以不留痕。

    「空文件」正是 R51~R53 查不出来的原因，所以每个 target 都必须落一个
    文件，且文件里必须写明 rc / stdout 长度 / stderr 原文。
    """
    mod = _load_tool()
    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], cwd=str(ROOT))
    try:
        time.sleep(3)
        written = mod._dump_stacks("unittest_tag", proc.pid)
        assert written, "_dump_stacks 一个文件都没写"
        for name in written:
            p = mod.STACK_DIR / name
            assert p.exists(), f"{name} 没落盘"
            body = p.read_text(encoding="utf-8", errors="replace")
            assert body.strip(), f"{name} 是空文件 —— 那就等于没取证"
            assert "rc=" in body and "stdout" in body, (
                f"{name} 里没有 rc/stdout/stderr 字段，无法判断是真挂死还是抓取失败")
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        # 清理本测试自己产生的取证文件（只删本测试 tag，不碰别人的）
        if mod.STACK_DIR.exists():
            for p in mod.STACK_DIR.glob("unittest_tag_*"):
                try:
                    p.unlink()
                except OSError:
                    pass


# ── 5. 端到端层：子进程 CPU 判定必须**能取到值**（None 会静默失去检测力）
def test_subtree_cpu_is_readable_for_a_live_child():
    """R53 踩过的坑：取值失败返回 None，而 None 在判定里 = 「跳过」=
    静默失去卡死检测能力，比报错更危险。所以这里钉死「能取到」。

    ⚠ 判据用 `is not None`，不是真值—— 空闲进程的 CPU 增量合法地=0，
    0 是假值，R53 自己的验证脚本就栽在这一条上。
    """
    mod = _load_tool()
    proc = subprocess.Popen([sys.executable, "-c", _SLEEPER], cwd=str(ROOT))
    try:
        time.sleep(3)
        a = mod._subtree_cpu_ticks(proc.pid)
        time.sleep(2)
        b = mod._subtree_cpu_ticks(proc.pid)
        assert a is not None and b is not None, (
            f"子树 CPU 取不到（a={a} b={b}）—— 卡死检测会静默失效")
        assert b >= a, "累计 CPU 时间倒退了，_proc_cpu_ticks 读的不对"
    finally:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass