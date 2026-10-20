"""Add sensor range limits and stuck-time configuration to thresholds.

Revision ID: 20261019_0011
Revises: 20261019_0010
Create Date: 2026-10-19
"""

from __future__ import annotations

import re
from typing import Optional

from alembic import context, op
from sqlalchemy import text

revision = "20261019_0011"
down_revision = "20261019_0010"
branch_labels = None
depends_on = None


def _normalize(text: str) -> str:
    normalized = (
        text.replace("～", "~")
        .replace("〜", "~")
        .replace("−", "-")
        .replace("–", "-")
        .replace("－", "-")
        .replace("—", "-")
        .replace("土", "±")
        .replace("℃", "°C")
    )
    return re.sub(r"(?<=\d)-(?=\d)", "~", normalized)


def _select_range_segment(range_text: str, quantity: str, channel: str) -> Optional[str]:
    normalized = _normalize(range_text.strip())
    candidates = [
        part.strip(" ,，;；")
        for part in re.split(r"[,，;；/]", normalized)
        if part.strip(" ,，;；")
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    quantity_lower = quantity.lower()
    channel_lower = channel.lower()

    def score(segment: str) -> int:
        lower = segment.lower()
        value = 0
        label_checks = (
            ("温度", "temperature", "温度", "temperature"),
            ("湿度", "humidity", "湿度", "humidity"),
            ("风速", "windspeed", "风速", "windspeed"),
            ("风向", "winddirection", "风向", "winddirection"),
            ("降雨", "rainfall", "降雨", "rainfall"),
        )
        for cn_label, en_label, cn_quantity, en_quantity in label_checks:
            if cn_label in segment or en_label in lower:
                matched = (
                    cn_quantity in quantity
                    or en_quantity in quantity_lower
                    or channel_lower == en_label
                )
                value += 100 if matched else -20
        unit_checks = (
            ("%", "%rh", "湿度", "humidity"),
            ("°C", "°c", "温度", "temperature"),
            ("mm/h", "mm/h", "降雨", "rainfall"),
            ("m/s", "m/s", "风速", "windspeed"),
            ("°", "°", "风向", "winddirection"),
        )
        for marker, _marker_lower, cn_quantity, en_quantity in unit_checks:
            if marker in segment:
                matched = (
                    cn_quantity in quantity
                    or en_quantity in quantity_lower
                    or channel_lower == en_quantity
                )
                value += 40 if matched else -40
        return value

    best = max(candidates, key=score)
    return best if score(best) > 0 else None


def _parse_range(
    range_text: Optional[str],
    quantity: str,
    channel: str,
) -> tuple[Optional[float], Optional[float]]:
    if not range_text:
        return None, None
    normalized = _normalize(str(range_text).strip())
    segment = _select_range_segment(normalized, quantity, channel)
    if segment:
        normalized = segment

    numbers = [float(item) for item in re.findall(r"[+-]?\d+(?:\.\d+)?", normalized)]
    if not numbers:
        return None, None
    if "±" in normalized:
        upper = abs(numbers[0])
        lower = -upper
    elif "winddirection" in channel.lower() and len(numbers) >= 4:
        lower, upper = numbers[-2], numbers[-1]
    elif len(numbers) >= 2:
        lower, upper = numbers[0], numbers[1]
    else:
        return None, None

    if "g" in normalized.lower() and (
        "加速度" in quantity or channel.lower() in {"x", "y", "z"}
    ):
        lower *= 9.80665
        upper *= 9.80665
    return round(lower, 5), round(upper, 5)


def _stuck_time_ms(interval_ms: Optional[int], quantity: str, channel: str) -> int:
    interval = max(10, int(interval_ms or 1000))
    quantity_lower = (quantity or "").lower()
    channel_lower = (channel or "").lower()
    if channel_lower in {"x", "y", "z"} and (
        "加速度" in quantity or "accel" in quantity_lower
    ):
        return interval * 50
    if "温度" in quantity or "temperature" in quantity_lower:
        return interval * 60
    if "风向" in quantity or "winddirection" in quantity_lower:
        return interval * 120
    return interval * 30


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          ADD COLUMN IF NOT EXISTS range_min NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS range_max NUMERIC(15,5),
          ADD COLUMN IF NOT EXISTS stuck_time_ms INTEGER NOT NULL DEFAULT 30000
        """
    )

    # Data-dependent backfills cannot be evaluated while Alembic renders
    # offline SQL. The DDL and column comments still render for CI validation.
    if not context.is_offline_mode():
        connection = op.get_bind()
        rows = list(connection.execute(
            text(
                """
                SELECT t.id, t.sensor_no, t.channel, t.physical_quantity,
                       t.expected_interval_ms, s.range
                FROM public.t_alert_threshold t
                LEFT JOIN public.t_sensor s
                  ON LOWER(s.sensor_no) = LOWER(t.sensor_no)
                """
            )
        ).mappings())

        update_sql = text(
            """
            UPDATE public.t_alert_threshold
            SET range_min = :range_min,
                range_max = :range_max,
                stuck_time_ms = :stuck_time_ms
            WHERE id = :id
            """
        )
        for row in rows:
            range_min, range_max = _parse_range(
                row["range"],
                row["physical_quantity"] or "",
                row["channel"] or "",
            )
            connection.execute(
                update_sql,
                {
                    "id": row["id"],
                    "range_min": range_min,
                    "range_max": range_max,
                    "stuck_time_ms": _stuck_time_ms(
                        row["expected_interval_ms"],
                        row["physical_quantity"] or "",
                        row["channel"] or "",
                    ),
                },
            )

    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.range_min IS
          '传感器量程最小值，按监测物理量或通道从 t_sensor.range 匹配'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.range_max IS
          '传感器量程最大值，按监测物理量或通道从 t_sensor.range 匹配'
        """
    )
    op.execute(
        """
        COMMENT ON COLUMN public.t_alert_threshold.stuck_time_ms IS
          '传感器死值判定时间，单位毫秒'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.t_alert_threshold
          DROP COLUMN IF EXISTS range_min,
          DROP COLUMN IF EXISTS range_max,
          DROP COLUMN IF EXISTS stuck_time_ms
        """
    )
