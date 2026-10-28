from __future__ import annotations

import logging

from app.core.config import Settings
from app.core.logging import LOG_FILE_NAME, configure_logging


def test_configure_logging_writes_logs_to_the_configured_folder(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        environment="development",
        log_dir=tmp_path,
        log_json=False,
    )
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    try:
        configure_logging(settings)
        logging.getLogger("alert_analysis.test").info("scan file check")
        for handler in root.handlers:
            handler.flush()

        log_path = tmp_path / LOG_FILE_NAME
        assert log_path.is_file()
        assert "scan file check" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers:
            if handler not in original_handlers:
                handler.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)
