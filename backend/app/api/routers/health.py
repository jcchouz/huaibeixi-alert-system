"""存活检查和数据库就绪检查接口。"""

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import get_engine
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor
from app.schemas.common import HealthResponse

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
def health_check(
    request: Request,
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> dict:
    """不访问数据库，返回进程存活以及引擎和 Worker 状态。"""
    response = {
        "status": "ok",
        "service": "alert-analysis-backend",
        "engine": engine.get_status(),
    }
    worker = getattr(request.app.state, "detection_worker", None)
    if worker is not None:
        response["detection"] = worker.get_status()
    return response


@router.get("/ready", response_model=HealthResponse)
def readiness_check(
    request: Request,
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> dict:
    """数据库查询成功后才返回就绪状态。"""
    try:
        with get_db_cursor() as cur:
            cur.execute("SELECT 1 AS ready")
            cur.fetchone()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="数据库尚未就绪") from exc
    response = {
        "status": "ready",
        "service": "alert-analysis-backend",
        "engine": engine.get_status(),
    }
    worker = getattr(request.app.state, "detection_worker", None)
    if worker is not None:
        response["detection"] = worker.get_status()
    return response
