from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.alert_types import ALERT_TYPE_OFFLINE, ALERT_TYPE_THRESHOLD
from app.domain.engine import AlertAnalyticsEngine
from app.repositories.detection_state import state_rows_for_persistence


def test_force_refresh_updates_existing_same_level_alert() -> None:
    engine = AlertAnalyticsEngine()
    key = engine._alert_key("sensor-1", "x", ALERT_TYPE_THRESHOLD)
    existing = {"id": "alert-old", "alert_level": 2}
    active_map = {key: existing}
    triggered: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def fake_upsert(cur: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"id": "alert-old", **kwargs}

    engine._upsert_alert_incident = fake_upsert  # type: ignore[method-assign]
    result = engine._apply_condition(
        object(),
        active_map,
        triggered,
        rule={"id": "threshold-1"},
        sensor_no="sensor-1",
        channel="x",
        alert_type=ALERT_TYPE_THRESHOLD,
        active=True,
        alert_level=2,
        trigger_val=12.0,
        fire_time=datetime(2026, 10, 21, 12, 0, 0),
        force_refresh=True,
    )

    assert result is not None
    assert len(calls) == 1
    assert len(triggered) == 1


def test_state_rows_include_channel_runtime_and_daq_state() -> None:
    rule = {
        "sensor_no_lower": "sensor-1",
        "channel": "x",
        "last_ts": "2026-10-21 12:00:00.000",
        "threshold_condition_active": True,
        "threshold_streak_count": 3,
        "offline_condition_active": False,
    }
    rows = state_rows_for_persistence(
        {"sensor-1:x": rule},
        {
            "sensor-1:x": {
                "last_ts": "2026-10-21 12:00:01.000",
                "threshold_condition_active": False,
                "offline_condition_active": True,
            }
        },
        {"instrument-1": True},
    )

    assert rows[0][0:2] == ("sensor-1", "x")
    assert rows[0][2]["threshold_condition_active"] is False
    assert rows[0][2]["offline_condition_active"] is True
    assert rows[1] == (
        "instrument-1",
        "__DAQ__",
        {"daq_condition_active": True},
    )


def test_offline_alert_uses_force_refresh_for_new_incident() -> None:
    engine = AlertAnalyticsEngine()
    key = engine.make_key("sensor-1", "x")
    rule = {
        "key": key,
        "sensor_no_lower": "sensor-1",
        "channel": "x",
        "instrument_id": None,
        "last_ts": "2026-10-21 11:59:00",
        "offline_seconds": 5.0,
        "offline_condition_active": False,
    }
    calls: list[dict[str, Any]] = []

    def fake_apply(cur: Any, active_map: Any, triggered: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    engine._apply_condition = fake_apply  # type: ignore[method-assign]
    engine._apply_offline_if_due(
        object(),
        {},
        [],
        {},
        {},
        rule=rule,
        key=key,
        window_end=datetime(2026, 10, 21, 12, 0, 0),
    )

    assert calls[0]["alert_type"] == ALERT_TYPE_OFFLINE
    assert calls[0]["force_refresh"] is True
