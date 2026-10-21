"""PostgreSQL 连接池和事务辅助函数。"""

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
    """创建进程级线程安全连接池，重复调用时复用已有实例。"""
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
    """关闭并清理共享连接池。"""
    global _connection_pool
    with _pool_lock:
        if _connection_pool is not None:
            _connection_pool.closeall()
            _connection_pool = None


@contextmanager
def get_db_conn() -> Generator[Any, None, None]:
    """借用一个池化连接，并在使用后归还连接池。"""
    db_pool = init_db_pool()
    conn = db_pool.getconn()
    try:
        if conn.closed != 0:
            # 空闲期间已关闭的连接需要替换。
            db_pool.putconn(conn, close=True)
            conn = psycopg2.connect(
                get_settings().dsn,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
        yield conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # 已损坏的连接直接丢弃，不能重新放回连接池。
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
    """提供游标，并统一处理调用方事务的提交和回滚。"""
    with get_db_conn() as conn:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            if commit:
                # 只读调用不提交，写操作由调用方显式开启提交。
                conn.commit()
        except Exception:
            # 回滚失败事务，保证连接可以继续复用。
            conn.rollback()
            raise
        finally:
            cur.close()
