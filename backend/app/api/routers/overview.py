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
    return service.get_overview()
