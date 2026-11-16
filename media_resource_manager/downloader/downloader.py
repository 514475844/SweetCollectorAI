"""Concurrent, resumable downloader with min-size/res filters & metadata.

Compliance:
  * Never overwrites existing files (skip if present & verified).
  * Never deletes system files; isolation dir used for unsafe/quarantined.
  * Pauses automatically when free disk space drops below threshold.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from ..crawler.fetcher import Fetcher, RobotDisallowed, AccessBlocked
from ..dedup.hash import sha256_bytes, phash_image, image_dimensions, image_exif, is_low_variance
from .naming import url_basename, build_path, sanitize

MIN_FREE_BYTES = 200 * 1024 * 1024  # 默认阈值；可被 cfg.min_free_mb 覆盖

# URL 无后缀时按 PIL 识别的格式补后缀（覆盖 Photon/Blogger/图床等 extensionless CDN）
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
_FMT_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif",
            "BMP": "bmp", "AVIF": "avif", "TIFF": "tif", "ICO": "ico"}


def _threshold_bytes(cfg) -> int:
    """#54 磁盘空间预警阈值（字节）。cfg.min_free_mb=0 表示不检查。"""
    try:
        mb = int(getattr(cfg, "min_free_mb", 200) or 0)
    except Exception:
        mb = 200
    if mb <= 0:
        return 0
    return mb * 1024 * 1024


@dataclass
class DownloadOutcome:
    url: str
    path: Optional[Path]
    ok: bool
    skipped: bool = False
    reason: str = ""
    detail: str = ""      # 人类可读的补充说明（如磁盘剩余多少 MB）
    sha256: Optional[str] = None
    phash: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    bytes: int = 0


class Downloader:
    def __init__(self, fetcher: Fetcher, root: Path,
                 template: str = "{site}/{album}/{date}",
                 min_width: int = 0, min_height: int = 0, min_bytes: int = 0,
                 concurrency: int = 4, delay: float = 1.0,
                 overwrite: bool = False, limiter=None):
        self.f = fetcher
        self.root = root
        self.template = template
        self.min_width = min_width
        self.min_height = min_height
        self.min_bytes = min_bytes
        self.sem = asyncio.Semaphore(concurrency)
        self.delay = delay
        self.overwrite = overwrite
        # F24：本轮**共享**的带宽令牌桶（rateplan.BandwidthLimiter）。
        # 必须是共享的 —— 每次下载各建一个桶的话，并发 4 就是 4 倍带宽，
        # 限速形同虚设。None = 不限速（默认配置下的老行为，一个字都不改）。
        self.limiter = limiter
        self._stop = False

    def stop(self):
        self._stop = True

    @staticmethod
    def _free_space(p: Path) -> int:
        try:
            return shutil.disk_usage(str(p)).free
        except Exception:
            return MIN_FREE_BYTES * 2

    def free_space_report(self) -> tuple[int, int]:
        """返回 (剩余字节, 阈值字节)。阈值来自 cfg.min_free_mb。"""
        thr = _threshold_bytes(self.f.cfg)
        return self._free_space(self.root), thr

    @staticmethod
    def _unique_dest(dest: Path) -> Path:
        """浏览器式同名处理：img.jpg 已存在 -> img(1).jpg -> img(2).jpg ...

        保留网页默认文件名，不随便改名；重复文件搜 "(1)" 就能找到。
        """
        if not dest.exists():
            return dest
        stem, dot, ext = dest.name.rpartition(".")
        if not dot:
            stem, ext = dest.name, ""
        for i in range(1, 1000):
            cand = (dest.with_name(f"{stem}({i}).{ext}") if ext
                    else dest.with_name(f"{stem}({i})"))
            if not cand.exists():
                return cand
        return dest

    @staticmethod
    def _reserve_path(dest: Path) -> Path:
        """原子地抢占一个不冲突的落盘路径。

        用 O_CREAT|O_EXCL 创建空文件：并发下只有一个下载者能拿到该文件名，
        其余立刻顺延到 name(1).ext，杜绝两个同名文件互相覆盖导致漏图。
        """
        import os
        o_bin = getattr(os, "O_BINARY", 0)
        cur = Path(dest)
        while True:
            try:
                fd = os.open(str(cur), os.O_CREAT | os.O_EXCL | os.O_WRONLY | o_bin)
            except FileExistsError:
                cur = Downloader._unique_dest(cur)
                continue
            os.close(fd)
            return cur

    @staticmethod
    def _cleanup_reserved(dest: Path) -> None:
        """下载失败时，删除 _reserve_path 留下的 0 字节空文件。

        只删大小为 0 的文件，绝不碰已写入内容的真图；避免空文件堆积在磁盘上
        （之前会泄漏成「0 KB 空白图」被回填扫进图库）。
        """
        try:
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink()
        except Exception:
            pass

    async def download_one(self, url: str, *, site: str = "site",
                           album: str = "", date: str = "",
                           theme: str = "", resolution: str = "",
                           keyword: str = "", meta: Optional[dict] = None,
                           already: Optional[callable] = None) -> DownloadOutcome:
        """already(url): 由调用方（引擎/DB）判断这个 URL 是否已经下载过。
        已下载 -> 跳过；文件名冲突但 URL 是新的 -> 浏览器式 (1) 命名。"""
        async with self.sem:
            if self._stop:
                return DownloadOutcome(url, None, False, reason="stopped")
            # #54 磁盘空间预警：剩余空间低于阈值 → 停止本轮下载并上报，
            # 由队列层统一暂停所有任务（阈值可在设置里改，0=不检查）
            thr = _threshold_bytes(self.f.cfg)
            if thr > 0 and self._free_space(self.root) < thr:
                self._stop = True
                free = self._free_space(self.root)
                return DownloadOutcome(
                    url, None, False, reason="low_disk",
                    detail=f"磁盘剩余 {free // (1024 * 1024)} MB，"
                           f"低于阈值 {thr // (1024 * 1024)} MB")
            # 该 URL 已经下载过（看 DB，不看文件名）-> 幂等跳过
            if already is not None:
                try:
                    if already(url):
                        return DownloadOutcome(url, None, ok=False,
                                               skipped=True,
                                               reason="already_in_db")
                except Exception:
                    pass
            name = url_basename(url)
            final = build_path(self.template, self.root, site=site, album=album,
                               date=date, theme=theme, resolution=resolution,
                               keyword=keyword, filename=name)
            final.parent.mkdir(parents=True, exist_ok=True)
            # 原子抢占最终文件名（保留网页默认名；冲突按 name(1) 顺延），
            # 真正的内容先写进同名的 .part 临时文件，下载完成再 rename 过去——
            # 这样即使中途崩溃/被杀，磁盘上只剩 .part，不会被误当成"已下载"。
            final = self._reserve_path(final)
            part = final.with_name(final.name + ".part")
            part = self._reserve_path(part)
            # #51 断点续传：上次中断留下的 .part 还有字节 → 从断点接着下
            written = part.stat().st_size if part.exists() else 0
            got = 0          # F24：本次**实际收到**的字节数（断点续传只算新增）
            for attempt in range(self.f.cfg.retry + 1):
                try:
                    status, data = await self.f.fetch_bytes(
                        url, self.delay, range_start=written)
                except (RobotDisallowed, AccessBlocked) as e:
                    self._cleanup_reserved(part)
                    final.unlink(missing_ok=True)
                    return DownloadOutcome(url, None, False, reason=str(e))
                except httpx.HTTPStatusError as e:
                    # 4xx（除 429）是永久错误：不要再重试，避免对死链空等几分钟
                    code = getattr(getattr(e, "response", None), "status_code", 0)
                    if 400 <= code < 500 and code != 429:
                        self._cleanup_reserved(part)
                        final.unlink(missing_ok=True)
                        return DownloadOutcome(url, None, False,
                                               reason=f"http_{code}")
                    # 5xx / 429：退避后重试（含断点续传：written 已计入已写字节）
                    if attempt >= self.f.cfg.retry:
                        self._cleanup_reserved(part)
                        final.unlink(missing_ok=True)
                        return DownloadOutcome(url, None, False,
                                               reason="fetch_failed")
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                except Exception:
                    if attempt >= self.f.cfg.retry:
                        self._cleanup_reserved(part)
                        final.unlink(missing_ok=True)
                        return DownloadOutcome(url, None, False,
                                               reason="fetch_failed")
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                # 断点续传安全网：请求了偏移但服务器忽略 Range 回了 200（整文件），
                # 继续 append 会重复 → 丢弃已写部分，从头下
                if written > 0 and status == 200:
                    part.unlink(missing_ok=True)
                    written = 0
                with open(part, "ab") as fh:
                    fh.write(data)
                    written += len(data)
                    got += len(data)
                data = b""
                break
            # F24：分时段限速 —— 按**实收字节**扣令牌，超了就睡到额度回满。
            # 放在这里（而不是成功分支里）是为了让「被过滤/空文件」也计流量：
            # 字节已经过去了，不扣就等于限速漏了一条缝。
            # ⚠ 限速失败绝不能弄挂下载（它是优化不是主流程），所以吞掉异常。
            if self.limiter is not None and got > 0:
                try:
                    await self.limiter.take(got)
                except Exception:
                    pass
            # 无扩展名 CDN URL（Photon / Blogger / 图床等）落盘后没有后缀，
            # 图库按 suffix 判断是否图片会当成"非图片"不显示。这里按实际内容
            # 格式补后缀（仅在没有图片后缀时触发，其它站点 URL 不受影响）。
            _stem, _dot, _ext = final.name.rpartition(".")
            if not _dot or ("." + _ext.lower()) not in _IMG_EXTS:
                try:
                    _fmt, _ = image_exif(part)
                except Exception:
                    _fmt = None
                _newext = _FMT_EXT.get((_fmt or "").upper(), "jpg")
                _old_final = final
                final = final.with_name(f"{final.name}.{_newext}")
                final = self._reserve_path(final)
                try:
                    _old_final.unlink(missing_ok=True)
                except Exception:
                    pass
                _new_part = final.with_name(final.name + ".part")
                try:
                    os.replace(str(part), str(_new_part))
                    part = _new_part
                except Exception:
                    pass
            if not part.exists() or part.stat().st_size == 0:
                self._cleanup_reserved(part)
                final.unlink(missing_ok=True)
                return DownloadOutcome(url, None, False, reason="empty")
            # 2026-10-24 白图根治：站点反爬会把 HTML 拦截页/详情页当图片返回
            #（后缀 .jpg、有字节数、但不是图）。按文件头魔数校验，非图片
            # 一律当失败、删文件，绝不入库（图库白图+尺寸未知的根因）。
            from ..dedup.hash import looks_like_image as _looks_img
            if not _looks_img(part):
                self._cleanup_reserved(part)
                part.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                return DownloadOutcome(
                    url, None, False,
                    reason="下载到的不是图片（站点返回了网页/拦截页，可能需要 Cookie 或代理）")
            # r37：设置里开了「保存时转成 JPG」→ **在算 sha256 之前**先转码，
            # 这样哈希、体积、格式、后缀全是 jpg 的（转完再算会变成
            # 「内容是 jpg、名字叫 .webp」，图库按后缀过滤时这张就隐身了）。
            # 转失败就原样放过 —— 转码出问题绝不许弄挂一张图的落盘。
            try:
                if getattr(getattr(self.f, "cfg", None), "convert_to_jpg", False):
                    from ..convert_img import convert_file_to_jpg
                    _np, _conv = convert_file_to_jpg(part)
                    if _conv:
                        part = _np
                        final = _np.with_name(_np.stem + ".jpg")
            except Exception:
                pass
            total = part.stat().st_size
            # filters
            w, h = image_dimensions(part)
            if (self.min_width and w and w < self.min_width) or \
               (self.min_height and h and h < self.min_height) or \
               (self.min_bytes and total < self.min_bytes):
                part.unlink(missing_ok=True)
                final.unlink(missing_ok=True)
                return DownloadOutcome(url, None, False, reason="filtered",
                                       width=w, height=h, bytes=total)
            sha = sha256_bytes(part.read_bytes())
            # #14 纯色图 dHash 全 0 会互判重复 → 低方差图不给 phash，只走 sha256
            ph = None if is_low_variance(part) else phash_image(part)
            # R152-C7：EXIF/格式必须**趁 part 还在**先取。
            # 以前这行排在 os.replace(part, final) **之后** —— replace 一走，
            # part 这个路径指向的文件已经不存在了（极少数留下 .part 残件的
            # 情况除外），image_exif 内部又把异常吞成 (None, None) ⇒
            # 每张图的拍摄时间恒为空：图库「按时间排序」实际是全表乱序，
            # 用户眼里的症状就是「刚下的图混在三年前的图里」。
            fmt, exif_date = image_exif(part)
            # 来源等元数据保存在数据库（images 表），不再写 .json 边车文件
            try:
                os.replace(part, final)
            except OSError:
                # 极端情况：改名失败但内容已在 part，仍按成功返回 part 路径
                final = part
            return DownloadOutcome(url, final, True, sha256=sha, phash=ph,
                                   width=w, height=h, bytes=total,
                                   detail=json.dumps({"fmt": fmt,
                                                      "taken_at": exif_date},
                                                     ensure_ascii=False))

    async def download_many(self, urls: list[str], **kw) -> list[DownloadOutcome]:
        return await asyncio.gather(*(self.download_one(u, **kw) for u in urls))
