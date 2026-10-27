from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.domain.alert_rules import (
    OutOfRangeAlertRule,
    StuckAlertRule,
    ThresholdAlertRule,
)
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


def test_offline_timeout_uses_at_least_five_sample_periods() -> None:
    engine = AlertAnalyticsEngine()
    assert engine._parse_frequency_hz("1/60") == pytest.approx(1 / 60)
    assert engine._offline_timeout_ms(
        {"frequency": "1/60", "offline_seconds": 100}
    ) == pytest.approx(300_000)
    assert engine._offline_timeout_ms({"frequency": "100"}) == pytest.approx(5_000)
    assert engine._offline_timeout_ms(
        {"frequency": "1/60", "offline_timeout_ms": 5_000}
    ) == pytest.approx(300_000)


@pytest.mark.parametrize(
    ("frequency_hz", "expected_ms"),
    [
        (1.0, 5_000),
        (50.0, 5_000),
        (1 / 60, 300_000),
    ],
)
def test_offline_timeout_scales_to_five_periods_for_low_frequency(
    frequency_hz: float,
    expected_ms: float,
) -> None:
    engine = AlertAnalyticsEngine()

    assert engine._offline_timeout_ms(
        {
            "frequency_hz": frequency_hz,
            "offline_seconds": 5,
        }
    ) == pytest.approx(expected_ms)


def test_threshold_confirmation_uses_sample_count_not_elapsed_time() -> None:
    rule = {
        "frequency_hz": 1.0,
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
    }
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(milliseconds=index), 7.0)
        for index in range(4)
    ]

    count, started_at, active, snapshot = (
        ThresholdAlertRule.evaluate_streak(
            samples,
            rule,
            previous_count=0,
            target_count=3,
        )
    )

    assert count == 4
    assert started_at == first_ts
    assert active is True
    assert snapshot is not None


@pytest.mark.parametrize(
    ("frequency_hz", "expected"),
    [
        (1.0, 3),
        (50.0, 150),
        (1 / 60, 3),
    ],
)
def test_threshold_required_sample_count_uses_sensor_frequency(
    frequency_hz: float,
    expected: int,
) -> None:
    assert ThresholdAlertRule.required_sample_count(
        frequency_hz,
        reference_seconds=3,
    ) == expected


def test_threshold_at_50_hz_requires_150_consecutive_samples() -> None:
    rule = {
        "frequency_hz": 50.0,
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
    }
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(seconds=index / 50.0), 7.0)
        for index in range(150)
    ]
    target_count = ThresholdAlertRule.required_sample_count(
        rule["frequency_hz"],
        3,
    )

    count, _, active, _ = ThresholdAlertRule.evaluate_streak(
        samples[:149],
        rule,
        previous_count=0,
        target_count=target_count,
    )
    assert count == 149
    assert active is False

    count, _, active, snapshot = ThresholdAlertRule.evaluate_streak(
        samples,
        rule,
        previous_count=0,
        target_count=target_count,
    )
    assert count == 150
    assert active is True
    assert snapshot is not None


def test_stuck_confirmation_uses_sample_count_not_elapsed_time() -> None:
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(milliseconds=index), 12.5)
        for index in range(4)
    ]

    count, value, since, active, snapshot = StuckAlertRule.evaluate_streak(
        samples,
        previous_value=None,
        previous_count=0,
        previous_since=None,
        target_count=3,
    )

    assert count == 4
    assert value == 12.5
    assert since == first_ts
    assert active is True
    assert snapshot == (12.5, samples[-1][0])


def test_out_of_range_confirmation_uses_sample_count_not_elapsed_time() -> None:
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(milliseconds=index), 12.0)
        for index in range(4)
    ]

    count, started_at, active, snapshot = (
        OutOfRangeAlertRule.evaluate_streak(
            samples,
            {"physical_min": -10.0, "physical_max": 10.0},
            previous_count=0,
            target_count=3,
        )
    )

    assert count == 4
    assert started_at == first_ts
    assert active is True
    assert snapshot is not None


@pytest.mark.parametrize(
    ("reference_seconds", "frequency_hz", "expected"),
    [
        (3, 1.0, 3),
        (3, 50.0, 150),
        (3, 1 / 60, 3),
        (100, 1.0, 100),
        (100, 50.0, 5000),
        (100, 1 / 60, 3),
    ],
)
def test_confirm_sample_count_uses_rule_frequency(
    reference_seconds: float,
    frequency_hz: float,
    expected: int,
) -> None:
    assert ThresholdAlertRule.required_sample_count(
        frequency_hz,
        reference_seconds=reference_seconds,
    ) == expected


def test_threshold_streak_continues_across_windows() -> None:
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

    count, started_at, active, snapshot = ThresholdAlertRule.evaluate_streak(
        samples,
        rule,
        previous_count=0,
        target_count=3,
    )

    assert count == 1
    assert active is False
    assert snapshot is None

    count, started_at, active, snapshot = ThresholdAlertRule.evaluate_streak(
        [
            (first_ts + timedelta(seconds=3), 7.1),
            (first_ts + timedelta(seconds=5), 7.2),
        ],
        rule,
        previous_count=count,
        target_count=3,
        previous_started_at=started_at,
    )

    assert count == 3
    assert active is True
    assert snapshot is not None
    assert snapshot[0] == 3


def test_stuck_streak_requires_configured_sample_count() -> None:
    first_ts = datetime(2026, 10, 19, 10, 0, 0)
    samples = [
        (first_ts + timedelta(seconds=index), 12.5)
        for index in range(4)
    ]

    count, value, since, active, snapshot = StuckAlertRule.evaluate_streak(
        samples,
        previous_value=None,
        previous_count=0,
        previous_since=None,
        target_count=3,
    )

    assert count == 4
    assert value == 12.5
    assert since == first_ts
    assert active is True
    assert snapshot == (12.5, samples[-1][0])


def test_missing_sample_groups_break_threshold_streak() -> None:
    rule = {
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
    }
    previous_ts = datetime(2026, 10, 19, 10, 0, 0)

    count, started_at, active, snapshot = ThresholdAlertRule.evaluate_streak(
        [(previous_ts + timedelta(seconds=2), 7.0)],
        rule,
        previous_count=2,
        target_count=3,
        previous_sample_time=previous_ts,
        max_gap_ms=1500,
    )

    assert count == 1
    assert active is False
    assert snapshot is None
