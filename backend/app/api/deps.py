"""为 API 请求装配业务服务的依赖工厂。"""

from __future__ import annotations

from fastapi import Depends, Request

from app.domain.engine import AlertAnalyticsEngine
from app.repositories.alerts import AlertRepository
from app.repositories.statistics import StatisticsRepository
from app.repositories.thresholds import ThresholdRepository
from app.services.alerts import AlertService
from app.services.overview import OverviewService
from app.services.statistics import StatisticsService
from app.services.thresholds import ThresholdService


def get_engine(request: Request) -> AlertAnalyticsEngine:
    """返回应用启动时创建的进程级检测引擎。"""
    return request.app.state.engine


def get_alert_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> AlertService:
    """创建预警业务服务并注入仓储依赖。"""
    return AlertService(AlertRepository(), engine)


def get_threshold_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> ThresholdService:
    """创建阈值管理接口使用的业务服务。"""
    return ThresholdService(ThresholdRepository(), StatisticsRepository(), engine)


def get_statistics_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> StatisticsService:
    """创建统计查询业务服务。"""
    return StatisticsService(StatisticsRepository(), engine)


def get_overview_service(
    engine: AlertAnalyticsEngine = Depends(get_engine),
) -> OverviewService:
    """创建总览页汇总业务服务。"""
    return OverviewService(AlertRepository(), engine)


__all__ = [
    "get_alert_service", # 创建告警业务服务
    "get_engine", # 从 request.app.state.engine 获取全局共享的 AlertAnalyticsEngine 实例
    "get_overview_service", # 创建总览业务服务
    "get_statistics_service", # 创建统计业务服务
    "get_threshold_service", # 创建阈值业务服务
]
