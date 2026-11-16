"""规则漂移检查工具的单测（纯本地，无网络/无 GUI）。

覆盖 check_drift 的核心行为：
  * 两目录一致 -> ok=True
  * 部署缺文件 -> DRIFT
  * 内容不同（差一个字节）-> DRIFT
  * 仅部署多文件 -> WARN（不算致命）
  * 部署目录不存在 -> 跳过（ok=True，不误报）
"""
from __future__ import annotations

from pathlib import Path

from tools.check_rules_drift import check_drift

SAMPLE = """
name: sample
base_url: https://example.com
allowed_domains: [example.com]
image_link_selectors:
  - kind: css
    expr: "img[src]"
    attr: src
next_page_selectors:
  next:
    kind: css
    expr: "a.next"
    attr: href
max_depth: 2
"""


def _mk(tmp_path: Path, name="sample.yaml") -> Path:
    p = tmp_path / name
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_identical_dirs_ok(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _mk(src)
    _mk(dst)
    ok, lines = check_drift(src, dst)
    assert ok is True
    assert any("[OK]" in l for l in lines)
    assert not any("[DRIFT]" in l for l in lines)


def test_missing_in_dst_is_drift(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _mk(src)
    _mk(src, "extra.yaml")  # 源码多一个，部署缺
    ok, lines = check_drift(src, dst)
    assert ok is False
    assert any("extra.yaml" in l and "[DRIFT]" in l for l in lines)


def test_content_diff_one_byte_is_drift(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _mk(src)
    _mk(dst)
    # 部署侧多一个字节 -> 不同步
    (dst / "sample.yaml").write_text(SAMPLE + "\n", encoding="utf-8")
    ok, lines = check_drift(src, dst)
    assert ok is False
    assert any("sample.yaml" in l and "[DRIFT]" in l for l in lines)


def test_only_in_dst_is_warn_not_fatal(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    _mk(src)              # 共有 sample.yaml（一致）
    _mk(dst)              # 共有 sample.yaml（一致）
    _mk(dst, "useronly.yaml")  # 仅部署多一个
    ok, lines = check_drift(src, dst)
    assert ok is True  # WARN 不算致命
    assert any("useronly.yaml" in l and "[WARN]" in l for l in lines)


def test_dst_missing_is_skip(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _mk(src)
    ok, lines = check_drift(src, tmp_path / "nope")
    assert ok is True
    assert any("[SKIP]" in l for l in lines)
