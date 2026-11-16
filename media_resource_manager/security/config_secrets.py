"""R152-E6：config.json 敏感字段（web_password / ai_api_key）落盘加密。

背景（订单 E6）：活配置里 ``web_password`` / ``ai_api_key`` **明文落盘** ——
整个数据目录（或其中一份 config.json）被拷走/外发时，Web 密码与 AI 服务的
API key 等于裸奔。

做法：
- 密钥文件 ``config.key``（随机 32 字节 + 随机盐，JSON 落盘）与 config.json
  **同目录**。文件夹整体拷贝（软件的便携模式）时两文件一起走，换机可用；
  只拷走 config.json 时解不开 —— 这正是要的效果。
- 加密走 :func:`media_resource_manager.security.crypto.derive_key`
  （E7 升级后的 PBKDF2-HMAC-SHA256，20 万轮）派生 Fernet 密钥；
  ``cryptography`` 缺席时退化为**明文直写**（与 E7 的降级口径一致），
  绝不假装加密了。
- 密文带标记前缀 ``enc1$``：读旧明文**自动透传**（老用户升级不受影响）；
  有标记却解不开（密钥文件缺失/损坏/被换）→ 抛 :class:`ConfigSecretError`，
  由调用方（load_config）置空并记日志 —— 不静默返回乱码、不拖垮整个配置。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from .crypto import DEFAULT_SALT, derive_key, new_salt

#: 密文标记前缀。读旧明文（无标记）自动透传；换算法时改标记即隔离新旧格式。
MARK = "enc1$"

#: 密钥文件名（与 config.json 同目录）
KEY_FILE_NAME = "config.key"

__all__ = ["ConfigSecrets", "ConfigSecretError", "MARK", "KEY_FILE_NAME",
           "restrict_permissions"]


class ConfigSecretError(Exception):
    """密文带标记但解不开（密钥缺失/损坏/不匹配）。"""


def restrict_permissions(path: Path) -> None:
    """尽力把权限收到 0600（订单 E6 的「尽力」口径）。

    Windows 上 chmod 只影响只读位，属尽力而为；POSIX 上是真收紧。
    任何失败都不抛 —— 权限收紧不该让保存配置失败。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class ConfigSecrets:
    """一个数据目录一份实例：惰性构造 Fernet。

    ⚠ 密钥状态挂在**实例**上（调用方按目录缓存实例），不挂类属性 ——
    同进程多目录/多测试各自独立，互不串钥（R218 教训：状态挂类属性会让
    多用例互相锁死/串钥）。
    """

    def __init__(self, key_path: Path):
        self.key_path = Path(key_path)
        self._fernet = None
        self._built = False

    # -- 惰性构造 ---------------------------------------------------------
    def _build(self) -> None:
        """构造 Fernet：读/建密钥文件。cryptography 缺席 → 保持 None（降级）。"""
        if self._built:
            return
        self._built = True
        try:
            from cryptography.fernet import Fernet  # type: ignore
        except Exception:
            return                      # 明文直写降级（与 E7 口径一致）
        try:
            data = None
            if self.key_path.exists():
                data = json.loads(self.key_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("secret"):
                secret = os.urandom(32)
                salt = new_salt()
                data = {
                    "secret": base64.b64encode(secret).decode("ascii"),
                    "salt": base64.b64encode(salt).decode("ascii"),
                }
                self.key_path.write_text(
                    json.dumps(data, ensure_ascii=False), encoding="utf-8")
                restrict_permissions(self.key_path)
            secret = base64.b64decode(data["secret"])
            salt = base64.b64decode(data.get("salt") or DEFAULT_SALT)
            self._fernet = Fernet(derive_key(secret.hex(), salt))
        except Exception:
            self._fernet = None

    @property
    def available(self) -> bool:
        self._build()
        return self._fernet is not None

    # -- 加解密 -----------------------------------------------------------
    def encrypt(self, value):
        """加密落盘值。空值/非字符串原样返回（空密码就该存空串）。"""
        self._build()
        if not value or not isinstance(value, str) or self._fernet is None:
            return value
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        return MARK + token

    def decrypt(self, value):
        """解密读入值。

        - 非字符串 / 空串 / 无标记（旧明文）→ 原样返回（升级兼容）；
        - 有标记但解不开 → 抛 :class:`ConfigSecretError`
          （绝不静默变乱码，由调用方决定置空 + 告警）。
        """
        self._build()
        if not isinstance(value, str) or not value:
            return value
        if not value.startswith(MARK):
            return value
        if self._fernet is None:
            raise ConfigSecretError(
                "密文带标记但本机无法解密（密钥文件缺失或 cryptography 未装）")
        try:
            return self._fernet.decrypt(
                value[len(MARK):].encode("ascii")).decode("utf-8")
        except Exception as e:
            raise ConfigSecretError(
                f"config 敏感字段解密失败：{type(e).__name__}") from e
