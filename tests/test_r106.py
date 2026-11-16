"""R106：移除「按时段自动切换模型（R86B）」。

背景：R86B 来自台式机对单的误读（用户真实意图是给笔记本 WorkBuddy 自动任务
分时段绑模型，与软件无关）。现状是占位实现，从未真正接入，移除无功能损失。

移除范围（本单已落地，这里钉死不回归）：
  · config 里的 ai_slot_auto / ai_slot_day / ai_slot_evening / ai_slot_night /
    ai_slot_applied 字段删除；
  · main_window 的 AI 模型对话框里「按时段自动切换模型（省积分）」复选框 + 三个
    时段下拉 + 相关文案全部删除；do_save 不再写这些字段；
  · _apply_ai_slot / _ai_busy 方法及后台 _slot_timer 定时器删除；
  · ai_schedule 模块整体删除（due() 的时段切换逻辑随之消失）。

护栏：全树 grep 无 ai_slot / 按时段 / 省积分 / 当前时段 / slot_auto / slot_combos
死代码引用；老配置带 ai_slot_* 能正常加载（from_dict 自动忽略）。
"""
import pytest

pytest.importorskip("PySide6")

from media_resource_manager.config import AppConfig
from media_resource_manager.gui import main_window as mw_mod

from pathlib import Path

_AI_SLOT_FIELDS = (
    "ai_slot_auto", "ai_slot_day", "ai_slot_evening",
    "ai_slot_night", "ai_slot_applied",
)


# ============================================================ config 字段已删

def test_config_has_no_ai_slot_fields():
    """dataclass 不再有 ai_slot_* 五个字段。"""
    fields = AppConfig.__dataclass_fields__
    for f in _AI_SLOT_FIELDS:
        assert f not in fields, f"config 还残留字段 {f}"


def test_old_config_with_ai_slot_ignored():
    """老配置带 ai_slot_* 字段：from_dict 不报错，且这些键不会写进配置。"""
    d = {
        "ai_slot_auto": True,
        "ai_slot_day": "Step-5-Preview",
        "ai_slot_evening": "HY3",
        "ai_slot_night": "HY4",
        "ai_slot_applied": "HY3",
        "ai_api_model": "",
    }
    cfg = AppConfig.from_dict(d)
    out = cfg.to_dict()
    for f in _AI_SLOT_FIELDS:
        assert f not in out, f"老配置的 {f} 又被写回了配置"
    # 不得因老配置而自动改模型
    assert cfg.ai_api_model == ""


# ============================================================ main_window 无切换逻辑

def test_main_window_has_no_slot_switch_methods():
    """MainWindow 不再有自动切换模型的方法/定时器。"""
    mw = mw_mod.MainWindow
    assert not hasattr(mw, "_apply_ai_slot"), "还残留 _apply_ai_slot"
    assert not hasattr(mw, "_ai_busy"), "还残留 _ai_busy"
    assert not hasattr(mw, "_slot_timer"), "还残留 _slot_timer 属性定义"


def test_main_window_source_no_slot_references():
    """源码里不得再出现按时段切换相关的死代码引用。"""
    src = Path(mw_mod.__file__).read_text(encoding="utf-8")
    for token in ("slot_auto", "slot_combos", "ai_slot", "按时段",
                  "省积分", "当前时段", "_slot_timer", "_apply_ai_slot",
                  "_ai_busy"):
        assert token not in src, f"main_window 源码还残留「{token}」"


# ============================================================ ai_schedule 模块已删

def test_ai_schedule_module_removed():
    """ai_schedule 模块整体删除（含 due() 的时段切换逻辑）。

    R106 后 ai_api_model 永不被本功能自动改；导入它必须失败。
    """
    with pytest.raises(ImportError):
        from media_resource_manager import ai_schedule  # noqa: F401
