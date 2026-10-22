"""仪表盘和图表的统计查询组装。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.core.serialization import serialize_val
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor
from app.repositories.statistics import StatisticsRepository
from app.services.common import parse_datetime


class StatisticsService:
    """将持久化统计结果与实时引擎元数据组合。"""

    def __init__(self, repository: StatisticsRepository, engine: AlertAnalyticsEngine):
        self.repository = repository
        self.engine = engine

    def list_sensors(self) -> Dict[str, Any]:
        """列出已有统计记录的通道及其最新统计值。"""
        with get_db_cursor() as cur:
            rows = self.repository.list_sensor_rows(cur)

        # 将数据库聚合结果与内存中的规则元数据合并。
        items: List[Dict[str, Any]] = []
        for row in rows:
            sensor_no = row["sensor_no"]
            channel = row["channel"]
            key = self.engine.make_key(sensor_no, channel)
            rule = self.engine.channel_states.get(key) or {}
            sensor_meta = self.engine.sensor_meta.get(sensor_no) or {}
            sensor_name = (
                rule.get("sensor_name")
                or sensor_meta.get("sensor_name")
                or sensor_no
            )
            station_name = (
                rule.get("station_name")
                or sensor_meta.get("station_name")
                or "未命名站点"
            )
            physical_quantity = rule.get("physical_quantity") or channel or "监测值"
            unit = rule.get("unit") or self.engine._infer_unit(physical_quantity, channel)

            items.append(
                {
                    "key": key,
                    "sensorNo": sensor_no,
                    "sensor_no": sensor_no,
                    "channel": channel,
                    "sensorName": sensor_name,
                    "sensor_name": sensor_name,
                    "stationName": station_name,
                    "station_name": station_name,
                    "physicalQuantity": physical_quantity,
                    "physical_quantity": physical_quantity,
                    "unit": unit,
                    "latestStatTime": serialize_val(row["latest_stat_time"]),
                    "valMax": serialize_val(row["val_max"]),
                    "valMin": serialize_val(row["val_min"]),
                    "valAvg": serialize_val(row["val_avg"]),
                    "maxTime": serialize_val(row["max_time"]),
                    "minTime": serialize_val(row["min_time"]),
                    "sampleCount": int(row["sample_count"] or 0),
                    "level1Upper": rule.get("level1_upper"),
                    "level1Lower": rule.get("level1_lower"),
                    "level2Upper": rule.get("level2_upper"),
                    "level2Lower": rule.get("level2_lower"),
                    "level3Upper": rule.get("level3_upper"),
                    "level3Lower": rule.get("level3_lower"),
                }
            )

        items.sort(key=lambda item: (item["stationName"], item["sensorNo"], item["channel"]))
        return {"items": items, "total": len(items)}

    def extremes(
        self,
        *,
        sensor_no: str,
        channel: str | None,
        period_type: str,
        start_time: str | None,
        end_time: str | None,
        limit: int,
    ) -> Dict[str, Any]:
        """返回极值时间序列和区间汇总。"""
        sensor_no = (sensor_no or "sensor-01").strip().lower()
        period_type = "1m"
        with get_db_cursor() as cur:
            # 调用方未指定通道时，自动解析默认通道。
            if channel is None:
                channel = self.repository.resolve_channel(cur, sensor_no)
            channel = channel.strip()
            rows = self.repository.extremes_rows(
                cur,
                sensor_no=sensor_no,
                channel=channel,
                period_type=period_type,
                start_time=(
                    parse_datetime(start_time, datetime(2026, 9, 21, 0, 0))
                    if start_time
                    else None
                ),
                end_time=(
                    parse_datetime(end_time, datetime(2026, 9, 30, 23, 59))
                    if end_time
                    else None
                ),
                limit=limit,
            )
            summary_row = self.repository.extremes_summary(
                cur,
                sensor_no=sensor_no,
                channel=channel,
                period_type=period_type,
                start_time=(
                    parse_datetime(start_time, datetime(2026, 9, 21, 0, 0))
                    if start_time
                    else None
                ),
                end_time=(
                    parse_datetime(end_time, datetime(2026, 9, 30, 23, 59))
                    if end_time
                    else None
                ),
            )

        # 组装图表数据点，并汇总整个时间区间的极值。
        key = self.engine.make_key(sensor_no, channel)
        rule = self.engine.channel_states.get(key) or {}
        sensor_meta = self.engine.sensor_meta.get(sensor_no) or {}
        series: List[Dict[str, Any]] = []
        window_max = None
        window_max_time = None
        window_min = None
        window_min_time = None
        total_samples = 0

        for row in rows:
            value_max = float(row["val_max"])
            value_min = float(row["val_min"])
            value_avg = float(
                row["val_avg"]
                if row["val_avg"] is not None
                else (value_max + value_min) / 2.0
            )
            sample_count = int(row["sample_count"] or 0)
            if window_max is None or value_max > window_max:
                window_max = value_max
                window_max_time = serialize_val(row["max_time"] or row["stat_time"])
            if window_min is None or value_min < window_min:
                window_min = value_min
                window_min_time = serialize_val(row["min_time"] or row["stat_time"])
            total_samples += sample_count
            series.append(
                {
                    "id": row["id"],
                    "statTime": serialize_val(row["stat_time"]),
                    "stat_time": serialize_val(row["stat_time"]),
                    "valMax": round(value_max, 5),
                    "val_max": round(value_max, 5),
                    "valMin": round(value_min, 5),
                    "val_min": round(value_min, 5),
                    "valAvg": round(value_avg, 5),
                    "val_avg": round(value_avg, 5),
                    "maxTime": serialize_val(row["max_time"] or row["stat_time"]),
                    "minTime": serialize_val(row["min_time"] or row["stat_time"]),
                    "sampleCount": sample_count,
                }
            )

        if int(summary_row.get("bucket_count") or 0) > 0:
            # 请求时间范围内的最终汇总以 SQL 聚合结果为准。
            window_max = float(summary_row["window_max"])
            window_max_time = serialize_val(summary_row["window_max_time"])
            window_min = float(summary_row["window_min"])
            window_min_time = serialize_val(summary_row["window_min_time"])
            summary_avg = float(summary_row["window_avg"] or 0.0)
            total_samples = int(summary_row["total_samples"] or 0)
        else:
            summary_avg = None

        sensor_name = (
            rule.get("sensor_name")
            or sensor_meta.get("sensor_name")
            or sensor_no
        )
        station_name = (
            rule.get("station_name")
            or sensor_meta.get("station_name")
            or "未命名站点"
        )
        physical_quantity = rule.get("physical_quantity") or channel or "监测值"
        unit = rule.get("unit") or self.engine._infer_unit(physical_quantity, channel)
        return {
            "sensorNo": sensor_no,
            "sensorName": sensor_name,
            "stationName": station_name,
            "channel": channel,
            "periodType": period_type,
            "physicalQuantity": physical_quantity,
            "unit": unit,
            "thresholds": {
                "level1Upper": rule.get("level1_upper"),
                "level1Lower": rule.get("level1_lower"),
                "level2Upper": rule.get("level2_upper"),
                "level2Lower": rule.get("level2_lower"),
                "level3Upper": rule.get("level3_upper"),
                "level3Lower": rule.get("level3_lower"),
            },
            "summary": {
                "bucketCount": len(series),
                "windowMax": round(window_max, 5) if window_max is not None else None,
                "windowMaxTime": window_max_time,
                "windowMin": round(window_min, 5) if window_min is not None else None,
                "windowMinTime": window_min_time,
                "windowAvg": round(summary_avg, 5) if summary_avg is not None else None,
                "totalSamples": total_samples,
            },
            "series": series,
        }
