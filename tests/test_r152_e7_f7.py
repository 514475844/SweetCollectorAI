# -*- coding: utf-8 -*-
"""R152-E7（主密码 KDF 升级）与 R152-F7（i18n 死参数接线 / 死模块清理）护栏。

判据纪律（R73/R203b 教训：判据必须变异过、且判的是**被护对象本身**）：
* E7 的 KDF 判据全部走**行为**：用 hashlib 独立算一遍 PBKDF2 与被测
  ``derive_key`` 对拍；用注入的假 ``cryptography.fernet`` 抓 ``Vault`` 真正
  交给 Fernet 的 key —— 不看源码字面量、不被注释/docstring 骗。
* 本机 venv **没装 cryptography**，所以绝不用 ``importorskip``（skip 的判据
  永远绿 = 假闭环）；用 stub 注入把依赖替换掉，判据照旧能红。
* F7 的"死模块已删"判的是模块解析结果（find_spec / import 抛错），不是注释。
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
import types

import pytest

from media_resource_manager import i18n
from media_resource_manager.security import crypto as sec


# ------------------------------------------------------------------ 夹具 --
@pytest.fixture(autouse=True)
def _clean_i18n_state(monkeypatch):
    """每个用例前后都把语言包缓存与 MRM_LANG 复位，避免互相污染。"""
    monkeypatch.delenv("MRM_LANG", raising=False)
    i18n.clear_locale_cache()
    yield
    i18n.clear_locale_cache()


def _install_stub_fernet(monkeypatch):
    """注入假的 cryptography.fernet，记录 Vault 实际使用的 key。

    密文格式：``CT|<key>|<payload>`` —— 只有持有同一把 key 的 Fernet 才解得开，
    这样"新密钥 vs 旧密钥"的语义差异能被行为断言出来。
    """
    seen = {"keys": []}

    class InvalidToken(Exception):
        pass

    class Fernet:
        def __init__(self, key):
            self.key = bytes(key)
            seen["keys"].append(self.key)

        def encrypt(self, data: bytes) -> bytes:
            return b"CT|" + self.key + b"|" + bytes(data)

        def decrypt(self, token: bytes) -> bytes:
            if not token.startswith(b"CT|"):
                raise InvalidToken("bad magic")
            _, key, payload = token.split(b"|", 2)
            if key != self.key:
                raise InvalidToken("key mismatch")
            return payload

    fernet_mod = types.ModuleType("cryptography.fernet")
    fernet_mod.Fernet = Fernet
    fernet_mod.InvalidToken = InvalidToken
    crypto_mod = types.ModuleType("cryptography")
    crypto_mod.fernet = fernet_mod
    monkeypatch.setitem(sys.modules, "cryptography", crypto_mod)
    monkeypatch.setitem(sys.modules, "cryptography.fernet", fernet_mod)
    return seen, Fernet, InvalidToken


# ------------------------------------------------------------- E7: KDF ---
def test_e7_rounds_floor():
    """订单硬要求：PBKDF2 轮数不低于 60000（单次无盐哈希=1 轮，必须被挡住）。"""
    assert sec.KDF_ROUNDS >= 60000
    assert sec.Vault.KDF_ROUNDS >= 60000
    assert sec.KDF_NAME.startswith("pbkdf2")


def test_e7_derive_key_is_pbkdf2_not_sha256():
    """行为对拍：derive_key 的输出 == 独立计算的 PBKDF2，且 != 老方案 sha256。"""
    pw, salt, rounds = "pw-1", b"0123456789abcdef", 1000
    got = sec.derive_key(pw, salt, rounds)
    expect_pbkdf2 = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, rounds,
                                        dklen=32)
    assert got == _b64(expect_pbkdf2)
    assert got != sec.legacy_key(pw)                      # 不是老方案
    assert got != _b64(hashlib.sha256(pw.encode()).digest())


def _b64(raw: bytes) -> bytes:
    import base64
    return base64.urlsafe_b64encode(raw)


def test_e7_derive_key_follows_inputs():
    """同输入必同输出；换密码/换盐/换轮数必换输出（KDF 的三条基本关系）。"""
    base = sec.derive_key("pw", b"salt-salt-salt", 500)
    assert base == sec.derive_key("pw", b"salt-salt-salt", 500)
    assert base != sec.derive_key("pw2", b"salt-salt-salt", 500)
    assert base != sec.derive_key("pw", b"salt-salt-salT", 500)
    assert base != sec.derive_key("pw", b"salt-salt-salt", 501)


def test_e7_vault_hands_derived_key_to_fernet(monkeypatch):
    """Vault 交给 Fernet 的**第一把** key 必须是新 KDF 派生的，不是 sha256。"""
    seen, _F, _ = _install_stub_fernet(monkeypatch)
    v = sec.Vault("pw-x", salt=b"salt-salt-salt", rounds=800)
    assert v.available is True
    assert seen["keys"][0] == sec.derive_key("pw-x", b"salt-salt-salt", 800)
    assert seen["keys"][0] != sec.legacy_key("pw-x")


def test_e7_encrypt_no_longer_writes_legacy_cipher(monkeypatch, tmp_path):
    """升级后**写**出来的密文不再用老密钥 —— 老 Fernet 必须解不开。"""
    _seen, Fernet, InvalidToken = _install_stub_fernet(monkeypatch)
    src = tmp_path / "a.bin"
    src.write_bytes(b"payload-1")
    v = sec.Vault("pw", salt=b"s" * 16, rounds=500)
    enc = v.encrypt_file(src, tmp_path / "a.enc")
    blob = enc.read_bytes()
    with pytest.raises(InvalidToken):
        Fernet(sec.legacy_key("pw")).decrypt(blob)


def test_e7_legacy_ciphertext_still_readable(monkeypatch, tmp_path):
    """升级前用老密钥加密的数据仍要读得出来（不许把老库变乱码）。"""
    _seen, Fernet, _ = _install_stub_fernet(monkeypatch)
    legacy = Fernet(sec.legacy_key("old-pw"))
    enc = tmp_path / "legacy.enc"
    enc.write_bytes(legacy.encrypt(b"old-bytes"))
    v = sec.Vault("old-pw", salt=b"s" * 16, rounds=500)
    out = v.decrypt_file(enc, tmp_path / "out.bin")
    assert out.read_bytes() == b"old-bytes"


def test_e7_wrong_password_raises_not_silent(monkeypatch, tmp_path):
    """密码错必须抛，**绝不**静默返回一串看起来正常的乱码。"""
    _seen, Fernet, InvalidToken = _install_stub_fernet(monkeypatch)
    enc = tmp_path / "x.enc"
    enc.write_bytes(Fernet(sec.derive_key("right", b"s" * 16, 500)).encrypt(b"d"))
    v = sec.Vault("wrong", salt=b"s" * 16, rounds=500)
    with pytest.raises(InvalidToken):
        v.decrypt_file(enc, tmp_path / "out.bin")


def test_e7_unavailable_vault_is_passthrough(tmp_path):
    """available=False 时加密/解密都是原样拷贝（不吞数据、不抛）。"""
    src = tmp_path / "p.bin"
    src.write_bytes(b"plain")
    v = sec.Vault("pw", available=False)
    assert v.available is False
    assert v.encrypt_file(src, tmp_path / "e.bin").read_bytes() == b"plain"
    assert v.decrypt_file(src, tmp_path / "d.bin").read_bytes() == b"plain"


def test_e7_availability_matches_support():
    """Vault.available 与 is_supported() 必须一致（依赖缺失不能假装能用）。"""
    v = sec.Vault("pw")
    assert v.available == sec.Vault.is_supported()


# ------------------------------------------------------------ F7: i18n ---
def test_f7_dead_logging_module_is_gone():
    """utils/logging.py 已删：解析不到、import 必抛（判模块解析，不判注释）。"""
    importlib.invalidate_caches()
    spec = importlib.util.find_spec("media_resource_manager.utils.logging")
    assert spec is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("media_resource_manager.utils.logging")


def test_f7_label_text_honors_lang():
    """lang 不再是被忽略的死参数：英文界面回原文，中文界面走映射。"""
    assert i18n.label_text("stockings", "en_US") == "stockings"
    assert i18n.label_text("stockings", "zh_CN") == "丝袜"
    assert i18n.label_text("cute") == "可爱"          # 默认仍是中文


def test_f7_lang_comes_from_env(monkeypatch):
    """MRM_LANG 真能切换界面语言（current_lang 已接进 label_text）。"""
    monkeypatch.setenv("MRM_LANG", "en_US")
    i18n.clear_locale_cache()
    assert i18n.current_lang() == "en_US"
    assert i18n.label_text("cute") == "cute"
    assert i18n.label_text("pantyhose") == "pantyhose"


def test_f7_locale_pack_is_used(monkeypatch, tmp_path):
    """语言包最高优先：命中包就用包里的词（load_locale 不再是没人调的死函数）。"""
    base = tmp_path / "locale"
    for lang, data in (("zh_CN", {"stockings": "长筒袜"}),
                       ("en_US", {"cute": "萌"})):
        d = base / lang / "LC_MESSAGES"
        d.mkdir(parents=True)
        (d / "tags.json").write_text(json.dumps(data, ensure_ascii=False),
                                     encoding="utf-8")
    monkeypatch.setattr(i18n, "locale_dir", lambda: base)
    i18n.clear_locale_cache()
    assert i18n.label_text("stockings") == "长筒袜"           # 包覆盖内置表
    assert i18n.label_text("cute", "en_US") == "萌"          # 英文界面也有词
    assert i18n.label_text("nsfw", "en_US") == "nsfw"        # 包里没有→原文


def test_f7_locale_cache_and_invalidation(monkeypatch, tmp_path):
    """热路径缓存：同一语言返回同一对象；clear 后重新读盘。"""
    base = tmp_path / "locale"
    i18n.clear_locale_cache()
    a = i18n.load_locale("zh_CN")
    assert i18n.load_locale("zh_CN") is a              # 缓存命中
    i18n.clear_locale_cache("zh_CN")
    assert i18n.load_locale("zh_CN") is not a          # 失效后是新对象
    i18n.clear_locale_cache()
    assert i18n.load_locale("zh_CN") == {}


def test_f7_translate_tags_passes_lang():
    """translate_tags 的 lang 同样不再是摆设。"""
    assert i18n.translate_tags(["cute", "nsfw"], "en_US") == ["cute", "nsfw"]
    assert i18n.translate_tags(["cute", "nsfw"], "zh_CN") == ["可爱", "不适宜"]


def test_f7_default_zh_mapping_not_regressed():
    """改 lang 接线不能把原有中文映射/复合词搞坏（回归面）。"""
    assert i18n.label_text("pantyhose") == "连裤袜"
    assert i18n.label_text("white stockings") == "白丝"
    assert i18n.label_text("不存在的标签xyz") == "不存在的标签xyz"
    assert i18n.tag_key("White  Stockings") == "white stockings"  # 存库 key 不变


def test_f7_logutil_is_the_live_logger():
    """活日志层仍是 logutil（F7 删的是 utils/logging.py 这个死模块，不是日志）。"""
    from media_resource_manager import logutil
    masked = logutil.sanitize_text("C:/Users/someone/lib/a.jpg")
    assert "someone" not in masked and "lib" not in masked
    assert masked.endswith("a.jpg")
    assert hasattr(logutil, "SanitizeFilter")
