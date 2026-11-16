"""R152-E15 环境变量补齐护栏（2026-10-22 R209 棒）。

订单原文（R152-E15）：「MRM_ 覆盖缺口：缺 MRM_WEB_ENABLED/MRM_LOG_LEVEL/
MRM_COMPLIANCE_LEVEL/MRM_AI；MRM_WEB_USER 被接收不鉴权（R105 后失效但文档
仍宣传）。修：补齐 env + 删/改文档与 compose 的"登录账号"表述。」
（文档半边已由 R207 棒完成，本文件只护**代码半边**。）

口径：
* 四个 env 全是**活旋钮**（web_enabled / ai_enabled_global / compliance_level
  在引擎与 Web 里有真实行为；LOG_LEVEL 进 setup_logging）——写完要能真调出来；
* **只改内存不回写 config.json**（容器按 env 重建，写回会让排障变地狱）；
* 非法值当场 SystemExit + 中文（与既有 int/bool 口径一致，不许静默用错值）；
* MRM_LOG_LEVEL 特判：AppConfig 无 log_level 字段，不进 _ENV_MAP。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from media_resource_manager.config import AppConfig
from media_resource_manager.headless import (
    _ENV_MAP, _LOG_LEVELS, _env_log_level, apply_env_overrides,
)

_ROOT = Path(__file__).resolve().parents[1]
_HEADLESS = _ROOT / "media_resource_manager" / "headless.py"
_README = _ROOT / "docker" / "README.md"


def _code(path: Path) -> str:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("MRM_WEB_ENABLED", "MRM_AI", "MRM_COMPLIANCE_LEVEL",
              "MRM_LOG_LEVEL"):
        monkeypatch.delenv(k, raising=False)


# ================================================== 一、_ENV_MAP 形状
def test_env_map_has_three_new_knobs():
    assert _ENV_MAP["MRM_WEB_ENABLED"] == ("web_enabled", "bool")
    assert _ENV_MAP["MRM_AI"] == ("ai_enabled_global", "bool")
    assert _ENV_MAP["MRM_COMPLIANCE_LEVEL"] == ("compliance_level", "str")


def test_log_level_is_special_cased_not_in_env_map():
    """AppConfig 没有 log_level 字段 ⇒ 不许塞进 _ENV_MAP（塞了会
    setattr 出一个没人读的字段，接口还返回 ok —— 假闭环）。"""
    assert "MRM_LOG_LEVEL" not in _ENV_MAP
    src = _code(_HEADLESS)
    assert "def _env_log_level" in src


# ================================================== 二、真调覆盖
def test_apply_env_overrides_three_knobs(monkeypatch):
    monkeypatch.setenv("MRM_WEB_ENABLED", "1")
    monkeypatch.setenv("MRM_AI", "false")
    monkeypatch.setenv("MRM_COMPLIANCE_LEVEL", "strict")
    cfg = AppConfig()
    applied = apply_env_overrides(cfg)
    assert cfg.web_enabled is True
    assert cfg.ai_enabled_global is False
    assert cfg.compliance_level == "strict"
    assert any("web_enabled=True" in a for a in applied)
    assert any("ai_enabled_global=False" in a for a in applied)
    assert any("compliance_level=strict" in a for a in applied)


def test_apply_env_overrides_does_not_write_config(monkeypatch):
    """只改内存：函数体不许**调用** save_config（容器写回 = 排障地狱）。

    判据用调用形态 ``save_config(``——docstring 里为说明 R157 历史
    提过这个词（全角括号），按子串搜会被注释/dialog 文案骗到（R73 家族）。
    """
    import inspect
    src = inspect.getsource(apply_env_overrides)
    assert "save_config(" not in src


def test_bad_compliance_level_exits_with_chinese(monkeypatch):
    monkeypatch.setenv("MRM_COMPLIANCE_LEVEL", "bogus")
    with pytest.raises(SystemExit) as ei:
        apply_env_overrides(AppConfig())
    msg = str(ei.value)
    assert "MRM_COMPLIANCE_LEVEL" in msg and "合规基线" in msg
    assert "off" in msg and "standard" in msg, "要把合法值列出来"


def test_bad_bool_env_exits(monkeypatch):
    """int 解析失败照旧 SystemExit；bool 走 _as_bool 宽容口径
    （认不出的串 = False，既有语义不变——本轮不改它）。"""
    monkeypatch.setenv("MRM_WEB_PORT", "abc")
    with pytest.raises(SystemExit):
        apply_env_overrides(AppConfig())
    monkeypatch.delenv("MRM_WEB_PORT")
    monkeypatch.setenv("MRM_WEB_ENABLED", "maybe")
    cfg = AppConfig()
    apply_env_overrides(cfg)
    assert cfg.web_enabled is False


# ================================================== 三、MRM_LOG_LEVEL 特判
def test_env_log_level_parse_and_validate(monkeypatch):
    monkeypatch.setenv("MRM_LOG_LEVEL", "debug")
    assert _env_log_level() == logging.DEBUG
    monkeypatch.setenv("MRM_LOG_LEVEL", " ERROR ")
    assert _env_log_level() == logging.ERROR
    monkeypatch.delenv("MRM_LOG_LEVEL")
    assert _env_log_level() is None
    monkeypatch.setenv("MRM_LOG_LEVEL", "")
    assert _env_log_level() is None


def test_env_log_level_rejects_garbage(monkeypatch):
    monkeypatch.setenv("MRM_LOG_LEVEL", "loud")
    with pytest.raises(SystemExit) as ei:
        _env_log_level()
    msg = str(ei.value)
    assert "MRM_LOG_LEVEL" in msg and "日志级别" in msg
    assert "DEBUG" in msg and "CRITICAL" in msg


def test_headless_main_passes_level_to_setup_logging():
    """headless_main 里 setup_logging 必须带上 env 级别（否则 env 是死旋钮）。"""
    src = _code(_HEADLESS)
    seg = src[src.index("cfg.ensure_dirs()"):]
    assert "setup_logging(cfg.log_dir, sink=sys.stderr," in seg
    assert "level=_lvl" in seg, "setup_logging 没接 env 级别"
    assert "_env_log_level()" in src, "没人调 _env_log_level（env 是死旋钮）"


def test_headless_logs_level_when_env_set():
    src = _code(_HEADLESS)
    assert "MRM_LOG_LEVEL 只对本进程有效" in src


# ================================================== 四、文档同步
def test_docker_readme_documents_four_envs():
    """E15 要求 env 与文档一起补（文档半边 R207 已修登录表述，本轮补四项）。"""
    text = _README.read_text(encoding="utf-8")
    for k in ("MRM_WEB_ENABLED", "MRM_AI", "MRM_COMPLIANCE_LEVEL",
              "MRM_LOG_LEVEL"):
        assert f"`{k}`" in text, f"docker/README.md 没写 {k}"
    assert "off/loose/standard/strict" in text
    assert "DEBUG/INFO/WARNING/ERROR/CRITICAL" in text
