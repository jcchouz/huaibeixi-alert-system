from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    get_alert_service,
    require_roles,
)
from app.core.security import Principal
from app.schemas.alerts import (
    AlertAckRequest,
    AlertActionResponse,
    AlertCreateRequest,
    AlertDeleteResponse,
    AlertListResponse,
    AlertPollResponse,
    BatchAckRequest,
    BatchAlertActionResponse,
)
from app.services.alerts import AlertService
from app.services.common import pick

router = APIRouter(prefix="/alerts", tags=["Alerts"])


@router.get("/poll", response_model=AlertPollResponse)
def poll_alerts(
    afterSeq: int = Query(0, alias="afterSeq"),
    after_seq: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    service: AlertService = Depends(get_alert_service),
    _: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> Dict[str, Any]:
    cursor_seq = after_seq if after_seq is not None else afterSeq
    return service.poll(cursor_seq, limit)


@router.get("", response_model=AlertListResponse)
def list_alerts(
    alertLevel: Optional[int] = Query(None),
    alert_level: Optional[int] = Query(None),
    alertStatus: Optional[int] = Query(None),
    alert_status: Optional[int] = Query(None),
    alertType: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    alertCategory: Optional[int] = Query(None, ge=0, le=1),
    alert_category: Optional[int] = Query(None, ge=0, le=1),
    sensorNo: Optional[str] = Query(None),
    sensor_no: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    page_size: Optional[int] = Query(None),
    service: AlertService = Depends(get_alert_service),
    _: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> Dict[str, Any]:
    return service.list_alerts(
        alert_level=pick(alertLevel, alert_level),
        alert_status=pick(alertStatus, alert_status),
        alert_type=pick(alertType, alert_type),
        alert_category=pick(alertCategory, alert_category),
        sensor_no=pick(sensorNo, sensor_no),
        keyword=keyword,
        page=page,
        page_size=int(pick(page_size, pageSize, 20)),
    )


@router.post("", response_model=AlertActionResponse)
def create_alert(
    body: AlertCreateRequest,
    service: AlertService = Depends(get_alert_service),
    _: Principal = Depends(require_roles("operator", "admin")),
) -> Dict[str, Any]:
    return service.create_alert(body)


@router.post("/{alert_id}/ack", response_model=AlertActionResponse)
def ack_alert(
    alert_id: str,
    body: AlertAckRequest,
    service: AlertService = Depends(get_alert_service),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> Dict[str, Any]:
    return service.acknowledge(alert_id, body, principal.username)


@router.delete("/{alert_id}", response_model=AlertDeleteResponse)
def delete_alert(
    alert_id: str,
    service: AlertService = Depends(get_alert_service),
    _: Principal = Depends(require_roles("operator", "admin")),
) -> Dict[str, Any]:
    return service.delete_alert(alert_id)


@router.post("/ack-all", response_model=BatchAlertActionResponse)
def ack_all_alerts(
    body: Optional[BatchAckRequest] = None,
    service: AlertService = Depends(get_alert_service),
    principal: Principal = Depends(require_roles("operator", "admin")),
) -> Dict[str, Any]:
    return service.acknowledge_all(body, principal.username)
