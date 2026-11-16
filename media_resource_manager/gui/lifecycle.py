"""「立即、彻底退出」的唯一出口 `force_quit()`（R185）。

为什么必须**模块级函数**（同 `_tray_can_reside` 的理由）：
退出路径的测试用的是纯 Python 替身（不跑 `MainWindow.__init__`），方法挂在
`self` 上会 AttributeError，被 `except` 吞掉后就变成「既没杀子进程也没退出」
的假绿 —— R141 那次托盘退出就是这么漏掉的。

R185 用户原话：「退出不就是应该立即退出吗？」。原来的路径是
`request_quit()` → `close()`（四层安全收尾，预算 6s）→ `app.quit()`，
**最后一步交回解释器 finalization**：只要还有一条后台线程在跑 Python，
就不保证什么时候真退（R39d 甚至见过 finalization 期 0xC0000409 无声死亡）。
现在收尾照做，但收尾完**立刻 `os._exit(0)`**，并且先把子进程杀干净。

顺序不能反：
  ① 先掐子进程（gallery-dl 黑窗 = R184）——它们不退，退出就不叫「彻底」；
  ② 再发停止信号给队列 / 等后台 QThread（有界，共用一个截止时刻，
     沿用 R117 第 6 项的预算口径，绝不各等各的）；
  ③ 最后硬退。
"""
from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Optional

from .. import childproc

#: R185：从「点退出」到进程消失的硬预算（秒）。订单验收 ≤3s。
QUIT_BUDGET_S = 3.0


def _budget_left(deadline: float, default: float) -> float:
    try:
        return max(0.0, float(deadline - time.monotonic()))
    except Exception:
        return float(default)


def _stop_everything(obj: Any, budget: float) -> None:
    """给队列/引擎发停止信号 + 有界等待（缺什么都静默跳过）。"""
    deadline = time.monotonic() + max(0.0, float(budget))
    # ── 0）先停 Web 服务与订阅线程（2026-11-05 修复退出 AV 的第一步）──
    # 崩溃日志（gui-native.log）11 次访问违例里 6 次栈停在 os._exit 瞬间，
    # 崩溃时刻还活着的线程正是：webui serve_forever、webui _loop（growth
    # 清理线程）、subscribe._loop、scheduler asyncio 队列线程。Web 服务
    # stop() 自带 join(3s) 上限；订阅全局 watcher 停止后 _loop 线程退出；
    # 两样都停掉才轮得到下一步关库，否则关库后 Web 线程还在拿同一 db。
    try:
        srv = getattr(obj, "_web_server", None)
        if srv is not None and callable(getattr(srv, "stop", None)):
            srv.stop()
    except Exception:
        pass
    try:
        from ..subscribe import stop_global_watcher
        stop_global_watcher()
    except Exception:
        pass
    q = getattr(obj, "_queue", None)
    if q is not None:
        for meth in ("stop_all", "stop"):
            try:
                fn = getattr(q, meth, None)
                if callable(fn):
                    fn()
            except Exception:
                pass
        th = getattr(q, "_thread", None)
        try:
            if th is not None and th.is_alive():
                th.join(timeout=_budget_left(deadline, 1.0))
        except Exception:
            pass
    try:
        fn = getattr(obj, "_wait_bg_qthreads", None)
        if callable(fn):
            fn(budget=_budget_left(deadline, 0.8))
    except Exception:
        pass
    # ── 2.5）释放 onnxruntime 共享会话（2026-11-05）──
    # 崩溃日志另有 2 次 AV 线程直接停在 onnxruntime pybind run 里。进程级
    # _CLIP_CACHE 持有 InferenceSession（153MB 权重 + 原生线程池），若不在
    # os._exit 前丢弃引用，TerminateProcess 的 DLL_PROCESS_DETACH 会在其它
    # 线程仍在跑推理时拆除 CRT 堆。这里只丢引用（session 无显式 close API），
    # 让解释器先回收再硬退；失败不影响退出。
    try:
        from ..classify import clip_onnx as _co
        with _co._CLIP_CACHE_LOCK:
            _co._CLIP_CACHE.clear()
    except Exception:
        pass
    db = getattr(obj, "db", None)
    if db is not None:
        try:
            db.close()
        except Exception:
            pass


def force_quit(obj: Any = None, budget: float = QUIT_BUDGET_S,
               exit_fn: Optional[Callable[[int], Any]] = None,
               sweep: bool = True) -> None:
    """立即彻底退出：**不返回**（除非被调用方注入的 exit_fn 拦下）。

    参数：
      obj     MainWindow（或任何带 `_queue` / `_wait_bg_qthreads` / `db` 的对象）；
      budget  总预算（秒），各层共用同一个截止时刻；
      exit_fn 测试注入点（默认 `os._exit`）——生产路径不要传；
      sweep   是否按进程名兜底清扫残留（Windows）。

    ⚠ `os._exit` 跳过了 `atexit` 与缓冲区 flush，所以退出前自己 flush 一次
    （mrm_app.main 里早就是这么做的，这里保持同一口径）。
    """
    total = max(0.2, float(budget or QUIT_BUDGET_S))
    # ① 子进程先死（黑窗 + 残留下载器）
    try:
        childproc.kill_children(grace=min(0.5, total / 4.0))
    except Exception:
        pass
    # ② 队列 / 后台线程：有界
    try:
        _stop_everything(obj, total * 0.6)
    except Exception:
        pass
    # ③ 兜底清扫（登记漏网的 gallery-dl）
    if sweep:
        try:
            childproc.sweep_named(timeout=min(1.5, total / 2.0))
        except Exception:
            pass
    # ④ 硬退
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    fn = exit_fn or os._exit
    fn(0)


def hard_exit() -> None:
    """R185/R71：全产品唯一的 os._exit 出口（除 force_quit 自身）。

    main_window 的退出兜底（force_quit 自己炸了时）也必须走这里 ——
    test_r71 的「产品码不许硬掐」护栏把 os._exit 白名单收敛到本文件，
    别处再出现 `os._exit` 就是回归。
    """
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)
