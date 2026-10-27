#!/usr/bin/env python
"""Exercise every alert type against the station's existing real rules.

The tool never creates sensors, channels, thresholds, or collectors. It writes
only to ``t_monitor`` for channels already configured in ``t_alert_threshold``
and lets the running ``AlertDetectionWorker`` perform detection and statistics.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings
from app.domain.alert_rules.base import MIN_OFFLINE_PERIODS, required_sample_count
from app.domain.alert_types import (
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)
from app.domain.engine import AlertAnalyticsEngine

DEFAULT_HEALTH_URL = "http://127.0.0.1:8090/api/health"


class StressError(RuntimeError):
    """Raised when a safety or verification check fails."""


@dataclass(frozen=True)
class RealChannel:
    sensor_no: str
    channel: str
    frequency_hz: float
    instrument_id: str
    physical_min: float
    physical_max: float
    normal_value: float
    threshold_value: float
    threshold_level: int
    extreme_value: float
    threshold_samples: int
    oor_samples: int
    stuck_samples: int
    offline_timeout_ms: float

    @property
    def key(self) -> tuple[str, str]:
        return (self.sensor_no.lower(), self.channel)

    @property
    def sample_interval_seconds(self) -> float:
        return 1.0 / self.frequency_hz


@dataclass(frozen=True)
class ExpectedAlert:
    sensor_no: str
    channel: str
    alert_type: str
    level: int

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.sensor_no.lower(), self.channel, self.alert_type)


@dataclass
class HealthObserver:
    url: str
    timeout: float
    interval: float = 0.5
    stop_requested: bool = False
    samples: list[dict[str, Any]] = field(default_factory=list)

    def run(self) -> None:
        while not self.stop_requested:
            try:
                health = fetch_json(self.url, self.timeout)
                detection = health.get("detection") or {}
                if isinstance(detection, dict):
                    self.samples.append(detection)
            except (OSError, ValueError):
                pass
            time.sleep(self.interval)

    def summary(self) -> dict[str, Any]:
        elapsed_values: list[float] = []
        backlog_values: list[float] = []
        for detection in self.samples:
            result = detection.get("lastAlertResult")
            if isinstance(result, dict) and result.get("elapsedMs") is not None:
                elapsed_values.append(float(result["elapsedMs"]))
            backlog_values.append(float(detection.get("alertBacklogSeconds") or 0))
        last = self.samples[-1] if self.samples else {}
        return {
            "observedSamples": len(self.samples),
            "maxScanElapsedMs": round(max(elapsed_values), 2) if elapsed_values else None,
            "maxBacklogSeconds": round(max(backlog_values), 3) if backlog_values else 0,
            "lastAlertScanAt": last.get("lastAlertScanAt"),
            "lastError": last.get("lastError"),
            "lastAlertError": last.get("lastAlertError"),
            "lastStatisticsError": last.get("lastStatisticsError"),
        }


def positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "all"), default="preflight")
    parser.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    parser.add_argument("--health-timeout", type=positive_float, default=5.0)
    parser.add_argument("--confirm-database")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("--report-json")
    return parser


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "real-alert-stress"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise StressError("health endpoint did not return a JSON object")
    return payload


def database_now(conn: Any) -> datetime:
    with conn.cursor() as cur:
        cur.execute("SELECT LOCALTIMESTAMP")
        row = cur.fetchone()
    conn.rollback()
    if row is None:
        raise StressError("database did not return a timestamp")
    return row[0]


def database_identity(conn: Any) -> dict[str, Any]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT current_database() AS database,
                   current_user AS username,
                   inet_server_addr()::text AS server_address,
                   inet_server_port() AS server_port
            """
        )
        return dict(cur.fetchone() or {})


def verify_health(health: dict[str, Any]) -> dict[str, Any]:
    engine = health.get("engine") or {}
    detection = health.get("detection") or {}
    if not engine or not detection:
        raise StressError("health response is missing engine or detection status")
    if not detection.get("running"):
        raise StressError("AlertDetectionWorker is not running")
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
        raise StressError(f"alert types are disabled: {disabled}")
    for name in ("lastError", "lastAlertError", "lastStatisticsError"):
        if detection.get(name):
            raise StressError(f"worker reports {name}: {detection[name]}")
    station_code = str(engine.get("stationCode") or "").strip()
    if not station_code:
        raise StressError("health response is missing engine.stationCode")
    return {
        "stationCode": station_code,
        "loadedRulesCount": int(engine.get("loadedRulesCount") or 0),
        "intervalSeconds": float(detection.get("intervalSeconds") or 1),
        "windowMinutes": int(detection.get("windowMinutes") or 1),
        "lagSeconds": float(detection.get("lagSeconds") or 0),
    }


def float_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


def safe_value(rule: dict[str, Any], physical_min: float, physical_max: float) -> float:
    uppers = [
        float(rule[f"level{level}_upper"])
        for level in (3, 2, 1)
        if rule.get(f"level{level}_upper") is not None
    ]
    lowers = [
        float(rule[f"level{level}_lower"])
        for level in (3, 2, 1)
        if rule.get(f"level{level}_lower") is not None
    ]
    if uppers and lowers:
        return (max(lowers) + min(uppers)) / 2.0
    if uppers:
        return max(physical_min, min(uppers) - 0.01)
    if lowers:
        return min(physical_max, max(lowers) + 0.01)
    return (physical_min + physical_max) / 2.0


def threshold_value(
    rule: dict[str, Any],
    physical_min: float,
    physical_max: float,
) -> tuple[int, float]:
    for level in (1, 2, 3):
        upper = float_or_none(rule.get(f"level{level}_upper"))
        if upper is not None:
            margin = max(abs(upper) * 1e-5, 1e-5)
            candidate = upper + margin
            if candidate < physical_max:
                return level, candidate
    for level in (1, 2, 3):
        lower = float_or_none(rule.get(f"level{level}_lower"))
        if lower is not None:
            margin = max(abs(lower) * 1e-5, 1e-5)
            candidate = lower - margin
            if candidate > physical_min:
                return level, candidate
    # A few real rules place the threshold on the physical range boundary.
    # Such channels still produce both threshold and out-of-range alerts.
    for level in (1, 2, 3):
        upper = float_or_none(rule.get(f"level{level}_upper"))
        if upper is not None:
            return level, upper + max(abs(upper) * 1e-5, 1e-5)
    for level in (1, 2, 3):
        lower = float_or_none(rule.get(f"level{level}_lower"))
        if lower is not None:
            return level, lower - max(abs(lower) * 1e-5, 1e-5)
    raise StressError(f"no threshold bound for {rule['sensor_no']}:{rule['channel']}")


def load_channels(conn: Any, station_code: str) -> list[RealChannel]:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT at.*, s.frequency, s.instrument_id
            FROM public.t_alert_threshold AS at
            JOIN public.t_sensor AS s
              ON LOWER(BTRIM(s.sensor_no)) = LOWER(BTRIM(at.sensor_no))
            JOIN public.t_collect_instrument AS ci ON ci.id = s.instrument_id
            JOIN public.t_collect_box AS cb ON cb.id = ci.box_id
            WHERE cb.station = %s
            ORDER BY at.sensor_no, at.channel
            """,
            (station_code,),
        )
        rules = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    channels: list[RealChannel] = []
    for rule in rules:
        sensor_no = str(rule["sensor_no"]).strip()
        channel = str(rule["channel"]).strip()
        frequency_hz = AlertAnalyticsEngine._parse_frequency_hz(rule.get("frequency"))
        physical_min = float_or_none(rule.get("range_min"))
        physical_max = float_or_none(rule.get("range_max"))
        if physical_min is None or physical_max is None:
            raise StressError(f"missing physical range for {sensor_no}:{channel}")
        if physical_min >= physical_max:
            raise StressError(f"invalid physical range for {sensor_no}:{channel}")
        level, threshold = threshold_value(rule, physical_min, physical_max)
        margin = max(abs(physical_max) * 1e-5, 1e-5)
        extreme = physical_max + margin
        channels.append(
            RealChannel(
                sensor_no=sensor_no,
                channel=channel,
                frequency_hz=frequency_hz,
                instrument_id=str(rule["instrument_id"]),
                physical_min=physical_min,
                physical_max=physical_max,
                normal_value=safe_value(rule, physical_min, physical_max),
                threshold_value=threshold,
                threshold_level=level,
                extreme_value=extreme,
                threshold_samples=required_sample_count(frequency_hz, 3.0),
                oor_samples=required_sample_count(frequency_hz, 3.0),
                stuck_samples=required_sample_count(frequency_hz, 100.0),
                offline_timeout_ms=required_sample_count(
                    frequency_hz,
                    5.0,
                    minimum_samples=MIN_OFFLINE_PERIODS,
                )
                / frequency_hz
                * 1000.0,
            )
        )
    if not channels:
        raise StressError("station has no real threshold channels")
    return channels


def expected_alerts(channels: Iterable[RealChannel]) -> list[ExpectedAlert]:
    items: list[ExpectedAlert] = []
    instruments: set[str] = set()
    for channel in channels:
        items.extend(
            [
                ExpectedAlert(
                    channel.sensor_no,
                    channel.channel,
                    ALERT_TYPE_THRESHOLD,
                    channel.threshold_level,
                ),
                ExpectedAlert(
                    channel.sensor_no,
                    channel.channel,
                    ALERT_TYPE_OOR,
                    1,
                ),
                ExpectedAlert(
                    channel.sensor_no,
                    channel.channel,
                    ALERT_TYPE_STUCK,
                    1,
                ),
                ExpectedAlert(
                    channel.sensor_no,
                    channel.channel,
                    ALERT_TYPE_OFFLINE,
                    1,
                ),
            ]
        )
        instruments.add(channel.instrument_id)
    items.extend(
        ExpectedAlert(instrument_id, "__DAQ__", ALERT_TYPE_DAQ_OFFLINE, 1)
        for instrument_id in sorted(instruments)
    )
    return items


def build_rows(
    channels: Sequence[RealChannel],
    phase: str,
    start_time: datetime,
) -> list[tuple[str, str, float, datetime]]:
    rows: list[tuple[str, str, float, datetime]] = []
    for channel in channels:
        if phase == "normal":
            count = 1
            value = channel.normal_value
            interval = 1.0
        elif phase == "threshold":
            count = channel.threshold_samples
            value = channel.threshold_value
            interval = min(channel.sample_interval_seconds, 1.0)
        elif phase == "oor":
            count = channel.oor_samples
            value = channel.extreme_value
            interval = min(channel.sample_interval_seconds, 1.0)
        elif phase == "stuck":
            count = channel.stuck_samples
            value = channel.extreme_value
            interval = channel.sample_interval_seconds
        else:
            raise StressError(f"unknown phase {phase}")
        for index in range(count):
            rows.append(
                (
                    channel.sensor_no,
                    channel.channel,
                    value,
                    start_time + timedelta(seconds=index * interval),
                )
            )
    rows.sort(key=lambda row: row[3])
    return rows


def write_phase(
    conn: Any,
    rows: Sequence[tuple[str, str, float, datetime]],
    batch_size: int = 2_000,
) -> tuple[datetime, datetime, int]:
    if not rows:
        raise StressError("cannot write an empty phase")
    first_time = rows[0][3]
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset : offset + batch_size]
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
    return first_time, rows[-1][3], len(rows)


def wait_for_scan(
    health_url: str,
    timeout: float,
    after: datetime,
    max_wait: float = 120.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max_wait
    last_scan: datetime | None = None
    while time.monotonic() < deadline:
        health = fetch_json(health_url, timeout)
        detection = health.get("detection") or {}
        raw_scan = detection.get("lastAlertScanAt")
        if raw_scan:
            last_scan = datetime.fromisoformat(str(raw_scan).replace("Z", "+00:00"))
            if last_scan > after:
                return health
        time.sleep(0.5)
    raise StressError(f"worker did not scan past {after}; last scan was {last_scan}")


def next_phase_start(
    conn: Any,
    health_url: str,
    timeout: float,
) -> datetime:
    """Return a future timestamp strictly beyond the worker's current cursor."""
    health = fetch_json(health_url, timeout)
    detection = health.get("detection") or {}
    raw_scan = detection.get("lastAlertScanAt")
    last_scan = (
        datetime.fromisoformat(str(raw_scan).replace("Z", "+00:00"))
        if raw_scan
        else database_now(conn)
    )
    return max(database_now(conn), last_scan) + timedelta(seconds=10.0)


def fetch_matching_alerts(
    conn: Any,
    channels: Sequence[RealChannel],
    start_time: datetime,
    end_time: datetime,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    sensors = sorted({channel.sensor_no for channel in channels})
    instruments = sorted({channel.instrument_id for channel in channels})
    identifiers = sorted(set(sensors + instruments))
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT sensor_no, channel, alert_type, alert_level, create_time, end_time
            FROM public.t_alert_record
            WHERE create_time >= %s AND create_time <= %s
              AND LOWER(sensor_no) = ANY(%s)
            ORDER BY seq
            """,
            (start_time, end_time, [item.lower() for item in identifiers]),
        )
        rows = [dict(row) for row in cur.fetchall()]
    conn.rollback()
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row["sensor_no"]).lower(),
            str(row["channel"] or ""),
            str(row["alert_type"]),
        )
        result[key] = row
    return result


def verify_alerts(
    expected: Sequence[ExpectedAlert],
    observed: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    failures: list[str] = []
    matched = 0
    for item in expected:
        row = observed.get(item.key)
        if row is None:
            failures.append(f"missing {item.key}")
            continue
        matched += 1
        if int(row["alert_level"]) != item.level:
            failures.append(
                f"{item.key} level expected {item.level}, got {row['alert_level']}"
            )
    expected_keys = {item.key for item in expected}
    unexpected = sorted(set(observed) - expected_keys)
    if unexpected:
        failures.append(f"unexpected alerts: {unexpected}")
    return {
        "passed": not failures,
        "expectedCount": len(expected),
        "matchedCount": matched,
        "observedCount": len(observed),
        "failures": failures[:50],
    }


def verify_statistics(
    conn: Any,
    channels: Sequence[RealChannel],
    start_time: datetime,
    end_time: datetime,
    window_minutes: int,
    timeout: float = 45.0,
) -> dict[str, Any]:
    expected = {channel.key for channel in channels}
    deadline = time.monotonic() + timeout
    rows: list[tuple[Any, ...]] = []
    while time.monotonic() < deadline:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT LOWER(sensor_no), COALESCE(channel, ''), COUNT(*),
                       MIN(sample_count)
                FROM public.t_stat_record
                WHERE stat_time >= %s AND stat_time <= %s
                  AND period_type = %s
                  AND (LOWER(sensor_no), COALESCE(channel, '')) IN (
                      SELECT * FROM UNNEST(%s::text[], %s::text[])
                  )
                GROUP BY LOWER(sensor_no), COALESCE(channel, '')
                """,
                (
                    start_time.replace(second=0, microsecond=0),
                    end_time + timedelta(minutes=window_minutes),
                    f"{window_minutes}m",
                    [key[0] for key in expected],
                    [key[1] for key in expected],
                ),
            )
            rows = cur.fetchall()
        conn.rollback()
        found = {(str(row[0]), str(row[1])) for row in rows}
        if expected <= found:
            break
        time.sleep(1)
    found = {(str(row[0]), str(row[1])) for row in rows}
    missing = sorted(expected - found)
    zero_samples = [
        (str(row[0]), str(row[1]))
        for row in rows
        if int(row[3] or 0) <= 0
    ]
    failures = []
    if missing:
        failures.append(f"missing statistics for {len(missing)} channels")
    if zero_samples:
        failures.append(f"zero-sample statistics for {zero_samples[:10]}")
    return {
        "passed": not failures,
        "periodType": f"{window_minutes}m",
        "expectedChannelCount": len(expected),
        "observedChannelCount": len(expected & found),
        "failures": failures,
    }


def wait_for_offline_alerts(
    conn: Any,
    channels: Sequence[RealChannel],
    expected: Sequence[ExpectedAlert],
    start_time: datetime,
    timeout: float,
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], datetime]:
    deadline = time.monotonic() + timeout
    offline_expected = {
        item.key
        for item in expected
        if item.alert_type in {ALERT_TYPE_OFFLINE, ALERT_TYPE_DAQ_OFFLINE}
    }
    observed: dict[tuple[str, str, str], dict[str, Any]] = {}
    while time.monotonic() < deadline:
        now = database_now(conn)
        observed = fetch_matching_alerts(conn, channels, start_time, now)
        if offline_expected <= set(observed):
            return observed, now
        time.sleep(2)
    return observed, database_now(conn)


def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings(debug=False)
    conn = psycopg2.connect(
        settings.dsn,
        application_name="real_alert_stress",
    )
    try:
        identity = database_identity(conn)
        if args.mode == "all":
            if not args.allow_write:
                raise StressError("--mode all requires --allow-write")
            if args.confirm_database != identity["database"]:
                raise StressError(
                    f"--confirm-database must exactly match {identity['database']}"
                )
        health = fetch_json(args.health_url, args.health_timeout)
        health_config = verify_health(health)
        station_code = health_config["stationCode"]
        channels = load_channels(conn, station_code)
        expected = expected_alerts(channels)
        max_offline_seconds = max(
            channel.offline_timeout_ms for channel in channels
        ) / 1000.0
        summary: dict[str, Any] = {
            "database": identity,
            "stationCode": station_code,
            "health": health_config,
            "channelCount": len(channels),
            "instrumentCount": len({item.instrument_id for item in channels}),
            "expectedAlertCount": len(expected),
            "expectedByType": {
                alert_type: sum(
                    1 for item in expected if item.alert_type == alert_type
                )
                for alert_type in (
                    ALERT_TYPE_THRESHOLD,
                    ALERT_TYPE_OOR,
                    ALERT_TYPE_STUCK,
                    ALERT_TYPE_OFFLINE,
                    ALERT_TYPE_DAQ_OFFLINE,
                )
            },
            "maxOfflineTimeoutSeconds": max_offline_seconds,
            "channels": [f"{channel.sensor_no}:{channel.channel}" for channel in channels],
        }
        if args.mode == "preflight":
            summary["passed"] = True
            return summary

        observer = HealthObserver(args.health_url, args.health_timeout)
        import threading

        observer_thread = threading.Thread(target=observer.run, daemon=True)
        observer_thread.start()
        session_start = database_now(conn)
        phase_rows = 0
        try:
            for phase in ("normal", "threshold", "oor"):
                start = next_phase_start(
                    conn,
                    args.health_url,
                    args.health_timeout,
                )
                rows = build_rows(channels, phase, start)
                _, phase_end, written = write_phase(conn, rows)
                phase_rows += written
                wait_for_scan(args.health_url, args.health_timeout, phase_end)
                print(
                    f"[phase] {phase} rows={written} end={phase_end.isoformat(' ')}",
                    flush=True,
                )

            start = next_phase_start(
                conn,
                args.health_url,
                args.health_timeout,
            )
            rows = build_rows(channels, "stuck", start)
            _, stuck_end, written = write_phase(conn, rows)
            phase_rows += written
            wait_for_scan(args.health_url, args.health_timeout, stuck_end, max_wait=180)
            print(
                f"[phase] stuck rows={written} end={stuck_end.isoformat(' ')}",
                flush=True,
            )

            start = next_phase_start(
                conn,
                args.health_url,
                args.health_timeout,
            )
            rows = build_rows(channels, "normal", start)
            _, normal_end, written = write_phase(conn, rows)
            phase_rows += written
            wait_for_scan(args.health_url, args.health_timeout, normal_end)
            print(
                f"[phase] recovery rows={written} end={normal_end.isoformat(' ')}",
                flush=True,
            )

            _, test_end = wait_for_offline_alerts(
                conn,
                channels,
                expected,
                normal_end,
                max_offline_seconds + 45,
            )
            wait_for_scan(
                args.health_url,
                args.health_timeout,
                test_end,
                max_wait=30,
            )
            observed = fetch_matching_alerts(
                conn,
                channels,
                session_start,
                test_end,
            )
        finally:
            observer.stop_requested = True
            observer_thread.join(timeout=5)

        final_health = fetch_json(args.health_url, args.health_timeout)
        final_config = verify_health(final_health)
        alert_result = verify_alerts(expected, observed)
        stats_result = verify_statistics(
            conn,
            channels,
            session_start,
            test_end,
            final_config["windowMinutes"],
        )
        observer_result = observer.summary()
        rows_read_back = 0
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM public.t_monitor
                WHERE time >= %s AND time <= %s
                  AND (LOWER(sensor_no), COALESCE(channel, '')) IN (
                      SELECT * FROM UNNEST(%s::text[], %s::text[])
                  )
                """,
                (
                    session_start,
                    test_end,
                    [channel.key[0] for channel in channels],
                    [channel.key[1] for channel in channels],
                ),
            )
            rows_read_back = int(cur.fetchone()[0] or 0)
        passed = (
            alert_result["passed"]
            and stats_result["passed"]
            and rows_read_back == phase_rows
            and not observer_result["lastError"]
            and not observer_result["lastAlertError"]
            and not observer_result["lastStatisticsError"]
        )
        summary.update(
            {
                "sessionStart": session_start.isoformat(sep=" ", timespec="milliseconds"),
                "sessionEnd": test_end.isoformat(sep=" ", timespec="milliseconds"),
                "rowsWritten": phase_rows,
                "rowsReadBack": rows_read_back,
                "alerts": alert_result,
                "statistics": stats_result,
                "worker": observer_result,
                "passed": passed,
            }
        )
        return summary
    finally:
        conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except (StressError, psycopg2.Error, OSError, ValueError) as exc:
        print(f"[error] {exc}", file=sys.stderr, flush=True)
        return 1
    payload = json.dumps(summary, indent=2, ensure_ascii=False, default=str)
    print(payload, flush=True)
    if args.report_json:
        with open(args.report_json, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
    return 0 if summary.get("passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
