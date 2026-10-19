from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.domain.engine import AlertAnalyticsEngine


@pytest.mark.parametrize(
    ("range_text", "quantity", "channel", "expected"),
    [
        ("±2g", "加速度", "x", (-19.6133, 19.6133)),
        ("0~200m", "位移", "", (0.0, 200.0)),
        ("土15°", "x轴_倾角", "x", (-15.0, 15.0)),
        ("-40℃～+119℃", "温度", "temperature", (-40.0, 119.0)),
        ("-50～+80℃/0～100%RH", "温度", "temperature", (-50.0, 80.0)),
        ("-50～+80℃/0～100%RH", "湿度", "humidity", (0.0, 100.0)),
        ("0～60m/s +0～360°", "风速", "windSpeed", (0.0, 60.0)),
        ("0～60m/s +0～360°", "风向", "windDirection", (0.0, 360.0)),
        (
            "风速: 0～60m/s，风向: 0～360°，温度: -50～80℃，湿度: 0-100%RH，降雨量：0-200mm/h",
            "降雨量",
            "rainfall",
            (0.0, 200.0),
        ),
    ],
)
def test_multirange_sensor_matches_physical_quantity(
    range_text: str,
    quantity: str,
    channel: str,
    expected: tuple[float, float],
) -> None:
    assert AlertAnalyticsEngine._parse_physical_range(
        range_text,
        "",
        quantity,
        channel,
    ) == expected


def test_stuck_duration_accumulates_across_windows() -> None:
    engine = AlertAnalyticsEngine.__new__(AlertAnalyticsEngine)
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    last_ts = first_ts + timedelta(seconds=60)

    active, since, duration_ms = engine._evaluate_stuck(
        previous_last_val=12.5,
        previous_stuck_since=first_ts - timedelta(seconds=60),
        sample_cnt=60,
        val_std=0.0,
        val_avg=12.5,
        first_ts=first_ts,
        last_ts=last_ts,
        stuck_time_ms=120000,
    )

    assert active is True
    assert since == first_ts - timedelta(seconds=60)
    assert duration_ms == 120000.0


def test_changed_value_resets_stuck_duration() -> None:
    engine = AlertAnalyticsEngine.__new__(AlertAnalyticsEngine)
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    last_ts = first_ts + timedelta(seconds=60)

    active, since, duration_ms = engine._evaluate_stuck(
        previous_last_val=11.0,
        previous_stuck_since=first_ts - timedelta(seconds=60),
        sample_cnt=60,
        val_std=0.0,
        val_avg=12.5,
        first_ts=first_ts,
        last_ts=last_ts,
        stuck_time_ms=30000,
    )

    assert active is True
    assert since == first_ts
    assert duration_ms == 60000.0
