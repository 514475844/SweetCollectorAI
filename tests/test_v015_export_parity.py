"""V0.0.15 护栏：诊断包两端同格式加密 + 网页设置补齐显卡开关。

钉死两条会悄悄回退的事：
  1. 需求 19「所有导出的任务和所有导出的信息必须加密」——诊断包含脱敏配置、
     库统计、日志，含库版本还带图片路径与来源网址。网页端曾经直接送明文
     zip，比桌面端少一层加密。两端必须都走 ``crypt.encrypt_bytes`` 出 .mrmx。
  2. 后端 ``_api_settings`` 早就把 ``ai_gpu`` 回给网页、保存白名单也收它，
     只是设置页没渲染 —— 用户以为「用显卡加速」只能在桌面改。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEBUI = ROOT / "media_resource_manager" / "webui.py"
MAIN_WINDOW = ROOT / "media_resource_manager" / "gui" / "main_window.py"
SETTINGS = ROOT / "media_resource_manager" / "webapp" / "src" / "views" / "Settings.vue"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _diag_block(text: str, marker: str, span: int = 2600) -> str:
    """截取诊断包方法体，避免整文件里别处的 zip / mrmx 字样造成假绿。"""
    i = text.find(marker)
    assert i >= 0, f"未找到 {marker}"
    return text[i:i + span]


def test_web_diagnostic_is_encrypted_mrmx():
    block = _diag_block(_read(WEBUI), "def _api_diagnostic(self, u):")
    assert "ek.encrypted(" in block, "网页诊断包未调用统一加密函数"
    assert ".mrmx" in block, "网页诊断包扩展名不是 .mrmx"
    assert "application/zip" not in block, "网页诊断包仍以明文 zip 送出"


def test_desktop_diagnostic_uses_same_crypto():
    block = _diag_block(_read(MAIN_WINDOW),
                        "def export_diagnostic_bundle(self):", span=12000)
    assert "encrypt_bytes" in block
    assert ".mrmx" in block


def test_web_settings_exposes_gpu_switch():
    """网页设置页要能决定「用不用显卡」。

    V0.0.40 改判（不是放宽）：原来查的是布尔开关 ``{ k: 'ai_gpu'``，但那个
    开关和「推理设备」下拉是同一件事的两套写法（引擎只在自动档才看它），
    并存的结果是「勾了取消都没反应」。现在网页只留档位一项，判据随之改成
    ``ai_device``；显卡相关的设置仍然在网页上可改。
    """
    text = _read(SETTINGS)
    assert "{ k: 'ai_device'" in text, "网页设置页缺少推理档位设置"
    assert "{ k: 'ai_gpu'" not in text, "旧的显卡开关又回来了（和设备档位打架）"
    assert "导出加密日志诊断包" in text and "导出加密含库诊断包" in text
    assert ".mrmx" in text


def test_encrypted_blob_roundtrip():
    """加密包能被本软件解开，且绝不是原文。"""
    from media_resource_manager.crypt import decrypt_bytes
    from media_resource_manager.export_kit import encrypted

    raw = b"diagnostic-zip-content"
    blob = encrypted(raw, "")
    assert blob != raw
    assert decrypt_bytes(blob, "") == raw


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
