"""R152-E6 护栏：config.json 敏感字段落盘加密。

E6 修一件事：web_password / ai_api_key 原来明文落盘 → 现在用
security.crypto 的 PBKDF2 派生 Fernet 密钥加密，密钥文件 config.key
与 config.json 同目录（便携模式整体拷贝两文件一起走）。

判据：
  · save_config 后磁盘上不见明文、可见 enc1$ 标记；
  · load_config round-trip 还原；
  · 旧明文 config.json 自动透传（升级不受影响）；
  · 密钥文件丢失 → 敏感字段置空、不崩、其余字段不受影响；
  · from_dict 不解密（解密只在 load_config 磁盘边界）；
  · 空值不标记；cryptography 缺席降级明文直写；
  · 跨目录不串钥（A 目录密文在 B 目录解不开 → 置空）。

反向可变：
  · save 不加密 → 明文泄漏测试红；
  · load 不解密 → round-trip 测试红；
  · 解密失败不置空 → 密钥丢失测试红；
  · MARK 不匹配 → 标记测试红。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from media_resource_manager import config as config_mod
from media_resource_manager.config import AppConfig, load_config, save_config
from media_resource_manager.security.config_secrets import (
    MARK, KEY_FILE_NAME, ConfigSecretError, ConfigSecrets)


def test_save_no_plaintext_on_disk(tmp_path):
    """save 后磁盘上不见明文密码/key，可见 enc1$ 标记。"""
    cfg = AppConfig()
    cfg.web_password = "s3cret-pw-123"
    cfg.ai_api_key = "sk-abcdef-987654"
    p = tmp_path / "config.json"
    save_config(cfg, p)
    raw = p.read_text(encoding="utf-8")
    assert "s3cret-pw-123" not in raw, "web_password 明文落盘"
    assert "sk-abcdef-987654" not in raw, "ai_api_key 明文落盘"
    # ⚠ 钉**字面量**而非 MARK 变量：MARK 是落盘格式契约（旧版本写的密文
    # 前缀），变异 MARK 时这条必须红 —— 用变量等于自己跟自己对答案（R73）。
    assert "enc1$" in raw, "密文标记 enc1$ 不在磁盘上"


def test_round_trip(tmp_path):
    """save → load round-trip 还原敏感字段。"""
    cfg = AppConfig()
    cfg.web_password = "s3cret-pw-123"
    cfg.ai_api_key = "sk-abcdef-987654"
    cfg.ui_preset = "night"
    p = tmp_path / "config.json"
    save_config(cfg, p)
    cfg2 = load_config(p)
    assert cfg2.web_password == "s3cret-pw-123"
    assert cfg2.ai_api_key == "sk-abcdef-987654"
    assert cfg2.ui_preset == "night"


def test_old_plaintext_passthrough(tmp_path):
    """旧明文 config.json（无标记）→ 自动透传，升级不受影响。"""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "web_password": "old-pw", "ai_api_key": "old-key",
        "ui_preset": "classic",
    }), encoding="utf-8")
    cfg = load_config(p)
    assert cfg.web_password == "old-pw"
    assert cfg.ai_api_key == "old-key"


def test_key_file_created(tmp_path):
    """save 后 config.key 与 config.json 同目录。"""
    cfg = AppConfig()
    cfg.web_password = "s3cret-pw-123"
    p = tmp_path / "config.json"
    save_config(cfg, p)
    assert (tmp_path / KEY_FILE_NAME).exists(), "config.key 未创建"


def test_key_lost_empties_secrets(tmp_path):
    """密钥文件丢失 → load 不崩、敏感字段置空、其余字段不受影响。"""
    cfg = AppConfig()
    cfg.web_password = "s3cret-pw-123"
    cfg.ai_api_key = "sk-abcdef-987654"
    cfg.ui_preset = "night"
    p = tmp_path / "config.json"
    save_config(cfg, p)
    # 模拟密钥文件丢失 + 进程重启（清缓存）
    (tmp_path / KEY_FILE_NAME).unlink()
    config_mod._SECRET_BOX_CACHE.clear()
    cfg2 = load_config(p)
    assert cfg2.web_password == "", "密钥丢失后 web_password 应置空"
    assert cfg2.ai_api_key == "", "密钥丢失后 ai_api_key 应置空"
    assert cfg2.ui_preset == "night", "非敏感字段不应受影响"


def test_from_dict_no_decrypt():
    """from_dict 不解密（解密只在 load_config 磁盘边界）。"""
    d = {"web_password": MARK + "garbage-token", "ai_api_key": "plain-key"}
    cfg = AppConfig.from_dict(d)
    assert cfg.web_password == MARK + "garbage-token"
    assert cfg.ai_api_key == "plain-key"


def test_empty_not_marked(tmp_path):
    """空值不标记（空密码就该存空串）。"""
    cfg = AppConfig()
    cfg.web_password = ""
    cfg.ai_api_key = ""
    p = tmp_path / "config.json"
    save_config(cfg, p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["web_password"] == "", "空密码不应被标记加密"
    assert raw["ai_api_key"] == "", "空 key 不应被标记加密"
    assert MARK not in p.read_text(encoding="utf-8")


def test_no_cryptography_fallback(tmp_path, monkeypatch):
    """cryptography 缺席 → 降级明文直写（与 E7 口径一致）。"""
    monkeypatch.setattr(ConfigSecrets, "_build", lambda self: None)
    cfg = AppConfig()
    cfg.web_password = "s3cret-pw-123"
    p = tmp_path / "config.json"
    save_config(cfg, p)
    raw = p.read_text(encoding="utf-8")
    assert "s3cret-pw-123" in raw, "cryptography 缺席时应降级明文直写"
    assert MARK not in raw


def test_legacy_enc1_mark_still_recognized(tmp_path):
    """旧版本写下的 enc1$ 前缀必须仍被识别为**密文**（标记是落盘格式契约）。

    R224 补（R223 变异 E6-8 漏网的补强）：改 MARK 会让旧密文被当成明文
    透传 —— 用户升级后密码「变了」且磁盘上仍是密文，属于静默数据错乱。
    判据用**硬编码字面量**而不是导入的 MARK 常量，否则变异恒绿。
    """
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "web_password": "enc1$" + "Z2FyYmFnZS10b2tlbg==",
        "ai_api_key": "",
        "ui_preset": "classic",
    }), encoding="utf-8")
    config_mod._SECRET_BOX_CACHE.clear()
    cfg = load_config(p)
    assert cfg.web_password == "", (
        "enc1$ 前缀应被识别为密文（解不开→置空），而不是当明文透传")


def test_cross_dir_not_shared(tmp_path):
    """A 目录密文在 B 目录解不开 → 置空不串钥。"""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    cfg = AppConfig()
    cfg.web_password = "s3cret-pw-123"
    save_config(cfg, dir_a / "config.json")
    # 只拷 config.json 到 B（不带 key）
    (dir_b / "config.json").write_text(
        (dir_a / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
    config_mod._SECRET_BOX_CACHE.clear()
    cfg2 = load_config(dir_b / "config.json")
    assert cfg2.web_password == "", "A 目录密文在 B 目录应解不开→置空"
