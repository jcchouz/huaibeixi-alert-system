from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_engine
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor
from app.schemas.common import HealthResponse

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
def health_check(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> dict:
    return {
        "status": "ok",
        "service": "huaibeixi-alert-backend",
        "engine": engine.get_status(),
    }


@router.get("/ready", response_model=HealthResponse)
def readiness_check(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> dict:
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT 1 AS ready")
            cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="数据库尚未就绪") from exc
    return {
        "status": "ready",
        "service": "huaibeixi-alert-backend",
        "engine": engine.get_status(),
    }
