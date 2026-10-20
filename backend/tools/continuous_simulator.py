#!/usr/bin/env python
"""Continuously generate bridge monitoring data and run alert detection.

Default sample rates:
  * acceleration channels: 100 Hz
  * all other channels: 50 Hz

Alert scenarios included:
  * threshold exceedance: stn-12-b/stress
  * stuck sensor: wy-sj-01/x
  * out-of-physical-range: wprs-01
  * sensor offline: zd-wg-01/x
  * acquisition instrument offline: accl-03 and accl-04 channels
"""

from __future__ import annotations

import argparse
import math
import queue
import signal
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import DSN
from app.domain.engine import AlertAnalyticsEngine

ACCELERATION_SENSOR_PREFIXES = ("accl-", "zd-wg-", "zd-cg-")
OFFLINE_CHANNELS = {("zd-wg-01", "x")}
DAQ_OFFLINE_SENSORS = {"accl-03", "accl-04"}
THRESHOLD_CHANNELS = {("stn-12-b", "stress")}
STUCK_CHANNELS = {("wy-sj-01", "x")}
OOR_CHANNELS = {("wprs-01", "")}


@dataclass(frozen=True)
class ChannelPlan:
    sensor_no: str
    channel: str
    sample_rate: int
    is_acceleration: bool
    scenario: str
    base_value: float
    amplitude: float
    phase: float
    dc_offset: float


def load_channel_plans() -> list[ChannelPlan]:
    query = """
        SELECT t.sensor_no, COALESCE(t.channel, '') AS channel,
               t.physical_quantity, t.level3_lower, t.level3_upper,
               t.range_min, t.range_max, s.name AS sensor_name
        FROM public.t_alert_threshold t
        LEFT JOIN public.t_sensor s
          ON LOWER(s.sensor_no) = LOWER(t.sensor_no)
        ORDER BY t.sensor_no, t.channel
    """
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    plans: list[ChannelPlan] = []
    for sensor_no, channel, quantity, level3_lower, level3_upper, range_min, range_max, sensor_name in rows:
        sensor_key = str(sensor_no).strip().lower()
        channel_key = str(channel or "").strip()
        key = (sensor_key, channel_key)
        sensor_text = f"{sensor_no} {sensor_name or ''}".lower()
        is_acceleration = (
            "加速度" in str(quantity or "")
            or sensor_key.startswith(ACCELERATION_SENSOR_PREFIXES)
            or "加速度" in sensor_text
        )
        sample_rate = 100 if is_acceleration else 50

        lower = float(level3_lower) if level3_lower is not None else None
        upper = float(level3_upper) if level3_upper is not None else None
        minimum = float(range_min) if range_min is not None else float("-inf")
        maximum = float(range_max) if range_max is not None else float("inf")
        safe_lower = max(lower, minimum) if lower is not None else minimum
        safe_upper = min(upper, maximum) if upper is not None else maximum
        if math.isfinite(safe_lower) and math.isfinite(safe_upper) and safe_upper > safe_lower:
            base_value = (safe_lower + safe_upper) / 2.0
            amplitude = max(0.01, (safe_upper - safe_lower) * 0.08)
        elif math.isfinite(minimum) and math.isfinite(maximum) and maximum > minimum:
            base_value = (minimum + maximum) / 2.0
            amplitude = max(0.01, (maximum - minimum) * 0.01)
        else:
            base_value = 0.0
            amplitude = 0.05

        scenario = "normal"
        dc_offset = 0.0
        if is_acceleration:
            dc_offset = 9.80665
            base_value = 0.0
            amplitude = 0.25
        if key in THRESHOLD_CHANNELS:
            scenario = "threshold"
            base_value = float(level3_upper or 10.0) * 1.35
            amplitude = 0.2
        elif key in STUCK_CHANNELS:
            scenario = "stuck"
            base_value = min(max(base_value, -1.0), 1.0)
            amplitude = 0.0
        elif key in OOR_CHANNELS:
            scenario = "oor"
            minimum = float(range_min) if range_min is not None else -1.0
            maximum = float(range_max) if range_max is not None else 1.0
            base_value = maximum + max(1.0, abs(maximum - minimum) * 0.25)
            amplitude = 0.2

        phase = (zlib.crc32(f"{sensor_key}:{channel_key}".encode("utf-8")) % 360) * math.pi / 180.0
        plans.append(
            ChannelPlan(
                sensor_no=str(sensor_no),
                channel=channel_key,
                sample_rate=sample_rate,
                is_acceleration=is_acceleration,
                scenario=scenario,
                base_value=base_value,
                amplitude=amplitude,
                phase=phase,
                dc_offset=dc_offset,
            )
        )
    return plans


def should_skip(plan: ChannelPlan) -> bool:
    key = (plan.sensor_no.lower(), plan.channel)
    if key in OFFLINE_CHANNELS:
        return True
    return plan.sensor_no.lower() in DAQ_OFFLINE_SENSORS


def channel_value(plan: ChannelPlan, second_index: int, sample_index: int) -> float:
    if plan.scenario == "stuck":
        return plan.base_value
    t = second_index + sample_index / plan.sample_rate
    wave = (
        math.sin(plan.phase + t * 2.0 * math.pi * 0.8)
        + 0.35 * math.sin(plan.phase * 0.7 + t * 2.0 * math.pi * 3.2)
    )
    return plan.dc_offset + plan.base_value + plan.amplitude * wave


def insert_rows(rows: list[tuple[str, str, float, datetime]]) -> None:
    if not rows:
        return
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO public.t_monitor (sensor_no, channel, value, time)
                VALUES %s
                """,
                rows,
                page_size=5000,
            )


def detection_worker(
    windows: queue.Queue[tuple[datetime, datetime]],
    stop_event: threading.Event,
) -> None:
    engine = AlertAnalyticsEngine()
    engine.initialize()
    total_alerts = 0
    while not stop_event.is_set() or not windows.empty():
        try:
            window_start, window_end = windows.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            result = engine.step_detection(window_start, window_end)
            count = int(result.get("triggeredAlertsCount") or 0)
            total_alerts += count
            print(
                f"[detect] {window_start:%H:%M:%S} -> {window_end:%H:%M:%S} "
                f"stats={result.get('upsertedStatsCount', 0)} "
                f"alerts={count} total_alerts={total_alerts}",
                flush=True,
            )
        except Exception as exc:
            print(f"[detect] failed for {window_start} -> {window_end}: {exc}", flush=True)
        finally:
            windows.task_done()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-seconds", type=int, default=60, help="detection window size")
    parser.add_argument("--max-seconds", type=int, default=0, help="0 means run forever")
    parser.add_argument("--no-detect", action="store_true", help="only insert raw monitor data")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.window_seconds < 1:
        raise SystemExit("--window-seconds must be >= 1")

    plans = load_channel_plans()
    if not plans:
        raise SystemExit("No threshold channels found")

    active_plans = [plan for plan in plans if not should_skip(plan)]
    skipped_plans = [plan for plan in plans if should_skip(plan)]
    rows_per_second = sum(plan.sample_rate for plan in active_plans)
    accel_channels = sum(1 for plan in plans if plan.is_acceleration)
    other_channels = len(plans) - accel_channels

    print(
        f"[simulator] channels={len(plans)} acceleration={accel_channels} "
        f"other={other_channels} skipped={len(skipped_plans)} "
        f"rows_per_second={rows_per_second}",
        flush=True,
    )
    print(
        "[simulator] active alert scenarios: threshold, stuck, OOR, sensor offline, DAQ offline",
        flush=True,
    )
    if skipped_plans:
        print(
            "[simulator] skipped channels: "
            + ", ".join(f"{p.sensor_no}/{p.channel or '(default)'}" for p in skipped_plans),
            flush=True,
        )

    stop_event = threading.Event()
    windows: queue.Queue[tuple[datetime, datetime]] = queue.Queue(maxsize=2)
    detector: threading.Thread | None = None
    if not args.no_detect:
        detector = threading.Thread(
            target=detection_worker,
            args=(windows, stop_event),
            daemon=True,
            name="alert-detector",
        )
        detector.start()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    simulation_start = datetime.now().replace(microsecond=0)
    next_tick = time.monotonic()
    second_index = 0
    total_rows = 0
    window_start = simulation_start
    next_detection = simulation_start + timedelta(seconds=args.window_seconds)
    last_report = time.monotonic()
    skipped_detection_windows = 0

    print(f"[simulator] started at {simulation_start:%Y-%m-%d %H:%M:%S}", flush=True)
    try:
        while not stop_event.is_set():
            if args.max_seconds and second_index >= args.max_seconds:
                break

            tick_start = time.monotonic()
            second_start = simulation_start + timedelta(seconds=second_index)
            rows: list[tuple[str, str, float, datetime]] = []
            for plan in active_plans:
                interval = 1.0 / plan.sample_rate
                for sample_index in range(plan.sample_rate):
                    rows.append(
                        (
                            plan.sensor_no,
                            plan.channel,
                            channel_value(plan, second_index, sample_index),
                            second_start + timedelta(seconds=sample_index * interval),
                        )
                    )

            insert_rows(rows)
            total_rows += len(rows)
            second_index += 1

            if not args.no_detect and second_start + timedelta(seconds=1) >= next_detection:
                try:
                    windows.put_nowait((window_start, next_detection))
                except queue.Full:
                    skipped_detection_windows += 1
                    if skipped_detection_windows == 1 or skipped_detection_windows % 10 == 0:
                        print(
                            f"[simulator] detector backlog full; "
                            f"skipped_detection_windows={skipped_detection_windows}",
                            flush=True,
                        )
                window_start = next_detection
                next_detection += timedelta(seconds=args.window_seconds)

            now_monotonic = time.monotonic()
            if now_monotonic - last_report >= 10:
                elapsed = max(1.0, time.monotonic() - tick_start)
                print(
                    f"[simulator] rows={total_rows} "
                    f"rate={len(rows) / elapsed:.0f}/s "
                    f"time={second_start:%Y-%m-%d %H:%M:%S}",
                    flush=True,
                )
                last_report = now_monotonic

            next_tick += 1.0
            sleep_seconds = next_tick - time.monotonic()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        while not args.no_detect and not windows.empty():
            time.sleep(0.2)
        if detector:
            detector.join(timeout=30)
        print(
            f"[simulator] stopped total_rows={total_rows} "
            f"skipped_detection_windows={skipped_detection_windows}",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
