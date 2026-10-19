from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.api.deps import get_engine_control_service, require_roles
from app.core.security import Principal
from app.schemas.engine import EngineReplayRequest
from app.services.engine_control import EngineControlService

router = APIRouter(prefix="/engine", tags=["Engine"])


@router.post("/replay")
def control_engine_replay(
    body: EngineReplayRequest,
    service: EngineControlService = Depends(get_engine_control_service),
    _: Principal = Depends(require_roles("admin")),
) -> Dict[str, Any]:
    return service.control(body)
