"""Regression: browser extension (Sweet采集助手) must be present and valid."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension" / "Sweet采集助手"


def test_extension_files_present():
    for name in ("manifest.json", "popup.html", "popup.js",
                 "icon16.png", "icon48.png", "icon128.png", "安装说明.txt"):
        assert (EXT / name).exists(), f"missing: {name}"


def test_manifest_is_valid_mv3():
    m = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3
    assert m["action"]["default_popup"] == "popup.html"
    for perm in ("cookies", "activeTab", "scripting", "clipboardWrite"):
        assert perm in m["permissions"], f"missing permission: {perm}"
    assert "<all_urls>" in m["host_permissions"]


def test_popup_js_uses_cookies_api_and_clipboard():
    js = (EXT / "popup.js").read_text(encoding="utf-8")
    assert "chrome.cookies.getAll" in js
    assert "navigator.clipboard.writeText" in js
    assert "chrome.scripting.executeScript" in js
