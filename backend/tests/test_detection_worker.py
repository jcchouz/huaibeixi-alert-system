from __future__ import annotations

import threading
import time
from datetime import datetime

from app.core.config import Settings
from app.services.detection import AlertDetectionWorker


class FakeEngine:
    def __init__(self) -> None:
        self.alert_scans: list[tuple[datetime, datetime]] = []
        self.windows: list[tuple[datetime, datetime]] = []
        self.period_types: list[str] = []

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
        detection_max_catchup_windows=3,
    )
    return AlertDetectionWorker(engine, settings)  # type: ignore[arg-type]


def test_detection_worker_processes_latest_completed_minute(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)

    results = worker.run_once(datetime(2026, 10, 20, 15, 42, 30))

    assert len(results) == 1
    assert engine.alert_scans == [
        (datetime(2026, 10, 20, 15, 42, 29), datetime(2026, 10, 20, 15, 42, 30))
    ]
    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 41), datetime(2026, 10, 20, 15, 42))
    ]
    assert engine.period_types == ["1m"]


def test_detection_worker_chunks_alert_catchup(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)

    worker.run_once(datetime(2026, 10, 20, 15, 42, 30))
    worker.run_once(datetime(2026, 10, 20, 15, 42, 45))

    assert len(engine.windows) == 1
    assert engine.alert_scans == [
        (datetime(2026, 10, 20, 15, 42, 29), datetime(2026, 10, 20, 15, 42, 30)),
        (datetime(2026, 10, 20, 15, 42, 29), datetime(2026, 10, 20, 15, 42, 35)),
        (datetime(2026, 10, 20, 15, 42, 34), datetime(2026, 10, 20, 15, 42, 40)),
        (datetime(2026, 10, 20, 15, 42, 39), datetime(2026, 10, 20, 15, 42, 45)),
    ]


def test_alert_scan_persists_watermark_after_each_catchup_chunk(
    monkeypatch,
) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)
    persisted: list[tuple[str, datetime, datetime]] = []
    monkeypatch.setattr(
        worker,
        "_persist_worker_watermark",
        lambda name, processed, completed: persisted.append(
            (name, processed, completed)
        ),
    )

    worker.run_once(datetime(2026, 10, 20, 15, 42, 30))
    worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 45))

    assert [item[1] for item in persisted if item[0] == "alert"] == [
        datetime(2026, 10, 20, 15, 42, 30),
        datetime(2026, 10, 20, 15, 42, 35),
        datetime(2026, 10, 20, 15, 42, 40),
        datetime(2026, 10, 20, 15, 42, 45),
    ]
    assert sum(item[0] == "alert" for item in persisted) == 4
    assert sum(item[0] == "statistics" for item in persisted) == 1


def test_detection_worker_catches_up_a_bounded_number_of_windows(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)

    worker.run_once(datetime(2026, 10, 20, 15, 40, 30))
    worker.run_once(datetime(2026, 10, 20, 15, 46, 30))

    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 39), datetime(2026, 10, 20, 15, 40)),
        (datetime(2026, 10, 20, 15, 43), datetime(2026, 10, 20, 15, 44)),
        (datetime(2026, 10, 20, 15, 44), datetime(2026, 10, 20, 15, 45)),
        (datetime(2026, 10, 20, 15, 45), datetime(2026, 10, 20, 15, 46)),
    ]


def test_detection_worker_supports_a_configurable_window(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine, window_minutes=10)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)

    worker.run_once(datetime(2026, 10, 20, 15, 42, 30))

    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 30), datetime(2026, 10, 20, 15, 40))
    ]
    assert engine.period_types == ["10m"]


def test_alerts_are_scanned_even_when_no_stat_window_is_due(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)

    worker.run_once(datetime(2026, 10, 20, 15, 42, 30))
    worker.run_once(datetime(2026, 10, 20, 15, 42, 31))

    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 41), datetime(2026, 10, 20, 15, 42))
    ]
    assert engine.alert_scans[-1] == (
        datetime(2026, 10, 20, 15, 42, 29),
        datetime(2026, 10, 20, 15, 42, 31),
    )


def test_alert_scan_windows_reuse_a_short_overlap(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)

    assert worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 30)) is not None
    assert worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 31)) is not None

    assert engine.alert_scans == [
        (datetime(2026, 10, 20, 15, 42, 29), datetime(2026, 10, 20, 15, 42, 30)),
        (datetime(2026, 10, 20, 15, 42, 29), datetime(2026, 10, 20, 15, 42, 31)),
    ]


def test_run_once_scans_alerts_without_waiting_for_statistics_leadership(
    monkeypatch,
) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: False)

    results = worker.run_once(datetime(2026, 10, 20, 15, 42, 30))

    assert results == []
    assert len(engine.alert_scans) == 1
    assert engine.windows == []


def test_run_once_processes_statistics_without_alert_leadership(
    monkeypatch,
) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: False)
    monkeypatch.setattr(worker, "_ensure_statistics_leader", lambda: True)

    results = worker.run_once(datetime(2026, 10, 20, 15, 42, 30))

    assert len(results) == 1
    assert engine.alert_scans == []
    assert len(engine.windows) == 1


def test_alert_scans_are_serialized_before_advancing_watermark(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
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
        args=(datetime(2026, 10, 20, 15, 42, 30),),
    )
    second = threading.Thread(
        target=worker.scan_alerts_once,
        args=(datetime(2026, 10, 20, 15, 42, 31),),
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


def test_worker_starts_separate_realtime_and_statistics_threads(
    monkeypatch,
) -> None:
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
