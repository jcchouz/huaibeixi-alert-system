from __future__ import annotations

from fastapi import Depends, Request

from app.core.security import get_current_principal, require_roles
from app.domain.engine import AlertAnalyticsEngine
from app.repositories.alerts import AlertRepository
from app.repositories.statistics import StatisticsRepository
from app.repositories.thresholds import ThresholdRepository
from app.services.alerts import AlertService
from app.services.overview import OverviewService
from app.services.statistics import StatisticsService
from app.services.thresholds import ThresholdService


def get_engine(request: Request) -> AlertAnalyticsEngine:
    return request.app.state.engine


def get_alert_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> AlertService:
    return AlertService(AlertRepository(), engine)


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


__all__ = [
    "get_alert_service",
    "get_current_principal",
    "get_engine",
    "get_overview_service",
    "get_statistics_service",
    "get_threshold_service",
    "require_roles",
]
