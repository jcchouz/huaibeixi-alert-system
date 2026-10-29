from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import app.services.statistics as statistics_module
from app.services.statistics import StatisticsService


class FakeStatisticsRepository:
    def __init__(self) -> None:
        self.requested_periods: list[str] = []

    def extremes_rows(self, cur: Any, **kwargs: Any) -> list[dict[str, Any]]:
        self.requested_periods.append(str(kwargs["period_type"]))
        return []

    def extremes_summary(self, cur: Any, **kwargs: Any) -> dict[str, Any]:
        self.requested_periods.append(str(kwargs["period_type"]))
        return {
            "window_max": None,
            "window_max_time": None,
            "window_min": None,
            "window_min_time": None,
            "window_avg": None,
            "total_samples": 0,
            "bucket_count": 0,
        }


class FakeEngine:
    channel_states: dict[str, dict[str, Any]] = {}
    sensor_meta: dict[str, dict[str, Any]] = {}
    station_name = "test"

    @staticmethod
    def make_key(sensor_no: str, channel: str) -> str:
        return f"{sensor_no}:{channel}"

    @staticmethod
    def _infer_unit(physical_quantity: str, channel: str) -> str:
        return ""


def test_statistics_service_honors_requested_period(monkeypatch) -> None:
    repository = FakeStatisticsRepository()
    service = StatisticsService(repository, FakeEngine())  # type: ignore[arg-type]

    @contextmanager
    def fake_get_db_cursor():
        yield object()

    monkeypatch.setattr(
        statistics_module,
        "get_db_cursor",
        fake_get_db_cursor,
    )

    result = service.extremes(
        sensor_no="sensor-1",
        channel="x",
        period_type="10M",
        start_time=None,
        end_time=None,
        limit=100,
    )

    assert repository.requested_periods == ["10m", "10m"]
    assert result["periodType"] == "10m"
