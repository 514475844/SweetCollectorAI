"""R230 · 「学习」开关的**共享读写层**（R149-B 补全）。

订单出处：``.workbuddy/PARITY_MATRIX.md`` 缺失项第 3 条 —— 桌面端有「学习」
开关（AI与过滤菜单 + AI 设置页，R149-B 落地），Web 端只有**代码层**的 gate
（``classify.preference.is_learning_enabled`` 生效），用户**看不见也勾不了**。

为什么单独开一层：桌面与网页若各写一遍「改 cfg.learn_enabled → 同步全局
gate → 落盘」，迟早漂成「桌面关了、手机还在学」。三步合并成一个函数，
两端都只调它。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

#: 老配置没有 ``learn_enabled`` 键时的取值（与 config.AppConfig 默认值同源）。
DEFAULT = True

#: cfg 上的字段名。**不许**在别处硬写这个字符串，一律走本模块的读写函数。
FIELD = "learn_enabled"


def read_enabled(cfg: Any) -> bool:
    """读开关：**老配置无键 = 开**（兼容 R149-B 之前的配置文件）。"""
    return bool(getattr(cfg, FIELD, DEFAULT))


def apply_enabled(cfg: Any, value: bool,
                  save: Optional[Callable[[], Any]] = None) -> bool:
    """写开关：改 cfg → 同步全局 gate →（给了 ``save`` 才）落盘。

    ⚠ 顺序不许换：先翻 gate 再落盘的话，落盘一失败，界面显示的状态和
    重启后的真实状态就对不上，用户会以为「我关了它又自己开了」。

    ``save`` 走**参数注入**而不是在这里 import：网页端落盘走
    ``config.save_config``，桌面端走自己的保存链，写死一个会把另一端的
    落盘绕过去（只改内存 = 重启丢设置）。
    """
    v = bool(value)
    setattr(cfg, FIELD, v)
    from .classify.preference import set_learning_enabled
    set_learning_enabled(v)
    if save is not None:
        save()
    return v
