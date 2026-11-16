from pathlib import Path
from media_resource_manager.downloader.naming import sanitize, build_path, url_basename


def test_sanitize_strips_illegal():
    assert sanitize('a/b:c*?') == "a_b_c__"


def test_sanitize_reserved():
    assert sanitize("CON") == "_CON"


def test_build_path_guards_traversal(tmp_path):
    # use a real, existing root: build_path() resolves internally, and on
    # Windows comparing an unresolved '/tmp/lib' against a resolved path fails.
    root = tmp_path.resolve()
    # filename tries to escape
    out = build_path("{site}/{filename}", root, site="s",
                     filename="../etc/passwd")
    # The important properties: result stays UNDER root, contains no '..'
    # component, and never resolves outside to a system path.
    assert out.is_relative_to(root)
    assert ".." not in out.parts
    assert out == root / "etc" / "passwd" or out.parent == root


def test_url_basename():
    assert url_basename("https://x.com/a/b/IMG_01.jpg?x=1") == "IMG_01.jpg"
