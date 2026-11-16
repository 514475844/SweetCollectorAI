"""V0.0.12 护栏：网页任务表单补齐桌面已有的 9 个运行控制字段。

背景：两端对齐是用户硬约定。桌面表单早就有定时/允许时段/限速/最大页数/
图片过滤/关键词子目录/匹配模式，网页表单全部缺失——手机上建任务配不了
这些，回到桌面又得重配一遍。
钉四件事：
  1. 后端映射表 + 类型分组齐全（少一组 = 存进去类型不对）；
  2. 定时/允许时段有**校验**（填错静默存库 = 用户以为设上了、调度器永远
     不触发，这是补齐字段时最容易留下的暗坑）；
  3. 详情接口回读齐全（少一个 = 编辑页一打开是空的，保存即抹掉旧值）；
  4. 前端表单控件与提交换算（KB→字节）在位，且产物与源码同步。
"""
import re
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_WEBUI = (_ROOT / "media_resource_manager/webui.py").read_text(encoding="utf-8")
_VUE = (_ROOT / "media_resource_manager/webapp/src/views/Tasks.vue").read_text(
    encoding="utf-8")

NEW_FIELDS = ["schedule_at", "allowed_hours", "rate_limit", "max_pages",
              "min_width", "min_height", "min_bytes", "max_bytes",
              "keyword_subdir", "ai_match_mode", "ai_exclude_match_mode"]


def test_form_fields_mapped():
    m = re.search(r"_TASK_FORM_FIELDS = \{(.*?)\n    \}", _WEBUI, re.S)
    assert m, "找不到 _TASK_FORM_FIELDS，检查映射表是否重构并同步本护栏"
    body = m.group(1)
    for k in NEW_FIELDS:
        assert f'"{k}"' in body, f"网页表单字段映射缺 {k}"


def test_type_groups_cover_new_fields():
    ints = re.search(r"_TASK_INT_FIELDS = \((.*?)\)", _WEBUI, re.S).group(1)
    bools = re.search(r"_TASK_BOOL_FIELDS = \((.*?)\)", _WEBUI, re.S).group(1)
    for k in ("rate_limit", "max_pages", "min_width", "min_height",
              "min_bytes", "max_bytes"):
        assert k in ints, f"{k} 必须按整数收（否则 '30' 字符串直接进模型）"
    assert "keyword_subdir" in bools, "keyword_subdir 必须按布尔收"
    assert "ai_match_mode" not in ints and "ai_match_mode" not in bools, \
        "匹配模式是文本 any/all，不许误进数字/布尔组"


def test_schedule_and_hours_validated():
    assert "parse_schedule" in _WEBUI, "定时必须走共享解析校验（桌面同一函数）"
    seg = _WEBUI[_WEBUI.index("def _task_from_form"):]
    assert "定时格式不对" in seg, "非法定时未拒绝——会静默存库永不触发"
    assert "允许时段格式不对" in seg, "非法时段未拒绝——运行时判据匹配不上全天照跑"


def test_shared_parser_single_source():
    # 桌面转调共享实现，本体不许留第二份解析（两份早晚漂移）
    mw = (_ROOT / "media_resource_manager/gui/main_window.py").read_text(
        encoding="utf-8")
    seg = mw[mw.index("def _parse_schedule"):]
    seg = seg[:seg.index("\n    @")] if "\n    @" in seg else seg[:3000]
    assert "from ..schedule import parse_schedule" in seg
    assert "re.match" not in seg, "_parse_schedule 还在自己解析：两处实现会漂移"


def test_detail_returns_all_new_fields():
    for k in NEW_FIELDS:
        assert f'"{k}"' in _WEBUI, f"详情缺字段 {k}（编辑保存会抹掉旧值）"


def test_vue_form_controls_and_units():
    # 表单态里大小过滤用 KB（与桌面 spinbox 同口径），bytes 只在提交时出现
    frontend_keys = [k for k in NEW_FIELDS
                     if k not in ("min_bytes", "max_bytes")] + ["min_kb", "max_kb"]
    for k in frontend_keys:
        assert f"form.{k}" in _VUE, f"Tasks.vue 缺 {k} 控件绑定"
    # KB↔字节换算（与桌面 *1024 同口径）
    assert "min_bytes: Math.round((Number(form.min_kb) || 0) * 1024)" in _VUE
    assert "delete body.min_kb" in _VUE, "KB 表单态字段不许发给后端"
    # 编辑回填 bytes→KB
    assert "Math.round((row.min_bytes ?? 0) / 1024)" in _VUE


def test_priority_default_aligned_with_desktop():
    # 桌面 1–9 默认 5；网页曾用 0，两端默认不一致会悄悄改变排队结果
    m = re.search(r"const blank = \(\) => \(\{(.*?)\n\}\)", _VUE, re.S)
    assert m and "priority: 5" in m.group(1), \
        "网页新建任务优先级默认必须与桌面一致（5）"
