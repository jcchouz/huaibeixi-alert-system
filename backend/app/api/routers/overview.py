"""仪表盘总览接口。"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.api.deps import get_overview_service
from app.schemas.alerts import OverviewResponse
from app.services.overview import OverviewService

router = APIRouter(tags=["Overview"])


@router.get("/overview", response_model=OverviewResponse)
def get_overview(
    service: OverviewService = Depends(get_overview_service),
) -> Dict[str, Any]:
    """返回仪表盘 KPI 和最近预警快照。"""
    return service.get_overview()
