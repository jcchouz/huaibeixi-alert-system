from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

import pytest

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
        environment="development",
        detection_enabled=True,
        detection_interval_seconds=1,
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


def test_alert_scan_windows_are_delayed_by_one_interval_without_overlap() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))

    worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 32))
    worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 33))

    assert engine.alert_scans == [
        (datetime(2026, 10, 20, 15, 42, 30), datetime(2026, 10, 20, 15, 42, 31)),
        (datetime(2026, 10, 20, 15, 42, 31), datetime(2026, 10, 20, 15, 42, 32)),
    ]


def test_alert_scan_logs_the_database_scan_time_and_elapsed(caplog) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))

    with caplog.at_level(logging.INFO, logger="alert_analysis.detection"):
        worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 32))

    assert "scan_at=2026-10-20 15:42:32" in caplog.text
    assert "window_start=2026-10-20 15:42:30" in caplog.text
    assert "window_end=2026-10-20 15:42:31" in caplog.text
    assert "elapsed_ms=0.5" in caplog.text
    assert "succeeded=True" in caplog.text


def test_alert_scan_logs_elapsed_when_the_engine_fails(
    monkeypatch,
    caplog,
) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))

    def fail_scan(window_start: datetime, window_end: datetime) -> dict:
        raise RuntimeError("scan failed")

    monkeypatch.setattr(engine, "scan_realtime_alerts", fail_scan)

    with (
        caplog.at_level(logging.INFO, logger="alert_analysis.detection"),
        pytest.raises(RuntimeError, match="scan failed"),
    ):
        worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 32))

    assert "elapsed_ms=" in caplog.text
    assert "succeeded=False" in caplog.text
    assert worker._last_alert_scan_at == datetime(2026, 10, 20, 15, 42, 30)


def test_alert_scan_waits_for_the_previous_period_to_become_eligible() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42, 30))

    assert worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 30)) is None
    assert worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 31)) is None
    assert engine.alert_scans == []

    worker.scan_alerts_once(datetime(2026, 10, 20, 15, 42, 32))
    assert engine.alert_scans == [
        (datetime(2026, 10, 20, 15, 42, 30), datetime(2026, 10, 20, 15, 42, 31))
    ]


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
    assert result["backlogSeconds"] == 13.0
    assert worker._last_alert_scan_at == datetime(2026, 10, 20, 15, 42, 31)


def test_alert_scan_resumes_from_previous_backlog_cursor_without_gaps() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    started_at = datetime(2026, 10, 20, 15, 42, 30)
    worker.initialize(started_at)
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
    assert first is not None and first["backlogSeconds"] == 8.0
    assert second is not None and second["backlogSeconds"] == 7.0
    assert third is not None and third["backlogSeconds"] == 6.0


def test_alert_loop_compensates_for_wait_oversleep(
    monkeypatch,
) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    current_time = datetime(2026, 10, 20, 15, 42, 45)
    monotonic_values = iter((10.0, 10.0, 10.034, 11.014, 11.048))
    wait_calls: list[float] = []

    monkeypatch.setattr(worker, "_current_time", lambda: current_time)
    monkeypatch.setattr(
        worker,
        "scan_alerts_once",
        lambda scan_time: {},
    )
    monkeypatch.setattr(
        "app.services.detection.time.monotonic",
        lambda: next(monotonic_values),
    )

    def wait_with_oversleep(wait_seconds: float) -> bool:
        wait_calls.append(wait_seconds)
        if len(wait_calls) == 2:
            worker._stop_event.set()
        return worker._stop_event.is_set()

    monkeypatch.setattr(worker._stop_event, "wait", wait_with_oversleep)
    worker._run_alert_loop()

    # 第一轮晚醒 14ms 后，第二轮等待主动扣除这部分误差。
    assert wait_calls == [pytest.approx(0.966), pytest.approx(0.952)]


def test_alert_status_does_not_report_the_intentional_scan_delay_as_backlog() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    started_at = datetime(2026, 10, 20, 15, 42, 30)
    worker.initialize(started_at)

    worker.scan_alerts_once(started_at + timedelta(seconds=2))

    status = worker.get_status()
    assert status["lastAlertScanAt"] == "2026-10-20 15:42:31"
    assert status["alertBacklogSeconds"] == 0.0


def test_statistics_start_with_first_complete_window_after_startup() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 40, 30))

    current_time = datetime(2026, 10, 20, 15, 42, 30)
    first = worker.process_statistics_once(current_time)
    second = worker.process_statistics_once(datetime(2026, 10, 20, 15, 42, 31))

    assert len(first) == 1
    assert second == []
    assert engine.alert_scans == []
    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 41), datetime(2026, 10, 20, 15, 42))
    ]
    assert engine.period_types == ["1m"]


def test_statistics_processes_all_due_windows_in_order() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 40, 30))

    results = worker.process_statistics_once(datetime(2026, 10, 20, 15, 45, 4))

    assert len(results) == 3
    assert engine.windows == [
        (datetime(2026, 10, 20, 15, 41), datetime(2026, 10, 20, 15, 42)),
        (datetime(2026, 10, 20, 15, 42), datetime(2026, 10, 20, 15, 43)),
        (datetime(2026, 10, 20, 15, 43), datetime(2026, 10, 20, 15, 44)),
    ]
    assert worker._last_window_end == datetime(2026, 10, 20, 15, 44)


def test_detection_worker_supports_a_configurable_window() -> None:
    engine = FakeEngine()
    worker = make_worker(engine, window_minutes=10)
    worker.initialize(datetime(2026, 10, 20, 15, 30, 30))

    current_time = datetime(2026, 10, 20, 15, 42, 30)
    results = worker.process_statistics_once(current_time)

    assert results == []
    assert engine.alert_scans == []
    assert worker._last_window_end == datetime(2026, 10, 20, 15, 40)
    assert engine.windows == []


def test_statistics_loop_waits_for_the_next_complete_window() -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42))

    assert worker._next_statistics_scan_at() == datetime(2026, 10, 20, 15, 43, 5)
    assert worker._statistics_wait_seconds(
        datetime(2026, 10, 20, 15, 42, 1)
    ) == 64.0

    worker.process_statistics_once(datetime(2026, 10, 20, 15, 43, 5))

    assert worker._next_statistics_scan_at() == datetime(2026, 10, 20, 15, 44, 5)


def test_statistics_loop_uses_a_monotonic_window_deadline(monkeypatch) -> None:
    engine = FakeEngine()
    worker = make_worker(engine)
    worker.initialize(datetime(2026, 10, 20, 15, 42))
    current_times = iter(
        (
            datetime(2026, 10, 20, 15, 43),
            datetime(2026, 10, 20, 15, 43, 5, 100000),
        )
    )
    # 每轮依次读取开始、数据库时间采样完成和处理结束三个单调时刻。
    monotonic_values = iter((10.0, 10.2, 10.8, 15.3, 15.5, 16.0))
    wait_calls: list[float] = []

    def process_statistics_once(current_time: datetime) -> list[dict]:
        if current_time >= datetime(2026, 10, 20, 15, 43, 5):
            worker._last_window_end = datetime(2026, 10, 20, 15, 43)
        return []

    monkeypatch.setattr(worker, "_current_time", lambda: next(current_times))
    monkeypatch.setattr(
        worker,
        "process_statistics_once",
        process_statistics_once,
    )
    monkeypatch.setattr(
        "app.services.detection.time.monotonic",
        lambda: next(monotonic_values),
    )

    def wait_with_oversleep(wait_seconds: float) -> bool:
        wait_calls.append(wait_seconds)
        if len(wait_calls) == 2:
            worker._stop_event.set()
        return worker._stop_event.is_set()

    monkeypatch.setattr(worker._stop_event, "wait", wait_with_oversleep)
    worker._run_statistics_loop()

    # 第一轮等待到 15.2；晚醒后下一窗口仍对齐 15:44:05。
    assert wait_calls == [pytest.approx(4.4), pytest.approx(59.4)]


def test_startup_on_window_boundary_uses_next_complete_window() -> None:
    engine = FakeEngine()
    worker = make_worker(engine, window_minutes=10)
    worker.initialize(datetime(2026, 10, 20, 15, 30))

    assert worker._last_window_end == datetime(2026, 10, 20, 15, 30)


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
