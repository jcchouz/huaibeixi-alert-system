"""应用日志格式配置。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

from app.core.config import Settings

# 单个日志文件最大 10 MB，并保留 5 个历史轮转文件。
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 5
LOG_FILE_NAME = "alert-system.log"


class JsonFormatter(logging.Formatter):
    """将一条日志记录转换为结构化 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for field in (
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "scan_at",
            "window_start",
            "window_end",
            "scan_elapsed_ms",
            "scan_succeeded",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    """配置控制台和后端日志文件，并统一接管 Uvicorn 访问日志。"""
    if settings.log_json:
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.log_dir / LOG_FILE_NAME,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    root.setLevel(settings.log_level.upper())

    for noisy_logger in ("uvicorn.access",):
        logging.getLogger(noisy_logger).handlers.clear()
        logging.getLogger(noisy_logger).propagate = True
