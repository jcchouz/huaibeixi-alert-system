from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from prometheus_client import make_asgi_app

from app.api.routers import alerts, auth, engine, health, overview, statistics, thresholds
from app.core.config import Settings, get_settings
from app.core.exceptions import install_exception_handlers
from app.core.logging import configure_logging
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import close_db_pool, init_db_pool
from app.infrastructure.middleware import RequestContextMiddleware
from app.infrastructure.sse import SSEHub
from app.infrastructure.workers import ReplayWorker


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    engine_instance = AlertAnalyticsEngine()
    sse_hub = SSEHub(
        redis_url=settings.redis_url,
        redis_channel=settings.redis_channel,
    )
    replay_worker = ReplayWorker(engine_instance, sse_hub, settings)
    engine_instance.register_sse_callback(sse_hub.publish_threadsafe)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sse_hub.bind_loop(asyncio.get_running_loop())
        await sse_hub.start()
        init_db_pool(
            minconn=settings.db_pool_minconn,
            maxconn=settings.db_pool_maxconn,
        )
        await asyncio.to_thread(engine_instance.initialize)
        replay_worker.start()
        try:
            yield
        finally:
            await replay_worker.stop()
            await sse_hub.stop()
            close_db_pool()

    app = FastAPI(
        title=settings.app_name,
        description=(
            "High-concurrency Alert & Scheduled Extremes / Vector Wind Rose Analytics Engine"
        ),
        version=settings.service_version,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine_instance
    app.state.sse_hub = sse_hub
    app.state.replay_worker = replay_worker
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
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    api_prefix = settings.api_prefix.rstrip("/")
    app.include_router(health.router, prefix=api_prefix)
    app.include_router(auth.router, prefix=api_prefix)
    app.include_router(overview.router, prefix=api_prefix)
    app.include_router(alerts.router, prefix=api_prefix)
    app.include_router(thresholds.router, prefix=api_prefix)
    app.include_router(statistics.router, prefix=api_prefix)
    app.include_router(engine.router, prefix=api_prefix)
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
