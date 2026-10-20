from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import psycopg2

from app.core.config import Settings
from app.core.serialization import serialize_val
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor

logger = logging.getLogger("huaibeixi.detection")

DETECTION_ADVISORY_LOCK_KEY = 0x48425844


class AlertDetectionWorker:
    """Continuously turn t_monitor windows into statistics and alerts."""

    def __init__(
        self,
        engine: AlertAnalyticsEngine,
        settings: Settings,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.interval_seconds = settings.detection_interval_seconds
        self.lag_seconds = settings.detection_lag_seconds
        self.window_minutes = settings.detection_window_minutes
        self.max_catchup_windows = settings.detection_max_catchup_windows

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock_connection: Optional[Any] = None
        self._state_lock = threading.Lock()
        self._leader = False
        self._running = False
        self._last_window_end: Optional[datetime] = None
        self._last_run_at: Optional[datetime] = None
        self._last_completed_at: Optional[datetime] = None
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._cycles = 0

    def start(self) -> None:
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                name="alert-detection-worker",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "Alert detection worker started "
            "(window=%sm, interval=%ss, lag=%ss, catchup=%s).",
            self.window_minutes,
            self.interval_seconds,
            self.lag_seconds,
            self.max_catchup_windows,
        )

    def stop(self) -> None:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=30)
        self._release_leader_lock()
        with self._state_lock:
            self._running = False
            self._leader = False
            self._thread = None
        logger.info("Alert detection worker stopped.")

    def run_once(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Run all currently eligible average windows once."""
        if not self._ensure_leader():
            return []

        current_time = now or self._current_time()
        with self._state_lock:
            self._last_run_at = current_time

        eligible_end = self.latest_eligible_window_end(current_time)
        with self._state_lock:
            last_window_end = self._last_window_end

        if last_window_end is not None and eligible_end <= last_window_end:
            return []

        if last_window_end is None:
            window_ends = [eligible_end]
        else:
            first_end = last_window_end + timedelta(minutes=self.window_minutes)
            earliest_end = eligible_end - timedelta(
                minutes=max(0, self.max_catchup_windows - 1) * self.window_minutes
            )
            first_end = max(first_end, earliest_end)
            if first_end > eligible_end:
                return []
            count = (
                int(
                    (eligible_end - first_end).total_seconds()
                    // (self.window_minutes * 60)
                )
                + 1
            )
            window_ends = [
                first_end + timedelta(minutes=index * self.window_minutes)
                for index in range(count)
            ]

        results: List[Dict[str, Any]] = []
        for window_end in window_ends:
            window_start = window_end - timedelta(minutes=self.window_minutes)
            result = self.engine.step_detection(
                window_start,
                window_end,
                period_type=f"{self.window_minutes}m",
            )
            results.append(result)
            with self._state_lock:
                self._last_window_end = window_end
                self._last_result = {
                    "windowStart": serialize_val(window_start),
                    "windowEnd": serialize_val(window_end),
                    "rawPointsProcessed": int(result.get("rawPointsProcessed") or 0),
                    "upsertedStatsCount": int(result.get("upsertedStatsCount") or 0),
                    "triggeredAlertsCount": int(result.get("triggeredAlertsCount") or 0),
                    "elapsedMs": result.get("elapsedMs"),
                }
                self._last_completed_at = current_time
                self._last_error = None
                self._cycles += 1

        return results

    def latest_eligible_window_end(self, now: datetime) -> datetime:
        cutoff = now - timedelta(seconds=self.lag_seconds)
        day_start = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_minutes = int((cutoff - day_start).total_seconds() // 60)
        block_start = day_start + timedelta(
            minutes=(elapsed_minutes // self.window_minutes) * self.window_minutes
        )
        return block_start

    def _current_time(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        with get_db_cursor() as cur:
            cur.execute("SELECT LOCALTIMESTAMP AS now")
            row = cur.fetchone()
        return row["now"]

    def get_status(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "enabled": bool(
                    self.settings.detection_enabled
                    and self.settings.environment != "test"
                ),
                "running": self._running,
                "leader": self._leader,
                "intervalSeconds": self.interval_seconds,
                "lagSeconds": self.lag_seconds,
                "windowMinutes": self.window_minutes,
                "detectionMode": "frequency_sample_groups",
                "confirmPoints": self.settings.detection_confirm_points,
                "stuckPoints": self.settings.detection_stuck_points,
                "offlinePoints": self.settings.detection_offline_points,
                "maxCatchupWindows": self.max_catchup_windows,
                "cycles": self._cycles,
                "lastRunAt": serialize_val(self._last_run_at),
                "lastCompletedAt": serialize_val(self._last_completed_at),
                "lastWindowEnd": serialize_val(self._last_window_end),
                "lastResult": dict(self._last_result) if self._last_result else None,
                "lastError": self._last_error,
            }

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Alert detection cycle failed")
                with self._state_lock:
                    self._last_error = str(exc)
            elapsed = time.monotonic() - started
            wait_seconds = max(0.1, self.interval_seconds - elapsed)
            self._stop_event.wait(wait_seconds)

    def _ensure_leader(self) -> bool:
        if (
            self._lock_connection is not None
            and not self._lock_connection.closed
            and self._leader
        ):
            return True

        try:
            if self._lock_connection is None or self._lock_connection.closed:
                self._lock_connection = psycopg2.connect(self.settings.dsn)
                self._lock_connection.autocommit = True
            with self._lock_connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS locked",
                    (DETECTION_ADVISORY_LOCK_KEY,),
                )
                row = cur.fetchone()
            is_leader = bool(row and row[0])
            if is_leader != self._leader:
                logger.info(
                    "Alert detection worker %s leader lock.",
                    "acquired" if is_leader else "is waiting for",
                )
            self._leader = is_leader
            return is_leader
        except Exception as exc:
            logger.warning("Could not acquire alert detection leader lock: %s", exc)
            self._close_lock_connection()
            self._leader = False
            return False

    def _release_leader_lock(self) -> None:
        if self._lock_connection is None:
            return
        try:
            if not self._lock_connection.closed:
                if self._leader:
                    with self._lock_connection.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_unlock(%s)",
                            (DETECTION_ADVISORY_LOCK_KEY,),
                        )
        except Exception:
            logger.debug("Could not release alert detection leader lock", exc_info=True)
        finally:
            self._close_lock_connection()

    def _close_lock_connection(self) -> None:
        if self._lock_connection is not None:
            try:
                self._lock_connection.close()
            except Exception:
                pass
        self._lock_connection = None
