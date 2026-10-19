from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from app.core.config import Settings
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import acquire_replay_leader_lock
from app.infrastructure.sse import SSEHub

logger = logging.getLogger("huaibeixi.replay")


class ReplayWorker:
    def __init__(
        self,
        engine: AlertAnalyticsEngine,
        sse: SSEHub,
        settings: Settings,
    ):
        self.engine = engine
        self.sse = sse
        self.settings = settings
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self.settings.replay_worker_enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="replay-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _run(self) -> None:
        if self.settings.replay_leader_lock_enabled:
            with acquire_replay_leader_lock(self.settings.replay_leader_lock_key) as acquired:
                if not acquired:
                    logger.info("Replay worker skipped because another instance holds the leader lock")
                    return
                logger.info("Replay worker acquired leader lock")
                await self._run_loop()
            return
        await self._run_loop()

    async def _run_loop(self) -> None:
        start_watermark = datetime.strptime(
            self.settings.replay_start_watermark,
            "%Y-%m-%d %H:%M:%S",
        )
        while True:
            try:
                if self.engine.is_running:
                    step_minutes = max(1, int(self.engine.speed_multiplier))
                    window_start = self.engine.current_watermark
                    window_end = window_start + timedelta(minutes=step_minutes)
                    if window_end > self.engine.latest_data_ts:
                        window_start = start_watermark
                        window_end = window_start + timedelta(minutes=step_minutes)
                    result = await asyncio.to_thread(
                        self.engine.step_detection,
                        window_start,
                        window_end,
                    )
                    self.sse.publish_threadsafe({"type": "ENGINE_TICK", "data": result})
                    await asyncio.sleep(max(1.0, 3.0 / max(1, self.engine.speed_multiplier)))
                else:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Replay worker iteration failed")
                await asyncio.sleep(2.0)
