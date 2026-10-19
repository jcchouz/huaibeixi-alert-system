from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Query, Request, status

from app.core.config import Settings, get_settings
from app.core.security import (
    Principal,
    SecurityService,
    get_current_principal,
    get_security_service,
    require_roles,
)
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.sse import SSEHub
from app.repositories.alerts import AlertRepository
from app.repositories.statistics import StatisticsRepository
from app.repositories.thresholds import ThresholdRepository
from app.services.alerts import AlertService
from app.services.engine_control import EngineControlService
from app.services.overview import OverviewService
from app.services.statistics import StatisticsService
from app.services.thresholds import ThresholdService


def get_engine(request: Request) -> AlertAnalyticsEngine:
    return request.app.state.engine


def get_sse_hub(request: Request) -> SSEHub:
    return request.app.state.sse_hub


def get_alert_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
    sse: SSEHub = Depends(get_sse_hub),
) -> AlertService:
    return AlertService(AlertRepository(), engine, sse)


def get_threshold_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> ThresholdService:
    return ThresholdService(ThresholdRepository(), StatisticsRepository(), engine)


def get_statistics_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> StatisticsService:
    return StatisticsService(StatisticsRepository(), engine)


def get_overview_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> OverviewService:
    return OverviewService(AlertRepository(), engine)


def get_engine_control_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
    settings: Settings = Depends(get_settings),
) -> EngineControlService:
    return EngineControlService(engine, settings)


def get_sse_principal(
    request: Request,
    token: Optional[str] = Query(None),
    settings: Settings = Depends(get_settings),
    security: SecurityService = Depends(get_security_service),
) -> Principal:
    if not settings.auth_enabled:
        return Principal(username="local-admin", role="admin", auth_mode="disabled")

    raw_token = token
    if not raw_token:
        authorization = request.headers.get("Authorization", "")
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer":
            raw_token = credentials.strip()
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少访问令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return security.decode_token(raw_token)


__all__ = [
    "get_alert_service",
    "get_current_principal",
    "get_engine",
    "get_engine_control_service",
    "get_overview_service",
    "get_settings",
    "get_sse_hub",
    "get_sse_principal",
    "get_statistics_service",
    "get_threshold_service",
    "require_roles",
]
