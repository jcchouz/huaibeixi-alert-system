import logging
import math
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.serialization import serialize_row, serialize_val
from app.domain.alert_types import (
    ALERT_CATEGORY_LABELS,
    ALERT_DIRECTION_LOWER,
    ALERT_DIRECTION_NONE,
    ALERT_DIRECTION_UPPER,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
    NON_THRESHOLD_ALERT_LEVEL,
    alert_category_for_type,
    normalize_alert_type,
)
from app.infrastructure.database import get_db_cursor

logger = logging.getLogger("huaibeixi.engine")

# Single-pass push-down aggregation over one t_monitor window.
#   s_no / ch            -> grouping key (sensor_no normalized to lower-case)
#   max_time / min_time  -> true timestamp of the extreme sample (not a window boundary)
WINDOW_AGG_SQL = """
WITH ordered_samples AS (
    SELECT
        LOWER(sensor_no) AS s_no,
        COALESCE(channel, '') AS ch,
        time,
        value,
        time - LAG(time) OVER (
            PARTITION BY LOWER(sensor_no), COALESCE(channel, '')
            ORDER BY time
        ) AS sample_gap
    FROM public.t_monitor
    WHERE time >= %s AND time < %s
)
SELECT
    s_no,
    ch,
    COUNT(*) AS sample_cnt,
    MAX(value) AS val_max,
    MIN(value) AS val_min,
    AVG(value) AS val_avg,
    COALESCE(STDDEV_POP(value), 0) AS value_std,
    MIN(time) AS first_ts,
    MAX(time) AS last_ts,
    COALESCE(MAX(EXTRACT(EPOCH FROM sample_gap) * 1000.0), 0) AS max_gap_ms,
    (ARRAY_AGG(time ORDER BY value ASC, time ASC))[1] AS min_time,
    (ARRAY_AGG(time ORDER BY value DESC, time ASC))[1] AS max_time
FROM ordered_samples
GROUP BY s_no, ch
"""

STAT_UPSERT_SQL = """
INSERT INTO public.t_stat_record (
    sensor_no, channel, period_type, stat_time,
    val_max, val_min, val_avg,
    max_time, min_time, sample_count,
    create_by, create_time
) VALUES (
    %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    'system', NOW()
)
ON CONFLICT (stat_time, sensor_no, channel, period_type)
DO UPDATE SET
    val_max = EXCLUDED.val_max,
    val_min = EXCLUDED.val_min,
    val_avg = EXCLUDED.val_avg,
    max_time = EXCLUDED.max_time,
    min_time = EXCLUDED.min_time,
    sample_count = EXCLUDED.sample_count
"""

class ConcurrentDict:
    """Thread-safe in-memory dictionary for channel rules and runtime state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            val = self._data.get(key, default)
            return dict(val) if isinstance(val, dict) else val

    def set(self, key: str, value: Dict[str, Any]) -> None:
        with self._lock:
            self._data[key] = value

    def update_field(self, key: str, **kwargs: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data[key].update(kwargs)

    def replace_all(self, new_map: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            # Preserve runtime state fields if already present
            merged: Dict[str, Dict[str, Any]] = {}
            for k, v in new_map.items():
                old = self._data.get(k)
                if old:
                    for rt_field in (
                        "last_ts",
                        "last_val",
                        "online_status",
                        "threshold_streak_count",
                        "threshold_streak_direction",
                        "threshold_last_level",
                        "threshold_last_value",
                        "threshold_last_limit",
                        "oor_streak_count",
                        "oor_streak_direction",
                        "oor_last_value",
                        "stuck_streak_count",
                        "stuck_last_value",
                        "stuck_since",
                    ):
                        if rt_field in old and rt_field not in v:
                            v[rt_field] = old[rt_field]
                merged[k] = v
            self._data = merged

    def values(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._data.values()]

    def items(self) -> List[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            return [(k, dict(v)) for k, v in self._data.items()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class AlertAnalyticsEngine:
    """
    Core Alert & Scheduled Analytics Engine:
    - Maintains in-memory ConcurrentDict of all 208 sensor channel threshold rules & metadata.
    - Supports zero-downtime hot reload (`reload_thresholds()`).
    - Evaluates explicit time windows over `t_monitor`.
    - Persists window statistics while evaluating threshold, out-of-range, stuck
      and offline conditions as configurable consecutive sample groups.
    - Derives each sample period from ``t_sensor.frequency``.
    - Supports dynamic incident upgrade and manual handling.
    """

    def __init__(
        self,
        *,
        confirm_points: int = 3,
        stuck_points: int = 100,
        offline_points: int = 5,
    ) -> None:
        self.channel_states = ConcurrentDict()
        self.sensor_meta: Dict[str, Dict[str, Any]] = {}
        self.instrument_meta: Dict[str, Dict[str, Any]] = {}
        self.total_monitor_rows: int = 398059510
        self.last_reload_time: Optional[datetime] = None
        self.confirm_points = max(1, int(confirm_points))
        self.stuck_points = max(1, int(stuck_points))
        self.offline_points = max(1, int(offline_points))
        self._step_lock = threading.Lock()

    @staticmethod
    def make_key(sensor_no: str, channel: Optional[str]) -> str:
        return f"{(sensor_no or '').strip().lower()}:{(channel or '').strip()}"

    def initialize(self) -> None:
        """Load sensor metadata, instrument metadata, and 208 threshold rules into memory."""
        self.reload_thresholds()
        self.bootstrap_last_seen()
        self.refresh_monitor_row_count()

    def bootstrap_last_seen(self) -> None:
        """Load each configured channel's latest t_monitor timestamp."""
        rules = self.channel_states.values()
        if not rules:
            return

        with get_db_cursor() as cur:
            cur.execute("SELECT LOCALTIMESTAMP AS now")
            now = cur.fetchone()["now"]
        max_timeout_seconds = max(
            self._offline_timeout_ms(rule) / 1000.0 for rule in rules
        )
        lookback_seconds = max(60.0, max_timeout_seconds)
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT
                  LOWER(sensor_no) AS sensor_no,
                  COALESCE(channel, '') AS channel,
                  MAX(time) AS last_ts
                FROM public.t_monitor
                WHERE time >= %s
                GROUP BY LOWER(sensor_no), COALESCE(channel, '')
                """,
                (now - timedelta(seconds=lookback_seconds),),
            )
            latest_by_target = {
                self.make_key(row["sensor_no"], row["channel"]): row["last_ts"]
                for row in cur.fetchall()
            }

        for rule in rules:
            key = rule["key"]
            last_seen = self._parse_maybe_dt(latest_by_target.get(key))
            offline_timeout_ms = self._offline_timeout_ms(rule)
            timeout = timedelta(milliseconds=offline_timeout_ms)
            if last_seen is None:
                last_seen = now - timeout - timedelta(minutes=2)
            online_status = "ONLINE" if now - last_seen <= timeout else "OFFLINE"
            self.channel_states.update_field(
                key,
                last_ts=serialize_val(last_seen),
                online_status=online_status,
            )

    def refresh_monitor_row_count(self) -> int:
        """Refresh ``total_monitor_rows`` from TimescaleDB metadata (cheap, no full scan)."""
        try:
            with get_db_cursor() as cur:
                cur.execute("SELECT hypertable_approximate_row_count('public.t_monitor') AS cnt")
                row = cur.fetchone()
                if row and row["cnt"]:
                    self.total_monitor_rows = int(row["cnt"])
        except Exception:
            try:
                with get_db_cursor() as cur:
                    cur.execute("SELECT reltuples::bigint AS cnt FROM pg_class WHERE relname = 't_monitor'")
                    row = cur.fetchone()
                    if row and row["cnt"] and int(row["cnt"]) > 0:
                        self.total_monitor_rows = int(row["cnt"])
            except Exception as exc:
                logger.warning("Could not refresh monitor row count: %s", exc)
        return self.total_monitor_rows

    def reload_thresholds(self) -> int:
        """Hot-reload all threshold rules and sensor/station/instrument metadata with zero downtime."""
        with get_db_cursor() as cur:
            # 1. Load collect instruments and station mapping
            cur.execute(
                """
                SELECT ci.id, ci.number, ci.name, ci.model, ci.manufacturer, ci.box_id,
                       cb.name AS box_name, cb.station AS station_code,
                       COALESCE(st.name, CASE WHEN cb.station = 'YHZ' THEN '玉环站' ELSE '淮北西站' END) AS station_name
                FROM t_collect_instrument ci
                LEFT JOIN t_collect_box cb ON ci.box_id = cb.id
                LEFT JOIN t_station st ON cb.station = st.number
                """
            )
            inst_map: Dict[str, Dict[str, Any]] = {}
            for r in cur.fetchall():
                inst_map[r["id"]] = serialize_row(r)
            self.instrument_meta = inst_map

            # 2. Load sensors and channels
            cur.execute(
                """
                SELECT s.id AS sensor_id, s.sensor_no, s.name AS sensor_name, s.model,
                       s.manufacturer, s.range, s.precision, s.frequency, s.position,
                       s.instrument_id,
                       ci.name AS instrument_name,
                       cb.station AS station_code,
                       st.name AS station_name
                FROM t_sensor s
                LEFT JOIN t_collect_instrument ci ON s.instrument_id = ci.id
                LEFT JOIN t_collect_box cb ON ci.box_id = cb.id
                LEFT JOIN t_station st ON cb.station = st.number
                """
            )
            s_meta: Dict[str, Dict[str, Any]] = {}
            for r in cur.fetchall():
                sno_lower = (r["sensor_no"] or "").strip().lower()
                sname = r["sensor_name"] or r["sensor_no"]
                st_code = r["station_code"]
                st_name = r["station_name"]
                if not st_name:
                    if "_YH" in sname or sno_lower.startswith(("stn-", "wlg-", "wea-", "accl-", "disp-", "phtar-", "wprs-", "temp-", "phdm-")):
                        st_code = "YHZ"
                        st_name = "玉环站"
                    else:
                        st_code = "HBXZ"
                        st_name = "淮北西站"
                s_meta[sno_lower] = {
                    "sensor_id": r["sensor_id"],
                    "sensor_no": r["sensor_no"],
                    "sensor_name": sname,
                    "model": r["model"],
                    "manufacturer": r["manufacturer"],
                    "range": r["range"] or "-",
                    "precision": r["precision"] or "-",
                    "frequency": r["frequency"] or "1",
                    "position": r["position"] or "-",
                    "instrument_id": r["instrument_id"],
                    "instrument_name": r["instrument_name"] or "-",
                    "station_code": st_code,
                    "station_name": st_name,
                }
            self.sensor_meta = s_meta

            # 3. Load channel metadata
            cur.execute(
                "SELECT sensor_no, name, unit, monitor_content, monitor_code "
                "FROM t_sensor_channel"
            )
            channel_meta: Dict[str, Dict[str, str]] = {}
            for r in cur.fetchall():
                sno_l = (r["sensor_no"] or "").strip().lower()
                meta = {
                    "unit": (r["unit"] or "").strip(),
                    "physical_quantity": (r["monitor_content"] or r["name"] or "").strip(),
                }
                for alias in (r["monitor_code"], r["monitor_content"], r["name"]):
                    alias_text = (alias or "").strip()
                    if alias_text:
                        channel_meta[f"{sno_l}:{alias_text}"] = meta

            # 4. Load all threshold rules
            cur.execute(
                """
                SELECT *
                FROM t_alert_threshold
                ORDER BY sensor_no ASC, channel ASC
                """
            )
            new_states: Dict[str, Dict[str, Any]] = {}
            for row in cur.fetchall():
                d = serialize_row(row)
                sno_l = (d["sensor_no"] or "").strip().lower()
                ch = (d["channel"] or "").strip()
                key = self.make_key(sno_l, ch)
                sm = s_meta.get(sno_l, {})
                ch_meta = channel_meta.get(f"{sno_l}:{ch}", {})
                physical_quantity = d.get("physical_quantity") or ch_meta.get(
                    "physical_quantity"
                ) or ch
                unit = (
                    ch_meta.get("unit")
                    or self._infer_unit(physical_quantity, ch)
                )
                sname = sm.get("sensor_name") or d["sensor_no"]
                st_name = sm.get("station_name") or ("玉环站" if "_YH" in sname or sno_l.startswith(("stn-", "wlg-", "wea-", "accl-", "disp-", "phtar-", "wprs-", "temp-")) else "淮北西站")
                st_code = sm.get("station_code") or ("YHZ" if st_name == "玉环站" else "HBXZ")
                remove_dc_offset = self._infer_remove_dc_offset(sno_l, sname, ch)
                range_min = d.get("range_min")
                range_max = d.get("range_max")
                if range_min is None or range_max is None:
                    range_min, range_max = self._parse_physical_range(
                        sm.get("range"),
                        unit,
                        physical_quantity,
                        ch,
                    )
                frequency_text = (
                    sm.get("frequency")
                    or self._hz_from_interval(d.get("expected_interval_ms"))
                )
                frequency_hz = self._parse_frequency_hz(frequency_text)
                sample_interval_ms = 1000.0 / frequency_hz
                confirm_points = self.confirm_points
                stuck_points = self.stuck_points
                offline_points = self.offline_points

                d.update(
                    {
                        "key": key,
                        "sensor_no_lower": sno_l,
                        "sensor_name": sname,
                        "station_name": st_name,
                        "station_code": st_code,
                        "instrument_id": sm.get("instrument_id"),
                        "instrument_name": sm.get("instrument_name") or "-",
                        "range": sm.get("range") or "-",
                        "frequency": str(frequency_text),
                        "frequency_hz": frequency_hz,
                        "sample_interval_ms": sample_interval_ms,
                        "expected_interval_ms": max(1, int(round(sample_interval_ms))),
                        "position": sm.get("position") or "-",
                        "physical_quantity": physical_quantity,
                        "unit": unit,
                        "remove_dc_offset": remove_dc_offset,
                        "physical_min": range_min,
                        "physical_max": range_max,
                        "confirm_points": confirm_points,
                        "stuck_points": stuck_points,
                        "offline_points": offline_points,
                        "online_status": "ONLINE",
                        "threshold_streak_count": 0,
                        "threshold_streak_direction": ALERT_DIRECTION_NONE,
                        "oor_streak_count": 0,
                        "oor_streak_direction": ALERT_DIRECTION_NONE,
                        "stuck_streak_count": 0,
                    }
                )
                new_states[key] = d

            self.channel_states.replace_all(new_states)
            self.last_reload_time = datetime.now()
            logger.info("Loaded %d threshold rules into memory.", len(new_states))
            return len(new_states)

    @staticmethod
    def _hz_from_interval(interval_ms: Optional[int]) -> str:
        if not interval_ms or interval_ms <= 0:
            return "1"
        if interval_ms >= 60000:
            return "1/60"
        if interval_ms >= 1000:
            return str(round(1000 / interval_ms, 2)).rstrip("0").rstrip(".")
        return str(int(1000 / interval_ms))

    @staticmethod
    def _parse_frequency_hz(value: Any) -> float:
        """Parse ``t_sensor.frequency`` values such as ``100`` or ``1/60``."""
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

    @classmethod
    def _offline_timeout_ms(cls, rule: Dict[str, Any]) -> float:
        interval_ms = float(
            rule.get("sample_interval_ms")
            or 1000.0 / cls._parse_frequency_hz(rule.get("frequency"))
        )
        return max(1, int(rule.get("offline_points") or 5)) * interval_ms

    @staticmethod
    def _infer_remove_dc_offset(sensor_no: str, sensor_name: str, channel: str) -> bool:
        channel_name = (channel or "").strip().lower()
        sensor_text = f"{sensor_no} {sensor_name}".lower()
        return (
            channel_name in {"x", "y", "z"}
            and ("accl" in sensor_text or "加速度" in sensor_text or "振动" in sensor_text)
        )

    def _evaluate_threshold_streak(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        dc_offset: float,
        previous_direction: Any,
        previous_count: Any,
        target_points: int,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[str, int, bool, Optional[Tuple[int, float, float, datetime]]]:
        direction = (
            str(previous_direction)
            if previous_direction in (ALERT_DIRECTION_UPPER, ALERT_DIRECTION_LOWER)
            else ALERT_DIRECTION_NONE
        )
        count = max(0, int(previous_count or 0))
        active = False
        snapshot: Optional[Tuple[int, float, float, datetime]] = None
        run_best_level: Optional[int] = None
        run_best_value: Optional[float] = None
        run_best_limit: Optional[float] = None
        run_best_time: Optional[datetime] = None
        last_sample_time = self._parse_maybe_dt(previous_sample_time)

        for sample_time, value in samples:
            if (
                max_gap_ms is not None
                and last_sample_time is not None
                and (sample_time - last_sample_time).total_seconds() * 1000.0
                > max_gap_ms
            ):
                direction = ALERT_DIRECTION_NONE
                count = 0
                run_best_level = None
                run_best_value = None
                run_best_limit = None
                run_best_time = None
            last_sample_time = sample_time
            level, sample_direction, eval_value, limit = self._sample_level(
                rule,
                value,
                dc_offset,
            )
            if level is None or sample_direction == ALERT_DIRECTION_NONE:
                direction = ALERT_DIRECTION_NONE
                count = 0
                run_best_level = None
                run_best_value = None
                run_best_limit = None
                run_best_time = None
                continue
            if sample_direction == direction:
                count += 1
            else:
                direction = sample_direction
                count = 1
                run_best_level = None
                run_best_value = None
                run_best_limit = None
                run_best_time = None
            if run_best_level is None or level < run_best_level:
                run_best_level = level
                run_best_value = eval_value
                run_best_limit = float(limit or eval_value)
                run_best_time = sample_time
            if count >= target_points:
                active = True
                snapshot = (
                    run_best_level or level,
                    run_best_value if run_best_value is not None else eval_value,
                    run_best_limit if run_best_limit is not None else eval_value,
                    run_best_time or sample_time,
                )

        return direction, count, active, snapshot

    @staticmethod
    def _out_of_range_direction(
        rule: Dict[str, Any],
        value: float,
    ) -> str:
        physical_max = rule.get("physical_max")
        physical_min = rule.get("physical_min")
        upper_exceeded = physical_max is not None and value > float(physical_max)
        lower_exceeded = physical_min is not None and value < float(physical_min)
        if upper_exceeded and lower_exceeded:
            return (
                ALERT_DIRECTION_LOWER
                if float(physical_min) - value > value - float(physical_max)
                else ALERT_DIRECTION_UPPER
            )
        if lower_exceeded:
            return ALERT_DIRECTION_LOWER
        if upper_exceeded:
            return ALERT_DIRECTION_UPPER
        return ALERT_DIRECTION_NONE

    def _evaluate_out_of_range_streak(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        previous_direction: Any,
        previous_count: Any,
        target_points: int,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[str, int, bool, Optional[Tuple[float, datetime]]]:
        direction = (
            str(previous_direction)
            if previous_direction in (ALERT_DIRECTION_UPPER, ALERT_DIRECTION_LOWER)
            else ALERT_DIRECTION_NONE
        )
        count = max(0, int(previous_count or 0))
        active = False
        snapshot: Optional[Tuple[float, datetime]] = None
        last_sample_time = self._parse_maybe_dt(previous_sample_time)

        for sample_time, value in samples:
            if (
                max_gap_ms is not None
                and last_sample_time is not None
                and (sample_time - last_sample_time).total_seconds() * 1000.0
                > max_gap_ms
            ):
                direction = ALERT_DIRECTION_NONE
                count = 0
            last_sample_time = sample_time
            sample_direction = self._out_of_range_direction(rule, value)
            if sample_direction == ALERT_DIRECTION_NONE:
                direction = ALERT_DIRECTION_NONE
                count = 0
                continue
            if sample_direction == direction:
                count += 1
            else:
                direction = sample_direction
                count = 1
            if count >= target_points:
                active = True
                snapshot = (value, sample_time)

        return direction, count, active, snapshot

    @staticmethod
    def _evaluate_stuck_streak(
        samples: List[Tuple[datetime, float]],
        previous_value: Any,
        previous_count: Any,
        previous_since: Any,
        target_points: int,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[int, Optional[float], Optional[datetime], bool, Optional[Tuple[float, datetime]]]:
        count = max(0, int(previous_count or 0))
        last_value = (
            float(previous_value) if previous_value is not None else None
        )
        since = AlertAnalyticsEngine._parse_maybe_dt(previous_since)
        active = False
        snapshot: Optional[Tuple[float, datetime]] = None
        last_sample_time = AlertAnalyticsEngine._parse_maybe_dt(previous_sample_time)

        for sample_time, value in samples:
            if (
                max_gap_ms is not None
                and last_sample_time is not None
                and (sample_time - last_sample_time).total_seconds() * 1000.0
                > max_gap_ms
            ):
                count = 0
                last_value = None
                since = None
            last_sample_time = sample_time
            if last_value is not None and abs(value - last_value) <= 1e-9:
                count += 1
            else:
                count = 1
                since = sample_time
            last_value = value
            if count >= target_points:
                active = True
                snapshot = (value, sample_time)

        return count, last_value, since, active, snapshot

    @classmethod
    def _parse_physical_range(
        cls,
        range_text: Optional[str],
        unit: str,
        physical_quantity: str,
        channel: str,
    ) -> Tuple[Optional[float], Optional[float]]:
        if not range_text:
            return None, None
        text = str(range_text).strip()
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
        normalized = re.sub(r"(?<=\d)-(?=\d)", "~", normalized)
        segment = cls._range_segment_for_channel(
            normalized,
            physical_quantity,
            channel,
        )
        if segment:
            normalized = segment
        numbers = [float(item) for item in re.findall(r"[+-]?\d+(?:\.\d+)?", normalized)]
        if not numbers:
            return None, None

        if "±" in normalized:
            upper = abs(numbers[0])
            lower = -upper
        elif "winddirection" in (channel or "").lower() and len(numbers) >= 4:
            lower, upper = numbers[-2], numbers[-1]
        elif len(numbers) >= 2:
            lower, upper = numbers[0], numbers[1]
        else:
            return None, None

        if "g" in normalized.lower() and (
            "加速度" in (physical_quantity or "") or "m/s" in (unit or "")
        ):
            lower *= 9.80665
            upper *= 9.80665
        return round(lower, 5), round(upper, 5)

    @staticmethod
    def _range_segment_for_channel(
        range_text: str,
        physical_quantity: str,
        channel: str,
    ) -> Optional[str]:
        text = range_text.strip()
        channel_lower = (channel or "").lower()
        quantity = (physical_quantity or "").lower()
        candidates = [
            part.strip(" ,，;；")
            for part in re.split(r"[,，;；/]", text)
            if part.strip(" ,，;；")
        ]
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]

        def score(segment: str) -> int:
            lower = segment.lower()
            value = 0
            if "温度" in segment or "temperature" in lower:
                value += 100 if "温度" in quantity or "temperature" in quantity or channel_lower == "temperature" else -20
            if "湿度" in segment or "humidity" in lower:
                value += 100 if "湿度" in quantity or "humidity" in quantity or channel_lower == "humidity" else -20
            if "风速" in segment or "windspeed" in lower:
                value += 100 if "风速" in quantity or "windspeed" in quantity or channel_lower == "windspeed" else -20
            if "风向" in segment or "winddirection" in lower:
                value += 100 if "风向" in quantity or "winddirection" in quantity or channel_lower == "winddirection" else -20
            if "降雨" in segment or "rainfall" in lower:
                value += 100 if "降雨" in quantity or "rainfall" in quantity or channel_lower == "rainfall" else -20

            if "%rh" in lower or "%" in lower:
                value += 40 if "湿度" in quantity or "humidity" in quantity or channel_lower == "humidity" else -40
            if "℃" in segment or "°c" in lower:
                value += 40 if "温度" in quantity or "temperature" in quantity or channel_lower == "temperature" else -40
            if "mm/h" in lower:
                value += 40 if "降雨" in quantity or "rainfall" in quantity or channel_lower == "rainfall" else -40
            if "m/s" in lower:
                value += 40 if "风速" in quantity or "windspeed" in quantity or channel_lower == "windspeed" else -20
            if "°" in segment:
                value += 40 if "风向" in quantity or "winddirection" in quantity or channel_lower == "winddirection" else -10
            return value

        best = max(candidates, key=score)
        return best if score(best) > 0 else None

    @staticmethod
    def _infer_unit(pq: str, ch: str) -> str:
        text = f"{pq} {ch}".lower()
        if "应力" in text or "stress" in text:
            return "MPa"
        if "应变" in text or "strain" in text:
            return "με"
        if "加速度" in text or "accl" in text:
            return "m/s²"
        if "温度" in text or "temp" in text:
            return "℃"
        if "湿度" in text or "humid" in text:
            return "%RH"
        if "风速" in text or "windspeed" in text:
            return "m/s"
        if "风向" in text or "winddir" in text:
            return "°"
        if "风压" in text or "气压" in text or "pressure" in text:
            return "kPa"
        if "倾角" in text:
            return "°"
        if "水位" in text or "位移" in text or "沉降" in text or "挠度" in text:
            return "mm"
        return ""

    def enrich_alert_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Enrich an alert record dict with sensor, station, unit, and severity labels."""
        d = serialize_row(row)
        sno = d.get("sensor_no") or ""
        sno_l = sno.strip().lower()
        ch = (d.get("channel") or "").strip()
        key = self.make_key(sno_l, ch)
        rule = self.channel_states.get(key) or {}
        sm = self.sensor_meta.get(sno_l) or {}

        if ch == "__DAQ__" or d.get("alert_type") == ALERT_TYPE_DAQ_OFFLINE:
            inst = self.instrument_meta.get(sno) or {}
            sensor_name = inst.get("name") or f"采集仪({sno[:8]})"
            station_name = inst.get("station_name") or "玉环站"
            physical_quantity = "采集仪连通状态"
            unit = "ms"
            position = inst.get("box_name") or "机柜层"
        else:
            sensor_name = d.get("sensor_name") or rule.get("sensor_name") or sm.get("sensor_name") or sno
            station_name = d.get("station_name") or rule.get("station_name") or sm.get("station_name") or ("玉环站" if "_YH" in str(sensor_name) else "淮北西站")
            physical_quantity = d.get("physical_quantity") or rule.get("physical_quantity") or ch or "监测物理量"
            unit = d.get("unit") or rule.get("unit") or self._infer_unit(physical_quantity, ch)
            position = sm.get("position") or rule.get("position") or "-"

        lvl = int(d.get("alert_level") or 3)
        level_map = {
            1: {"label": "一级红色报警", "tag": "L1", "color": "#ef4444", "severity": 3},
            2: {"label": "二级橙色报警", "tag": "L2", "color": "#f97316", "severity": 2},
            3: {"label": "三级黄色预警", "tag": "L3", "color": "#eab308", "severity": 1},
        }
        lvl_info = level_map.get(lvl, level_map[3])

        status_code = int(d.get("alert_status") if d.get("alert_status") is not None else 0)
        status_map = {
            0: "未处理",
            1: "已处理",
        }
        category = d.get("alert_category")
        if category is None:
            category = alert_category_for_type(d.get("alert_type"))
        category = int(category)

        d.update(
            {
                "sensor_name": sensor_name,
                "station_name": station_name,
                "physical_quantity": physical_quantity,
                "unit": unit,
                "position": position,
                "level_label": lvl_info["label"],
                "level_tag": lvl_info["tag"],
                "level_color": lvl_info["color"],
                "severity": lvl_info["severity"],
                "status_label": status_map.get(status_code, "未知"),
                "alert_category": category,
                "alert_category_label": ALERT_CATEGORY_LABELS.get(category, "未知"),
            }
        )
        return d

    def _upsert_alert_incident(
        self,
        cur: Any,
        *,
        sensor_no: str,
        channel: str,
        threshold_id: Optional[str],
        alert_type: str,
        alert_level: int,
        trigger_val: Optional[float],
        fire_time: datetime,
    ) -> Dict[str, Any]:
        """Insert or refresh an unhandled alert."""
        alert_type = normalize_alert_type(alert_type) or alert_type
        alert_category = alert_category_for_type(alert_type)
        alert_id = uuid.uuid4().hex
        cur.execute(
            """
            SELECT id
            FROM public.t_alert_record
            WHERE LOWER(sensor_no) = %s
              AND COALESCE(channel, '') = %s
              AND alert_type = %s
              AND alert_status = 0
            ORDER BY seq DESC
            LIMIT 1
            FOR UPDATE
            """,
            (sensor_no.lower(), channel or "", alert_type),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE public.t_alert_record
                SET seq = nextval('public.seq_alert_record'),
                    alert_category = %s,
                    threshold_id = COALESCE(%s, threshold_id),
                    alert_level = LEAST(alert_level, %s),
                    trigger_val = %s,
                    trigger_count = trigger_count + 1,
                    update_time = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (alert_category, threshold_id, alert_level, trigger_val, existing["id"]),
            )
        else:
            cur.execute(
                """
                INSERT INTO public.t_alert_record (
                    id, seq, sensor_no, channel, threshold_id,
                    alert_type, alert_category, alert_level,
                    trigger_val, create_time, trigger_count,
                    alert_status, update_time
                ) VALUES (
                    %s, nextval('public.seq_alert_record'), %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, 1,
                    0, NOW()
                )
                RETURNING *
                """,
                (
                    alert_id,
                    sensor_no.lower(),
                    channel,
                    threshold_id,
                    alert_type,
                    alert_category,
                    alert_level,
                    trigger_val,
                    fire_time,
                ),
            )
        row = cur.fetchone()
        enriched = self.enrich_alert_row(row)
        return enriched

    @staticmethod
    def _parse_maybe_dt(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00").replace("T", " "))
            except ValueError:
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(text, fmt)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _alert_key(
        sensor_no: str,
        channel: Optional[str],
        alert_type: str,
    ) -> Tuple[str, str, str]:
        return (
            (sensor_no or "").strip().lower(),
            (channel or "").strip(),
            normalize_alert_type(alert_type) or (alert_type or "").strip(),
        )

    def _level_for(
        self,
        rule: Dict[str, Any],
        eval_upper: float,
        eval_lower: float,
        remove_dc: bool = False,
    ) -> Tuple[Optional[int], str, Optional[float], Optional[float]]:
        for level in (1, 2, 3):
            upper = rule.get(f"level{level}_upper")
            if upper is not None and eval_upper > float(upper):
                return level, ALERT_DIRECTION_UPPER, eval_upper, float(upper)
            if remove_dc:
                continue
            lower = rule.get(f"level{level}_lower")
            if lower is not None and eval_lower < float(lower):
                return level, ALERT_DIRECTION_LOWER, eval_lower, float(lower)
        return None, ALERT_DIRECTION_NONE, None, None

    def _sample_level(
        self,
        rule: Dict[str, Any],
        value: float,
        dc_offset: float,
    ) -> Tuple[Optional[int], str, Optional[float], Optional[float]]:
        remove_dc = bool(rule.get("remove_dc_offset", False))
        if remove_dc:
            amplitude = abs(value - dc_offset)
            return self._level_for(rule, amplitude, -amplitude, remove_dc=True)
        return self._level_for(rule, value, value, remove_dc=False)

    @staticmethod
    def _fetch_samples(
        cur: Any,
        sensor_no: str,
        channel: Optional[str],
        window_start: datetime,
        window_end: datetime,
        limit: int = 20000,
    ) -> List[Tuple[datetime, float]]:
        cur.execute(
            """
            SELECT time, value
            FROM public.t_monitor
            WHERE LOWER(sensor_no) = %s
              AND COALESCE(channel, '') = %s
              AND time >= %s AND time < %s
            ORDER BY time DESC
            LIMIT %s
            """,
            (sensor_no, channel or "", window_start, window_end, limit),
        )
        rows = list(reversed(cur.fetchall()))
        return [(r["time"], float(r["value"])) for r in rows if r["value"] is not None]

    def _load_active_alerts(self, cur: Any) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        cur.execute(
            """
            SELECT *
            FROM public.t_alert_record
            WHERE alert_status = 0
            """
        )
        active: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for row in cur.fetchall():
            d = dict(row)
            active[
                self._alert_key(
                    d["sensor_no"],
                    d.get("channel"),
                    d["alert_type"],
                )
            ] = d
        return active

    def _apply_condition(
        self,
        cur: Any,
        active_map: Dict[Tuple[str, str, str], Dict[str, Any]],
        triggered: List[Dict[str, Any]],
        *,
        rule: Dict[str, Any],
        sensor_no: str,
        channel: str,
        alert_type: str,
        active: bool,
        alert_level: int,
        trigger_val: Optional[float],
        fire_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        if not active:
            return None
        key = self._alert_key(sensor_no, channel, alert_type)
        alert = self._upsert_alert_incident(
            cur,
            sensor_no=sensor_no,
            channel=channel,
            threshold_id=rule.get("id"),
            alert_type=alert_type,
            alert_level=alert_level,
            trigger_val=trigger_val,
            fire_time=fire_time,
        )
        active_map[key] = alert
        triggered.append(alert)
        return alert

    def _run_detection_window(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str,
    ) -> Dict[str, Any]:
        triggered_alerts: List[Dict[str, Any]] = []
        upserted_stats = 0
        raw_points_processed = 0

        with get_db_cursor(commit=True) as cur:
            cur.execute(WINDOW_AGG_SQL, (window_start, window_end))
            agg_rows = cur.fetchall()

            observed_map: Dict[str, Dict[str, Any]] = {}
            for row in agg_rows:
                raw_points_processed += int(row["sample_cnt"] or 0)
                observed_map[self.make_key(row["s_no"], row["ch"])] = row

            active_map = self._load_active_alerts(cur)
            rules = self.channel_states.values()
            instrument_totals = Counter(
                r.get("instrument_id")
                for r in rules
                if (r.get("instrument_id") or "").strip()
            )
            offline_by_instrument: Dict[str, List[Dict[str, Any]]] = {}

            for rule in rules:
                key = rule["key"]
                sensor_no = rule["sensor_no_lower"]
                channel = rule.get("channel") or ""
                obs = observed_map.get(key)
                offline_timeout_ms = self._offline_timeout_ms(rule)
                if obs is None or int(obs["sample_cnt"] or 0) <= 0:
                    last_seen = self._parse_maybe_dt(rule.get("last_ts"))
                    if last_seen is None:
                        silence_ms = float("inf")
                    else:
                        silence_ms = (window_end - last_seen).total_seconds() * 1000.0
                    if silence_ms < offline_timeout_ms:
                        continue

                    instrument_id = (rule.get("instrument_id") or "").strip()
                    if instrument_id:
                        offline_by_instrument.setdefault(instrument_id, []).append(rule)

                    self._apply_condition(
                        cur,
                        active_map,
                        triggered_alerts,
                        rule=rule,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_OFFLINE,
                        active=True,
                        alert_level=NON_THRESHOLD_ALERT_LEVEL,
                        trigger_val=0.0,
                        fire_time=window_end,
                    )
                    self.channel_states.update_field(
                        key,
                        online_status="OFFLINE",
                        stuck_since=None,
                        stuck_streak_count=0,
                        threshold_streak_count=0,
                        oor_streak_count=0,
                    )
                    continue

                sample_cnt = int(obs["sample_cnt"] or 0)
                first_ts = obs["first_ts"]
                last_ts = obs["last_ts"]
                val_max = float(obs["val_max"])
                val_min = float(obs["val_min"])
                val_avg = float(obs["val_avg"])
                max_time = obs.get("max_time") or last_ts
                min_time = obs.get("min_time") or first_ts
                cur.execute(
                    STAT_UPSERT_SQL,
                    (
                        sensor_no,
                        channel,
                        period_type,
                        window_start,
                        val_max,
                        val_min,
                        val_avg,
                        max_time,
                        min_time,
                        sample_cnt,
                    ),
                )
                upserted_stats += 1

                confirm_points = max(1, int(rule.get("confirm_points") or 3))
                stuck_points = max(1, int(rule.get("stuck_points") or 100))
                remove_dc = bool(rule.get("remove_dc_offset", False))
                upper_limits = [
                    float(rule[f"level{level}_upper"])
                    for level in (1, 2, 3)
                    if rule.get(f"level{level}_upper") is not None
                ]
                lower_limits = [
                    float(rule[f"level{level}_lower"])
                    for level in (1, 2, 3)
                    if rule.get(f"level{level}_lower") is not None
                ]
                upper_extreme = (
                    max(abs(val_max - val_avg), abs(val_min - val_avg))
                    if remove_dc
                    else val_max
                )
                threshold_possible = (
                    bool(upper_limits) and upper_extreme > min(upper_limits)
                ) or (
                    not remove_dc
                    and bool(lower_limits)
                    and val_min < max(lower_limits)
                )
                physical_max = rule.get("physical_max")
                physical_min = rule.get("physical_min")
                oor_possible = (
                    physical_max is not None and val_max > float(physical_max)
                ) or (
                    physical_min is not None and val_min < float(physical_min)
                )
                stuck_possible = (
                    sample_cnt >= stuck_points
                    and abs(val_max - val_min) <= 1e-9
                ) or int(rule.get("stuck_streak_count") or 0) > 0
                fetch_limit = (
                    20000
                    if threshold_possible or oor_possible or stuck_possible
                    else max(confirm_points, stuck_points)
                )
                samples = self._fetch_samples(
                    cur,
                    sensor_no,
                    channel,
                    window_start,
                    window_end,
                    limit=fetch_limit,
                )
                if not samples:
                    samples = [(last_ts, val_avg)]

                (
                    threshold_direction,
                    threshold_count,
                    threshold_active,
                    threshold_snapshot,
                ) = self._evaluate_threshold_streak(
                    samples,
                    rule,
                    val_avg,
                    rule.get("threshold_streak_direction"),
                    rule.get("threshold_streak_count"),
                    confirm_points,
                    previous_sample_time=rule.get("last_ts"),
                    max_gap_ms=float(rule.get("sample_interval_ms") or 1000) * 1.5,
                )
                state_updates: Dict[str, Any] = {
                    "last_ts": serialize_val(last_ts),
                    "last_val": round(samples[-1][1], 5),
                    "online_status": "ONLINE",
                    "threshold_streak_direction": threshold_direction,
                    "threshold_streak_count": threshold_count,
                }
                if threshold_snapshot is not None:
                    level, value, limit, fire_time = threshold_snapshot
                    state_updates.update(
                        {
                            "threshold_last_level": level,
                            "threshold_last_value": value,
                            "threshold_last_limit": limit,
                        }
                    )
                    self._apply_condition(
                        cur,
                        active_map,
                        triggered_alerts,
                        rule=rule,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_THRESHOLD,
                        active=threshold_active,
                        alert_level=level,
                        trigger_val=value,
                        fire_time=fire_time,
                    )

                (
                    oor_direction,
                    oor_count,
                    oor_active,
                    oor_snapshot,
                ) = self._evaluate_out_of_range_streak(
                    samples,
                    rule,
                    rule.get("oor_streak_direction"),
                    rule.get("oor_streak_count"),
                    confirm_points,
                    previous_sample_time=rule.get("last_ts"),
                    max_gap_ms=float(rule.get("sample_interval_ms") or 1000) * 1.5,
                )
                state_updates.update(
                    {
                        "oor_streak_direction": oor_direction,
                        "oor_streak_count": oor_count,
                    }
                )
                if oor_snapshot is not None:
                    oor_value, fire_time = oor_snapshot
                    state_updates["oor_last_value"] = oor_value
                    self._apply_condition(
                        cur,
                        active_map,
                        triggered_alerts,
                        rule=rule,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_OOR,
                        active=oor_active,
                        alert_level=NON_THRESHOLD_ALERT_LEVEL,
                        trigger_val=oor_value,
                        fire_time=fire_time,
                    )

                (
                    stuck_count,
                    stuck_last_value,
                    stuck_since,
                    stuck_active,
                    stuck_snapshot,
                ) = self._evaluate_stuck_streak(
                    samples,
                    rule.get("stuck_last_value"),
                    rule.get("stuck_streak_count"),
                    rule.get("stuck_since"),
                    stuck_points,
                    previous_sample_time=rule.get("last_ts"),
                    max_gap_ms=float(rule.get("sample_interval_ms") or 1000) * 1.5,
                )
                state_updates.update(
                    {
                        "stuck_streak_count": stuck_count,
                        "stuck_last_value": stuck_last_value,
                        "stuck_since": serialize_val(stuck_since) if stuck_since else None,
                    }
                )
                if stuck_snapshot is not None:
                    stuck_value, fire_time = stuck_snapshot
                    self._apply_condition(
                        cur,
                        active_map,
                        triggered_alerts,
                        rule=rule,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_STUCK,
                        active=stuck_active,
                        alert_level=NON_THRESHOLD_ALERT_LEVEL,
                        trigger_val=stuck_value,
                        fire_time=fire_time,
                    )

                last_sample_time = samples[-1][0]
                silence_ms = (window_end - last_sample_time).total_seconds() * 1000.0
                previous_last_seen = self._parse_maybe_dt(rule.get("last_ts"))
                boundary_gap_ms = (
                    (samples[0][0] - previous_last_seen).total_seconds() * 1000.0
                    if previous_last_seen is not None
                    else 0.0
                )
                largest_gap_ms = max(
                    boundary_gap_ms,
                    float(obs.get("max_gap_ms") or 0.0),
                )
                currently_offline = silence_ms >= offline_timeout_ms
                if currently_offline or largest_gap_ms >= offline_timeout_ms:
                    if currently_offline:
                        instrument_id = (rule.get("instrument_id") or "").strip()
                        if instrument_id:
                            offline_by_instrument.setdefault(
                                instrument_id,
                                [],
                            ).append(rule)
                    self._apply_condition(
                        cur,
                        active_map,
                        triggered_alerts,
                        rule=rule,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_OFFLINE,
                        active=True,
                        alert_level=NON_THRESHOLD_ALERT_LEVEL,
                        trigger_val=0.0,
                        fire_time=window_end,
                    )
                if currently_offline:
                    state_updates.update(
                        {
                            "online_status": "OFFLINE",
                            "stuck_since": None,
                            "stuck_streak_count": 0,
                            "threshold_streak_count": 0,
                            "threshold_streak_direction": ALERT_DIRECTION_NONE,
                            "oor_streak_count": 0,
                            "oor_streak_direction": ALERT_DIRECTION_NONE,
                        }
                    )

                self.channel_states.update_field(
                    key,
                    **state_updates,
                )

            active_daq_instruments = {
                key[0]
                for key in active_map
                if key[2] == ALERT_TYPE_DAQ_OFFLINE
            }
            for instrument_id in sorted(set(offline_by_instrument) | active_daq_instruments):
                offline_rules = offline_by_instrument.get(instrument_id, [])
                total_channels = int(instrument_totals.get(instrument_id, 0))
                required_offline = max(2, int(math.ceil(total_channels * 0.5)))
                if total_channels <= 0 or len(offline_rules) < required_offline:
                    continue

                daq_alert = self._upsert_alert_incident(
                    cur,
                    sensor_no=instrument_id,
                    channel="__DAQ__",
                    threshold_id=None,
                    alert_type=ALERT_TYPE_DAQ_OFFLINE,
                    alert_level=NON_THRESHOLD_ALERT_LEVEL,
                    trigger_val=0.0,
                    fire_time=window_end,
                )
                active_map[
                    self._alert_key(
                        instrument_id,
                        "__DAQ__",
                        ALERT_TYPE_DAQ_OFFLINE,
                    )
                ] = daq_alert
                triggered_alerts.append(daq_alert)

        return {
            "windowStart": serialize_val(window_start),
            "windowEnd": serialize_val(window_end),
            "periodType": period_type,
            "rawPointsProcessed": raw_points_processed,
            "upsertedStatsCount": upserted_stats,
            "triggeredAlertsCount": len(triggered_alerts),
            "triggeredAlerts": triggered_alerts[:10],
        }

    def step_detection(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str = "1m",
    ) -> Dict[str, Any]:
        """Persist one average window and evaluate frequency-based sample streaks."""
        with self._step_lock:
            t0 = time.perf_counter()
            if window_end <= window_start:
                raise ValueError("window_end must be later than window_start")

            result = self._run_detection_window(
                window_start,
                window_end,
                period_type,
            )
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            return {
                "windowStart": serialize_val(window_start),
                "windowEnd": serialize_val(window_end),
                "periodType": period_type,
                "subWindowCount": 1,
                "rawPointsProcessed": result["rawPointsProcessed"],
                "upsertedStatsCount": result["upsertedStatsCount"],
                "triggeredAlertsCount": result["triggeredAlertsCount"],
                "triggeredAlerts": result["triggeredAlerts"],
                "elapsedMs": elapsed_ms,
            }

    def get_status(self) -> Dict[str, Any]:
        rules = self.channel_states.values()
        online_cnt = sum(1 for r in rules if r.get("online_status") == "ONLINE")
        offline_cnt = len(rules) - online_cnt
        return {
            "loadedRulesCount": len(rules),
            "onlineChannels": online_cnt,
            "offlineChannels": offline_cnt,
            "totalMonitorRows": self.total_monitor_rows,
            "lastReloadTime": serialize_val(self.last_reload_time),
            "detectionMode": "frequency_sample_groups",
            "confirmPoints": self.confirm_points,
            "stuckPoints": self.stuck_points,
            "offlinePoints": self.offline_points,
        }

