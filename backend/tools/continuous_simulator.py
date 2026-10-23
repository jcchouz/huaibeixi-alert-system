#!/usr/bin/env python
"""持续向 t_monitor 写入模拟桥梁监测数据。

采样频率从 ``t_sensor.frequency`` 读取。

包含的预警场景：
  * 阈值超限：sensor-01/channel-a
  * 传感器死值：wy-sj-01/x
  * 超物理量程：wprs-01
  * 传感器离线：zd-wg-01/x
  * 采集仪离线：加速度采集仪下全部通道（accl-01 ~ accl-04）
"""

from __future__ import annotations

import argparse
import math
import signal
import sys
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

ACCELERATION_SENSOR_PREFIXES = ("accl-", "zd-wg-", "zd-cg-")
OFFLINE_CHANNELS = {("zd-wg-01", "x")}
# 采集仪离线需该采集仪下全部通道同时离线，因此整台加速度采集仪都停止上报。
DAQ_OFFLINE_SENSORS = {"accl-01", "accl-02", "accl-03", "accl-04"}
THRESHOLD_CHANNELS = {("sensor-01", "channel-a")}
STUCK_CHANNELS = {("wy-sj-01", "x")}
OOR_CHANNELS = {("wprs-01", "")}


@dataclass(frozen=True)
class ChannelPlan:
    """单个通道的采样和波形配置。"""

    sensor_no: str
    channel: str
    sample_rate: float
    is_acceleration: bool
    scenario: str
    base_value: float
    amplitude: float
    phase: float


def load_channel_plans() -> list[ChannelPlan]:
    """加载阈值和传感器元数据，并生成模拟计划。"""
    query = """
        SELECT t.sensor_no, COALESCE(t.channel, '') AS channel,
               t.physical_quantity, t.level3_lower, t.level3_upper,
               t.range_min, t.range_max, s.name AS sensor_name, s.frequency
        FROM public.t_alert_threshold t
        LEFT JOIN public.t_sensor s
          ON LOWER(s.sensor_no) = LOWER(t.sensor_no)
        ORDER BY t.sensor_no, t.channel
    """
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

    # 将数据库元数据转换为波形参数和预警场景配置。
    plans: list[ChannelPlan] = []
    for sensor_no, channel, quantity, level3_lower, level3_upper, range_min, range_max, sensor_name, frequency in rows:
        sensor_key = str(sensor_no).strip().lower()
        channel_key = str(channel or "").strip()
        key = (sensor_key, channel_key)
        sensor_text = f"{sensor_no} {sensor_name or ''}".lower()
        is_acceleration = (
            "加速度" in str(quantity or "")
            or sensor_key.startswith(ACCELERATION_SENSOR_PREFIXES)
            or "加速度" in sensor_text
        )
        try:
            if "/" in str(frequency or ""):
                numerator, denominator = str(frequency).split("/", 1)
                sample_rate = float(numerator) / float(denominator)
            else:
                sample_rate = float(frequency)
        except (TypeError, ValueError, ZeroDivisionError):
            sample_rate = 100.0 if is_acceleration else 1.0
        if sample_rate <= 0:
            sample_rate = 1.0

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

        # 为指定测试通道选择固定的预警场景。
        scenario = "normal"
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
            )
        )
    return plans


def should_skip(plan: ChannelPlan) -> bool:
    """跳过用于模拟传感器或采集仪离线的通道。"""
    key = (plan.sensor_no.lower(), plan.channel)
    if key in OFFLINE_CHANNELS:
        return True
    return plan.sensor_no.lower() in DAQ_OFFLINE_SENSORS


def channel_value(plan: ChannelPlan, elapsed_seconds: float) -> float:
    """生成一个正常、死值或超量程样本。"""
    if plan.scenario == "stuck":
        return plan.base_value
    t = elapsed_seconds
    wave = (
        math.sin(plan.phase + t * 2.0 * math.pi * 0.8)
        + 0.35 * math.sin(plan.phase * 0.7 + t * 2.0 * math.pi * 3.2)
    )
    return plan.base_value + plan.amplitude * wave


def insert_rows(rows: list[tuple[str, str, float, datetime]]) -> None:
    """将生成的样本批量写入 t_monitor。"""
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


def database_now() -> datetime:
    """使用数据库时间作为模拟起始时间。"""
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT LOCALTIMESTAMP")
            return cur.fetchone()[0]


def parse_args() -> argparse.Namespace:
    """解析模拟器运行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-seconds", type=int, default=0, help="0 means run forever")
    parser.add_argument(
        "--timestamp-lead-seconds",
        type=float,
        default=0.0,
        help=(
            "generate sample timestamps this many seconds ahead of wall time; "
            "use a positive value when the detector runs with overlap=0"
        ),
    )
    args = parser.parse_args()
    if args.timestamp_lead_seconds < 0:
        parser.error("--timestamp-lead-seconds must be zero or greater")
    return args


def main() -> int:
    """持续生成按秒分批的监测样本。"""
    args = parse_args()

    # 生成正常写入和有意跳过的通道集合。
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
        f"rows_per_second={rows_per_second:.3f}",
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

    stop_requested = [False]

    # 捕获退出信号，保证最终统计信息正常输出。
    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested[0] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    simulation_start = database_now().replace(microsecond=0) + timedelta(
        seconds=args.timestamp_lead_seconds
    )
    next_tick = time.monotonic()
    second_index = 0
    total_rows = 0
    last_report = time.monotonic()

    print(f"[simulator] started at {simulation_start:%Y-%m-%d %H:%M:%S}", flush=True)
    try:
        while not stop_requested[0]:
            if args.max_seconds and second_index >= args.max_seconds:
                break

            tick_start = time.monotonic()
            second_start = simulation_start + timedelta(seconds=second_index)
            rows: list[tuple[str, str, float, datetime]] = []
            # 生成当前一秒内应写入的全部样本。
            for plan in active_plans:
                first_index = math.ceil(second_index * plan.sample_rate)
                last_index = math.ceil((second_index + 1) * plan.sample_rate)
                for sample_index in range(first_index, last_index):
                    elapsed_seconds = sample_index / plan.sample_rate
                    rows.append(
                        (
                            plan.sensor_no,
                            plan.channel,
                            channel_value(plan, elapsed_seconds),
                            simulation_start + timedelta(seconds=elapsed_seconds),
                        )
                    )

            # 模拟器只写数据库，检测由后端读取 t_monitor 独立完成。
            insert_rows(rows)
            total_rows += len(rows)
            second_index += 1

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

            # 对齐下一批数据，保持稳定的一秒节奏。
            next_tick += 1.0
            sleep_seconds = next_tick - time.monotonic()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        stop_requested[0] = True
    finally:
        print(f"[simulator] stopped total_rows={total_rows}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
