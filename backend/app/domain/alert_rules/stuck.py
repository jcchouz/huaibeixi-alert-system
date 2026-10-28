"""传感器死值预警规则。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.domain.alert_rules.base import (
    AlertTrigger,
    RuleContext,
    RuleEvaluation,
    parse_datetime,
    serialize_runtime_datetime,
)
from app.domain.alert_types import ALERT_TYPE_STUCK, NON_THRESHOLD_ALERT_LEVEL


class StuckAlertRule:
    """检测连续不变的传感器读数。"""

    alert_type = ALERT_TYPE_STUCK

    @classmethod
    def evaluate_streak(
        cls,
        samples: List[Tuple[datetime, float]],
        previous_value: Any,
        previous_count: Any,
        previous_since: Any,
        target_count: int,
    ) -> Tuple[
        int,
        Optional[float],
        Optional[datetime],
        bool,
        Optional[Tuple[float, datetime]],
    ]:
        count = max(0, int(previous_count or 0))
        target_count = max(1, int(target_count))
        last_value = float(previous_value) if previous_value is not None else None
        since = parse_datetime(previous_since)
        active = False
        snapshot: Optional[Tuple[float, datetime]] = None
        for sample_time, value in samples:
            if last_value is not None and abs(value - last_value) <= 1e-9:
                count += 1
            else:
                count = 1
                since = sample_time
            last_value = value
            if count >= target_count:
                snapshot = (value, sample_time)
        # 只有最后一个采样点仍满足死值条件时，事件才保持活动。
        active = count >= target_count
        return count, last_value, since, active, snapshot

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        rule = context.rule
        target_count = int(rule["stuck_confirm_samples"])
        count, last_value, since, active, snapshot = self.evaluate_streak(
            context.samples,
            rule.get("stuck_last_value"),
            rule.get("stuck_streak_count"),
            rule.get("stuck_since"),
            target_count,
        )
        previous_active = bool(rule.get("stuck_condition_active"))
        state_updates = {
            "stuck_streak_count": count,
            "stuck_last_value": last_value,
            "stuck_since": (
                serialize_runtime_datetime(since) if since is not None else None
            ),
            "stuck_condition_active": bool(active),
        }
        trigger = None
        if snapshot is not None and not previous_active:
            value, fire_time = snapshot
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
