"""「保存时转成 JPG」——把采集回来的图统一落成 .jpg（r37）。

为什么需要它：真机跑完 916 张图库统计下来是 **699 webp + 180 avif + 37 jpg**。
而 Qt 二进制自带的 imageformats 插件只有 jpeg/webp/gif/bmp/tiff/svg/ico/pdf，
**没有 AVIF** —— 也就是说打包版里那 180 张 avif 在灯箱/预览里天生读不出来
（灯箱里已经加了 Pillow 回退兜底，但那只是"能看"，缩略图缓存、图库网格这些
仍按后缀判断）。开这个开关后，新采集的图一律存成 .jpg，一劳永逸。

转换规则
--------
* 只处理 Pillow 能读的位图（webp/avif/png/gif/bmp/tiff…），读不了的原样放过
  （绝不删原文件，失败绝不影响任务）。
* 有 alpha 的（png/webp/webm 逻辑透明）合成到白底再存 jpg，避免黑底。
* EXIF 方向先纠正（ImageOps.exif_transpose），转完方向不会反。
* 同目录、同名换后缀，覆盖同名目标 **前** 会先删旧文件——调用方保证路径唯一
  （下载器本来就走 _reserve_path 保持唯一）。
* 返回 (new_path, converted: bool)，converted=False 表示"本来就是 jpg/没动"。

性能：一张 1.5MB 的 avif 转 jpg 大概 30~80ms，gallery-dl 快路径是下载完
统一扫一遍目录（单线程，几百张也就十几秒），不影响采集主流程。
"""
from __future__ import annotations

from pathlib import Path

# 这些格式转 jpg 才有意义；jpg/jpeg 直接跳过
_JPG_EXT = {".jpg", ".jpeg"}
_SKIP_EXT = _JPG_EXT | {".mp4", ".webm", ".mkv", ".mov", ".avi", ".txt",
                        ".json", ".part"}

QUALITY = 88


def _to_rgb(im):
    """统一成 RGB 并返回（jpg 不支持 alpha / CMYK / 16bit）。

    带透明通道的（png / webp 透明 / gif）**合成到白底**，不是丢 alpha ——
    jpg 没 alpha 通道，硬丢会变成一块黑底，比原图还难看。
    """
    from PIL import Image as _I
    if im.mode in ("RGBA", "LA", "PA"):
        rgba = im.convert("RGBA")
        canvas = _I.new("RGB", im.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    if im.mode == "P":
        if (im.info or {}).get("transparency") is not None:
            return _to_rgb(im.convert("RGBA"))
        return im.convert("RGB")
    if im.mode not in ("RGB", "L"):
        try:
            return im.convert("RGB")
        except Exception:
            return im.convert("RGBA").convert("RGB")
    return im


def convert_file_to_jpg(path: Path, quality: int = QUALITY) -> tuple[Path, bool]:
    """把 path 转成同名 .jpg（原地）。返回 (最终路径, 是否真的转换了)。

    任何异常都吞掉并返回 (path, False)：转码失败绝不能让采集任务挂掉。
    """
    try:
        p = Path(path)
        if not p.exists() or p.suffix.lower() in _SKIP_EXT:
            return p, False
        if p.stat().st_size <= 0:
            return p, False
        from PIL import Image, ImageOps
        tmp = p.with_name(p.name + ".jpg.tmp")
        try:
            with Image.open(str(p)) as im:
                im = ImageOps.exif_transpose(im)
                im = _to_rgb(im)
                try:
                    im.save(str(tmp), "JPEG", quality=quality,
                            subsampling=1, optimize=True)
                except Exception:
                    im.save(str(tmp), "JPEG", quality=quality)
            # 换后缀前先把旧的原状留着：转换成功才动 final
            newp = p.with_suffix(".jpg")
            try:
                newp.unlink(missing_ok=True)
            except Exception:
                pass
            tmp.replace(newp)
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
            return newp, True
        finally:
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        try:
            Path(path).with_name(Path(path).name + ".jpg.tmp").unlink(
                missing_ok=True)
        except Exception:
            pass
        return Path(path), False


def convert_tree_to_jpg(root: Path, quality: int = QUALITY):
    """把一个目录（gallery-dl 快路径的落盘目录）里所有非 jpg 图转成 jpg。

    返回 (转换张数, 失败张数)。跳过子目录里的视频/元数据文件。
    """
    root = Path(root)
    n = fail = 0
    if not root.exists():
        return 0, 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() in _SKIP_EXT:
            continue
        _np, conv = convert_file_to_jpg(p, quality)
        if conv:
            n += 1
        elif p.exists() and p.stat().st_size > 0 and \
                p.suffix.lower() not in _JPG_EXT:
            # 非 jpg 但没转成（Pillow 读不了）→ 记失败，别把原文件删了
            fail += 1
    return n, fail
