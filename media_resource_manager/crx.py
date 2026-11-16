"""把浏览器插件打包成 CRX —— **包内模块**（R249 从 tools/ 搬进来）。

为什么搬：``tools/`` 不在打包清单里（``build/pyinstaller.spec`` 的 datas
只有 icon / extension / web），而桌面端「打包 CRX」菜单直接
``from tools.build_crx import build_crx`` —— 安装版点它必崩。
同一类问题本轮已修了三处（修复图库 / 清理 0kb / 任务后自动清理）。

``tools/build_crx.py`` 保留为薄壳转发，命令行用法不变。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

CRX_MAGIC = b"Cr24"
CRX_VERSION = 3


# ------------------------------------------------------------ protobuf --
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _bytes_field(field: int, data: bytes) -> bytes:
    """proto3 length-delimited 字段。"""
    return _tag(field, 2) + _varint(len(data)) + data


def build_crx_id(public_key_der: bytes) -> bytes:
    """crx_id = 公钥 DER 的 SHA-256 前 20 字节。"""
    return hashlib.sha256(public_key_der).digest()[:20]


def build_signed_header(crx_id: bytes) -> bytes:
    """SerializedSignedData { bytes crx_id = 1; }"""
    return _bytes_field(1, crx_id)


def build_header(proof: dict | None, signed_header_data: bytes) -> bytes:
    """CrxFileHeader。proof=None 表示自签（不放证明字段）。"""
    out = bytearray()
    if proof:
        pk = _bytes_field(1, proof["public_key"])
        sig = _bytes_field(2, proof["signature"])
        out += _bytes_field(2, pk + sig)     # sha256_with_rsa
    out += _bytes_field(10000, signed_header_data)
    return bytes(out)


# ------------------------------------------------------------ 签名 ----
def ensure_key(key_path: Path | None) -> tuple[bytes, bytes | None]:
    """确保有一把 RSA 私钥。返回 (私钥 PEM, openssl 路径或 None)。

    优先用 cryptography（如果装了）；否则退到 openssl 命令行。
    两条路都拿不到就明确报错 —— 宁可告诉用户装 openssl，也别悄悄出一个
    永远装不上的包。
    """
    if key_path and key_path.exists():
        pem = key_path.read_bytes()
    else:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            key = rsa.generate_private_key(public_exponent=65537,
                                          key_size=2048)
            pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption())
            if key_path:
                key_path.write_bytes(pem)
        except Exception:
            exe = _find_openssl()
            if not exe:
                raise RuntimeError(
                    "要打 CRX 需要 RSA 私钥，但既没装 cryptography 也找不到 "
                    "openssl 命令。\n装其一即可：\n"
                    "  pip install cryptography\n"
                    "  或装 OpenSSL 并把它加进 PATH")
            if key_path:
                key_path.parent.mkdir(parents=True, exist_ok=True)
                if not key_path.exists():
                    subprocess.run([exe, "genrsa", "-out", str(key_path),
                                    "2048"], check=True,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            pem = key_path.read_bytes() if key_path else b""
    # 私钥有了，还要能取到公钥 DER
    return pem


def _find_openssl() -> str | None:
    for exe in ("openssl", "openssl.exe"):
        try:
            r = subprocess.run([exe, "version"], capture_output=True,
                               timeout=5)
            if r.returncode == 0:
                return exe
        except Exception:
            continue
    return None


def public_key_der(private_pem: bytes, openssl: str | None) -> bytes:
    """从私钥取出公钥的 SubjectPublicKeyInfo DER。"""
    try:
        from cryptography.hazmat.primitives import serialization
        key = serialization.load_pem_private_key(private_pem, password=None)
        return key.public_key().public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo)
    except Exception:
        pass
    if openssl:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as tf:
            tf.write(private_pem)
            tmp = tf.name
        try:
            r = subprocess.run([openssl, "rsa", "-in", tmp, "-pubout",
                                "-outform", "DER"],
                               capture_output=True, timeout=20)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    raise RuntimeError("取不到公钥：openssl 与 cryptography 都不可用")


def sign(data: bytes, private_pem: bytes, openssl: str | None) -> bytes:
    """RSA-SHA256 签名，返回 PKCS#1 v1.5 签名块。"""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(private_pem, password=None)
        return key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        pass
    if openssl:
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tf.write(data)
            f_in = tf.name
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            f_sig = tf.name
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as tf:
            tf.write(private_pem)
            f_key = tf.name
        try:
            subprocess.run([openssl, "dgst", "-sha256", "-sign", f_key,
                            "-out", f_sig, f_in], check=True,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
            return Path(f_sig).read_bytes()
        finally:
            for f in (f_in, f_sig, f_key):
                try:
                    os.unlink(f)
                except OSError:
                    pass
    raise RuntimeError("签名失败：openssl 与 cryptography 都不可用")


# ------------------------------------------------------------ 打包 ----
def make_zip(ext_dir: Path, zip_path: Path) -> None:
    """把插件目录打成 zip（CRX 的第三段）。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(ext_dir.rglob("*")):
            if p.is_file():
                # 弧杠路径，Windows 上必须是 /
                z.write(p, p.relative_to(ext_dir).as_posix())


def build_crx(ext_dir: Path, out_path: Path,
              key_path: Path | None = None,
              store_zip: Path | None = None) -> Path:
    """把插件目录打成 CRX3。"""
    ext_dir = Path(ext_dir)
    if not (ext_dir / "manifest.json").exists():
        raise RuntimeError(f"{ext_dir} 里没有 manifest.json，不是插件目录")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    zp = store_zip or out_path.with_suffix(".zip")
    make_zip(ext_dir, zp)
    zip_bytes = zp.read_bytes()

    private_pem = ensure_key(key_path)
    openssl = _find_openssl()
    pub_der = public_key_der(private_pem, openssl)
    crx_id = build_crx_id(pub_der)
    signed_data = build_signed_header(crx_id)
    signature = sign(signed_data + zip_bytes, private_pem, openssl)
    header = build_header(
        {"public_key": pub_der, "signature": signature}, signed_data)

    out_path.write_bytes(
        CRX_MAGIC + struct.pack("<II", CRX_VERSION, len(header))
        + header + zip_bytes)
    return out_path


def verify_crx(path: Path) -> tuple[bool, str]:
    """自检：拆回 zip 并比内容。返回 (是否通过, 说明)。"""
    data = Path(path).read_bytes()
    if data[:4] != CRX_MAGIC:
        return False, "magic 不对，不是 CRX"
    ver, hlen = struct.unpack("<II", data[4:12])
    if ver != CRX_VERSION:
        return False, f"版本是 {ver}，不是 CRX3"
    body = data[12 + hlen:]
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            names = z.namelist()
            if "manifest.json" not in names:
                return False, "包内没有 manifest.json"
            return True, f"{len(names)} 个文件，含 manifest.json"
    except zipfile.BadZipFile as e:
        return False, f"zip 段损坏：{e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="把浏览器插件打包成 CRX3")
    ap.add_argument("ext_dir", help="插件目录（含 manifest.json）")
    ap.add_argument("-o", "--out", default="", help="输出 .crx 路径")
    ap.add_argument("--key", default="", help="RSA 私钥 PEM（正式上架用）")
    ap.add_argument("--keep-zip", action="store_true", help="同时保留 .zip")
    a = ap.parse_args(argv)

    ext = Path(a.ext_dir)
    out = Path(a.out) if a.out else ext.with_suffix(".crx")
    key = Path(a.key) if a.key else (out.with_suffix(".pem"))
    try:
        build_crx(ext, out, key if a.key else None)
    except Exception as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 1
    ok, msg = verify_crx(out)
    print(f"[{'OK' if ok else 'BAD'}] {out}  ({out.stat().st_size} 字节) {msg}")
    if not a.key:
        print("提示：本次用的是临时自签密钥，只能本机拖进浏览器安装；"
              "要上架请用 --key 指定你自己的 RSA 私钥。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
