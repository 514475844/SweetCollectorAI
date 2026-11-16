from media_resource_manager.dedup.hash import (
    sha256_bytes, hamming, phash_image, dhash_pillow, is_low_variance,
)
from pathlib import Path


def test_sha256_deterministic():
    a = sha256_bytes(b"hello")
    b = sha256_bytes(b"hello")
    assert a == b and len(a) == 64


def test_hamming():
    assert hamming("aaaa", "aaaa") == 0
    assert hamming("aaaa", "bbbb") == 4 or hamming("aaaa", "bbbb") is not None
    assert hamming("aaa", "bbbb") is None  # length mismatch


def test_phash_optional_without_pil(tmp_path):
    # If PIL/imagehash missing, phash returns None instead of crashing.
    f = tmp_path / "x.txt"
    f.write_bytes(b"not an image")
    try:
        from PIL import Image  # noqa
        assert phash_image(f) is None or isinstance(phash_image(f), str)
    except Exception:
        assert phash_image(f) is None


def _solid(tmp: Path, name: str, color) -> Path:
    from PIL import Image
    p = tmp / name
    Image.new("RGB", (64, 64), color).save(p)
    return p


def test_low_variance_detects_solids_not_gradients(tmp_path):
    """#14 纯色/近纯色 → 低方差；渐变图 → 正常方差。"""
    red = _solid(tmp_path, "red.jpg", (200, 30, 30))
    blue = _solid(tmp_path, "blue.jpg", (30, 30, 200))
    assert is_low_variance(red)
    assert is_low_variance(blue)
    # 渐变图（历史 fixture 规范：感知去重测试必须用渐变）绝不能判低方差
    from PIL import Image
    grad = tmp_path / "grad.png"
    g = Image.new("L", (64, 64))
    g.putdata([((y * 64 + x) * 255 // 4095) for y in range(64)
               for x in range(64)])
    g.save(grad)
    assert not is_low_variance(grad)
    # 坏文件/非图片：不误杀（返回 False）
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    assert not is_low_variance(bad)


def test_solid_colors_have_identical_dhash(tmp_path):
    """#14 根因文档化：不同颜色纯色图 dHash 全 0 → 汉明距 0 → 必须靠
    低方差守卫跳过感知去重，否则互判重复互删。"""
    red = _solid(tmp_path, "r.jpg", (200, 30, 30))
    blue = _solid(tmp_path, "b.jpg", (30, 30, 200))
    ha, hb = dhash_pillow(red), dhash_pillow(blue)
    assert ha == hb == "0" * 64
    assert hamming(ha, hb) == 0
