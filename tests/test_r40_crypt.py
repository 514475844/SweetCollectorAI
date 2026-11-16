"""r40 需求 19：导出加密。

最要紧的一条是 ``test_wrong_passphrase_is_rejected``。实写时踩过一个狠坑：
``_derive()`` 只返回 32 字节，而调用方按 ``keys[:32]=加密钥 / keys[32:]=认证钥``
切分 —— 后者成了**空字节**，MAC 用空密钥签，于是**任何口令都能通过校验**，
「解密成功」但内容全是乱码。密码看着设了、实际形同虚设。

这类 bug 单跑一次看不出来（往返测试用的是同一个密码），必须专门写一条
「错密码必须抛异常」盯着。
"""
from __future__ import annotations

import pytest

from media_resource_manager import crypt


def test_roundtrip():
    data = ("任务=cosplaytele,关键词=丝袜" * 200).encode("utf-8")
    blob = crypt.encrypt_bytes(data, "pw")
    assert crypt.decrypt_bytes(blob, "pw") == data


def test_wrong_passphrase_is_rejected():
    """错密码必须抛 DecryptError，绝不能「解」出乱码当成功。"""
    data = b"top-secret-task-config" * 50
    blob = crypt.encrypt_bytes(data, "right")
    with pytest.raises(crypt.DecryptError):
        crypt.decrypt_bytes(blob, "wrong")


def test_empty_passphrase_cannot_open_others():
    """空密码不能解开用密码加密的文件。"""
    blob = crypt.encrypt_bytes(b"data" * 100, "pw")
    with pytest.raises(crypt.DecryptError):
        crypt.decrypt_bytes(blob, "")


def test_authentication_detects_tampering():
    """改一个字节就必须被发现（否则加密等于没加密）。"""
    blob = bytearray(crypt.encrypt_bytes(b"payload" * 100, "pw"))
    blob[-1] ^= 0x01
    with pytest.raises(crypt.DecryptError):
        crypt.decrypt_bytes(bytes(blob), "pw")


def test_magic_and_version_checked():
    with pytest.raises(crypt.DecryptError):
        crypt.decrypt_bytes(b"XXXX" + b"\0" * 100, "")
    with pytest.raises(crypt.DecryptError):
        crypt.decrypt_bytes(b"too-short", "")
    blob = bytearray(crypt.encrypt_bytes(b"x" * 10, ""))
    blob[4] = 99                       # 版本号改掉
    with pytest.raises(crypt.DecryptError):
        crypt.decrypt_bytes(bytes(blob), "")


def test_derive_returns_two_distinct_keys():
    """KDF 必须给够 64 字节，且两半不能相同（否则等于只有一把钥匙）。"""
    k = crypt._derive("pw", b"0123456789abcdef")
    assert len(k) == 64, f"KDF 只给了 {len(k)} 字节 → 认证钥会变空字节"
    assert k[:32] != k[32:]


def test_salt_and_nonce_are_random_per_file():
    """同内容两次加密必须得到不同密文（否则等于没加密）。"""
    a = crypt.encrypt_bytes(b"same" * 100, "pw")
    b = crypt.encrypt_bytes(b"same" * 100, "pw")
    assert a != b, "两次加密结果相同：salt/nonce 没随机"
    assert crypt.decrypt_bytes(a, "pw") == crypt.decrypt_bytes(b, "pw")


def test_file_level_roundtrip_and_is_encrypted(tmp_path):
    src = tmp_path / "t.json"
    src.write_text('{"task": "x"}', encoding="utf-8")
    enc = crypt.encrypt_file(src, tmp_path / "t.mrmx", "pw")
    assert crypt.is_encrypted(enc)
    assert not crypt.is_encrypted(src)
    out = crypt.decrypt_file(enc, tmp_path / "back.json", "pw")
    assert out.read_text(encoding="utf-8") == '{"task": "x"}'
    with pytest.raises(crypt.DecryptError):
        crypt.decrypt_file(enc, tmp_path / "x.json", "bad")


def test_empty_payload():
    assert crypt.decrypt_bytes(crypt.encrypt_bytes(b"", "pw"), "pw") == b""
