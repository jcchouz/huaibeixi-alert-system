"""持久化统计 API 响应模型。"""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, ConfigDict


class StatSensor(BaseModel):
    """一个可用于统计查询的传感器和通道。"""

    model_config = ConfigDict(extra="allow")

    key: str
    sensorNo: str
    channel: str
    sensorName: str


class StatSensorListResponse(BaseModel):
    """已有统计记录的通道列表。"""

    items: List[StatSensor]
    total: int


class ExtremesResponse(BaseModel):
    """极值汇总和图表序列。"""

    model_config = ConfigDict(extra="allow")

    sensorNo: str
    channel: str
    periodType: str
    summary: dict[str, Any]
    series: List[dict[str, Any]]
