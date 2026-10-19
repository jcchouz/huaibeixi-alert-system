from __future__ import annotations

from typing import Optional

ALERT_TYPE_THRESHOLD = "阈值超限"
ALERT_TYPE_OFFLINE = "传感器离线"
ALERT_TYPE_DAQ_OFFLINE = "采集仪离线"
ALERT_TYPE_STUCK = "传感器死值"
ALERT_TYPE_OOR = "超物理量程"

ALERT_DIRECTION_UPPER = "上限超限"
ALERT_DIRECTION_LOWER = "下限超限"
ALERT_DIRECTION_NONE = "无方向"

ALERT_DIRECTION_VALUES = (
    ALERT_DIRECTION_UPPER,
    ALERT_DIRECTION_LOWER,
    ALERT_DIRECTION_NONE,
)

ALERT_TYPE_VALUES = (
    ALERT_TYPE_THRESHOLD,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_OOR,
)

ALERT_TYPE_ALIASES = {
    "THRESHOLD": ALERT_TYPE_THRESHOLD,
    "OFFLINE": ALERT_TYPE_OFFLINE,
    "DAQ_OFFLINE": ALERT_TYPE_DAQ_OFFLINE,
    "STUCK": ALERT_TYPE_STUCK,
    "OOR": ALERT_TYPE_OOR,
}

ALERT_DIRECTION_ALIASES = {
    "UPPER": ALERT_DIRECTION_UPPER,
    "LOWER": ALERT_DIRECTION_LOWER,
    "NONE": ALERT_DIRECTION_NONE,
    "上限超限": ALERT_DIRECTION_UPPER,
    "下限超限": ALERT_DIRECTION_LOWER,
    "上限": ALERT_DIRECTION_UPPER,
    "下限": ALERT_DIRECTION_LOWER,
    "无方向": ALERT_DIRECTION_NONE,
}


def normalize_alert_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return ALERT_TYPE_ALIASES.get(text.upper(), text)


def normalize_alert_direction(value: Optional[str]) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        return ALERT_DIRECTION_NONE
    return ALERT_DIRECTION_ALIASES.get(text.upper(), text)
