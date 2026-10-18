import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Generator, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool

logger = logging.getLogger("huaibeixi.config")

DB_HOST = os.getenv("DB_HOST", "82.156.158.31")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "huaibeixi_db_test")
DB_USER = os.getenv("DB_USER", "huaibeixi")
DB_PASSWORD = os.getenv("DB_PASSWORD", "h4xNSL_7")

DSN = f"host={DB_HOST} port={DB_PORT} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD} connect_timeout=10"


class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.strftime("%Y-%m-%d %H:%M:%S")
        return super().default(obj)


def json_dumps(data: Any) -> str:
    return json.dumps(data, cls=CustomJSONEncoder, ensure_ascii=False)


def serialize_val(val: Any) -> Any:
    if isinstance(val, Decimal):
        return round(float(val), 6)
    if isinstance(val, datetime):
        if val.microsecond:
            return val.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return val.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(val, date):
        return val.strftime("%Y-%m-%d")
    if isinstance(val, list):
        return [serialize_val(v) for v in val]
    if isinstance(val, dict):
        return {k: serialize_val(v) for k, v in val.items()}
    return val


def serialize_row(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {k: serialize_val(v) for k, v in dict(row).items()}


def serialize_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [serialize_row(r) for r in rows if r is not None]


_pool_lock = threading.Lock()
_connection_pool: Optional[pool.ThreadedConnectionPool] = None


def init_db_pool(minconn: int = 2, maxconn: int = 20) -> pool.ThreadedConnectionPool:
    global _connection_pool
    with _pool_lock:
        if _connection_pool is None:
            _connection_pool = pool.ThreadedConnectionPool(
                minconn=minconn,
                maxconn=maxconn,
                dsn=DSN,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            logger.info("Initialized ThreadedConnectionPool (%s:%s/%s)", DB_HOST, DB_PORT, DB_NAME)
        return _connection_pool


def close_db_pool() -> None:
    global _connection_pool
    with _pool_lock:
        if _connection_pool is not None:
            _connection_pool.closeall()
            _connection_pool = None


@contextmanager
def get_db_conn() -> Generator[Any, None, None]:
    db_pool = init_db_pool()
    conn = db_pool.getconn()
    try:
        if conn.closed != 0:
            db_pool.putconn(conn, close=True)
            conn = psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)
        yield conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        try:
            db_pool.putconn(conn, close=True)
        except Exception:
            pass
        conn = None
        raise
    finally:
        if conn is not None:
            try:
                db_pool.putconn(conn)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass


@contextmanager
def get_db_cursor(commit: bool = False) -> Generator[Any, None, None]:
    with get_db_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
