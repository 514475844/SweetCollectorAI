"""R152-F1（日志脱敏加码）+ F5（autostart 启动器乱码）护栏。

F1 订单原文：「logutil.py:54-60 只 sanitize str 型 args（LOG.info("%s",
Path(...)) 裸奔）；:30-31 只认 Windows 路径。修：先 str() 再 sanitize；
补 POSIX 路径正则。」

审计追加修复（同属 F1 防「假闭环」）：SanitizeFilter 原来只挂在 "mrm"
logger 上，而 Python logging 的 callHandlers 只应用 *handler 级* filter——
mrm.engine / mrm.headless 等子 logger 的记录整体绕过脱敏。现改为
logger + handler 双挂。

F5 订单原文：「autostart.py:68-74 生成 .bat 无 chcp 65001。修：参照
shortcut.py:126 补。」
"""
import io
import logging
import os
import sys
from pathlib import Path

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from media_resource_manager import autostart
from media_resource_manager.logutil import (
    SanitizeFilter, sanitize_text, setup_logging)

SECRET_WIN = r"C:\Users\admin\AppData\Roaming\private\pic_001.jpg"
SECRET_POSIX = "/home/u/pics/private/pic_002.jpg"


def _engine_line(msg, *args):
    """模拟 engine 的调用方式：子 logger + handler 级 SanitizeFilter，
    返回落到 handler 里的那一行文本（生产接线同款，见 setup_logging）。"""
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.addFilter(SanitizeFilter())
    root = logging.getLogger("mrm")
    root.addHandler(h)
    try:
        # 故意走子 logger（engine.py:41 同款），验证父 filter 不再被绕过
        logging.getLogger("mrm.engine").info(msg, *args)
        h.flush()
        return stream.getvalue()
    finally:
        root.removeHandler(h)


# ---------------------------------------------------------------- F1 参数脱敏

def test_f1_path_arg_is_masked():
    out = _engine_line("下载 %s 完成", Path(SECRET_WIN))
    assert "pic_001.jpg" in out, "文件名要保留，否则无法排障"
    assert "AppData" not in out
    assert "private" not in out


def test_f1_posix_path_arg_is_masked():
    out = _engine_line("saved %s", Path(SECRET_POSIX))
    assert "pic_002.jpg" in out
    assert "/home/u" not in out
    assert "pics/private" not in out


def test_f1_posix_path_in_plain_text():
    s = sanitize_text(f"保存到 {SECRET_POSIX} 完成")
    assert "pic_002.jpg" in s
    assert "/home/u" not in s


def test_f1_dict_args_masked():
    out = _engine_line("结果 %(p)s %(n)d",
                       {"p": SECRET_WIN, "n": 3})
    assert "AppData" not in out
    assert "pic_001.jpg" in out
    assert "3" in out


def test_f1_numeric_args_still_format():
    # %d 的参数不能被脱敏逻辑换成 str，否则 formatting TypeError
    out = _engine_line("进度 %d/%d", 3, 7)
    assert "3/7" in out


def test_f1_exception_arg_with_path_masked():
    out = _engine_line("失败: %s",
                       OSError(f"cannot open {SECRET_WIN}"))
    assert "AppData" not in out
    assert "pic_001.jpg" in out


def test_f1_pathlike_msg_masked():
    out = _engine_line(Path(SECRET_WIN))
    assert "AppData" not in out
    assert "pic_001.jpg" in out


# ------------------------------------------------------------- F1 防误伤回归

def test_f1_normal_slashes_untouched():
    for text in ("TCP/IP 连接失败", "和/或 二选一", "得分 9/10",
                 "a/b/c 相对路径", "上传/下载 均支持"):
        assert sanitize_text(text) == text, f"误伤正常文本: {text}"


def test_f1_url_still_keeps_domain_only():
    s = sanitize_text("GET https://s1.pictoa.com/album/12345.jpg?token=abc")
    assert "pictoa.com" in s
    assert "12345.jpg" not in s
    assert "token=abc" not in s


def test_f1_masking_is_idempotent():
    once = sanitize_text(f"x {SECRET_WIN} y {SECRET_POSIX}")
    assert sanitize_text(once) == once, "脱敏必须幂等（双挂 filter 不二次损坏）"


# ------------------------------------------------- F1 handler 级 filter 接线

def test_f1_handler_level_filter_installed(tmp_path):
    logger = setup_logging(tmp_path, sanitize=True)
    assert any(isinstance(f, SanitizeFilter) for f in logger.filters)
    for h in logger.handlers:
        assert any(isinstance(f, SanitizeFilter) for f in h.filters), \
            "handler 也必须挂 SanitizeFilter，否则子 logger 记录绕过脱敏"
    logger = setup_logging(tmp_path, sanitize=False)
    assert not any(isinstance(f, SanitizeFilter) for f in logger.filters)
    for h in logger.handlers:
        assert not any(isinstance(f, SanitizeFilter) for f in h.filters)
    # 还原，避免影响同会话其它用例
    setup_logging(tmp_path, sanitize=True)


def test_f1_child_logger_record_reaches_handler_masked(tmp_path):
    # 端到端：setup_logging 之后用 engine 同款子 logger 记录，落盘须已脱敏
    root = logging.getLogger("mrm")
    # 先清掉历史 FileHandler（可能指向已删除的 tmp 目录），确保落到本用例目录
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            h.close()
            root.removeHandler(h)
    setup_logging(tmp_path, sanitize=True)
    try:
        logging.getLogger("mrm.headless").info("headless %s",
                                               Path(SECRET_WIN))
        for h in root.handlers:
            h.flush()
        text = (tmp_path / "mrm.log").read_text(encoding="utf-8")
        assert "AppData" not in text
        assert "pic_001.jpg" in text
    finally:
        for h in list(root.handlers):
            if isinstance(h, logging.FileHandler):
                h.close()
                root.removeHandler(h)


# ------------------------------------------------------------------- F5 乱码

def test_f5_autostart_bat_has_chcp65001(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    ok, detail = autostart.install(interval=1234, concurrency=3)
    assert ok, detail
    bat = autostart.bat_path()
    assert bat is not None and bat.exists()
    raw = bat.read_bytes()
    text = raw.decode("utf-8")          # 文件按 UTF-8 写，chcp 后 cmd 才读对
    assert "\r\r" not in text, "文本模式二次转译：\r\n 变成了 \r\r\n"
    lines = text.splitlines()
    assert lines[0] == "@echo off"
    assert lines[1] == "chcp 65001 >nul", "缺 chcp 65001：中文路径会变乱码"
    assert 'cd /d "' in text
    assert "--interval 1234" in text
    assert "--concurrency 3" in text


def test_f5_autostart_remove_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert autostart.install()[0]
    assert autostart.bat_path().exists()
    ok, _ = autostart.remove()
    assert ok
    assert not autostart.bat_path().exists()
