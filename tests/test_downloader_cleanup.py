"""Regression tests for the 0-KB blank-image bug.

Root cause: _reserve_path() creates an empty 0-byte file via O_CREAT|O_EXCL,
but download_one used to return on failure WITHOUT deleting it. Those empty
files piled up on disk and got swept into the gallery as "0 KB blank images".
"""
import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest

from media_resource_manager.downloader.downloader import Downloader


class _FakeCfg:
    retry = 0
    delay = 0


class _FakeFetcher:
    """Minimal fetcher stub: returns bytes, raises an exception, or 404s."""
    def __init__(self, data):
        self.cfg = _FakeCfg()
        self._data = data

    async def fetch_bytes(self, url, delay=None, range_start=0):
        if isinstance(self._data, Exception):
            raise self._data
        return (200, self._data)


def _http_404():
    req = httpx.Request("GET", "http://x/a.jpg")
    resp = httpx.Response(404, request=req)
    return httpx.HTTPStatusError("404", request=req, response=resp)


def test_cleanup_reserved_removes_empty_but_keeps_real():
    with tempfile.TemporaryDirectory() as d:
        empty = Path(d) / "empty.jpg"
        empty.write_bytes(b"")
        real = Path(d) / "real.jpg"
        real.write_bytes(b"x" * 10)
        Downloader._cleanup_reserved(empty)
        Downloader._cleanup_reserved(real)
        assert not empty.exists(), "0-byte reserved file must be deleted"
        assert real.exists(), "non-empty file must never be touched"


def test_download_one_empty_cleans_reserved_file():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        dl = Downloader(_FakeFetcher(b""), root, concurrency=1, delay=0)
        out = asyncio.run(dl.download_one("http://x/a.jpg", site="s"))
        assert out.ok is False
        assert out.reason == "empty"
        leftovers = [p for p in root.rglob("*")
                     if p.is_file() and p.stat().st_size == 0]
        assert leftovers == [], f"empty reserved file leaked: {leftovers}"


def test_download_one_4xx_does_not_retry_and_cleans():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        dl = Downloader(_FakeFetcher(_http_404()), root, concurrency=1, delay=0)
        out = asyncio.run(dl.download_one("http://x/a.jpg", site="s"))
        assert out.ok is False
        assert out.reason == "http_404"
        leftovers = [p for p in root.rglob("*")
                     if p.is_file() and p.stat().st_size == 0]
        assert leftovers == [], f"empty reserved file leaked on 4xx: {leftovers}"
