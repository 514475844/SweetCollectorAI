"""守卫测试：合规门禁必须保持【已删除】状态（2026-10-26 用户定案）。

背景：R186 当时只做了"off 档跳过"没删干净，导致 2026-10-26 清数据后
compliance 表清空 → 门禁全线开火 → 24 任务全灭（详见当日日志）。
本测试钉死"门禁不存在"这个事实，防止将来被无意加回来：
  1. engine 里不得再有 ComplianceChecker / permission basis 门禁；
  2. fetcher 不得再有 robots 阻断（RobotDisallowed raise）；
  3. compliance 包不得再存在；
  4. 默认合规档必须是 off。
"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "media_resource_manager"


def _src(rel: str) -> str:
    return (PKG / rel).read_text(encoding="utf-8", errors="replace")


def test_compliance_package_is_gone():
    assert not (PKG / "compliance").exists(), (
        "media_resource_manager/compliance/ 包必须保持删除状态")


def test_engine_has_no_compliance_gate():
    src = _src("engine.py")
    assert "ComplianceChecker" not in src, "engine 不得再引用合规检查器"
    assert "compliance not cleared" not in src, "engine 不得再有授权门禁报错"
    assert "ComplianceChecker(" not in src, "engine 不得再构造合规检查器"
    assert "SELECT 1 FROM compliance" not in src, "engine 不得再查授权表"

def test_fetcher_has_no_robots_block():
    src = _src("crawler/fetcher.py")
    assert "raise RobotDisallowed" not in src, (
        "fetcher 不得再有 robots 阻断（RobotDisallowed raise）")


def test_default_compliance_level_is_off():
    sys.path.insert(0, str(ROOT))
    from media_resource_manager.config import DEFAULT_COMPLIANCE_LEVEL
    assert DEFAULT_COMPLIANCE_LEVEL == "off", (
        "默认合规档必须是 off（门禁已删，此值仅兜底）")


def test_cli_has_no_comply_command():
    src = _src("__main__.py")
    assert "cmd_comply" not in src, "__main__ 不得再有 comply 子命令"


def test_engine_run_keeps_basis_param():
    """删门禁不许伤到引擎本体：run() 签名兼容 basis（老调用方仍会传）。"""
    sys.path.insert(0, str(ROOT))
    from media_resource_manager.engine import Engine
    sig = inspect.signature(Engine.run)
    assert "basis" in sig.parameters, (
        "run() 保留 basis 形参（老调用方/CLI 仍会传，删除属破坏性变更）")
