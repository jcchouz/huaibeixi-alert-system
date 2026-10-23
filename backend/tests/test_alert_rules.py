from __future__ import annotations

from datetime import datetime

from app.domain.alert_rules import (
    AlertRuleRegistry,
    DaqOfflineRule,
    OfflineAlertRule,
    OutOfRangeAlertRule,
    StuckAlertRule,
    ThresholdAlertRule,
)
from app.domain.alert_rules.base import (
    AlertTrigger,
    RuleContext,
    RuleEvaluation,
)
from app.domain.engine import AlertAnalyticsEngine


class CustomRule:
    alert_type = "自定义预警"

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        return RuleEvaluation(
            alert_type=self.alert_type,
            active=True,
            previous_active=False,
            state_updates={"custom_active": True},
            trigger=AlertTrigger(
                alert_type=self.alert_type,
                level=1,
                value=1.0,
                limit=0.5,
                fire_time=context.window_end,
            ),
        )


def test_default_registry_contains_independent_rules() -> None:
    rules = AlertRuleRegistry().rules

    assert [type(rule) for rule in rules] == [
        ThresholdAlertRule,
        OutOfRangeAlertRule,
        StuckAlertRule,
    ]


def test_registry_accepts_a_new_rule_without_engine_changes() -> None:
    context = RuleContext(
        rule={},
        samples=[],
        previous_last_seen=None,
        window_end=datetime(2026, 10, 23, 12, 0, 0),
        confirm_seconds=3.0,
        stuck_seconds=100.0,
        offline_timeout_ms=5_000.0,
    )

    results = AlertRuleRegistry([CustomRule()]).evaluate(context)

    assert len(results) == 1
    assert results[0].alert_type == "自定义预警"
    assert results[0].state_updates == {"custom_active": True}


def test_engine_accepts_an_injected_rule_set() -> None:
    engine = AlertAnalyticsEngine(channel_rules=[CustomRule()])

    assert [type(rule) for rule in engine.channel_rule_registry.rules] == [
        CustomRule
    ]


def test_offline_rule_suppresses_other_channel_alerts() -> None:
    result = OfflineAlertRule().evaluate(
        RuleContext(
            rule={
                "last_ts": "2026-10-23 11:59:50",
                "offline_condition_active": False,
            },
            samples=[],
            previous_last_seen=None,
            window_end=datetime(2026, 10, 23, 12, 0, 0),
            confirm_seconds=3.0,
            stuck_seconds=100.0,
            offline_timeout_ms=5_000.0,
        )
    )

    assert result.active is True
    assert result.suppress_other_alerts is True
    assert result.trigger is not None


def test_daq_offline_requires_all_real_channels_offline() -> None:
    rule = DaqOfflineRule()

    partial = rule.evaluate(
        total_channels=3,
        offline_channels={"sensor:x", "sensor:y"},
        previous_active=False,
        active_alert_exists=False,
        state_known=False,
    )
    complete = rule.evaluate(
        total_channels=3,
        offline_channels={"sensor:x", "sensor:y", "sensor:z"},
        previous_active=False,
        active_alert_exists=False,
        state_known=False,
    )

    assert partial.active is False
    assert complete.active is True
    assert complete.should_create is True
