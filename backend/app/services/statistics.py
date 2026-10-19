from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Dict, List

from app.core.serialization import serialize_val
from app.domain.engine import DIRECTION_SECTORS_16, SPEED_BINS_7, AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor
from app.repositories.statistics import StatisticsRepository
from app.services.common import parse_datetime

DIRECTION_CN_16 = [
    "北 (N)",
    "东北偏北 (NNE)",
    "东北 (NE)",
    "东北偏东 (ENE)",
    "东 (E)",
    "东南偏东 (ESE)",
    "东南 (SE)",
    "东南偏南 (SSE)",
    "南 (S)",
    "西南偏南 (SSW)",
    "西南 (SW)",
    "西南偏西 (WSW)",
    "西 (W)",
    "西北偏西 (WNW)",
    "西北 (NW)",
    "西北偏北 (NNW)",
]


class StatisticsService:
    def __init__(self, repository: StatisticsRepository, engine: AlertAnalyticsEngine):
        self.repository = repository
        self.engine = engine

    def list_sensors(self) -> Dict[str, Any]:
        with get_db_cursor() as cur:
            rows = self.repository.list_sensor_rows(cur)

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
                or ("玉环站" if "_YH" in sensor_name else "淮北西站")
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
                    "valStd": serialize_val(row["val_std"]),
                    "maxTime": serialize_val(row["max_time"]),
                    "minTime": serialize_val(row["min_time"]),
                    "sampleCount": int(row["sample_count"] or 0),
                    "level1Upper": rule.get("level1_upper"),
                    "level1Lower": rule.get("level1_lower"),
                    "level2Upper": rule.get("level2_upper"),
                    "level2Lower": rule.get("level2_lower"),
                    "level3Upper": rule.get("level3_upper"),
                    "level3Lower": rule.get("level3_lower"),
                    "removeDcOffset": rule.get("remove_dc_offset", False),
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
        sensor_no = (sensor_no or "stn-12-b").strip().lower()
        period_type = (period_type or "1m").strip()
        with get_db_cursor() as cur:
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

        key = self.engine.make_key(sensor_no, channel)
        rule = self.engine.channel_states.get(key) or {}
        sensor_meta = self.engine.sensor_meta.get(sensor_no) or {}
        series: List[Dict[str, Any]] = []
        window_max = None
        window_max_time = None
        window_min = None
        window_min_time = None
        average_sum = 0.0
        total_samples = 0

        for row in rows:
            value_max = float(row["val_max"])
            value_min = float(row["val_min"])
            value_avg = float(
                row["val_avg"]
                if row["val_avg"] is not None
                else (value_max + value_min) / 2.0
            )
            value_std = float(row["val_std"] or 0.0)
            sample_count = int(row["sample_count"] or 0)
            if window_max is None or value_max > window_max:
                window_max = value_max
                window_max_time = serialize_val(row["max_time"] or row["stat_time"])
            if window_min is None or value_min < window_min:
                window_min = value_min
                window_min_time = serialize_val(row["min_time"] or row["stat_time"])
            average_sum += value_avg
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
                    "valStd": round(value_std, 5),
                    "val_std": round(value_std, 5),
                    "maxTime": serialize_val(row["max_time"] or row["stat_time"]),
                    "minTime": serialize_val(row["min_time"] or row["stat_time"]),
                    "sampleCount": sample_count,
                }
            )

        sensor_name = (
            rule.get("sensor_name")
            or sensor_meta.get("sensor_name")
            or sensor_no
        )
        station_name = (
            rule.get("station_name")
            or sensor_meta.get("station_name")
            or ("玉环站" if "_YH" in sensor_name else "淮北西站")
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
                "removeDcOffset": rule.get("remove_dc_offset", False),
            },
            "summary": {
                "bucketCount": len(series),
                "windowMax": round(window_max, 5) if window_max is not None else None,
                "windowMaxTime": window_max_time,
                "windowMin": round(window_min, 5) if window_min is not None else None,
                "windowMinTime": window_min_time,
                "windowAvg": round(average_sum / len(series), 5) if series else None,
                "totalSamples": total_samples,
            },
            "series": series,
        }

    def wind_rose(
        self,
        *,
        sensor_no: str,
        start_time: str | None,
        end_time: str | None,
        include_demo_typhoon: bool,
    ) -> Dict[str, Any]:
        sensor_no = (sensor_no or "all").strip().lower()
        with get_db_cursor() as cur:
            rows = self.repository.wind_rose_rows(
                cur,
                sensor_no=sensor_no,
                start_time=(
                    parse_datetime(start_time, datetime(2026, 9, 24, 0, 0))
                    if start_time
                    else None
                ),
                end_time=(
                    parse_datetime(end_time, datetime(2026, 9, 30, 23, 59))
                    if end_time
                    else None
                ),
            )

        matrix_count = [[0 for _ in range(7)] for _ in range(16)]
        calm_count = 0
        small_wind_count = 0
        max_gust = 0.0
        max_gust_time = None
        sum_vector_speed = 0.0
        sum_scalar_speed = 0.0
        sum_turbulence = 0.0
        total_samples = 0
        recent_records: List[Dict[str, Any]] = []

        def speed_to_bin(speed: float) -> int:
            if speed <= 0.2:
                return -1
            if speed <= 1.5:
                return 0
            if speed <= 3.3:
                return 1
            if speed <= 5.4:
                return 2
            if speed <= 7.9:
                return 3
            if speed <= 10.7:
                return 4
            if speed <= 13.8:
                return 5
            return 6

        for index, row in enumerate(rows):
            ext = row.get("ext_json") or {}
            vector_speed = float(ext.get("vector_speed") or row["val_avg"] or 0.0)
            scalar_speed = float(ext.get("scalar_speed") or vector_speed)
            vector_direction = float(ext.get("vector_direction") or 45.0)
            value_max = float(row["val_max"] or vector_speed)
            value_std = float(row["val_std"] or 0.0)
            sample_count = int(row["sample_count"] or 0)

            sector_index = int(math.floor(((vector_direction + 11.25) % 360.0) / 22.5)) % 16
            speed_index = speed_to_bin(vector_speed)
            if speed_index == -1:
                calm_count += 1
                small_wind_count += 1
            else:
                matrix_count[sector_index][speed_index] += 1
                if vector_speed <= 0.5:
                    small_wind_count += 1

            if value_max > max_gust:
                max_gust = value_max
                max_gust_time = serialize_val(row["max_time"] or row["stat_time"])
            sum_vector_speed += vector_speed
            sum_scalar_speed += scalar_speed
            turbulence_intensity = value_std / scalar_speed if scalar_speed > 0.1 else 0.0
            sum_turbulence += turbulence_intensity
            total_samples += sample_count

            if index < 40:
                recent_records.append(
                    {
                        "statTime": serialize_val(row["stat_time"]),
                        "sensorNo": row["sensor_no"],
                        "sensorName": (
                            "小型气象站_YH"
                            if row["sensor_no"] == "wea-01"
                            else "超声风速仪(FHJ-01)"
                        ),
                        "stationName": (
                            "玉环站"
                            if row["sensor_no"] == "wea-01"
                            else "淮北西站"
                        ),
                        "vectorSpeed": round(vector_speed, 3),
                        "scalarSpeed": round(scalar_speed, 3),
                        "vectorDirection": round(vector_direction, 2),
                        "directionLabel": DIRECTION_SECTORS_16[sector_index],
                        "directionCn": DIRECTION_CN_16[sector_index],
                        "gustMax": round(value_max, 3),
                        "valStd": round(value_std, 3),
                        "turbulenceIntensity": round(turbulence_intensity, 3),
                        "sampleCount": sample_count,
                    }
                )

        if include_demo_typhoon:
            typhoon_profile = [
                (2, [6, 18, 28, 22, 14, 8, 4]),
                (3, [5, 16, 26, 24, 16, 9, 5]),
                (4, [4, 12, 20, 18, 11, 6, 3]),
                (1, [4, 11, 16, 12, 7, 3, 1]),
                (5, [3, 9, 14, 10, 6, 2, 1]),
                (0, [3, 8, 10, 6, 3, 1, 0]),
                (6, [2, 6, 9, 6, 3, 1, 0]),
                (15, [2, 5, 7, 4, 2, 0, 0]),
                (7, [2, 4, 5, 3, 1, 0, 0]),
                (14, [1, 3, 4, 2, 1, 0, 0]),
                (8, [1, 3, 3, 2, 0, 0, 0]),
                (9, [1, 2, 2, 1, 0, 0, 0]),
                (10, [1, 2, 2, 1, 0, 0, 0]),
                (11, [1, 2, 1, 1, 0, 0, 0]),
                (12, [1, 2, 2, 1, 0, 0, 0]),
                (13, [1, 2, 3, 1, 1, 0, 0]),
            ]
            for sector_index, bin_counts in typhoon_profile:
                for speed_index, count in enumerate(bin_counts):
                    matrix_count[sector_index][speed_index] += count
            max_gust = max(max_gust, 19.85)
            max_gust_time = max_gust_time or "2026-09-29 14:20:00"

        total_buckets = sum(sum(row) for row in matrix_count) + calm_count
        denominator = max(1, total_buckets)
        matrix_pct = [
            [round(count * 100.0 / denominator, 2) for count in row]
            for row in matrix_count
        ]
        sector_totals_pct = [
            round(sum(row) * 100.0 / denominator, 2)
            for row in matrix_count
        ]
        sector_totals_count = [sum(row) for row in matrix_count]
        dominant_index = (
            int(max(range(16), key=lambda index: sector_totals_count[index]))
            if total_buckets > 0
            else 2
        )
        row_count = max(1, len(rows))
        return {
            "sensorNo": sensor_no,
            "includeDemoTyphoon": include_demo_typhoon,
            "sectors": [
                {
                    "index": index,
                    "code": DIRECTION_SECTORS_16[index],
                    "label": DIRECTION_CN_16[index],
                    "angleDeg": index * 22.5,
                    "totalPct": sector_totals_pct[index],
                    "totalCount": sector_totals_count[index],
                }
                for index in range(16)
            ],
            "directionLabels": DIRECTION_SECTORS_16,
            "directionLabelsCn": DIRECTION_CN_16,
            "speedBins": SPEED_BINS_7,
            "matrixPct": matrix_pct,
            "matrixCount": matrix_count,
            "sectorTotalsPct": sector_totals_pct,
            "summary": {
                "totalBuckets": total_buckets,
                "rawDbBuckets": len(rows),
                "totalSamples": total_samples,
                "calmCount": calm_count,
                "calmRatePct": round(calm_count * 100.0 / denominator, 2),
                "smallWindRatePct": round(small_wind_count * 100.0 / denominator, 2),
                "maxWindSpeed": round(max_gust, 2),
                "maxWindTime": max_gust_time,
                "avgVectorSpeed": (
                    round(sum_vector_speed / row_count, 2)
                    if not include_demo_typhoon
                    else 5.86
                ),
                "avgScalarSpeed": (
                    round(sum_scalar_speed / row_count, 2)
                    if not include_demo_typhoon
                    else 6.42
                ),
                "dominantSector": DIRECTION_SECTORS_16[dominant_index],
                "dominantSectorCn": DIRECTION_CN_16[dominant_index],
                "dominantDirectionDeg": dominant_index * 22.5,
                "dominantSectorPct": sector_totals_pct[dominant_index],
                "turbulenceIntensityAvg": round(sum_turbulence / row_count, 3),
            },
            "recentRecords": recent_records,
        }
