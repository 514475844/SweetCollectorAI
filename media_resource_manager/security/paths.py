"""Path-traversal & malicious-filename protection.

All library writes must go through safe_join so a remote filename can never
escape the library root (e.g. "../../etc/passwd").
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote


def safe_join(root: Path, *parts: str) -> Path:
    root = root.resolve()
    rel = Path()
    for p in parts:
        p = unquote(p)
        rel = rel / p
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Path escape blocked: {rel}")
    return candidate


def is_safe_under(root: Path, candidate: Path) -> bool:
    root = root.resolve()
    candidate = candidate.resolve()
    return candidate == root or root in candidate.parents
