"""Filename / path templating & sanitization (security-conscious)."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM\d|LPT\d)$", re.I)


def sanitize(name: str, max_len: int = 120) -> str:
    name = unicodedata.normalize("NFC", name)
    name = _ILLEGAL.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", "_", name)
    if _RESERVED.match(name):
        name = "_" + name
    if not name:
        name = "untitled"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        name = stem[: max_len - len(ext) - 1] + dot + ext
    return name


def url_basename(url: str) -> str:
    path = urlsplit(url).path
    name = path.rstrip("/").rsplit("/", 1)[-1] or "file"
    return sanitize(name)


def build_path(template: str, root: Path, *, site: str, album: str = "",
               date: str = "", theme: str = "", resolution: str = "",
               keyword: str = "", filename: str = "") -> Path:
    """template tokens: {site} {album} {date} {theme} {resolution} {keyword}
    {filename}

    The filename is always guaranteed to be part of the result: if the
    template omits {filename} it is appended, otherwise every image in an
    album would resolve to the same directory path and overwrite each other.
    {keyword} 为空时不产生子目录（#28 按主题词分子目录）。
    """
    # 2026-10-27（编号 12）：空模板 = 「全部平铺在 root 下」。
    # 原写法对空串会拼出 "/{filename}" —— 开头那个斜杠让 Path 变成**绝对路径**，
    # 随后被下面的越界守卫打平（文件名前多一个下划线）。空模板是合法配置
    # （子目录模式选"不分子目录"），必须走正常分支。
    if not (template or "").strip():
        template = "{filename}"
    elif "{filename}" not in template:
        template = template.rstrip("/") + "/{filename}"
    token_vals = {
        "site": sanitize(site or "site"),
        "album": sanitize(album or "album"),
        "date": sanitize(date or datetime.now().strftime("%Y-%m-%d")),
        "theme": sanitize(theme or "uncategorized"),
        "resolution": sanitize(resolution or "orig"),
        "keyword": sanitize(keyword) if keyword else "",
        "filename": filename,
    }
    rel = template.format(**token_vals)
    # Path traversal guard: resolve and ensure it stays under root.
    out = (root / rel).resolve()
    root_resolved = root.resolve()
    if root_resolved not in out.parents and out != root_resolved:
        # Reject escapes by flattening back into root.
        out = root_resolved / sanitize(rel.replace("\\", "/").replace("/", "_"))
    return out
