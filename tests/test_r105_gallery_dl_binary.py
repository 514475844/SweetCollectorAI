"""R105 护栏：vendor/gallery-dl.exe 必须是**真能跑**的自包含 onefile 二进制。

背景（R105 抓到的真 bug，交付级）：旧真身 17977277 字节是 PyInstaller
**onedir** 产物，只拷了 exe、没带 sibling `_internal/`（缺 python313.dll）
⇒ 交付目录里 `gallery-dl.exe --version` 直接 PYI-ERROR 挂掉，整条
gallery-dl 快路径从 10-07 起就是死的。历届验包只比对字节数、没有人真跑过
二进制，所以假绿了十几轮（R105 首次真跑才发现）。
备份：.workbuddy/r105_backup/gallery-dl.exe.broken_onedir_17977277。

本测试判据（全部真跑，不猜）：
  1. 在**无任何 sibling 的隔离临时目录**里跑 `--version`：rc=0 且输出 semver
     —— 证明 onefile 自包含，不依赖旁边的 _internal/；
  2. 跑 `--list-extractors`：rc=0 且注册表含一批已知站点
     —— 证明 extractor 动态子模块都打进去了（`2ch` 这种数字开头的也会漏，
     漏了 import 期不报、运行期才炸，所以必须真跑）；
  3. build_nodel.py 的 GALLERY_DL_BYTES 与 vendor 真身实测字节数一致
     —— 防拷到半截/旧版（与 test_r59 的同名判据互补，那里管构建脚本口径）。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_GDL = os.path.join(ROOT, "vendor", "gallery-dl.exe")

# 已知站点：覆盖字母开头与数字开头（2ch）两类提取器命名
KNOWN_EXTRACTORS = ("danbooru", "imgur", "twitter", "reddit",
                    "weibo", "bilibili", "pixiv", "2ch")


def _run_isolated(args: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    """在无 sibling 的临时目录里跑 vendor 真身——onedir 残包在此必挂。"""
    with tempfile.TemporaryDirectory(prefix="r105_gdl_") as td:
        return subprocess.run([VENDOR_GDL, *args], cwd=td, timeout=timeout,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")


def test_r105_vendor_gallery_dl_exists():
    assert os.path.isfile(VENDOR_GDL), "vendor/gallery-dl.exe 缺失"


def test_r105_gallery_dl_version_runs_isolated():
    """隔离目录 --version：rc=0 + semver。旧 onedir 残包在此步必败。"""
    p = _run_isolated(["--version"])
    out = (p.stdout or "") + (p.stderr or "")
    assert p.returncode == 0, f"--version rc={p.returncode}: {out[-500:]}"
    m = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", out)
    assert m, f"--version 输出里没有 semver：{out[-300:]}"
    # 版本下限：requirements-docker.txt 的容器侧下限不得高于真身版本
    ver = tuple(int(x) for x in m.groups())
    assert ver >= (1, 32, 0), f"gallery-dl 版本过低：{ver}"


def test_r105_gallery_dl_extractor_registry_complete():
    """--list-extractors：注册表必须含已知站点（动态子模块打进包的证明）。"""
    p = _run_isolated(["--list-extractors"])
    out = (p.stdout or "") + (p.stderr or "")
    assert p.returncode == 0, f"--list-extractors rc={p.returncode}: {out[-500:]}"
    assert len(out.splitlines()) > 1000, "提取器注册表异常小，疑似漏打包"
    missing = [k for k in KNOWN_EXTRACTORS if k not in out]
    assert not missing, f"注册表缺少已知提取器：{missing}（漏打包动态子模块）"


def test_r105_build_nodel_bytes_match_vendor():
    """build_nodel.py 的 GALLERY_DL_BYTES 必须等于 vendor 真身实测字节数。"""
    src = open(os.path.join(ROOT, "build_nodel.py"), encoding="utf-8").read()
    m = re.search(r"GALLERY_DL_BYTES\s*=\s*(\d+)", src)
    assert m, "build_nodel.py 缺少 GALLERY_DL_BYTES"
    assert int(m.group(1)) == os.path.getsize(VENDOR_GDL), \
        f"常量 {m.group(1)} != vendor 实测 {os.path.getsize(VENDOR_GDL)}"


def test_r105_requirements_docker_has_version_floor():
    """容器侧 requirements-docker.txt 必须带 gallery-dl 版本下限（不许裸名）。"""
    p = os.path.join(ROOT, "requirements-docker.txt")
    assert os.path.isfile(p), "requirements-docker.txt 缺失"
    for line in open(p, encoding="utf-8"):
        s = line.strip()
        if s.startswith("gallery-dl"):
            assert re.search(r"gallery-dl\s*>=\s*\d+\.\d+", s), \
                f"gallery-dl 依赖行没有版本下限：{s}"
            break
    else:
        raise AssertionError("requirements-docker.txt 里没有 gallery-dl 依赖行")


def test_r105_broken_onedir_binary_not_shipped():
    """旧残包（17977277 字节 onedir）不许回潮到 vendor/。"""
    if not os.path.isfile(VENDOR_GDL):
        return
    assert os.path.getsize(VENDOR_GDL) != 17977277, \
        "vendor 里又是 17977277 字节的坏 onedir 包（见 r105_backup）"


def test_r105_spec_bundles_gallery_dl_registry():
    """pyinstaller.spec 必须把 gallery_dl/extractor 收进包。

    否则冻结态 supports() 恒 False（ImportError 被吞）⇒ 快路径全灭、
    无规则任务误报「这个网址我们还不会抓」（R105 实测的第二只真 bug）。
    """
    spec = os.path.join(ROOT, "build", "pyinstaller.spec")
    src = open(spec, encoding="utf-8").read()
    assert "gallery_dl" in src, "spec 未收录 gallery_dl"
    assert "collect_submodules('gallery_dl.extractor')" in src or \
        "collect_submodules(\"gallery_dl.extractor\")" in src, \
        "spec 未 collect_submodules gallery_dl.extractor（数字开头模块名会漏）"


def test_r105_supports_works_in_dev_interpreter():
    """开发解释器里 supports() 必须真能判定（守护 _modules_internal 链路）。"""
    sys.path.insert(0, ROOT)
    try:
        from media_resource_manager.backends.gallery_dl import supports
        assert supports("https://danbooru.donmai.us/posts/1") is True, \
            "danbooru URL 判定失败——gallery_dl 包或其 extractor 注册表坏了"
    finally:
        sys.path.remove(ROOT)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
