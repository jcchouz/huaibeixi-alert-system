#!/usr/bin/env python
"""Benchmark the realtime alert engine with a configurable synthetic load.

The tool writes only to ``public.t_monitor`` during the load phase. Runtime
threshold rules are injected into an isolated ``AlertAnalyticsEngine`` instance
so the benchmark does not modify ``public.t_alert_threshold``.

Every generated sensor starts with ``bench10k-`` by default. Rows using that
prefix are removed from monitor, alert, and statistics tables before the run
and again after the run unless ``--keep-data`` is supplied.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass
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
from app.domain.alert_types import (
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)
from app.infrastructure.database import close_db_pool

DEFAULT_ROWS_PER_SECOND = 10_000
DEFAULT_SECONDS = 10
DEFAULT_SENSORS = 10
DEFAULT_CHANNELS = 4
DEFAULT_BATCH_SIZE = 1_000
DEFAULT_SENSOR_PREFIX = "bench10k-"
WRITE_TARGET_TOLERANCE = 0.95

CLEANUP_TABLES = (
    "t_alert_record",
    "t_stat_record",
    "t_monitor",
    "t_detection_channel_state",
)
CLEANUP_TIME_COLUMNS = {
    "t_monitor": "time",
    "t_alert_record": "create_time",
    "t_stat_record": "stat_time",
    "t_detection_channel_state": "updated_at",
}
REQUIRED_COLUMNS = {
    "t_monitor": {"sensor_no", "channel", "value", "time"},
    "t_alert_record": {"sensor_no"},
    "t_stat_record": {"sensor_no"},
    "t_detection_channel_state": {"sensor_no", "updated_at"},
}
PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*-$")


class BenchmarkError(RuntimeError):
    """Raised when benchmark options or database state are invalid."""


@dataclass(frozen=True)
class ChannelSpec:
    sensor_no: str
    channel: str


@dataclass(frozen=True)
class RatePlan:
    rows_per_second: int
    seconds: int
    batch_size: int
    total_channels: int
    total_rows: int
    batch_count: int
    rows_per_batch: int
    rows_per_channel: float


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def normalize_sensor_prefix(value: str) -> str:
    """Return a lower-case prefix that is visibly reserved for test data."""
    prefix = value.strip().lower()
    if len(prefix) < 6:
        raise BenchmarkError("sensor prefix must contain at least six characters")
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise BenchmarkError(
            "sensor prefix must contain only lowercase letters, digits, '.', '_' "
            "or '-', and must end with '-'"
        )
    if not (prefix.startswith("bench") or prefix.startswith("test-")):
        raise BenchmarkError("sensor prefix must start with 'bench' or 'test-'")
    return prefix


def validate_benchmark_options(
    *,
    rows_per_second: int,
    seconds: int,
    sensors: int,
    channels: int,
    batch_size: int,
    sensor_prefix: str,
    cleanup: bool,
    keep_data: bool,
) -> None:
    """Validate CLI values without touching argparse or a database."""
    normalize_sensor_prefix(sensor_prefix)
    if cleanup and keep_data:
        raise BenchmarkError("--cleanup and --keep-data cannot be used together")
    if rows_per_second <= 0:
        raise BenchmarkError("--rows-per-second must be greater than zero")
    if seconds <= 0:
        raise BenchmarkError("--seconds must be greater than zero")
    if sensors <= 0:
        raise BenchmarkError("--sensors must be greater than zero")
    if channels <= 0:
        raise BenchmarkError("--channels must be greater than zero")
    if batch_size <= 0:
        raise BenchmarkError("--batch-size must be greater than zero")
    if sensors * channels > 100_000:
        raise BenchmarkError("--sensors multiplied by --channels is too large")


def build_rate_plan(
    rows_per_second: int,
    seconds: int,
    batch_size: int,
    total_channels: int,
) -> RatePlan:
    """Build a deterministic batching and throughput plan."""
    if rows_per_second <= 0 or seconds <= 0 or batch_size <= 0:
        raise BenchmarkError("rate, duration, and batch size must be greater than zero")
    if total_channels <= 0:
        raise BenchmarkError("total channel count must be greater than zero")

    total_rows = rows_per_second * seconds
    rows_per_batch = min(batch_size, total_rows)
    batch_count = math.ceil(total_rows / rows_per_batch)
    return RatePlan(
        rows_per_second=rows_per_second,
        seconds=seconds,
        batch_size=batch_size,
        total_channels=total_channels,
        total_rows=total_rows,
        batch_count=batch_count,
        rows_per_batch=rows_per_batch,
        rows_per_channel=total_rows / total_channels,
    )


def build_channel_specs(
    sensor_prefix: str,
    sensors: int,
    channels_per_sensor: int,
) -> list[ChannelSpec]:
    """Create deterministic sensor and channel names under the test prefix."""
    prefix = normalize_sensor_prefix(sensor_prefix)
    if sensors <= 0 or channels_per_sensor <= 0:
        raise BenchmarkError("sensor and channel counts must be greater than zero")

    specs: list[ChannelSpec] = []
    for sensor_index in range(sensors):
        sensor_no = f"{prefix}sensor-{sensor_index:04d}"
        for channel_index in range(channels_per_sensor):
            specs.append(
                ChannelSpec(
                    sensor_no=sensor_no,
                    channel=f"ch{channel_index:03d}",
                )
            )
    return specs


def synthetic_value(row_index: int, rows_per_second: int) -> float:
    """Return a deterministic normal value or a periodic level-one excursion."""
    if row_index < 0:
        raise BenchmarkError("row index must be zero or greater")
    if rows_per_second <= 0:
        raise BenchmarkError("rows per second must be greater than zero")

    cycle_rows = rows_per_second * 6
    alarm_rows = max(1, rows_per_second // 2)
    if row_index % cycle_rows < alarm_rows:
        return 12.0
    return round(2.0 * math.sin(row_index / 173.0), 6)


def generate_row_batch(
    channels: Sequence[ChannelSpec],
    start_time: datetime,
    start_index: int,
    count: int,
    rows_per_second: int,
) -> list[tuple[str, str, float, datetime]]:
    """Generate one ordered batch of ``t_monitor`` rows."""
    if not channels:
        raise BenchmarkError("at least one channel is required")
    if start_index < 0 or count < 0:
        raise BenchmarkError("start index and count must be zero or greater")
    if rows_per_second <= 0:
        raise BenchmarkError("rows per second must be greater than zero")

    rows: list[tuple[str, str, float, datetime]] = []
    for offset in range(count):
        row_index = start_index + offset
        channel = channels[row_index % len(channels)]
        sample_time = start_time + timedelta(seconds=row_index / rows_per_second)
        rows.append(
            (
                channel.sensor_no,
                channel.channel,
                synthetic_value(row_index, rows_per_second),
                sample_time,
            )
        )
    return rows


def iter_row_batches(
    plan: RatePlan,
    channels: Sequence[ChannelSpec],
    start_time: datetime,
) -> Iterator[list[tuple[str, str, float, datetime]]]:
    """Yield the complete synthetic workload in insertion order."""
    for start_index in range(0, plan.total_rows, plan.rows_per_batch):
        count = min(plan.rows_per_batch, plan.total_rows - start_index)
        yield generate_row_batch(
            channels,
            start_time,
            start_index,
            count,
            plan.rows_per_second,
        )


def assess_capacity(
    *,
    target_rows_per_second: int,
    expected_rows: int,
    processed_rows: int,
    write_rows_per_second: float,
    scan_rows_per_second: float,
    max_scan_latency_seconds: float | None = None,
    scan_interval_seconds: float | None = None,
) -> dict[str, Any]:
    """Compare actual write and scan throughput with the target."""
    required_rate = target_rows_per_second * WRITE_TARGET_TOLERANCE
    write_kept_up = write_rows_per_second >= required_rate
    scan_processed_all = processed_rows == expected_rows
    scan_kept_up = scan_processed_all and scan_rows_per_second >= required_rate
    latency_kept_up = True
    if max_scan_latency_seconds is not None and scan_interval_seconds is not None:
        latency_kept_up = max_scan_latency_seconds <= scan_interval_seconds * 1.5
    return {
        "requiredRowsPerSecond": round(required_rate, 2),
        "writeKeptUp": write_kept_up,
        "scanProcessedAllRows": scan_processed_all,
        "scanKeptUp": scan_kept_up,
        "scanLatencyKeptUp": latency_kept_up,
        "keepsUp": write_kept_up and scan_kept_up and latency_kept_up,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--rows-per-second",
        type=positive_int,
        default=DEFAULT_ROWS_PER_SECOND,
        help="target aggregate write rate (default: 10000)",
    )
    parser.add_argument(
        "--seconds",
        type=positive_int,
        default=DEFAULT_SECONDS,
        help="simulated duration (default: 10)",
    )
    parser.add_argument(
        "--sensors",
        type=positive_int,
        default=DEFAULT_SENSORS,
        help="number of synthetic sensors (default: 10)",
    )
    parser.add_argument(
        "--channels",
        type=positive_int,
        default=DEFAULT_CHANNELS,
        help="channels per synthetic sensor (default: 4)",
    )
    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=DEFAULT_BATCH_SIZE,
        help="maximum rows per execute_values call (default: 1000)",
    )
    parser.add_argument(
        "--sensor-prefix",
        default=DEFAULT_SENSOR_PREFIX,
        help="test-only sensor prefix; must start with 'bench' or 'test-'",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete rows for --sensor-prefix and exit without running the benchmark",
    )
    parser.add_argument(
        "--keep-data",
        action="store_true",
        help="keep generated rows after the benchmark",
    )
    return parser


def make_connection(settings: Settings) -> Any:
    return psycopg2.connect(
        settings.dsn,
        application_name="huaibeixi_benchmark_10k",
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
        raise BenchmarkError(f"required public table columns are missing: {missing}")


def count_prefixed_rows(conn: Any, table: str, prefix: str) -> int:
    if table not in CLEANUP_TABLES:
        raise BenchmarkError(f"unsupported cleanup table: {table}")
    time_column = CLEANUP_TIME_COLUMNS[table]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM public.{table}
            WHERE {time_column} >= LOCALTIMESTAMP - INTERVAL '1 day'
              AND LEFT(LOWER(sensor_no), %s) = %s
            """,
            (len(prefix), prefix),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def cleanup_prefix(conn: Any, prefix: str) -> dict[str, Any]:
    """Delete all synthetic rows whose sensor number uses the exact prefix."""
    normalized = normalize_sensor_prefix(prefix)
    before = {
        table: count_prefixed_rows(conn, table, normalized)
        for table in CLEANUP_TABLES
    }
    deleted: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            for table in CLEANUP_TABLES:
                time_column = CLEANUP_TIME_COLUMNS[table]
                cur.execute(
                    f"""
                    DELETE FROM public.{table}
                    WHERE {time_column} >= LOCALTIMESTAMP - INTERVAL '1 day'
                      AND LEFT(LOWER(sensor_no), %s) = %s
                    """,
                    (len(normalized), normalized),
                )
                deleted[table] = int(cur.rowcount)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    after = {
        table: count_prefixed_rows(conn, table, normalized)
        for table in CLEANUP_TABLES
    }
    return {
        "prefix": normalized,
        "before": before,
        "deleted": deleted,
        "after": after,
        "totalDeleted": sum(deleted.values()),
        "totalRemaining": sum(after.values()),
    }


def database_time(conn: Any) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT LOCALTIMESTAMP")
        row = cur.fetchone()
    if row is None:
        raise BenchmarkError("could not read database time")
    return row[0]


def build_runtime_rules(
    channels: Sequence[ChannelSpec],
    plan: RatePlan,
) -> dict[str, dict[str, Any]]:
    """Build isolated runtime rules consumed directly by the detection engine."""
    per_channel_rate = plan.rows_per_second / len(channels)
    sample_interval_ms = 1000.0 / per_channel_rate
    rules: dict[str, dict[str, Any]] = {}
    for spec in channels:
        sensor_no = spec.sensor_no.lower()
        channel = spec.channel
        key = f"{sensor_no}:{channel}"
        rules[key] = {
            "key": key,
            "sensor_no": spec.sensor_no,
            "sensor_no_lower": sensor_no,
            "channel": channel,
            "sensor_name": spec.sensor_no,
            "station_name": "benchmark",
            "station_code": "BENCH",
            "instrument_id": None,
            "instrument_name": "-",
            "physical_quantity": "benchmark",
            "unit": "unit",
            "position": "-",
            "range": "-",
            "range_min": -100.0,
            "range_max": 100.0,
            "physical_min": -100.0,
            "physical_max": 100.0,
            "level1_upper": 10.0,
            "level2_upper": 8.0,
            "level3_upper": 5.0,
            "level1_lower": -10.0,
            "level2_lower": -8.0,
            "level3_lower": -5.0,
            "frequency": str(round(per_channel_rate, 6)),
            "frequency_hz": per_channel_rate,
            "sample_interval_ms": sample_interval_ms,
            "condition_unit": "points",
            "confirm_points": 3,
            "stuck_points": 100,
            "offline_points": 5,
            "confirm_seconds": None,
            "stuck_seconds": None,
            "offline_seconds": None,
            "offline_timeout_ms": max(1.0, sample_interval_ms * 5),
            "online_status": "ONLINE",
            "threshold_streak_count": 0,
            "threshold_streak_started_at": None,
            "threshold_condition_active": False,
            "threshold_condition_level": None,
            "oor_streak_count": 0,
            "oor_streak_started_at": None,
            "oor_condition_active": False,
            "stuck_streak_count": 0,
            "stuck_condition_active": False,
            "offline_condition_active": False,
        }
    return rules


def run_benchmark(
    conn: Any,
    args: argparse.Namespace,
    plan: RatePlan,
    channels: Sequence[ChannelSpec],
    *,
    scan_interval_seconds: float,
    scan_overlap_seconds: float,
) -> dict[str, Any]:
    prefix = normalize_sensor_prefix(args.sensor_prefix)
    initial_cleanup = cleanup_prefix(conn, prefix)
    print(
        f"[cleanup] before run deleted={initial_cleanup['totalDeleted']} "
        f"remaining={initial_cleanup['totalRemaining']}",
        flush=True,
    )

    start_time = database_time(conn)
    print(
        f"[benchmark] prefix={prefix} rows={plan.total_rows} "
        f"target={plan.rows_per_second}/s duration={plan.seconds}s "
        f"channels={plan.total_channels} batches={plan.batch_count}",
        flush=True,
    )

    write_started = time.perf_counter()
    next_report = write_started + 2.0
    rows_written = 0
    for batch in iter_row_batches(plan, channels, start_time):
        target_elapsed = (rows_written + len(batch)) / plan.rows_per_second
        remaining = target_elapsed - (time.perf_counter() - write_started)
        if remaining > 0:
            time.sleep(remaining)

        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO public.t_monitor (sensor_no, channel, value, time)
                VALUES %s
                """,
                batch,
                page_size=len(batch),
            )
        conn.commit()
        rows_written += len(batch)

        now = time.perf_counter()
        if now >= next_report:
            elapsed = max(now - write_started, 0.001)
            print(
                f"[write] rows={rows_written} actual={rows_written / elapsed:.1f}/s",
                flush=True,
            )
            next_report = now + 2.0

    write_seconds = max(time.perf_counter() - write_started, 0.001)
    actual_write_rate = rows_written / write_seconds
    inserted_rows = count_prefixed_rows(conn, "t_monitor", prefix)
    if inserted_rows != plan.total_rows:
        raise BenchmarkError(
            f"inserted row count mismatch: expected {plan.total_rows}, got {inserted_rows}"
        )

    from app.domain.engine import AlertAnalyticsEngine

    engine = AlertAnalyticsEngine(
        condition_unit="points",
        confirm_points=3,
        stuck_points=100,
        offline_points=5,
        persist_state=True,
        alert_type_switches={
            ALERT_TYPE_THRESHOLD: True,
            ALERT_TYPE_OFFLINE: False,
            ALERT_TYPE_DAQ_OFFLINE: False,
            ALERT_TYPE_STUCK: False,
            ALERT_TYPE_OOR: False,
        },
    )
    engine.channel_states.replace_all(build_runtime_rules(channels, plan))

    print(
        f"[scan] starting {plan.seconds} realtime scans "
        f"(interval={scan_interval_seconds:g}s overlap={scan_overlap_seconds:g}s)",
        flush=True,
    )
    scan_started = time.perf_counter()
    queried_rows = 0
    triggered = 0
    engine_elapsed_ms = 0.0
    scan_latencies: list[float] = []
    interval = max(0.1, float(scan_interval_seconds))
    scan_offsets: list[float] = []
    offset = interval
    while offset < plan.seconds:
        scan_offsets.append(offset)
        offset += interval
    scan_offsets.append(float(plan.seconds))

    scan_cursor = start_time
    for index, offset in enumerate(scan_offsets):
        scan_end = start_time + timedelta(seconds=offset)
        window_start = (
            start_time
            if index == 0
            else scan_cursor - timedelta(seconds=scan_overlap_seconds)
        )
        one_started = time.perf_counter()
        scan_result = engine.scan_realtime_alerts(window_start, scan_end)
        scan_cursor = scan_end
        scan_latencies.append(time.perf_counter() - one_started)
        queried_rows += int(scan_result.get("rawPointsProcessed") or 0)
        triggered += int(scan_result.get("triggeredAlertsCount") or 0)
        engine_elapsed_ms += float(scan_result.get("elapsedMs") or 0.0)
    scan_seconds = max(time.perf_counter() - scan_started, 0.001)
    analysis_rate = plan.total_rows / scan_seconds
    query_rate = queried_rows / scan_seconds
    max_scan_latency = max(scan_latencies, default=0.0)
    first_scan_latency = scan_latencies[0] if scan_latencies else 0.0
    steady_scan_latencies = scan_latencies[1:] or scan_latencies
    max_steady_scan_latency = max(steady_scan_latencies, default=0.0)
    alert_rows = count_prefixed_rows(conn, "t_alert_record", prefix)
    capacity = assess_capacity(
        target_rows_per_second=plan.rows_per_second,
        expected_rows=plan.total_rows,
        processed_rows=plan.total_rows,
        write_rows_per_second=actual_write_rate,
        scan_rows_per_second=analysis_rate,
        max_scan_latency_seconds=max_steady_scan_latency,
        scan_interval_seconds=interval,
    )
    print(
        f"[scan] uniqueRows={plan.total_rows} queriedRows={queried_rows} "
        f"seconds={scan_seconds:.3f} uniqueRate={analysis_rate:.1f}/s "
        f"queryRate={query_rate:.1f}/s firstLatency={first_scan_latency:.3f}s "
        f"maxSteadyLatency={max_steady_scan_latency:.3f}s "
        f"alerts={alert_rows} keepsUp={capacity['keepsUp']}",
        flush=True,
    )

    return {
        "sensorPrefix": prefix,
        "startTime": start_time.isoformat(sep=" ", timespec="milliseconds"),
        "simulatedSeconds": plan.seconds,
        "expectedRows": plan.total_rows,
        "rowsWritten": rows_written,
        "actualWriteRowsPerSecond": round(actual_write_rate, 2),
        "writeSeconds": round(write_seconds, 3),
        "scanSeconds": round(scan_seconds, 3),
        "engineReportedElapsedMs": round(engine_elapsed_ms, 2),
        "scanCount": len(scan_offsets),
        "scanIntervalSeconds": interval,
        "scanOverlapSeconds": scan_overlap_seconds,
        "maxScanLatencySeconds": round(max_scan_latency, 3),
        "firstScanLatencySeconds": round(first_scan_latency, 3),
        "maxSteadyStateScanLatencySeconds": round(
            max_steady_scan_latency,
            3,
        ),
        "rowsProcessed": plan.total_rows,
        "queriedRows": queried_rows,
        "analysisRowsPerSecond": round(analysis_rate, 2),
        "queriedRowsPerSecond": round(query_rate, 2),
        "triggeredAlerts": triggered,
        "persistedAlertRows": alert_rows,
        "capacity": capacity,
        "keepsUp": capacity["keepsUp"],
        "cleanupBeforeRun": initial_cleanup,
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("\n[summary]", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=True, default=str), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_benchmark_options(
            rows_per_second=args.rows_per_second,
            seconds=args.seconds,
            sensors=args.sensors,
            channels=args.channels,
            batch_size=args.batch_size,
            sensor_prefix=args.sensor_prefix,
            cleanup=args.cleanup,
            keep_data=args.keep_data,
        )
    except BenchmarkError as exc:
        parser.error(str(exc))

    prefix = normalize_sensor_prefix(args.sensor_prefix)
    # The developer shell may define DEBUG=release, which pydantic cannot parse
    # as a boolean. The benchmark does not need debug mode.
    settings = Settings(debug=False)
    conn: Any | None = None
    cleanup_done = False
    try:
        conn = make_connection(settings)
        verify_schema(conn)

        if args.cleanup:
            cleanup = cleanup_prefix(conn, prefix)
            print_summary(
                {
                    "mode": "cleanup",
                    "sensorPrefix": prefix,
                    "cleanup": cleanup,
                }
            )
            return 0 if cleanup["totalRemaining"] == 0 else 1

        channels = build_channel_specs(prefix, args.sensors, args.channels)
        plan = build_rate_plan(
            args.rows_per_second,
            args.seconds,
            args.batch_size,
            len(channels),
        )
        summary = run_benchmark(
            conn,
            args,
            plan,
            channels,
            scan_interval_seconds=max(1, settings.detection_interval_seconds),
            scan_overlap_seconds=max(0, settings.detection_alert_overlap_seconds),
        )
        if args.keep_data:
            summary["cleanupAfterRun"] = {"skipped": True}
            print(
                "[cleanup] skipped because --keep-data was supplied; "
                f"run --cleanup --sensor-prefix {prefix} later",
                flush=True,
            )
        else:
            summary["cleanupAfterRun"] = cleanup_prefix(conn, prefix)
            cleanup_done = True
            print(
                f"[cleanup] after run deleted="
                f"{summary['cleanupAfterRun']['totalDeleted']} "
                f"remaining={summary['cleanupAfterRun']['totalRemaining']}",
                flush=True,
            )

        print_summary(
            {
                "mode": "benchmark",
                "targetRowsPerSecond": plan.rows_per_second,
                "targetSeconds": plan.seconds,
                "sensors": args.sensors,
                "channelsPerSensor": args.channels,
                "totalChannels": plan.total_channels,
                **summary,
            }
        )
        return 0 if summary["keepsUp"] else 2
    except KeyboardInterrupt:
        print("[benchmark] interrupted", file=sys.stderr, flush=True)
        return 130
    except BenchmarkError as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 1
    except psycopg2.Error as exc:
        print(f"[database-error] {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if (
            conn is not None
            and not args.cleanup
            and not args.keep_data
            and not cleanup_done
        ):
            try:
                cleanup = cleanup_prefix(conn, prefix)
                print(
                    f"[cleanup] fallback deleted={cleanup['totalDeleted']} "
                    f"remaining={cleanup['totalRemaining']}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[cleanup-error] {exc}", file=sys.stderr, flush=True)
        if conn is not None:
            conn.close()
        close_db_pool()


if __name__ == "__main__":
    raise SystemExit(main())
