"""Alert rule registry and evaluation orchestration."""

from __future__ import annotations

from typing import Iterable, List, Optional

from app.domain.alert_rules.base import AlertRule, RuleContext, RuleEvaluation
from app.domain.alert_rules.out_of_range import OutOfRangeAlertRule
from app.domain.alert_rules.stuck import StuckAlertRule
from app.domain.alert_rules.threshold import ThresholdAlertRule


def default_alert_rules() -> tuple[AlertRule, ...]:
    """Return the default independent per-channel rule set."""
    return (
        ThresholdAlertRule(),
        OutOfRangeAlertRule(),
        StuckAlertRule(),
    )


class AlertRuleRegistry:
    """Evaluate independent alert rules in a stable order.

    New alert types can be added by implementing ``AlertRule`` and passing the
    instance into this registry. Database writes remain centralized in the scan
    orchestrator.
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
