#!/usr/bin/env python
"""End-to-end load test for every supported realtime alert scenario.

The tool creates isolated sensors, thresholds, and monitor samples under a
test-only prefix in ``t_monitor``. Detection is performed by the already
running ``AlertDetectionWorker``; this process never calls the detection engine.

Synthetic write mode is disabled. Only preflight and cleanup of historical
``benchall-*`` data remain available.

Covered scenarios:
  * threshold levels 1, 2, and 3
  * sensor stuck value
  * upper and lower out-of-range values
  * sensor offline
  * data acquisition instrument offline
  * one complete statistics window for every active channel
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

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
    ALERT_CATEGORY_DATA,
    ALERT_CATEGORY_DEVICE,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)

DEFAULT_HEALTH_URL = "http://127.0.0.1:8090/api/health"
DEFAULT_RATE = 10_000
DEFAULT_DURATION = 115
DEFAULT_BATCH_SIZE = 2_000
DEFAULT_PREFIX = "benchall"
WRITE_TARGET_TOLERANCE = 0.95

ACTIVE_SCENARIOS = {
    "normal": None,
    "threshold_l3": 15.0,
    "threshold_l2": 25.0,
    "threshold_l1": 35.0,
    "stuck": 5.0,
    "oor_upper": 20.0,
    "oor_lower": -20.0,
}
PREFIX_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


class StressTestError(RuntimeError):
    """Raised for an unsafe target, invalid option, or failed live check."""


@dataclass(frozen=True)
class ChannelSpec:
    sensor_no: str
    channel: str
    scenario: str
    value: float | None

    @property
    def key(self) -> str:
        return f"{self.sensor_no.lower()}:{self.channel}"


@dataclass(frozen=True)
class ExpectedAlert:
    sensor_no: str
    channel: str
    alert_type: str
    alert_level: int
    alert_category: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (
            self.sensor_no.lower(),
            self.channel,
            self.alert_type,
        )


@dataclass(frozen=True)
class TestPlan:
    prefix: str
    session_id: str
    channels: tuple[ChannelSpec, ...]
    offline_channels: tuple[ChannelSpec, ...]
    daq_channels: tuple[ChannelSpec, ...]
    instrument_id: str
    expected_alerts: tuple[ExpectedAlert, ...]

    @property
    def active_channels(self) -> tuple[ChannelSpec, ...]:
        return self.channels

    @property
    def all_channels(self) -> tuple[ChannelSpec, ...]:
        return self.channels + self.offline_channels + self.daq_channels


@dataclass(frozen=True)
class LoadResult:
    rows_written: int
    batches: int
    started_at: datetime
    simulation_end: datetime
    elapsed_seconds: float
    rows_per_second: float
    max_batch_seconds: float


@dataclass(eq=False)
class HealthObserver(threading.Thread):
    url: str
    timeout_seconds: float
    run_started_at: datetime
    interval_seconds: float = 0.5
    stop_event: threading.Event = field(default_factory=threading.Event)
    first_status: dict[str, Any] | None = None
    last_status: dict[str, Any] | None = None
    scan_times: set[str] = field(default_factory=set)
    scan_latencies_ms: list[float] = field(default_factory=list)
    scan_raw_points: int = 0
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(name="stress-health-observer", daemon=True)

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_seconds):
            try:
                status = fetch_json(self.url, self.timeout_seconds)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                message = str(exc)
                if not self.errors or self.errors[-1] != message:
                    self.errors.append(message)
                continue
            detection = status.get("detection")
            if not isinstance(detection, dict):
                self.errors.append("health response has no detection status")
                continue
            if self.first_status is None:
                self.first_status = detection
            self.last_status = detection
            scan_at_text = str(detection.get("lastAlertScanAt") or "")
            scan_at = parse_datetime(scan_at_text)
            if (
                scan_at is None
                or scan_at < self.run_started_at
                or scan_at_text in self.scan_times
            ):
                continue
            self.scan_times.add(scan_at_text)
            result = detection.get("lastAlertResult")
            if not isinstance(result, dict):
                continue
            elapsed_ms = result.get("elapsedMs")
            if elapsed_ms is not None:
                self.scan_latencies_ms.append(float(elapsed_ms))
            self.scan_raw_points += int(result.get("rawPointsProcessed") or 0)

    def stop(self) -> None:
        self.stop_event.set()
        self.join(timeout=5)

    def summary(self) -> dict[str, Any]:
        first = self.first_status or {}
        last = self.last_status or {}
        first_cycles = int(first.get("alertCycles") or 0)
        last_cycles = int(last.get("alertCycles") or 0)
        latencies = self.scan_latencies_ms
        last_scan = parse_datetime(str(last.get("lastAlertScanAt") or ""))
        return {
            "available": self.last_status is not None,
            "alertCycleDelta": max(0, last_cycles - first_cycles),
            "lastAlertScanAt": last.get("lastAlertScanAt"),
            "observedScans": len(latencies),
            "observedRawPoints": self.scan_raw_points,
            "scanLatencyMs": {
                "min": round(min(latencies), 2) if latencies else None,
                "avg": round(sum(latencies) / len(latencies), 2) if latencies else None,
                "max": round(max(latencies), 2) if latencies else None,
            },
            "lastAlertResult": last.get("lastAlertResult"),
            "lastStatisticsResult": last.get("lastResult"),
            "lastError": last.get("lastError"),
            "lastAlertError": last.get("lastAlertError"),
            "lastStatisticsError": last.get("lastStatisticsError"),
            "observerErrors": self.errors[-10:],
            "lastScanTimestamp": last_scan,
        }


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("preflight", "all", "cleanup"),
        default="preflight",
        help="preflight is read-only; all runs and cleans up; cleanup removes one session",
    )
    parser.add_argument("--sensor-prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--rows-per-second", type=positive_int, default=DEFAULT_RATE)
    parser.add_argument("--duration", type=positive_float, default=DEFAULT_DURATION)
    parser.add_argument("--batch-size", type=positive_int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--health-timeout", type=positive_float, default=3.0)
    parser.add_argument("--report-interval", type=positive_float, default=5.0)
    parser.add_argument("--confirm-database", default=None)
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--allow-production", action="store_true")
    parser.add_argument("--keep-data", action="store_true")
    return parser


def normalize_name(value: str, label: str) -> str:
    normalized = value.strip().lower().rstrip(".-")
    if len(normalized) < 2 or not PREFIX_PATTERN.fullmatch(normalized):
        raise StressTestError(
            f"{label} must contain lowercase letters, digits, '.', '_' or '-'"
        )
    return normalized


def resolve_prefix(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, str]:
    if args.mode == "cleanup" and not args.session_id:
        parser.error("--session-id is required for cleanup")
    try:
        base = normalize_name(args.sensor_prefix, "sensor prefix")
        session = normalize_name(
            args.session_id or uuid.uuid4().hex[:16],
            "session id",
        )
    except StressTestError as exc:
        parser.error(str(exc))
    if not (base.startswith("bench") or base.startswith("test-")):
        parser.error("sensor prefix must start with 'bench' or 'test-'")
    prefix = f"{base}-{session}-"
    if len(prefix) > 48:
        parser.error("full sensor prefix is too long")
    return session, prefix


def validate_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.mode in {"all", "cleanup"}:
        if not args.allow_write:
            parser.error(f"--mode {args.mode} requires --allow-write")
        if not args.confirm_database:
            parser.error(f"--mode {args.mode} requires --confirm-database")
    if args.mode == "preflight" and args.keep_data:
        parser.error("--keep-data is only valid with --mode all")
    if args.mode == "cleanup" and args.keep_data:
        parser.error("--keep-data is not valid with --mode cleanup")


def build_test_plan(prefix: str, session_id: str, active_channel_count: int = 20) -> TestPlan:
    if active_channel_count < 10:
        raise StressTestError("active channel count must be at least ten")
    normal_channels = active_channel_count - 6
    channels: list[ChannelSpec] = [
        ChannelSpec(prefix + "normal", f"c{index:02d}", "normal", None)
        for index in range(normal_channels)
    ]
    channels.extend(
        [
            ChannelSpec(prefix + "threshold", "l3", "threshold_l3", 15.0),
            ChannelSpec(prefix + "threshold", "l2", "threshold_l2", 25.0),
            ChannelSpec(prefix + "threshold", "l1", "threshold_l1", 35.0),
            ChannelSpec(prefix + "stuck", "x", "stuck", 5.0),
            ChannelSpec(prefix + "oor", "upper", "oor_upper", 20.0),
            ChannelSpec(prefix + "oor", "lower", "oor_lower", -20.0),
        ]
    )
    offline_channels = (
        ChannelSpec(prefix + "offline", "x", "offline", None),
    )
    instrument_id = prefix + "daq"
    if len(instrument_id) > 32:
        raise StressTestError("full prefix is too long for the instrument id column")
    daq_channels = (
        ChannelSpec(prefix + "daq-sensor", "x", "daq_offline", None),
        ChannelSpec(prefix + "daq-sensor", "y", "daq_offline", None),
    )
    expected = [
        ExpectedAlert(prefix + "threshold", "l3", ALERT_TYPE_THRESHOLD, 3, ALERT_CATEGORY_DATA),
        ExpectedAlert(prefix + "threshold", "l2", ALERT_TYPE_THRESHOLD, 2, ALERT_CATEGORY_DATA),
        ExpectedAlert(prefix + "threshold", "l1", ALERT_TYPE_THRESHOLD, 1, ALERT_CATEGORY_DATA),
        ExpectedAlert(prefix + "stuck", "x", ALERT_TYPE_STUCK, 1, ALERT_CATEGORY_DATA),
        ExpectedAlert(prefix + "oor", "upper", ALERT_TYPE_OOR, 1, ALERT_CATEGORY_DATA),
        ExpectedAlert(prefix + "oor", "lower", ALERT_TYPE_OOR, 1, ALERT_CATEGORY_DATA),
        ExpectedAlert(prefix + "offline", "x", ALERT_TYPE_OFFLINE, 1, ALERT_CATEGORY_DEVICE),
        ExpectedAlert(prefix + "daq-sensor", "x", ALERT_TYPE_OFFLINE, 1, ALERT_CATEGORY_DEVICE),
        ExpectedAlert(prefix + "daq-sensor", "y", ALERT_TYPE_OFFLINE, 1, ALERT_CATEGORY_DEVICE),
        ExpectedAlert(instrument_id, "__DAQ__", ALERT_TYPE_DAQ_OFFLINE, 1, ALERT_CATEGORY_DEVICE),
    ]
    return TestPlan(
        prefix=prefix,
        session_id=session_id,
        channels=tuple(channels),
        offline_channels=offline_channels,
        daq_channels=daq_channels,
        instrument_id=instrument_id,
        expected_alerts=tuple(expected),
    )


def value_for(spec: ChannelSpec, row_index: int) -> float:
    if spec.scenario == "normal":
        return round(math.sin(row_index / 31.0) * 0.8, 6)
    value = ACTIVE_SCENARIOS.get(spec.scenario)
    if value is None:
        raise StressTestError(f"unsupported active scenario: {spec.scenario}")
    if spec.scenario == "stuck":
        return value
    # Keep alert conditions active without making every alert channel a
    # constant signal, which would also satisfy the stuck-value rule.
    amplitude = 0.5 if spec.scenario.startswith("oor_") else 0.2
    return round(value + amplitude * math.sin(row_index / 7.0), 6)


def generate_batch(
    channels: Sequence[ChannelSpec],
    start_time: datetime,
    start_index: int,
    count: int,
    rows_per_second: int,
) -> list[tuple[str, str, float, datetime]]:
    rows: list[tuple[str, str, float, datetime]] = []
    for offset in range(count):
        index = start_index + offset
        spec = channels[index % len(channels)]
        sample_time = start_time + timedelta(seconds=index / rows_per_second)
        rows.append(
            (
                spec.sensor_no,
                spec.channel,
                value_for(spec, index),
                sample_time,
            )
        )
    return rows


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "alert-analysis-stress-test"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("health endpoint did not return a JSON object")
    return payload


def database_now(conn: Any) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT LOCALTIMESTAMP")
        row = cur.fetchone()
    if row is None:
        raise StressTestError("could not read database time")
    return row[0]


def database_identity(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_database(), current_user,
                   inet_server_addr()::text, inet_server_port()
            """
        )
        row = cur.fetchone()
    return {
        "database": row[0],
        "user": row[1],
        "serverAddress": row[2],
        "serverPort": row[3],
    }


def verify_backend(health: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    engine = health.get("engine")
    detection = health.get("detection")
    if not isinstance(engine, dict) or not isinstance(detection, dict):
        raise StressTestError("health response is missing engine or detection status")
    if not detection.get("running"):
        raise StressTestError("AlertDetectionWorker is not running")
    switches = engine.get("alertTypeSwitches") or {}
    disabled = [
        alert_type
        for alert_type in (
            ALERT_TYPE_THRESHOLD,
            ALERT_TYPE_OFFLINE,
            ALERT_TYPE_DAQ_OFFLINE,
            ALERT_TYPE_STUCK,
            ALERT_TYPE_OOR,
        )
        if switches.get(alert_type) is False
    ]
    if disabled:
        raise StressTestError(f"alert types are disabled in the running backend: {disabled}")
    for field_name in ("lastError", "lastAlertError", "lastStatisticsError"):
        if detection.get(field_name):
            raise StressTestError(f"worker reports {field_name}: {detection[field_name]}")
    return {
        "loadedRulesCount": int(engine.get("loadedRulesCount") or 0),
        "stationCode": engine.get("stationCode"),
        "confirmSeconds": float(detection.get("confirmSeconds") or 0),
        "stuckSeconds": float(detection.get("stuckSeconds") or 0),
        "offlineSeconds": float(detection.get("offlineSeconds") or 0),
        "intervalSeconds": float(detection.get("intervalSeconds") or 1),
        "windowMinutes": int(detection.get("windowMinutes") or 1),
        "lagSeconds": float(detection.get("lagSeconds") or 0),
    }


def validate_duration(
    args: argparse.Namespace,
    config: dict[str, Any],
) -> None:
    # Rule refresh runs every few seconds. Keep enough data after the refresh
    # for both the confirm time and the long stuck-value window.
    stuck_requirement = config["stuckSeconds"] + 15
    stats_requirement = (
        config["windowMinutes"] * 60
        + config["lagSeconds"]
        + max(15.0, config["intervalSeconds"] * 5)
    )
    required = max(stuck_requirement, stats_requirement)
    if args.duration < required:
        raise StressTestError(
            f"--duration {args.duration:g}s is too short for the running config; "
            f"use at least {required:.1f}s"
        )


def count_prefix_rows(
    conn: Any,
    table: str,
    time_column: str,
    prefix: str,
    start_time: datetime,
    end_time: datetime,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*)
            FROM public.{table}
            WHERE {time_column} >= %s
              AND {time_column} < %s
              AND LEFT(LOWER(sensor_no), %s) = %s
            """,
            (start_time, end_time, len(prefix), prefix),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def cleanup_plan(conn: Any, plan: TestPlan, now: datetime) -> dict[str, Any]:
    start_time = now - timedelta(hours=1)
    end_time = now + timedelta(hours=1)
    deleted: dict[str, int] = {}
    conn.rollback()
    try:
        with conn.cursor() as cur:
            for table, time_column in (
                ("t_monitor", "time"),
                ("t_alert_record", "create_time"),
                ("t_stat_record", "stat_time"),
            ):
                cur.execute(
                    f"""
                    DELETE FROM public.{table}
                    WHERE {time_column} >= %s
                      AND {time_column} < %s
                      AND LEFT(LOWER(sensor_no), %s) = %s
                    """,
                    (start_time, end_time, len(plan.prefix), plan.prefix),
                )
                deleted[table] = int(cur.rowcount)
            for table in (
                "t_alert_threshold",
                "t_sensor_channel",
                "t_sensor",
                "t_collect_instrument",
                "t_collect_box",
            ):
                cur.execute(
                    f"""
                    DELETE FROM public.{table}
                    WHERE LEFT(LOWER(sensor_no), %s) = %s
                    """
                    if table not in {"t_collect_instrument", "t_collect_box"}
                    else """
                    DELETE FROM public.""" + table + """
                    WHERE LEFT(LOWER(id), %s) = %s
                    """,
                    (len(plan.prefix), plan.prefix),
                )
                deleted[table] = int(cur.rowcount)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "prefix": plan.prefix,
        "deleted": deleted,
        "totalDeleted": sum(deleted.values()),
    }


def insert_metadata(
    conn: Any,
    plan: TestPlan,
    rows_per_second: int,
    station_code: str,
) -> int:
    per_channel_rate = rows_per_second / len(plan.active_channels)
    sensor_specs: dict[str, list[ChannelSpec]] = {}
    for spec in plan.all_channels:
        sensor_specs.setdefault(spec.sensor_no, []).append(spec)

    with conn.cursor() as cur:
        box_id = plan.prefix + "box"
        active_instrument_id = plan.prefix + "active"
        if len(active_instrument_id) > 32:
            raise StressTestError("full prefix is too long for the instrument id column")
        cur.execute(
            """
            INSERT INTO public.t_collect_box (id, number, name, station)
            VALUES (%s, %s, %s, %s)
            """,
            (box_id, plan.session_id, "Benchmark Box " + plan.session_id, station_code),
        )
        cur.execute(
            """
            INSERT INTO public.t_collect_instrument (
                id, number, name, model, manufacturer, box_id, channel_count, create_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                active_instrument_id,
                plan.session_id + "-active",
                "Benchmark Active DAQ " + plan.session_id,
                "benchmark",
                "benchmark",
                box_id,
                len(plan.active_channels),
                "stress-test",
            ),
        )
        cur.execute(
            """
            INSERT INTO public.t_collect_instrument (
                id, number, name, model, manufacturer, box_id, channel_count, create_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                plan.instrument_id,
                plan.session_id,
                "Benchmark DAQ " + plan.session_id,
                "benchmark",
                "benchmark",
                box_id,
                len(plan.daq_channels),
                "stress-test",
            ),
        )

        sensor_rows = []
        channel_rows = []
        rule_rows = []
        for sensor_no, channels in sorted(sensor_specs.items()):
            is_offline = channels[0].scenario == "offline"
            is_daq = channels[0].scenario == "daq_offline"
            frequency = per_channel_rate if not (is_offline or is_daq) else 1.0
            sensor_rows.append(
                (
                    uuid.uuid4().hex,
                    sensor_no,
                    "benchmark",
                    plan.instrument_id if is_daq else active_instrument_id,
                    "Benchmark " + channels[0].scenario,
                    "benchmark",
                    "benchmark",
                    "1",
                    "-100~100",
                    str(frequency),
                    "benchmark",
                    "stress-test",
                )
            )
            for spec in channels:
                channel_rows.append(
                    (
                        uuid.uuid4().hex,
                        sensor_no,
                        f"Benchmark {spec.scenario}",
                        spec.channel,
                        "unit",
                        "benchmark",
                        spec.channel,
                        "stress-test",
                    )
                )
                if spec.scenario == "oor_upper":
                    levels = (None, None, None, None, None, None)
                    range_min, range_max = -10.0, 10.0
                elif spec.scenario == "oor_lower":
                    levels = (None, None, None, None, None, None)
                    range_min, range_max = -10.0, 10.0
                elif spec.scenario == "stuck":
                    levels = (90.0, 80.0, 70.0, -90.0, -80.0, -70.0)
                    range_min, range_max = -100.0, 100.0
                else:
                    levels = (30.0, 20.0, 10.0, -30.0, -20.0, -10.0)
                    range_min, range_max = -100.0, 100.0
                rule_rows.append(
                    (
                        uuid.uuid4().hex,
                        sensor_no,
                        "benchmark",
                        spec.channel,
                        *levels,
                        range_min,
                        range_max,
                        "stress-test",
                        "stress-test",
                    )
                )

        execute_values(
            cur,
            """
            INSERT INTO public.t_sensor (
                id, sensor_no, type, instrument_id, name, model, manufacturer,
                precision, range, frequency, position, create_by
            ) VALUES %s
            """,
            sensor_rows,
            page_size=len(sensor_rows),
        )
        execute_values(
            cur,
            """
            INSERT INTO public.t_sensor_channel (
                id, sensor_no, name, instrument_channel_no, unit,
                monitor_content, monitor_code, create_by
            ) VALUES %s
            """,
            channel_rows,
            page_size=len(channel_rows),
        )
        execute_values(
            cur,
            """
            INSERT INTO public.t_alert_threshold (
                id, sensor_no, physical_quantity, channel,
                level1_upper, level2_upper, level3_upper,
                level1_lower, level2_lower, level3_lower,
                range_min, range_max, create_by, update_by
            ) VALUES %s
            """,
            rule_rows,
            page_size=len(rule_rows),
        )
    conn.commit()
    return len(rule_rows)


def wait_for_rules(
    health_url: str,
    timeout_seconds: float,
    expected_count: int,
    timeout: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_count = None
    while time.monotonic() < deadline:
        health = fetch_json(health_url, timeout_seconds)
        engine = health.get("engine") or {}
        count = int(engine.get("loadedRulesCount") or 0)
        last_count = count
        if count >= expected_count:
            return health
        time.sleep(0.5)
    raise StressTestError(
        f"worker did not load {expected_count} rules; last loaded count was {last_count}"
    )


def run_load(
    conn: Any,
    plan: TestPlan,
    args: argparse.Namespace,
) -> tuple[LoadResult, dict[tuple[str, str, str], dict[str, Any]]]:
    start_time = database_now(conn) + timedelta(seconds=1)
    total_rows = int(round(args.rows_per_second * args.duration))
    batch_size = min(args.batch_size, total_rows)
    batches = math.ceil(total_rows / batch_size)
    started = time.perf_counter()
    next_report = started + args.report_interval
    next_audit = started
    rows_written = 0
    max_batch_seconds = 0.0
    live_alerts: dict[tuple[str, str, str], dict[str, Any]] = {}
    print(
        f"[load] prefix={plan.prefix} target={args.rows_per_second}/s "
        f"duration={args.duration:g}s rows={total_rows} "
        f"channels={len(plan.active_channels)} batch={batch_size}",
        flush=True,
    )

    while rows_written < total_rows:
        count = min(batch_size, total_rows - rows_written)
        target_elapsed = rows_written / args.rows_per_second
        remaining = target_elapsed - (time.perf_counter() - started)
        if remaining > 0:
            time.sleep(remaining)
        batch = generate_batch(
            plan.active_channels,
            start_time,
            rows_written,
            count,
            args.rows_per_second,
        )
        batch_started = time.perf_counter()
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
        batch_seconds = time.perf_counter() - batch_started
        max_batch_seconds = max(max_batch_seconds, batch_seconds)
        rows_written += count

        now = time.perf_counter()
        if now >= next_audit:
            live_alerts.update(fetch_alert_snapshot(conn, plan, start_time))
            next_audit = now + args.report_interval
        if now >= next_report:
            elapsed = max(now - started, 0.001)
            print(
                f"[load] rows={rows_written} actual={rows_written / elapsed:.1f}/s "
                f"alertsSeen={len(live_alerts)}",
                flush=True,
            )
            next_report = now + args.report_interval

    elapsed = max(time.perf_counter() - started, 0.001)
    actual_rate = rows_written / elapsed
    simulation_end = start_time + timedelta(seconds=args.duration)
    live_alerts.update(fetch_alert_snapshot(conn, plan, start_time))
    result = LoadResult(
        rows_written=rows_written,
        batches=batches,
        started_at=start_time,
        simulation_end=simulation_end,
        elapsed_seconds=elapsed,
        rows_per_second=actual_rate,
        max_batch_seconds=max_batch_seconds,
    )
    print(
        f"[load] complete rows={rows_written} actual={actual_rate:.1f}/s "
        f"batches={batches}",
        flush=True,
    )
    return result, live_alerts


def fetch_alert_snapshot(
    conn: Any,
    plan: TestPlan,
    start_time: datetime,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT sensor_no, channel, alert_type, alert_level, alert_category,
                   trigger_val, create_time, end_time
            FROM public.t_alert_record
            WHERE create_time >= %s
              AND create_time < %s
              AND LEFT(LOWER(sensor_no), %s) = %s
            ORDER BY seq ASC
            """,
            (
                start_time - timedelta(minutes=2),
                database_now(conn) + timedelta(minutes=1),
                len(plan.prefix),
                plan.prefix,
            ),
        )
        rows = cur.fetchall()
    out: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        item = {
            "sensor_no": str(row[0]).lower(),
            "channel": str(row[1] or ""),
            "alert_type": str(row[2]),
            "alert_level": int(row[3]),
            "alert_category": int(row[4]),
            "trigger_val": float(row[5]) if row[5] is not None else None,
            "create_time": row[6],
            "end_time": row[7],
        }
        key = (item["sensor_no"], item["channel"], item["alert_type"])
        prior = out.get(key)
        if prior is None or (
            prior.get("end_time") is not None and item.get("end_time") is None
        ):
            out[key] = item
    return out


def wait_for_expected_alerts(
    conn: Any,
    plan: TestPlan,
    observed: dict[tuple[str, str, str], dict[str, Any]],
    timeout: float = 20.0,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    expected = {item.key for item in plan.expected_alerts}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed.update(fetch_alert_snapshot(conn, plan, min(
            item["create_time"] for item in observed.values()
        ) if observed else database_now(conn) - timedelta(minutes=2)))
        if expected <= set(observed):
            return observed
        time.sleep(0.5)
    return observed


def verify_alerts(
    plan: TestPlan,
    observed: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    expected_keys = {item.key for item in plan.expected_alerts}
    for item in plan.expected_alerts:
        row = observed.get(item.key)
        label = f"{item.sensor_no}:{item.channel}:{item.alert_type}"
        if row is None:
            failures.append(f"missing alert {label}")
            continue
        if int(row["alert_level"]) != item.alert_level:
            failures.append(
                f"{label} level expected {item.alert_level}, got {row['alert_level']}"
            )
        if int(row["alert_category"]) != item.alert_category:
            failures.append(
                f"{label} category expected {item.alert_category}, "
                f"got {row['alert_category']}"
            )
    unexpected = sorted(set(observed) - expected_keys)
    if unexpected:
        failures.append(f"unexpected alerts appeared during load: {unexpected}")
    return {
        "passed": not failures,
        "expectedCount": len(expected_keys),
        "observedExpectedCount": len(expected_keys & set(observed)),
        "observedTotalCount": len(observed),
        "failures": failures,
        "alerts": [
            {
                "sensorNo": key[0],
                "channel": key[1],
                "alertType": key[2],
                "level": observed[key]["alert_level"],
                "category": observed[key]["alert_category"],
                "createTime": observed[key]["create_time"].isoformat(sep=" ", timespec="milliseconds"),
                "endTime": (
                    observed[key]["end_time"].isoformat(sep=" ", timespec="milliseconds")
                    if observed[key]["end_time"] is not None
                    else None
                ),
            }
            for key in sorted(expected_keys & set(observed))
        ],
    }


def verify_statistics(
    conn: Any,
    plan: TestPlan,
    result: LoadResult,
    window_minutes: int,
    timeout: float = 30.0,
) -> dict[str, Any]:
    period_type = f"{window_minutes}m"
    expected_channels = {
        (item.sensor_no.lower(), item.channel)
        for item in plan.active_channels
    }
    deadline = time.monotonic() + timeout
    rows: list[tuple[Any, ...]] = []
    while time.monotonic() < deadline:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT LOWER(sensor_no), channel, COUNT(*), MIN(sample_count),
                       MIN(val_min), MAX(val_max)
                FROM public.t_stat_record
                WHERE stat_time >= %s
                  AND stat_time < %s
                  AND period_type = %s
                  AND LEFT(LOWER(sensor_no), %s) = %s
                GROUP BY LOWER(sensor_no), channel
                """,
                (
                    result.started_at.replace(second=0, microsecond=0),
                    result.simulation_end + timedelta(minutes=window_minutes),
                    period_type,
                    len(plan.prefix),
                    plan.prefix,
                ),
            )
            rows = cur.fetchall()
        found = {(str(row[0]), str(row[1] or "")) for row in rows}
        if expected_channels <= found:
            break
        time.sleep(1)

    grouped = {
        (str(row[0]), str(row[1] or "")): {
            "windowCount": int(row[2]),
            "minSampleCount": int(row[3] or 0),
            "minValue": float(row[4]) if row[4] is not None else None,
            "maxValue": float(row[5]) if row[5] is not None else None,
        }
        for row in rows
    }
    failures: list[str] = []
    for key in sorted(expected_channels):
        stats = grouped.get(key)
        if stats is None:
            failures.append(f"missing {period_type} statistics for {key}")
        elif stats["minSampleCount"] <= 0:
            failures.append(f"zero sample count in statistics for {key}")
    for spec in plan.active_channels:
        stats = grouped.get((spec.sensor_no.lower(), spec.channel))
        if stats is None or spec.value is None:
            continue
        if spec.scenario == "stuck":
            bounds = (spec.value, spec.value)
        else:
            amplitude = 0.5 if spec.scenario.startswith("oor_") else 0.2
            bounds = (spec.value - amplitude, spec.value + amplitude)
        stats_ok = (
            stats["minValue"] is not None
            and stats["maxValue"] is not None
            and stats["minValue"] >= bounds[0] - 1e-6
            and stats["maxValue"] <= bounds[1] + 1e-6
        )
        if not stats_ok:
            failures.append(
                f"statistics mismatch for {spec.sensor_no}:{spec.channel}: "
                f"expected range {bounds}, got {stats}"
            )
    return {
        "passed": not failures,
        "periodType": period_type,
        "expectedChannelCount": len(expected_channels),
        "observedChannelCount": len(expected_channels & set(grouped)),
        "rowCount": sum(item["windowCount"] for item in grouped.values()),
        "failures": failures,
    }


def wait_for_worker_caught_up(
    health_url: str,
    timeout_seconds: float,
    simulation_end: datetime,
    timeout: float = 20.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        health = fetch_json(health_url, timeout_seconds)
        detection = health.get("detection") or {}
        last = detection
        scan_at = parse_datetime(detection.get("lastAlertScanAt"))
        if scan_at is not None and scan_at >= simulation_end:
            return detection
        time.sleep(0.5)
    raise StressTestError(
        "worker did not catch up to the simulation end; "
        f"last scan was {last.get('lastAlertScanAt')}"
    )


def preflight(
    conn: Any,
    args: argparse.Namespace,
    plan: TestPlan,
    health: dict[str, Any],
) -> dict[str, Any]:
    identity = database_identity(conn)
    config = verify_backend(health, args)
    validate_duration(args, config)
    if args.mode != "preflight" and args.confirm_database != identity["database"]:
        raise StressTestError(
            f"--confirm-database must exactly match {identity['database']}"
        )
    if identity["database"].lower().endswith("_prod") and not args.allow_production:
        raise StressTestError("refusing a database ending in '_prod' without --allow-production")
    now = database_now(conn)
    recent_monitor = count_prefix_rows(
        conn,
        "t_monitor",
        "time",
        plan.prefix,
        now - timedelta(hours=1),
        now + timedelta(hours=1),
    )
    recent_alerts = count_prefix_rows(
        conn,
        "t_alert_record",
        "create_time",
        plan.prefix,
        now - timedelta(hours=1),
        now + timedelta(hours=1),
    )
    return {
        "identity": identity,
        "config": config,
        "activeChannels": len(plan.active_channels),
        "totalConfiguredChannels": len(plan.all_channels),
        "expectedAlerts": len(plan.expected_alerts),
        "estimatedRows": int(round(args.rows_per_second * args.duration)),
        "recentPrefixMonitorRows": recent_monitor,
        "recentPrefixAlertRows": recent_alerts,
    }


def print_summary(summary: dict[str, Any]) -> None:
    print("\n[summary]", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=True, default=str), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_cli(args, parser)
    session_id, prefix = resolve_prefix(args, parser)
    plan = build_test_plan(prefix, session_id)
    settings = Settings(debug=False)
    conn: Any | None = None
    observer: HealthObserver | None = None
    summary: dict[str, Any] = {
        "mode": args.mode,
        "targetRowsPerSecond": args.rows_per_second,
        "targetDurationSeconds": args.duration,
        "sensorPrefix": prefix,
        "sessionId": session_id,
    }
    cleanup_done = False
    exit_code = 0
    try:
        conn = psycopg2.connect(
            settings.dsn,
            application_name="alert_analysis_stress_all_alerts",
        )
        if settings.environment == "production" and not args.allow_production:
            raise StressTestError(
                "refusing ENVIRONMENT=production without --allow-production"
            )
        health = fetch_json(args.health_url, args.health_timeout)
        preflight_result = preflight(conn, args, plan, health)
        summary["preflight"] = preflight_result
        if args.mode == "preflight":
            if preflight_result["recentPrefixMonitorRows"]:
                raise StressTestError("exact prefix already has recent monitor rows")
            print_summary(summary)
            return 0

        if args.mode == "cleanup":
            cleanup = cleanup_plan(conn, plan, database_now(conn))
            summary["cleanup"] = cleanup
            print_summary(summary)
            return 0 if cleanup["totalDeleted"] >= 0 else 1

        raise StressTestError(
            "synthetic sensor load testing is disabled; use existing real sensors "
            "from t_sensor and t_alert_threshold"
        )

        if preflight_result["recentPrefixMonitorRows"] or preflight_result["recentPrefixAlertRows"]:
            raise StressTestError("exact prefix already has recent monitor or alert rows")

        baseline_rules = int(preflight_result["config"]["loadedRulesCount"])
        expected_rules = baseline_rules + len(plan.all_channels)
        setup_started = database_now(conn)
        station_code = str(
            preflight_result["config"].get("stationCode") or ""
        ).strip()
        if not station_code:
            raise StressTestError("worker health response is missing engine.stationCode")
        rule_count = insert_metadata(
            conn,
            plan,
            args.rows_per_second,
            station_code,
        )
        summary["setup"] = {
            "rulesInserted": rule_count,
            "activeChannels": len(plan.active_channels),
        }
        print(
            f"[setup] rules={rule_count} expectedAlerts={len(plan.expected_alerts)}",
            flush=True,
        )

        observer_start = database_now(conn)
        observer = HealthObserver(
            args.health_url,
            args.health_timeout,
            observer_start,
        )
        observer.start()
        load_result, live_alerts = run_load(conn, plan, args)
        health = wait_for_rules(
            args.health_url,
            args.health_timeout,
            expected_rules,
        )
        summary["setup"]["workerLoadedRules"] = int(
            (health.get("engine") or {}).get("loadedRulesCount") or 0
        )
        live_alerts = wait_for_expected_alerts(conn, plan, live_alerts)
        wait_for_worker_caught_up(
            args.health_url,
            args.health_timeout,
            load_result.simulation_end,
        )
        observer_summary = observer.summary()
        observer.stop()
        observer = None

        final_health = fetch_json(args.health_url, args.health_timeout)
        detection = final_health.get("detection") or {}
        worker_summary = {
            **(observer_summary or {}),
            "available": bool(detection),
            "lastAlertScanAt": detection.get("lastAlertScanAt"),
            "lastAlertResult": detection.get("lastAlertResult"),
            "lastStatisticsResult": detection.get("lastResult"),
            "lastError": detection.get("lastError"),
            "lastAlertError": detection.get("lastAlertError"),
            "lastStatisticsError": detection.get("lastStatisticsError"),
        }

        alert_verification = verify_alerts(plan, live_alerts)
        stats_verification = verify_statistics(
            conn,
            plan,
            load_result,
            int(preflight_result["config"]["windowMinutes"]),
        )
        inserted_rows = count_prefix_rows(
            conn,
            "t_monitor",
            "time",
            plan.prefix,
            load_result.started_at - timedelta(seconds=2),
            load_result.simulation_end + timedelta(seconds=2),
        )
        write_kept_up = (
            load_result.rows_per_second
            >= args.rows_per_second * WRITE_TARGET_TOLERANCE
        )
        scan_latency = (worker_summary.get("scanLatencyMs") or {}).get("max")
        scan_latency_kept_up = (
            scan_latency is None
            or scan_latency
            <= preflight_result["config"]["intervalSeconds"] * 1500
        )
        keeps_up = (
            write_kept_up
            and scan_latency_kept_up
            and alert_verification["passed"]
            and stats_verification["passed"]
            and inserted_rows == load_result.rows_written
        )
        summary["load"] = {
            "rowsWritten": load_result.rows_written,
            "rowsReadBack": inserted_rows,
            "rowsPerSecond": round(load_result.rows_per_second, 2),
            "writeKeptUp": write_kept_up,
            "elapsedSeconds": round(load_result.elapsed_seconds, 3),
            "batches": load_result.batches,
            "maxBatchSeconds": round(load_result.max_batch_seconds, 3),
            "startedAt": load_result.started_at.isoformat(sep=" ", timespec="milliseconds"),
            "simulationEnd": load_result.simulation_end.isoformat(
                sep=" ",
                timespec="milliseconds",
            ),
        }
        summary["worker"] = worker_summary
        summary["alerts"] = alert_verification
        summary["statistics"] = stats_verification
        summary["capacity"] = {
            "writeKeptUp": write_kept_up,
            "scanLatencyKeptUp": scan_latency_kept_up,
            "maxScanLatencyMs": scan_latency,
            "intervalSeconds": preflight_result["config"]["intervalSeconds"],
            "keepsUp": keeps_up,
        }
        summary["verification"] = {
            "allAlertScenariosPassed": alert_verification["passed"],
            "statisticsPassed": stats_verification["passed"],
            "rowCountPassed": inserted_rows == load_result.rows_written,
            "passed": keeps_up,
        }
        exit_code = 0 if keeps_up else 2

        if args.keep_data:
            summary["cleanup"] = {"skipped": True, "setupStartedAt": setup_started}
            print("[cleanup] skipped because --keep-data was supplied", flush=True)
        else:
            cleanup = cleanup_plan(conn, plan, database_now(conn))
            cleanup_done = True
            summary["cleanup"] = cleanup
            print(
                f"[cleanup] deleted={cleanup['totalDeleted']}",
                flush=True,
            )
            try:
                wait_for_rules(
                    args.health_url,
                    args.health_timeout,
                    baseline_rules,
                    timeout=15.0,
                )
            except StressTestError as exc:
                summary["cleanup"]["reloadWarning"] = str(exc)

        print_summary(summary)
        return exit_code
    except KeyboardInterrupt:
        print("[stress-test] interrupted", file=sys.stderr, flush=True)
        exit_code = 130
    except (StressTestError, psycopg2.Error, OSError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        summary["error"] = str(exc)
        exit_code = 1
        print_summary(summary)
    finally:
        if observer is not None:
            observer.stop()
        if conn is not None:
            if (
                args.mode == "all"
                and not args.keep_data
                and not cleanup_done
            ):
                try:
                    cleanup = cleanup_plan(conn, plan, database_now(conn))
                    print(
                        f"[cleanup] fallback deleted={cleanup['totalDeleted']}",
                        flush=True,
                    )
                except Exception as exc:
                    print(f"[cleanup-error] {exc}", file=sys.stderr, flush=True)
            conn.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
