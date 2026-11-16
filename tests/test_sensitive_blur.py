"""敏感图本地模糊（#35）。"""
import numpy as np
import pytest
from PIL import Image

from media_resource_manager.security.blur import blur_file, hits_sensitive


@pytest.fixture
def sample(tmp_path):
    """一张有明显纹理的测试图（纯色图无法验证模糊效果）。"""
    arr = np.zeros((64, 64, 3), dtype=np.uint8)
    for i in range(64):
        for j in range(64):
            arr[i, j] = (i * 4 % 256, j * 4 % 256, (i + j) * 2 % 256)
    p = tmp_path / "shot.png"
    Image.fromarray(arr).save(p)
    return p


def test_hits_sensitive_basic():
    assert hits_sensitive(["https://x.com/face/album"], ["face"])
    assert not hits_sensitive(["https://x.com/cat"], ["face"])
    assert hits_sensitive(["身份证 照片"], ["身份证"])


def test_hits_sensitive_case_insensitive():
    assert hits_sensitive(["NUDE beach"], ["nude"])


def test_no_words_never_hits():
    assert not hits_sensitive(["face"], [])


def test_mosaic_changes_pixels(sample):
    dst = blur_file(sample, mode="mosaic", block=8)
    assert dst is not None and dst.exists()
    assert dst.name == "shot_blur.png"
    a = np.asarray(Image.open(sample).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(dst).convert("RGB"), dtype=np.int16)
    assert np.abs(a - b).mean() > 5, "模糊后像素应当明显改变"


def test_gaussian_changes_pixels(sample):
    dst = blur_file(sample, mode="gaussian", radius=6)
    assert dst is not None and dst.exists()
    a = np.asarray(Image.open(sample).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(dst).convert("RGB"), dtype=np.int16)
    assert np.abs(a - b).mean() > 1


def test_original_preserved(sample):
    before = sample.read_bytes()
    blur_file(sample)
    assert sample.read_bytes() == before, "原图必须原样保留在磁盘"


def test_size_preserved(sample):
    dst = blur_file(sample, mode="mosaic", block=8)
    assert Image.open(dst).size == Image.open(sample).size


def test_second_call_no_overwrite(sample):
    d1 = blur_file(sample)
    d2 = blur_file(sample)
    assert d1 != d2 and d2.exists(), "同名模糊版不能互相覆盖"


def test_missing_file_returns_none(tmp_path):
    assert blur_file(tmp_path / "nope.png") is None
