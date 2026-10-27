"""仪表盘总览数据聚合。"""

from __future__ import annotations

from typing import Any, Dict

from app.domain.alert_types import (
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)
from app.domain.engine import AlertAnalyticsEngine
from app.repositories.alerts import AlertRepository


class OverviewService:
    """将预警快照与实时通道和引擎状态组合。"""

    def __init__(self, alerts: AlertRepository, engine: AlertAnalyticsEngine):
        self.alerts = alerts
        self.engine = engine

    def get_overview(self) -> Dict[str, Any]:
        """返回总览页需要的 KPI、类型、级别和最近预警数据。"""
        rules = self.engine.channel_states.values()
        total_channels = len(rules)
        enabled_channels = total_channels
        total_sensors = len(self.engine.sensor_meta) or 146

        snapshot = self.alerts.get_overview_snapshot()
        kpi = snapshot["kpi"]
        by_type_rows = snapshot["by_type_rows"]
        recent_rows = [self.engine.enrich_alert_row(row) for row in snapshot["recent_rows"]]

        # 离线通道数同时参考活动离线预警和当前引擎状态。
        offline_channels = max(
            int(kpi["offline_alert_count"] or 0),
            sum(1 for rule in rules if rule.get("online_status") == "OFFLINE"),
        )
        online_channels = max(0, total_channels - offline_channels)
        type_labels = {
            ALERT_TYPE_THRESHOLD: "阈值超限",
            ALERT_TYPE_OFFLINE: "传感器离线",
            ALERT_TYPE_DAQ_OFFLINE: "采集仪离线",
            ALERT_TYPE_OOR: "超物理量程",
            ALERT_TYPE_STUCK: "传感器死值",
        }
        # 将 SQL 原始计数整理为前端可直接使用的标签和数量结构。
        alerts_by_type = [
            {
                "alertType": row["alert_type"],
                "label": type_labels.get(row["alert_type"], row["alert_type"]),
                "totalCount": int(row["total_count"]),
                "activeCount": int(row["active_count"]),
            }
            for row in by_type_rows
        ]
        alerts_by_type_map = {
            row["alert_type"]: int(row["total_count"])
            for row in by_type_rows
        }

        engine_status = self.engine.get_status()
        return {
            "totalSensors": total_sensors,
            "total_sensors": total_sensors,
            "totalChannels": total_channels,
            "total_channels": total_channels,
            "enabledChannels": enabled_channels,
            "onlineChannels": online_channels,
            "online_channels": online_channels,
            "offlineChannels": offline_channels,
            "offline_channels": offline_channels,
            "activeAlertsCount": int(kpi["active_alerts"] or 0),
            "active_alerts_count": int(kpi["active_alerts"] or 0),
            "unackedAlertsCount": int(kpi["unacked_alerts"] or 0),
            "ackedAlertsCount": int(kpi["acked_alerts"] or 0),
            "resolvedAlertsCount": int(kpi["resolved_alerts"] or 0),
            "suppressedAlertsCount": int(kpi["suppressed_alerts"] or 0),
            "totalAlertsCount": int(kpi["total_alerts"] or 0),
            "total_alerts_count": int(kpi["total_alerts"] or 0),
            "level1Count": int(kpi["active_l1"] or 0),
            "level2Count": int(kpi["active_l2"] or 0),
            "level3Count": int(kpi["active_l3"] or 0),
            "level1_count": int(kpi["active_l1"] or 0),
            "level2_count": int(kpi["active_l2"] or 0),
            "level3_count": int(kpi["active_l3"] or 0),
            "totalLevel1Count": int(kpi["total_l1"] or 0),
            "totalLevel2Count": int(kpi["total_l2"] or 0),
            "totalLevel3Count": int(kpi["total_l3"] or 0),
            "statRecordCount": snapshot["stat_count"],
            "stat_record_count": snapshot["stat_count"],
            "engineStatus": engine_status,
            "maxSeq": int(kpi["max_seq"] or 0),
            "max_seq": int(kpi["max_seq"] or 0),
            "alertsByType": alerts_by_type,
            "alertsByTypeMap": alerts_by_type_map,
            "alertsByLevel": [
                {
                    "level": 1,
                    "label": "一级红色报警",
                    "activeCount": int(kpi["active_l1"] or 0),
                    "totalCount": int(kpi["total_l1"] or 0),
                    "color": "#ef4444",
                },
                {
                    "level": 2,
                    "label": "二级橙色报警",
                    "activeCount": int(kpi["active_l2"] or 0),
                    "totalCount": int(kpi["total_l2"] or 0),
                    "color": "#f97316",
                },
                {
                    "level": 3,
                    "label": "三级黄色预警",
                    "activeCount": int(kpi["active_l3"] or 0),
                    "totalCount": int(kpi["total_l3"] or 0),
                    "color": "#eab308",
                },
            ],
            "recentAlerts": recent_rows,
        }
