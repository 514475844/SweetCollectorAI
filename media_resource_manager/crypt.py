"""导出加密（需求 19）。

用户要求：「所有导出的任务和所有导出的信息必须加密，只允许我的软件使用，
图片和图片信息除外」。

范围（用户逐条点过的）：
  任务导出 / 偏好(学习模型) / 备份 / 诊断包 / **图库索引（图片→标签映射）**
不加密：
  图片文件本身、EXIF 等图片元信息。

方案：ChaCha20-Poly1305 风格的「流加密 + HMAC 认证」，全部用标准库实现
（hashlib 的 sha256 做 KDF，hmac 做认证，hashlib.blake2b 做流加密），
**不引入 cryptography 依赖** —— 这个软件要在别人机器上跑，依赖越少越好。

密钥来源：**内置 salt + 机器/用户指纹**。用户说过「密码来源未定，倾向内置
salt」，这里就按这个来：
  - 内置一段编译期常量 SALT_B64（源码里，不算秘密但能挡住「直接解压看」）；
  - 再混入本机指纹（机器名 + 用户名 + 主目录路径）做 KDF 输入，
    让导出的包**在别的机器上解不开** —— 这正是「只允许我的软件使用」的本意。

文件格式（.mrmx）：
    magic  "MRMX"            4 字节
    ver    1                 1 字节
    salt   16 字节（每份文件随机，便于同密码多次加密）
    nonce  16 字节
    mac    32 字节（HMAC-SHA256，覆盖头部+密文）
    body   密文
自检：magic 不对就直接拒绝，不给「可能是别的格式」的任何猜测。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
from typing import Union

MAGIC = b"MRMX"
VERSION = 1
SALT_LEN = 16
NONCE_LEN = 16
MAC_LEN = 32
CHUNK = 65536

# 编译期常量：源码在就不算真秘密，但足以让「拿 zip 当普通文件解开看」失效。
# 真正的安全来自它和本机指纹的组合 —— 换台机器解不开。
SALT_B64 = "bUdyTUNvbi0xNXQtZnJlc2gta2V5LTAx"


class DecryptError(Exception):
    """解密失败（不是本软件导出的 / 被改过 / 换机器了）。"""


# ---------------------------------------------------------------- KDF ----
def _fingerprint() -> bytes:
    """本机指纹：机器名 + 用户名 + 主目录。跨机器就不同 → 跨机解不开。

    R152-E8：Linux 下 COMPUTERNAME/USERNAME/USERPROFILE 三个环境变量**皆空**
    （它们是 Windows 专有），原来指纹恒为空串 → 所有 Linux 机器指纹相同 →
    导出包在任意 Linux 上都能解开（「只允许我的软件使用」形同虚设）。
    Linux 改取 /etc/machine-id（systemd）或 /var/lib/dbus/machine-id（旧）
    + hostname + 用户名 + 主目录。Windows 路径不变（存量导出仍可解）。
    """
    import socket
    parts = []
    if os.name == "nt":
        getters = (lambda: os.environ.get("COMPUTERNAME", ""),
                   lambda: os.environ.get("USERNAME", ""),
                   lambda: os.environ.get("USERPROFILE", ""))
    else:
        mid = ""
        for mid_path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(mid_path, "r", encoding="utf-8") as fh:
                    mid = fh.read().strip()
                if mid:
                    break
            except OSError:
                pass
        try:
            import pwd
            user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            user = os.environ.get("USER", "")
        try:
            home = os.path.expanduser("~")
        except Exception:
            home = ""
        getters = (lambda: mid,
                   lambda: socket.gethostname(),
                   lambda: user,
                   lambda: home)
    for getter in getters:
        try:
            parts.append(str(getter() or ""))
        except Exception:
            parts.append("")
    return "|".join(parts).encode("utf-8", "replace")


def _derive(passphrase: str, salt: bytes) -> bytes:
    """口令+盐 → 64 字节（32 加密 + 32 认证）。

    用 6 万轮 sha256 迭代，纯粹为了拖慢暴力破解；这是在纯标准库前提下的
    合理折中，真要抗 GPU 攻击得上 Argon2/scrypt（那就得引依赖了）。
    """
    base = hashlib.sha256(
        base64.b64decode(SALT_B64) + _fingerprint()
        + str(passphrase or "").encode("utf-8", "replace") + salt
    ).digest()
    cur = base
    for _ in range(60000):
        cur = hashlib.sha256(cur + base).digest()
    # 必须**再迭代一轮**拿到第二块 32 字节：调用方按 keys[:32]=加密钥、
    # keys[32:]=认证钥 切分。直接返回 cur 的话 keys[32:] 是空字节，
    # MAC 就用空密钥签 —— 任何口令都能通过校验，密码形同虚设
    # （这个 bug 实测过：错密码能"解"出乱码而不报错）。
    cur2 = hashlib.sha256(cur + b"\x01" + base).digest()
    return cur + cur2


# ------------------------------------------------------------ 加密 ----
def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """blake2b 计数器模式生成密钥流（标准库、够快、跨平台一致）。"""
    out = bytearray()
    ctr = 0
    while len(out) < length:
        out += hashlib.blake2b(nonce + ctr.to_bytes(8, "little"),
                               key=key, digest_size=64).digest()
        ctr += 1
    return bytes(out[:length])


def encrypt_bytes(data: bytes, passphrase: str = "") -> bytes:
    """加密任意字节，返回完整 .mrmx 容器。"""
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    keys = _derive(passphrase, salt)
    enc_key, mac_key = keys[:32], keys[32:]
    ks = _keystream(enc_key, nonce, len(data))
    body = bytes(a ^ b for a, b in zip(data, ks))
    header = MAGIC + bytes([VERSION]) + salt + nonce
    mac = hmac.new(mac_key, header + body, hashlib.sha256).digest()
    return header + mac + body


def decrypt_bytes(blob: bytes, passphrase: str = "") -> bytes:
    """解密 .mrmx 容器。格式不对/被改过/换机器 → DecryptError。"""
    head = len(MAGIC) + 1 + SALT_LEN + NONCE_LEN
    if len(blob) < head + MAC_LEN:
        raise DecryptError("文件太短，不是本软件的加密导出")
    if blob[:4] != MAGIC:
        raise DecryptError("不是本软件导出的加密文件（magic 不匹配）")
    ver = blob[4]
    if ver != VERSION:
        raise DecryptError(f"不支持的加密版本：{ver}")
    off = 5
    salt = blob[off:off + SALT_LEN]; off += SALT_LEN
    nonce = blob[off:off + NONCE_LEN]; off += NONCE_LEN
    mac = blob[off:off + MAC_LEN]; off += MAC_LEN
    body = blob[off:]
    keys = _derive(passphrase, salt)
    enc_key, mac_key = keys[:32], keys[32:]
    header = MAGIC + bytes([VERSION]) + salt + nonce
    want = hmac.new(mac_key, header + body, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, want):
        # 分不清是「密码错」还是「文件被改」，统一报「校验失败」
        raise DecryptError("校验失败：口令不对，或文件在传输中损坏")
    ks = _keystream(enc_key, nonce, len(body))
    return bytes(a ^ b for a, b in zip(body, ks))


def is_encrypted(path: Union[str, Path]) -> bool:
    """看文件头判断是不是本软件的加密包（不尝试解密）。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == MAGIC
    except OSError:
        return False


# ------------------------------------------------- 文件级便捷封装 ----
def encrypt_file(src: Union[str, Path], dst: Union[str, Path],
                 passphrase: str = "") -> Path:
    """把一个文件加密成 .mrmx。

    直接复用字节级实现：一次读进内存。诊断包/任务导出/图库索引这类文件
    实测都在几十 MB 以内，比手写分块 + 回填 MAC 那一套可靠得多
    （那套在实写时把 MAC 算错了，文件级往返解出来是乱码）。
    以后真要支持 GB 级导出，再换流式实现，并且必须有对应的往返测试兜底。
    """
    src, dst = Path(src), Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    blob = encrypt_bytes(src.read_bytes(), passphrase)
    tmp = dst.with_suffix(dst.suffix + ".part")
    tmp.write_bytes(blob)
    tmp.replace(dst)
    return dst


def decrypt_file(src: Union[str, Path], dst: Union[str, Path],
                 passphrase: str = "") -> Path:
    """解密 .mrmx 回普通文件。"""
    blob = Path(src).read_bytes()
    data = decrypt_bytes(blob, passphrase)
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    return dst
