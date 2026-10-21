"""请求字段兼容和日期解析等公共服务函数。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def pick(value_1: Any, value_2: Any, default: Any = None) -> Any:
    """返回第一个非空值，兼容驼峰和下划线字段名。"""
    if value_1 is not None:
        return value_1
    if value_2 is not None:
        return value_2
    return default


def parse_datetime(value: Optional[str], fallback: datetime) -> datetime:
    """解析常见 API 日期格式，失败时返回默认值。"""
    if not value:
        return fallback
    normalized = value.strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(normalized[: len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return fallback
