from __future__ import annotations

from typing import Optional

ALERT_TYPE_THRESHOLD = "阈值超限"
ALERT_TYPE_OFFLINE = "传感器离线"
ALERT_TYPE_DAQ_OFFLINE = "采集仪离线"
ALERT_TYPE_STUCK = "传感器死值"
ALERT_TYPE_OOR = "超物理量程"

NON_THRESHOLD_ALERT_LEVEL = 1

ALERT_DIRECTION_UPPER = "上限超限"
ALERT_DIRECTION_LOWER = "下限超限"
ALERT_DIRECTION_NONE = "无方向"

ALERT_CATEGORY_DATA = 0
ALERT_CATEGORY_DEVICE = 1

ALERT_CATEGORY_LABELS = {
    ALERT_CATEGORY_DATA: "数据预警",
    ALERT_CATEGORY_DEVICE: "设备预警",
}

DEVICE_ALERT_TYPES = {
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_DAQ_OFFLINE,
}

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


def normalize_alert_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return ALERT_TYPE_ALIASES.get(text.upper(), text)


def alert_category_for_type(value: Optional[str]) -> int:
    normalized = normalize_alert_type(value)
    if normalized in DEVICE_ALERT_TYPES:
        return ALERT_CATEGORY_DEVICE
    return ALERT_CATEGORY_DATA
