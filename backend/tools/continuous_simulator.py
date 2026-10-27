#!/usr/bin/env python
"""持续向 t_monitor 写入模拟桥梁监测数据。

采样频率从 ``t_sensor.frequency`` 读取。

包含的预警场景均由 ``t_alert_threshold`` 中当前存在的真实通道动态生成：
  * 阈值超限
  * 传感器死值
  * 超物理量程
  * 传感器离线
  * 采集仪离线
"""

from __future__ import annotations

import argparse
import math
import signal
import sys
import time
import zlib
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings
from app.domain.alert_rules.base import parse_frequency_hz

DSN = get_settings().dsn

# 离线场景不写采样数据，由后端根据 t_monitor 中的缺测情况独立判定。
OFFLINE_SCENARIOS = {"sensor_offline", "daq_offline"}

# 固定启动日志中预警场景的展示顺序，未知场景排在最后。
SCENARIO_ORDER = {
    "threshold": 0,
    "stuck": 1,
    "oor": 2,
    "sensor_offline": 3,
    "daq_offline": 4,
}


@dataclass(frozen=True)
class ChannelPlan:
    """单个通道的采样和波形配置。"""

    sensor_no: str
    channel: str
    instrument_id: str
    sample_rate: float
    scenario: str
    base_value: float
    # 波形振幅缩放系数，单位与监测值一致；置为 0 时输出不变的死值。
    amplitude: float
    # 主波形初始相位，单位为弧度；按传感器和通道稳定生成，用于错开各通道波形。
    phase: float
    level3_lower: float | None
    level3_upper: float | None
    range_min: float | None
    range_max: float | None


def plan_key(plan: ChannelPlan) -> tuple[str, str]:
    """返回用于匹配阈值规则的通道键。"""
    return plan.sensor_no.strip().lower(), plan.channel.strip()


def threshold_target(plan: ChannelPlan) -> tuple[float, float] | None:
    """生成位于三级阈值外、物理量程内的波形中心和振幅。"""
    # 判断该通道是否有可用的三级阈值，并计算一个能让波形中心越过阈值、同时仍处于物理量程内的基准值和振幅。
    candidates: list[tuple[float, float]] = []

    if plan.level3_upper is not None:
        upper = plan.level3_upper
        boundary = plan.range_max
        if boundary is not None and math.isfinite(boundary):
            span = boundary - upper
        else:
            span = max(abs(upper) * 0.1, 1.0)
        if span > 0:
            candidates.append((upper + span * 0.5, span * 0.15))

    if plan.level3_lower is not None:
        lower = plan.level3_lower
        boundary = plan.range_min
        if boundary is not None and math.isfinite(boundary):
            span = lower - boundary
        else:
            span = max(abs(lower) * 0.1, 1.0)
        if span > 0:
            candidates.append((lower - span * 0.5, span * 0.15))

    if not candidates:
        return None
    return candidates[0]


def out_of_range_target(plan: ChannelPlan) -> tuple[float, float] | None:
    """生成物理量程外的波形中心和振幅。"""
    minimum = plan.range_min
    maximum = plan.range_max
    if maximum is not None and math.isfinite(maximum):
        span = abs(maximum - minimum) if minimum is not None else abs(maximum)
        margin = max(span * 0.02, abs(maximum) * 1e-6, 0.01)
        return maximum + margin * 2.0, margin
    if minimum is not None and math.isfinite(minimum):
        span = abs(maximum - minimum) if maximum is not None else abs(minimum)
        margin = max(span * 0.02, abs(minimum) * 1e-6, 0.01)
        return minimum - margin * 2.0, margin
    return None


def assign_alert_scenarios(plans: list[ChannelPlan]) -> list[ChannelPlan]:
    """从真实阈值通道中选择互不重叠的五类预警通道。"""
    assigned = list(plans)
    used: set[tuple[str, str]] = set()
    by_instrument: dict[str, list[int]] = defaultdict(list)
    for index, plan in enumerate(assigned):
        if plan.instrument_id:
            by_instrument[plan.instrument_id].append(index)

    # 模拟预警（采集仪离线）：优先选择通道数最少的多通道采集仪，
    # 将其全部通道标记为跳过写入，后端会按缺少采样判定整台采集仪离线。
    daq_candidates = [
        (instrument_id, indexes)
        for instrument_id, indexes in by_instrument.items()
        if len(indexes) >= 2
    ]
    daq_instrument = ""
    if daq_candidates:
        daq_instrument, daq_indexes = min(
            daq_candidates,
            key=lambda item: (len(item[1]), item[0]),
        )
        for index in daq_indexes:
            assigned[index] = replace(assigned[index], scenario="daq_offline")
            used.add(plan_key(assigned[index]))

    # 模拟预警（传感器离线）：只停写一个通道，并确保同一采集仪
    # 还有其他通道持续上报，以便后端区分单通道离线与采集仪离线。
    offline_candidates = [
        (instrument_id, indexes)
        for instrument_id, indexes in by_instrument.items()
        if instrument_id != daq_instrument and len(indexes) >= 2
    ]
    if offline_candidates:
        _, offline_indexes = min(
            offline_candidates,
            key=lambda item: (-len(item[1]), item[0]),
        )
        index = min(
            offline_indexes,
            key=lambda item: plan_key(assigned[item]),
        )
        assigned[index] = replace(assigned[index], scenario="sensor_offline")
        used.add(plan_key(assigned[index]))

    def first_available(
        predicate: Any,
    ) -> int | None:
        # 遍历当前通道计划，排除已分配给离线等场景的通道
        candidates = [
            index
            for index, plan in enumerate(assigned)
            if plan_key(plan) not in used and predicate(plan)
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: plan_key(assigned[item]))

    # 模拟预警（阈值超限）：选一个存在三级阈值的真实通道，
    # 让波形中心越过阈值，但振幅仍控制在物理量程内。
    threshold_index = first_available(lambda plan: threshold_target(plan) is not None)
    if threshold_index is not None:
        # 把该通道的 scenario 改成 "threshold"，并替换 base_value 和 amplitude，让模拟器持续写入触发阈值超限的采样数据
        plan = assigned[threshold_index]
        target = threshold_target(plan)
        if target is not None:
            base_value, amplitude = target
            assigned[threshold_index] = replace(
                plan,
                scenario="threshold",
                base_value=base_value,
                amplitude=amplitude,
            )
            used.add(plan_key(plan))

    # 模拟预警（超物理量程）：选另一个真实通道，
    # 让波形中心落在 range_min/range_max 之外。
    oor_index = first_available(lambda plan: out_of_range_target(plan) is not None)
    if oor_index is not None:
        plan = assigned[oor_index]
        target = out_of_range_target(plan)
        if target is not None:
            base_value, amplitude = target
            assigned[oor_index] = replace(
                plan,
                scenario="oor",
                base_value=base_value,
                amplitude=amplitude,
            )
            used.add(plan_key(plan))

    # 模拟预警（传感器死值）：再选一个真实通道并把振幅置零，
    # channel_value() 会持续输出相同的 base_value。
    stuck_index = first_available(lambda _plan: True)
    if stuck_index is not None:
        plan = assigned[stuck_index]
        assigned[stuck_index] = replace(plan, scenario="stuck", amplitude=0.0)

    return assigned


def load_channel_plans() -> list[ChannelPlan]:
    """加载阈值和传感器元数据，并生成模拟计划。"""
    query = """
        SELECT t.sensor_no, COALESCE(t.channel, '') AS channel,
               t.level3_lower, t.level3_upper, t.range_min, t.range_max,
               s.frequency, s.instrument_id
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
    for (
        sensor_no,
        channel,
        level3_lower,
        level3_upper,
        range_min,
        range_max,
        frequency,
        instrument_id,
    ) in rows:
        sensor_key = str(sensor_no).strip().lower()
        channel_key = str(channel or "").strip()
        sample_rate = parse_frequency_hz(frequency)

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

        phase = (zlib.crc32(f"{sensor_key}:{channel_key}".encode("utf-8")) % 360) * math.pi / 180.0
        plans.append(
            ChannelPlan(
                sensor_no=str(sensor_no),
                channel=channel_key,
                instrument_id=str(instrument_id or "").strip(),
                sample_rate=sample_rate,
                scenario="normal",
                base_value=base_value,
                amplitude=amplitude,
                phase=phase,
                level3_lower=lower,
                level3_upper=upper,
                range_min=float(range_min) if range_min is not None else None,
                range_max=float(range_max) if range_max is not None else None,
            )
        )
    return assign_alert_scenarios(plans)


def should_skip(plan: ChannelPlan) -> bool:
    """跳过用于模拟传感器或采集仪离线的通道。"""
    return plan.scenario in OFFLINE_SCENARIOS


def channel_value(plan: ChannelPlan, elapsed_seconds: float) -> float:
    """生成一个正常、死值或超量程样本。"""
    # 模拟数据：死值场景每个采样点都返回相同值；其余场景叠加两条正弦波，
    # 正常、阈值超限和超量程的差异由分配场景时设置的 base_value/amplitude 决定。
    if plan.scenario == "stuck":
        return plan.base_value
    t = elapsed_seconds
    wave = math.sin(plan.phase + t * 2.0 * math.pi * 0.8) + 0.35 * math.sin(
        plan.phase * 0.7 + t * 2.0 * math.pi * 3.2
    )
    return plan.base_value + plan.amplitude * wave


def insert_rows(rows: list[tuple[str, str, float, datetime]]) -> None:
    """将生成的样本批量写入 t_monitor。"""
    if not rows:
        return
    with psycopg2.connect(DSN) as conn:
        with conn.cursor() as cur:
            # 模拟数据：仅写入 t_monitor；预警记录和统计记录由后端独立检测生成。
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

    # 离线场景不写入采样点，由后端根据 t_monitor 缺少新数据来判定离线。
    active_plans = [plan for plan in plans if not should_skip(plan)]
    skipped_plans = [plan for plan in plans if should_skip(plan)]
    rows_per_second = sum(plan.sample_rate for plan in active_plans)

    print(
        f"[simulator] channels={len(plans)} skipped={len(skipped_plans)} "
        f"rows_per_second={rows_per_second:.3f}",
        flush=True,
    )
    print(
        "[simulator] active alert scenarios: threshold, stuck, OOR, sensor offline, DAQ offline",
        flush=True,
    )
    scenario_plans = sorted(
        (plan for plan in plans if plan.scenario != "normal"),
        key=lambda plan: (SCENARIO_ORDER.get(plan.scenario, 99), plan_key(plan)),
    )
    if scenario_plans:
        print(
            "[simulator] scenario channels: "
            + ", ".join(
                f"{plan.scenario}={plan.sensor_no}/{plan.channel or '(default)'}"
                for plan in scenario_plans
            ),
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

    # 所有模拟样本的时间基准：数据库当前时间（截断到秒），也可通过参数提前到未来。
    simulation_start = database_now().replace(microsecond=0) + timedelta(
        seconds=args.timestamp_lead_seconds
    )
    # 下一批数据的单调时钟目标点；按 1 秒固定推进，避免受系统时间校准影响。
    next_tick = time.monotonic()
    # 当前逻辑秒序号（从 0 开始），同时用于计算时间窗口、限制运行时长和生成采样序号。
    # second_index 决定采样时间窗口，next_tick 只控制实际写入节奏
    second_index = 0
    # 已成功写入 t_monitor 的累计采样点数。
    total_rows = 0
    # 上次输出进度日志的单调时间，用于把日志频率限制为约每 10 秒一次。
    last_report = time.monotonic()

    print(f"[simulator] started at {simulation_start:%Y-%m-%d %H:%M:%S}", flush=True)
    try:
        while not stop_requested[0]:
            if args.max_seconds and second_index >= args.max_seconds:
                break

            # 本批处理开始时的单调时间，仅用于统计本批写入吞吐量。
            tick_start = time.monotonic()
            # 本批对应的逻辑秒起点，也是进度日志中显示的数据时间。
            second_start = simulation_start + timedelta(seconds=second_index)
            # 本批待写入的数据，元组依次为传感器编号、通道、值和时间戳。
            rows: list[tuple[str, str, float, datetime]] = []
            # 模拟数据：按 t_sensor.frequency 生成当前一秒内的全部采样点。
            for plan in active_plans:
                # 把全局采样序号切分到 [second_index, second_index + 1) 秒；
                # 使用 ceil 可兼容小数采样率，并避免不同批次重复或遗漏样本。
                first_index = math.ceil(second_index * plan.sample_rate)
                last_index = math.ceil((second_index + 1) * plan.sample_rate)
                for sample_index in range(first_index, last_index):
                    # 全局采样序号换算为相对 simulation_start 的精确秒偏移。
                    elapsed_seconds = sample_index / plan.sample_rate
                    # 模拟数据：当前场景决定样本值，时间戳按采样序号精确推进。
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
            # 仅在整批写入成功后累计，保证该值表示已落库的样本数。
            total_rows += len(rows)
            # 推进逻辑秒；下一批将生成紧随其后的时间窗口。
            second_index += 1

            now_monotonic = time.monotonic()
            if now_monotonic - last_report >= 10:
                # 本批实际耗时不小于 1 秒，避免短批次把瞬时写入速率放大。
                elapsed = max(1.0, time.monotonic() - tick_start)
                print(
                    f"[simulator] rows={total_rows} "
                    f"rate={len(rows) / elapsed:.0f}/s "
                    f"time={second_start:%Y-%m-%d %H:%M:%S}",
                    flush=True,
                )
                last_report = now_monotonic

            # 对齐下一批数据，保持稳定的一秒节奏。
            # next_tick 是绝对目标时间，而不是相对 sleep 叠加；处理延迟时可自动追赶。
            next_tick += 1.0
            # 若本批尚未用完 1 秒，则补足剩余时间；已超时则不等待并立即处理下一批。
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
