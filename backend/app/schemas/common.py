"""共享错误和健康检查响应模型。"""

from typing import Any, Optional

from pydantic import BaseModel


class ErrorBody(BaseModel):
    """稳定的错误载荷字段。"""

    code: str
    message: str
    details: Optional[Any] = None


class ErrorResponse(BaseModel):
    """顶层错误响应。"""

    error: ErrorBody


class HealthResponse(BaseModel):
    """存活检查或就绪检查响应。"""

    status: str
    service: str
    engine: dict[str, Any]
    detection: Optional[dict[str, Any]] = None
