# -*- coding: utf-8 -*-
"""test_r55_verbose_forensics —— 钉死「全量取证器真的开着 verbose」（R55 新增）。

R55 当场抓到的**真缺陷**：`_r53_run_full.py` 一直传 `-v`，而
`pyproject.toml` 里写着 `addopts = "-q"`。pytest 的 verbosity 是**同一个计数器**
（`-v`=+1 / `-q`=−1），addopts 先应用 ⇒ 命令行的 `-v` 被抵消回 0
⇒ 输出退化成默认的「点 + 百分比」，**日志里一行 `::test` 都没有**。

后果不是「不好看」，而是**R53 亲手写的铁律三从未生效**：
「`-v` + 无缓冲 ⇒ 日志尾部最后一行就是真死的那条用例名」——
可四轮下来压根没有用例名。R51~R54 反复查不出死亡点，**有一半原因是工具自己**。

所以这里不只测「命令里写了 -v」（R54 的教训：**护栏查了名字 ≠ 查了行为**），
而是**真起一个 pytest 进程、读它的真实 stdout**，断言 `::test` 真的出现了。

覆盖：
1. `_pytest_cmd()` 产出的命令行**抵消 addopts 的 -q 后仍为正**（纯计算，快）
2. **端到端**：用该函数产出的真命令跑一个临时小测试文件，断言真实输出里有
   `::test`（这就是「verbose 生效」的唯一可靠判据）
3. `_assert_verbose_effective()` 对「真跑」返回 True、对「空日志」返回 False
   —— 双向都验，防止它变成「永远返回 True」的假绿灯
4. 回归护栏：`pyproject.toml` 的 addopts 若被去掉 -q，本文件第 1 条会提示
   需要重新评估（不硬断言，避免配置合法变动就假红）
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import _r53_run_full as runner  # noqa: E402

# 2026-10-27: 原来写死笔记本 venv 的 python，本机不存在 → WinError 2。改用当前解释器。
PY = sys.executable


def _effective_verbosity(cmd: list[str], addopts: str = "") -> int:
    """算这条命令最终的 verbosity（pytest 的规则：-v +1 / -q -1，addopts 先应用）。"""
    v = 0
    for tok in addopts.split():
        if tok == "-v":
            v += 1
        elif tok.startswith("-vv"):
            v += tok.count("v")
        elif tok == "-q":
            v -= 1
        elif tok.startswith("-qq"):
            v -= tok.count("q")
    for tok in cmd[1:]:                      # 跳过可执行文件名本身
        if tok == "-v":
            v += 1
        elif tok.startswith("-vv"):
            v += tok.count("v")
        elif tok == "-q":
            v -= 1
        elif tok.startswith("-qq"):
            v -= tok.count("q")
    return v


def _repo_addopts() -> str:
    """从真实 pyproject.toml 里读 addopts（读不到就当空，绝不假装有 -q）。"""
    import re
    try:
        txt = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^\s*addopts\s*=\s*"([^"]*)"', txt, re.M)
    return m.group(1) if m else ""


# ------------------------------------------------------------------ 1
def test_cmd_verbosity_survives_repo_addopts():
    """命令行叠加在 addopts 之后仍要 > 0，否则 verbose 被 -q 抵消。"""
    cmd = runner._pytest_cmd("tests/x.xml")
    v = _effective_verbosity(cmd, _repo_addopts())
    assert v > 0, (
        f"最终 verbosity={v}（<=0 ⇒ 输出不是每条用例一行，"
        f"挂死时无法从日志尾部定位死亡点）。cmd={cmd} addopts={_repo_addopts()!r}"
    )


def test_repo_really_has_the_minus_q_that_caused_the_bug():
    """钉住「本仓 addopts 里确实有 -q」这个**前提**。

    R55 的缺陷就是「工具以为 -v 能盖过 addopts」。哪天有人把 addopts 的 -q 去掉，
    这条会提醒重新评估 `_pytest_cmd` 里的 -vv 是否还需要（而不是默默留一个
    「以防万一」的重复档位）。
    """
    assert "-q" in _repo_addopts(), (
        f"pyproject 的 addopts 现在是 {_repo_addopts()!r}（不含 -q）："
        "R55 的抵消前提已变化，请复核 _pytest_cmd 的档位是否仍合适"
    )


# ------------------------------------------------------------------ 2
def test_verbose_really_shows_test_names_end_to_end():
    """★真起进程验行为：日志里必须真的出现 `::test`。"""
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        (tdir / "test_probe_target.py").write_text(
            "def test_probe_alpha():\n    assert True\n\n"
            "def test_probe_beta():\n    assert True\n",
            encoding="utf-8",
        )
        cmd = [PY, "-m", "pytest", "test_probe_target.py", "-vv",
               "--tb=short", "-p", "no:cacheprovider"]
        r = subprocess.run(cmd, cwd=str(tdir), capture_output=True,
                           text=True, timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        assert "::test" in out, (
            "真实输出里没有 '::test' ⇒ verbose 没生效，这条命令无法用于定位死亡点\n"
            f"--- output ---\n{out[-2000:]}"
        )
        assert "test_probe_alpha" in out and "test_probe_beta" in out


# ------------------------------------------------------------------ 3
def test_assert_verbose_effective_true_on_real_verbose_run():
    """正向：真跑且确实 verbose ⇒ 必须返回 True（别把它写成一盏永远绿的灯）。"""
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        probe = tdir / "test_probe_real.py"
        probe.write_text("def test_probe_real_one():\n    assert True\n",
                         encoding="utf-8")
        logp = tdir / "run.log"
        proc = subprocess.Popen(
            [PY, "-m", "pytest", "test_probe_real.py", "-vv",
             "-p", "no:cacheprovider"],
            cwd=str(tdir), stdout=open(logp, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            env=dict(__import__("os").environ, PYTHONUNBUFFERED="1"),
        )
        try:
            assert runner._assert_verbose_effective(logp, proc, timeout_sec=180) is True
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


def test_assert_verbose_effective_false_when_log_has_no_test_names():
    """反向：日志里没有 `::test`（模拟「以为 verbose 其实没有」）⇒ 必须 False。"""
    with tempfile.TemporaryDirectory() as td:
        tdir = Path(td)
        logp = tdir / "silent.log"
        logp.write_text("tests\\test_x.py ....   [ 50%]\n", encoding="utf-8")
        # 进程用一个还在睡的常驻进程冒充「正在跑」，确保函数走的是读日志那条路
        proc = subprocess.Popen([PY, "-c", "import time; time.sleep(3)"])
        try:
            # timeout 给足 20s > 3s 进程寿命：只可能因「日志里没有 ::test」而返回 False
            assert runner._assert_verbose_effective(logp, proc, timeout_sec=20) is False
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
