from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Generator, Optional

import psycopg2
import psycopg2.extras
from psycopg2 import pool

from app.core.config import get_settings

logger = logging.getLogger("huaibeixi.database")

_pool_lock = threading.Lock()
_connection_pool: Optional[pool.ThreadedConnectionPool] = None


def init_db_pool(
    minconn: int | None = None,
    maxconn: int | None = None,
) -> pool.ThreadedConnectionPool:
    global _connection_pool
    settings = get_settings()
    minconn = minconn or settings.db_pool_minconn
    maxconn = maxconn or settings.db_pool_maxconn

    with _pool_lock:
        if _connection_pool is None:
            _connection_pool = pool.ThreadedConnectionPool(
                minconn=minconn,
                maxconn=maxconn,
                dsn=settings.dsn,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            logger.info(
                "Initialized ThreadedConnectionPool (%s:%s/%s)",
                settings.db_host,
                settings.db_port,
                settings.db_name,
            )
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
            conn = psycopg2.connect(
                get_settings().dsn,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
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


@contextmanager
def acquire_replay_leader_lock(lock_key: int) -> Generator[bool, None, None]:
    """Use a dedicated PostgreSQL session advisory lock for a single replay leader."""
    conn = psycopg2.connect(
        get_settings().dsn,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    acquired = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))
            acquired = bool(cur.fetchone()["acquired"])
        yield acquired
    finally:
        if acquired:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            except Exception:
                logger.exception("Failed to release replay leader lock")
        conn.close()
