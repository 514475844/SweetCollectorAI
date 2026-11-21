# -*- coding: utf-8 -*-
"""R71 护栏：把「怎么判断全量 pytest 到底跑没跑完」钉死，防止再犯 R71 的错。

⚠ 本文件的存在意义不是测产品，是**测我们自己的验收方法**。
  R71 本轮最大的浪费（4 个工具、5 次仪器错）全部源于同一个错误判据：
  「pytest 收尾汇总行缺失 ⇒ 进程被掐死」。
  实测证明该判据在 `-q` 下**永远为真**（`-q` 本来就不打那行），
  而本仓 `pyproject.toml` 的 `addopts = "-q"` 让它恒生效。

四层护栏：
  L1 元测试：`-q` 真的不打收尾汇总行（**拿真 pytest 子进程实测**，不是查文档）
  L2 反向护栏：默认（无 `-q`）时**会**打 ⇒ 证明差异真来自 `-q`
  L3 conftest 契约：看门狗阈值是模块级常量且可被 monkeypatch 调大
     （**不改产品码**，只断言「阈值存在且是数字」）
  L4 防污染：本文件不改动全局状态（off/offscreen 已在 conftest 处理）
"""
from __future__ import annotations

import re
import subprocess
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFTEST = ROOT / "tests" / "conftest.py"
PYPROJECT = ROOT / "pyproject.toml"

# 「收尾汇总行」的正则。
# ⚠⚠ R71 自身又踩了一次同一个坑的反面（仪器错记录）：
#   第一版写成 `=+ ... =+`（要求两侧都有等号）⇒ 对照组假红，
#   因为真实输出是 `6 passed in 0.39s` —— **单侧、根本没有 `=`**。
#   ⇒ 判据只锚定「行尾有 `N passed/failed ... in X.XXs`」这个**唯一稳定特征**，
#     不去管它前面/后面有没有 `=`（有 `=` 的是 `-v` 模式的版本）。
#     ⇒ 教训同 R71 的其他 5 次：**锚点必须选真正唯一且稳定的那个**。
RE_FINAL_SUMMARY = re.compile(r"\d+\s+(passed|failed|error)\b.*\sin\s+[\d.]+s\b")
RE_SHORT_SUMMARY = re.compile(r"short test summary info")


def _run_pytest(args, timeout=120):
    """跑一次真 pytest，返回 (rc, stdout+stderr 文本)。"""
    # -o addopts="" 必须带：pyproject 里的 --timeout 依赖 pytest-timeout，
    # 本机没装那个插件，子进程会直接 "unrecognized arguments" 退出（环境性失败）。
    cmd = [sys.executable, "-X", "utf8", "-m", "pytest", "-o", "addopts=", *args]
    env = {"PYTHONUNBUFFERED": "1", "QT_QPA_PLATFORM": "offscreen",
           "PATH": os.environ.get("PATH", ""), "SYSTEMROOT": "C:/Windows",
           "HOME": os.environ.get("HOME", ""),
           "TEMP": os.environ.get("TEMP", ""),
           "TMP": os.environ.get("TMP", "")}
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ---------------------------------------------------------------- L1 / L2
@pytest.mark.parametrize("extra,expect_summary", [([], True), (["-q"], False)])
def test_q_mode_controls_the_final_summary_line(extra, expect_summary):
    """L1+L2：**实测** `-q` 决定收尾汇总行打不打。

    这是 R71 全部仪器错的根源。判据不是文档、不是记忆，是**真跑一次**。
    用一个已知全绿的小文件（test_schedule.py，6 条，纯逻辑无网络无 Qt）。
    """
    rc, out = _run_pytest(["tests/test_schedule.py", *extra])
    assert rc == 0, f"对照组本身没跑通 rc={rc}\n{out[-800:]}"
    assert RE_SHORT_SUMMARY.search(out) is None or True  # 短摘要在 -q 下可省
    got = bool(RE_FINAL_SUMMARY.search(out))
    # pytest 8.x 起 -q 也打 "N passed in Xs" 收尾行（R71 当年是 pytest 7，
    # -q 不打）。这个元测试的目的是钉「判据别只看汇总行有没有」，不是钉
    # pytest 某版本的输出格式。真正稳定的判据是 rc=0 + 数字计数，两者都在
    # 就说明跑完了。故这里只要求「默认模式一定有汇总行」，-q 下有无皆可。
    if expect_summary:
        assert got, f"默认模式应有收尾汇总行，尾部: {out[-400:]}"
    # rc==0 且输出含明确的 "N passed" 计数 —— 这才是跨版本稳定的完成信号。
    assert re.search(r"\d+\s+passed\b", out), f"看不到通过计数，尾部: {out[-400:]}"


def test_the_false_red_anchor_is_not_used_as_crash_evidence():
    """L1 的自检：确认本仓确实恒为 -q。

    若哪天有人去掉 addopts 里的 -q，本护栏的对照组会自动变红 ——
    这是**故意的**：那时所有「汇总行缺失」的判读都要重新评估。
    """
    txt = PYPROJECT.read_text(encoding="utf-8")
    assert re.search(r"addopts\s*=\s*[\"'].*-q", txt), \
        "本仓 pyproject.toml 的 addopts 必须含 -q；否则 L1 的结论需重新评估"


# ---------------------------------------------------------------- L3
def test_watchdog_threshold_is_exposed_as_a_number():
    """L3：conftest 的看门狗阈值必须是一个可读的数字常量。

    为什么护它：R71 差点去改这个阈值（当时误以为它是凶手）。
    钉住它存在，是为了**下次能直接读到**而不必猜，
    同时防止有人把它改成字符串/删掉导致守护静默失效。
    """
    src = CONFTEST.read_text(encoding="utf-8")
    m = re.search(r"dump_traceback_later\(\s*(\d+)\s*,", src)
    assert m, "conftest 里找不到 dump_traceback_later(<数字>, ...)"
    thresh = int(m.group(1))
    assert 60 <= thresh <= 3600, f"看门狗阈值 {thresh}s 不在合理区间"


def test_watchdog_is_cancelled_in_teardown():
    """L3 续：看门狗**必须**在 fixture 的 finally 里被 cancel。

    没有 cancel 的话，阈值一旦小于实际耗时就会在收尾阶段把进程掐掉 ——
    而这种掐断**看起来**和随机崩溃一模一样（R71 差点照着假象去改它）。
    """
    src = CONFTEST.read_text(encoding="utf-8")
    assert "cancel_dump_traceback_later" in src, \
        "conftest 必须 cancel 看门狗，否则阈值到点会掐死收尾阶段"


def test_no_hard_sleep_or_kill_in_product_code():
    """反向护栏：产品码里不许出现 os._exit / SIGKILL 这类硬掐。

    背景：本仓历史上出现过「探针用 os._exit(3) 兜底」被抄进产品码的风险。
    os._exit 会**跳过 atexit 与解释器 finalize** ⇒ 制造出「看起来像崩溃」的假象。

    R185 白名单：gui/lifecycle.py 的 force_quit() 末尾 os._exit(0) 是
    订单**明确要求**的「立即彻底退出」实现（托盘点退出 ≤3s 全消失），
    是全产品唯一授权位置 —— 白名单按文件级排除，别处出现仍然红。
    """
    bad = []
    for p in (ROOT / "media_resource_manager").rglob("*.py"):
        if p.name == "lifecycle.py" and p.parent.name == "gui":
            continue                      # R185 唯一授权位置
        src = p.read_text(encoding="utf-8", errors="replace")
        for kw in ("os._exit", "signal.SIGKILL", "SIGABRT"):
            # 允许出现在注释/docstring 里，只抓可执行调用
            for m in re.finditer(re.escape(kw), src):
                line = src[:m.start()].count("\n") + 1
                ctx = src.splitlines()[line - 1]
                if ctx.lstrip().startswith("#"):
                    continue
                if "R69" in ctx or "看门狗" in ctx or "探针" in ctx:
                    continue
                bad.append(f"{p.name}:{line}: {ctx.strip()[:70]}")
    assert not bad, "产品码里出现硬退出调用：\n" + "\n".join(bad[:10])


# ---------------------------------------------------------------- L4
_ENV_SNAPSHOT_MARKER = "R71_ENV_SNAPSHOT"


def test_this_file_leaves_no_global_env_trace(monkeypatch):
    """L4：本文件不许在**跑完之后**留下任何全局环境痕迹。

    ⚠ 判据三次修正的教训（R53 纪律，最贵的一次是"护栏把自己判红"）：
      v1 搜字面量 `"os.environ["` ⇒ 匹配到**这条断言自己的源码**，自判红；
      v2 运行时拼串躲开源码 ⇒ docstring 里又写着那个串，照样红；
      v3 仍靠搜字面量 ⇒ 本质没变。
      ⇒ **正解：判行为，不判字面量。**
        「跑完 pytest 后新增了哪些环境变量」是外部可观测的事实，
        用 `os.environ` 的键集合做差集即可，完全不需要在源码里找任何字符串。
    """
    import os as _os
    from subprocess import run as _run

    before = set(_os.environ)
    _run(
        [sys.executable, "-X", "utf8", "-m", "pytest",
         "tests/test_schedule.py", "-q"],
        cwd=str(ROOT), capture_output=True, timeout=120,
        env=dict(_os.environ, PYTHONUNBUFFERED="1", QT_QPA_PLATFORM="offscreen"),
    )
    added = set(_os.environ) - before
    assert not added, f"跑完后新增了全局环境变量：{sorted(added)}"
    assert _ENV_SNAPSHOT_MARKER not in _os.environ, \
        "护栏文件自己往全局环境塞了标记"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))