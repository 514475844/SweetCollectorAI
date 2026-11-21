"""V0.0.72 护栏：本地描述模型一档不留误导死提示（V0.0.74 演进说明见下）。

V0.0.72 当时：模型没接入，该档必须置灰禁用 + 标明暂不可用，
提示「请在 AI 模型页下载」是误导（模型页里根本没有）。
V0.0.74：BLIP 真正接入、模型页加上了可下载条目，该档解除禁用——
「误导」钉子依然有效（死提示不许回来），「禁用」钉子按时代作废，
由 test_v074 反向钉住（local 必须可选 + 文案指向真实可下载模型）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_local_method_has_honest_description():
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    assert "name: '本地描述模型'" in c, "本地模型档不见了"
    line = [x for x in c.splitlines() if "name: '本地描述模型'" in x][0]
    block = "\n".join(c.splitlines()[c.splitlines().index(line):c.splitlines().index(line) + 2])
    # 文案必须指向真实存在的东西（V0.0.74：可下载的 BLIP 条目）
    assert "BLIP" in block, "本地模型文案没指向真实可下载的模型"


def test_no_misleading_download_hint():
    b = _read("media_resource_manager/auto_vocab.py")
    assert "请在 AI 模型页下载后重试" not in b, "还留着误导的下载提示"
    assert "暂不可用" in b, "后端降级文案未如实说明暂不可用"
    c = _read("media_resource_manager/webapp/src/components/AutoVocab.vue")
    assert "首次要下约 1G" not in c, "前端还留着'下 1G'的误导文案"


def test_scan_logic_untouched():
    """这轮只修提示，扫描/写库逻辑必须原样。"""
    b = _read("media_resource_manager/auto_vocab.py")
    for fn in ("scan_vocab", "scan_local", "scan_api", "apply_choices"):
        assert f"def {fn}" in b, f"扫描逻辑被动了：{fn}"


def test_version_and_changelog():
    import re
    v = _read("media_resource_manager/__init__.py")
    m = re.search(r'VERSION = "0\.0\.(\d+)"', v)
    assert m and int(m.group(1)) >= 72, "版本号未升到 0.0.72"
    ch = _read("CHANGELOG.md")
    assert "V0.0.72" in ch, "更新日志缺 V0.0.72"
