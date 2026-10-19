from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_statistics_service, require_roles
from app.core.security import Principal
from app.schemas.statistics import (
    ExtremesResponse,
    StatSensorListResponse,
    WindRoseResponse,
)
from app.services.common import pick
from app.services.statistics import StatisticsService

router = APIRouter(prefix="/stats", tags=["Statistics"])


@router.get("/sensors", response_model=StatSensorListResponse)
def list_stat_sensors(
    service: StatisticsService = Depends(get_statistics_service),
    _: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> Dict[str, Any]:
    return service.list_sensors()


@router.get("/extremes", response_model=ExtremesResponse)
def get_stat_extremes(
    sensorNo: str = Query("stn-12-b"),
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
    _: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> Dict[str, Any]:
    return service.extremes(
        sensor_no=pick(sensor_no, sensorNo, "stn-12-b"),
        channel=channel,
        period_type=pick(period_type, periodType, "1m"),
        start_time=pick(start_time, startTime),
        end_time=pick(end_time, endTime),
        limit=limit,
    )


@router.get("/wind-rose", response_model=WindRoseResponse)
def get_wind_rose(
    sensorNo: str = Query("all", description="wea-01 | fhj-01 | all"),
    sensor_no: Optional[str] = Query(None),
    startTime: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    endTime: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    includeDemotyphoon: bool = Query(False),
    includeDemoTyphoon: bool = Query(False),
    service: StatisticsService = Depends(get_statistics_service),
    _: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> Dict[str, Any]:
    return service.wind_rose(
        sensor_no=pick(sensor_no, sensorNo, "all"),
        start_time=pick(start_time, startTime),
        end_time=pick(end_time, endTime),
        include_demo_typhoon=bool(includeDemotyphoon or includeDemoTyphoon),
    )
