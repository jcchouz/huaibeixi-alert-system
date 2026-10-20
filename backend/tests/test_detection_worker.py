from __future__ import annotations

from datetime import datetime

from app.core.config import Settings
from app.services.detection import AlertDetectionWorker


class FakeEngine:
    def __init__(self) -> None:
        self.windows: list[tuple[datetime, datetime]] = []
        self.period_types: list[str] = []

    def step_detection(
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
            "triggeredAlertsCount": 1,
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
        detection_interval_seconds=5,
        detection_lag_seconds=5,
        detection_window_minutes=window_minutes,
        detection_max_catchup_windows=3,
    )
    return AlertDetectionWorker(engine, settings)  # type: ignore[arg-type]


def test_detection_worker_processes_latest_completed_minute(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)

    results = worker.run_once(datetime(2026, 10, 20, 15, 42, 30))

    assert len(results) == 1
    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 41), datetime(2026, 10, 20, 15, 42))
    ]
    assert engine.period_types == ["1m"]


def test_detection_worker_does_not_repeat_a_completed_window(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)

    worker.run_once(datetime(2026, 10, 20, 15, 42, 30))
    worker.run_once(datetime(2026, 10, 20, 15, 42, 45))

    assert len(engine.windows) == 1


def test_detection_worker_catches_up_a_bounded_number_of_windows(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    monkeypatch.setattr(worker, "_ensure_leader", lambda: True)

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

    worker.run_once(datetime(2026, 10, 20, 15, 42, 30))

    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 30), datetime(2026, 10, 20, 15, 40))
    ]
    assert engine.period_types == ["10m"]
