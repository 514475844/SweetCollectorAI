"""R152 D3/E8/E10 护栏（2026-10-23 棒）。

D3：CLIP/dedup 解码内存峰值 —— `_bound_decode` 把大图解码峰值钉在 max_side 内，
   原来 `_open`/`dhash_pillow`/`is_low_variance`/`is_grayscale` 全尺寸 decode
   后才缩放（100MP ~1GB 峰值，并发 2 拖死机器）。
E8：crypt.py Linux 跨平台指纹 —— Linux 下 COMPUTERNAME/USERNAME/USERPROFILE
   皆空，原来指纹恒空 → 所有 Linux 机器指纹相同 → 导出包跨机可解。
   改取 /etc/machine-id + hostname + 用户名 + 主目录。
E10：Web 图片流式 + Range —— `_serve_image`/`_serve_original` 原来对任意
   大小的图都 `read_bytes()` 整张读进内存；≥1MB 改走 `_send_file` 分块流式
   + Range（206/Content-Range/Accept-Ranges）。

行为级断言为主；每条都配「变异后会红」的判据（见 _r223_mutate.py）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from media_resource_manager import crypt, webui
from media_resource_manager.classify.clip_model import LocalClassifier
from media_resource_manager.db import Database
from media_resource_manager.dedup.hash import (
    DECODE_MAX_SIDE, _bound_decode, dhash_pillow, is_grayscale,
    is_low_variance,
)


# ============================================================ D3 ====

def _solid_image(tmp_path: Path, name: str, w: int, h: int,
                 color=(200, 30, 30)) -> Path:
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", (w, h), color).save(p)
    return p


def test_d3_bound_decode_limits_large_image(tmp_path):
    """大图经 _bound_decode 后尺寸被钉在 max_side 内（不再全尺寸解码）。"""
    from PIL import Image
    p = _solid_image(tmp_path, "big.jpg", 2000, 1500)
    im = Image.open(p)
    assert max(im.size) > DECODE_MAX_SIDE          # 原图确实超大
    _bound_decode(im, DECODE_MAX_SIDE)
    assert max(im.size) <= DECODE_MAX_SIDE         # 解码峰值被钉住


def test_d3_bound_decode_preserves_small_image(tmp_path):
    """小图不受影响（尺寸不变，行为与原来一致）。"""
    from PIL import Image
    p = _solid_image(tmp_path, "small.jpg", 100, 80)
    im = Image.open(p)
    _bound_decode(im, DECODE_MAX_SIDE)
    assert im.size == (100, 80)


def test_d3_clip_open_bounds_large_image(tmp_path):
    """CLIP _open 对大图不再返回全尺寸（原来 ~1GB 峰值）。"""
    p = _solid_image(tmp_path, "huge.jpg", 3000, 2000)
    clf = LocalClassifier(labels=["a", "b"])
    pil = clf._open(p)
    assert pil is not None
    assert max(pil.size) <= DECODE_MAX_SIDE


def test_d3_dedup_functions_work_on_large_image(tmp_path):
    """dedup 三函数对大图不崩且结果确定。"""
    big = _solid_image(tmp_path, "big.jpg", 2000, 1500, (200, 30, 30))
    assert is_low_variance(big) is True            # 纯色 → 低方差
    assert dhash_pillow(big) == "0" * 64           # 纯色 dHash 全 0
    assert is_grayscale(big) is False              # 纯红 → 非黑白


def test_d3_dedup_gradient_not_low_variance(tmp_path):
    """渐变大图不被误判低方差（防「降采样把渐变抹平」的回归）。"""
    from PIL import Image
    grad = tmp_path / "grad.png"
    g = Image.new("L", (2000, 1500))
    g.putdata([((y * 2000 + x) * 255 // 3_000_000) for y in range(1500)
               for x in range(2000)])
    g.save(grad)
    assert is_low_variance(grad) is False


# ============================================================ E8 ====

def test_e8_windows_fingerprint_uses_computername():
    """Windows 路径不变：指纹含 COMPUTERNAME（存量导出兼容）。"""
    fp = crypt._fingerprint()
    assert fp, "Windows fingerprint should not be empty"
    computer = os.environ.get("COMPUTERNAME", "")
    if computer:
        assert computer.encode() in fp


def test_e8_linux_fingerprint_not_empty(monkeypatch):
    """Linux 下三个 Windows 环境变量皆空，但指纹不再为空
    （原来恒空 → 所有 Linux 机器指纹相同 → 跨机可解）。"""
    monkeypatch.setattr(os, "name", "posix")
    for k in ("COMPUTERNAME", "USERNAME", "USERPROFILE"):
        monkeypatch.delenv(k, raising=False)
    fp = crypt._fingerprint()
    assert fp, "Linux fingerprint must not be empty (was empty before E8)"
    # Linux 分支用 socket.gethostname()，指纹应含主机名
    hostname = socket.gethostname().encode()
    assert hostname in fp


def test_e8_linux_uses_machine_id(monkeypatch, tmp_path):
    """Linux 分支读 /etc/machine-id（mock 掉文件读，验证指纹含 machine-id）。"""
    monkeypatch.setattr(os, "name", "posix")
    for k in ("COMPUTERNAME", "USERNAME", "USERPROFILE"):
        monkeypatch.delenv(k, raising=False)
    fake_mid = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"

    real_open = open

    def _fake_open(path, *a, **kw):
        if str(path) in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            import io
            return io.StringIO(fake_mid)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", _fake_open)
    fp = crypt._fingerprint()
    assert fake_mid.encode() in fp, "Linux fingerprint should include machine-id"


def test_e8_roundtrip_still_works():
    """E8 改了指纹来源，但同机加密/解密往返仍一致。"""
    data = b"secret-payload-" * 100
    blob = crypt.encrypt_bytes(data, "pw")
    assert crypt.decrypt_bytes(blob, "pw") == data


# ============================================================ E10 ====

class _Cfg:
    def __init__(self):
        self.web_user = "admin"
        self.web_password = "secret123"
        self.web_bind = "127.0.0.1"
        self.web_port = 0
        self.ui_theme = "light"
        self.ai_gpu = False
        self.blur_sensitive = False
        self.review_key_ask = False


@pytest.fixture()
def db():
    td = Path(tempfile.mkdtemp(prefix="r152_e10_db_"))
    d = Database(td / "mrm.db")
    try:
        yield d
    finally:
        d.close()
        shutil.rmtree(td, ignore_errors=True)


@pytest.fixture()
def srv(db):
    s = webui.WebServer(_Cfg(), db)
    ok, _ = s.start()
    assert ok, "WebServer 启动失败"
    yield s
    s.stop()


def _auth():
    return {"Authorization": webui._token("admin", "secret123")}


def _get(port, path, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    for k, v in _auth().items():
        req.add_header(k, v)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def _add_image(db, path):
    db.execute("INSERT INTO tasks(id,name,adapter_name) "
               "VALUES (1,'t','generic')")
    db.execute(
        "INSERT INTO images(task_id,source_url,path,tags,score) "
        "VALUES (1,'http://x/1',?, '[]',0.5)", (str(path),))
    row = db.query("SELECT id FROM images WHERE path=?", (str(path),))
    return row[0]["id"]


def _big_file(tmp_path, name="big.jpg", size=2 * 1024 * 1024):
    p = tmp_path / name
    p.write_bytes(os.urandom(size))
    return p


def test_e10_small_file_full_response(srv, db, tmp_path):
    """<1MB 小图：200 全量返回，字节一致（简单路径行为不变）。"""
    p = tmp_path / "small.jpg"
    payload = b"\xff\xd8" + os.urandom(512 * 1024)   # ~512KB
    p.write_bytes(payload)
    iid = _add_image(db, p)
    st, body, hd = _get(srv.port, f"/thumb/{iid}")
    assert st == 200
    assert body == payload


def test_e10_large_file_full_response(srv, db, tmp_path):
    """≥1MB 大图：200 全量返回 + Accept-Ranges 头，字节一致。"""
    p = _big_file(tmp_path)
    payload = p.read_bytes()
    iid = _add_image(db, p)
    st, body, hd = _get(srv.port, f"/thumb/{iid}")
    assert st == 200
    assert body == payload
    assert hd.get("Accept-Ranges") == "bytes"


def test_e10_large_file_range_bytes(srv, db, tmp_path):
    """Range: bytes=0-1023 → 206 + Content-Range + 正确 1024 字节。"""
    p = _big_file(tmp_path)
    payload = p.read_bytes()
    iid = _add_image(db, p)
    st, body, hd = _get(srv.port, f"/thumb/{iid}",
                        {"Range": "bytes=0-1023"})
    assert st == 206
    assert body == payload[0:1024]
    assert "bytes 0-1023/" in hd.get("Content-Range", "")


def test_e10_large_file_range_open_ended(srv, db, tmp_path):
    """Range: bytes=1024- → 206，从 1024 到末尾。"""
    p = _big_file(tmp_path)
    payload = p.read_bytes()
    iid = _add_image(db, p)
    st, body, hd = _get(srv.port, f"/thumb/{iid}",
                        {"Range": "bytes=1024-"})
    assert st == 206
    assert body == payload[1024:]


def test_e10_large_file_range_suffix(srv, db, tmp_path):
    """Range: bytes=-512 → 206，最后 512 字节。"""
    p = _big_file(tmp_path)
    payload = p.read_bytes()
    iid = _add_image(db, p)
    st, body, hd = _get(srv.port, f"/thumb/{iid}",
                        {"Range": "bytes=-512"})
    assert st == 206
    assert body == payload[-512:]


def test_e10_orig_content_disposition(srv, db, tmp_path):
    """/orig/ 大图：200 + Content-Disposition: attachment。"""
    p = _big_file(tmp_path)
    iid = _add_image(db, p)
    st, body, hd = _get(srv.port, f"/orig/{iid}")
    assert st == 200
    assert "attachment" in hd.get("Content-Disposition", "")
    assert body == p.read_bytes()
