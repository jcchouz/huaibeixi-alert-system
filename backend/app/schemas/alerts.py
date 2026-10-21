"""预警 API 请求和响应模型。"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class AlertAckRequest(BaseModel):
    """处理单条预警时接收的字段。"""

    alertStatus: Optional[int] = Field(None, description="1=已处理")
    alert_status: Optional[int] = None
    handleBy: Optional[str] = None
    handle_by: Optional[str] = None
    handlePhone: Optional[str] = None
    handle_phone: Optional[str] = None
    handleOpinion: Optional[str] = None
    handle_opinion: Optional[str] = None


class BatchAckRequest(BaseModel):
    """批量处理预警时使用的可选筛选条件。"""

    handleBy: Optional[str] = None
    handle_by: Optional[str] = None
    handlePhone: Optional[str] = None
    handle_phone: Optional[str] = None
    alertLevel: Optional[int] = None
    alert_level: Optional[int] = None
    alertCategory: Optional[int] = None
    alert_category: Optional[int] = None


class AlertCreateRequest(BaseModel):
    """人工创建预警时接收的字段。"""

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
    """经过引擎补充展示字段后的预警记录。"""

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
    """预警页签展示的已处理和未处理计数。"""

    total: int
    unhandled: int
    handled: int


class AlertListResponse(BaseModel):
    """分页预警列表响应。"""

    items: List[AlertRecord]
    total: int
    page: int
    pageSize: int
    totalPages: int
    statusCounts: AlertStatusCounts


class AlertPollResponse(BaseModel):
    """增量预警轮询响应。"""

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
    """创建和确认操作响应。"""

    success: bool
    alert: Optional[AlertRecord] = None


class AlertDeleteResponse(BaseModel):
    """预警删除响应。"""

    success: bool
    alertId: str


class BatchAlertActionResponse(BaseModel):
    """批量确认响应。"""

    success: bool
    ackedCount: int
    ackedIds: List[str]


class OverviewResponse(BaseModel):
    """仪表盘总览响应，允许携带额外字段。"""

    model_config = ConfigDict(extra="allow")

    totalSensors: int
    totalChannels: int
    activeAlertsCount: int
    alertsByType: List[dict[str, Any]]
    maxSeq: int
