from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AlertAckRequest(BaseModel):
    alertStatus: Optional[int] = Field(None, description="1=已处理")
    alert_status: Optional[int] = None
    handleBy: Optional[str] = None
    handle_by: Optional[str] = None
    handlePhone: Optional[str] = None
    handle_phone: Optional[str] = None
    handleOpinion: Optional[str] = None
    handle_opinion: Optional[str] = None


class BatchAckRequest(BaseModel):
    handleBy: Optional[str] = None
    handle_by: Optional[str] = None
    handlePhone: Optional[str] = None
    handle_phone: Optional[str] = None
    alertLevel: Optional[int] = None
    alert_level: Optional[int] = None
    alertCategory: Optional[int] = None
    alert_category: Optional[int] = None


class AlertCreateRequest(BaseModel):
    sensorNo: Optional[str] = None
    sensor_no: Optional[str] = None
    channel: Optional[str] = ""
    alertType: Optional[str] = None
    alert_type: Optional[str] = None
    alertLevel: Optional[int] = None
    alert_level: Optional[int] = None
    triggerVal: Optional[float] = None
    trigger_val: Optional[float] = None


class AlertRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    seq: Optional[int] = None
    sensor_no: Optional[str] = None
    channel: Optional[str] = None
    alert_type: Optional[str] = None
    alert_category: Optional[int] = None
    alert_level: Optional[int] = None
    alert_status: Optional[int] = None
    create_time: Optional[str] = None
    update_time: Optional[str] = None
    handle_by: Optional[str] = None
    handle_phone: Optional[str] = None
    handle_time: Optional[str] = None
    handle_opinion: Optional[str] = None


class AlertStatusCounts(BaseModel):
    total: int
    unhandled: int
    handled: int


class AlertListResponse(BaseModel):
    items: List[AlertRecord]
    total: int
    page: int
    pageSize: int
    totalPages: int
    statusCounts: AlertStatusCounts


class AlertPollResponse(BaseModel):
    items: List[AlertRecord]
    maxSeq: int
    max_seq: int
    latestSeq: int
    latest_seq: int
    hasMore: bool
    has_more: bool
    serverTime: str
    elapsedMs: float


class AlertActionResponse(BaseModel):
    success: bool
    alert: Optional[AlertRecord] = None


class AlertDeleteResponse(BaseModel):
    success: bool
    alertId: str


class BatchAlertActionResponse(BaseModel):
    success: bool
    ackedCount: int
    ackedIds: List[str]


class OverviewResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    totalSensors: int
    totalChannels: int
    activeAlertsCount: int
    alertsByType: List[dict[str, Any]]
    maxSeq: int
