"""Backward-compatible configuration imports.

New code should import from ``app.core.config``, ``app.core.serialization`` and
``app.infrastructure.database``.
"""

from app.core.config import get_settings
from app.core.serialization import (
    CustomJSONEncoder,
    json_dumps,
    serialize_row,
    serialize_rows,
    serialize_val,
)
from app.infrastructure.database import (
    close_db_pool,
    get_db_conn,
    get_db_cursor,
    init_db_pool,
)

DSN = get_settings().dsn

__all__ = [
    "CustomJSONEncoder",
    "DSN",
    "close_db_pool",
    "get_db_conn",
    "get_db_cursor",
    "init_db_pool",
    "json_dumps",
    "serialize_row",
    "serialize_rows",
    "serialize_val",
]
