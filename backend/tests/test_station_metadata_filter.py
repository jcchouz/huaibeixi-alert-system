from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any

import app.domain.engine as engine_module
from app.domain.engine import AlertAnalyticsEngine


class MetadataCursor:
    def __init__(self) -> None:
        self.result: list[dict[str, Any]] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(
        self,
        sql: str,
        params: tuple[Any, ...] | None = None,
    ) -> None:
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params or ()))

        if normalized.startswith("SELECT LOCALTIMESTAMP"):
            self.result = [{"now": datetime(2026, 10, 23, 12, 0, 0)}]
        elif normalized.startswith("SELECT number FROM t_station"):
            self.result = [{"number": "YHZ"}]
        elif normalized.startswith("SELECT ci.id"):
            self.result = [
                {
                    "id": "instrument-yh",
                    "number": "1",
                    "name": "instrument-yh",
                    "model": None,
                    "manufacturer": None,
                    "box_id": "box-yh",
                    "box_name": "box-yh",
                    "station_code": "YHZ",
                    "station_name": "玉环站",
                }
            ]
        elif normalized.startswith("SELECT s.id AS sensor_id"):
            self.result = [
                {
                    "sensor_id": "sensor-id-yh",
                    "sensor_no": "sensor-yh",
                    "sensor_name": "sensor-yh",
                    "model": None,
                    "manufacturer": None,
                    "range": None,
                    "precision": None,
                    "frequency": "1",
                    "position": None,
                    "instrument_id": "instrument-yh",
                    "instrument_name": "instrument-yh",
                    "station_code": "YHZ",
                    "station_name": "玉环站",
                }
            ]
        elif normalized.startswith("SELECT sensor_no, name, unit"):
            self.result = []
        elif normalized.startswith("SELECT at.*"):
            self.result = [
                {
                    "id": "rule-yh",
                    "sensor_no": "sensor-yh",
                    "physical_quantity": "temperature",
                    "channel": "x",
                    "range_min": 0,
                    "range_max": 100,
                }
            ]
        else:
            raise AssertionError(f"Unexpected SQL: {normalized}")

    def fetchone(self) -> dict[str, Any] | None:
        return self.result[0] if self.result else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.result


def test_load_thresholds_filters_all_metadata_by_station_code(monkeypatch) -> None:
    cursor = MetadataCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)

    engine = AlertAnalyticsEngine(station_name="玉环站")
    rule_count = engine.load_thresholds()

    assert rule_count == 1
    assert engine.station_code == "YHZ"
    assert set(engine.instrument_meta) == {"instrument-yh"}
    assert set(engine.sensor_meta) == {"sensor-yh"}
    assert next(iter(engine.sensor_meta.values()))["station_code"] == "YHZ"
    assert engine._sample_query is not None
    _, sample_params = engine._sample_query
    assert sample_params == ("sensor-yh", "x")

    instrument_sql, instrument_params = next(
        statement
        for statement in cursor.statements
        if statement[0].startswith("SELECT ci.id")
    )
    sensor_sql, sensor_params = next(
        statement
        for statement in cursor.statements
        if statement[0].startswith("SELECT s.id AS sensor_id")
    )
    threshold_sql, threshold_params = next(
        statement
        for statement in cursor.statements
        if statement[0].startswith("SELECT at.*")
    )

    assert "WHERE cb.station = %s" in instrument_sql
    assert instrument_params == ("玉环站", "YHZ")
    assert "WHERE cb.station = %s" in sensor_sql
    assert sensor_params == ("玉环站", "YHZ")
    assert "AND cb.station = %s" in threshold_sql
    assert threshold_params == ("YHZ",)
