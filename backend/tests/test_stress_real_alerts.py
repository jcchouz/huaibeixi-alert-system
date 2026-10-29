from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.alert_types import (
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)
from tools.stress_real_alerts import (
    floor_statistics_boundary,
    verify_health,
    verify_statistics,
)


class StatisticsCursor:
    def __init__(self) -> None:
        self.params: tuple[Any, ...] | None = None

    def __enter__(self) -> "StatisticsCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.params = params

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class StatisticsConnection:
    def __init__(self) -> None:
        self.statistics_cursor = StatisticsCursor()

    def cursor(self) -> StatisticsCursor:
        return self.statistics_cursor

    def rollback(self) -> None:
        return None


def test_verify_health_reads_all_statistics_windows() -> None:
    health = {
        "engine": {
            "stationCode": "YHZ",
            "alertTypeSwitches": {
                ALERT_TYPE_THRESHOLD: True,
                ALERT_TYPE_OFFLINE: True,
                ALERT_TYPE_DAQ_OFFLINE: True,
                ALERT_TYPE_STUCK: True,
                ALERT_TYPE_OOR: True,
            },
        },
        "detection": {
            "running": True,
            "windowMinutes": 1,
            "windowMinutesList": [10, 1],
        },
    }

    config = verify_health(health)

    assert config["windowMinutes"] == 1
    assert config["windowMinutesList"] == [1, 10]


def test_statistics_verification_uses_period_aligned_start() -> None:
    connection = StatisticsConnection()

    result = verify_statistics(
        connection,
        [],
        datetime(2026, 10, 29, 17, 23, 30),
        datetime(2026, 10, 29, 17, 24, 30),
        10,
        timeout=0.1,
    )

    assert result["passed"] is True
    assert connection.statistics_cursor.params is not None
    assert connection.statistics_cursor.params[0] == datetime(
        2026,
        10,
        29,
        17,
        20,
    )


def test_floor_statistics_boundary_aligns_to_natural_window() -> None:
    assert floor_statistics_boundary(
        datetime(2026, 10, 29, 17, 29, 59),
        10,
    ) == datetime(2026, 10, 29, 17, 20)
