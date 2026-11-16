"""V0.0.11 护栏：旧「全局严格模式」勾选已撤销，每级独立严格开关是唯一入口。

背景：2026-10-28 起白名单每一级都有独立「严格」勾选（含第 4 级起的动态行），
老的全局勾只剩「全部严格」的观感同步，且与单级勾双向互相覆盖，用户确认去掉。
数据层兼容不变：label_levels.apply_levels 仍按 any(st[1:]) 回写 ai_label_strict，
老任务读取时 stricts_of 展开——本文件钉的是**界面**不许再把那勾加回来。
"""
import re
from pathlib import Path

_MAIN = Path(__file__).resolve().parents[1] / \
    "media_resource_manager/gui/main_window.py"
_TXT = _MAIN.read_text(encoding="utf-8")


def test_global_strict_checkbox_gone():
    assert "self.f_ai_strict = QCheckBox" not in _TXT, \
        "旧全局严格勾选又被加回来了：每级已有独立开关"
    assert "f_ai_strict" not in _TXT, \
        "main_window 里不应再有任何 f_ai_strict 引用"


def test_save_does_not_or_global_strict():
    # 保存段：二三级严格只看各自自己的勾
    m = re.search(r"_st_all = \[.*?\]", _TXT, re.S)
    assert m, "找不到 _st_all 保存段，检查表单代码是否重构并同步本护栏"
    assert "f_strict2.isChecked()" in m.group(0)
    assert "f_strict3.isChecked()" in m.group(0)


def test_bottom_bar_removed():
    # 原 R52 的底部栏（左下角放全局严格勾）不许阴魂不散回来
    assert "严格模式放到" not in _TXT


def test_data_layer_compat_kept():
    # 撤销的是界面勾，不是数据字段：读写两头必须还能处理老 ai_label_strict
    from media_resource_manager.label_levels import stricts_of, apply_levels
    from media_resource_manager.models import TaskConfig
    t = TaskConfig(id=None, name="x", adapter_name="", entry_urls=["u"], ai_labels=["猫"],
                   ai_labels_l2=["黑长直"], ai_label_strict=True)
    st = stricts_of(t)
    assert st[1] is True, "老任务的全局严格必须展开成第 2 层起全部严格"
    t2 = TaskConfig(id=None, name="y", adapter_name="", entry_urls=["u"], ai_labels=["猫"],
                    ai_labels_l2=["黑长直"])
    apply_levels(t2, [["猫"], ["黑长直"]], [False, True])
    assert t2.ai_label_strict is True, \
        "apply_levels 仍要按任一级严格回写老字段（老读者依赖它）"
