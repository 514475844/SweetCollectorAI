"""HTTPS 自签证书（编号 23）。

用户视角要的只有一句："打开 HTTPS，别让我去搞证书"。所以这里的流程是：
1. 第一次启用 HTTPS → 在本机数据目录生成一对自签证书（10 年有效）；
2. 之后每次启动直接复用（证书在、没过期、SAN 里还有本机地址 → 不动它）；
3. 生成/加载失败 → 返回中文原因，**绝不让服务起在半坏的状态**。

为什么用 `cryptography` 而不是 openssl 命令行：
* 打包版里没有 openssl.exe（依赖系统 PATH 等于没保证）；
* 命令行生成要处理密码交互、编码、临时文件，出错面大得多。
`cryptography` 已经是本项目的既有依赖（DB 加密在用），不新增体积。
"""
from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
from pathlib import Path
from typing import Iterable, Optional

CERT_NAME = "web-selfsigned.crt"
KEY_NAME = "web-selfsigned.key"
#: 有效期（天）。自签证书没人吊销，长一点省得两年后又打不开。
VALID_DAYS = 3650
#: 剩余有效期少于这么多天就重签（避免"用了 9 年 364 天突然过期"）。
RENEW_BEFORE_DAYS = 30


def cert_paths(data_dir: Path) -> tuple[Path, Path]:
    d = Path(data_dir)
    return d / CERT_NAME, d / KEY_NAME


def _san_names(extra: Iterable[str] = ()) -> list[str]:
    """证书里要写进去的地址：localhost + 主机名 + 本机所有 IP + 用户额外填的。"""
    names: list[str] = ["localhost", "127.0.0.1", "::1"]
    try:
        host = socket.gethostname()
        if host and host not in names:
            names.append(host)
            try:
                fq = socket.getfqdn()
                if fq and fq not in names:
                    names.append(fq)
            except Exception:
                pass
    except Exception:
        pass
    try:
        from .nets import list_local_ips
        for ip in list_local_ips():
            if ip and ip not in names:
                names.append(ip)
    except Exception:
        pass
    for n in extra or ():
        s = str(n or "").strip()
        if s and s not in names:
            names.append(s)
    return names


def _cert_still_ok(cert_file: Path, key_file: Path) -> bool:
    """证书与私钥都在、还没到期、且不是快到期 → 复用。"""
    try:
        if not (cert_file.is_file() and key_file.is_file()):
            return False
        from cryptography import x509
        with cert_file.open("rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        # not_valid_after 在新版是 UTC aware，老版是 naive —— 两种都兼容
        try:
            exp = cert.not_valid_after_utc
            now = datetime.datetime.now(datetime.timezone.utc)
        except Exception:
            exp = cert.not_valid_after
            now = datetime.datetime.utcnow()
        return (exp - now) > datetime.timedelta(days=RENEW_BEFORE_DAYS)
    except Exception:
        return False


def ensure_self_signed(data_dir: Path,
                       extra_hosts: Iterable[str] = (),
                       force: bool = False) -> tuple[bool, str]:
    """确保自签证书存在且可用。返回 (成功?, 中文说明)。

    ⚠ 说明里**不能**出现"请确认 OpenSSL 已安装"这类话 —— 我们不用它。
    """
    cert_file, key_file = cert_paths(Path(data_dir))
    try:
        cert_file.parent.mkdir(parents=True, exist_ok=True)
    except Exception as ex:                             # noqa: BLE001
        return False, f"证书目录建不出来：{ex}"
    if not force and _cert_still_ok(cert_file, key_file):
        return True, f"沿用已有证书：{cert_file}"

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except Exception as ex:                             # noqa: BLE001
        return False, (f"缺少证书组件 cryptography（{type(ex).__name__}）："
                       "打包版请确认已带上该模块；源码运行请 pip install cryptography")

    try:
        key = ec.generate_private_key(ec.SECP256R1())
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "Sweet采集 AI版 本机"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Sweet Collector (self-signed)"),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        sans: list = [x509.DNSName("localhost")]
        for n in _san_names(extra_hosts):
            try:
                sans.append(x509.IPAddress(ipaddress.ip_address(n)))
            except ValueError:
                if n and n != "localhost":
                    try:
                        sans.append(x509.DNSName(n))
                    except Exception:
                        pass
        cert = (x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=5))
                .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
                .add_extension(x509.SubjectAlternativeName(sans), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                               critical=True)
                .sign(key, hashes.SHA256()))
        key_file.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()))
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        try:                                            # 尽量收紧权限（Windows 上是尽力而为）
            import os
            os.chmod(key_file, 0o600)
        except Exception:
            pass
        return True, f"已生成本机自签证书（{VALID_DAYS} 天有效）：{cert_file}"
    except Exception as ex:                             # noqa: BLE001
        return False, f"生成自签证书失败：{type(ex).__name__}: {ex}"


def make_ssl_context(data_dir: Path, extra_hosts: Iterable[str] = (),
                     force_new: bool = False) -> tuple[Optional[ssl.SSLContext], str]:
    """建 SSLContext（服务端）。失败返回 (None, 中文原因)。"""
    ok, msg = ensure_self_signed(data_dir, extra_hosts, force=force_new)
    if not ok:
        return None, msg
    cert_file, key_file = cert_paths(Path(data_dir))
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
        try:                                            # 会话复用：手机端重连更快
            ctx.options |= ssl.OP_NO_COMPRESSION
        except Exception:
            pass
        return ctx, msg
    except Exception as ex:                             # noqa: BLE001
        return None, f"加载证书失败：{type(ex).__name__}: {ex}"


def cert_info(data_dir: Path) -> dict:
    """给界面看的证书信息（有=能显示到期时间，没有=提示还没生成）。"""
    cert_file, key_file = cert_paths(Path(data_dir))
    out = {"exists": False, "path": str(cert_file),
           "not_after": "", "days_left": 0, "text": "尚未生成（首次启用 HTTPS 时自动生成）"}
    try:
        if not (cert_file.is_file() and key_file.is_file()):
            return out
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
        try:
            exp = cert.not_valid_after_utc
            now = datetime.datetime.now(datetime.timezone.utc)
        except Exception:
            exp = cert.not_valid_after
            now = datetime.datetime.utcnow()
        left = max(0, (exp - now).days)
        out.update({
            "exists": True,
            "not_after": exp.strftime("%Y-%m-%d"),
            "days_left": left,
            "text": f"本机自签证书，{out_ts(exp)} 到期（还剩 {left} 天）",
        })
    except Exception as ex:                             # noqa: BLE001
        out["text"] = f"证书读不出来：{type(ex).__name__}"
    return out


def out_ts(dt) -> str:
    try:
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return str(dt)
