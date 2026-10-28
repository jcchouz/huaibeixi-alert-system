"""阈值超限预警规则。"""

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
    """基于原始采样点检测持续性的阈值超限。"""

    alert_type = ALERT_TYPE_THRESHOLD
    minimum_confirm_samples = MIN_CONFIRM_SAMPLES

    @classmethod
    def required_sample_count(
        cls,
        frequency_hz: Any,
        reference_seconds: Any,
    ) -> int:
        """将配置的参考时长折算为需要连续命中的采样点数。"""
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
        """返回单个值命中的最高严重等级及其数值和限值。

        等级数值越小表示越严重。上下限判断均使用严格不等号，因此数值等于
        限值时不视为超限。
        """
        # 按等级从高到低依次匹配，首个命中的等级即为当前值的最高严重等级。
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
        """使用同一原始采样值同时评估阈值上限和下限。"""
        return cls.level_for(rule, value, value)

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
        Optional[Tuple[int, float, float, datetime]],
    ]:
        """跨采样点和扫描窗口累计连续阈值命中次数。

        ``previous_count`` 和 ``previous_started_at`` 来自上一轮扫描状态。
        """
        # 防御性地修正历史状态和目标点数，避免异常值导致无法触发预警。
        count = max(0, int(previous_count or 0))
        target_count = max(1, int(target_count))
        started_at = parse_datetime(previous_started_at)
        active = False
        snapshot: Optional[Tuple[int, float, float, datetime]] = None
        # 记录本轮连续命中区间内最严重的等级，以及该等级首次出现时的值。
        run_best_level: Optional[int] = None
        run_best_value: Optional[float] = None
        run_best_limit: Optional[float] = None
        run_best_time: Optional[datetime] = None

        for sample_time, value in samples:
            level, eval_value, limit = cls.sample_level(rule, value)
            if level is None:
                # 正常采样点会中断连续命中，后续超限必须重新累计确认点数。
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
            # 同一连续命中区间内保留最严重的等级；同等级时保留首次出现的样本。
            if run_best_level is None or level < run_best_level:
                run_best_level = level
                run_best_value = eval_value
                run_best_limit = float(limit or eval_value)
                run_best_time = sample_time
            if count >= target_count:
                # 记录批次内达到确认点数后的最严重命中，用于触发或升级事件。
                snapshot = (
                    run_best_level or level,
                    run_best_value if run_best_value is not None else eval_value,
                    run_best_limit if run_best_limit is not None else eval_value,
                    run_best_time or sample_time,
                )

        # 事件是否仍活动必须以批次最后一个采样点的连续命中状态为准。
        # snapshot 继续保留，确保批次内短暂达到确认点后恢复的事件仍可落库。
        active = count >= target_count
        return count, started_at, active, snapshot

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        rule = context.rule
        # 确认点数由采集频率和确认时长预先折算，最少需要连续命中三个采样点。
        target_count = int(rule["threshold_confirm_samples"])
        count, started_at, active, snapshot = self.evaluate_streak(
            context.samples,
            rule,
            rule.get("threshold_streak_count"),
            target_count,
            previous_started_at=rule.get("threshold_streak_started_at"),
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
            # 当前事件保持已记录的最高严重等级，避免回落采样把事件等级降级。
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
                    "threshold_condition_level": active_level if active else None,
                }
            )
            # 首次确认、迟滞状态下首次获得等级或等级升级时，创建或刷新预警事件。
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
