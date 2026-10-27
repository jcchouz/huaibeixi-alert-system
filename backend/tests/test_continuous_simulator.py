import sys

import pytest

import tools.continuous_simulator as continuous_simulator
from tools.continuous_simulator import (
    ChannelPlan,
    assign_alert_scenarios,
    env_bool,
    out_of_range_target,
    parse_args,
    plan_key,
    should_skip,
    threshold_target,
)


@pytest.fixture(autouse=True)
def _enable_all_alert_scenarios(monkeypatch) -> None:
    monkeypatch.setattr(continuous_simulator, "ENABLE_THRESHOLD_ALERT", True)
    monkeypatch.setattr(continuous_simulator, "ENABLE_STUCK_ALERT", True)
    monkeypatch.setattr(continuous_simulator, "ENABLE_OOR_ALERT", True)
    monkeypatch.setattr(continuous_simulator, "ENABLE_SENSOR_OFFLINE_ALERT", True)
    monkeypatch.setattr(continuous_simulator, "ENABLE_DAQ_OFFLINE_ALERT", True)


def channel_plan(
    sensor_no: str,
    channel: str,
    instrument_id: str,
    *,
    level3_lower: float | None = None,
    level3_upper: float | None = None,
    range_min: float | None = 0.0,
    range_max: float | None = 100.0,
) -> ChannelPlan:
    lower = level3_lower if level3_lower is not None else range_min
    upper = level3_upper if level3_upper is not None else range_max
    base = ((lower or 0.0) + (upper or 0.0)) / 2.0
    return ChannelPlan(
        sensor_no=sensor_no,
        channel=channel,
        instrument_id=instrument_id,
        sample_rate=1.0,
        scenario="normal",
        base_value=base,
        amplitude=0.1,
        phase=0.0,
        level3_lower=level3_lower,
        level3_upper=level3_upper,
        range_min=range_min,
        range_max=range_max,
    )


def test_env_bool_parses_supported_values(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ALERT_ENABLED", "yes")
    assert env_bool("TEST_ALERT_ENABLED", False) is True

    monkeypatch.setenv("TEST_ALERT_ENABLED", "off")
    assert env_bool("TEST_ALERT_ENABLED", True) is False

    monkeypatch.delenv("TEST_ALERT_ENABLED")
    assert env_bool("TEST_ALERT_ENABLED", True) is True

    monkeypatch.setenv("TEST_ALERT_ENABLED", "invalid")
    with pytest.raises(ValueError, match="TEST_ALERT_ENABLED"):
        env_bool("TEST_ALERT_ENABLED", True)


def test_assign_alert_scenarios_uses_only_existing_threshold_channels() -> None:
    plans = [
        channel_plan("sensor-a", "x", "instrument-a", level3_upper=8.0),
        channel_plan("sensor-b", "x", "instrument-a", level3_upper=8.0),
        channel_plan("sensor-c", "x", "instrument-b", level3_upper=8.0),
        channel_plan("sensor-d", "x", "instrument-b", level3_upper=8.0),
        channel_plan("sensor-e", "x", "instrument-c", level3_upper=8.0),
        channel_plan("sensor-f", "x", "instrument-c", level3_upper=8.0),
    ]

    assigned = assign_alert_scenarios(plans)
    original_keys = {plan_key(plan) for plan in plans}
    scenario_plans = {plan.scenario: plan for plan in assigned if plan.scenario != "normal"}

    assert set(scenario_plans) == {
        "threshold",
        "stuck",
        "oor",
        "sensor_offline",
        "daq_offline",
    }
    assert {plan_key(plan) for plan in scenario_plans.values()} <= original_keys
    assert all(
        should_skip(plan) for plan in assigned if plan.scenario in {"sensor_offline", "daq_offline"}
    )


def test_threshold_and_oor_targets_respect_real_physical_range() -> None:
    plan = channel_plan(
        "real-sensor",
        "x",
        "real-instrument",
        level3_upper=8.0,
        range_min=0.0,
        range_max=100.0,
    )

    threshold = threshold_target(plan)
    oor = out_of_range_target(plan)

    assert threshold is not None
    assert oor is not None
    assert threshold[0] > 8.0
    assert threshold[0] - threshold[1] > 8.0
    assert threshold[0] + threshold[1] < 100.0
    assert oor[0] - oor[1] > 100.0


def test_assign_alert_scenarios_keeps_sensor_offline_instrument_online() -> None:
    plans = [
        channel_plan("sensor-a", "x", "instrument-a", level3_upper=8.0),
        channel_plan("sensor-a", "y", "instrument-a", level3_upper=8.0),
        channel_plan("sensor-b", "x", "instrument-b", level3_upper=8.0),
        channel_plan("sensor-b", "y", "instrument-b", level3_upper=8.0),
    ]

    assigned = assign_alert_scenarios(plans)
    offline = next(plan for plan in assigned if plan.scenario == "sensor_offline")
    siblings = [
        plan
        for plan in assigned
        if plan.instrument_id == offline.instrument_id and plan_key(plan) != plan_key(offline)
    ]

    assert siblings
    assert all(plan.scenario != "daq_offline" for plan in siblings)


def test_assign_alert_scenarios_respects_macro_switches(monkeypatch) -> None:
    plans = [
        channel_plan("sensor-a", "x", "instrument-a", level3_upper=8.0),
        channel_plan("sensor-a", "y", "instrument-a", level3_upper=8.0),
        channel_plan("sensor-b", "x", "instrument-b", level3_upper=8.0),
        channel_plan("sensor-b", "y", "instrument-b", level3_upper=8.0),
    ]
    monkeypatch.setattr(continuous_simulator, "ENABLE_THRESHOLD_ALERT", True)
    monkeypatch.setattr(continuous_simulator, "ENABLE_STUCK_ALERT", False)
    monkeypatch.setattr(continuous_simulator, "ENABLE_OOR_ALERT", False)
    monkeypatch.setattr(continuous_simulator, "ENABLE_SENSOR_OFFLINE_ALERT", False)
    monkeypatch.setattr(continuous_simulator, "ENABLE_DAQ_OFFLINE_ALERT", False)

    assigned = assign_alert_scenarios(plans)

    assert {plan.scenario for plan in assigned} <= {"normal", "threshold"}
    assert any(plan.scenario == "threshold" for plan in assigned)
    assert not any(should_skip(plan) for plan in assigned)


def test_parse_args_accepts_timestamp_lead(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["continuous_simulator.py", "--timestamp-lead-seconds", "3"],
    )

    args = parse_args()

    assert args.timestamp_lead_seconds == 3.0


def test_parse_args_rejects_negative_timestamp_lead(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["continuous_simulator.py", "--timestamp-lead-seconds", "-1"],
    )

    with pytest.raises(SystemExit):
        parse_args()
