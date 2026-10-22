"""共享错误和健康检查响应模型。"""

from typing import Any, Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """存活检查或就绪检查响应。"""

    status: str
    service: str
    engine: dict[str, Any]
    detection: Optional[dict[str, Any]] = None
