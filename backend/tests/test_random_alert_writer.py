from __future__ import annotations

import random
from datetime import datetime

import pytest

from app.domain.alert_types import (
    ALERT_CATEGORY_DATA,
    ALERT_CATEGORY_DEVICE,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)
from tools.random_alert_writer import (
    RealThreshold,
    StatTarget,
    floor_window_start,
    generate_alert,
    generate_statistics_rows,
    out_of_range_value,
    parse_alert_types,
    threshold_triggers,
)


def make_rule() -> RealThreshold:
    return RealThreshold(
        threshold_id="threshold-1",
        sensor_no="accl-01",
        channel="x",
        instrument_id="daq-01",
        alert_function=1,
        physical_quantity="加速度",
        frequency="100Hz",
        level1_upper=2.0,
        level1_lower=-2.0,
        level2_upper=1.8,
        level2_lower=-1.8,
        level3_upper=1.6,
        level3_lower=-1.6,
        range_min=-19.6133,
        range_max=19.6133,
    )


def test_parse_alert_types_preserves_standard_order() -> None:
    parsed = parse_alert_types(f"{ALERT_TYPE_OOR},{ALERT_TYPE_THRESHOLD}")

    assert parsed == (ALERT_TYPE_THRESHOLD, ALERT_TYPE_OOR)
    with pytest.raises(Exception, match="不支持"):
        parse_alert_types("未知预警")


def test_threshold_triggers_use_real_rule_bounds() -> None:
    rule = make_rule()
    triggers = threshold_triggers(rule, random.Random(1))

    assert {level for level, _ in triggers} == {1, 2, 3}
    assert any(value > rule.level3_upper for level, value in triggers if level == 3)
    assert any(value < rule.level3_lower for level, value in triggers if level == 3)


@pytest.mark.parametrize(
    ("alert_type", "category"),
    (
        (ALERT_TYPE_THRESHOLD, ALERT_CATEGORY_DATA),
        (ALERT_TYPE_STUCK, ALERT_CATEGORY_DATA),
        (ALERT_TYPE_OOR, ALERT_CATEGORY_DATA),
        (ALERT_TYPE_OFFLINE, ALERT_CATEGORY_DEVICE),
        (ALERT_TYPE_DAQ_OFFLINE, ALERT_CATEGORY_DEVICE),
    ),
)
def test_generate_alert_uses_existing_real_channel(
    alert_type: str,
    category: int,
) -> None:
    alert = generate_alert(make_rule(), (alert_type,), random.Random(2))

    assert alert.alert_type == alert_type
    assert alert.alert_category == category
    if alert_type == ALERT_TYPE_DAQ_OFFLINE:
        assert alert.sensor_no == "daq-01"
        assert alert.channel == "__DAQ__"
        assert alert.threshold_id is None
    else:
        assert alert.sensor_no == "accl-01"
        assert alert.channel == "x"
        assert alert.threshold_id == "threshold-1"


def test_out_of_range_value_is_outside_real_physical_range() -> None:
    rule = make_rule()
    value = out_of_range_value(rule, random.Random(3))

    assert value < rule.range_min or value > rule.range_max


def test_floor_window_start_aligns_one_and_ten_minutes() -> None:
    value = datetime(2026, 11, 27, 14, 59, 42)

    assert floor_window_start(value, 1) == datetime(2026, 11, 27, 14, 59)
    assert floor_window_start(value, 10) == datetime(2026, 11, 27, 14, 50)


@pytest.mark.parametrize(("period_type", "minutes"), (("1m", 1), ("10m", 10)))
def test_generate_statistics_rows_covers_every_real_target(
    period_type: str,
    minutes: int,
) -> None:
    targets = [
        StatTarget("sensor-a", "x", -10.0, 10.0),
        StatTarget("sensor-b", "temperature", 0.0, 50.0),
    ]
    window_start = datetime(2026, 11, 27, 14, 50)

    rows = generate_statistics_rows(
        targets,
        period_type=period_type,
        minutes=minutes,
        window_start=window_start,
        rng=random.Random(4),
    )

    assert len(rows) == len(targets)
    assert {(row[1], row[2]) for row in rows} == {
        ("sensor-a", "x"),
        ("sensor-b", "temperature"),
    }
    assert {row[3] for row in rows} == {period_type}
    assert {row[4] for row in rows} == {window_start}
    assert all(row[6] <= row[7] <= row[5] for row in rows)
