#!/usr/bin/env python
"""单通道阈值超限样本注入器。

只向 ``t_monitor`` 写入一个传感器通道的样本，用于本地调试「阈值超限」预警
链路；不写预警表、不写统计表，也不制造离线、死值或超量程场景。

使用前提：

* ``backend/.env`` 中 ``DETECTION_ALERT_THRESHOLD_ENABLED=true``，
  其余 ``DETECTION_ALERT_*_ENABLED=false``；
* 后端已启动，且检测 Worker 正在运行；
* 不要同时运行 ``continuous_simulator.py``，避免同通道数据混在一起。

典型用法：

  .\\.venv\\Scripts\\python.exe tools\\inject_threshold.py --level 3 --seconds 20
  .\\.venv\\Scripts\\python.exe tools\\inject_threshold.py --level 1 --seconds 10 --ack-existing
  .\\.venv\\Scripts\\python.exe tools\\inject_threshold.py --dry-run

默认通道是 ``stn-01-e/stress``，它没有历史未处理阈值告警，适合反复测试。
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Sequence

import psycopg2
from psycopg2.extras import execute_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import DSN
from app.domain.alert_types import ALERT_TYPE_THRESHOLD

DEFAULT_SENSOR = "stn-01-e"
DEFAULT_CHANNEL = "stress"


@dataclass(frozen=True)
class ThresholdRule:
    """注入器需要的单通道阈值和采样信息。"""

    sensor_no: str
    channel: str
    upper: dict[int, Optional[float]]
    lower: dict[int, Optional[float]]
    physical_min: Optional[float]
    physical_max: Optional[float]
    frequency_hz: float


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sensor", default=DEFAULT_SENSOR, help="传感器编号")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL, help="通道名称")
    parser.add_argument(
        "--direction",
        choices=("upper", "lower"),
        default="upper",
        help="上限超限或下限超限，默认 upper",
    )
    parser.add_argument(
        "--level",
        type=int,
        choices=(1, 2, 3),
        default=3,
        help="希望命中的告警级别，默认 3",
    )
    parser.add_argument(
        "--value",
        type=float,
        default=None,
        help="显式指定注入值，指定后忽略 --level 的自动计算",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=20,
        help="注入总时长，单位秒；0 表示一直运行到 Ctrl+C",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="采样间隔秒；默认取 1/frequency，并最多 1 秒",
    )
    parser.add_argument(
        "--ack-existing",
        action="store_true",
        help="开始前把该通道未处理的阈值告警置为已处理，便于反复测试",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="不先写入一个正常样本，保留后端内存中的连续命中状态",
    )
    parser.add_argument(
        "--verify-delay",
        type=float,
        default=2.0,
        help="注入结束后等待多少秒再查询告警，默认 2 秒",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="注入结束后不查询 t_alert_record",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取配置并打印注入计划，不写数据库",
    )
    return parser.parse_args(argv)


def parse_frequency_hz(value: object) -> float:
    """解析 ``t_sensor.frequency``，例如 ``1``、``100`` 或 ``1/60``。"""
    text = str(value or "").strip().lower().removesuffix("hz").strip()
    if not text:
        return 1.0
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            parsed = float(numerator) / float(denominator)
        else:
            parsed = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0
    return parsed if parsed > 0 else 1.0


def to_float(value: object) -> Optional[float]:
    """把数据库 Decimal 转成 float，空值保持 None。"""
    return float(value) if value is not None else None


def load_rule(conn: psycopg2.extensions.connection, sensor_no: str, channel: str) -> ThresholdRule:
    """读取单通道阈值规则，失败时打印该传感器可用通道。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.sensor_no, COALESCE(t.channel, '') AS channel,
                   t.level1_upper, t.level2_upper, t.level3_upper,
                   t.level1_lower, t.level2_lower, t.level3_lower,
                   t.range_min, t.range_max, s.frequency
            FROM public.t_alert_threshold t
            LEFT JOIN public.t_sensor s
              ON LOWER(s.sensor_no) = LOWER(t.sensor_no)
            WHERE LOWER(t.sensor_no) = LOWER(%s)
              AND COALESCE(t.channel, '') = %s
            """,
            (sensor_no, channel),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                SELECT COALESCE(channel, '')
                FROM public.t_alert_threshold
                WHERE LOWER(sensor_no) = LOWER(%s)
                ORDER BY channel
                """,
                (sensor_no,),
            )
            channels = [item[0] or "(default)" for item in cur.fetchall()]
            available = ", ".join(channels) if channels else "无"
            raise SystemExit(
                f"未找到阈值规则：{sensor_no}/{channel or '(default)'}。"
                f"该传感器可用通道：{available}"
            )

    return ThresholdRule(
        sensor_no=str(row[0]),
        channel=str(row[1] or ""),
        upper={1: to_float(row[2]), 2: to_float(row[3]), 3: to_float(row[4])},
        lower={1: to_float(row[5]), 2: to_float(row[6]), 3: to_float(row[7])},
        physical_min=to_float(row[8]),
        physical_max=to_float(row[9]),
        frequency_hz=parse_frequency_hz(row[10]),
    )


def target_value(
    rule: ThresholdRule,
    direction: str,
    level: int,
    explicit: Optional[float],
) -> float:
    """计算一个能命中指定级别、又不会落入更严重级别的注入值。"""
    if explicit is not None:
        return explicit

    bounds = rule.upper if direction == "upper" else rule.lower
    boundary = bounds.get(level)
    if boundary is None:
        raise SystemExit(
            f"通道 {rule.sensor_no}/{rule.channel or '(default)'} "
            f"未配置 level{level}_{direction}，请用 --value 显式指定注入值。"
        )

    more_severe = bounds.get(level - 1) if level > 1 else None
    margin = max(abs(boundary) * 0.02, 0.1)
    if direction == "upper":
        if more_severe is not None and more_severe > boundary:
            value = (boundary + more_severe) / 2.0
        else:
            value = boundary + margin
        if rule.physical_max is not None and value > rule.physical_max:
            raise SystemExit(
                f"计算值 {value:g} 超出物理量程上限 {rule.physical_max:g}，"
                "请检查阈值配置或用 --value 指定。"
            )
    else:
        if more_severe is not None and more_severe < boundary:
            value = (boundary + more_severe) / 2.0
        else:
            value = boundary - margin
        if rule.physical_min is not None and value < rule.physical_min:
            raise SystemExit(
                f"计算值 {value:g} 超出物理量程下限 {rule.physical_min:g}，"
                "请检查阈值配置或用 --value 指定。"
            )
    return round(value, 5)


def normal_value(rule: ThresholdRule) -> float:
    """返回一个位于三级阈值以内、可用于重置连续命中状态的正常值。"""
    lows = [v for v in (rule.lower.get(3), rule.physical_min) if v is not None]
    highs = [v for v in (rule.upper.get(3), rule.physical_max) if v is not None]
    low = max(lows) if lows else None
    high = min(highs) if highs else None
    if low is not None and high is not None and high >= low:
        return round((low + high) / 2.0, 5)
    if low is not None:
        return round(low, 5)
    if high is not None:
        return round(high, 5)
    return 0.0


def choose_interval(rule: ThresholdRule, requested: Optional[float]) -> tuple[float, int, float]:
    """确定采样间隔，并保证每秒至少一个样本。"""
    if requested is not None:
        if requested <= 0:
            raise SystemExit("--interval 必须大于 0。")
        if requested > 1.0:
            raise SystemExit(
                "--interval 最大为 1 秒；更慢的注入会让连续命中被重置。"
                "如需测试断点，请使用 --no-reset 或手工插入样本。"
            )
        interval = requested
    else:
        interval = min(1.0, 1.0 / rule.frequency_hz)

    max_gap = 1.5 / rule.frequency_hz
    samples_per_second = max(1, round(1.0 / interval))
    effective = 1.0 / samples_per_second
    if abs(effective - interval) > 1e-9:
        print(
            f"[inject] 提示：为按整秒批量写入，采样间隔由 {interval:g}s "
            f"调整为 {effective:g}s。"
        )
    if effective > max_gap + 1e-9:
        print(
            f"[inject] 警告：采样间隔 {effective:g}s 超过检测允许的最大间隔 "
            f"{max_gap:g}s，连续命中会被重置。"
        )
    return effective, samples_per_second, max_gap


def count_active_alerts(
    conn: psycopg2.extensions.connection,
    sensor_no: str,
    channel: str,
) -> int:
    """统计该通道未处理的阈值告警数量。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM public.t_alert_record
            WHERE LOWER(sensor_no) = LOWER(%s)
              AND COALESCE(channel, '') = %s
              AND alert_type = %s
              AND end_time IS NULL
            """,
            (sensor_no, channel, ALERT_TYPE_THRESHOLD),
        )
        return int(cur.fetchone()[0] or 0)


def acknowledge_existing(conn: psycopg2.extensions.connection, sensor_no: str, channel: str) -> int:
    """把该通道未处理的阈值告警置为已处理，返回影响行数。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE public.t_alert_record
            SET alert_status = 1,
                handle_by = 'inject_threshold',
                handle_time = NOW(),
                end_time = NOW()
            WHERE LOWER(sensor_no) = LOWER(%s)
              AND COALESCE(channel, '') = %s
              AND alert_type = %s
              AND end_time IS NULL
            """,
            (sensor_no, channel, ALERT_TYPE_THRESHOLD),
        )
        return int(cur.rowcount or 0)


def database_now(conn: psycopg2.extensions.connection) -> datetime:
    """读取数据库当前时间，保证与检测 Worker 使用同一时钟。"""
    with conn.cursor() as cur:
        cur.execute("SELECT LOCALTIMESTAMP")
        return cur.fetchone()[0]


def insert_rows(
    conn: psycopg2.extensions.connection,
    rows: Iterable[tuple[str, str, float, datetime]],
) -> int:
    """批量写入 t_monitor，返回写入行数。"""
    batch = list(rows)
    if not batch:
        return 0
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO public.t_monitor (sensor_no, channel, value, time) VALUES %s",
            batch,
            page_size=len(batch),
        )
    return len(batch)


def verify_alerts(
    conn: psycopg2.extensions.connection,
    sensor_no: str,
    channel: str,
    since: datetime,
) -> None:
    """查询本次注入期间新建的阈值告警，并打印结果。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT seq, alert_level, alert_status, trigger_val,
                   create_time, end_time
            FROM public.t_alert_record
            WHERE LOWER(sensor_no) = LOWER(%s)
              AND COALESCE(channel, '') = %s
              AND alert_type = %s
              AND create_time >= %s
            ORDER BY seq DESC
            LIMIT 5
            """,
            (sensor_no, channel, ALERT_TYPE_THRESHOLD, since),
        )
        rows = cur.fetchall()

    if not rows:
        print(
            "[inject] 未发现本次注入产生的阈值告警。请检查：\n"
            "  1. 后端是否已重启并加载了新的 .env（/api/health 里 running=true）；\n"
            "  2. 是否有同一通道的未处理告警（重复测试请加 --ack-existing）；\n"
            "  3. 注入期间后端是否持续运行。"
        )
        return

    print("[inject] 最近阈值告警：")
    for row in rows:
        seq, level, status, trigger_val, create_time, end_time = row
        status_text = "未处理" if int(status) == 0 else "已处理"
        print(
            f"  seq={seq} level={level} status={status_text} "
            f"trigger_val={trigger_val} "
            f"create_time={create_time} end_time={end_time}"
        )


def resolve_plan(
    conn: psycopg2.extensions.connection,
    args: argparse.Namespace,
) -> tuple[ThresholdRule, float, float, float, int, float]:
    """读取规则并计算注入参数，供 dry-run 和正式注入共用。"""
    rule = load_rule(conn, args.sensor, args.channel)
    value = target_value(rule, args.direction, args.level, args.value)
    normal = normal_value(rule)
    interval, samples_per_second, max_gap = choose_interval(rule, args.interval)

    if args.direction == "upper" and rule.upper.get(args.level) is not None:
        boundary = rule.upper[args.level]
        if value <= float(boundary):
            if args.value is None:
                raise SystemExit(
                    f"注入值 {value:g} 未超过 level{args.level}_upper {boundary:g}。"
                )
            print(
                f"[inject] 提示：显式注入值 {value:g} 未超过 "
                f"level{args.level}_upper {boundary:g}，本次不会命中该级别。"
            )
    if args.direction == "lower" and rule.lower.get(args.level) is not None:
        boundary = rule.lower[args.level]
        if value >= float(boundary):
            if args.value is None:
                raise SystemExit(
                    f"注入值 {value:g} 未低于 level{args.level}_lower {boundary:g}。"
                )
            print(
                f"[inject] 提示：显式注入值 {value:g} 未低于 "
                f"level{args.level}_lower {boundary:g}，本次不会命中该级别。"
            )

    return rule, value, normal, interval, samples_per_second, max_gap


def run_injection(
    conn: psycopg2.extensions.connection,
    args: argparse.Namespace,
) -> int:
    """在已建立的数据库连接上执行注入流程。"""
    rule, value, normal, interval, samples_per_second, max_gap = resolve_plan(conn, args)

    active = count_active_alerts(conn, rule.sensor_no, rule.channel)
    print(
        f"[inject] 通道={rule.sensor_no}/{rule.channel or '(default)'} "
        f"频率={rule.frequency_hz:g}Hz 间隔={interval:g}s "
        f"每轮={samples_per_second} 点 最大允许间隔={max_gap:g}s"
    )
    print(
        f"[inject] 目标=level{args.level} {args.direction} "
        f"注入值={value:g} 正常值={normal:g} 时长={args.seconds or '∞'}s"
    )
    print(f"[inject] 当前未处理阈值告警={active}")

    if args.dry_run:
        total_rows = "∞" if args.seconds == 0 else samples_per_second * args.seconds
        print(f"[inject] dry-run：预计写入 {total_rows} 行，未写数据库。")
        return 0

    if active and not args.ack_existing:
        print(
            "[inject] 提示：该通道已有未处理告警，同级别不会生成新记录。"
            "重复测试请加 --ack-existing。"
        )
    if args.ack_existing:
        acknowledged = acknowledge_existing(conn, rule.sensor_no, rule.channel)
        print(f"[inject] 已处理 {acknowledged} 条历史阈值告警。")

    started_at = database_now(conn)
    if not args.no_reset:
        # 正常样本的时间必须晚于后端已处理水位，因此先写入；
        # 超限样本从下一时刻开始，保证时间顺序仍然是“先正常、后超限”。
        insert_rows(
            conn,
            [(rule.sensor_no, rule.channel, normal, started_at)],
        )
        print(f"[inject] 已写入正常样本 {normal:g} @ {started_at}")

    injection_start = database_now(conn)
    second_index = 0
    total_rows = 0
    print(f"[inject] 开始注入：{injection_start}")

    try:
        while args.seconds == 0 or second_index < args.seconds:
            tick_start = time.monotonic()
            # 每一秒都重新取数据库时间，避免调试暂停后时间戳落后于扫描窗口。
            batch_start = database_now(conn)
            batch = [
                (
                    rule.sensor_no,
                    rule.channel,
                    value,
                    batch_start + timedelta(seconds=index * interval),
                )
                for index in range(samples_per_second)
            ]
            total_rows += insert_rows(conn, batch)
            second_index += 1
            print(
                f"[inject] +{second_index}s rows={total_rows} value={value:g}",
                flush=True,
            )
            if args.seconds == 0 or second_index < args.seconds:
                time.sleep(max(0.0, 1.0 - (time.monotonic() - tick_start)))
    except KeyboardInterrupt:
        print("\n[inject] 收到 Ctrl+C，停止注入。")

    print(
        f"[inject] 注入结束：共 {total_rows} 行，"
        f"时间范围 {injection_start} ~ {database_now(conn)}"
    )

    if not args.no_verify:
        if args.verify_delay > 0:
            print(f"[inject] 等待 {args.verify_delay:g}s 让检测 Worker 完成扫描...")
            time.sleep(args.verify_delay)
        verify_alerts(
            conn,
            rule.sensor_no,
            rule.channel,
            started_at - timedelta(seconds=1),
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """执行单通道注入。

    注意：不要使用 ``with psycopg2.connect(...)``。psycopg2 的连接上下文
    会保持一个隐式事务，``LOCALTIMESTAMP`` 会固定为事务开始时间，导致
    每秒写入的样本时间戳完全相同，阈值连续时长永远无法达到确认条件。
    """
    args = parse_args(argv)
    if args.seconds < 0:
        raise SystemExit("--seconds 不能为负数。")

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        return run_injection(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
