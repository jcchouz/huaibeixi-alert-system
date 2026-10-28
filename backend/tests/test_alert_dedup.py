from __future__ import annotations

from datetime import datetime
from typing import Any

from app.domain.alert_types import ALERT_TYPE_THRESHOLD
from app.domain.engine import AlertAnalyticsEngine


class RecordingCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[Any, ...] | None]] = []
        self.last_sql = ""

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.last_sql = sql
        self.executions.append((sql, params))

    def fetchone(self) -> dict[str, Any] | None:
        if "SELECT id" in self.last_sql:
            return None
        return {
            "id": "new-alert",
            "sensor_no": "sensor-1",
            "channel": "x",
            "alert_type": ALERT_TYPE_THRESHOLD,
            "alert_category": 0,
            "alert_level": 1,
            "alert_status": 0,
            "end_time": None,
        }


def test_handled_alert_is_not_reused_for_a_new_incident() -> None:
    cursor = RecordingCursor()
    engine = AlertAnalyticsEngine()

    engine._upsert_alert_incident(
        cursor,
        sensor_no="sensor-1",
        channel="x",
        threshold_id=None,
        alert_type=ALERT_TYPE_THRESHOLD,
        alert_level=1,
        trigger_val=1.0,
        fire_time=datetime(2026, 10, 20, 12, 0, 0),
    )

    dedup_sql = next(sql for sql, _ in cursor.executions if "SELECT id" in sql)
    assert "end_time IS NULL" in dedup_sql
    assert "alert_status" not in dedup_sql
    insert_sql = next(
        sql
        for sql, _ in cursor.executions
        if "INSERT INTO public.t_alert_record" in sql
    )
    assert "trigger_count" not in insert_sql
    assert "update_time" not in insert_sql
    assert "end_time" in insert_sql
    assert "rule_snapshot" in insert_sql


def test_rule_snapshot_contains_rule_definition_without_runtime_state() -> None:
    engine = AlertAnalyticsEngine()

    snapshot = engine.build_rule_snapshot(
        {
            "id": "threshold-1",
            "sensor_no": "sensor-1",
            "channel": "x",
            "level3_upper": 10.0,
            "frequency": "1",
            "offline_timeout_ms": 5000,
            "last_ts": "2026-10-20 11:59:00",
            "threshold_condition_active": True,
        },
        ALERT_TYPE_THRESHOLD,
    )

    assert snapshot["id"] == "threshold-1"
    assert snapshot["level3_upper"] == 10.0
    assert snapshot["offline_timeout_ms"] == 5000
    assert "last_ts" not in snapshot
    assert "threshold_condition_active" not in snapshot


def test_rule_snapshot_is_only_written_when_alert_is_created() -> None:
    class ExistingAlertCursor(RecordingCursor):
        def fetchone(self) -> dict[str, Any] | None:
            if "SELECT id" in self.last_sql:
                return {"id": "open-alert"}
            return {
                "id": "open-alert",
                "sensor_no": "sensor-1",
                "channel": "x",
                "alert_type": ALERT_TYPE_THRESHOLD,
                "alert_category": 0,
                "alert_level": 1,
                "alert_status": 0,
                "end_time": None,
                "rule_snapshot": {"level3_upper": 10.0},
            }

    cursor = ExistingAlertCursor()
    engine = AlertAnalyticsEngine()
    engine._upsert_alert_incident(
        cursor,
        sensor_no="sensor-1",
        channel="x",
        threshold_id="threshold-1",
        alert_type=ALERT_TYPE_THRESHOLD,
        alert_level=2,
        trigger_val=12.0,
        fire_time=datetime(2026, 10, 20, 12, 0, 0),
        rule_snapshot={"level3_upper": 9.0},
    )

    update_sql = next(
        sql for sql, _ in cursor.executions if "UPDATE public.t_alert_record" in sql
    )
    assert "rule_snapshot" not in update_sql


def test_close_alert_incident_writes_recovery_time() -> None:
    cursor = RecordingCursor()
    engine = AlertAnalyticsEngine()
    active_map = {
        engine._alert_key("sensor-1", "x", ALERT_TYPE_THRESHOLD): {
            "id": "open-alert",
            "sensor_no": "sensor-1",
            "channel": "x",
            "alert_type": ALERT_TYPE_THRESHOLD,
        }
    }
    recovered_at = datetime(2026, 10, 20, 12, 5, 0)

    result = engine._close_alert_incident(
        cursor,
        active_map,
        sensor_no="sensor-1",
        channel="x",
        alert_type=ALERT_TYPE_THRESHOLD,
        end_time=recovered_at,
    )

    update_sql, update_params = next(
        (sql, params)
        for sql, params in cursor.executions
        if "SET end_time" in sql
    )
    assert "update_time" not in update_sql
    assert "trigger_count" not in update_sql
    assert "LOWER(sensor_no) = %s" in update_sql
    assert "alert_type = %s" in update_sql
    assert update_params == (
        recovered_at,
        "sensor-1",
        "x",
        ALERT_TYPE_THRESHOLD,
    )
    assert result is not None
    assert not active_map
