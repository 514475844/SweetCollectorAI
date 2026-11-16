"""日志脱敏（#36）：URL 只留域名、本机路径只留文件名。"""
import logging

from media_resource_manager.logutil import (
    SanitizeFilter, sanitize_text, setup_logging)


def test_url_keeps_domain_only():
    s = sanitize_text("下载 https://s1.pictoa.com/album/12345.jpg?token=abc 完成")
    assert "pictoa.com" in s, "域名必须保留，否则没法排障"
    assert "12345.jpg" not in s
    assert "token=abc" not in s


def test_http_also_masked():
    s = sanitize_text("GET http://example.org/a/b/c")
    assert "example.org" in s
    assert "/a/b/c" not in s


def test_windows_path_keeps_filename():
    s = sanitize_text(r"保存到 <user-path>")
    assert "pic_001.jpg" in s, "文件名要留着，否则无法定位是哪张图"
    assert "Admin" not in s
    assert "AppData" not in s


def test_plain_text_untouched():
    assert sanitize_text("下载完成 3 张") == "下载完成 3 张"


def test_empty_safe():
    assert sanitize_text("") == ""
    assert sanitize_text(None) is None


def test_filter_masks_record():
    rec = logging.LogRecord("mrm", logging.INFO, __file__, 1,
                            "fetch https://x.com/secret/path", None, None)
    assert SanitizeFilter().filter(rec) is True
    assert "secret/path" not in rec.getMessage()


def test_filter_masks_args():
    rec = logging.LogRecord("mrm", logging.INFO, __file__, 1,
                            "err %s", (r"D:\data\private\a.jpg",), None)
    SanitizeFilter().filter(rec)
    out = rec.getMessage()
    assert "private" not in out
    assert "a.jpg" in out


def test_setup_logging_toggle(tmp_path):
    logger = setup_logging(tmp_path, sanitize=True)
    assert any(isinstance(f, SanitizeFilter) for f in logger.filters)
    logger = setup_logging(tmp_path, sanitize=False)
    assert not any(isinstance(f, SanitizeFilter) for f in logger.filters)
    # 还原，避免影响同会话其它用例
    setup_logging(tmp_path, sanitize=True)
