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
            "trigger_count": 1,
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
    assert "alert_status = 0" in dedup_sql
    assert "alert_status IN (0, 1)" not in dedup_sql
    assert "direction" not in dedup_sql
    assert any("INSERT INTO public.t_alert_record" in sql for sql, _ in cursor.executions)
