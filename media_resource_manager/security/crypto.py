"""Optional local encryption (master password -> Fernet).

Used for DB and library-at-rest encryption. Fully local; no network. If the
`cryptography` package is unavailable, encryption is a no-op and callers must
disable the feature (the GUI warns the user). Destructive ops remain undoable.

R152-E7（密钥派生升级）
----------------------
旧实现是 ``urlsafe_b64(sha256(password))``：**单次哈希、无盐、无迭代**，
主密码一旦进了彩虹表/被撞库就等同明文，且换机器也解不开的差异性全无。
现在改为 PBKDF2-HMAC-SHA256 + 盐 + 高迭代（stdlib ``hashlib``，零依赖）：

* :func:`derive_key` —— 新方案（PBKDF2），**写与读都走它**；
* :func:`legacy_key` —— 旧方案，**只用于读**：老版本加密过的数据仍要能解
  （:meth:`Vault.decrypt_file` 先试新密钥，失败再退旧密钥），否则一升级
  就把历史库变成永久打不开的乱码；
* 盐：调用方若能持久化「每库随机盐」就显式传 ``salt=``；传不了的场景退化为
  应用级常量盐（比无盐强，但别当真秘密）；
* 轮数 :data:`KDF_ROUNDS` 是**下限护栏**，不小于 60000（订单要求）。
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Optional

#: KDF 名称（落盘/诊断用，便于以后换算法时识别）
KDF_NAME = "pbkdf2_sha256"
#: PBKDF2 迭代轮数。订单下限 60000；这里取 200000（OWASP 对
#: PBKDF2-HMAC-SHA256 的建议量级），只在 Vault 构造/解密失败回退时各跑一次，
#: 不在热路径上。
KDF_ROUNDS = 200_000
#: 显式盐的长度（:func:`new_salt` 生成）
SALT_LEN = 16
#: 调用方无法持久化盐时的兜底盐 —— 仍然比「无盐」强，但别当秘密。
DEFAULT_SALT = b"mrm-vault-v1-salt"

__all__ = ["Vault", "derive_key", "legacy_key", "new_salt",
           "KDF_NAME", "KDF_ROUNDS", "SALT_LEN", "DEFAULT_SALT"]


def new_salt() -> bytes:
    """生成一份新的随机盐（调用方能持久化就持久化，持久化不了就别传）。"""
    return os.urandom(SALT_LEN)


def derive_key(password: str, salt: bytes, rounds: int = KDF_ROUNDS) -> bytes:
    """PBKDF2-HMAC-SHA256(password, salt, rounds) → Fernet 用的 urlsafe key。

    纯 stdlib、纯函数：同输入必同输出，任一输入变了输出必变（护栏断言的关系）。
    """
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        str(password).encode("utf-8"),
        bytes(salt),
        int(rounds),
        dklen=32,
    )
    return base64.urlsafe_b64encode(dk)


def legacy_key(password: str) -> bytes:
    """E7 之前的旧方案（单次 sha256）。**只用于读老数据**，绝不用于加密。"""
    return base64.urlsafe_b64encode(
        hashlib.sha256(str(password).encode("utf-8")).digest())


class Vault:
    #: 类级镜像，便于不实例化也能查到护栏下限（测试/诊断用）
    KDF_ROUNDS = KDF_ROUNDS
    KDF_NAME = KDF_NAME

    def __init__(self, password: str, available: bool = True,
                 salt: Optional[bytes] = None, rounds: Optional[int] = None):
        self.available = available
        self.salt = bytes(salt) if salt is not None else DEFAULT_SALT
        self.rounds = int(rounds) if rounds else KDF_ROUNDS
        self.kdf = KDF_NAME
        self._fernet = None      # 新密钥（写+读）
        self._legacy = None      # 旧密钥（只读回退）
        if not available:
            return
        try:
            from cryptography.fernet import Fernet  # type: ignore
            from cryptography.fernet import InvalidToken  # type: ignore
        except Exception:
            self.available = False
            return
        self._InvalidToken = InvalidToken
        self._key = derive_key(password, self.salt, self.rounds)
        self._fernet = Fernet(self._key)
        # 老数据兼容：旧版本用 legacy_key 加密过的内容仍要读得出来
        self._legacy = Fernet(legacy_key(password))

    @classmethod
    def is_supported(cls) -> bool:
        try:
            import cryptography  # type: ignore
            return True
        except Exception:
            return False

    def encrypt_file(self, src: Path, dest: Path) -> Path:
        """加密：恒用新密钥（不再产出旧格式的密文）。"""
        if not self.available:
            dest.write_bytes(src.read_bytes())
            return dest
        data = self._fernet.encrypt(src.read_bytes())
        dest.write_bytes(data)
        return dest

    def decrypt_file(self, src: Path, dest: Path) -> Path:
        """解密：先新密钥，失败再退旧密钥（E7 升级后老库仍可读）。

        两个密钥都解不开才抛；**绝不静默返回错误内容** —— 旧实现只有一把
        密钥，解不开就抛，语义一致。
        """
        if not self.available:
            dest.write_bytes(src.read_bytes())
            return dest
        raw = src.read_bytes()
        try:
            data = self._fernet.decrypt(raw)
        except Exception:
            if self._legacy is None:
                raise
            data = self._legacy.decrypt(raw)   # 老格式：允许，读出来即可
        dest.write_bytes(data)
        return dest
