"""Shared logging for Sweet采集AI版.

Two sinks:
  * a rotating-ish file in the app log dir (always written, for later diagnosis)
  * an optional GUI sink (a callable) so the engine can push progress lines
    into the on-screen 日志 panel without importing Qt.

The engine only ever does ``LOG.info(...)``; how it reaches the screen is wired
once at startup via :func:`setup_logging`.

Throttling: the GUI sink handler drops messages that arrive faster than
``min_interval`` seconds, so a fast image-download loop can't flood the widget
(the file handler always keeps everything).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path

_ROOT = logging.getLogger("mrm")
_ROOT.setLevel(logging.INFO)

# ---- 日志脱敏（设置 → 日志脱敏）----------------------------------------
# URL 只保留 scheme+域名，路径与查询串打码；本地绝对路径只保留最后一段文件名。
# 目的：日志/诊断包里不再出现完整网址与本机目录结构，同时保留排障所需信息。
_URL_RE = re.compile(r"(https?://)([^/\s'\"<>)]+)([^\s'\"<>]*)")
_WIN_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n]+[\\/])+([^\\/:*?\"<>|\r\n]*)")
# R152-F1: POSIX 绝对路径（Linux/docker/macOS 日志）。
# 前导字符三重排除：\w/. 防「和/或」「TCP/IP」「9/10」误伤；:/ 防把
# URL 的 "//" 与 "域名/" 之后半段当路径起点（URL 掩码在先跑，此正则
# 不能再二次破坏已掩码文本）。
_POSIX_PATH_RE = re.compile(
    r"(?<![\w.])(?<![:/])/(?:[^/\\:*?\"<>|\s\r\n]+/)+([^/\\:*?\"<>|\s\r\n]*)")


def sanitize_text(text: str) -> str:
    """Return `text` with URLs/local paths masked. Safe for any input."""
    if not text:
        return text
    try:
        text = _URL_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}/<路径已打码>", text)
        text = _WIN_PATH_RE.sub(lambda m: f"<目录已打码>\\{m.group(1)}", text)
        text = _POSIX_PATH_RE.sub(lambda m: f"<目录已打码>/{m.group(1)}", text)
    except Exception:
        return text
    return text


def _mask_arg(a):
    """R152-F1: 日志参数先脱敏再交给 ``%`` 格式化。

    旧代码只 sanitize str 型参数，``LOG.info("%s", Path(...))`` 之类会把
    完整路径裸奔进日志。现在：
      * str            → 直接脱敏；
      * os.PathLike    → 先 str() 再脱敏；
      * dict/list/tuple→ 递归；
      * int/float/bool/None → 原样返回（既无敏感内容，也保证 ``%d`` 可用）；
      * 其它对象（异常等）→ str() 后脱敏，但仅当确有内容被打码才替换，
        避免改掉参数类型导致格式化 TypeError。
    """
    if isinstance(a, str):
        return sanitize_text(a)
    if isinstance(a, os.PathLike):
        return sanitize_text(str(a))
    if isinstance(a, dict):
        return {k: _mask_arg(v) for k, v in a.items()}
    if isinstance(a, (list, tuple)):
        masked = [_mask_arg(x) for x in a]
        return tuple(masked) if isinstance(a, tuple) else masked
    if a is None or isinstance(a, (int, float, bool)):
        return a
    try:
        s = str(a)
    except Exception:
        return a
    m = sanitize_text(s)
    return m if m != s else a


class SanitizeFilter(logging.Filter):
    """Attach to the root logger so every handler receives masked records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # msg 也可能是 Path/异常等非 str（R152-F1）：同样先脱敏；
            # str 型 msg 是主路径，不能漏（test_log_sanitize 既有护栏）。
            if record.msg is not None:
                try:
                    s = (record.msg if isinstance(record.msg, str)
                         else str(record.msg))
                    m = sanitize_text(s)
                    if m != s or isinstance(record.msg, str):
                        record.msg = m
                except Exception:
                    pass
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {k: _mask_arg(v)
                                   for k, v in record.args.items()}
                elif isinstance(record.args, tuple):
                    record.args = tuple(_mask_arg(a) for a in record.args)
        except Exception:
            pass
        return True


class GuiSinkHandler(logging.Handler):
    def __init__(self, sink, min_interval: float = 0.12):
        super().__init__()
        self.sink = sink
        self.min_interval = min_interval
        self._last = 0.0
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        now = time.monotonic()
        with self._lock:
            if now - self._last < self.min_interval:
                return
            self._last = now
        try:
            self.sink(msg)
        except Exception:
            pass


def setup_logging(log_dir, sink=None, level=logging.INFO, file_name="mrm.log",
                  sanitize: bool = True, max_mb: int = 10,
                  backup_count: int = 5):
    """Configure the shared logger.

    Idempotent per sink type: calling again only adds a sink that isn't present.
    `sanitize=True` installs :class:`SanitizeFilter`, masking URLs/local paths.
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    _ROOT.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    # 脱敏过滤器：开关变化时先摘掉旧的再加新的，保证只有一个生效。
    # R152-F1：除了挂在 _ROOT logger 上，还必须挂到各 handler 上——
    # Python logging 的 callHandlers 只应用 *handler 级* filter，父 logger
    # 的 filter 不会作用于子 logger（mrm.engine / mrm.headless）的记录，
    # 只挂 logger 等于引擎日志整体绕过脱敏（假闭环）。
    for f in list(_ROOT.filters):
        if isinstance(f, SanitizeFilter):
            _ROOT.removeFilter(f)
    for h in _ROOT.handlers:
        h.filters = [x for x in h.filters if not isinstance(x, SanitizeFilter)]

    # 2026-10-27（编号 8）：日志轮转。原来是裸 FileHandler —— 只长不缩，
    # 长期跑采集（每天几十万行）会把磁盘吃满，而且出问题时单个文件大到打不开。
    # 现在按大小切分并保留 N 份；`max_mb<=0` 时退回老的单文件行为。
    # ⚠ 判断条件必须用 **FileHandler 的父类语义**：RotatingFileHandler 也是
    #   FileHandler 子类，所以「有没有文件 handler」这个判断本身不用改；
    #   但两条路径的 handler 类型不同，重复调用时不能既加 Rotating 又加 File。
    _has_file = any(isinstance(h, logging.FileHandler) for h in _ROOT.handlers)
    if not _has_file:
        try:
            _mb = int(max_mb or 0)
        except (TypeError, ValueError):
            _mb = 10
        if _mb > 0:
            from logging.handlers import RotatingFileHandler
            try:
                _cnt = max(1, int(backup_count or 1))
            except (TypeError, ValueError):
                _cnt = 5
            fh = RotatingFileHandler(
                log_dir / file_name,
                maxBytes=_mb * 1024 * 1024,
                backupCount=_cnt,
                encoding="utf-8",
            )
        else:
            fh = logging.FileHandler(log_dir / file_name, encoding="utf-8")
        fh.setFormatter(fmt)
        _ROOT.addHandler(fh)

    if sink and not any(isinstance(h, GuiSinkHandler) for h in _ROOT.handlers):
        gh = GuiSinkHandler(sink)
        gh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
        _ROOT.addHandler(gh)

    # handler 级脱敏在 handler 建好之后再挂（先建后挂，见上方注释）
    if sanitize:
        _ROOT.addFilter(SanitizeFilter())
        for h in _ROOT.handlers:
            if not any(isinstance(x, SanitizeFilter) for x in h.filters):
                h.addFilter(SanitizeFilter())
    return _ROOT


def get_logger(name: str = "mrm"):
    return logging.getLogger(name)
