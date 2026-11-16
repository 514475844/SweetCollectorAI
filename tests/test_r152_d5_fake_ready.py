"""R152-D5 护栏：模型下载「假就绪」根治。

D5 修三件事：
  1. _download_one 收尾校验 Content-Length 期望值 —— 截断 body 抛 RuntimeError，
     绝不把半截文件 replace 成成品（旧实现连接一断就静默成功 = 假就绪）；
  2. download_model 换源即重置 .part —— 不同镜像的同一文件可能不是同一份字节，
     跨源 Range 续传会把两份内容拼成「能过大小检查的坏文件」；
  3. is_ready 加实证大小边车（.mrm_sizes.json）—— 边车在位时 onnx 大小必须
     与下载时一致；边车缺失/损坏 = 老目录，维持宽松判据（升级不误伤老用户）。

反向可变：
  · 删掉 Content-Length 校验 → 截断测试红；
  · 删掉换源重置 .part → 换源测试红；
  · is_ready 不读边车 → 边车校验测试红；
  · 不写边车 → 边车写入测试红。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import media_resource_manager.models_lib as M


class _FakeResp:
    """模拟 urllib 响应：headers / status / read / close。"""

    def __init__(self, body=b"", status=200, content_length=None):
        self._buf = bytearray(body)
        self.status = status
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, n=-1):
        if n < 0 or n > len(self._buf):
            n = len(self._buf)
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk

    def close(self):
        pass


# ---- _download_one：Content-Length 期望值校验 ----


def test_truncated_raises_and_keeps_part(monkeypatch, tmp_path):
    """body 比 Content-Length 短 → RuntimeError，dest 不落盘，.part 保留。"""
    dest = tmp_path / "model.onnx"
    fake = _FakeResp(body=b"x" * 500, status=200, content_length=1000)
    monkeypatch.setattr(M, "_open_url", lambda req: fake)
    with pytest.raises(RuntimeError, match="下载中断"):
        M._download_one("http://example.com/model.onnx", dest, None, None)
    assert not dest.exists(), "截断下载不应产出成品"
    part = dest.with_suffix(dest.suffix + ".part")
    assert part.exists(), "截断下载应保留 .part 供续传"
    assert part.stat().st_size == 500


def test_full_body_lands(monkeypatch, tmp_path):
    """body 完整 = Content-Length → 正常落盘。"""
    dest = tmp_path / "model.onnx"
    fake = _FakeResp(body=b"x" * 1000, status=200, content_length=1000)
    monkeypatch.setattr(M, "_open_url", lambda req: fake)
    M._download_one("http://example.com/model.onnx", dest, None, None)
    assert dest.exists()
    assert dest.stat().st_size == 1000
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_resume_206_two_chunks(monkeypatch, tmp_path):
    """已有 400B .part，206 续传 600B → 成品 1000B 且内容正确。"""
    dest = tmp_path / "model.onnx"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"A" * 400)
    fake = _FakeResp(body=b"B" * 600, status=206, content_length=600)
    monkeypatch.setattr(M, "_open_url", lambda req: fake)
    M._download_one("http://example.com/model.onnx", dest, None, None)
    assert dest.stat().st_size == 1000
    assert dest.read_bytes() == b"A" * 400 + b"B" * 600


def test_server_ignores_range_200(monkeypatch, tmp_path):
    """服务器忽略 Range 返回 200 整份 → 覆盖 .part，成品正确。"""
    dest = tmp_path / "model.onnx"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"stale" * 80)
    fake = _FakeResp(body=b"C" * 1000, status=200, content_length=1000)
    monkeypatch.setattr(M, "_open_url", lambda req: fake)
    M._download_one("http://example.com/model.onnx", dest, None, None)
    assert dest.stat().st_size == 1000
    assert dest.read_bytes() == b"C" * 1000


def test_resume_interrupted_raises(monkeypatch, tmp_path):
    """206 续传但 body 比 Content-Length 短 → RuntimeError。"""
    dest = tmp_path / "model.onnx"
    part = dest.with_suffix(dest.suffix + ".part")
    part.write_bytes(b"A" * 400)
    fake = _FakeResp(body=b"B" * 300, status=206, content_length=600)
    monkeypatch.setattr(M, "_open_url", lambda req: fake)
    with pytest.raises(RuntimeError, match="下载中断"):
        M._download_one("http://example.com/model.onnx", dest, None, None)
    assert not dest.exists()


def test_no_content_length_skips_check(monkeypatch, tmp_path):
    """无 Content-Length（chunked 等）→ 跳过校验，正常落盘。"""
    dest = tmp_path / "model.onnx"
    fake = _FakeResp(body=b"x" * 500, status=200, content_length=None)
    monkeypatch.setattr(M, "_open_url", lambda req: fake)
    M._download_one("http://example.com/model.onnx", dest, None, None)
    assert dest.stat().st_size == 500


# ---- download_model：换源重置 .part + 边车写入 ----


def test_source_switch_resets_part(monkeypatch, tmp_path):
    """换源前 .part 必须被重置（不同镜像的字节可能不同）。"""
    part_reset_ok = []

    def fake_download(url, dest, on_progress, cancel, source=""):
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        if source == "hf-mirror 国内镜像":
            part.write_bytes(b"stale" * 100)
            raise RuntimeError(f"来源 {source} 失败（测试模拟）")
        part_reset_ok.append(not part.exists())
        if dest.suffix == ".json":
            dest.write_text("{}", encoding="utf-8")
        else:
            dest.write_bytes(b"x" * 2048)

    monkeypatch.setattr(M, "_download_one", fake_download)
    m = M.CATALOG[0]
    ok, msg = M.download_model(tmp_path, m["id"], mirror="auto")
    assert ok is True
    assert len(part_reset_ok) > 0, "没有发生换源，测试无效"
    assert all(part_reset_ok), f"换源前 .part 未被重置：{part_reset_ok}"


def test_sidecar_written_after_download(monkeypatch, tmp_path):
    """下载成功后写边车 .mrm_sizes.json，记录各文件实证大小。"""
    def fake_download(url, dest, on_progress, cancel, source=""):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix == ".json":
            dest.write_text("{}", encoding="utf-8")
        else:
            dest.write_bytes(b"x" * 2048)

    monkeypatch.setattr(M, "_download_one", fake_download)
    m = M.CATALOG[0]
    ok, msg = M.download_model(tmp_path, m["id"], mirror="auto")
    assert ok is True
    d = M.model_dir(tmp_path, m["id"])
    sidecar = d / M.SIZES_SIDECAR
    assert sidecar.exists(), "边车 .mrm_sizes.json 未写"
    sizes = json.loads(sidecar.read_text(encoding="utf-8"))
    onnx_name = Path(m["files"][0]).name
    assert sizes.get(onnx_name) == 2048


# ---- is_ready：边车校验 ----


def _mk_model_dir(tmp_path, model_id="clip-vit-b32", onnx_size=2048):
    d = M.model_dir(tmp_path, model_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "model_quantized.onnx").write_bytes(b"x" * onnx_size)
    (d / "config.json").write_text("{}", encoding="utf-8")
    return d


def test_sidecar_validates_onnx_size(tmp_path):
    """边车在位时 onnx 被改小 → is_ready False；恢复 → True。"""
    d = _mk_model_dir(tmp_path)
    (d / M.SIZES_SIDECAR).write_text(
        json.dumps({"model_quantized.onnx": 2048}), encoding="utf-8")
    assert M.is_ready(tmp_path, "clip-vit-b32") is True
    (d / "model_quantized.onnx").write_bytes(b"x" * 1500)
    assert M.is_ready(tmp_path, "clip-vit-b32") is False
    (d / "model_quantized.onnx").write_bytes(b"x" * 2048)
    assert M.is_ready(tmp_path, "clip-vit-b32") is True


def test_sidecar_missing_old_dir_lenient(tmp_path):
    """无边车（老目录）→ 宽松放行，升级不误伤。"""
    d = _mk_model_dir(tmp_path)
    assert not (d / M.SIZES_SIDECAR).exists()
    assert M.is_ready(tmp_path, "clip-vit-b32") is True


def test_sidecar_corrupt_lenient(tmp_path):
    """边车损坏（非 JSON / 非 dict）→ 宽松放行。"""
    d = _mk_model_dir(tmp_path)
    (d / M.SIZES_SIDECAR).write_text("not json{{{", encoding="utf-8")
    assert M.is_ready(tmp_path, "clip-vit-b32") is True
    (d / M.SIZES_SIDECAR).write_text("[1,2,3]", encoding="utf-8")
    assert M.is_ready(tmp_path, "clip-vit-b32") is True
