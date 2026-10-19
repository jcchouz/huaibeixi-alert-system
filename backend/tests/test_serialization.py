from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.core.serialization import json_dumps, serialize_row, serialize_val


def test_serialize_decimal_datetime_and_date() -> None:
    assert serialize_val(Decimal("12.3456789")) == 12.345679
    assert serialize_val(datetime(2026, 10, 19, 12, 30, 5)) == "2026-10-19 12:30:05"
    assert serialize_val(date(2026, 10, 19)) == "2026-10-19"


def test_serialize_nested_row() -> None:
    row = serialize_row(
        {
            "value": Decimal("1.25"),
            "nested": {"timestamp": datetime(2026, 10, 19, 8, 0, 0)},
        }
    )
    assert row == {
        "value": 1.25,
        "nested": {"timestamp": "2026-10-19 08:00:00"},
    }


def test_json_dumps_handles_database_types() -> None:
    payload = json_dumps(
        {
            "value": Decimal("2.50"),
            "timestamp": datetime(2026, 10, 19, 9, 30, 0),
        }
    )
    assert '"value": 2.5' in payload
    assert '"timestamp": "2026-10-19 09:30:00"' in payload
