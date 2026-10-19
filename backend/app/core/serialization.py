from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        return super().default(obj)


def json_dumps(data: Any) -> str:
    return json.dumps(data, cls=CustomJSONEncoder, ensure_ascii=False)


def serialize_val(val: Any) -> Any:
    if isinstance(val, Decimal):
        return round(float(val), 6)
    if isinstance(val, datetime):
        if val.microsecond:
            return val.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, list):
        return [serialize_val(v) for v in val]
    if isinstance(val, dict):
        return {k: serialize_val(v) for k, v in val.items()}
    return val


def serialize_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: serialize_val(v) for k, v in dict(row).items()}


def serialize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [serialize_row(r) for r in rows if r is not None]
