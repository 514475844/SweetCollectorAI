"""请求2：黑名单「黑白照」内容级灰度识别。

文字黑名单匹配不到 B&W 图（URL/标签里没"黑白照"字样），必须看图片本身。
"""
import pytest
from pathlib import Path
from PIL import Image

from media_resource_manager.engine import _hit_blacklist
from media_resource_manager.dedup.hash import is_grayscale


def _gray_jpg(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (128, 128, 128)).save(p, "JPEG")
    return p


def _color_jpg(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (200, 30, 10)).save(p, "JPEG")
    return p


def test_is_grayscale_detects_bw(tmp_path):
    g = _gray_jpg(tmp_path / "g.jpg")
    c = _color_jpg(tmp_path / "c.jpg")
    assert is_grayscale(g) is True
    assert is_grayscale(c) is False


def test_is_grayscale_palette_mode(tmp_path):
    # 调色板灰度图也应判为灰度
    p = tmp_path / "p.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", (32, 32), 100).convert("P").save(p, "PNG")
    assert is_grayscale(p) is True


def test_blacklist_grayscale_content_hit(tmp_path):
    g = _gray_jpg(tmp_path / "g.jpg")
    c = _color_jpg(tmp_path / "c.jpg")
    # 纯文字匹配必不中（URL/标签里没有"黑白照"字样）
    assert _hit_blacklist("https://x.com/g.jpg", [], ["黑白照"]) is False
    # 内容级灰度识别：黑名单含黑白照 + 图片真灰度 → 命中
    assert _hit_blacklist("https://x.com/g.jpg", [], ["黑白照"],
                          path=str(g)) is True
    # 彩色图不命中
    assert _hit_blacklist("https://x.com/c.jpg", [], ["黑白照"],
                          path=str(c)) is False


def test_blacklist_grayscale_alias_keywords(tmp_path):
    g = _gray_jpg(tmp_path / "g.jpg")
    for kw in ("黑白", "灰度", "bw", "monochrome", "grayscale"):
        assert _hit_blacklist("https://x.com/x.jpg", [], [kw],
                              path=str(g)) is True


def test_blacklist_text_still_works():
    # 文字黑名单行为不变
    assert _hit_blacklist("https://x.com/nude/foo.jpg", [], ["nude"]) is True
    assert _hit_blacklist("https://x.com/clean/foo.jpg", [], ["nude"]) is False
