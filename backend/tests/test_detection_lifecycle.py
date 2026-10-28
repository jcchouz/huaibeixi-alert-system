from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.alert_types import ALERT_TYPE_OFFLINE, ALERT_TYPE_THRESHOLD
from app.domain.engine import AlertAnalyticsEngine


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
        "offline_timeout_ms": 5_000.0,
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
