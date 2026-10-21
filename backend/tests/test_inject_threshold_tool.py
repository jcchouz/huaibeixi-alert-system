from __future__ import annotations

import pytest

from tools.inject_threshold import (
    ThresholdRule,
    normal_value,
    parse_frequency_hz,
    target_value,
)


def make_rule(**overrides) -> ThresholdRule:
    values = {
        "sensor_no": "sensor-1",
        "channel": "stress",
        "upper": {1: 15.0, 2: 13.0, 3: 11.5},
        "lower": {1: -15.0, 2: -13.0, 3: -11.5},
        "physical_min": -1500.0,
        "physical_max": 1500.0,
        "frequency_hz": 1.0,
    }
    values.update(overrides)
    return ThresholdRule(**values)


def test_parse_frequency_supports_plain_hz_and_fraction() -> None:
    assert parse_frequency_hz("1") == 1.0
    assert parse_frequency_hz("100hz") == 100.0
    assert parse_frequency_hz("1/60") == pytest.approx(1 / 60)
    assert parse_frequency_hz(None) == 1.0
    assert parse_frequency_hz("invalid") == 1.0


def test_target_value_selects_the_requested_upper_level() -> None:
    rule = make_rule()
    assert target_value(rule, "upper", 3, None) == 12.25
    assert target_value(rule, "upper", 2, None) == 14.0
    assert target_value(rule, "upper", 1, None) == 15.3


def test_target_value_selects_the_requested_lower_level() -> None:
    rule = make_rule()
    assert target_value(rule, "lower", 3, None) == -12.25
    assert target_value(rule, "lower", 2, None) == -14.0
    assert target_value(rule, "lower", 1, None) == -15.3


def test_target_value_uses_explicit_value_and_checks_physical_range() -> None:
    rule = make_rule()
    assert target_value(rule, "upper", 3, 12.0) == 12.0

    narrow = make_rule(physical_max=15.2)
    with pytest.raises(SystemExit):
        target_value(narrow, "upper", 1, None)


def test_normal_value_stays_inside_level3_band() -> None:
    assert normal_value(make_rule()) == 0.0
    temperature = make_rule(lower={1: -30.0, 2: -20.0, 3: -15.0}, upper={1: 65.0, 2: 55.0, 3: 45.0})
    assert normal_value(temperature) == 15.0
