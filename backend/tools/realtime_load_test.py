#!/usr/bin/env python
"""为检测 Worker 安全生成实时原始样本负载。

默认模式只执行只读预检。写入 ``t_monitor`` 时必须同时提供
``--allow-write`` 和与目标库完全一致的 ``--confirm-database``。

每次运行使用唯一的完整传感器前缀：

    <sensor-prefix>-<session-id>-

所有生成的传感器编号都以该完整前缀开头。清理时只删除 ``sensor_no``
以相同完整前缀开头的记录。

模拟传感器不要求预置阈值规则。Worker 仍会扫描这些原始数据，并在结果中
记录 Worker 周期数和扫描延迟。如需验证逐通道预警和统计，请先为使用该
完整前缀的传感器配置阈值规则，重启或重新加载后端使 Worker 载入规则，
再使用 ``--use-configured-channels`` 运行。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg2
from psycopg2.extras import execute_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.core.config import Settings

DEFAULT_HEALTH_URL = "http://127.0.0.1:8090/api/health"
DEFAULT_MAX_ROWS = 5_000_000

TABLE_TIME_COLUMNS = {
    "t_alert_record": "create_time",
    "t_monitor": "time",
    "t_stat_record": "stat_time",
}

REQUIRED_COLUMNS = {
    "t_monitor": {"sensor_no", "channel", "value", "time"},
    "t_alert_record": {
        "sensor_no",
        "channel",
        "alert_type",
        "alert_status",
        "create_time",
    },
    "t_stat_record": {
        "sensor_no",
        "channel",
        "period_type",
        "stat_time",
        "sample_count",
    },
    "t_alert_threshold": {
        "sensor_no",
        "channel",
    },
}

PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
SESSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class LoadTestError(RuntimeError):
    """安全检查、目标检查或运行时检查失败。"""


@dataclass(frozen=True)
class TargetInfo:
    database: str
    user: str
    server_address: str | None
    server_port: int | None
    read_only: bool


@dataclass(frozen=True)
class ChannelSpec:
    sensor_no: str
    channel: str
    configured: bool


@dataclass
class WorkerObserver:
    url: str
    timeout_seconds: float
    run_started_at: datetime | None = None
    available: bool = False
    first_status: dict[str, Any] | None = None
    last_status: dict[str, Any] | None = None
    scan_latencies_ms: list[float] = field(default_factory=list)
    scan_times: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def poll(self) -> dict[str, Any] | None:
        if not self.url:
            return None
        try:
            payload = fetch_json(self.url, self.timeout_seconds)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            message = str(exc)
            if not self.errors or self.errors[-1] != message:
                self.errors.append(message)
            return None

        detection = payload.get("detection")
        if not isinstance(detection, dict):
            self.errors.append("health response does not contain detection status")
            return None

        self.available = True
        if self.first_status is None:
            self.first_status = detection
        self.last_status = detection

        scan_at_text = str(detection.get("lastAlertScanAt") or "")
        scan_at = parse_datetime(scan_at_text)
        if (
            self.run_started_at is not None
            and scan_at is not None
            and scan_at >= self.run_started_at
            and scan_at_text not in self.scan_times
        ):
            self.scan_times.add(scan_at_text)
            last_result = detection.get("lastAlertResult")
            if isinstance(last_result, dict):
                elapsed_ms = last_result.get("elapsedMs")
                if elapsed_ms is not None:
                    self.scan_latencies_ms.append(float(elapsed_ms))
        return detection

    def summary(self) -> dict[str, Any]:
        first = self.first_status or {}
        last = self.last_status or {}
        first_alert_cycles = int(first.get("alertCycles") or 0)
        last_alert_cycles = int(last.get("alertCycles") or 0)
        first_scan = parse_datetime(str(first.get("lastAlertScanAt") or ""))

        first_scan_after_start_seconds = None
        if self.run_started_at is not None:
            scan_sets = [
                parse_datetime(text)
                for text in self.scan_times
                if parse_datetime(text) is not None
            ]
            if scan_sets:
                first_after = min(scan_sets)
                first_scan_after_start_seconds = round(
                    (first_after - self.run_started_at).total_seconds(),
                    3,
                )

        latencies = self.scan_latencies_ms
        return {
            "url": self.url,
            "available": self.available,
            "running": last.get("running"),
            "alertLeader": last.get("leader"),
            "statisticsLeader": last.get("statisticsLeader"),
            "alertCycleDelta": max(0, last_alert_cycles - first_alert_cycles),
            "firstPolledAlertScanAt": (
                first_scan.isoformat(sep=" ", timespec="milliseconds")
                if first_scan is not None
                else None
            ),
            "lastAlertScanAt": last.get("lastAlertScanAt"),
            "firstDetectionAfterRunSeconds": first_scan_after_start_seconds,
            "observedScans": len(latencies),
            "scanLatencyMs": {
                "min": round(min(latencies), 2) if latencies else None,
                "max": round(max(latencies), 2) if latencies else None,
                "avg": (
                    round(sum(latencies) / len(latencies), 2)
                    if latencies
                    else None
                ),
            },
            "lastAlertResult": last.get("lastAlertResult"),
            "lastStatisticsResult": last.get("lastResult"),
            "lastError": last.get("lastError"),
            "lastAlertError": last.get("lastAlertError"),
            "lastStatisticsError": last.get("lastStatisticsError"),
            "errors": self.errors[-10:],
        }


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("preflight", "run", "cleanup"),
        default="preflight",
        help="preflight is read-only; run generates load; cleanup only removes the exact prefix",
    )
    parser.add_argument("--channels", type=positive_int, default=16)
    parser.add_argument(
        "--frequency",
        type=positive_float,
        default=20.0,
        help="samples per second per channel",
    )
    parser.add_argument("--duration", type=positive_float, default=30.0, help="seconds")
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=1000,
        help="maximum rows per transaction; batches are additionally capped to about 1s",
    )
    parser.add_argument(
        "--sensor-prefix",
        default="rtload",
        help="base prefix; the full prefix is <base>-<session-id>-",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="explicit unique session id; required for cleanup and generated for other modes",
    )
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--health-timeout", type=positive_float, default=2.0)
    parser.add_argument("--report-interval", type=positive_float, default=2.0)
    parser.add_argument(
        "--settle-seconds",
        type=nonnegative_float,
        default=8.0,
        help="wait after inserts so the worker can catch up",
    )
    parser.add_argument(
        "--max-rows",
        type=nonnegative_int,
        default=DEFAULT_MAX_ROWS,
        help="refuse larger runs; zero disables the row-count guard",
    )
    parser.add_argument(
        "--use-configured-channels",
        action="store_true",
        help="require configured threshold rules under the exact full prefix",
    )
    parser.add_argument(
        "--skip-worker-check",
        action="store_true",
        help="do not require the health endpoint to report a worker",
    )
    parser.add_argument(
        "--deep-prefix-check",
        action="store_true",
        help="scan all t_monitor history for prefix collisions instead of the last hour",
    )
    parser.add_argument(
        "--allow-existing-prefix-data",
        action="store_true",
        help=(
            "allow an existing exact-prefix alert/stat/monitor row; "
            "cleanup still only removes the exact prefix"
        ),
    )
    parser.add_argument(
        "--cleanup-lookback-minutes",
        type=nonnegative_int,
        default=60,
        help="cleanup mode time bound; zero scans all history for the exact prefix",
    )
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="required for run and cleanup; t_monitor is never written in preflight",
    )
    parser.add_argument(
        "--confirm-database",
        default=None,
        help="required for run and cleanup; must exactly match current_database()",
    )
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="additional explicit switch required when ENVIRONMENT=production",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="run mode only: keep generated rows and print the exact cleanup arguments",
    )
    return parser


def normalize_name(value: str, label: str) -> str:
    normalized = value.strip().lower().rstrip(".-")
    if len(normalized) < 2:
        raise LoadTestError(f"{label} must contain at least two characters")
    if not PREFIX_PATTERN.fullmatch(normalized):
        raise LoadTestError(
            f"{label} may contain only lowercase letters, digits, '.', '_' and '-'"
        )
    return normalized


def resolve_sensor_prefix(base: str, session_id: str) -> tuple[str, str, str]:
    normalized_base = normalize_name(base, "sensor prefix")
    normalized_session = normalize_name(session_id, "session id")
    if not SESSION_PATTERN.fullmatch(normalized_session):
        raise LoadTestError("session id contains unsupported characters")
    full_prefix = f"{normalized_base}-{normalized_session}-"
    if len(full_prefix) > 96:
        raise LoadTestError("full sensor prefix is too long")
    return normalized_base, normalized_session, full_prefix


def resolve_prefix(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, str, str]:
    if args.mode == "cleanup" and not args.session_id:
        parser.error("--session-id is required for cleanup")
    session_id = args.session_id or uuid.uuid4().hex[:16]
    try:
        return resolve_sensor_prefix(args.sensor_prefix, session_id)
    except LoadTestError as exc:
        parser.error(str(exc))


def validate_cli(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    estimated_rows: int | None = None,
) -> None:
    if args.mode in {"run", "cleanup"}:
        if not args.allow_write:
            parser.error(f"--mode {args.mode} requires --allow-write")
        if not args.confirm_database:
            parser.error(f"--mode {args.mode} requires --confirm-database")
    if args.mode == "preflight" and args.keep_data:
        parser.error("--keep-data is only valid with --mode run")
    if args.max_rows and estimated_rows is not None and estimated_rows > args.max_rows:
        parser.error(
            f"estimated {estimated_rows} rows exceed --max-rows {args.max_rows}; "
            "reduce the load or raise the explicit guard"
        )
    if args.mode == "run" and not args.health_url and not args.skip_worker_check:
        parser.error("--health-url is required unless --skip-worker-check is set")


def make_connection(settings: Settings) -> Any:
    return psycopg2.connect(
        settings.dsn,
        application_name="huaibeixi_realtime_load_test",
    )


def inspect_target(conn: Any) -> TargetInfo:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_database() AS database,
                   current_user AS username,
                   inet_server_addr()::text AS server_address,
                   inet_server_port() AS server_port,
                   current_setting('transaction_read_only') AS read_only
            """
        )
        row = cur.fetchone()
    if row is None:
        raise LoadTestError("could not inspect the target database")
    return TargetInfo(
        database=str(row[0]),
        user=str(row[1]),
        server_address=row[2],
        server_port=row[3],
        read_only=str(row[4]).lower() in {"on", "true", "1"},
    )


def verify_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)
            """,
            (list(REQUIRED_COLUMNS),),
        )
        present: dict[str, set[str]] = {name: set() for name in REQUIRED_COLUMNS}
        for table_name, column_name in cur.fetchall():
            present.setdefault(str(table_name), set()).add(str(column_name))

        missing = {
            table: sorted(columns - present.get(table, set()))
            for table, columns in REQUIRED_COLUMNS.items()
            if columns - present.get(table, set())
        }
        if missing:
            detail = "; ".join(f"{table}: {columns}" for table, columns in missing.items())
            raise LoadTestError(f"required public table columns are missing: {detail}")

        cur.execute(
            """
            SELECT c.relname, c.relkind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY(%s)
            """,
            (list(REQUIRED_COLUMNS),),
        )
        relations = {str(name): str(kind) for name, kind in cur.fetchall()}
        bad = {
            table: relations.get(table, "missing")
            for table in REQUIRED_COLUMNS
            if relations.get(table) not in {"r", "p"}
        }
        if bad:
            raise LoadTestError(f"required relations are not tables: {bad}")


def approximate_monitor_total(conn: Any) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT to_regprocedure(
                'public.hypertable_approximate_row_count(regclass)'
            ) IS NOT NULL AS available
            """
        )
        row = cur.fetchone()
        if row and row[0]:
            cur.execute(
                "SELECT hypertable_approximate_row_count("
                "'public.t_monitor'::regclass)::bigint AS row_count"
            )
            result = cur.fetchone()
            return int(result[0]) if result and result[0] is not None else None

        cur.execute(
            """
            SELECT CASE
                     WHEN c.reltuples >= 0 THEN c.reltuples::bigint
                     ELSE NULL
                   END AS row_count
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = 't_monitor'
            """
        )
        result = cur.fetchone()
        return int(result[0]) if result and result[0] is not None else None


def count_recent_monitor_rows(conn: Any, minutes: int = 5) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.t_monitor
            WHERE time >= LOCALTIMESTAMP - (%s * INTERVAL '1 minute')
            """,
            (minutes,),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def count_prefixed_rows(
    conn: Any,
    table: str,
    prefix: str,
    *,
    since: datetime | None = None,
) -> int:
    if table not in TABLE_TIME_COLUMNS:
        raise LoadTestError(f"unsupported table for prefix count: {table}")
    time_column = TABLE_TIME_COLUMNS[table]
    sql = (
        f"SELECT COUNT(*) FROM public.{table} "
        "WHERE LEFT(sensor_no, %s) = %s"
    )
    params: list[Any] = [len(prefix), prefix]
    if since is not None:
        sql += f" AND {time_column} >= %s"
        params.append(since)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def count_total_rows(
    conn: Any,
    table: str,
    *,
    since: datetime | None = None,
) -> int:
    if table not in TABLE_TIME_COLUMNS:
        raise LoadTestError(f"unsupported table for total count: {table}")
    time_column = TABLE_TIME_COLUMNS[table]
    sql = f"SELECT COUNT(*) FROM public.{table}"
    params: tuple[Any, ...] = ()
    if since is not None:
        sql += f" WHERE {time_column} >= %s"
        params = (since,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return int(row[0]) if row else 0


def recent_prefix_collision(
    conn: Any,
    full_prefix: str,
    *,
    deep: bool,
    allow_thresholds: bool = False,
) -> bool:
    monitor_sql = """
        SELECT EXISTS (
            SELECT 1
            FROM public.t_monitor
            WHERE LEFT(sensor_no, %s) = %s
        )
    """
    monitor_params: tuple[Any, ...] = (len(full_prefix), full_prefix)
    if not deep:
        monitor_sql = """
            SELECT EXISTS (
                SELECT 1
                FROM public.t_monitor
                WHERE time >= LOCALTIMESTAMP - INTERVAL '1 hour'
                  AND LEFT(sensor_no, %s) = %s
            )
        """
    with conn.cursor() as cur:
        cur.execute(monitor_sql, monitor_params)
        monitor_exists = bool(cur.fetchone()[0])
        if monitor_exists:
            return True

        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM public.t_alert_record
                WHERE LEFT(sensor_no, %s) = %s
            )
            """,
            (len(full_prefix), full_prefix),
        )
        if bool(cur.fetchone()[0]):
            return True

        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM public.t_stat_record
                WHERE LEFT(sensor_no, %s) = %s
            )
            """,
            (len(full_prefix), full_prefix),
        )
        if bool(cur.fetchone()[0]):
            return True

        if allow_thresholds:
            return False

        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM public.t_alert_threshold
                WHERE LEFT(sensor_no, %s) = %s
            )
            """,
            (len(full_prefix), full_prefix),
        )
        return bool(cur.fetchone()[0])


def load_configured_channels(
    conn: Any,
    full_prefix: str,
    limit: int,
) -> list[ChannelSpec]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sensor_no, channel
            FROM public.t_alert_threshold
            WHERE LEFT(sensor_no, %s) = %s
            ORDER BY sensor_no, channel
            LIMIT %s
            """,
            (len(full_prefix), full_prefix, limit),
        )
        return [
            ChannelSpec(
                sensor_no=str(sensor_no),
                channel=str(channel or ""),
                configured=True,
            )
            for sensor_no, channel in cur.fetchall()
        ]


def synthetic_channels(full_prefix: str, count: int) -> list[ChannelSpec]:
    return [
        ChannelSpec(
            sensor_no=f"{full_prefix}ch{index:05d}",
            channel="",
            configured=False,
        )
        for index in range(count)
    ]


def choose_channels(
    conn: Any,
    args: argparse.Namespace,
    full_prefix: str,
) -> list[ChannelSpec]:
    if args.use_configured_channels:
        configured = load_configured_channels(conn, full_prefix, args.channels)
        if len(configured) < args.channels:
            raise LoadTestError(
                f"requested {args.channels} configured channels under {full_prefix}, "
                f"but only found {len(configured)}"
            )
        return configured
    return synthetic_channels(full_prefix, args.channels)


def sample_plan(
    channels: Sequence[ChannelSpec],
    frequency: float,
    duration: float,
    batch_size: int,
) -> tuple[int, int, int]:
    samples_per_channel = max(1, int(math.ceil(duration * frequency)))
    estimated_rows = samples_per_channel * len(channels)
    total_rate = len(channels) * frequency
    effective_batch_limit = min(batch_size, max(1, int(math.ceil(total_rate))))
    return samples_per_channel, estimated_rows, effective_batch_limit


def iter_batches(
    channels: Sequence[ChannelSpec],
    frequency: float,
    start_time: datetime,
    samples_per_channel: int,
    batch_limit: int,
) -> Iterator[list[tuple[str, str, float, datetime]]]:
    batch: list[tuple[str, str, float, datetime]] = []
    for sample_index in range(samples_per_channel):
        sample_time = start_time + timedelta(seconds=sample_index / frequency)
        for channel_index, channel in enumerate(channels):
            value = math.sin((sample_index + channel_index * 17) * 0.01)
            batch.append((channel.sensor_no, channel.channel, value, sample_time))
            if len(batch) >= batch_limit:
                yield batch
                batch = []
    if batch:
        yield batch


def insert_batch(conn: Any, rows: Sequence[tuple[str, str, float, datetime]]) -> None:
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO public.t_monitor (sensor_no, channel, value, time)
            VALUES %s
            """,
            rows,
            page_size=max(1, len(rows)),
        )
    conn.commit()


def normalize_health_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LoadTestError("health URL must be an absolute http(s) URL")
    path = parsed.path.rstrip("/")
    if not path:
        path = "/api/health"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, "")
    )


def fetch_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "huaibeixi-load-test/1.0"},
    )
    with urllib.request.urlopen(  # noqa: B310
        request,
        timeout=timeout_seconds,
    ) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("health response is not a JSON object")
    return payload


def parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def enforce_write_target(
    args: argparse.Namespace,
    settings: Settings,
    target: TargetInfo,
) -> None:
    if target.read_only:
        raise LoadTestError("target database is transaction read-only")
    if args.confirm_database != target.database:
        raise LoadTestError(
            f"--confirm-database {args.confirm_database!r} does not match "
            f"current_database() {target.database!r}"
        )
    if target.database != settings.db_name:
        raise LoadTestError(
            f"connected database {target.database!r} does not match Settings.db_name "
            f"{settings.db_name!r}"
        )
    if settings.environment == "production" and not args.allow_production:
        raise LoadTestError(
            "ENVIRONMENT=production requires the additional --allow-production switch"
        )


def cleanup_prefix(
    conn: Any,
    full_prefix: str,
    *,
    since: datetime | None = None,
) -> dict[str, int]:
    def prefixed_count(table: str) -> int:
        return count_prefixed_rows(conn, table, full_prefix, since=since)

    before = {
        table: prefixed_count(table)
        for table in ("t_alert_record", "t_stat_record", "t_monitor")
    }
    deleted: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            for table in ("t_alert_record", "t_stat_record", "t_monitor"):
                sql = (
                    f"DELETE FROM public.{table} "
                    "WHERE LEFT(sensor_no, %s) = %s"
                )
                params: list[Any] = [len(full_prefix), full_prefix]
                if since is not None:
                    sql += f" AND {TABLE_TIME_COLUMNS[table]} >= %s"
                    params.append(since)
                cur.execute(sql, params)
                deleted[table] = int(cur.rowcount)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    after = {
        table: prefixed_count(table)
        for table in ("t_alert_record", "t_stat_record", "t_monitor")
    }
    return {
        "before": sum(before.values()),
        "deleted": sum(deleted.values()),
        "after": sum(after.values()),
        **{f"{table}Before": before[table] for table in before},
        **{f"{table}Deleted": deleted[table] for table in deleted},
        **{f"{table}After": after[table] for table in after},
    }


def current_database_time(conn: Any) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT LOCALTIMESTAMP")
        row = cur.fetchone()
    if row is None:
        raise LoadTestError("could not read database time")
    return row[0]


def preflight(
    conn: Any,
    args: argparse.Namespace,
    settings: Settings,
    full_prefix: str,
    channels: Sequence[ChannelSpec],
    samples_per_channel: int,
    estimated_rows: int,
    effective_batch_limit: int,
    observer: WorkerObserver,
) -> dict[str, Any]:
    target = inspect_target(conn)
    verify_schema(conn)
    collision = False
    if not args.allow_existing_prefix_data:
        collision = recent_prefix_collision(
            conn,
            full_prefix,
            deep=args.deep_prefix_check,
            allow_thresholds=args.use_configured_channels,
        )
    monitor_total = approximate_monitor_total(conn)
    monitor_recent = count_recent_monitor_rows(conn)
    worker_status = observer.poll()
    configured_count = sum(1 for channel in channels if channel.configured)

    print(
        f"[preflight] target={target.database}@{target.server_address or '?'}:"
        f"{target.server_port or '?'} user={target.user} "
        f"environment={settings.environment}",
        flush=True,
    )
    print("[preflight] required public tables and columns: OK", flush=True)
    print(
        f"[preflight] monitor approximate total={monitor_total} "
        f"recent_5m={monitor_recent} planned_rows={estimated_rows}",
        flush=True,
    )
    print(
        f"[preflight] channels={len(channels)} configured={configured_count} "
        f"frequency={args.frequency}/s/channel duration={args.duration}s",
        flush=True,
    )
    print(
        f"[preflight] full_sensor_prefix={full_prefix} "
        f"samples_per_channel={samples_per_channel} "
        f"effective_batch_limit={effective_batch_limit}",
        flush=True,
    )
    if collision:
        raise LoadTestError(
            f"the exact prefix {full_prefix!r} already has recent or historical test data"
        )
    if configured_count == 0:
        print(
            "[preflight] WARNING no configured threshold rules use this prefix; "
            "raw rows will pressure the worker scan but should not create prefix alerts/stats",
            flush=True,
        )
    if observer.url:
        if worker_status is None:
            print("[preflight] worker health: unavailable", flush=True)
        else:
            print(
                "[preflight] worker health: "
                f"running={worker_status.get('running')} "
                f"alertLeader={worker_status.get('leader')} "
                f"alertCycles={worker_status.get('alertCycles')} "
                f"lastScanElapsedMs={(worker_status.get('lastAlertResult') or {}).get('elapsedMs')}",
                flush=True,
            )
    else:
        print("[preflight] worker health: skipped", flush=True)
    print("[preflight] mode is read-only; no rows were written", flush=True)

    if args.mode == "run" and not args.skip_worker_check and worker_status is None:
        raise LoadTestError("worker health check failed; use --skip-worker-check to override")

    return {
        "target": {
            "database": target.database,
            "serverAddress": target.server_address,
            "serverPort": target.server_port,
            "user": target.user,
            "environment": settings.environment,
        },
        "monitorApproximateTotal": monitor_total,
        "monitorRecent5m": monitor_recent,
        "channelCount": len(channels),
        "configuredChannelCount": configured_count,
        "samplesPerChannel": samples_per_channel,
        "estimatedRows": estimated_rows,
        "effectiveBatchLimit": effective_batch_limit,
        "worker": observer.summary(),
    }


def run_load(
    conn: Any,
    args: argparse.Namespace,
    full_prefix: str,
    channels: Sequence[ChannelSpec],
    samples_per_channel: int,
    effective_batch_limit: int,
    observer: WorkerObserver,
) -> dict[str, Any]:
    start_time = current_database_time(conn)
    observer.run_started_at = start_time
    observer.poll()
    conn.rollback()

    before_counts = {
        "t_alert_record": count_prefixed_rows(conn, "t_alert_record", full_prefix),
        "t_stat_record": count_prefixed_rows(conn, "t_stat_record", full_prefix),
        "t_monitor": count_prefixed_rows(
            conn,
            "t_monitor",
            full_prefix,
            since=start_time - timedelta(hours=1),
        ),
    }
    before_alert_total = count_total_rows(conn, "t_alert_record")
    before_stat_recent = count_total_rows(
        conn,
        "t_stat_record",
        since=start_time - timedelta(minutes=10),
    )
    before_monitor_approx = approximate_monitor_total(conn)
    before_monitor_recent = count_recent_monitor_rows(conn)
    conn.rollback()

    print(
        f"[run] started at {start_time:%Y-%m-%d %H:%M:%S.%f} "
        f"prefix={full_prefix}",
        flush=True,
    )
    rows_sent = 0
    commit_durations_ms: list[float] = []
    write_started = time.perf_counter()
    next_report = write_started

    for rows in iter_batches(
        channels,
        args.frequency,
        start_time,
        samples_per_channel,
        effective_batch_limit,
    ):
        max_sample_time = rows[-1][3]
        target_elapsed = max(
            0.0,
            (max_sample_time - start_time).total_seconds(),
        )
        remaining = target_elapsed - (time.perf_counter() - write_started)
        if remaining > 0:
            time.sleep(remaining)

        commit_started = time.perf_counter()
        insert_batch(conn, rows)
        commit_durations_ms.append((time.perf_counter() - commit_started) * 1000.0)
        rows_sent += len(rows)

        now = time.perf_counter()
        if now >= next_report:
            observer.poll()
            elapsed = max(now - write_started, 0.001)
            worker = observer.summary()
            print(
                f"[run] rows={rows_sent} "
                f"avg_rate={rows_sent / elapsed:.1f}/s "
                f"alertCyclesDelta={worker['alertCycleDelta']} "
                f"lastScanMs={(worker['scanLatencyMs']['max'])}",
                flush=True,
            )
            next_report = now + args.report_interval

    write_elapsed = max(time.perf_counter() - write_started, 0.001)
    if args.settle_seconds > 0:
        settle_deadline = time.monotonic() + args.settle_seconds
        while time.monotonic() < settle_deadline:
            observer.poll()
            time.sleep(min(1.0, max(0.0, settle_deadline - time.monotonic())))
    observer.poll()

    end_time = current_database_time(conn)
    after_counts = {
        "t_alert_record": count_prefixed_rows(conn, "t_alert_record", full_prefix),
        "t_stat_record": count_prefixed_rows(conn, "t_stat_record", full_prefix),
        "t_monitor": count_prefixed_rows(
            conn,
            "t_monitor",
            full_prefix,
            since=start_time - timedelta(hours=1),
        ),
    }
    after_alert_total = count_total_rows(conn, "t_alert_record")
    after_stat_recent = count_total_rows(
        conn,
        "t_stat_record",
        since=start_time - timedelta(minutes=10),
    )
    after_monitor_approx = approximate_monitor_total(conn)
    after_monitor_recent = count_recent_monitor_rows(conn)
    conn.rollback()

    return {
        "startedAt": start_time.isoformat(sep=" ", timespec="milliseconds"),
        "endedAt": end_time.isoformat(sep=" ", timespec="milliseconds"),
        "rowsSubmitted": rows_sent,
        "writeSeconds": round(write_elapsed, 3),
        "submittedRowsPerSecond": round(rows_sent / write_elapsed, 2),
        "commitLatencyMs": {
            "min": round(min(commit_durations_ms), 2) if commit_durations_ms else None,
            "max": round(max(commit_durations_ms), 2) if commit_durations_ms else None,
            "avg": (
                round(sum(commit_durations_ms) / len(commit_durations_ms), 2)
                if commit_durations_ms
                else None
            ),
            "batches": len(commit_durations_ms),
        },
        "monitorApproximateDelta": (
            after_monitor_approx - before_monitor_approx
            if after_monitor_approx is not None and before_monitor_approx is not None
            else None
        ),
        "monitorRecent5mDelta": after_monitor_recent - before_monitor_recent,
        "prefixCounts": {
            "before": before_counts,
            "after": after_counts,
            "delta": {
                table: after_counts[table] - before_counts[table]
                for table in after_counts
            },
        },
        "tableChanges": {
            "tAlertRecordTotal": after_alert_total - before_alert_total,
            "tStatRecordRecent": after_stat_recent - before_stat_recent,
        },
        "databaseRowsSubmittedMatch": after_counts["t_monitor"] == rows_sent,
        "worker": observer.summary(),
    }


def print_summary(
    mode: str,
    base_prefix: str,
    session_id: str,
    full_prefix: str,
    summary: dict[str, Any],
) -> None:
    payload = {
        "mode": mode,
        "sensorPrefix": base_prefix,
        "sessionId": session_id,
        "fullSensorPrefix": full_prefix,
        **summary,
    }
    print("\n[summary]", flush=True)
    print(json.dumps(payload, indent=2, ensure_ascii=True, default=str), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.health_url:
            args.health_url = normalize_health_url(args.health_url)
    except LoadTestError as exc:
        parser.error(str(exc))

    base_prefix, session_id, full_prefix = resolve_prefix(args, parser)
    estimated_rows = None
    samples_per_channel = 0
    effective_batch_limit = args.batch_size
    if args.mode in {"preflight", "run"}:
        samples_per_channel = max(1, int(math.ceil(args.duration * args.frequency)))
        estimated_rows = samples_per_channel * args.channels
        _, _, effective_batch_limit = sample_plan(
            [ChannelSpec("", "", False)] * args.channels,
            args.frequency,
            args.duration,
            args.batch_size,
        )
    validate_cli(args, parser, estimated_rows)

    settings = Settings()
    observer = WorkerObserver(args.health_url, args.health_timeout)
    conn: Any | None = None
    summary: dict[str, Any] = {}
    cleanup_since: datetime | None = None
    exit_code = 0

    try:
        conn = make_connection(settings)
        target = inspect_target(conn)
        verify_schema(conn)
        if args.mode in {"run", "cleanup"}:
            enforce_write_target(args, settings, target)

        if args.mode == "cleanup":
            cleanup_now = current_database_time(conn)
            cleanup_since = (
                cleanup_now - timedelta(minutes=args.cleanup_lookback_minutes)
                if args.cleanup_lookback_minutes
                else None
            )
            print(
                f"[cleanup] exact prefix={full_prefix} target={target.database} "
                f"lookback_minutes={args.cleanup_lookback_minutes}",
                flush=True,
            )
            summary["cleanup"] = cleanup_prefix(
                conn,
                full_prefix,
                since=cleanup_since,
            )
            print(
                f"[cleanup] deleted={summary['cleanup']['deleted']} "
                f"remaining={summary['cleanup']['after']}",
                flush=True,
            )
        else:
            channels = choose_channels(conn, args, full_prefix)
            samples_per_channel, estimated_rows, effective_batch_limit = sample_plan(
                channels,
                args.frequency,
                args.duration,
                args.batch_size,
            )
            validate_cli(args, parser, estimated_rows)
            summary["preflight"] = preflight(
                conn,
                args,
                settings,
                full_prefix,
                channels,
                samples_per_channel,
                estimated_rows,
                effective_batch_limit,
                observer,
            )
            if args.mode == "run":
                run_summary: dict[str, Any] = {}
                try:
                    run_summary = run_load(
                        conn,
                        args,
                        full_prefix,
                        channels,
                        samples_per_channel,
                        effective_batch_limit,
                        observer,
                    )
                    summary["run"] = run_summary
                    started_at = parse_datetime(str(run_summary.get("startedAt") or ""))
                    if started_at is not None:
                        cleanup_since = started_at - timedelta(hours=1)
                except KeyboardInterrupt:
                    summary["run"] = {"interrupted": True}
                    print("[run] interrupted by user", flush=True)
                    exit_code = 130
                finally:
                    if args.keep_data:
                        summary["cleanup"] = {"skipped": True}
                        print(
                            "[cleanup] skipped because --keep-data was set",
                            flush=True,
                        )
                    else:
                        summary["cleanup"] = cleanup_prefix(
                            conn,
                            full_prefix,
                            since=cleanup_since,
                        )
                        print(
                            f"[cleanup] deleted={summary['cleanup']['deleted']} "
                            f"remaining={summary['cleanup']['after']}",
                            flush=True,
                        )
                if args.keep_data:
                    print(
                        "[cleanup] later command: "
                        f"--mode cleanup --sensor-prefix {base_prefix} "
                        f"--session-id {session_id} --allow-write "
                        f"--confirm-database {target.database}",
                        flush=True,
                    )

        print_summary(args.mode, base_prefix, session_id, full_prefix, summary)
        return exit_code
    except LoadTestError as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 1
    except psycopg2.Error as exc:
        print(f"[database-error] {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
