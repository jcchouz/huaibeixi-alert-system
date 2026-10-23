"""Channel offline alert rule."""

from __future__ import annotations

from app.domain.alert_rules.base import (
    AlertTrigger,
    RuleContext,
    RuleEvaluation,
    max_sample_gap_ms,
    parse_datetime,
)
from app.domain.alert_types import ALERT_TYPE_OFFLINE, NON_THRESHOLD_ALERT_LEVEL


class OfflineAlertRule:
    """Detect missing samples or excessive gaps for one channel."""

    alert_type = ALERT_TYPE_OFFLINE

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        rule = context.rule
        last_seen = parse_datetime(rule.get("last_ts"))
        previous_active = bool(rule.get("offline_condition_active"))

        if not context.samples:
            silence_ms = (
                float("inf")
                if last_seen is None
                else (context.window_end - last_seen).total_seconds() * 1000.0
            )
            currently_offline = silence_ms >= context.offline_timeout_ms
            condition_hit = currently_offline
            largest_gap_ms = 0.0
        else:
            last_sample_time = context.samples[-1][0]
            silence_ms = (
                context.window_end - last_sample_time
            ).total_seconds() * 1000.0
            boundary_gap_ms = (
                (context.samples[0][0] - context.previous_last_seen).total_seconds()
                * 1000.0
                if context.previous_last_seen is not None
                else 0.0
            )
            largest_gap_ms = max(
                boundary_gap_ms,
                max_sample_gap_ms(context.samples),
            )
            currently_offline = silence_ms >= context.offline_timeout_ms
            condition_hit = (
                currently_offline or largest_gap_ms >= context.offline_timeout_ms
            )

        state_updates = {
            "offline_condition_active": bool(currently_offline),
        }
        if currently_offline:
            state_updates.update(
                {
                    "online_status": "OFFLINE",
                    "stuck_since": None,
                    "stuck_streak_count": 0,
                    "threshold_streak_count": 0,
                    "threshold_streak_started_at": None,
                    "oor_streak_count": 0,
                    "oor_streak_started_at": None,
                    "threshold_condition_active": False,
                    "threshold_condition_level": None,
                    "stuck_condition_active": False,
                    "oor_condition_active": False,
                }
            )

        trigger = None
        if condition_hit and not previous_active:
            trigger = AlertTrigger(
                alert_type=self.alert_type,
                level=NON_THRESHOLD_ALERT_LEVEL,
                value=0.0,
                limit=0.0,
                fire_time=context.window_end,
            )
        return RuleEvaluation(
            alert_type=self.alert_type,
            active=currently_offline,
            previous_active=previous_active,
            state_updates=state_updates,
            trigger=trigger,
            suppress_other_alerts=currently_offline,
        )
