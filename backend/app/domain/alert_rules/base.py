"""Shared contracts and utilities for alert rules."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Protocol, Tuple

MIN_CONFIRM_SAMPLES = 3
MIN_OFFLINE_PERIODS = 5


def parse_frequency_hz(value: Any) -> float:
    """Parse ``t_sensor.frequency`` values such as ``50`` or ``1/60``."""
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
    """Convert a reference duration into consecutive samples for one channel."""
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
    """Parse database, JSON or serialized runtime datetime values."""
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
    """Keep full microsecond precision for strict sample de-duplication."""
    return value.isoformat(sep=" ", timespec="microseconds")


@dataclass(frozen=True)
class AlertTrigger:
    """Alert data needed to create or refresh one incident."""

    alert_type: str
    level: int
    value: float
    limit: float
    fire_time: datetime


@dataclass
class RuleEvaluation:
    """Pure evaluation result returned by one alert rule."""

    alert_type: str
    active: bool
    previous_active: bool
    state_updates: Dict[str, Any] = field(default_factory=dict)
    trigger: Optional[AlertTrigger] = None
    suppress_other_alerts: bool = False


@dataclass(frozen=True)
class RuleContext:
    """Input shared by per-channel alert rules."""

    rule: Dict[str, Any]
    samples: List[Tuple[datetime, float]]
    previous_last_seen: Optional[datetime]
    window_end: datetime
    confirm_seconds: float
    stuck_seconds: float
    offline_timeout_ms: float


class AlertRule(Protocol):
    """Protocol implemented by each per-channel alert rule."""

    alert_type: str

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        """Evaluate one channel window without writing to the database."""
