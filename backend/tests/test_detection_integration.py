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

pytestmark = pytest.mark.integration
TEST_RUN_PREFIX = f"itest-det-{uuid.uuid4().hex[:10]}"
TEST_STATION_NAME = f"{TEST_RUN_PREFIX}-station"
TEST_STATION_CODE = f"{TEST_RUN_PREFIX}-code"
TEST_BOX_ID = f"{TEST_RUN_PREFIX}-box"
TEST_INSTRUMENT_ID = f"{TEST_RUN_PREFIX}-instrument"


@pytest.fixture()
def db_conn() -> Iterator[Any]:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is not configured")
    conn = psycopg2.connect(dsn, cursor_factory=RealDictCursor)
    time_floor = database_time(conn) - timedelta(minutes=1)
    cleanup(conn, TEST_RUN_PREFIX, time_floor)
    ensure_station_metadata(conn)
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
        for table in ("t_alert_threshold", "t_sensor_channel", "t_sensor"):
            cur.execute(
                f"DELETE FROM public.{table} WHERE LOWER(sensor_no) LIKE %s",
                (pattern,),
            )
        cur.execute(
            "DELETE FROM public.t_collect_instrument WHERE id LIKE %s",
            (pattern,),
        )
        cur.execute(
            "DELETE FROM public.t_collect_box WHERE id LIKE %s",
            (pattern,),
        )
        cur.execute(
            "DELETE FROM public.t_station WHERE id LIKE %s",
            (pattern,),
        )
    conn.commit()


def ensure_station_metadata(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.t_station (id, name, number)
            VALUES (%s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (f"{TEST_RUN_PREFIX}-station", TEST_STATION_NAME, TEST_STATION_CODE),
        )
        cur.execute(
            """
            INSERT INTO public.t_collect_box (id, number, name, station)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (TEST_BOX_ID, TEST_BOX_ID, TEST_BOX_ID, TEST_STATION_CODE),
        )
        cur.execute(
            """
            INSERT INTO public.t_collect_instrument (id, number, name, box_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (TEST_INSTRUMENT_ID, TEST_INSTRUMENT_ID, TEST_INSTRUMENT_ID, TEST_BOX_ID),
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
            INSERT INTO public.t_sensor (
                id, sensor_no, name, instrument_id, frequency
            ) VALUES (%s, %s, %s, %s, '1')
            ON CONFLICT (id) DO NOTHING
            """,
            (
                f"itest-{uuid.uuid4().hex[:20]}",
                sensor_no,
                sensor_no,
                TEST_INSTRUMENT_ID,
            ),
        )
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
        station_name=TEST_STATION_NAME,
        rule_refresh_seconds=0.1,
    )


def test_threshold_lifecycle_and_recurrence_without_state_restore(db_conn: Any) -> None:
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
            (base + timedelta(seconds=1), 12.1),
            (base + timedelta(seconds=2), 12.2),
            (base + timedelta(seconds=3), 12.3),
        ],
    )

    engine.scan_realtime_alerts(base, base + timedelta(seconds=3, milliseconds=50))
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
    assert restored_state["threshold_condition_active"] is False
    assert restored_state["threshold_streak_count"] == 0

    normal_time = base + timedelta(seconds=4)
    insert_samples(
        db_conn,
        sensor_no=sensor_no,
        channel=channel,
        samples=[(normal_time, 1.0)],
    )
    engine.scan_realtime_alerts(
        normal_time - timedelta(seconds=1),
        normal_time + timedelta(seconds=1),
    )
    normal_state = engine.channel_states.get(key)
    assert normal_state["threshold_condition_active"] is False, {
        "last_ts": normal_state["last_ts"],
        "last_val": normal_state.get("last_val"),
        "threshold_streak_count": normal_state["threshold_streak_count"],
        "threshold_streak_started_at": normal_state[
            "threshold_streak_started_at"
        ],
    }
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

    recurrence_base = base + timedelta(seconds=6)
    insert_samples(
        db_conn,
        sensor_no=sensor_no,
        channel=channel,
        samples=[
            (recurrence_base, 12.0),
            (recurrence_base + timedelta(seconds=1), 12.1),
            (recurrence_base + timedelta(seconds=2), 12.2),
            (recurrence_base + timedelta(seconds=3), 12.3),
        ],
    )
    engine.scan_realtime_alerts(
        recurrence_base - timedelta(seconds=1),
        recurrence_base + timedelta(seconds=3, milliseconds=50),
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
