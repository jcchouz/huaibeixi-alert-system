from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_threshold_service
from app.schemas.thresholds import (
    ThresholdBatchUpdateRequest,
    ThresholdBatchUpdateResponse,
    ThresholdListResponse,
    ThresholdSimulateRequest,
    ThresholdSimulationResponse,
    ThresholdUpdateRequest,
    ThresholdUpdateResponse,
)
from app.services.common import pick
from app.services.thresholds import ThresholdService

router = APIRouter(prefix="/thresholds", tags=["Thresholds"])


@router.get("", response_model=ThresholdListResponse)
def list_thresholds(
    sensorNo: Optional[str] = Query(None),
    sensor_no: Optional[str] = Query(None),
    physicalQuantity: Optional[str] = Query(None),
    physical_quantity: Optional[str] = Query(None),
    stationName: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    service: ThresholdService = Depends(get_threshold_service),
) -> Dict[str, Any]:
    return service.list_thresholds(
        sensor_no=pick(sensorNo, sensor_no),
        physical_quantity=pick(physicalQuantity, physical_quantity),
        station_name=stationName,
        keyword=keyword,
    )


@router.put("/{threshold_id}", response_model=ThresholdUpdateResponse)
def update_threshold(
    threshold_id: str,
    body: ThresholdUpdateRequest,
    service: ThresholdService = Depends(get_threshold_service),
) -> Dict[str, Any]:
    return service.update_threshold(threshold_id, body)


@router.post("/batch-update", response_model=ThresholdBatchUpdateResponse)
def batch_update_thresholds(
    body: ThresholdBatchUpdateRequest,
    service: ThresholdService = Depends(get_threshold_service),
) -> Dict[str, Any]:
    return service.batch_update(body)


@router.post("/simulate", response_model=ThresholdSimulationResponse)
def simulate_threshold(
    body: ThresholdSimulateRequest,
    service: ThresholdService = Depends(get_threshold_service),
) -> Dict[str, Any]:
    return service.simulate(body)
