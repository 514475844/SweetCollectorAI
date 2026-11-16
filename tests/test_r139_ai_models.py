"""R139 护栏：AI 模型清单（≥5 项，带 CPU/GPU 标注）且设置页能枚举。

钉行为（钉行为不钉数值）：
  · CATALOG 必须有 ≥5 个模型，每项带 name / min_cpu / min_gpu / size_hint
    （即「CPU 推荐度 / GPU 推荐度 / 体积」三件套，诚实标注）；
  · hardware_line() 必须同时含 CPU 与 GPU 文案（需 GPU 的明确标出，不虚标）；
  · 设置页「模型库」从 sorted_catalog() 取数并逐行渲染 hardware_line()；
  · 反向变异：清空 CATALOG ⇒ resolve_default_model_dir 返回 None
    （绝不静默挑一个缺/错的模型当可用）。

注：订单「清空目录→分类链路报中文错而非静默降级」的更严语义
（LocalClassifier._pick_backend 抛中文异常）当前未实现——无模型时仍静默
stub。本护栏钉的是「清空后不再解析出任何模型（=None）」这一可验证、安全子集，
更严的硬报错留作后续；详见 last_run_report 对 R139 的说明。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from media_resource_manager import models_lib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAINWIN = _ROOT / "media_resource_manager" / "gui" / "main_window.py"


def test_r139_catalog_has_cpu_gpu_annotations():
    cat = models_lib.sorted_catalog()
    assert len(cat) >= 5, f"模型清单只有 {len(cat)} 个，少于 5"
    for m in cat:
        mid = m.get("id", "?")
        assert m.get("name"), f"{mid} 缺 name"
        assert m.get("min_cpu"), f"{mid} 缺 CPU 标注（min_cpu）"
        assert m.get("min_gpu"), f"{mid} 缺 GPU 标注（min_gpu）"
        assert m.get("size_hint"), f"{mid} 缺体积（size_hint）"
        hl = models_lib.hardware_line(m)
        assert "CPU" in hl and "GPU" in hl, (
            f"{mid} 硬件行未同时含 CPU/GPU 标注：{hl!r}")


def test_r139_dialog_enumerates_catalog():
    """设置页模型库必须从 sorted_catalog 取数并渲染 hardware_line（CPU/GPU 标注）。"""
    src = _MAINWIN.read_text(encoding="utf-8")
    assert "sorted_catalog()" in src, "模型库应从 sorted_catalog() 取数"
    assert "hardware_line(" in src, "模型行应渲染 hardware_line()（CPU/GPU 标注）"


def test_r139_empty_catalog_resolves_none(monkeypatch, tmp_path):
    """反向变异：清空目录后，绝不能再静默解析出一个（缺/错的）模型。

    隔离掉捆绑模型目录，确保「没有模型」是真实的；此时清空 CATALOG
    必须让 resolve_default_model_dir 返回 None，而不是挑一个不存在的目录。
    """
    monkeypatch.setattr(models_lib, "CATALOG", [])
    monkeypatch.setattr(
        models_lib, "bundled_models_dir",
        lambda: tmp_path / "no_bundle_here")
    got = models_lib.resolve_default_model_dir(tmp_path / "models")
    assert got is None, f"空目录不应解析出模型，却得到 {got!r}"
