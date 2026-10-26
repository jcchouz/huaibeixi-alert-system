"""应用装配和生命周期管理。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from prometheus_client import make_asgi_app

from app.api.routers import alerts, health, overview, statistics, thresholds
from app.core.config import Settings, get_settings
from app.core.exceptions import install_exception_handlers
from app.core.logging import configure_logging
from app.domain.alert_types import (
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import close_db_pool, init_db_pool
from app.infrastructure.middleware import RequestContextMiddleware
from app.services.detection import AlertDetectionWorker


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建共享检测引擎、检测 Worker、中间件和路由。"""
    settings = settings or get_settings()
    configure_logging(settings)

    # 引擎是进程内唯一的检测状态持有者：
    #   - load_thresholds() 在启动时把 t_alert_threshold、t_sensor、t_sensor_channel
    #     组装成 engine.channel_states 里的运行时规则；
    #   - 阈值连续命中、离线、死值等跨轮次状态同样保存在 channel_states；
    #   - REST 接口和后台 Worker 共用这一个实例，接口看到的就是检测状态。
    engine_instance = AlertAnalyticsEngine(
        # 连续命中统一使用持续时间，适合不同采样频率的通道。
        confirm_seconds=settings.detection_confirm_seconds,
        stuck_seconds=settings.detection_stuck_seconds,
        offline_seconds=settings.detection_offline_seconds,
        # 五类预警的独立开关来自 backend/.env。开关只决定“是否写预警”，
        # 样本读取、连续命中计数和 t_stat_record 统计都不受开关影响。
        station_name=settings.station_name,
        alert_type_switches={
            # 监测值超过阈值。
            ALERT_TYPE_THRESHOLD: settings.detection_alert_threshold_enabled,
            # 单个传感器通道离线。
            ALERT_TYPE_OFFLINE: settings.detection_alert_offline_enabled,
            # 采集仪整体离线。
            ALERT_TYPE_DAQ_OFFLINE: settings.detection_alert_daq_offline_enabled,
            # 传感器持续输出相同值，判定为死值。
            ALERT_TYPE_STUCK: settings.detection_alert_stuck_enabled,
            # 监测值超出传感器物理量程。
            ALERT_TYPE_OOR: settings.detection_alert_oor_enabled,
        },
    )
    # Worker 只是驱动者：不保存规则，只按周期调用引擎推进两条处理链路。
    detection_worker = AlertDetectionWorker(engine_instance, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 接收请求前先初始化数据库连接池并加载检测元数据。
        init_db_pool(
            minconn=settings.db_pool_minconn,
            maxconn=settings.db_pool_maxconn,
        )
        # initialize() 只加载阈值、传感器元数据和概览数据量，不恢复历史状态。
        await asyncio.to_thread(engine_instance.initialize)
        # test 环境只跑单元测试和本地联调，不启动后台检测线程。
        if settings.detection_enabled and settings.environment != "test":
            # 以数据库当前时间为边界，只处理本次启动后的样本和完整窗口。
            await asyncio.to_thread(detection_worker.initialize)
            # start() 拉起两条 daemon 线程：
            #   alert-realtime-worker    按 DETECTION_INTERVAL_SECONDS 扫描原始点并写预警；
            #   alert-statistics-worker  按完整窗口写 t_stat_record。
            detection_worker.start()
        try:
            yield
        finally:
            # 先停止后台检测任务，再释放数据库连接。
            await asyncio.to_thread(detection_worker.stop)
            close_db_pool()

    app = FastAPI(
        title=settings.app_name,
        description=(
            "Bridge structural monitoring alerts and scheduled extreme-value analytics API"
        ),
        version=settings.service_version,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine_instance
    app.state.detection_worker = detection_worker
    app.dependency_overrides[get_settings] = lambda: settings

    # 统一处理错误、请求编号、主机校验和跨域策略。
    install_exception_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    if settings.allowed_hosts and "*" not in settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    api_prefix = settings.api_prefix.rstrip("/")
    # REST 接口主要用于本地联调和人工测试；生产检测由生命周期中的
    # 后台 Worker 独立完成。
    app.include_router(health.router, prefix=api_prefix)
    app.include_router(overview.router, prefix=api_prefix)
    app.include_router(alerts.router, prefix=api_prefix)
    app.include_router(thresholds.router, prefix=api_prefix)
    app.include_router(statistics.router, prefix=api_prefix)
    app.mount("/metrics", make_asgi_app())

    @app.get("/", include_in_schema=False)
    def service_root() -> dict[str, str]:
        return {
            "service": "alert-analysis-backend",
            "version": settings.service_version,
            "docs": "/docs",
            "health": f"{api_prefix}/health",
        }

    return app
