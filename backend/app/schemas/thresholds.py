"""阈值管理 API 请求和响应模型。"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict


class ThresholdUpdateRequest(BaseModel):
    """阈值和物理量程的部分更新字段。"""

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
    expected_interval_ms: Optional[int] = None
    expectedIntervalMs: Optional[int] = None
    range_min: Optional[float] = None
    rangeMin: Optional[float] = None
    range_max: Optional[float] = None
    rangeMax: Optional[float] = None


class ThresholdBatchUpdateRequest(ThresholdUpdateRequest):
    """批量选择条件以及可选的公共阈值覆盖值。"""

    physical_quantity: Optional[str] = None
    physicalQuantity: Optional[str] = None
    threshold_ids: Optional[List[str]] = None
    thresholdIds: Optional[List[str]] = None


class ThresholdSimulateRequest(BaseModel):
    """阈值模拟使用的拟调整阈值和时间范围。"""

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
    """阈值管理接口返回的规则记录。"""

    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    sensor_no: Optional[str] = None
    channel: Optional[str] = None
    create_by: Optional[str] = None
    range_min: Optional[float] = None
    range_max: Optional[float] = None


class ThresholdListResponse(BaseModel):
    """筛选后的阈值列表和可用筛选项。"""

    items: List[ThresholdRule]
    total: int
    physicalQuantities: List[str]
    stations: List[str]


class ThresholdUpdateResponse(BaseModel):
    """单条阈值更新响应。"""

    success: bool
    threshold: ThresholdRule


class ThresholdBatchUpdateResponse(BaseModel):
    """批量阈值更新响应。"""

    success: bool
    updatedCount: int
    physicalQuantity: Optional[str] = None


class ThresholdSimulationResponse(BaseModel):
    """阈值对比序列和汇总。"""

    model_config = ConfigDict(extra="allow")

    sensorNo: str
    channel: str
    totalPoints: int
    series: List[dict[str, Any]]
