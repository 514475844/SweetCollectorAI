"""R241 · 重操作后台作业表（整库备份导出 / 恢复 / 诊断包用）。

为什么单独成一个模块（而不是塞在 ``webui.py`` 里）：

* ``webui.py`` 已经 6000+ 行，**作业表与 HTTP 无关** —— 它只是「起一个线程、
  把进度写进一个字典、让另一个请求能读到」。放独立模块才能被单独测。
* ``test_r157_guards`` / ``test_r161_guards`` 有一条**按源码字面量**钉住的护栏：
  ``webui.py`` 里不许再出现自动生成密码的调用（R157 裁决定了免密模式）。
  作业编号是**标识符不是凭证**，但它同样用随机数生成 —— 混在 webui 里会让
  那条护栏变成「靠人肉判断这次算不算例外」，迟早被人当成假红改掉。
  分成两层后，护栏继续扫 webui（保持原样、零豁免），作业编号的来源在这边
  有自己的一句话：它是 **id，不是口令**，不做密码用途、不外泄、不持久化。

⚠ 作业表只在内存里：进程重启后旧 id 一律查不到，前端会提示「请重新发起」。
   这是**刻意**的 —— 把「上次没跑完的作业」持久化下来，下次启动就有一堆
   半截状态要判断，收益远小于复杂度。
"""
from __future__ import annotations

import secrets
import threading
import time

#: 作业记录保留多久（秒）：留着是让前端回看结果，不是长期日志。
TTL = 3600.0

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def new(kind: str) -> str:
    """登记一个 running 状态的作业，返回 job_id。

    ⚠ 8 字节随机（64 位）而不是自增数字：自增 id 会被相邻请求猜到（例如
    用户 A 看到 12，就能读 13 的进度/结果）。这里不是权限校验，但「别人的
    备份文件名」也不该白送。
    """
    jid = secrets.token_hex(8)
    now = time.time()
    with _LOCK:
        for k in [k for k, v in _JOBS.items()
                  if now - float(v.get("finished_at") or v.get("started_at") or 0)
                  > TTL]:
            _JOBS.pop(k, None)
        _JOBS[jid] = {"id": jid, "kind": kind, "state": "running",
                      "pct": 0, "message": "准备中…", "result": None,
                      "error": "", "started_at": now, "finished_at": 0.0}
    return jid


def update(jid: str, **kw) -> None:
    with _LOCK:
        rec = _JOBS.get(jid)
        if rec is not None:
            rec.update(kw)


def get(jid: str):
    """取作业快照（拷贝，调用方随便改都不会污染内部状态）。"""
    with _LOCK:
        rec = _JOBS.get(str(jid or ""))
        return dict(rec) if rec else None


def progress_cb(jid: str):
    """给重活用的 ``progress(pct, msg)`` 闭包（屏蔽越界与倒退）。"""
    def _cb(pct, msg=""):
        try:
            p = max(0, min(100, int(pct)))
        except (TypeError, ValueError):
            p = 0
        with _LOCK:
            rec = _JOBS.get(jid)
            if rec is None:
                return
            # 百分比单调不减：多阶段任务里后一阶段的 0% 不该让进度条倒回去
            rec["pct"] = max(int(rec.get("pct") or 0), p)
            if msg:
                rec["message"] = str(msg)[:120]
    return _cb


def finish(jid: str, result=None, error: str = "", message: str = "") -> None:
    with _LOCK:
        rec = _JOBS.get(jid)
        if rec is None:
            return
        rec["state"] = "failed" if error else "done"
        rec["error"] = str(error or "")
        rec["result"] = result
        rec["pct"] = 100 if not error else int(rec.get("pct") or 0)
        rec["message"] = message or ("完成" if not error else "失败")
        rec["finished_at"] = time.time()


def clear() -> None:
    """清空（测试用；也方便以后加「清掉历史作业」的按钮）。"""
    with _LOCK:
        _JOBS.clear()
