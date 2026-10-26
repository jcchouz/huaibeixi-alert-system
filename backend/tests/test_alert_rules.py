from __future__ import annotations

from datetime import datetime, timedelta

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


def test_threshold_rule_uses_frequency_based_sample_count() -> None:
    first_ts = datetime(2026, 10, 23, 12, 0, 0)
    rule = {
        "frequency_hz": 50.0,
        "sample_interval_ms": 20.0,
        "level1_upper": 10.0,
        "level2_upper": 8.0,
        "level3_upper": 6.0,
        "threshold_streak_count": 0,
        "threshold_condition_active": False,
    }
    samples = [
        (first_ts + timedelta(seconds=index / 50.0), 7.0)
        for index in range(150)
    ]
    rule_instance = ThresholdAlertRule()

    def evaluate(sample_batch):
        return rule_instance.evaluate(
            RuleContext(
                rule=rule,
                samples=sample_batch,
                previous_last_seen=None,
                window_end=first_ts + timedelta(seconds=3),
                confirm_seconds=3.0,
                stuck_seconds=100.0,
                offline_timeout_ms=5_000.0,
            )
        )

    below_target = evaluate(samples[:149])
    at_target = evaluate(samples)

    assert below_target.active is False
    assert below_target.trigger is None
    assert at_target.active is True
    assert at_target.trigger is not None


def test_out_of_range_rule_uses_frequency_based_sample_count() -> None:
    first_ts = datetime(2026, 10, 23, 12, 0, 0)
    rule = {
        "frequency_hz": 50.0,
        "sample_interval_ms": 20.0,
        "physical_min": -10.0,
        "physical_max": 10.0,
        "oor_streak_count": 0,
        "oor_condition_active": False,
    }
    samples = [
        (first_ts + timedelta(seconds=index / 50.0), 12.0)
        for index in range(150)
    ]
    rule_instance = OutOfRangeAlertRule()

    def evaluate(sample_batch):
        return rule_instance.evaluate(
            RuleContext(
                rule=rule,
                samples=sample_batch,
                previous_last_seen=None,
                window_end=first_ts + timedelta(seconds=3),
                confirm_seconds=3.0,
                stuck_seconds=100.0,
                offline_timeout_ms=5_000.0,
            )
        )

    below_target = evaluate(samples[:149])
    at_target = evaluate(samples)

    assert below_target.active is False
    assert below_target.trigger is None
    assert at_target.active is True
    assert at_target.trigger is not None


def test_stuck_rule_uses_frequency_based_sample_count() -> None:
    first_ts = datetime(2026, 10, 23, 12, 0, 0)
    rule = {
        "frequency_hz": 50.0,
        "sample_interval_ms": 20.0,
        "stuck_streak_count": 0,
        "stuck_condition_active": False,
    }
    samples = [
        (first_ts + timedelta(seconds=index / 50.0), 12.0)
        for index in range(150)
    ]
    rule_instance = StuckAlertRule()

    def evaluate(sample_batch):
        return rule_instance.evaluate(
            RuleContext(
                rule=rule,
                samples=sample_batch,
                previous_last_seen=None,
                window_end=first_ts + timedelta(seconds=3),
                confirm_seconds=3.0,
                stuck_seconds=3.0,
                offline_timeout_ms=5_000.0,
            )
        )

    below_target = evaluate(samples[:149])
    at_target = evaluate(samples)

    assert below_target.active is False
    assert below_target.trigger is None
    assert at_target.active is True
    assert at_target.trigger is not None


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
