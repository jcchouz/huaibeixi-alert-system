from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

from app.core.config import Settings
from app.services.detection import AlertDetectionWorker


class FakeEngine:
    def __init__(self) -> None:
        self.sessions: list[datetime] = []
        self.alert_scans: list[tuple[datetime, datetime]] = []
        self.windows: list[tuple[datetime, datetime]] = []
        self.period_types: list[str] = []

    def begin_detection_session(self, started_at: datetime) -> None:
        self.sessions.append(started_at)

    def scan_realtime_alerts(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> dict:
        self.alert_scans.append((window_start, window_end))
        return {
            "rawPointsProcessed": 3,
            "triggeredAlertsCount": 1,
            "elapsedMs": 0.5,
        }

    def record_statistics_window(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str = "1m",
    ) -> dict:
        self.windows.append((window_start, window_end))
        self.period_types.append(period_type)
        return {
            "rawPointsProcessed": 10,
            "upsertedStatsCount": 2,
            "elapsedMs": 1.5,
        }


def make_worker(
    engine: FakeEngine,
    *,
    window_minutes: int = 1,
) -> AlertDetectionWorker:
    settings = Settings(
        environment="test",
        detection_enabled=True,
        detection_interval_seconds=1,
        detection_alert_overlap_seconds=1,
        detection_lag_seconds=5,
        detection_window_minutes=window_minutes,
    )
    return AlertDetectionWorker(engine, settings)  # type: ignore[arg-type]


def test_initialize_starts_from_current_time_without_historical_state() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    started_at = datetime(2026, 10, 20, 15, 42, 30)

    worker.initialize(started_at)

    assert engine.sessions == [started_at]
    assert worker._detection_started_at == started_at
    assert worker._last_alert_scan_at == started_at
    # 当前窗口包含启动前数据，因此统计从下一个完整窗口开始。
    assert worker._last_window_end == datetime(2026, 10, 20, 15, 43)


def test_alert_scan_never_reaches_before_startup_boundary() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))

    worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 31))
    worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 32))

    assert engine.alert_scans == [
        (datetime(2026, 10, 20, 15, 42, 30), datetime(2026, 10, 20, 15, 42, 31)),
        (datetime(2026, 10, 20, 15, 42, 30), datetime(2026, 10, 20, 15, 42, 32)),
    ]


def test_alert_scan_does_not_replay_data_before_start() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))

    assert worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 30)) is None
    assert engine.alert_scans == []


def test_alert_scan_processes_only_the_previous_one_second_window() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    started_at = datetime(2026, 10, 20, 15, 42, 30)
    worker.initialize(started_at)

    result = worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 45))

    assert engine.alert_scans == [
        (started_at, datetime(2026, 10, 20, 15, 42, 31))
    ]
    assert result is not None
    assert result["backlogSeconds"] == 14.0
    assert worker._last_alert_scan_at == datetime(2026, 10, 20, 15, 42, 31)


def test_alert_scan_resumes_from_previous_backlog_cursor_without_gaps() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    started_at = datetime(2026, 10, 20, 15, 42, 30)
    worker.initialize(started_at)
    worker.alert_overlap_seconds = 0
    current_time = started_at + timedelta(seconds=10)

    first = worker.scan_alerts_once(current_time)
    second = worker.scan_alerts_once(current_time)
    third = worker.scan_alerts_once(current_time)

    expected_starts = [
        started_at,
        started_at + timedelta(seconds=1),
        started_at + timedelta(seconds=2),
    ]
    assert engine.alert_scans == [
        (start, start + timedelta(seconds=1))
        for start in expected_starts
    ]
    assert worker._last_alert_scan_at == started_at + timedelta(seconds=3)
    assert first is not None and first["backlogSeconds"] == 9.0
    assert second is not None and second["backlogSeconds"] == 8.0
    assert third is not None and third["backlogSeconds"] == 7.0


def test_statistics_start_with_first_complete_window_after_startup() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 40, 30))

    first = worker.run_once(datetime(2026, 10, 20, 15, 42, 30))
    second = worker.run_once(datetime(2026, 10, 20, 15, 42, 31))

    assert len(first) == 1
    assert second == []
    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 41), datetime(2026, 10, 20, 15, 42))
    ]
    assert engine.period_types == ["1m"]


def test_detection_worker_supports_a_configurable_window() -> None:
    engine = FakeEngine()
    worker = make_worker(engine, window_minutes=10)
    worker.initialize(datetime(2026, 10, 20, 15, 30, 30))

    results = worker.run_once(datetime(2026, 10, 20, 15, 42, 30))

    assert results == []
    assert worker._last_window_end == datetime(2026, 10, 20, 15, 40)
    assert engine.windows == []


def test_startup_on_window_boundary_uses_next_complete_window() -> None:
    engine = FakeEngine()
    worker = make_worker(engine, window_minutes=10)
    worker.initialize(datetime(2026, 10, 20, 15, 30))

    assert worker._last_window_end == datetime(2026, 10, 20, 15, 30)


def test_alert_scan_windows_reuse_a_short_overlap() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))
    worker.alert_overlap_seconds = 0

    assert worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 31)) is not None
    assert worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 32)) is not None

    assert engine.alert_scans == [
        (datetime(2026, 10, 20, 15, 42, 30), datetime(2026, 10, 20, 15, 42, 31)),
        (datetime(2026, 10, 20, 15, 42, 31), datetime(2026, 10, 20, 15, 42, 32)),
    ]


def test_alert_scans_are_serialized_before_advancing_cursor(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))
    worker.alert_overlap_seconds = 0
    entered = threading.Event()
    release = threading.Event()
    original_scan = engine.scan_realtime_alerts
    active_scans = 0
    max_active_scans = 0

    def blocking_scan(window_start: datetime, window_end: datetime) -> dict:
        nonlocal active_scans, max_active_scans
        active_scans += 1
        max_active_scans = max(max_active_scans, active_scans)
        entered.set()
        try:
            assert release.wait(2)
            return original_scan(window_start, window_end)
        finally:
            active_scans -= 1

    monkeypatch.setattr(engine, "scan_realtime_alerts", blocking_scan)

    first = threading.Thread(
        target=worker.scan_alerts_once,
        args=(datetime(2026, 10, 20, 15, 42, 31),),
    )
    second = threading.Thread(
        target=worker.scan_alerts_once,
        args=(datetime(2026, 10, 20, 15, 42, 32),),
    )
    first.start()
    assert entered.wait(1)
    second.start()
    time.sleep(0.05)
    assert second.is_alive()

    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert max_active_scans == 1
    assert engine.alert_scans
    assert all(start < end for start, end in engine.alert_scans)


def test_worker_starts_separate_realtime_and_statistics_threads(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    entered = threading.Event()

    def wait_for_stop() -> None:
        entered.set()
        worker._stop_event.wait(1)

    monkeypatch.setattr(worker, "_run_alert_loop", wait_for_stop)
    monkeypatch.setattr(worker, "_run_statistics_loop", wait_for_stop)

    worker.start()
    assert entered.wait(1)
    assert worker._alert_thread is not None
    assert worker._statistics_thread is not None
    assert worker._alert_thread.name == "alert-realtime-worker"
    assert worker._statistics_thread.name == "alert-statistics-worker"
    worker.stop()
