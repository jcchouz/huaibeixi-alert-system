from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.api.deps import get_overview_service, require_roles
from app.core.security import Principal
from app.schemas.alerts import OverviewResponse
from app.services.overview import OverviewService

router = APIRouter(tags=["Overview"])


@router.get("/overview", response_model=OverviewResponse)
def get_overview(
    service: OverviewService = Depends(get_overview_service),
    _: Principal = Depends(require_roles("viewer", "operator", "admin")),
) -> Dict[str, Any]:
    return service.get_overview()
