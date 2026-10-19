from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict


class ThresholdUpdateRequest(BaseModel):
    level1_upper: Optional[float] = None
    level1_lower: Optional[float] = None
    level2_upper: Optional[float] = None
    level2_lower: Optional[float] = None
    level3_upper: Optional[float] = None
    level3_lower: Optional[float] = None
    level1Upper: Optional[float] = None
    level1Lower: Optional[float] = None
    level2Upper: Optional[float] = None
    level2Lower: Optional[float] = None
    level3Upper: Optional[float] = None
    level3Lower: Optional[float] = None
    offline_timeout_ms: Optional[int] = None
    offlineTimeoutMs: Optional[int] = None
    expected_interval_ms: Optional[int] = None
    expectedIntervalMs: Optional[int] = None
    confirm_points: Optional[int] = None
    confirmPoints: Optional[int] = None
    stuck_time_ms: Optional[int] = None
    stuckTimeMs: Optional[int] = None
    range_min: Optional[float] = None
    rangeMin: Optional[float] = None
    range_max: Optional[float] = None
    rangeMax: Optional[float] = None
    update_by: Optional[str] = None
    updateBy: Optional[str] = None


class ThresholdBatchUpdateRequest(ThresholdUpdateRequest):
    physical_quantity: Optional[str] = None
    physicalQuantity: Optional[str] = None
    threshold_ids: Optional[List[str]] = None
    thresholdIds: Optional[List[str]] = None


class ThresholdSimulateRequest(BaseModel):
    sensor_no: Optional[str] = None
    sensorNo: Optional[str] = None
    channel: Optional[str] = ""
    level1_upper: Optional[float] = None
    level1_lower: Optional[float] = None
    level2_upper: Optional[float] = None
    level2_lower: Optional[float] = None
    level3_upper: Optional[float] = None
    level3_lower: Optional[float] = None
    level1Upper: Optional[float] = None
    level1Lower: Optional[float] = None
    level2Upper: Optional[float] = None
    level2Lower: Optional[float] = None
    level3Upper: Optional[float] = None
    level3Lower: Optional[float] = None
    start_time: Optional[str] = None
    startTime: Optional[str] = None
    end_time: Optional[str] = None
    endTime: Optional[str] = None


class ThresholdRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    sensor_no: Optional[str] = None
    channel: Optional[str] = None
    create_by: Optional[str] = None
    range_min: Optional[float] = None
    range_max: Optional[float] = None
    stuck_time_ms: Optional[int] = None


class ThresholdListResponse(BaseModel):
    items: List[ThresholdRule]
    total: int
    physicalQuantities: List[str]
    stations: List[str]


class ThresholdUpdateResponse(BaseModel):
    success: bool
    threshold: ThresholdRule


class ThresholdBatchUpdateResponse(BaseModel):
    success: bool
    updatedCount: int
    physicalQuantity: Optional[str] = None


class ThresholdSimulationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    sensorNo: str
    channel: str
    totalPoints: int
    series: List[dict[str, Any]]
