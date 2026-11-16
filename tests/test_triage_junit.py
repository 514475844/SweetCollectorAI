# -*- coding: utf-8 -*-
"""test_triage_junit —— 给tools/triage_junit.py 兜底（第九/十轮踩坑的复发预防）。

背景：safe-delete 守卫按回合累计删除次数，会让全量 pytest 平白多出几条 FAILED。
过去两轮都是「人工看日志 → 手动另起一轮复跑 → 肉眼判断」，每次30 分钟。
本测试把分类逻辑钉死，保证 triage_junit 以后不会把真回归误判成守卫误伤。
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import triage_junit  # noqa: E402


GUARD_STDERR = (
    "SystemExit: 1\n"
    "[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] "
    '{"count":3981,"threshold":50,"scope":"turn"}'
)


def _write_junit(path, cases, tests=None, failures=None, errors="0", skipped="0", time_s="1.0"):
    """cases: [(classname, name, failure_text_or_None)]"""
    body = []
    for cls, name, fail in cases:
        node = '<testcase classname="%s" name="%s" time="0.1"' % (cls, name)
        if fail is None:
            body.append(node + "/>")
        else:
            body.append(node + "><failure message=\"boom\">%s</failure></testcase>" % fail)
    n_fail = failures if failures is not None else sum(1 for c in cases if c[2] is not None)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests"><testsuite name="pytest" errors="%s" failures="%s" '
        'skipped="%s" tests="%s" time="%s">%s</testsuite></testsuites>'
        % (errors, n_fail, skipped, tests if tests is not None else len(cases), time_s, "".join(body))
    )
    path.write_text(xml, encoding="utf-8")
    return str(path)


def test_guard_failure_is_classified_as_guard(tmp_path):
    p = _write_junit(tmp_path / "j.xml", [
        ("tests.test_a", "test_dislike_deletes", GUARD_STDERR),
    ])
    summary, guard, real = triage_junit.parse(p)
    assert guard == ["tests.test_a::test_dislike_deletes"]
    assert real == []
    assert summary["failures"] == 1


def test_real_failure_never_classified_as_guard(tmp_path):
    """没有守卫标记的一律当真回归 —— 宁可误报也不漏报。"""
    p = _write_junit(tmp_path / "j.xml", [
        ("tests.test_b", "test_broke", "AssertionError: expected 3 got 4"),
    ])
    summary, guard, real = triage_junit.parse(p)
    assert guard == []
    assert real == ["tests.test_b::test_broke"]


def test_passing_test_is_ignored(tmp_path):
    p = _write_junit(tmp_path / "j.xml", [
        ("tests.test_c", "test_ok", None),
        ("tests.test_c", "test_skipped", None),
    ], skipped="1")
    summary, guard, real = triage_junit.parse(p)
    assert guard == [] and real == []
    assert summary["skipped"] == 1
    assert summary["failures"] == 0


def test_time_is_float_and_counts_are_int(tmp_path):
    """回归：time="1793.551" 曾让 int() 直接抛 ValueError 崩掉整个工具。"""
    p = _write_junit(tmp_path / "j.xml", [("tests.test_d", "test_x", None)], time_s="1793.551")
    summary, _, _ = triage_junit.parse(p)
    assert isinstance(summary["time"], float) and summary["time"] == pytest.approx(1793.551)
    assert isinstance(summary["tests"], int)


def test_mixed_guard_and_real(tmp_path):
    p = _write_junit(tmp_path / "j.xml", [
        ("tests.test_e", "test_guard", GUARD_STDERR),
        ("tests.test_e", "test_real", "AssertionError: nope"),
        ("tests.test_e", "test_pass", None),
    ])
    summary, guard, real = triage_junit.parse(p)
    assert len(guard) == 1 and len(real) == 1
    assert summary["tests"] == 3 and summary["failures"] == 2


def test_rerun_command_has_exactly_one_junit_xml_flag():
    """回归：曾生成 `--junit-xml --junit-xml=...`，pytest 会把后者当前者取值。"""
    cmd = triage_junit.build_rerun_cmd(["tests.t::test_a"], ROOT, "tests/junit_guard_rerun.xml")
    assert cmd.count("--junit-xml") == 1
    assert "--junit-xml=tests/junit_guard_rerun.xml" in cmd
    assert "QT_QPA_PLATFORM=offscreen" in cmd


def test_rerun_command_quotes_nodeids():
    cmd = triage_junit.build_rerun_cmd(["tests.t::test_a"], ROOT, "out.xml")
    # ⚠️ `::` 前面必须是**斜杠路径**。junit 里的 classname 是点分的
    # `tests.t::test_a`，直接当 pytest 参数会报
    # "file or directory not found"（EXIT=4，第 17 轮实测踩到）。
    assert '"tests/t.py::test_a"' in cmd, cmd


def test_path_nodeid_conversion_is_idempotent():
    """已经是路径形式的不该被再转一遍（`tests/t.py::x` 不是点分名）。"""
    assert triage_junit._to_path_nodeid("tests/t.py::test_a") \
        == "tests/t.py::test_a"
    assert triage_junit._to_path_nodeid("tests.t::test_a") \
        == "tests/t.py::test_a"
    assert triage_junit._to_path_nodeid("t.py::test_a") \
        == "t.py::test_a"


def test_bare_testsuite_root_is_supported(tmp_path):
    """有些工具写出的是裸 <testsuite>，不能只认 testsuites 根。"""
    p = tmp_path / "bare.xml"
    p.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuite name="pytest" errors="0" failures="1" skipped="0" tests="1" time="0.5">'
        '<testcase classname="tests.t" name="test_g" time="0.1">'
        '<failure message="m">%s</failure></testcase></testsuite>' % GUARD_STDERR,
        encoding="utf-8",
    )
    summary, guard, real = triage_junit.parse(str(p))
    assert guard == ["tests.t::test_g"] and real == []
    assert summary["tests"] == 1 and summary["time"] == pytest.approx(0.5)


def test_main_exit_code_zero_when_only_guard(tmp_path, capsys):
    p = _write_junit(tmp_path / "j.xml", [("tests.t", "test_g", GUARD_STDERR)])
    rc = triage_junit.main([p])
    out = capsys.readouterr().out
    assert rc == 0
    assert "无真回归" in out


def test_main_exit_code_one_on_real_regression(tmp_path, capsys):
    p = _write_junit(tmp_path / "j.xml", [("tests.t", "test_r", "AssertionError: x")])
    rc = triage_junit.main([p])
    out = capsys.readouterr().out
    assert rc == 1
    assert "存在真回归" in out


def test_main_survives_broken_xml(tmp_path, capsys):
    bad = tmp_path / "bad.xml"
    bad.write_text("<testsuite><<<", encoding="utf-8")
    rc = triage_junit.main([str(bad)])
    assert rc == 1
    out = capsys.readouterr().out
    assert "读不了" in out
    # R53 补：文件**存在但解析不了**走的是 except 分支（缺文件走前置守卫那条路），
    # 所以这条断言是「except 分支也必须记 unreadable」的唯一覆盖 ——
    # 少了它，删掉 except 里的 append 反向验证会**抓不到红**（本轮真踩到）。
    assert "结论：无真回归" not in out, "存在但读不了的 junit 同样是未知，不是干净"
    assert "未通过" in out


# ============================================================ R53：缺失 ≠ 干净
# 背景（R53 实测）：全量 pytest 挂在 95%、junit 根本没生成，
# triage_junit 读不到文件却在最后一行打出「结论：无真回归」——
# **假绿**，比不 triage 更危险（人会以为「查过了、是干净的」，于是放过一个未通过轮次）。
# 这几条把「读不到」钉死成「未通过」，且明确否掉「真回归 0 条 = 干净」的推论。
def test_missing_junit_is_not_reported_as_clean(tmp_path, capsys):
    """junit 不存在时：rc=1，且**绝不能**出现「结论：无真回归」。"""
    missing = str(tmp_path / "never_written.xml")
    rc = triage_junit.main([missing])
    out = capsys.readouterr().out
    assert rc == 1, "缺失 junit 必须以非 0 退出，否则无人看得出这轮没跑成"
    assert "结论：无真回归" not in out, "缺失 junit 时谎报『无真回归』= 假绿"
    assert "未通过" in out


def test_missing_junit_says_the_zero_is_unknown_not_clean(tmp_path, capsys):
    """必须显式说明「真回归 0 条」对这些轮次**不成立**（是未知，不是干净）。"""
    rc = triage_junit.main([str(tmp_path / "nope.xml")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "读不到" in out
    assert "未知" in out


def test_present_clean_junit_still_says_no_regression(tmp_path, capsys):
    """反向：junit 真的存在且干净时，**仍必须**打「无真回归」+ rc=0。

    这条是护栏的反向验证 —— 只测「缺失时报错」是不够的：
    万一改坏了正常路径（比如一律报错），上面两条照样全绿。
    """
    p = _write_junit(tmp_path / "ok.xml", [("tests.t", "test_ok", None)])
    rc = triage_junit.main([str(p)])
    out = capsys.readouterr().out
    assert rc == 0, "干净 junit 必须 rc=0，别被上一条的守卫误伤"
    assert "结论：无真回归" in out
    assert "未通过" not in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))