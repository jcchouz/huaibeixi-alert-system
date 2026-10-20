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
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import close_db_pool, init_db_pool
from app.infrastructure.middleware import RequestContextMiddleware
from app.services.detection import AlertDetectionWorker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    engine_instance = AlertAnalyticsEngine(
        confirm_points=settings.detection_confirm_points,
        stuck_points=settings.detection_stuck_points,
        offline_points=settings.detection_offline_points,
    )
    detection_worker = AlertDetectionWorker(engine_instance, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db_pool(
            minconn=settings.db_pool_minconn,
            maxconn=settings.db_pool_maxconn,
        )
        await asyncio.to_thread(engine_instance.initialize)
        if settings.detection_enabled and settings.environment != "test":
            detection_worker.start()
        try:
            yield
        finally:
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
    app.include_router(health.router, prefix=api_prefix)
    app.include_router(overview.router, prefix=api_prefix)
    app.include_router(alerts.router, prefix=api_prefix)
    app.include_router(thresholds.router, prefix=api_prefix)
    app.include_router(statistics.router, prefix=api_prefix)
    app.mount("/metrics", make_asgi_app())

    @app.get("/", include_in_schema=False)
    def service_root() -> dict[str, str]:
        return {
            "service": "huaibeixi-alert-backend",
            "version": settings.service_version,
            "docs": "/docs",
            "health": f"{api_prefix}/health",
        }

    return app
