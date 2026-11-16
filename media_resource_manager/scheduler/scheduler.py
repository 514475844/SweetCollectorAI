"""Task scheduler: periodic / idle auto-run for tasks flagged auto_run.

Runs locally. It does NOT circumvent any access control; each run still passes
through the compliance gate and robots checks. Interval is configurable.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from ..config import AppConfig
from ..db import Database
from ..engine import Engine
from ..repo import list_tasks
from ..models import TaskStatus


class Scheduler:
    def __init__(self, cfg: AppConfig, db: Database, interval: int = 3600):
        self.cfg = cfg
        self.db = db
        self.interval = interval
        self._stop = False

    def stop(self):
        self._stop = True

    async def _tick(self):
        eng = Engine(self.cfg, self.db)
        for t in list_tasks(self.db):
            if not t.auto_run or self._stop:
                continue
            if t.status == TaskStatus.RUNNING:
                continue
            try:
                rep = await eng.run(t.id)
                print(f"[scheduler] task {t.id} done: dl={rep.downloaded} "
                      f"dup={rep.dedup} bl={rep.blacklist_hit}")
            except Exception as e:
                self.db.audit("scheduler", "error", str(t.id), str(e)[:200])
                print(f"[scheduler] task {t.id} error: {e}")

    async def loop(self):
        while not self._stop:
            await self._tick()
            # wait in small steps so stop() is responsive
            for _ in range(self.interval):
                if self._stop:
                    break
                await asyncio.sleep(1)

    def start_blocking(self):
        asyncio.run(self.loop())
