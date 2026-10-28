"""通道离线预警规则。

每个检测窗口都必须执行本规则，包括通道在当前窗口没有产生任何新样本的情况。
离线检测不能依赖新数据到达，否则持续静默的通道将永远无法被判定为离线。
"""

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
    """检测单个通道的缺失采样或采样间隔过大。

    ``last_ts`` 表示运行状态中最后一次成功观测到样本的时间，
    ``offline_condition_active`` 表示上一次的判定结果，用于避免每次扫描都
    重复创建同一个离线预警事件。
    """

    alert_type = ALERT_TYPE_OFFLINE

    def evaluate(self, context: RuleContext) -> RuleEvaluation:
        rule = context.rule
        # 运行状态会跨检测窗口保留。``last_ts`` 缺失表示规则尚未观测到该通道。
        last_seen = parse_datetime(rule.get("last_ts"))
        previous_active = bool(rule.get("offline_condition_active"))

        if not context.samples:
            # 当前窗口没有新样本。若通道从未被观测到，则使用无限大的静默时长，
            # 确保仍能判定其离线，而不是继续等待可能永远不会到达的数据。
            silence_ms = (
                float("inf")
                if last_seen is None
                else (context.window_end - last_seen).total_seconds() * 1000.0
            )
            # 无样本时不存在窗口内部间隔，只需判断从最后观测时间到窗口结束的
            # 静默时长。
            currently_offline = silence_ms >= context.offline_timeout_ms
            condition_hit = currently_offline
            largest_gap_ms = 0.0
        else:
            # 静默时长计算到窗口结束，而不仅仅是窗口内首尾样本之间的间隔。
            # 这样可以识别只上报一个点、随后在整个窗口剩余时间内静默的通道。
            last_sample_time = context.samples[-1][0]
            silence_ms = (
                context.window_end - last_sample_time
            ).total_seconds() * 1000.0
            # 同时检查跨窗口边界的间隔。前一个样本不在 ``samples`` 中，
            # 省略这段会隐藏相邻窗口之间较长的采样中断。
            boundary_gap_ms = (
                (context.samples[0][0] - context.previous_last_seen).total_seconds()
                * 1000.0
                if context.previous_last_seen is not None
                else 0.0
            )
            # 最大采样中断可能位于窗口边界，也可能位于当前窗口内的相邻样本之间。
            largest_gap_ms = max(
                boundary_gap_ms,
                max_sample_gap_ms(context.samples),
            )
            currently_offline = silence_ms >= context.offline_timeout_ms
            # 即使最新样本足够新、通道在窗口结束时已经恢复在线，
            # ``condition_hit`` 仍会记录窗口内出现过的超时间隔；
            # ``currently_offline`` 则只描述通道在 ``window_end`` 时的状态。
            condition_hit = (
                currently_offline or largest_gap_ms >= context.offline_timeout_ms
            )

        # 持久化通道在窗口结束时是否仍处于离线状态。通道恢复上报后，
        # 窗口内曾经出现过的历史间隔不会继续维持该状态标记。
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
        # 时间，即使超时间隔在窗口内更早的位置就已经发生。
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
            # ``active`` 控制已有预警事件是否继续保持开启；
            # 上方的 ``condition_hit`` 还覆盖窗口结束时已经恢复的瞬时中断。
            active=currently_offline,
            previous_active=previous_active,
            state_updates=state_updates,
            trigger=trigger,
            # 通道离线期间，其他单通道规则不得基于缺失或过期数据创建预警；
            # 已有预警事件由检测引擎关闭。
            suppress_other_alerts=currently_offline,
        )
