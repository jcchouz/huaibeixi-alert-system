"""通道离线预警规则。

每个检测窗口都必须执行本规则，包括通道在当前窗口没有产生任何新样本的情况。
离线检测不能依赖新数据到达，否则持续静默的通道将永远无法被判定为离线。
"""

from __future__ import annotations

from app.domain.alert_rules.base import (
    AlertTrigger,
    RuleContext,
    RuleEvaluation,
    parse_datetime,
)
from app.domain.alert_types import ALERT_TYPE_OFFLINE, NON_THRESHOLD_ALERT_LEVEL


class OfflineAlertRule:
    """只根据当前扫描是否完全没有样本来检测通道离线。

    ``last_ts`` 表示运行状态中最后一次成功观测到样本的时间，
    ``offline_condition_active`` 表示上一次的判定结果，用于避免每次扫描都
    重复创建同一个离线预警事件。
    """

    alert_type = ALERT_TYPE_OFFLINE

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        rule = context.rule
        previous_active = bool(rule.get("offline_condition_active"))

        if context.samples:
            # 当前扫描只要存在该通道的原始样本，就视为在线。窗口边界间隔、
            # 窗口内最大采样间隔和末样本距窗口结束的时长都不参与离线判定。
            currently_offline = False
        else:
            # 运行状态会跨检测窗口保留。``last_ts`` 缺失表示规则尚未观测到该通道。
            last_seen = parse_datetime(rule.get("last_ts"))
            # 当前窗口没有新样本。若通道从未被观测到，则使用无限大的静默时长，
            # 确保仍能判定其离线，而不是继续等待可能永远不会到达的数据。
            silence_ms = (
                float("inf")
                if last_seen is None
                else (context.window_end - last_seen).total_seconds() * 1000.0
            )
            currently_offline = silence_ms >= context.offline_timeout_ms

        state_updates = {
            "offline_condition_active": bool(currently_offline),
        }
        if currently_offline:
            # 离线状态优先于数据质量类预警。重置阈值、死值和超量程的连续命中
            # 状态，避免通道恢复后基于过期样本继续累计这些预警。
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
        # 仅在进入离线条件时创建新预警事件。实际事件开始时间使用检测窗口结束
        # 时间。
        if currently_offline and not previous_active:
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
            # 通道离线期间，其他单通道规则不得基于缺失或过期数据创建预警；
            # 已有预警事件由检测引擎关闭。
            suppress_other_alerts=currently_offline,
        )
