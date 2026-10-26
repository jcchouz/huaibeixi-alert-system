"""阈值查询、校验、更新和模拟业务流程。"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from fastapi import HTTPException

from app.core.serialization import serialize_row, serialize_val
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor
from app.repositories.statistics import StatisticsRepository
from app.repositories.thresholds import ThresholdRepository
from app.schemas.thresholds import (
    ThresholdBatchUpdateRequest,
    ThresholdSimulateRequest,
    ThresholdUpdateRequest,
)
from app.services.common import parse_datetime, pick


class ThresholdService:
    """管理阈值规则，并用历史统计对比拟调整方案。"""

    def __init__(
        self,
        repository: ThresholdRepository,
        statistics: StatisticsRepository,
        engine: AlertAnalyticsEngine,
    ):
        self.repository = repository
        self.statistics = statistics
        self.engine = engine

    def list_thresholds(
        self,
        *,
        sensor_no: str | None,
        physical_quantity: str | None,
        station_name: str | None,
        keyword: str | None,
    ) -> Dict[str, Any]:
        """返回内存中的规则列表和可用筛选值。"""
        all_rules = self.engine.channel_states.values()
        all_physical_quantities = sorted(
            {rule.get("physical_quantity") for rule in all_rules if rule.get("physical_quantity")}
        )
        all_stations = sorted(
            {rule.get("station_name") for rule in all_rules if rule.get("station_name")}
        )
        # 在已加载的规则快照上应用可选筛选条件。
        filtered: List[Dict[str, Any]] = []
        for rule in all_rules:
            if sensor_no and sensor_no.strip().lower() not in (rule.get("sensor_no") or "").lower():
                continue
            if physical_quantity and physical_quantity.strip() != (
                rule.get("physical_quantity") or ""
            ).strip():
                continue
            if station_name and station_name.strip() != (rule.get("station_name") or ""):
                continue
            if keyword:
                keyword_lower = keyword.strip().lower()
                haystack = (
                    f"{rule.get('sensor_no', '')} {rule.get('sensor_name', '')} "
                    f"{rule.get('physical_quantity', '')} {rule.get('channel', '')} "
                    f"{rule.get('station_name', '')} {rule.get('position', '')}"
                ).lower()
                if keyword_lower not in haystack:
                    continue
            filtered.append(rule)

        filtered.sort(key=lambda item: (item.get("sensor_no") or "", item.get("channel") or ""))
        return {
            "items": filtered,
            "total": len(filtered),
            "physicalQuantities": all_physical_quantities,
            "stations": all_stations,
        }

    def update_threshold(
        self,
        threshold_id: str,
        body: ThresholdUpdateRequest,
    ) -> Dict[str, Any]:
        """持久化单条阈值规则；运行中的检测规则保持不变。"""
        with get_db_cursor(commit=True) as cur:
            row = self.repository.fetch_rule(cur, threshold_id)
            if not row:
                raise HTTPException(status_code=404, detail=f"Threshold {threshold_id} not found")

            values = self._merged_values(row, body)
            self._validate_thresholds(values)
            updated_row = self.repository.update_rule(cur, threshold_id, values)

        return {"success": True, "threshold": serialize_row(updated_row)}

    def batch_update(
        self,
        body: ThresholdBatchUpdateRequest,
    ) -> Dict[str, Any]:
        """按物理量或指定编号批量更新阈值规则。"""
        physical_quantity = pick(body.physical_quantity, body.physicalQuantity)
        threshold_ids = pick(body.threshold_ids, body.thresholdIds)
        if not physical_quantity and not threshold_ids:
            raise HTTPException(
                status_code=400,
                detail="Must provide physical_quantity or threshold_ids",
            )

        overrides = {
            "level1_upper": pick(body.level1_upper, body.level1Upper),
            "level1_lower": pick(body.level1_lower, body.level1Lower),
            "level2_upper": pick(body.level2_upper, body.level2Upper),
            "level2_lower": pick(body.level2_lower, body.level2Lower),
            "level3_upper": pick(body.level3_upper, body.level3Upper),
            "level3_lower": pick(body.level3_lower, body.level3Lower),
        }

        with get_db_cursor(commit=True) as cur:
            # 在同一事务中校验并更新所有匹配规则。
            rows = self.repository.fetch_rules(
                cur,
                threshold_ids=threshold_ids,
                physical_quantity=physical_quantity,
            )
            if not rows:
                raise HTTPException(status_code=404, detail="No matching threshold rules found")

            for row in rows:
                values = self._merged_values(row, ThresholdUpdateRequest(**overrides))
                self._validate_thresholds(values)
                self.repository.update_rule(cur, str(row["id"]), values)

        return {
            "success": True,
            "updatedCount": len(rows),
            "physicalQuantity": physical_quantity,
        }

    def simulate(self, body: ThresholdSimulateRequest) -> Dict[str, Any]:
        """使用历史统计对比当前阈值和拟调整阈值。"""
        started = time.perf_counter()
        sensor_no = (pick(body.sensor_no, body.sensorNo, "sensor-01") or "sensor-01").strip().lower()
        channel = (body.channel or "").strip()
        key = self.engine.make_key(sensor_no, channel)
        current_rule = self.engine.channel_states.get(key)

        # 未精确匹配时，回退到该传感器下的任意通道。
        if not current_rule:
            for rule in self.engine.channel_states.values():
                if rule.get("sensor_no_lower") == sensor_no:
                    current_rule = rule
                    channel = rule.get("channel") or ""
                    break
        current_rule = current_rule or {}

        l1_upper = pick(
            body.level1_upper,
            body.level1Upper,
            current_rule.get("level1_upper", 10.0),
        )
        l1_lower = pick(
            body.level1_lower,
            body.level1Lower,
            current_rule.get("level1_lower", -10.0),
        )
        l2_upper = pick(
            body.level2_upper,
            body.level2Upper,
            current_rule.get("level2_upper", 8.0),
        )
        l2_lower = pick(
            body.level2_lower,
            body.level2Lower,
            current_rule.get("level2_lower", -8.0),
        )
        l3_upper = pick(
            body.level3_upper,
            body.level3Upper,
            current_rule.get("level3_upper", 6.0),
        )
        l3_lower = pick(
            body.level3_lower,
            body.level3Lower,
            current_rule.get("level3_lower", -6.0),
        )
        # 保留当前规则，同时允许请求字段覆盖对应阈值。
        current_thresholds = {
            "level1_upper": float(
                current_rule.get("level1_upper")
                if current_rule.get("level1_upper") is not None
                else l1_upper
            ),
            "level1_lower": float(
                current_rule.get("level1_lower")
                if current_rule.get("level1_lower") is not None
                else l1_lower
            ),
            "level2_upper": float(
                current_rule.get("level2_upper")
                if current_rule.get("level2_upper") is not None
                else l2_upper
            ),
            "level2_lower": float(
                current_rule.get("level2_lower")
                if current_rule.get("level2_lower") is not None
                else l2_lower
            ),
            "level3_upper": float(
                current_rule.get("level3_upper")
                if current_rule.get("level3_upper") is not None
                else l3_upper
            ),
            "level3_lower": float(
                current_rule.get("level3_lower")
                if current_rule.get("level3_lower") is not None
                else l3_lower
            ),
        }
        proposed_thresholds = {
            "level1_upper": float(l1_upper),
            "level1_lower": float(l1_lower),
            "level2_upper": float(l2_upper),
            "level2_lower": float(l2_lower),
            "level3_upper": float(l3_upper),
            "level3_lower": float(l3_lower),
        }
        self._validate_thresholds(proposed_thresholds)

        start_time = parse_datetime(
            pick(body.start_time, body.startTime),
            datetime(2026, 9, 28, 12, 0, 0),
        )
        end_time = parse_datetime(
            pick(body.end_time, body.endTime),
            datetime(2026, 9, 30, 18, 0, 0),
        )
        # 读取有限长度的历史序列，供两套规则共同评估。
        with get_db_cursor() as cur:
            rows = self.statistics.threshold_series_rows(
                cur,
                sensor_no=sensor_no,
                channel=channel,
                start_time=start_time,
                end_time=end_time,
            )

        def evaluate_point(
            value_max: float,
            value_min: float,
            thresholds: Dict[str, float],
        ) -> Tuple[int, float, float]:
            """使用一套阈值评估一个统计桶。"""
            eval_upper, eval_lower = value_max, value_min
            if eval_upper > thresholds["level1_upper"] or (
                eval_lower < thresholds["level1_lower"]
            ):
                return 1, eval_upper, eval_lower
            if eval_upper > thresholds["level2_upper"] or (
                eval_lower < thresholds["level2_lower"]
            ):
                return 2, eval_upper, eval_lower
            if eval_upper > thresholds["level3_upper"] or (
                eval_lower < thresholds["level3_lower"]
            ):
                return 3, eval_upper, eval_lower
            return 0, eval_upper, eval_lower

        # 分别统计拟调整阈值和当前阈值的命中数量。
        proposed_counts = {0: 0, 1: 0, 2: 0, 3: 0}
        current_counts = {0: 0, 1: 0, 2: 0, 3: 0}
        series: List[Dict[str, Any]] = []
        observed_max = None
        observed_min = None
        observed_max_time = None
        observed_min_time = None

        for row in rows:
            value_max = float(row["val_max"])
            value_min = float(row["val_min"])
            value_avg = float(
                row["val_avg"]
                if row["val_avg"] is not None
                else (value_max + value_min) / 2.0
            )
            proposed_level, eval_upper, eval_lower = evaluate_point(
                value_max,
                value_min,
                proposed_thresholds,
            )
            current_level, _, _ = evaluate_point(
                value_max,
                value_min,
                current_thresholds,
            )
            proposed_counts[proposed_level] += 1
            current_counts[current_level] += 1

            if observed_max is None or eval_upper > observed_max:
                observed_max = eval_upper
                observed_max_time = serialize_val(row.get("max_time") or row["stat_time"])
            if observed_min is None or eval_lower < observed_min:
                observed_min = eval_lower
                observed_min_time = serialize_val(row.get("min_time") or row["stat_time"])

            # 同时返回两套规则的评估结果，便于图表并列展示。
            series.append(
                {
                    "time": serialize_val(row["stat_time"]),
                    "valMax": round(value_max, 5),
                    "valMin": round(value_min, 5),
                    "valAvg": round(value_avg, 5),
                    "evalUpper": round(eval_upper, 5),
                    "evalLower": round(eval_lower, 5),
                    "hitLevel": proposed_level,
                    "currentHitLevel": current_level,
                }
            )

        total_points = len(series)
        proposed_alerts = proposed_counts[1] + proposed_counts[2] + proposed_counts[3]
        current_alerts = current_counts[1] + current_counts[2] + current_counts[3]
        return {
            "sensorNo": sensor_no,
            "sensorName": current_rule.get("sensor_name") or sensor_no,
            "stationName": current_rule.get("station_name") or self.engine.station_name,
            "channel": channel,
            "physicalQuantity": current_rule.get("physical_quantity") or channel,
            "unit": current_rule.get("unit") or "",
            "totalPoints": total_points,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
            "proposedThresholds": {
                "level1Upper": proposed_thresholds["level1_upper"],
                "level1Lower": proposed_thresholds["level1_lower"],
                "level2Upper": proposed_thresholds["level2_upper"],
                "level2Lower": proposed_thresholds["level2_lower"],
                "level3Upper": proposed_thresholds["level3_upper"],
                "level3Lower": proposed_thresholds["level3_lower"],
            },
            "currentThresholds": {
                "level1Upper": current_thresholds["level1_upper"],
                "level1Lower": current_thresholds["level1_lower"],
                "level2Upper": current_thresholds["level2_upper"],
                "level2Lower": current_thresholds["level2_lower"],
                "level3Upper": current_thresholds["level3_upper"],
                "level3Lower": current_thresholds["level3_lower"],
            },
            "proposedHits": {
                "level1": proposed_counts[1],
                "level2": proposed_counts[2],
                "level3": proposed_counts[3],
                "normal": proposed_counts[0],
                "totalAlerts": proposed_alerts,
                "alertRatePct": round(proposed_alerts * 100.0 / max(1, total_points), 2),
            },
            "currentHits": {
                "level1": current_counts[1],
                "level2": current_counts[2],
                "level3": current_counts[3],
                "normal": current_counts[0],
                "totalAlerts": current_alerts,
                "alertRatePct": round(current_alerts * 100.0 / max(1, total_points), 2),
            },
            "observedExtremes": {
                "maxVal": round(observed_max, 5) if observed_max is not None else 0.0,
                "minVal": round(observed_min, 5) if observed_min is not None else 0.0,
                "maxTime": observed_max_time,
                "minTime": observed_min_time,
            },
            "series": series,
        }

    def _merged_values(
        self,
        row: Dict[str, Any],
        body: ThresholdUpdateRequest,
    ) -> Dict[str, Any]:
        """将请求中的部分字段合并到现有规则记录上。"""
        return {
            "level1_upper": pick(
                body.level1_upper,
                body.level1Upper,
                row["level1_upper"],
            ),
            "level1_lower": pick(
                body.level1_lower,
                body.level1Lower,
                row["level1_lower"],
            ),
            "level2_upper": pick(
                body.level2_upper,
                body.level2Upper,
                row["level2_upper"],
            ),
            "level2_lower": pick(
                body.level2_lower,
                body.level2Lower,
                row["level2_lower"],
            ),
            "level3_upper": pick(
                body.level3_upper,
                body.level3Upper,
                row["level3_upper"],
            ),
            "level3_lower": pick(
                body.level3_lower,
                body.level3Lower,
                row["level3_lower"],
            ),
            "range_min": pick(
                body.range_min,
                body.rangeMin,
                row["range_min"],
            ),
            "range_max": pick(
                body.range_max,
                body.rangeMax,
                row["range_max"],
            ),
            "update_by": "web",
        }

    @staticmethod
    def _validate_thresholds(values: Dict[str, Any]) -> None:
        """校验阈值排序和物理量程边界。"""
        l1_upper = values.get("level1_upper")
        l2_upper = values.get("level2_upper")
        l3_upper = values.get("level3_upper")
        l1_lower = values.get("level1_lower")
        l2_lower = values.get("level2_lower")
        l3_lower = values.get("level3_lower")

        if None not in (l1_upper, l2_upper, l3_upper) and not (
            float(l1_upper) >= float(l2_upper) >= float(l3_upper)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"上限阈值必须满足 一级上限({l1_upper}) >= 二级上限({l2_upper}) >= 三级上限({l3_upper})",
            )
        if None not in (l1_lower, l2_lower, l3_lower) and not (
            float(l1_lower) <= float(l2_lower) <= float(l3_lower)
        ):
            raise HTTPException(
                status_code=400,
                detail=f"下限阈值必须满足 一级下限({l1_lower}) <= 二级下限({l2_lower}) <= 三级下限({l3_lower})",
            )
        range_min = values.get("range_min")
        range_max = values.get("range_max")
        if (
            range_min is not None
            and range_max is not None
            and float(range_min) > float(range_max)
        ):
            raise HTTPException(
                status_code=400,
                detail="量程最小值不能大于量程最大值",
            )
