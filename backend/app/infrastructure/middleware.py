"""请求编号、访问日志和 Prometheus HTTP 指标。"""

from __future__ import annotations

import logging
import time
import uuid

from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("huaibeixi.request")

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ("method", "path", "status"),
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "path"),
)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每次请求附加编号，并记录指标和访问日志。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        # 成功和失败的请求都保留同一个请求编号。
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "Request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        # 使用路由模板，避免指标标签出现大量动态值。
        route_path = request.scope.get("route")
        metric_path = getattr(route_path, "path", "unmatched")
        REQUEST_COUNT.labels(request.method, metric_path, str(response.status_code)).inc()
        REQUEST_LATENCY.labels(request.method, metric_path).observe(duration_ms / 1000.0)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "Request completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response
