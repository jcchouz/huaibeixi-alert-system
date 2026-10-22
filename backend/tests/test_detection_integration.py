from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Iterator

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor, execute_values

from app.domain.alert_types import ALERT_TYPE_THRESHOLD
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import close_db_pool
from app.repositories.detection_state import (
    load_worker_watermark,
    save_worker_watermark,
)

pytestmark = pytest.mark.integration
TEST_RUN_PREFIX = f"itest-det-{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def db_conn() -> Iterator[Any]:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not configured")
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    time_floor = database_time(conn) - timedelta(minutes=1)
    cleanup(conn, TEST_RUN_PREFIX, time_floor)
    try:
        yield conn
    finally:
        cleanup(conn, TEST_RUN_PREFIX, time_floor)
        conn.close()
        close_db_pool()


def cleanup(conn: Any, prefix: str, time_floor: datetime) -> None:
    pattern = f"{prefix}%"
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM public.t_monitor
            WHERE time >= %s AND LOWER(sensor_no) LIKE %s
            """,
            (time_floor, pattern),
        )
        cur.execute(
            """
            DELETE FROM public.t_alert_record
            WHERE create_time >= %s AND LOWER(sensor_no) LIKE %s
            """,
            (time_floor, pattern),
        )
        cur.execute(
            """
            DELETE FROM public.t_stat_record
            WHERE stat_time >= %s AND LOWER(sensor_no) LIKE %s
            """,
            (time_floor, pattern),
        )
        for table in (
            "t_detection_channel_state",
            "t_alert_threshold",
            "t_sensor_channel",
            "t_sensor",
        ):
            cur.execute(
                f"DELETE FROM public.{table} WHERE LOWER(sensor_no) LIKE %s",
                (pattern,),
            )
        cur.execute(
            "DELETE FROM public.t_detection_worker_state WHERE worker_name LIKE %s",
            (pattern,),
        )
    conn.commit()


def insert_rule(
    conn: Any,
    *,
    sensor_no: str,
    channel: str,
    level1_upper: float = 30.0,
    level2_upper: float = 20.0,
    level3_upper: float = 10.0,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.t_alert_threshold (
                id, sensor_no, physical_quantity, channel,
                level1_upper, level1_lower,
                level2_upper, level2_lower,
                level3_upper, level3_lower,
                range_min, range_max
            ) VALUES (
                %s, %s, 'test', %s,
                %s, NULL,
                %s, NULL,
                %s, NULL,
                -100, 100
            )
            """,
            (
                uuid.uuid4().hex,
                sensor_no,
                channel,
                level1_upper,
                level2_upper,
                level3_upper,
            ),
        )
    conn.commit()


def insert_samples(
    conn: Any,
    *,
    sensor_no: str,
    channel: str,
    samples: list[tuple[datetime, float]],
) -> None:
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO public.t_monitor (sensor_no, channel, value, time)
            VALUES %s
            """,
            [
                (sensor_no, channel, value, sample_time)
                for sample_time, value in samples
            ],
        )
    conn.commit()


def database_time(conn: Any) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT LOCALTIMESTAMP AS now")
        return cur.fetchone()["now"]


def make_engine() -> AlertAnalyticsEngine:
    return AlertAnalyticsEngine(
        condition_unit="points",
        confirm_points=3,
        stuck_points=100,
        offline_points=5,
        persist_state=True,
        rule_refresh_seconds=0.1,
    )


def test_threshold_lifecycle_persists_state_and_handles_recurrence(db_conn: Any) -> None:
    sensor_no = f"{TEST_RUN_PREFIX}-lifecycle"
    channel = "x"
    insert_rule(
        db_conn,
        sensor_no=sensor_no,
        channel=channel,
    )

    engine = make_engine()
    engine.initialize()
    key = engine.make_key(sensor_no, channel)
    base = database_time(db_conn) + timedelta(seconds=1)
    insert_samples(
        db_conn,
        sensor_no=sensor_no,
        channel=channel,
        samples=[
            (base, 12.0),
            (base + timedelta(milliseconds=100), 12.1),
            (base + timedelta(milliseconds=200), 12.2),
        ],
    )

    engine.scan_realtime_alerts(base, base + timedelta(milliseconds=350))
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, alert_level, seq, end_time
            FROM public.t_alert_record
            WHERE LOWER(sensor_no) = %s AND channel = %s
              AND alert_type = %s
            """,
            (sensor_no, channel, ALERT_TYPE_THRESHOLD),
        )
        first_alert = cur.fetchone()
    assert first_alert is not None
    assert first_alert["alert_level"] == 3
    assert first_alert["end_time"] is None

    restarted = make_engine()
    restarted.initialize()
    restored_state = restarted.channel_states.get(key)
    assert restored_state is not None
    assert restored_state["threshold_condition_active"] is True
    assert restored_state["threshold_streak_count"] == 3

    normal_time = base + timedelta(milliseconds=500)
    insert_samples(
        db_conn,
        sensor_no=sensor_no,
        channel=channel,
        samples=[(normal_time, 1.0)],
    )
    engine.scan_realtime_alerts(
        normal_time - timedelta(milliseconds=50),
        normal_time + timedelta(milliseconds=50),
    )
    assert engine.channel_states.get(key)["threshold_condition_active"] is False
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT end_time
            FROM public.t_alert_record
            WHERE id = %s
            """,
            (first_alert["id"],),
        )
        assert cur.fetchone()["end_time"] is not None

    recurrence_base = base + timedelta(milliseconds=700)
    insert_samples(
        db_conn,
        sensor_no=sensor_no,
        channel=channel,
        samples=[
            (recurrence_base, 12.0),
            (recurrence_base + timedelta(milliseconds=100), 12.1),
            (recurrence_base + timedelta(milliseconds=200), 12.2),
        ],
    )
    engine.scan_realtime_alerts(
        recurrence_base - timedelta(milliseconds=50),
        recurrence_base + timedelta(milliseconds=250),
    )

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS cnt,
                   COUNT(*) FILTER (WHERE end_time IS NULL) AS open_count,
                   MAX(seq) AS seq
            FROM public.t_alert_record
            WHERE LOWER(sensor_no) = %s AND channel = %s
              AND alert_type = %s
            """,
            (sensor_no, channel, ALERT_TYPE_THRESHOLD),
        )
        recurring = cur.fetchone()
    assert recurring["cnt"] == 2
    assert recurring["open_count"] == 1
    assert recurring["seq"] >= first_alert["seq"]


def test_rule_version_change_reloads_thresholds(db_conn: Any) -> None:
    sensor_no = f"{TEST_RUN_PREFIX}-rule-sync"
    channel = "x"
    insert_rule(
        db_conn,
        sensor_no=sensor_no,
        channel=channel,
        level3_upper=10.0,
    )

    engine = make_engine()
    engine.initialize()
    key = engine.make_key(sensor_no, channel)
    assert engine.channel_states.get(key)["level3_upper"] == 10.0

    with db_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.t_alert_threshold
            SET level3_upper = 7.5
            WHERE LOWER(sensor_no) = %s AND channel = %s
            """,
            (sensor_no, channel),
        )
    db_conn.commit()

    assert engine.refresh_rules_if_changed(force=True) is True
    assert engine.channel_states.get(key)["level3_upper"] == 7.5



def test_worker_watermark_round_trip(db_conn: Any) -> None:
    worker_name = f"{TEST_RUN_PREFIX}-worker-{uuid.uuid4().hex[:8]}"
    last_processed = database_time(db_conn)
    completed = last_processed + timedelta(milliseconds=25)
    with db_conn.cursor() as cur:
        save_worker_watermark(
            cur,
            worker_name=worker_name,
            last_processed_time=last_processed,
            last_completed_time=completed,
        )
    db_conn.commit()

    with db_conn.cursor() as cur:
        restored = load_worker_watermark(cur, worker_name)
    assert restored is not None
    assert restored["last_processed_time"] == last_processed
    assert restored["last_completed_time"] == completed
