"""超物理量程预警规则。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.domain.alert_rules.base import (
    AlertTrigger,
    RuleContext,
    RuleEvaluation,
    parse_datetime,
    serialize_runtime_datetime,
)
from app.domain.alert_types import ALERT_TYPE_OOR, NON_THRESHOLD_ALERT_LEVEL


class OutOfRangeAlertRule:
    """检测持续超出配置物理量程的采样点。"""

    alert_type = ALERT_TYPE_OOR

    @staticmethod
    def is_out_of_range(rule: Dict[str, Any], value: float) -> bool:
        physical_min = rule.get("physical_min")
        physical_max = rule.get("physical_max")
        if physical_min is not None and value < float(physical_min):
            return True
        return physical_max is not None and value > float(physical_max)

    @classmethod
    def evaluate_streak(
        cls,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        previous_count: Any,
        target_count: int,
        previous_started_at: Any = None,
    ) -> Tuple[
        int,
        Optional[datetime],
        bool,
        Optional[Tuple[float, datetime]],
    ]:
        count = max(0, int(previous_count or 0))
        target_count = max(1, int(target_count))
        started_at = parse_datetime(previous_started_at)
        active = False
        snapshot: Optional[Tuple[float, datetime]] = None
        for sample_time, value in samples:
            if not cls.is_out_of_range(rule, value):
                count = 0
                started_at = None
                continue
            count += 1
            if started_at is None:
                started_at = sample_time
            if count >= target_count:
                active = True
                snapshot = (value, sample_time)
        return count, started_at, active, snapshot

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        rule = context.rule
        target_count = int(rule["oor_confirm_samples"])
        count, started_at, active, snapshot = self.evaluate_streak(
            context.samples,
            rule,
            rule.get("oor_streak_count"),
            target_count,
            previous_started_at=rule.get("oor_streak_started_at"),
        )
        previous_active = bool(rule.get("oor_condition_active"))
        state_updates: Dict[str, Any] = {
            "oor_streak_count": count,
            "oor_streak_started_at": (
                serialize_runtime_datetime(started_at)
                if started_at is not None
                else None
            ),
            "oor_condition_active": bool(active),
        }
        trigger = None
        if snapshot is not None:
            value, fire_time = snapshot
            state_updates["oor_last_value"] = value
            if not previous_active:
                trigger = AlertTrigger(
                    alert_type=self.alert_type,
                    level=NON_THRESHOLD_ALERT_LEVEL,
                    value=value,
                    limit=value,
                    fire_time=fire_time,
                )
        return RuleEvaluation(
            alert_type=self.alert_type,
            active=active,
            previous_active=previous_active,
            state_updates=state_updates,
            trigger=trigger,
        )
