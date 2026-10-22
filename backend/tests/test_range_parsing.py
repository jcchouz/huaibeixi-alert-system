from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.domain.alert_types import ALERT_DIRECTION_NONE, ALERT_DIRECTION_UPPER
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


def test_frequency_fraction_controls_offline_group_duration() -> None:
    engine = AlertAnalyticsEngine()
    assert engine._parse_frequency_hz("1/60") == pytest.approx(1 / 60)
    assert engine._offline_timeout_ms(
        {"frequency": "1/60", "offline_points": 5}
    ) == pytest.approx(300_000)
    assert engine._offline_timeout_ms(
        {"frequency": "100", "offline_points": 5}
    ) == pytest.approx(50)
    assert engine._offline_timeout_ms(
        {
            "condition_unit": "seconds",
            "offline_seconds": 100,
            "sample_interval_ms": 10,
            "offline_points": 5,
        }
    ) == pytest.approx(100_000)


def test_threshold_seconds_uses_sample_timestamps() -> None:
    engine = AlertAnalyticsEngine()
    rule = {
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
    }
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(seconds=index), 7.0)
        for index in range(4)
    ]

    direction, count, started_at, active, snapshot = (
        engine._evaluate_threshold_streak(
            samples,
            rule,
            previous_direction=ALERT_DIRECTION_NONE,
            previous_count=0,
            target_points=3,
            target_seconds=3,
        )
    )

    assert direction == ALERT_DIRECTION_UPPER
    assert count == 4
    assert started_at == first_ts
    assert active is True
    assert snapshot is not None


def test_points_and_seconds_condition_units_use_different_thresholds() -> None:
    engine = AlertAnalyticsEngine()
    rule = {
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
    }
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    three_samples = [
        (first_ts + timedelta(seconds=index), 7.0)
        for index in range(3)
    ]

    _, _, _, points_active, _ = engine._evaluate_threshold_streak(
        three_samples,
        rule,
        previous_direction=ALERT_DIRECTION_NONE,
        previous_count=0,
        target_points=3,
    )
    (
        direction,
        count,
        started_at,
        seconds_active,
        _,
    ) = engine._evaluate_threshold_streak(
        three_samples,
        rule,
        previous_direction=ALERT_DIRECTION_NONE,
        previous_count=0,
        target_points=3,
        target_seconds=3,
    )

    assert points_active is True
    assert seconds_active is False

    _, _, _, seconds_active, _ = engine._evaluate_threshold_streak(
        [(first_ts + timedelta(seconds=3), 7.0)],
        rule,
        previous_direction=direction,
        previous_count=count,
        target_points=3,
        target_seconds=3,
        previous_started_at=started_at,
        previous_sample_time=three_samples[-1][0],
    )

    assert seconds_active is True


def test_stuck_seconds_uses_elapsed_duration() -> None:
    engine = AlertAnalyticsEngine()
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(seconds=index), 12.5)
        for index in range(4)
    ]

    count, value, since, active, snapshot = engine._evaluate_stuck_streak(
        samples,
        previous_value=None,
        previous_count=0,
        previous_since=None,
        target_points=100,
        target_seconds=3,
    )

    assert count == 4
    assert value == 12.5
    assert since == first_ts
    assert active is True
    assert snapshot == (12.5, samples[-1][0])


def test_out_of_range_seconds_uses_elapsed_duration() -> None:
    engine = AlertAnalyticsEngine()
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(seconds=index), 12.0)
        for index in range(4)
    ]

    direction, count, started_at, active, snapshot = (
        engine._evaluate_out_of_range_streak(
            samples,
            {"physical_min": -10.0, "physical_max": 10.0},
            previous_direction=ALERT_DIRECTION_NONE,
            previous_count=0,
            target_points=3,
            target_seconds=3,
        )
    )

    assert direction == ALERT_DIRECTION_UPPER
    assert count == 4
    assert started_at == first_ts
    assert active is True
    assert snapshot is not None


def test_threshold_streak_continues_across_windows() -> None:
    engine = AlertAnalyticsEngine()
    rule = {
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
    }
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts, 5.0),
        (first_ts + timedelta(seconds=1), 5.0),
        (first_ts + timedelta(seconds=2), 7.0),
    ]

    direction, count, started_at, active, snapshot = engine._evaluate_threshold_streak(
        samples,
        rule,
        previous_direction=ALERT_DIRECTION_NONE,
        previous_count=0,
        target_points=3,
    )

    assert direction == ALERT_DIRECTION_UPPER
    assert count == 1
    assert active is False
    assert snapshot is None

    direction, count, started_at, active, snapshot = engine._evaluate_threshold_streak(
        [
            (first_ts + timedelta(seconds=3), 7.1),
            (first_ts + timedelta(seconds=4), 7.2),
        ],
        rule,
        previous_direction=direction,
        previous_count=count,
        target_points=3,
        previous_started_at=started_at,
    )

    assert direction == ALERT_DIRECTION_UPPER
    assert count == 3
    assert active is True
    assert snapshot is not None
    assert snapshot[0] == 3


def test_stuck_streak_requires_configured_equal_samples() -> None:
    engine = AlertAnalyticsEngine()
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(seconds=index), 12.5)
        for index in range(100)
    ]

    count, value, since, active, snapshot = engine._evaluate_stuck_streak(
        samples,
        previous_value=None,
        previous_count=0,
        previous_since=None,
        target_points=100,
    )

    assert count == 100
    assert value == 12.5
    assert since == first_ts
    assert active is True
    assert snapshot == (12.5, samples[-1][0])


def test_missing_sample_groups_break_threshold_streak() -> None:
    engine = AlertAnalyticsEngine()
    rule = {
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
    }
    previous_ts = datetime(2026, 10, 19, 10, 0, 0)

    direction, count, started_at, active, snapshot = engine._evaluate_threshold_streak(
        [(previous_ts + timedelta(seconds=2), 7.0)],
        rule,
        previous_direction=ALERT_DIRECTION_UPPER,
        previous_count=2,
        target_points=3,
        previous_sample_time=previous_ts,
        max_gap_ms=1500,
    )

    assert direction == ALERT_DIRECTION_UPPER
    assert count == 1
    assert active is False
    assert snapshot is None
