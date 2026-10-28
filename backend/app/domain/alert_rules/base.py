"""预警规则共用的协议、数据结构和工具函数。"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple

MIN_CONFIRM_SAMPLES = 3
MIN_OFFLINE_PERIODS = 5


def parse_frequency_hz(value: Any) -> float:
    """解析 ``t_sensor.frequency`` 中的 ``50`` 或 ``1/60`` 等频率值。"""
    text = str(value or "").strip().lower().removesuffix("hz").strip()
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            frequency = float(numerator) / float(denominator)
        else:
            frequency = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    return frequency if frequency > 0 else 1.0


def required_sample_count(
    frequency_hz: Any,
    reference_seconds: Any,
    *,
    minimum_samples: int = MIN_CONFIRM_SAMPLES,
) -> int:
    """将参考时长折算为单个通道需要连续命中的采样点数。"""
    frequency = parse_frequency_hz(frequency_hz)
    try:
        seconds = float(reference_seconds)
    except (TypeError, ValueError):
        seconds = 1.0
    if seconds <= 0:
        seconds = 1.0
    estimated = math.ceil(frequency * seconds)
    return max(max(1, int(minimum_samples)), estimated)


def parse_datetime(value: Any) -> Optional[datetime]:
    """解析数据库、JSON 或运行时序列化格式的日期时间值。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00").replace("T", " "))
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def serialize_runtime_datetime(value: datetime) -> str:
    """保留完整微秒精度，以便严格进行采样点去重。"""
    return value.isoformat(sep=" ", timespec="microseconds")


@dataclass(frozen=True)
class AlertTrigger:
    """创建或刷新一个预警事件所需的数据。"""

    alert_type: str
    level: int
    value: float
    limit: float
    fire_time: datetime


@dataclass
class RuleEvaluation:
    """单条预警规则返回的纯计算结果。"""

    alert_type: str
    active: bool
    previous_active: bool
    state_updates: Dict[str, Any] = field(default_factory=dict)
    trigger: Optional[AlertTrigger] = None
    suppress_other_alerts: bool = False


@dataclass(frozen=True)
class RuleContext:
    """各通道预警规则共用的输入数据。"""

    rule: Dict[str, Any]
    samples: List[Tuple[datetime, float]]
    previous_last_seen: Optional[datetime]
    window_end: datetime
    confirm_seconds: float
    stuck_seconds: float
    offline_timeout_ms: float


class AlertRule(Protocol):
    """各通道预警规则需要实现的协议。"""

    alert_type: str

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        """计算单个通道窗口，但不写入数据库。"""
