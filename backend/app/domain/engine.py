import logging
import math
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.config import get_settings
from app.core.serialization import json_dumps, serialize_row, serialize_val
from app.domain.alert_types import (
    ALERT_DIRECTION_LOWER,
    ALERT_DIRECTION_NONE,
    ALERT_DIRECTION_UPPER,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
    normalize_alert_direction,
    normalize_alert_type,
)
from app.infrastructure.database import get_db_cursor

logger = logging.getLogger("huaibeixi.engine")

DIRECTION_SECTORS_16 = [
    "N", "NNE", "NE", "ENE",
    "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW",
    "W", "WNW", "NW", "NNW",
]

SPEED_BINS_7 = [
    {"index": 0, "label": "0.2~1.5 m/s (1级软风)", "min": 0.2, "max": 1.5},
    {"index": 1, "label": "1.6~3.3 m/s (2级轻风)", "min": 1.5, "max": 3.3},
    {"index": 2, "label": "3.4~5.4 m/s (3级微风)", "min": 3.3, "max": 5.4},
    {"index": 3, "label": "5.5~7.9 m/s (4级和风)", "min": 5.4, "max": 7.9},
    {"index": 4, "label": "8.0~10.7 m/s (5级清劲风)", "min": 7.9, "max": 10.7},
    {"index": 5, "label": "10.8~13.8 m/s (6级强风)", "min": 10.7, "max": 13.8},
    {"index": 6, "label": "≥13.9 m/s (7级及以上疾风/台风)", "min": 13.8, "max": 999.0},
]

# Single-pass push-down aggregation over one t_monitor window.
#   s_no / ch            -> grouping key (sensor_no normalized to lower-case)
#   max_time / min_time  -> true timestamp of the extreme sample (not a window boundary)
WINDOW_AGG_SQL = """
SELECT
    LOWER(sensor_no) AS s_no,
    COALESCE(channel, '') AS ch,
    COUNT(*) AS sample_cnt,
    MAX(value) AS val_max,
    MIN(value) AS val_min,
    AVG(value) AS val_avg,
    COALESCE(STDDEV_POP(value), 0) AS val_std,
    MIN(time) AS first_ts,
    MAX(time) AS last_ts,
    (ARRAY_AGG(time ORDER BY value ASC, time ASC))[1] AS min_time,
    (ARRAY_AGG(time ORDER BY value DESC, time ASC))[1] AS max_time
FROM public.t_monitor
WHERE time >= %s AND time < %s
GROUP BY LOWER(sensor_no), COALESCE(channel, '')
"""

STAT_UPSERT_SQL = """
INSERT INTO public.t_stat_record (
    sensor_no, channel, period_type, stat_time,
    val_max, val_min, val_avg, val_std,
    max_time, min_time, sample_count,
    create_by, create_time
) VALUES (
    %s, %s, '1m', %s,
    %s, %s, %s, %s,
    %s, %s, %s,
    'system', NOW()
)
ON CONFLICT (stat_time, sensor_no, channel, period_type)
DO UPDATE SET
    val_max = EXCLUDED.val_max,
    val_min = EXCLUDED.val_min,
    val_avg = EXCLUDED.val_avg,
    val_std = EXCLUDED.val_std,
    max_time = EXCLUDED.max_time,
    min_time = EXCLUDED.min_time,
    sample_count = EXCLUDED.sample_count
"""

WIND_AGG_SQL = """
SELECT
    COUNT(*) AS pair_cnt,
    COALESCE(SUM(v * SIN(RADIANS(d))), 0) AS sum_sin,
    COALESCE(SUM(v * COS(RADIANS(d))), 0) AS sum_cos,
    COALESCE(AVG(v), 0) AS scalar_avg,
    COALESCE(MIN(v), 0) AS scalar_min,
    COALESCE(MAX(v), 0) AS scalar_max,
    COALESCE(STDDEV_POP(v), 0) AS scalar_std,
    (ARRAY_AGG(bucket ORDER BY v ASC, bucket ASC))[1] AS min_time,
    (ARRAY_AGG(bucket ORDER BY v DESC, bucket ASC))[1] AS max_time
FROM (
    SELECT
        date_trunc('second', time) AS bucket,
        MAX(CASE WHEN channel = 'windSpeed' THEN value END) AS v,
        MAX(CASE WHEN channel = 'windDirection' THEN value END) AS d
    FROM public.t_monitor
    WHERE LOWER(sensor_no) = %s
      AND channel IN ('windSpeed', 'windDirection')
      AND time >= %s AND time < %s
    GROUP BY date_trunc('second', time)
) q
WHERE v IS NOT NULL AND d IS NOT NULL
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
                        "exceed_streak",
                        "active_level",
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
    - Supports Dual-Mode Clock (LIVE vs REPLAY) and push-down SQL evaluation over `t_monitor`.
    - Evaluates Req 1 (1000ms offline & gap + DAQ storm suppression),
      Req 2 (3-level thresholds + debounce + dynamic incident upgrade + manual handling),
      Req 3 (传感器死值、超物理量程),
      Req 4 (1m extremes & 10m vector wind rose synthesis).
    """

    def __init__(self) -> None:
        self.channel_states = ConcurrentDict()
        self.sensor_meta: Dict[str, Dict[str, Any]] = {}
        self.instrument_meta: Dict[str, Dict[str, Any]] = {}
        self.mode: str = "REPLAY"
        settings = get_settings()
        self.current_watermark: datetime = datetime.strptime(
            settings.replay_start_watermark,
            "%Y-%m-%d %H:%M:%S",
        )
        self.latest_data_ts: datetime = datetime.strptime(
            settings.replay_fallback_end,
            "%Y-%m-%d %H:%M:%S",
        )
        self.speed_multiplier: int = 1
        self.is_running: bool = False
        self.step_count: int = 0
        self.last_step_ms: float = 0.0
        self.total_monitor_rows: int = 398059510
        self.last_reload_time: Optional[datetime] = None
        self._sse_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._demo_counter: int = 0
        self._step_lock = threading.Lock()

    def register_sse_callback(self, cb: Callable[[Dict[str, Any]], None]) -> None:
        self._sse_callbacks.append(cb)

    def _notify_sse(self, event_payload: Dict[str, Any]) -> None:
        for cb in list(self._sse_callbacks):
            try:
                cb(event_payload)
            except Exception as exc:
                logger.warning("SSE callback error: %s", exc)

    @staticmethod
    def make_key(sensor_no: str, channel: Optional[str]) -> str:
        return f"{(sensor_no or '').strip().lower()}:{(channel or '').strip()}"

    def initialize(self) -> None:
        """Load sensor metadata, instrument metadata, and 208 threshold rules into memory."""
        self.reload_thresholds()
        try:
            with get_db_cursor() as cur:
                cur.execute("SELECT MAX(stat_time) AS max_st FROM t_stat_record WHERE period_type = '1m'")
                row = cur.fetchone()
                if row and row["max_st"]:
                    self.latest_data_ts = row["max_st"] + timedelta(seconds=20)
        except Exception as exc:
            logger.warning("Could not query latest_data_ts: %s", exc)
        self.refresh_monitor_row_count()

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
                        "frequency": sm.get("frequency")
                        or self._hz_from_interval(d.get("expected_interval_ms")),
                        "position": sm.get("position") or "-",
                        "physical_quantity": physical_quantity,
                        "unit": unit,
                        "remove_dc_offset": remove_dc_offset,
                        "physical_min": range_min,
                        "physical_max": range_max,
                        "stuck_time_ms": int(
                            d.get("stuck_time_ms")
                            or self._default_stuck_time_ms(
                                d.get("expected_interval_ms"),
                                remove_dc_offset,
                                physical_quantity,
                            )
                        ),
                        "online_status": "ONLINE",
                        "exceed_streak": 0,
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
    def _infer_remove_dc_offset(sensor_no: str, sensor_name: str, channel: str) -> bool:
        channel_name = (channel or "").strip().lower()
        sensor_text = f"{sensor_no} {sensor_name}".lower()
        return (
            channel_name in {"x", "y", "z"}
            and ("accl" in sensor_text or "加速度" in sensor_text or "振动" in sensor_text)
        )

    @staticmethod
    def _default_stuck_time_ms(
        expected_interval_ms: Optional[int],
        remove_dc_offset: bool,
        physical_quantity: str,
    ) -> int:
        interval_ms = max(10, int(expected_interval_ms or 1000))
        if remove_dc_offset:
            return interval_ms * 50
        quantity = (physical_quantity or "").lower()
        if "温度" in quantity or "temperature" in quantity:
            return interval_ms * 60
        if "风向" in quantity or "winddirection" in quantity:
            return interval_ms * 120
        return interval_ms * 30

    def _evaluate_stuck(
        self,
        *,
        previous_last_val: Any,
        previous_stuck_since: Any,
        sample_cnt: int,
        val_std: float,
        val_avg: float,
        first_ts: datetime,
        last_ts: datetime,
        stuck_time_ms: int,
    ) -> Tuple[bool, Optional[datetime], float]:
        if sample_cnt <= 0 or val_std > 1e-9:
            return False, None, 0.0

        previous_value = (
            float(previous_last_val)
            if previous_last_val is not None
            else None
        )
        continues_previous = (
            previous_value is not None
            and abs(val_avg - previous_value) <= 1e-9
        )
        stuck_since = self._parse_maybe_dt(previous_stuck_since) if continues_previous else None
        stuck_since = stuck_since or first_ts
        stuck_duration_ms = max(
            0.0,
            (last_ts - stuck_since).total_seconds() * 1000.0,
        )
        return stuck_duration_ms >= stuck_time_ms, stuck_since, stuck_duration_ms

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
        direction: str,
        trigger_val: Optional[float],
        fire_time: datetime,
        remark: str,
    ) -> Dict[str, Any]:
        """Insert or refresh an unhandled alert."""
        alert_type = normalize_alert_type(alert_type) or alert_type
        direction = normalize_alert_direction(direction)
        alert_id = uuid.uuid4().hex
        cur.execute(
            """
            SELECT id
            FROM public.t_alert_record
            WHERE LOWER(sensor_no) = %s
              AND COALESCE(channel, '') = %s
              AND alert_type = %s
              AND COALESCE(direction, '无方向') = %s
              AND alert_status IN (0, 1)
            ORDER BY seq DESC
            LIMIT 1
            FOR UPDATE
            """,
            (sensor_no.lower(), channel or "", alert_type, direction),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                """
                UPDATE public.t_alert_record
                SET seq = nextval('public.seq_alert_record'),
                    threshold_id = COALESCE(%s, threshold_id),
                    alert_level = LEAST(alert_level, %s),
                    trigger_val = %s,
                    trigger_count = trigger_count + 1
                WHERE id = %s
                RETURNING *
                """,
                (threshold_id, alert_level, trigger_val, existing["id"]),
            )
        else:
            cur.execute(
                """
                INSERT INTO public.t_alert_record (
                    id, seq, sensor_no, channel, threshold_id,
                    alert_type, alert_level, direction,
                    trigger_val, create_time, trigger_count,
                    alert_status
                ) VALUES (
                    %s, nextval('public.seq_alert_record'), %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, 1,
                    0
                )
                RETURNING *
                """,
                (
                    alert_id,
                    sensor_no.lower(),
                    channel,
                    threshold_id,
                    alert_type,
                    alert_level,
                    direction,
                    trigger_val,
                    fire_time,
                ),
            )
        row = cur.fetchone()
        enriched = self.enrich_alert_row(row)
        self._notify_sse({"type": "ALERT_UPSERT", "alert": enriched})
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
        direction: Optional[str] = ALERT_DIRECTION_NONE,
    ) -> Tuple[str, str, str, str]:
        return (
            (sensor_no or "").strip().lower(),
            (channel or "").strip(),
            normalize_alert_type(alert_type) or (alert_type or "").strip(),
            normalize_alert_direction(direction),
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

    def _count_exceed_run(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        dc_offset: float,
        direction: str,
    ) -> Tuple[int, Optional[int], Optional[float], Optional[float]]:
        count = 0
        hit_level: Optional[int] = None
        hit_value: Optional[float] = None
        hit_limit: Optional[float] = None
        for _, value in reversed(samples):
            level, sample_direction, eval_value, limit = self._sample_level(rule, value, dc_offset)
            if level is None or sample_direction != direction:
                break
            count += 1
            if hit_level is None:
                hit_level = level
                hit_value = eval_value
                hit_limit = limit
        return count, hit_level, hit_value, hit_limit

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

    def _load_active_alerts(self, cur: Any) -> Dict[Tuple[str, str, str, str], Dict[str, Any]]:
        cur.execute(
            """
            SELECT *
            FROM public.t_alert_record
            WHERE alert_status IN (0, 1)
            """
        )
        active: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        for row in cur.fetchall():
            d = dict(row)
            if int(d.get("alert_status") or 0) == 0:
                active[
                    self._alert_key(
                        d["sensor_no"],
                        d.get("channel"),
                        d["alert_type"],
                        d.get("direction"),
                    )
                ] = d
        return active

    def _apply_condition(
        self,
        cur: Any,
        active_map: Dict[Tuple[str, str, str, str], Dict[str, Any]],
        triggered: List[Dict[str, Any]],
        *,
        rule: Dict[str, Any],
        sensor_no: str,
        channel: str,
        alert_type: str,
        active: bool,
        alert_level: int,
        direction: str,
        trigger_val: Optional[float],
        fire_time: datetime,
        remark: str,
    ) -> Optional[Dict[str, Any]]:
        if not active:
            return None
        key = self._alert_key(sensor_no, channel, alert_type, direction)
        alert = self._upsert_alert_incident(
            cur,
            sensor_no=sensor_no,
            channel=channel,
            threshold_id=rule.get("id"),
            alert_type=alert_type,
            alert_level=alert_level,
            direction=direction,
            trigger_val=trigger_val,
            fire_time=fire_time,
            remark=remark,
        )
        active_map[key] = alert
        triggered.append(alert)
        return alert

    def _upsert_wind_rose(
        self,
        cur: Any,
        sensor_no: str,
        window_start: datetime,
        window_end: datetime,
    ) -> bool:
        cur.execute(WIND_AGG_SQL, (sensor_no.lower(), window_start, window_end))
        row = cur.fetchone()
        if not row or int(row["pair_cnt"] or 0) <= 0:
            return False

        pair_cnt = int(row["pair_cnt"])
        sum_sin = float(row["sum_sin"] or 0.0)
        sum_cos = float(row["sum_cos"] or 0.0)
        vector_speed = math.sqrt(sum_sin * sum_sin + sum_cos * sum_cos) / max(1, pair_cnt)
        vector_direction = math.degrees(math.atan2(sum_sin, sum_cos)) % 360.0
        scalar_speed = float(row["scalar_avg"] or 0.0)
        sector = 0
        if vector_speed > 0.2:
            sector = int(math.floor(((vector_direction + 11.25) % 360.0) / 22.5)) % 16 + 1

        beaufort_level = 0
        for idx, speed_bin in enumerate(SPEED_BINS_7):
            if float(speed_bin["min"]) <= vector_speed < float(speed_bin["max"]):
                beaufort_level = idx + 1
                break
        if vector_speed >= float(SPEED_BINS_7[-1]["min"]):
            beaufort_level = 7

        ext_json = {
            "vector_speed": round(vector_speed, 5),
            "vector_direction": round(vector_direction, 5),
            "scalar_speed": round(scalar_speed, 5),
            "sector": sector,
            "direction_label": DIRECTION_SECTORS_16[(sector - 1) % 16] if sector else "CALM",
            "beaufort_level": beaufort_level,
            "calm": vector_speed <= 0.2,
            "small_wind": vector_speed <= 0.5,
            "pair_count": pair_cnt,
        }
        cur.execute(
            """
            INSERT INTO public.t_stat_record (
                sensor_no, channel, period_type, stat_time,
                val_max, val_min, val_avg, val_std,
                max_time, min_time, sample_count,
                ext_json, create_by, create_time
            ) VALUES (
                %s, 'wind_rose', '10m', %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s::jsonb, 'system', NOW()
            )
            ON CONFLICT (stat_time, sensor_no, channel, period_type)
            DO UPDATE SET
                val_max = EXCLUDED.val_max,
                val_min = EXCLUDED.val_min,
                val_avg = EXCLUDED.val_avg,
                val_std = EXCLUDED.val_std,
                max_time = EXCLUDED.max_time,
                min_time = EXCLUDED.min_time,
                sample_count = EXCLUDED.sample_count,
                ext_json = EXCLUDED.ext_json
            """,
            (
                sensor_no.lower(),
                window_start,
                float(row["scalar_max"] or 0.0),
                float(row["scalar_min"] or 0.0),
                scalar_speed,
                float(row["scalar_std"] or 0.0),
                row["max_time"],
                row["min_time"],
                pair_cnt,
                json_dumps(ext_json),
            ),
        )
        return True

    def _run_detection_window(self, window_start: datetime, window_end: datetime) -> Dict[str, Any]:
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
                if obs is None or int(obs["sample_cnt"] or 0) <= 0:
                    last_seen = self._parse_maybe_dt(rule.get("last_ts"))
                    if last_seen is None:
                        self.channel_states.update_field(
                            key,
                            last_ts=serialize_val(window_end),
                            online_status="OFFLINE",
                            stuck_since=None,
                        )
                        continue

                    silence_ms = (window_end - last_seen).total_seconds() * 1000.0
                    offline_timeout_ms = int(rule.get("offline_timeout_ms") or 10000)
                    if silence_ms <= offline_timeout_ms:
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
                        alert_level=3,
                        direction=ALERT_DIRECTION_NONE,
                        trigger_val=round(silence_ms, 3),
                        fire_time=window_end,
                        remark=(
                            f"[传感器离线] {rule.get('sensor_name')} "
                            f"{channel} 已连续 {silence_ms:.0f}ms 无数据，超过离线阈值 {offline_timeout_ms}ms"
                        ),
                    )
                    self.channel_states.update_field(
                        key,
                        online_status="OFFLINE",
                        stuck_since=None,
                    )
                    continue

                sample_cnt = int(obs["sample_cnt"] or 0)
                first_ts = obs["first_ts"]
                last_ts = obs["last_ts"]
                val_max = float(obs["val_max"])
                val_min = float(obs["val_min"])
                val_avg = float(obs["val_avg"])
                val_std = float(obs["val_std"] or 0.0)
                max_time = obs.get("max_time") or last_ts
                min_time = obs.get("min_time") or first_ts
                previous_last_val = rule.get("last_val")
                previous_stuck_since = rule.get("stuck_since")
                cur.execute(
                    STAT_UPSERT_SQL,
                    (
                        sensor_no,
                        channel,
                        window_start,
                        val_max,
                        val_min,
                        val_avg,
                        val_std,
                        max_time,
                        min_time,
                        sample_cnt,
                    ),
                )
                upserted_stats += 1

                self.channel_states.update_field(
                    key,
                    last_ts=serialize_val(last_ts),
                    last_val=round(val_max, 5),
                    online_status="ONLINE",
                )

                stuck_time_ms = int(rule.get("stuck_time_ms") or 30000)
                stuck_active, stuck_since, stuck_duration_ms = self._evaluate_stuck(
                    previous_last_val=previous_last_val,
                    previous_stuck_since=previous_stuck_since,
                    sample_cnt=sample_cnt,
                    val_std=val_std,
                    val_avg=val_avg,
                    first_ts=first_ts,
                    last_ts=last_ts,
                    stuck_time_ms=stuck_time_ms,
                )
                self.channel_states.update_field(
                    key,
                    stuck_since=serialize_val(stuck_since) if stuck_since else None,
                )
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type=ALERT_TYPE_STUCK,
                    active=stuck_active,
                    alert_level=3,
                    direction=ALERT_DIRECTION_NONE,
                    trigger_val=val_avg,
                    fire_time=last_ts,
                    remark=(
                        f"[传感器死值] {rule.get('sensor_name')} {channel} "
                        f"连续 {stuck_duration_ms:.0f}ms 读数恒为 {val_avg:.5f}，"
                        f"标准差为 0，超过判定时间 {stuck_time_ms}ms"
                    ),
                )

                physical_max = rule.get("physical_max")
                physical_min = rule.get("physical_min")
                upper_exceeded = (
                    physical_max is not None
                    and val_max > float(physical_max)
                )
                lower_exceeded = (
                    physical_min is not None
                    and val_min < float(physical_min)
                )
                oor_active = upper_exceeded or lower_exceeded
                oor_direction = ALERT_DIRECTION_NONE
                oor_value = val_max
                if upper_exceeded and lower_exceeded:
                    upper_excess = val_max - float(physical_max)
                    lower_excess = float(physical_min) - val_min
                    if lower_excess > upper_excess:
                        oor_direction = ALERT_DIRECTION_LOWER
                        oor_value = val_min
                    else:
                        oor_direction = ALERT_DIRECTION_UPPER
                        oor_value = val_max
                elif lower_exceeded:
                    oor_direction = ALERT_DIRECTION_LOWER
                    oor_value = val_min
                elif upper_exceeded:
                    oor_direction = ALERT_DIRECTION_UPPER
                    oor_value = val_max
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type=ALERT_TYPE_OOR,
                    active=oor_active,
                    alert_level=3,
                    direction=oor_direction,
                    trigger_val=oor_value,
                    fire_time=last_ts,
                    remark=(
                        f"[超物理量程] {rule.get('sensor_name')} {channel} "
                        f"读数范围 [{val_min:.5f}, {val_max:.5f}] 超出 [{physical_min}, {physical_max}]"
                    ),
                )

                remove_dc = bool(rule.get("remove_dc_offset", False))
                if remove_dc:
                    aggregate_upper = max(abs(val_max - val_avg), abs(val_min - val_avg))
                    aggregate_lower = -aggregate_upper
                else:
                    aggregate_upper = val_max
                    aggregate_lower = val_min
                aggregate_level, _, _, _ = self._level_for(
                    rule,
                    aggregate_upper,
                    aggregate_lower,
                    remove_dc=remove_dc,
                )
                active_thresholds = [
                    (alert_key, active_row)
                    for alert_key, active_row in active_map.items()
                    if alert_key[0] == sensor_no
                    and alert_key[1] == channel
                    and alert_key[2] == ALERT_TYPE_THRESHOLD
                ]
                if aggregate_level is not None or active_thresholds:
                    samples = self._fetch_samples(cur, sensor_no, channel, window_start, window_end)
                    directions = (
                        [ALERT_DIRECTION_UPPER]
                        if remove_dc
                        else [ALERT_DIRECTION_UPPER, ALERT_DIRECTION_LOWER]
                    )
                    for direction in directions:
                        threshold_key = self._alert_key(sensor_no, channel, ALERT_TYPE_THRESHOLD, direction)
                        existing = active_map.get(threshold_key)
                        exceeded, hit_level, hit_value, hit_limit = self._count_exceed_run(
                            samples,
                            rule,
                            val_avg,
                            direction,
                        )
                        if existing:
                            if exceeded > 0 and hit_level is not None:
                                level_names = {1: "一级红色报警", 2: "二级橙色报警", 3: "三级黄色预警"}
                                direction_cn = "去直流幅值超限" if remove_dc else (
                                    "上限超限" if direction == ALERT_DIRECTION_UPPER else "下限超限"
                                )
                                self._apply_condition(
                                    cur,
                                    active_map,
                                    triggered_alerts,
                                    rule=rule,
                                    sensor_no=sensor_no,
                                    channel=channel,
                                    alert_type=ALERT_TYPE_THRESHOLD,
                                    active=True,
                                    alert_level=hit_level,
                                    direction=direction,
                                    trigger_val=hit_value,
                                    fire_time=last_ts,
                                    remark=(
                                        f"[{level_names[hit_level]}] {rule.get('sensor_name')} "
                                        f"{rule.get('physical_quantity')}{direction_cn}: "
                                        f"峰值 {hit_value:.4f} {rule.get('unit') or ''} "
                                        f"(阈值 {hit_limit:.4f} {rule.get('unit') or ''})"
                                    ),
                                )
                        elif exceeded > 0 and hit_level is not None:
                            confirm_points = max(1, int(rule.get("confirm_points") or 3))
                            if exceeded >= confirm_points:
                                level_names = {1: "一级红色报警", 2: "二级橙色报警", 3: "三级黄色预警"}
                                direction_cn = "去直流幅值超限" if remove_dc else (
                                    "上限超限" if direction == ALERT_DIRECTION_UPPER else "下限超限"
                                )
                                self._apply_condition(
                                    cur,
                                    active_map,
                                    triggered_alerts,
                                    rule=rule,
                                    sensor_no=sensor_no,
                                    channel=channel,
                                    alert_type=ALERT_TYPE_THRESHOLD,
                                    active=True,
                                    alert_level=hit_level,
                                    direction=direction,
                                    trigger_val=hit_value,
                                    fire_time=last_ts,
                                    remark=(
                                        f"[{level_names[hit_level]}] {rule.get('sensor_name')} "
                                        f"{rule.get('physical_quantity')}{direction_cn}: "
                                        f"连续 {exceeded} 点超限，峰值 {hit_value:.4f} "
                                        f"{rule.get('unit') or ''} (阈值 {hit_limit:.4f} {rule.get('unit') or ''})"
                                    ),
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
                    alert_level=3,
                    direction=ALERT_DIRECTION_NONE,
                    trigger_val=float(len(offline_rules)),
                    fire_time=window_end,
                    remark=(
                        f"[采集仪离线] 采集仪 {instrument_id} 下 "
                        f"{len(offline_rules)}/{total_channels} 个通道离线，达到聚合阈值 {required_offline}，"
                        f"已生成设备级主告警，子通道告警保留并等待人工处理"
                    ),
                )
                active_map[
                    self._alert_key(
                        instrument_id,
                        "__DAQ__",
                        ALERT_TYPE_DAQ_OFFLINE,
                        ALERT_DIRECTION_NONE,
                    )
                ] = daq_alert
                triggered_alerts.append(daq_alert)

            if window_start.minute % 10 == 0:
                wind_start = window_start - timedelta(minutes=10)
                for wind_sensor in ("wea-01", "fhj-01"):
                    if self._upsert_wind_rose(cur, wind_sensor, wind_start, window_start):
                        upserted_stats += 1

        self.current_watermark = window_end
        return {
            "windowStart": serialize_val(window_start),
            "windowEnd": serialize_val(window_end),
            "rawPointsProcessed": raw_points_processed,
            "upsertedStatsCount": upserted_stats,
            "triggeredAlertsCount": len(triggered_alerts),
            "triggeredAlerts": triggered_alerts[:10],
        }

    def step_detection(
        self,
        window_start: Optional[datetime] = None,
        window_end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Execute detection in one-minute sub-windows so 1m statistics and debounce
        semantics remain correct even when replay speed is above 1x.
        """
        with self._step_lock:
            t0 = time.perf_counter()
            if window_start is None:
                window_start = self.current_watermark
            if window_end is None:
                window_end = window_start + timedelta(minutes=1)
            if window_end <= window_start:
                raise ValueError("window_end must be later than window_start")

            total_raw_points = 0
            total_stats = 0
            total_alerts = 0
            first_alerts: List[Dict[str, Any]] = []
            sub_window_count = 0
            cursor = window_start
            while cursor < window_end:
                sub_end = min(cursor + timedelta(minutes=1), window_end)
                result = self._run_detection_window(cursor, sub_end)
                total_raw_points += int(result["rawPointsProcessed"])
                total_stats += int(result["upsertedStatsCount"])
                total_alerts += int(result["triggeredAlertsCount"])
                if len(first_alerts) < 10:
                    first_alerts.extend(result["triggeredAlerts"][: 10 - len(first_alerts)])
                sub_window_count += 1
                cursor = sub_end

            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            self.current_watermark = window_end
            self.step_count += 1
            self.last_step_ms = elapsed_ms
            return {
                "windowStart": serialize_val(window_start),
                "windowEnd": serialize_val(window_end),
                "currentWatermark": serialize_val(self.current_watermark),
                "subWindowCount": sub_window_count,
                "rawPointsProcessed": total_raw_points,
                "upsertedStatsCount": total_stats,
                "triggeredAlertsCount": total_alerts,
                "triggeredAlerts": first_alerts,
                "elapsedMs": elapsed_ms,
            }

    def trigger_demo_alert(
        self,
        sensor_no: Optional[str] = None,
        channel: Optional[str] = None,
        alert_level: Optional[int] = None,
        alert_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Trigger or upgrade a realistic live alert in `t_alert_record` (incrementing `seq`
        and broadcasting over SSE) so the frontend popup alarm & sound can be demonstrated
        immediately on demand.
        """
        self._demo_counter += 1
        scenarios = [
            {
                "sensor_no": "stn-12-b",
                "channel": "stress",
                "alert_type": ALERT_TYPE_THRESHOLD,
                "alert_level": 1,
                "direction": ALERT_DIRECTION_LOWER,
                "trigger_val": -11.32,
                "remark": "[一级红色报警] 光纤光栅应变计_25_YH (玉环站主桁架) 应力下限超限: 实时峰值 -11.5840 MPa < 一级下限 -11.20 MPa",
            },
            {
                "sensor_no": "accl-02",
                "channel": "y",
                "alert_type": ALERT_TYPE_THRESHOLD,
                "alert_level": 2,
                "direction": ALERT_DIRECTION_UPPER,
                "trigger_val": 1.542,
                "remark": "[二级橙色报警] 三向加速度计_2_YH y轴去直流振动幅值超限: 1.6850 m/s² > 二级上限 1.52 m/s² (DC offset=20.0014)",
            },
            {
                "sensor_no": "wprs-03",
                "channel": "",
                "alert_type": ALERT_TYPE_THRESHOLD,
                "alert_level": 1,
                "direction": ALERT_DIRECTION_UPPER,
                "trigger_val": 392.5,
                "remark": "[一级红色报警] 风压计_3_YH 屋面风压超限: 实时极值 402.80 kPa > 一级上限 390.00 kPa",
            },
            {
                "sensor_no": "wea-01",
                "channel": "temperature",
                "alert_type": ALERT_TYPE_THRESHOLD,
                "alert_level": 1,
                "direction": ALERT_DIRECTION_UPPER,
                "trigger_val": 55.6,
                "remark": "[一级红色报警] 小型气象站_YH 温度动态升级告警: 峰值 58.40 ℃ > 一级上限 55.00 ℃ (三级->二级->一级)",
            },
        ]
        chosen = dict(scenarios[(self._demo_counter - 1) % len(scenarios)])
        if sensor_no:
            chosen["sensor_no"] = sensor_no.strip().lower()
        if channel is not None:
            chosen["channel"] = channel.strip()
        if alert_level in (1, 2, 3):
            chosen["alert_level"] = alert_level
        if alert_type:
            chosen["alert_type"] = normalize_alert_type(alert_type) or alert_type
        if chosen["alert_type"] != ALERT_TYPE_THRESHOLD:
            chosen["alert_level"] = 3

        key = self.make_key(chosen["sensor_no"], chosen["channel"])
        rule = self.channel_states.get(key) or {}
        now_ts = datetime.now()

        # Make sure each demo trigger creates a fresh or upgraded top-of-stream seq event
        with get_db_cursor(commit=True) as cur:
            alert = self._upsert_alert_incident(
                cur,
                sensor_no=chosen["sensor_no"],
                channel=chosen["channel"],
                threshold_id=rule.get("id"),
                alert_type=chosen["alert_type"],
                alert_level=int(chosen["alert_level"]),
                direction=chosen["direction"],
                trigger_val=float(chosen["trigger_val"]),
                fire_time=now_ts,
                remark=chosen["remark"],
            )
        return alert

    def get_status(self) -> Dict[str, Any]:
        rules = self.channel_states.values()
        online_cnt = sum(1 for r in rules if r.get("online_status") == "ONLINE")
        offline_cnt = len(rules) - online_cnt
        return {
            "mode": self.mode,
            "isRunning": self.is_running,
            "currentWatermark": serialize_val(self.current_watermark),
            "latestDataTimestamp": serialize_val(self.latest_data_ts),
            "speedMultiplier": self.speed_multiplier,
            "stepCount": self.step_count,
            "lastStepMs": self.last_step_ms,
            "loadedRulesCount": len(rules),
            "onlineChannels": online_cnt,
            "offlineChannels": offline_cnt,
            "lastReloadTime": serialize_val(self.last_reload_time),
        }

