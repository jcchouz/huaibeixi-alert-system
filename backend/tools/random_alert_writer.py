#!/usr/bin/env python
"""持续向远端写入随机预警和统计记录。

脚本固定连接 82.156.158.31 数据库，并从远端已有的
``t_alert_threshold``、``t_sensor`` 记录中选择真实传感器和通道。
脚本每 3 秒向 ``t_alert_record`` 写入一条随机预警，并在每个完整
1 分钟、10 分钟窗口向 ``t_stat_record`` 写入所有真实传感器通道的随机统计。
脚本不创建或修改传感器、通道和阈值。
"""

from __future__ import annotations

import argparse
import logging
import math
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg2
from psycopg2.extras import Json, RealDictCursor, execute_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import Settings
from app.domain.alert_types import (
    ALERT_CATEGORY_DATA,
    ALERT_CATEGORY_DEVICE,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
    DEVICE_ALERT_TYPES,
    NON_THRESHOLD_ALERT_LEVEL,
)

REMOTE_DB_HOST = "82.156.158.31"
DEFAULT_INTERVAL_SECONDS = 3.0
ALL_ALERT_TYPES = (
    ALERT_TYPE_THRESHOLD,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_OOR,
)
STAT_PERIODS = (("1m", 1), ("10m", 10))

logger = logging.getLogger("random_alert_writer")


@dataclass(frozen=True)
class RealThreshold:
    """从远端读取的一条真实阈值通道配置。"""

    threshold_id: str
    sensor_no: str
    channel: str
    instrument_id: str
    alert_function: int | None
    physical_quantity: str
    frequency: str
    level1_upper: float | None
    level1_lower: float | None
    level2_upper: float | None
    level2_lower: float | None
    level3_upper: float | None
    level3_lower: float | None
    range_min: float | None
    range_max: float | None


@dataclass(frozen=True)
class GeneratedAlert:
    """待写入 t_alert_record 的一条随机预警。"""

    sensor_no: str
    channel: str
    instrument_id: str
    threshold_id: str | None
    alert_type: str
    alert_category: int
    alert_level: int
    trigger_val: float
    alert_function: int | None
    rule_snapshot: dict[str, Any]


@dataclass(frozen=True)
class StatTarget:
    """需要生成统计记录的真实传感器通道。"""

    sensor_no: str
    channel: str
    range_min: float | None
    range_max: float | None


def positive_float(value: str) -> float:
    """解析大于零的有限浮点数。"""
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("必须为大于零的有限数字")
    return parsed


def non_negative_int(value: str) -> int:
    """解析大于等于零的整数。"""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须为大于等于零的整数")
    return parsed


def parse_alert_types(value: str) -> tuple[str, ...]:
    """解析逗号分隔的预警类型，并保留标准顺序。"""
    requested = {item.strip() for item in value.split(",") if item.strip()}
    unknown = requested - set(ALL_ALERT_TYPES)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"不支持的预警类型：{', '.join(sorted(unknown))}"
        )
    if not requested:
        raise argparse.ArgumentTypeError("至少需要指定一种预警类型")
    return tuple(item for item in ALL_ALERT_TYPES if item in requested)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--interval-seconds",
        type=positive_float,
        default=DEFAULT_INTERVAL_SECONDS,
        help="写入间隔秒数，默认 3 秒",
    )
    parser.add_argument(
        "--max-records",
        type=non_negative_int,
        default=0,
        help="最多写入条数，0 表示持续运行",
    )
    parser.add_argument(
        "--alert-types",
        type=parse_alert_types,
        default=ALL_ALERT_TYPES,
        help="逗号分隔的预警类型；默认包含全部五类",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只读取真实通道并打印生成的预警，不写数据库",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="日志文件路径；默认写入 backend/logs 下的时间戳文件",
    )
    return parser


def configure_logging(log_file: Path | None) -> Path:
    """初始化控制台和文件日志，返回实际日志路径。"""
    if log_file is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = BACKEND_DIR / "logs" / f"random_alert_writer_{timestamp}.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return log_file


def build_settings() -> Settings:
    """加载本地连接参数，并强制把数据库地址替换为远端地址。"""
    settings = Settings()
    return settings.model_copy(update={"db_host": REMOTE_DB_HOST})


def number_or_none(value: Any) -> float | None:
    """把数据库数值转换为浮点数。"""
    return float(value) if value is not None else None


def load_real_thresholds(conn: Any) -> list[RealThreshold]:
    """读取同时存在于阈值表和传感器表中的真实通道。"""
    query = """
        SELECT
            t.id AS threshold_id,
            BTRIM(t.sensor_no) AS sensor_no,
            COALESCE(BTRIM(t.channel), '') AS channel,
            COALESCE(BTRIM(s.instrument_id), '') AS instrument_id,
            t.alert_function,
            COALESCE(BTRIM(t.physical_quantity), '') AS physical_quantity,
            COALESCE(BTRIM(s.frequency), '') AS frequency,
            t.level1_upper,
            t.level1_lower,
            t.level2_upper,
            t.level2_lower,
            t.level3_upper,
            t.level3_lower,
            t.range_min,
            t.range_max
        FROM public.t_alert_threshold AS t
        JOIN public.t_sensor AS s
          ON LOWER(BTRIM(s.sensor_no)) = LOWER(BTRIM(t.sensor_no))
        WHERE BTRIM(t.sensor_no) <> ''
        ORDER BY LOWER(BTRIM(t.sensor_no)), COALESCE(BTRIM(t.channel), '')
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query)
        rows = [dict(row) for row in cur.fetchall()]

    return [
        RealThreshold(
            threshold_id=str(row["threshold_id"]),
            sensor_no=str(row["sensor_no"]),
            channel=str(row["channel"]),
            instrument_id=str(row["instrument_id"]),
            alert_function=(
                int(row["alert_function"])
                if row["alert_function"] is not None
                else None
            ),
            physical_quantity=str(row["physical_quantity"]),
            frequency=str(row["frequency"]),
            level1_upper=number_or_none(row["level1_upper"]),
            level1_lower=number_or_none(row["level1_lower"]),
            level2_upper=number_or_none(row["level2_upper"]),
            level2_lower=number_or_none(row["level2_lower"]),
            level3_upper=number_or_none(row["level3_upper"]),
            level3_lower=number_or_none(row["level3_lower"]),
            range_min=number_or_none(row["range_min"]),
            range_max=number_or_none(row["range_max"]),
        )
        for row in rows
    ]


def load_stat_targets(conn: Any) -> list[StatTarget]:
    """加载全部真实传感器通道，并补充阈值表中的可配置通道。"""
    query = """
        WITH target_pairs AS (
            SELECT
                LOWER(BTRIM(t.sensor_no)) AS sensor_no,
                COALESCE(BTRIM(t.channel), '') AS channel
            FROM public.t_alert_threshold AS t
            JOIN public.t_sensor AS s
              ON LOWER(BTRIM(s.sensor_no)) = LOWER(BTRIM(t.sensor_no))
            WHERE BTRIM(t.sensor_no) <> ''
            UNION
            SELECT
                LOWER(BTRIM(c.sensor_no)) AS sensor_no,
                COALESCE(BTRIM(c.monitor_code), '') AS channel
            FROM public.t_sensor_channel AS c
            JOIN public.t_sensor AS s
              ON LOWER(BTRIM(s.sensor_no)) = LOWER(BTRIM(c.sensor_no))
            WHERE BTRIM(c.sensor_no) <> ''
            UNION
            SELECT
                LOWER(BTRIM(s.sensor_no)) AS sensor_no,
                ''::text AS channel
            FROM public.t_sensor AS s
            WHERE BTRIM(s.sensor_no) <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.t_alert_threshold AS t
                  WHERE LOWER(BTRIM(t.sensor_no)) = LOWER(BTRIM(s.sensor_no))
                  UNION ALL
                  SELECT 1
                  FROM public.t_sensor_channel AS c
                  WHERE LOWER(BTRIM(c.sensor_no)) = LOWER(BTRIM(s.sensor_no))
              )
        )
        SELECT
            target.sensor_no,
            target.channel,
            MAX(threshold.range_min) AS range_min,
            MAX(threshold.range_max) AS range_max
        FROM target_pairs AS target
        LEFT JOIN public.t_alert_threshold AS threshold
          ON LOWER(BTRIM(threshold.sensor_no)) = target.sensor_no
         AND COALESCE(BTRIM(threshold.channel), '') = target.channel
        GROUP BY target.sensor_no, target.channel
        ORDER BY target.sensor_no, target.channel
    """
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(query)
        rows = [dict(row) for row in cur.fetchall()]

    return [
        StatTarget(
            sensor_no=str(row["sensor_no"]),
            channel=str(row["channel"]),
            range_min=number_or_none(row["range_min"]),
            range_max=number_or_none(row["range_max"]),
        )
        for row in rows
    ]


def floor_window_start(value: datetime, minutes: int) -> datetime:
    """把数据库时间向下对齐到指定分钟窗口。"""
    day_start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = int((value - day_start).total_seconds() // 60)
    return day_start + timedelta(
        minutes=(elapsed_minutes // minutes) * minutes
    )


def generate_statistics_rows(
    targets: Sequence[StatTarget],
    *,
    period_type: str,
    minutes: int,
    window_start: datetime,
    rng: random.Random,
) -> list[tuple[Any, ...]]:
    """为所有真实传感器通道生成一个完整窗口的随机统计。"""
    window_end = window_start + timedelta(minutes=minutes)
    max_samples = minutes * 60
    rows: list[tuple[Any, ...]] = []
    for target in targets:
        range_min = target.range_min
        range_max = target.range_max
        if (
            range_min is None
            or range_max is None
            or not math.isfinite(range_min)
            or not math.isfinite(range_max)
            or range_max <= range_min
        ):
            range_min, range_max = -100.0, 100.0

        # 生成三个随机点并排序，保证最小值、平均值和最大值关系合法。
        sampled_values = sorted(
            rng.uniform(range_min, range_max) for _ in range(3)
        )
        val_min = sampled_values[0]
        val_avg = rng.uniform(sampled_values[0], sampled_values[2])
        val_max = sampled_values[2]
        max_time = window_start + timedelta(
            seconds=rng.uniform(0, max(minutes * 60 - 0.001, 0))
        )
        min_time = window_start + timedelta(
            seconds=rng.uniform(0, max(minutes * 60 - 0.001, 0))
        )
        sample_count = rng.randint(max(1, max_samples // 20), max_samples)
        rows.append(
            (
                uuid.uuid4().hex,
                target.sensor_no,
                target.channel,
                period_type,
                window_start,
                val_max,
                val_min,
                val_avg,
                max_time,
                min_time,
                sample_count,
                "random_alert_writer",
                window_end,
            )
        )
    return rows


def insert_statistics(conn: Any, rows: Sequence[tuple[Any, ...]]) -> int:
    """批量追加统计记录，不删除已有窗口数据。"""
    if not rows:
        return 0
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO public.t_stat_record (
                    id, sensor_no, channel, period_type, stat_time,
                    val_max, val_min, val_avg,
                    max_time, min_time, sample_count,
                    create_by, create_time
                ) VALUES %s
                """,
                rows,
                template=(
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                ),
                page_size=500,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)


def threshold_triggers(
    rule: RealThreshold,
    rng: random.Random,
) -> list[tuple[int, float]]:
    """生成该真实通道所有可用阈值等级的超限值。"""
    bounds = (
        (1, rule.level1_upper, rule.level1_lower),
        (2, rule.level2_upper, rule.level2_lower),
        (3, rule.level3_upper, rule.level3_lower),
    )
    candidates: list[tuple[int, float]] = []
    scale = max(
        abs(rule.range_max - rule.range_min)
        if rule.range_min is not None and rule.range_max is not None
        else 1.0,
        1.0,
    )
    for level, upper, lower in bounds:
        if upper is not None:
            candidates.append((level, upper + scale * rng.uniform(0.001, 0.01)))
        if lower is not None:
            candidates.append((level, lower - scale * rng.uniform(0.001, 0.01)))
    return candidates


def normal_value(rule: RealThreshold, rng: random.Random) -> float:
    """在已知正常区间或物理量程内生成一个值。"""
    uppers = [
        value
        for value in (
            rule.level3_upper,
            rule.level2_upper,
            rule.level1_upper,
            rule.range_max,
        )
        if value is not None
    ]
    lowers = [
        value
        for value in (
            rule.level3_lower,
            rule.level2_lower,
            rule.level1_lower,
            rule.range_min,
        )
        if value is not None
    ]
    if uppers and lowers:
        lower = max(lowers)
        upper = min(uppers)
        if lower < upper:
            return rng.uniform(lower, upper)
        return (lower + upper) / 2.0
    if uppers:
        return uppers[0] - abs(uppers[0]) * 0.01
    if lowers:
        return lowers[0] + abs(lowers[0]) * 0.01
    return 0.0


def out_of_range_value(rule: RealThreshold, rng: random.Random) -> float:
    """在真实通道的物理量程外生成一个值。"""
    if rule.range_min is not None and rule.range_max is not None:
        span = abs(rule.range_max - rule.range_min)
        margin = max(span * rng.uniform(0.001, 0.01), 0.00001)
        if rng.choice((True, False)):
            return rule.range_min - margin
        return rule.range_max + margin
    if rule.range_max is not None:
        return rule.range_max + max(abs(rule.range_max) * 0.01, 0.00001)
    if rule.range_min is not None:
        return rule.range_min - max(abs(rule.range_min) * 0.01, 0.00001)
    return rng.uniform(-1.0, 1.0)


def build_rule_snapshot(alert: GeneratedAlert) -> dict[str, Any]:
    """生成可追溯的随机预警规则快照。"""
    return {
        "generator": "random_alert_writer",
        **alert.rule_snapshot,
    }


def generate_alert(
    rule: RealThreshold,
    alert_types: Sequence[str],
    rng: random.Random,
) -> GeneratedAlert:
    """根据真实通道生成一条随机预警。"""
    selected_type = rng.choice(tuple(alert_types))
    threshold_id = rule.threshold_id
    sensor_no = rule.sensor_no
    channel = rule.channel
    alert_level = NON_THRESHOLD_ALERT_LEVEL

    if selected_type == ALERT_TYPE_DAQ_OFFLINE:
        if not rule.instrument_id:
            selected_type = ALERT_TYPE_OFFLINE
        else:
            sensor_no = rule.instrument_id
            channel = "__DAQ__"
            threshold_id = None
            trigger_val = 0.0
    elif selected_type == ALERT_TYPE_THRESHOLD:
        candidates = threshold_triggers(rule, rng)
        if candidates:
            alert_level, trigger_val = rng.choice(candidates)
        else:
            trigger_val = normal_value(rule, rng)
    elif selected_type == ALERT_TYPE_OFFLINE:
        trigger_val = 0.0
    elif selected_type == ALERT_TYPE_STUCK:
        trigger_val = normal_value(rule, rng)
    else:
        trigger_val = out_of_range_value(rule, rng)

    alert_category = (
        ALERT_CATEGORY_DEVICE
        if selected_type in DEVICE_ALERT_TYPES
        else ALERT_CATEGORY_DATA
    )
    alert = GeneratedAlert(
        sensor_no=sensor_no,
        channel=channel,
        instrument_id=rule.instrument_id,
        threshold_id=threshold_id,
        alert_type=selected_type,
        alert_category=alert_category,
        alert_level=alert_level,
        trigger_val=float(trigger_val),
        alert_function=rule.alert_function,
        rule_snapshot={
            "alert_type": selected_type,
            "threshold_id": rule.threshold_id,
            "sensor_no": rule.sensor_no,
            "channel": rule.channel,
            "instrument_id": rule.instrument_id,
            "alert_function": rule.alert_function,
            "physical_quantity": rule.physical_quantity,
            "frequency": rule.frequency,
            "level1_upper": rule.level1_upper,
            "level1_lower": rule.level1_lower,
            "level2_upper": rule.level2_upper,
            "level2_lower": rule.level2_lower,
            "level3_upper": rule.level3_upper,
            "level3_lower": rule.level3_lower,
            "range_min": rule.range_min,
            "range_max": rule.range_max,
        },
    )
    return alert


def has_open_alert(conn: Any, alert: GeneratedAlert) -> bool:
    """检查同一传感器、通道和类型是否已有未结束预警。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM public.t_alert_record
            WHERE LOWER(sensor_no) = LOWER(%s)
              AND COALESCE(channel, '') = %s
              AND alert_type = %s
              AND end_time IS NULL
            LIMIT 1
            """,
            (alert.sensor_no, alert.channel, alert.alert_type),
        )
        return cur.fetchone() is not None


def insert_alert(conn: Any, alert: GeneratedAlert) -> dict[str, Any]:
    """写入一条预警；若同键活动预警已存在，则把新记录标记为已结束。"""
    close_immediately = has_open_alert(conn, alert)
    alert_id = uuid.uuid4().hex
    snapshot = build_rule_snapshot(alert)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO public.t_alert_record (
                    id, seq, sensor_no, channel, threshold_id,
                    alert_type, alert_category, alert_level,
                    trigger_val, create_time, alert_status, end_time,
                    rule_snapshot, alert_function
                ) VALUES (
                    %s, nextval(
                        pg_get_serial_sequence('public.t_alert_record', 'seq')
                    ), %s, %s, %s,
                    %s, %s, %s,
                    %s, NOW(), 0, CASE WHEN %s THEN NOW() ELSE NULL END,
                    %s, %s
                )
                RETURNING seq, create_time, end_time
                """,
                (
                    alert_id,
                    alert.sensor_no,
                    alert.channel,
                    alert.threshold_id,
                    alert.alert_type,
                    alert.alert_category,
                    alert.alert_level,
                    alert.trigger_val,
                    close_immediately,
                    Json(snapshot),
                    alert.alert_function,
                ),
            )
            row = dict(cur.fetchone())
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise


def database_now(conn: Any) -> datetime:
    """读取数据库当前时间。"""
    with conn.cursor() as cur:
        cur.execute("SELECT LOCALTIMESTAMP")
        row = cur.fetchone()
    conn.rollback()
    if row is None:
        raise RuntimeError("数据库未返回当前时间")
    return row[0]


def database_identity(conn: Any) -> str:
    """读取当前连接的数据库名称和用户。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_database(), current_user, current_setting('server_version')"
        )
        row = cur.fetchone()
    conn.rollback()
    if row is None:
        raise RuntimeError("数据库未返回连接身份")
    return f"database={row[0]} user={row[1]} server_version={row[2]}"


def format_alert_message(
    alert: GeneratedAlert,
    sequence: int,
    written: int,
    row: dict[str, Any] | None = None,
) -> str:
    """格式化单条预警日志。"""
    status = "dry-run"
    if row is not None:
        status = f"seq={row['seq']} create_time={row['create_time']}"
        if row["end_time"] is not None:
            status += " end_time=同键活动预警已存在"
    return (
        f"[{written}] {status} sensor={alert.sensor_no} "
        f"channel={alert.channel or '(default)'} "
        f"instrument={alert.instrument_id or '(unknown)'} "
        f"type={alert.alert_type} level={alert.alert_level} "
        f"value={alert.trigger_val:.6f} sequence={sequence}"
    )


def run(args: argparse.Namespace, stop_requested: list[bool]) -> int:
    """建立远端连接并持续写入随机预警。"""
    log_file = configure_logging(args.log_file)
    settings = build_settings()
    logger.info(
        "启动 target=%s:%s/%s user=%s dry_run=%s log_file=%s",
        settings.db_host,
        settings.db_port,
        settings.db_name,
        settings.db_user,
        args.dry_run,
        log_file,
    )

    with psycopg2.connect(
        settings.dsn,
        application_name="random_alert_writer",
    ) as conn:
        logger.info("连接成功 %s", database_identity(conn))
        rules = load_real_thresholds(conn)
        if not rules:
            raise RuntimeError("远端没有同时存在于 t_alert_threshold 和 t_sensor 的真实通道")
        logger.info("加载真实阈值通道 count=%d", len(rules))
        stat_targets = load_stat_targets(conn)
        if not stat_targets:
            raise RuntimeError("远端没有可生成统计记录的真实传感器通道")
        logger.info(
            "加载统计目标 count=%d sensors=%d",
            len(stat_targets),
            len({target.sensor_no for target in stat_targets}),
        )

        session_start = database_now(conn)
        logger.info(
            "测试开始 database_time=%s alert_types=%s",
            session_start.isoformat(sep=" ", timespec="seconds"),
            ",".join(args.alert_types),
        )

        rng = random.SystemRandom()
        written = 0
        statistics_written = 0
        last_stat_boundaries: dict[str, datetime] = {}
        next_tick = time.monotonic()
        while not stop_requested[0] and (
            args.max_records == 0 or written < args.max_records
        ):
            rule = rng.choice(rules)
            alert = generate_alert(rule, args.alert_types, rng)
            sequence = written + 1
            if args.dry_run:
                logger.info(format_alert_message(alert, sequence, sequence))
            else:
                row = insert_alert(conn, alert)
                logger.info(format_alert_message(alert, sequence, sequence, row))
            written = sequence

            iteration_now = database_now(conn)
            for period_type, minutes in STAT_PERIODS:
                boundary = floor_window_start(iteration_now, minutes)
                if last_stat_boundaries.get(period_type) == boundary:
                    continue
                window_start = boundary - timedelta(minutes=minutes)
                stat_rows = generate_statistics_rows(
                    stat_targets,
                    period_type=period_type,
                    minutes=minutes,
                    window_start=window_start,
                    rng=rng,
                )
                if args.dry_run:
                    inserted = len(stat_rows)
                else:
                    inserted = insert_statistics(conn, stat_rows)
                statistics_written += inserted
                last_stat_boundaries[period_type] = boundary
                logger.info(
                    "统计%s period=%s window_start=%s window_end=%s rows=%d",
                    "生成" if args.dry_run else "写入",
                    period_type,
                    window_start.isoformat(sep=" ", timespec="seconds"),
                    (
                        window_start + timedelta(minutes=minutes)
                    ).isoformat(sep=" ", timespec="seconds"),
                    inserted,
                )

            if args.max_records and written >= args.max_records:
                break
            next_tick += args.interval_seconds
            sleep_seconds = next_tick - time.monotonic()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            elif written < args.max_records or args.max_records == 0:
                logger.warning("写入耗时超过间隔，已出现 %.3f 秒积压", -sleep_seconds)

        session_end = database_now(conn)
        stop_reason = "signal" if stop_requested[0] else "completed"
        logger.info(
            "测试结束 database_time=%s alerts=%d statistics=%d "
            "elapsed_seconds=%.3f stop_reason=%s log_file=%s",
            session_end.isoformat(sep=" ", timespec="seconds"),
            written,
            statistics_written,
            max((session_end - session_start).total_seconds(), 0.0),
            stop_reason,
            log_file,
        )
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    """解析参数并处理正常退出和中断退出。"""
    args = build_parser().parse_args(argv)
    stop_requested = [False]

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_requested[0] = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        return run(args, stop_requested)
    except KeyboardInterrupt:
        logger.info("收到中断信号，脚本停止")
        return 0
    except (psycopg2.Error, OSError, RuntimeError, ValueError) as exc:
        logger.error("运行失败：%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
