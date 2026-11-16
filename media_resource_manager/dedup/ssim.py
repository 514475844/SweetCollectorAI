"""Optional SSIM-based near-duplicate clustering (requires opencv).

When opencv-python is not installed this degrades to a no-op so the rest of
the pipeline keeps working. All processing is local.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def _cv2():
    try:
        import cv2  # type: ignore
        import numpy as np
        return cv2, np
    except Exception:
        return None, None


def ssim_pair(a: Path, b: Path) -> Optional[float]:
    cv2, np = _cv2()
    if cv2 is None:
        return None
    try:
        ia = cv2.imread(str(a), cv2.IMREAD_GRAYSCALE)
        ib = cv2.imread(str(b), cv2.IMREAD_GRAYSCALE)
        if ia is None or ib is None:
            return None
        # resize to common size for fair comparison
        ia = cv2.resize(ia, (256, 256))
        ib = cv2.resize(ib, (256, 256))
        return float(cv2.matchTemplate(ia, ib, cv2.TM_CCOEFF_NORMED)[0][0])
    except Exception:
        return None


def cluster(paths: list[Path], threshold: float = 0.92) -> list[list[Path]]:
    """Greedy clustering by SSIM. Returns groups of near-duplicates.

    **r39 修复**：原实现每比较一对就 ``imread`` 两次 —— 150 张图 ≈ 22000 次
    解码，真机全按钮扫描实测把主线程堵了 **295 秒**（看门狗一路 STALL）。
    而且库里只要混进读不出的坏文件（0 字节 / 截断的 webp），每次解码都白跑一遍。

    这里改成**先把每张图解码成 256×256 灰度矩阵缓存下来（只解码 N 次）**，
    之后 pair 比较只做 ``matchTemplate``（微秒级）。
    """
    cv2, np = _cv2()
    if cv2 is None or len(paths) < 2:
        return []
    cache: list[tuple[Path, object]] = []
    for p in paths:
        g = None
        try:
            raw = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if raw is not None:
                g = cv2.resize(raw, (256, 256))
        except Exception:
            g = None
        cache.append((p, g))
    usable = [i for i, (_, g) in enumerate(cache) if g is not None]
    if len(usable) < 2:
        return []

    def sim(i: int, j: int):
        try:
            return float(cv2.matchTemplate(
                cache[i][1], cache[j][1], cv2.TM_CCOEFF_NORMED)[0][0])
        except Exception:
            return None

    groups: list[list[Path]] = []
    assigned = set()
    for i in usable:
        if i in assigned:
            continue
        grp = [cache[i][0]]
        assigned.add(i)
        for j in usable:
            if j <= i or j in assigned:
                continue
            s = sim(i, j)
            if s is not None and s >= threshold:
                grp.append(cache[j][0])
                assigned.add(j)
        if len(grp) > 1:
            groups.append(grp)
    return groups
