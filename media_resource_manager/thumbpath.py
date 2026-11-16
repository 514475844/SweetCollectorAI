"""缩略图缓存的分桶路径（R247）。

为什么必须分桶
--------------
缩略图原来**全部平铺**在 ``data/thumbs/`` 一个目录里。10 万张图，按
「网页一档 256 + 桌面两档 160/256」算，就是十几万到几十万个小文件挤在
同一层；到百万级就是几百万个。文件系统存得下，但**目录列举**会退化：
robocopy / rsync / 备份打包在 10 万文件的单目录上就要几十秒，百万级
直接不可用。而软件做备份、做「完整性检查」、做清理都要遍历这个目录。

分桶规则
--------
对**文件名**做 sha1，取前 2 位 + 第 3 位做两级目录：``a3/f/``。
共 256 × 16 = 4096 个桶，百万文件约 244 个/桶，千万级约 2441 个/桶，
都还在文件系统的舒适区。

为什么按文件名哈希、不按 id 取模
--------------------------------
两处调用方的 key 不一样：网页端是 ``<image_id>_<宽>.jpg``，桌面端是
``<内容摘要>_<标签><尺寸>.png``。用文件名统一处理，两条路一套规则，
而且「按名字找文件」不需要先知道 id。

兼容旧路径
----------
已经部署的 data/ 里是旧平铺文件。``find()`` 先查新路径、再退旧路径，
**不做迁移**：老文件继续能读（不白瞎一次重新生成），新生成的落到新路径。
旧的平铺文件可以随时由「清理缩略图」整目录删掉，删了会按需重建。
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def bucket_of(name: str) -> tuple[str, str]:
    """文件名 -> 两级桶名（2 + 1 个十六进制字符）。"""
    h = hashlib.sha1(str(name).encode("utf-8", "replace")).hexdigest()
    return h[0:2], h[2:3]


def cache_path(root, name: str) -> Path:
    """新路径：``<root>/a3/f/<name>``。只算路径，不碰磁盘。"""
    a, b = bucket_of(name)
    return Path(root) / a / b / name


def legacy_path(root, name: str) -> Path:
    """旧版平铺路径：``<root>/<name>``（只读兼容用）。"""
    return Path(root) / name


def find(root, name: str) -> Path:
    """找一张缩略图：新路径优先，没有就退回旧平铺路径。

    两条都不存在时返回**新路径** —— 调用方拿到后直接写即可
    （父目录由调用方 ``mkdir(parents=True)``，不要在这里建：
    连查都不该产生副作用，测试里也会断言「查不存在的文件不建目录」）。
    """
    p = cache_path(root, name)
    try:
        if p.exists():
            return p
    except OSError:
        pass
    old = legacy_path(root, name)
    try:
        if old.exists():
            return old
    except OSError:
        pass
    return p
