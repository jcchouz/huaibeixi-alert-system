"""统计通道和极值查询接口。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_statistics_service
from app.schemas.statistics import (
    ExtremesResponse,
    StatSensorListResponse,
)
from app.services.common import pick
from app.services.statistics import StatisticsService

router = APIRouter(prefix="/stats", tags=["Statistics"])


@router.get("/sensors", response_model=StatSensorListResponse)
def list_stat_sensors(
    service: StatisticsService = Depends(get_statistics_service),
) -> Dict[str, Any]:
    """列出已有持久化统计数据的通道。"""
    return service.list_sensors()


@router.get("/extremes", response_model=ExtremesResponse)
def get_stat_extremes(
    sensorNo: str = Query("sensor-01"),
    sensor_no: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    periodType: str = Query("1m"),
    period_type: Optional[str] = Query(None),
    startTime: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    endTime: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    limit: int = Query(500, ge=10, le=2000),
    service: StatisticsService = Depends(get_statistics_service),
) -> Dict[str, Any]:
    """返回指定传感器和通道的极值序列和汇总。"""
    return service.extremes(
        sensor_no=pick(sensor_no, sensorNo, "sensor-01"),
        channel=channel,
        period_type=pick(period_type, periodType, "1m"),
        start_time=pick(start_time, startTime),
        end_time=pick(end_time, endTime),
        limit=limit,
    )
