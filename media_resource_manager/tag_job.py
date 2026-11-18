# -*- coding: utf-8 -*-
"""V0.0.60 · 独立「AI 识别」作业 —— 与导入彻底分开、各管各的。

为什么单独成模块：
  用户要「导入是导入的进度条，识别是识别的进度条」「导入时能随时控制识别」。
  以前打标是 import_folder._run 里的一个阶段，进度混在导入作业里、
  停止也只能跟着导入一起停。现在识别是**独立作业**：
    · 有自己的开关（_TAG_LOCK）、自己的停止信号、自己的进度；
    · 能在导入进行中同时跑（边入库边识别）；
    · 也能导入完成后单独发起（补识别）。
  DB 全程走 Database 的 RLock，短事务，两个线程各写各的列（导入写登记、
  识别写标签），不会互相踩。

安全边界（跟 import_folder 一致，别破）：
  · 只动 images 表的 tags/score/ai_state 与向量列，不碰文件、不删任何图；
  · stop() 返回 True 时优雅停，已识别的保留，剩下的下次接着识别。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# 识别作业独占锁（与导入的 _RUNNING 分离 —— 两者可以同时跑）
_TAG_RUNNING = threading.Lock()


class TagStopped(Exception):
    """识别被请求停止（内部信号，转成 stats['stopped']）。"""


def is_tagging() -> bool:
    """当前是否有识别作业在跑（供接口/界面查状态）。"""
    got = _TAG_RUNNING.acquire(blocking=False)
    if not got:
        return True
    _TAG_RUNNING.release()
    return False


def start_tagging(db, cfg, *, task_id: Optional[int] = None,
                  stop: Optional[Callable[[], bool]] = None,
                  progress: Optional[Callable[..., None]] = None,
                  import_running: Optional[Callable[[], bool]] = None,
                  wait_idle_rounds: int = 3) -> dict:
    """给尚未识别的图片做 AI 识别。返回统计 dict。

    参数：
      task_id        只识别这个任务；None = 全库未识别的图。
      stop           返回 True 时优雅停止。
      progress       progress(phase, done, total, name, eta, pct)，phase 恒 'tag'。
      import_running 返回 True 表示还有导入在跑；识别捞不到行时会等它，
                     实现「边入库边识别」，避免导入没写完就提前结束。
      wait_idle_rounds 连续多少轮捞不到行才收工（每轮 sleep 1 秒）。
    """
    if not _TAG_RUNNING.acquire(blocking=False):
        return {"ok": False, "error": "已有 AI 识别作业在跑"}

    def _stopped() -> bool:
        try:
            return bool(stop and stop())
        except Exception:
            return False

    def _importing() -> bool:
        try:
            return bool(import_running and import_running())
        except Exception:
            return False

    def _prog(phase, done, total, name, eta, pct):
        if not progress:
            return
        try:
            progress("tag", done, total, name, eta, int(pct))
        except Exception:
            pass

    try:
        from . import import_folder as imp

        # 识别对象：指定任务 or 找任意一个有待识别图的任务。
        # V0.0.60：边入库边识别时，识别作业可能比导入的「第一批行」还早启动，
        # 此刻库里暂时没有待识别图 —— 不能像老逻辑那样立即返回"没有图片"，
        # 要在导入还跑着的时候等几轮，等它产出新行。
        tid = task_id
        if tid is None:
            waited = 0
            while True:
                row = db.query(
                    "SELECT task_id FROM images WHERE ai_state=0 "
                    "GROUP BY task_id ORDER BY task_id LIMIT 1")
                if row:
                    tid = int(row[0]["task_id"])
                    break
                if _stopped() or not _importing() or waited >= wait_idle_rounds:
                    return {"ok": True, "tagged": 0, "pending": 0,
                            "stopped": bool(_stopped()),
                            "note": "没有待识别的图片"}
                waited += 1
                time.sleep(1.0)
        from .repo import get_task
        task = get_task(db, int(tid))
        if task is None:
            return {"ok": False, "error": "任务不存在"}

        stats = {"ok": True, "task_id": int(tid), "tag_total": 0,
                 "tagged": 0, "pending": 0, "stopped": False, "ai_note": ""}

        # 复用 import_folder 的识别引擎（同一套模型/词表/阈值/向量），
        # 但给它一个「捞不到行时先等等」的钩子：_tag_stage 会在空批次时
        # 询问 should_wait()，导入还在跑就返回 True。
        stopped = imp._tag_stage(
            db, cfg, int(tid), task, stats, _stopped, _wrap_prog(_prog),
            tag_threads=0,
            should_wait=lambda: _importing() and not _stopped(),
            idle_rounds=wait_idle_rounds)

        left = db.query(
            "SELECT COUNT(*) c FROM images WHERE ai_state=0")
        stats["pending"] = int(left[0]["c"] or 0) if left else 0
        stats["stopped"] = bool(stopped)
        return stats
    finally:
        _TAG_RUNNING.release()


def _wrap_prog(report):
    """把 _tag_stage 的 6 参进度回调适配成带 pct 的形式。

    _tag_stage 内部调 _prog(phase, done, total, name, meter) 并自己算 pct，
    这里只透传：done/total 就是识别自己的口径（不是整体流程的 0-20%）。
    """
    def _cb(phase, done, total, name, meter):
        pct = int(done * 100 / total) if total else 100
        pct = max(0, min(100, pct))
        report(phase, done, total, name, "", pct)
    return _cb


def count_pending(db, task_id: Optional[int] = None) -> int:
    """待识别张数（前端显示用）。"""
    if task_id is None:
        rows = db.query("SELECT COUNT(*) c FROM images WHERE ai_state=0")
    else:
        rows = db.query("SELECT COUNT(*) c FROM images WHERE task_id=? AND ai_state=0",
                        (int(task_id),))
    return int(rows[0]["c"] or 0) if rows else 0
