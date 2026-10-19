from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, ConfigDict


class StatSensor(BaseModel):
    model_config = ConfigDict(extra="allow")

    key: str
    sensorNo: str
    channel: str
    sensorName: str


class StatSensorListResponse(BaseModel):
    items: List[StatSensor]
    total: int


class ExtremesResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    sensorNo: str
    channel: str
    periodType: str
    summary: dict[str, Any]
    series: List[dict[str, Any]]


class WindRoseResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    sensorNo: str
    sectors: List[dict[str, Any]]
    speedBins: List[dict[str, Any]]
    matrixPct: List[List[float]]
    matrixCount: List[List[int]]
    summary: dict[str, Any]
