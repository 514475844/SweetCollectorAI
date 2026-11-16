"""敏感图本地模糊 / 打码（#35）。

隐私考量：有些图想留档但不想留原样（人脸、证件、水印等）。这里只做**本地**
处理——不上传、不联网，用 Pillow 生成马赛克或高斯模糊副本，原文件保留在磁盘
（另起一条隔离记录隐藏起来，避免被"回填"重新灌进图库）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

SENSITIVE_DEFAULT = ["face", "nude", "naked", "idcard", "id card", "passport",
                     "watermark", "人脸", "身份证", "护照", "裸", "水印"]

_MOSAIC = "mosaic"
_GAUSS = "gaussian"


def hits_sensitive(texts: Iterable[str], words: Iterable[str]) -> bool:
    """判断给定文本（URL/标签/关键词）是否命中敏感词（大小写不敏感）。"""
    ws = [str(w).strip().lower() for w in (words or []) if str(w).strip()]
    if not ws:
        return False
    blob = " ".join(str(t or "") for t in texts).lower()
    return any(w in blob for w in ws)


def blur_file(src: Path, mode: str = _MOSAIC, block: int = 10,
              radius: int = 12, suffix: str = "_blur") -> Optional[Path]:
    """生成 `src` 的模糊副本，返回副本路径；失败（无 PIL / 打不开）返回 None。

    * mosaic  —— 先缩到 1/block 再放大回来，得到马赛克（更像"打码"）
    * gaussian —— 高斯模糊，画面更柔和但仍有轮廓
    """
    try:
        from PIL import Image, ImageFilter
    except Exception:
        return None
    src = Path(src)
    try:
        with Image.open(src) as im:
            im.load()
            if mode == _GAUSS:
                out = im.convert("RGB").filter(ImageFilter.GaussianBlur(radius))
            else:
                block = max(2, int(block or 10))
                w, h = im.size
                small = im.resize((max(1, w // block), max(1, h // block)),
                                  Image.BILINEAR)
                out = small.resize((w, h), Image.NEAREST)
                if out.mode not in ("RGB", "L"):
                    out = out.convert("RGB")
            dst = src.with_name(f"{src.stem}{suffix}{src.suffix}")
            n = 1
            while dst.exists():
                dst = src.with_name(f"{src.stem}{suffix}({n}){src.suffix}")
                n += 1
            save_kwargs = {}
            if dst.suffix.lower() in (".jpg", ".jpeg"):
                save_kwargs["quality"] = 88
            out.save(dst, **save_kwargs)
            return dst
    except Exception:
        return None
