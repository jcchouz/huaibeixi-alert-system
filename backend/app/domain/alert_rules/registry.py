"""预警规则注册表及评估调度。"""

from __future__ import annotations

from typing import Iterable, List, Optional

from app.domain.alert_rules.base import AlertRule, RuleContext, RuleEvaluation
from app.domain.alert_rules.out_of_range import OutOfRangeAlertRule
from app.domain.alert_rules.stuck import StuckAlertRule
from app.domain.alert_rules.threshold import ThresholdAlertRule


def default_alert_rules() -> tuple[AlertRule, ...]:
    """返回默认的独立单通道预警规则集合。"""
    return (
        ThresholdAlertRule(),
        OutOfRangeAlertRule(),
        StuckAlertRule(),
    )


class AlertRuleRegistry:
    """按稳定顺序评估各条独立的预警规则。

    新增预警类型时，实现 ``AlertRule`` 并将实例传入本注册表即可。
    数据库写入仍统一由扫描调度器负责。
    """

    def __init__(self, rules: Optional[Iterable[AlertRule]] = None) -> None:
        self._rules: List[AlertRule] = list(
            default_alert_rules() if rules is None else rules
        )

    @property
    def rules(self) -> tuple[AlertRule, ...]:
        return tuple(self._rules)

    def evaluate(self, context: RuleContext) -> List[RuleEvaluation]:
        return [rule.evaluate(context) for rule in self._rules]
