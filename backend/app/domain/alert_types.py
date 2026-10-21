"""后端共享的预警类型、方向和分类常量。"""

from __future__ import annotations

from typing import Optional

# 数据库中存储的预警类型名称。
ALERT_TYPE_THRESHOLD = "阈值超限"
ALERT_TYPE_OFFLINE = "传感器离线"
ALERT_TYPE_DAQ_OFFLINE = "采集仪离线"
ALERT_TYPE_STUCK = "传感器死值"
ALERT_TYPE_OOR = "超物理量程"

# 非阈值类预警统一使用最高严重级别。
NON_THRESHOLD_ALERT_LEVEL = 1

# 阈值和物理量程预警使用的超限方向。
ALERT_DIRECTION_UPPER = "上限超限"
ALERT_DIRECTION_LOWER = "下限超限"
ALERT_DIRECTION_NONE = "无方向"

# 预警过滤和展示使用的分类值。
ALERT_CATEGORY_DATA = 0
ALERT_CATEGORY_DEVICE = 1

# 设备类预警与数据类预警分开归类。
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

# 兼容旧版英文别名，但统一存储为中文标准名称。
ALERT_TYPE_ALIASES = {
    "THRESHOLD": ALERT_TYPE_THRESHOLD,
    "OFFLINE": ALERT_TYPE_OFFLINE,
    "DAQ_OFFLINE": ALERT_TYPE_DAQ_OFFLINE,
    "STUCK": ALERT_TYPE_STUCK,
    "OOR": ALERT_TYPE_OOR,
}


def normalize_alert_type(value: Optional[str]) -> Optional[str]:
    """将可接受的别名转换为标准预警类型。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return ALERT_TYPE_ALIASES.get(text.upper(), text)


def alert_category_for_type(value: Optional[str]) -> int:
    """解析预警类型对应的展示和过滤分类。"""
    normalized = normalize_alert_type(value)
    if normalized in DEVICE_ALERT_TYPES:
        return ALERT_CATEGORY_DEVICE
    return ALERT_CATEGORY_DATA
