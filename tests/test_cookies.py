"""Tests for the per-domain cookie store (CF cookie harvest feature)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def cookies_module(tmp_path: Path):
    """Import cookies with STORE_PATH pointed at a temp file."""
    import media_resource_manager.cookies as c
    c.STORE_PATH = tmp_path / "cookies.json"
    # clear any module-level cache state
    yield c
    if c.STORE_PATH.exists():
        c.STORE_PATH.unlink()


def test_set_get_delete_roundtrip(cookies_module):
    assert cookies_module.get_cookie("example.com") is None
    cookies_module.set_cookie("example.com", "cf_clearance=abc; x=1")
    assert cookies_module.get_cookie("example.com") == "cf_clearance=abc; x=1"
    cookies_module.del_cookie("example.com")
    assert cookies_module.get_cookie("example.com") is None


def test_all_cookies(cookies_module):
    cookies_module.set_cookie("a.com", "a=1")
    cookies_module.set_cookie("b.com", "b=2")
    assert cookies_module.all_cookies() == {"a.com": "a=1", "b.com": "b=2"}


def test_merge_domain_overrides_same_name(cookies_module):
    task = "cf_clearance=OLD; session=u"
    domain = "cf_clearance=NEW; csrf=k"
    merged = cookies_module.merge_cookies(task, domain)
    # cf_clearance from domain wins; both names preserved
    assert "cf_clearance=NEW" in merged
    assert "cf_clearance=OLD" not in merged
    assert "session=u" in merged
    assert "csrf=k" in merged


def test_merge_only_domain(cookies_module):
    assert cookies_module.merge_cookies("", "cf_clearance=Z") == "cf_clearance=Z"


def test_merge_none_domain(cookies_module):
    assert cookies_module.merge_cookies("a=1", None) == "a=1"


def test_cookie_for_task_uses_getter(cookies_module):
    seen = {}

    def fake_getter(domain):
        seen["d"] = domain
        return "cf_clearance=MAGIC" if domain == "cf.site" else None

    # task cookie present, domain cookie present -> merged, domain wins
    out = cookies_module.cookie_for_task("cf_clearance=old", "cf.site", fake_getter)
    assert seen["d"] == "cf.site"
    assert out == "cf_clearance=MAGIC"

    # no task cookie, domain has one
    out2 = cookies_module.cookie_for_task("", "cf.site", fake_getter)
    assert out2 == "cf_clearance=MAGIC"

    # neither -> empty
    out3 = cookies_module.cookie_for_task("", "", fake_getter)
    assert out3 == ""


def test_engine_imports_cookie_for_task():
    """engine.py must import cookie_for_task without breaking (smoke)."""
    import media_resource_manager.engine as eng
    assert hasattr(eng, "cookie_for_task")
