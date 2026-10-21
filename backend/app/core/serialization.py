"""JSON 和数据库值序列化辅助函数。"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional


class CustomJSONEncoder(json.JSONEncoder):
    """将 Decimal 和日期类型编码为 JSON 可用值。"""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        return super().default(obj)


def json_dumps(data: Any) -> str:
    """序列化 JSON，并支持常见数据库值类型。"""
    return json.dumps(data, cls=CustomJSONEncoder, ensure_ascii=False)


def serialize_val(val: Any) -> Any:
    """将单个数据库值转换为 API 安全值。"""
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
    """序列化一行数据库记录中的全部字段。"""
    if row is None:
        return None
    return {k: serialize_val(v) for k, v in dict(row).items()}


def serialize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """批量序列化数据库记录。"""
    return [serialize_row(r) for r in rows if r is not None]
