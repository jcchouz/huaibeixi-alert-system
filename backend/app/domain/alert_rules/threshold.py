"""Threshold alert rule."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.domain.alert_rules.base import (
    MIN_CONFIRM_SAMPLES,
    AlertTrigger,
    RuleContext,
    RuleEvaluation,
    parse_datetime,
    required_sample_count,
    serialize_runtime_datetime,
)
from app.domain.alert_types import ALERT_TYPE_THRESHOLD


class ThresholdAlertRule:
    """Detect sustained threshold exceedance from raw samples."""

    alert_type = ALERT_TYPE_THRESHOLD
    minimum_confirm_samples = MIN_CONFIRM_SAMPLES

    @classmethod
    def required_sample_count(
        cls,
        frequency_hz: Any,
        reference_seconds: Any,
    ) -> int:
        """Convert the configured reference duration to consecutive samples."""
        return required_sample_count(
            frequency_hz,
            reference_seconds,
            minimum_samples=cls.minimum_confirm_samples,
        )

    @staticmethod
    def level_for(
        rule: Dict[str, Any],
        eval_upper: float,
        eval_lower: float,
    ) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """Return the most severe threshold level hit by one value."""
        for level in (1, 2, 3):
            upper = rule.get(f"level{level}_upper")
            if upper is not None and eval_upper > float(upper):
                return level, eval_upper, float(upper)
            lower = rule.get(f"level{level}_lower")
            if lower is not None and eval_lower < float(lower):
                return level, eval_lower, float(lower)
        return None, None, None

    @classmethod
    def sample_level(
        cls,
        rule: Dict[str, Any],
        value: float,
    ) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """Evaluate a raw sample against threshold limits."""
        return cls.level_for(rule, value, value)

    @classmethod
    def evaluate_streak(
        cls,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        previous_count: Any,
        target_count: int,
        previous_started_at: Any = None,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[
        int,
        Optional[datetime],
        bool,
        Optional[Tuple[int, float, float, datetime]],
    ]:
        """Accumulate threshold hits across samples and scan windows."""
        count = max(0, int(previous_count or 0))
        target_count = max(1, int(target_count))
        started_at = parse_datetime(previous_started_at)
        active = False
        snapshot: Optional[Tuple[int, float, float, datetime]] = None
        run_best_level: Optional[int] = None
        run_best_value: Optional[float] = None
        run_best_limit: Optional[float] = None
        run_best_time: Optional[datetime] = None
        last_sample_time = parse_datetime(previous_sample_time)

        for sample_time, value in samples:
            if (
                max_gap_ms is not None
                and last_sample_time is not None
                and (sample_time - last_sample_time).total_seconds() * 1000.0
                > max_gap_ms
            ):
                count = 0
                started_at = None
                run_best_level = None
                run_best_value = None
                run_best_limit = None
                run_best_time = None
            last_sample_time = sample_time
            level, eval_value, limit = cls.sample_level(rule, value)
            if level is None:
                count = 0
                started_at = None
                run_best_level = None
                run_best_value = None
                run_best_limit = None
                run_best_time = None
                continue
            count += 1
            if started_at is None:
                started_at = sample_time
            if run_best_level is None or level < run_best_level:
                run_best_level = level
                run_best_value = eval_value
                run_best_limit = float(limit or eval_value)
                run_best_time = sample_time
            if count >= target_count:
                active = True
                snapshot = (
                    run_best_level or level,
                    run_best_value if run_best_value is not None else eval_value,
                    run_best_limit if run_best_limit is not None else eval_value,
                    run_best_time or sample_time,
                )

        return count, started_at, active, snapshot

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        rule = context.rule
        target_count = int(rule["threshold_confirm_samples"])
        count, started_at, active, snapshot = self.evaluate_streak(
            context.samples,
            rule,
            rule.get("threshold_streak_count"),
            target_count,
            previous_started_at=rule.get("threshold_streak_started_at"),
            previous_sample_time=context.previous_last_seen,
            max_gap_ms=float(rule.get("sample_interval_ms") or 1000) * 1.5,
        )
        previous_active = bool(rule.get("threshold_condition_active"))
        state_updates: Dict[str, Any] = {
            "threshold_streak_count": count,
            "threshold_streak_started_at": (
                serialize_runtime_datetime(started_at)
                if started_at is not None
                else None
            ),
            "threshold_condition_active": bool(active),
            "threshold_condition_level": None,
        }
        trigger = None
        if snapshot is not None:
            level, value, limit, fire_time = snapshot
            previous_level = rule.get("threshold_condition_level")
            previous_level_value = (
                int(previous_level) if previous_level is not None else None
            )
            active_level = (
                level
                if previous_level_value is None
                else min(previous_level_value, level)
            )
            state_updates.update(
                {
                    "threshold_last_level": level,
                    "threshold_last_value": value,
                    "threshold_last_limit": limit,
                    "threshold_condition_level": active_level,
                }
            )
            if (
                not previous_active
                or previous_level_value is None
                or level < previous_level_value
            ):
                trigger = AlertTrigger(
                    alert_type=self.alert_type,
                    level=level,
                    value=value,
                    limit=limit,
                    fire_time=fire_time,
                )
        return RuleEvaluation(
            alert_type=self.alert_type,
            active=active,
            previous_active=previous_active,
            state_updates=state_updates,
            trigger=trigger,
        )
