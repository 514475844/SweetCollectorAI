"""Exact + perceptual hashing.

Preference order for perceptual hashes:
  1. `imagehash` library (needs scipy/numpy) when installed.
  2. Pure Pillow dHash fallback (no heavy deps) so dedup works out of the box.

Both return a 64-char bit string, so the Hamming comparison is common to both.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: R152-D3：解码峰值边长上限。CLIP 只需 224×224、dedup 只需 ≤512，超过此
#: 边长的图**先降采样再 convert**，避免 100MP 原图 ~1GB 峰值（并发 2 直接
#: 把机器拖死）。draft 在 JPEG/MPO 解码期就降采样（近零内存）；非 JPEG 格式
#: draft 无效，thumbnail 兜底加载后缩到目标尺寸（仍有短暂全尺寸加载，但
#: 峰值被钉在 max_side 内，且后续 convert/resize 都在这个有界尺寸上做）。
DECODE_MAX_SIDE = 1024
_HASH_MAX_SIDE = 512      # dedup 系列只用 9px / 64px / 512px，512 足够


def _bound_decode(im, max_side: int) -> None:
    """把已惰性打开的 PIL 图的解码峰值限制在 max_side 边长内（原地）。

    Image.open 是惰性的（只读头），内存峰值发生在 .convert()/.resize() 强制
    全尺寸解码时。这里先 draft（JPEG/MPO 解码期降采样），再 thumbnail 兜底
    （非 JPEG 格式 draft 无效，thumbnail 加载后缩到目标尺寸）。调用方随后
    的 convert/resize 都在这个有界尺寸上做，峰值不再随原图分辨率爆炸。
    """
    from PIL import Image
    try:
        im.draft("RGB", (max_side, max_side))
    except Exception:
        pass
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def dhash_pillow(path: Path, hash_size: int = 8) -> Optional[str]:
    """Difference hash (64-bit) using Pillow only — no scipy/numpy needed."""
    try:
        from PIL import Image
        with Image.open(path) as im0:
            _bound_decode(im0, _HASH_MAX_SIDE)      # R152-D3：限峰值再 convert
            im = im0.convert("L").resize((hash_size + 1, hash_size),
                                         Image.Resampling.LANCZOS)
            px = list(im.getdata())
        bits = []
        for r in range(hash_size):
            row = px[r * (hash_size + 1):(r + 1) * (hash_size + 1)]
            bits.extend("1" if row[c] > row[c + 1] else "0" for c in range(hash_size))
        return "".join(bits)
    except Exception:
        return None


def phash_image(path: Path) -> Optional[str]:
    """pHash via `imagehash` when available, else Pillow dHash."""
    try:
        from PIL import Image
        import imagehash  # type: ignore
        with Image.open(path) as im:
            return "".join("1" if bool(b) else "0"
                           for b in imagehash.phash(im).hash.flatten())
    except Exception:
        return dhash_pillow(path)


def dhash_image(path: Path) -> Optional[str]:
    return dhash_pillow(path)


def is_low_variance(path: Path, std_threshold: float = 2.0) -> bool:
    """纯色/近纯色检测：像素亮度标准差 <= std_threshold 视为低方差。

    纯色图的 dHash 64 位全 0——任何两张纯色图（哪怕颜色完全不同）汉明距
    恒为 0，会被感知去重误判为重复互删。低方差图不参与感知去重，只走
    sha256 精确去重。异常时返回 False（宁可多比对，绝不误放）。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            _bound_decode(im, _HASH_MAX_SIDE)      # R152-D3：限峰值再 convert
            im = im.convert("L").resize((64, 64), Image.Resampling.LANCZOS)
            px = list(im.getdata())
        n = len(px)
        if n == 0:
            return False
        mean = sum(px) / n
        var = sum((v - mean) ** 2 for v in px) / n
        return var ** 0.5 <= std_threshold
    except Exception:
        return False


def image_dimensions(path: Path) -> tuple[Optional[int], Optional[int]]:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.width, im.height
    except Exception:
        return None, None


# ⚠ 2026-10-24 白图 bug 根治：站点反爬会把 HTML 拦截页/详情页当"图片"返回，
# gallery-dl / Downloader 只查后缀和 0 字节，这些 .jpg 后缀的 HTML 直接落盘
# 入库 → 图库一片白图 + 尺寸未知。统一在这里按**文件头魔数**判定真图片。
_IMAGE_MAGIC = (
    b"\xff\xd8\xff",            # JPEG
    b"\x89PNG\r\n\x1a\n",       # PNG
    b"GIF87a", b"GIF89a",       # GIF
    b"BM",                      # BMP
    b"RIFF",                    # WebP（还需后续 4 字节是 WEBP）
)


def looks_like_image(path) -> bool:
    """按文件头魔数判断是否是真图片（防 HTML 拦截页存成 .jpg）。

    只读前 16 字节，成本可忽略；任何异常一律返回 False（宁可误杀不错放）。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except Exception:
        return False
    if len(head) < 8:
        return False
    if head.startswith(b"<") or head[:5].lower() in (b"<!doc", b"<html"):
        return False                                    # HTML 拦截页
    for m in _IMAGE_MAGIC:
        if head.startswith(m):
            if m == b"RIFF":
                return head[8:12] == b"WEBP"            # RIFF 还可能是 WAV
            return True
    return False


def hamming(a: Optional[str], b: Optional[str]) -> Optional[int]:
    if not a or not b or len(a) != len(b):
        return None
    return sum(x != y for x, y in zip(a, b))


def is_grayscale(path: Path, frac: float = 0.97, tol: int = 20) -> bool:
    """内容级黑白/灰度检测：图片是否为黑白照。

    用于黑名单里的「黑白照」等关键词——纯文字黑名单匹配不到 B&W 图（URL/
    标签里没有"黑白照"字样），必须看图片本身。判定：逐像素 RGB 三通道最大
    最小差 <= tol 即视为灰像素；灰像素占比 >= frac 则整图判为黑白。已为灰度
    模式(L/LA/1)直接返回 True。大图先缩到 512 提速。异常时返回 False（不误杀）。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            if im.mode in ("L", "LA", "1"):
                return True
            _bound_decode(im, _HASH_MAX_SIDE)      # R152-D3：限峰值再 convert
            if im.mode == "P":
                try:
                    im = im.convert("RGB")
                except Exception:
                    return False
            rgb = im.convert("RGB")
            w, h = rgb.size
            if w * h > 1_000_000:
                rgb = rgb.resize((512, 512))
            px = rgb.getdata()
        gray = 0
        total = 0
        for (r, g, b) in px:
            total += 1
            if abs(r - g) <= tol and abs(g - b) <= tol and abs(r - b) <= tol:
                gray += 1
        return total > 0 and (gray / total) >= frac
    except Exception:
        return False


def image_exif(path: Path) -> tuple[Optional[str], Optional[str]]:
    """#68 提取图片格式与拍摄时间（EXIF DateTimeOriginal）。

    返回 (格式, 拍摄时间 ISO 串)；提取失败返回 (None, None)。
    只读取元数据，不解码整张图，开销很小。
    """
    fmt = None
    taken = None
    try:
        from PIL import Image
        with Image.open(path) as im:
            fmt = (getattr(im, "format", None) or "").upper() or None
            try:
                exif = im.getexif()
            except Exception:
                exif = None
            if exif:
                # 36867 = DateTimeOriginal, 306 = DateTime
                for tag in (36867, 306):
                    v = exif.get(tag)
                    if v:
                        taken = str(v).strip().replace(":", "-", 2)
                        break
    except Exception:
        pass
    return fmt, taken
