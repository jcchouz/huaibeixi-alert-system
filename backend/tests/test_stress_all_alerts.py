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
from tools.stress_all_alerts import (
    StressTestError,
    build_test_plan,
    generate_batch,
    normalize_name,
)


def test_test_plan_covers_every_alert_type_and_category() -> None:
    plan = build_test_plan("benchall-session-", "session")
    alert_types = {item.alert_type for item in plan.expected_alerts}

    assert len(plan.active_channels) == 20
    assert len(plan.all_channels) == 23
    assert len(plan.expected_alerts) == 10
    assert alert_types == {
        ALERT_TYPE_THRESHOLD,
        ALERT_TYPE_STUCK,
        ALERT_TYPE_OOR,
        ALERT_TYPE_OFFLINE,
        ALERT_TYPE_DAQ_OFFLINE,
    }
    threshold_levels = {
        item.alert_level
        for item in plan.expected_alerts
        if item.alert_type == ALERT_TYPE_THRESHOLD
    }
    assert threshold_levels == {1, 2, 3}
    assert {item.alert_category for item in plan.expected_alerts} == {
        ALERT_CATEGORY_DATA,
        ALERT_CATEGORY_DEVICE,
    }


def test_generated_batch_cycles_channels_at_target_rate() -> None:
    plan = build_test_plan("benchall-session-", "session")
    start = datetime(2026, 10, 22, 12, 0, 0)

    rows = generate_batch(plan.active_channels, start, 0, 40, 10_000)

    assert len(rows) == 40
    assert rows[0][3] == start
    assert rows[1][3] == start.replace(microsecond=100)
    assert rows[20][0] == rows[0][0]
    assert rows[20][1] == rows[0][1]
    assert rows[20][3] == start.replace(microsecond=2_000)


def test_prefix_validation_rejects_unsafe_names() -> None:
    assert normalize_name("BenchAll", "prefix") == "benchall"
    with pytest.raises(StressTestError):
        normalize_name("bench all", "prefix")
