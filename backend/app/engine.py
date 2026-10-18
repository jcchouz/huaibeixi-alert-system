import asyncio
import logging
import math
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

import psycopg2.extras

from app.config import get_db_cursor, json_dumps, serialize_row, serialize_val

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
#   distinct_case_cnt    -> >1 means the same key was written with mixed case (DATA_CONFLICT)
#   max_time / min_time  -> true timestamp of the extreme sample (not a window boundary)
#   gap_cnt / max_gap_ms -> adjacent-sample gaps larger than the strict threshold
WINDOW_AGG_SQL = """
WITH raw AS (
    SELECT
        sensor_no,
        LOWER(sensor_no) AS s_no,
        COALESCE(channel, '') AS ch,
        value,
        time,
        LAG(time) OVER (
            PARTITION BY LOWER(sensor_no), COALESCE(channel, '')
            ORDER BY time
        ) AS prev_time
    FROM public.t_monitor
    WHERE time >= %s AND time < %s
)
SELECT
    s_no,
    ch,
    COUNT(*) AS sample_cnt,
    COUNT(DISTINCT sensor_no) AS distinct_case_cnt,
    MAX(value) AS val_max,
    MIN(value) AS val_min,
    AVG(value) AS val_avg,
    COALESCE(STDDEV_POP(value), 0) AS val_std,
    MIN(time) AS first_ts,
    MAX(time) AS last_ts,
    (ARRAY_AGG(time ORDER BY value ASC, time ASC))[1] AS min_time,
    (ARRAY_AGG(time ORDER BY value DESC, time ASC))[1] AS max_time,
    COUNT(*) FILTER (
        WHERE prev_time IS NOT NULL
          AND EXTRACT(EPOCH FROM (time - prev_time)) * 1000.0 > %s
    ) AS gap_cnt,
    COALESCE(
        MAX(EXTRACT(EPOCH FROM (time - prev_time)) * 1000.0) FILTER (WHERE prev_time IS NOT NULL),
        0
    ) AS max_gap_ms
FROM raw
GROUP BY s_no, ch
"""

STAT_UPSERT_SQL = """
INSERT INTO public.t_stat_record (
    sensor_no, channel, period_type, stat_time,
    val_max, val_min, val_avg, val_std,
    max_time, min_time, sample_count, gap_count,
    calc_ts, create_time, is_delete
) VALUES (
    %s, %s, '1m', %s,
    %s, %s, %s, %s,
    %s, %s, %s, %s,
    NOW(), NOW(), false
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
    gap_count = EXCLUDED.gap_count,
    calc_ts = NOW()
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
                        "normal_streak",
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
      Req 2 (3-level thresholds + DC offset removal + debounce + hysteresis + dynamic incident upgrade),
      Req 3 (RATE, STUCK, OOR, SPIKE, DATA_CONFLICT),
      Req 4 (1m extremes & 10m vector wind rose synthesis).
    """

    def __init__(self) -> None:
        self.channel_states = ConcurrentDict()
        self.sensor_meta: Dict[str, Dict[str, Any]] = {}
        self.instrument_meta: Dict[str, Dict[str, Any]] = {}
        self.mode: str = "REPLAY"
        self.current_watermark: datetime = datetime(2026, 9, 28, 13, 55, 0)
        self.latest_data_ts: datetime = datetime(2026, 9, 30, 17, 55, 20)
        self.speed_multiplier: int = 1
        self.is_running: bool = False
        self.step_count: int = 0
        self.last_step_ms: float = 0.0
        self.total_monitor_rows: int = 398059510
        self.last_reload_time: Optional[datetime] = None
        self._sse_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self._demo_counter: int = 0
        self._step_lock = threading.Lock()
        self._type_normal_streak: Dict[str, int] = {}

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

            # 3. Load channel units
            cur.execute("SELECT sensor_no, name, unit, monitor_content, monitor_code FROM t_sensor_channel")
            ch_units: Dict[str, str] = {}
            for r in cur.fetchall():
                sno_l = (r["sensor_no"] or "").strip().lower()
                if r["unit"]:
                    ch_units[f"{sno_l}:{(r['monitor_code'] or '').strip()}"] = r["unit"]
                    ch_units[f"{sno_l}:{(r['monitor_content'] or '').strip()}"] = r["unit"]
                    ch_units[sno_l] = r["unit"]

            # 4. Load all active threshold rules
            cur.execute(
                """
                SELECT *
                FROM t_alert_threshold
                WHERE is_delete = false
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
                ext = d.get("ext_params") or {}
                unit = (
                    ext.get("unit")
                    or ch_units.get(f"{sno_l}:{ch}")
                    or ch_units.get(f"{sno_l}:{d.get('physical_quantity') or ''}")
                    or ch_units.get(sno_l)
                    or self._infer_unit(d.get("physical_quantity") or "", ch)
                )
                sname = sm.get("sensor_name") or ext.get("sensor_name") or d["sensor_no"]
                st_name = sm.get("station_name") or ("玉环站" if "_YH" in sname or sno_l.startswith(("stn-", "wlg-", "wea-", "accl-", "disp-", "phtar-", "wprs-", "temp-")) else "淮北西站")
                st_code = sm.get("station_code") or ("YHZ" if st_name == "玉环站" else "HBXZ")

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
                        "frequency": sm.get("frequency") or self._hz_from_interval(d.get("expected_interval_ms")),
                        "position": sm.get("position") or "-",
                        "unit": unit,
                        "online_status": "ONLINE",
                        "exceed_streak": 0,
                        "normal_streak": 0,
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

        if ch == "__DAQ__" or d.get("alert_type") == "DAQ_OFFLINE":
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
            0: "未处理 (Active)",
            1: "已确认 (Acked)",
            2: "已恢复 (Resolved)",
            3: "已关闭 (Closed)",
            4: "已聚合抑制 (Suppressed)",
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
        peak_val: Optional[float],
        fire_time: datetime,
        remark: str,
        rule_version: int = 1,
        threshold_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Upsert an active alert incident using partial unique index `ux_alert_record_active_dedup`
        on `dedup_key WHERE alert_status IN (0, 1, 4) AND is_delete = false`.
        Assigns a monotonically increasing `seq = nextval('public.seq_alert_record')`.
        """
        dedup_key = f"{sensor_no.lower()}:{channel}:{alert_type}:{direction}"
        fire_str = fire_time.strftime("%Y-%m-%d %H:%M:%S")
        lvl_tag = f"L{alert_level}"
        init_log = [
            {
                "time": fire_str,
                "event": "TRIGGER",
                "level": alert_level,
                "val": round(trigger_val, 5) if trigger_val is not None else None,
                "msg": f"触发 {alert_type} ({lvl_tag}): {remark[:80]}",
            }
        ]
        update_log_entry = {
            "time": fire_str,
            "event": "UPGRADE_OR_PEAK",
            "level": alert_level,
            "val": round(peak_val, 5) if peak_val is not None else None,
            "msg": f"更新状态/极值 ({lvl_tag})",
        }

        alert_id = uuid.uuid4().hex
        cur.execute(
            """
            INSERT INTO public.t_alert_record (
                id, seq, dedup_key, sensor_no, channel, threshold_id,
                alert_type, alert_level, direction,
                trigger_val, peak_val, peak_time,
                create_time, last_fire_time, update_time,
                trigger_count, alert_status, rule_version,
                threshold_snapshot, timeline_log, remark, is_delete
            ) VALUES (
                %s, nextval('public.seq_alert_record'), %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, NOW(),
                1, 0, %s,
                %s::jsonb, %s::jsonb, %s, false
            )
            ON CONFLICT (dedup_key) WHERE (alert_status = ANY (ARRAY[0, 1, 4])) AND (is_delete = false)
            DO UPDATE SET
                seq = nextval('public.seq_alert_record'),
                alert_level = LEAST(t_alert_record.alert_level, EXCLUDED.alert_level),
                peak_val = CASE
                    WHEN EXCLUDED.peak_val IS NULL THEN t_alert_record.peak_val
                    WHEN t_alert_record.peak_val IS NULL THEN EXCLUDED.peak_val
                    WHEN ABS(EXCLUDED.peak_val) >= ABS(t_alert_record.peak_val) THEN EXCLUDED.peak_val
                    ELSE t_alert_record.peak_val
                END,
                peak_time = CASE
                    WHEN EXCLUDED.peak_val IS NOT NULL AND (t_alert_record.peak_val IS NULL OR ABS(EXCLUDED.peak_val) >= ABS(t_alert_record.peak_val))
                    THEN EXCLUDED.peak_time
                    ELSE t_alert_record.peak_time
                END,
                last_fire_time = GREATEST(t_alert_record.last_fire_time, EXCLUDED.last_fire_time),
                update_time = NOW(),
                trigger_count = t_alert_record.trigger_count + 1,
                remark = EXCLUDED.remark,
                timeline_log = COALESCE(t_alert_record.timeline_log, '[]'::jsonb) || %s::jsonb
            RETURNING *
            """,
            (
                alert_id,
                dedup_key,
                sensor_no.lower(),
                channel,
                threshold_id,
                alert_type,
                alert_level,
                direction,
                trigger_val,
                peak_val,
                fire_time,
                fire_time,
                fire_time,
                rule_version,
                json_dumps(threshold_snapshot or {}),
                json_dumps(init_log),
                remark,
                json_dumps([update_log_entry]),
            ),
        )
        row = cur.fetchone()
        enriched = self.enrich_alert_row(row)
        self._notify_sse({"type": "ALERT_UPSERT", "alert": enriched})
        return enriched

    def _legacy_step_detection(
        self,
        window_start: Optional[datetime] = None,
        window_end: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Execute one detection & analytics window over `t_monitor` [window_start, window_end).
        Pushes aggregation down to TimescaleDB per (LOWER(sensor_no), COALESCE(channel, '')),
        evaluates Requirements 1, 2, 3, 4, updates `t_stat_record` and `t_alert_record`,
        and broadcasts any triggered/upgraded alerts via SSE.
        """
        t0 = time.perf_counter()
        if window_start is None:
            window_start = self.current_watermark
        if window_end is None:
            window_end = window_start + timedelta(minutes=1)

        triggered_alerts: List[Dict[str, Any]] = []
        upserted_stats = 0
        raw_points_processed = 0

        with get_db_cursor(commit=True) as cur:
            # Push-down aggregation over t_monitor for [window_start, window_end)
            cur.execute(
                """
                SELECT
                    LOWER(sensor_no) AS s_no,
                    COALESCE(channel, '') AS ch,
                    COUNT(*) AS sample_cnt,
                    COUNT(DISTINCT sensor_no) AS distinct_case_cnt,
                    MAX(value) AS val_max,
                    MIN(value) AS val_min,
                    AVG(value) AS val_avg,
                    COALESCE(STDDEV_POP(value), 0) AS val_std,
                    MIN(time) AS first_ts,
                    MAX(time) AS last_ts
                FROM public.t_monitor
                WHERE time >= %s AND time < %s
                GROUP BY LOWER(sensor_no), COALESCE(channel, '')
                """,
                (window_start, window_end),
            )
            agg_rows = cur.fetchall()

            observed_map: Dict[str, Dict[str, Any]] = {}
            for r in agg_rows:
                key = self.make_key(r["s_no"], r["ch"])
                observed_map[key] = r
                raw_points_processed += int(r["sample_cnt"] or 0)

            # Evaluate each configured channel rule
            rules = self.channel_states.values()
            offline_by_instrument: Dict[str, List[Dict[str, Any]]] = {}

            for rule in rules:
                if not rule.get("monitor_enabled", True):
                    continue
                key = rule["key"]
                s_no = rule["sensor_no_lower"]
                ch = rule.get("channel") or ""
                obs = observed_map.get(key)

                snap = {
                    "sensor_no": s_no,
                    "channel": ch,
                    "level1": [rule.get("level1_lower"), rule.get("level1_upper")],
                    "level2": [rule.get("level2_lower"), rule.get("level2_upper")],
                    "level3": [rule.get("level3_lower"), rule.get("level3_upper")],
                    "remove_dc_offset": rule.get("remove_dc_offset", False),
                    "expected_interval_ms": rule.get("expected_interval_ms", 1000),
                    "offline_timeout_ms": rule.get("offline_timeout_ms", 1000),
                }

                if obs is not None and int(obs["sample_cnt"]) > 0:
                    cnt = int(obs["sample_cnt"])
                    v_max = float(obs["val_max"])
                    v_min = float(obs["val_min"])
                    v_avg = float(obs["val_avg"])
                    v_std = float(obs["val_std"])
                    last_ts = obs["last_ts"]

                    # Requirement 4: Upsert 1-minute extreme statistics into t_stat_record
                    expected_ms = int(rule.get("expected_interval_ms") or 1000)
                    expected_cnt = max(1, int(60000 / max(1, expected_ms)))
                    gap_cnt = max(0, expected_cnt - cnt) if expected_ms <= 1000 else 0

                    cur.execute(
                        """
                        INSERT INTO public.t_stat_record (
                            sensor_no, channel, period_type, stat_time,
                            val_max, val_min, val_avg, val_std,
                            max_time, min_time, sample_count, gap_count,
                            calc_ts, create_time, is_delete
                        ) VALUES (
                            %s, %s, '1m', %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            NOW(), NOW(), false
                        )
                        ON CONFLICT (stat_time, sensor_no, channel, period_type)
                        DO UPDATE SET
                            val_max = EXCLUDED.val_max,
                            val_min = EXCLUDED.val_min,
                            val_avg = EXCLUDED.val_avg,
                            val_std = EXCLUDED.val_std,
                            sample_count = EXCLUDED.sample_count,
                            gap_count = EXCLUDED.gap_count,
                            calc_ts = NOW()
                        """,
                        (
                            s_no,
                            ch,
                            window_start.replace(second=0, microsecond=0),
                            v_max,
                            v_min,
                            v_avg,
                            v_std,
                            last_ts,
                            obs["first_ts"],
                            cnt,
                            gap_cnt,
                        ),
                    )
                    upserted_stats += 1

                    # Update in-memory runtime state
                    self.channel_states.update_field(
                        key,
                        last_ts=serialize_val(last_ts),
                        last_val=round(v_max, 5),
                        online_status="ONLINE",
                    )

                    # Requirement 3a: Case-mixing / duplicate timestamp DATA_CONFLICT detection
                    if int(obs["distinct_case_cnt"] or 1) > 1:
                        alert = self._upsert_alert_incident(
                            cur,
                            sensor_no=s_no,
                            channel=ch,
                            threshold_id=rule["id"],
                            alert_type="DATA_CONFLICT",
                            alert_level=3,
                            direction="NONE",
                            trigger_val=v_max,
                            peak_val=v_max,
                            fire_time=last_ts,
                            remark=f"[数据写入冲突 DATA_CONFLICT] 检测到 {s_no.upper()} 与 {s_no} 大小写混写上报，已自动归一化归并",
                            rule_version=int(rule.get("version") or 1),
                            threshold_snapshot=snap,
                        )
                        triggered_alerts.append(alert)

                    # Requirement 2: 3-level threshold evaluation (with AC amplitude DC offset removal)
                    remove_dc = bool(rule.get("remove_dc_offset", False))
                    if remove_dc:
                        eval_upper = max(abs(v_max - v_avg), abs(v_min - v_avg))
                        eval_lower = -eval_upper
                    else:
                        eval_upper = v_max
                        eval_lower = v_min

                    l1_u = rule.get("level1_upper")
                    l1_l = rule.get("level1_lower")
                    l2_u = rule.get("level2_upper")
                    l2_l = rule.get("level2_lower")
                    l3_u = rule.get("level3_upper")
                    l3_l = rule.get("level3_lower")

                    hit_level: Optional[int] = None
                    hit_dir = "UPPER"
                    hit_val = eval_upper
                    limit_hit = None

                    if l1_u is not None and eval_upper > float(l1_u):
                        hit_level, hit_dir, hit_val, limit_hit = 1, "UPPER", eval_upper, float(l1_u)
                    elif not remove_dc and l1_l is not None and eval_lower < float(l1_l):
                        hit_level, hit_dir, hit_val, limit_hit = 1, "LOWER", eval_lower, float(l1_l)
                    elif l2_u is not None and eval_upper > float(l2_u):
                        hit_level, hit_dir, hit_val, limit_hit = 2, "UPPER", eval_upper, float(l2_u)
                    elif not remove_dc and l2_l is not None and eval_lower < float(l2_l):
                        hit_level, hit_dir, hit_val, limit_hit = 2, "LOWER", eval_lower, float(l2_l)
                    elif l3_u is not None and eval_upper > float(l3_u):
                        hit_level, hit_dir, hit_val, limit_hit = 3, "UPPER", eval_upper, float(l3_u)
                    elif not remove_dc and l3_l is not None and eval_lower < float(l3_l):
                        hit_level, hit_dir, hit_val, limit_hit = 3, "LOWER", eval_lower, float(l3_l)

                    if hit_level is not None:
                        lvl_names = {1: "一级红色报警", 2: "二级橙色报警", 3: "三级黄色预警"}
                        dir_cn = "去直流幅值超限" if remove_dc else ("上限超限" if hit_dir == "UPPER" else "下限超限")
                        unit = rule.get("unit") or ""
                        remark = (
                            f"[{lvl_names[hit_level]}] {rule.get('sensor_name')} {rule.get('physical_quantity')}{dir_cn}: "
                            f"峰值 {hit_val:.4f} {unit} (阈值 {limit_hit:.4f} {unit})"
                        )
                        alert = self._upsert_alert_incident(
                            cur,
                            sensor_no=s_no,
                            channel=ch,
                            threshold_id=rule["id"],
                            alert_type="THRESHOLD",
                            alert_level=hit_level,
                            direction=hit_dir,
                            trigger_val=hit_val,
                            peak_val=hit_val,
                            fire_time=last_ts,
                            remark=remark,
                            rule_version=int(rule.get("version") or 1),
                            threshold_snapshot=snap,
                        )
                        triggered_alerts.append(alert)

                    # Requirement 3b: Out-Of-Range (OOR) physical range check
                    ext = rule.get("ext_params") or {}
                    p_max = ext.get("physical_max")
                    p_min = ext.get("physical_min")
                    if (p_max is not None and v_max > float(p_max)) or (p_min is not None and v_min < float(p_min)):
                        oor_val = v_max if (p_max is not None and v_max > float(p_max)) else v_min
                        alert = self._upsert_alert_incident(
                            cur,
                            sensor_no=s_no,
                            channel=ch,
                            threshold_id=rule["id"],
                            alert_type="OOR",
                            alert_level=2,
                            direction="NONE",
                            trigger_val=oor_val,
                            peak_val=oor_val,
                            fire_time=last_ts,
                            remark=f"[超量程异常 OOR] {rule.get('sensor_name')} 读数 {oor_val:.4f} {rule.get('unit','')} 超出物理量程 [{p_min}, {p_max}]",
                            rule_version=int(rule.get("version") or 1),
                            threshold_snapshot=snap,
                        )
                        triggered_alerts.append(alert)

                    # Requirement 1b: 1000ms sampling gap detection for >= 1Hz sensors
                    if expected_ms <= 1000 and cnt < expected_cnt * 0.5:
                        est_gap_ms = round(60000.0 / max(1, cnt), 1)
                        if est_gap_ms > 1000:
                            alert = self._upsert_alert_incident(
                                cur,
                                sensor_no=s_no,
                                channel=ch,
                                threshold_id=rule["id"],
                                alert_type="GAP_1000MS",
                                alert_level=3,
                                direction="NONE",
                                trigger_val=1000.0,
                                peak_val=est_gap_ms,
                                fire_time=last_ts,
                                remark=f"[采样断流 >1000ms] {rule.get('sensor_name')} 采样间隔出现 >1000ms 断流 (最大间隔约 {est_gap_ms:.0f}ms, 窗口实收 {cnt}/{expected_cnt})",
                                rule_version=int(rule.get("version") or 1),
                                threshold_snapshot=snap,
                            )
                            triggered_alerts.append(alert)
                else:
                    # No data in window -> track offline status by instrument
                    self.channel_states.update_field(key, online_status="OFFLINE")
                    inst_id = rule.get("instrument_id") or "__STANDALONE__"
                    offline_by_instrument.setdefault(inst_id, []).append(rule)

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        self.current_watermark = window_end
        self.step_count += 1
        self.last_step_ms = elapsed_ms

        return {
            "windowStart": serialize_val(window_start),
            "windowEnd": serialize_val(window_end),
            "currentWatermark": serialize_val(self.current_watermark),
            "rawPointsProcessed": raw_points_processed,
            "upsertedStatsCount": upserted_stats,
            "triggeredAlertsCount": len(triggered_alerts),
            "triggeredAlerts": triggered_alerts[:10],
            "elapsedMs": elapsed_ms,
        }

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
        direction: Optional[str] = "NONE",
    ) -> Tuple[str, str, str, str]:
        return (
            (sensor_no or "").strip().lower(),
            (channel or "").strip(),
            (alert_type or "").strip().upper(),
            (direction or "NONE").strip().upper(),
        )

    def _build_threshold_snapshot(self, rule: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "sensor_no": rule.get("sensor_no_lower"),
            "channel": rule.get("channel") or "",
            "level1": [rule.get("level1_lower"), rule.get("level1_upper")],
            "level2": [rule.get("level2_lower"), rule.get("level2_upper")],
            "level3": [rule.get("level3_lower"), rule.get("level3_upper")],
            "remove_dc_offset": bool(rule.get("remove_dc_offset", False)),
            "expected_interval_ms": rule.get("expected_interval_ms", 1000),
            "offline_timeout_ms": rule.get("offline_timeout_ms", 1000),
            "confirm_points": rule.get("confirm_points", 3),
            "recover_points": rule.get("recover_points", 3),
            "hysteresis": rule.get("hysteresis", 0),
            "ext_params": rule.get("ext_params") or {},
            "version": rule.get("version") or 1,
        }

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
                return level, "UPPER", eval_upper, float(upper)
            if remove_dc:
                continue
            lower = rule.get(f"level{level}_lower")
            if lower is not None and eval_lower < float(lower):
                return level, "LOWER", eval_lower, float(lower)
        return None, "NONE", None, None

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

    def _sample_is_normal(
        self,
        rule: Dict[str, Any],
        value: float,
        dc_offset: float,
        direction: str,
    ) -> bool:
        remove_dc = bool(rule.get("remove_dc_offset", False))
        if remove_dc:
            eval_upper = abs(value - dc_offset)
            eval_lower = -eval_upper
        else:
            eval_upper = value
            eval_lower = value

        hysteresis = abs(float(rule.get("hysteresis") or 0.0))
        level3_upper = rule.get("level3_upper")
        level3_lower = rule.get("level3_lower")

        if direction == "UPPER":
            if level3_upper is None:
                return True
            return eval_upper <= float(level3_upper) - hysteresis
        if direction == "LOWER":
            if remove_dc or level3_lower is None:
                return True
            return eval_lower >= float(level3_lower) + hysteresis
        return True

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

    def _count_normal_run(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        dc_offset: float,
        direction: str,
    ) -> int:
        count = 0
        for _, value in reversed(samples):
            if not self._sample_is_normal(rule, value, dc_offset, direction):
                break
            count += 1
        return count

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
            WHERE is_delete = false
              AND alert_status IN (0, 1, 4)
            """
        )
        active: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        for row in cur.fetchall():
            d = dict(row)
            active[self._alert_key(d["sensor_no"], d.get("channel"), d["alert_type"], d.get("direction"))] = d
        return active

    def _resolve_active(
        self,
        cur: Any,
        active_row: Dict[str, Any],
        *,
        resolve_time: datetime,
        value: Optional[float],
        remark: str,
    ) -> Optional[Dict[str, Any]]:
        if not active_row or int(active_row.get("alert_status") or 0) == 2:
            return None
        log_entry = {
            "time": resolve_time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": "RECOVER",
            "level": int(active_row.get("alert_level") or 3),
            "val": round(value, 5) if value is not None else None,
            "msg": remark,
        }
        cur.execute(
            """
            UPDATE public.t_alert_record
            SET alert_status = 2,
                end_ts = COALESCE(end_ts, %s),
                last_fire_time = GREATEST(COALESCE(last_fire_time, %s), %s),
                update_time = NOW(),
                seq = nextval('public.seq_alert_record'),
                timeline_log = COALESCE(timeline_log, '[]'::jsonb) || %s::jsonb
            WHERE id = %s
            RETURNING *
            """,
            (
                resolve_time,
                resolve_time,
                resolve_time,
                json_dumps([log_entry]),
                active_row["id"],
            ),
        )
        row = cur.fetchone()
        if not row:
            return None
        enriched = self.enrich_alert_row(row)
        self._notify_sse({"type": "ALERT_UPSERT", "alert": enriched})
        return enriched

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
        peak_val: Optional[float],
        fire_time: datetime,
        remark: str,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        key = self._alert_key(sensor_no, channel, alert_type, direction)
        existing = active_map.get(key)
        if active:
            alert = self._upsert_alert_incident(
                cur,
                sensor_no=sensor_no,
                channel=channel,
                threshold_id=rule.get("id"),
                alert_type=alert_type,
                alert_level=alert_level,
                direction=direction,
                trigger_val=trigger_val,
                peak_val=peak_val,
                fire_time=fire_time,
                remark=remark,
                rule_version=int(rule.get("version") or 1),
                threshold_snapshot=snapshot or self._build_threshold_snapshot(rule),
            )
            active_map[key] = alert
            triggered.append(alert)
            return alert

        if existing:
            resolved = self._resolve_active(
                cur,
                existing,
                resolve_time=fire_time,
                value=peak_val,
                remark=f"{sensor_no} {channel} {alert_type} 条件恢复正常，自动解除告警",
            )
            active_map.pop(key, None)
            if resolved:
                triggered.append(resolved)
            return resolved
        return None

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
                max_time, min_time, sample_count, gap_count,
                ext_json, calc_ts, create_time, is_delete
            ) VALUES (
                %s, 'wind_rose', '10m', %s,
                %s, %s, %s, %s,
                %s, %s, %s, 0,
                %s::jsonb, NOW(), NOW(), false
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
                gap_count = 0,
                ext_json = EXCLUDED.ext_json,
                calc_ts = NOW()
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
            cur.execute(WINDOW_AGG_SQL, (window_start, window_end, 1000.0))
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
                if r.get("monitor_enabled", True) and (r.get("instrument_id") or "").strip()
            )
            offline_by_instrument: Dict[str, List[Dict[str, Any]]] = {}

            for rule in rules:
                if not rule.get("monitor_enabled", True):
                    continue

                key = rule["key"]
                sensor_no = rule["sensor_no_lower"]
                channel = rule.get("channel") or ""
                obs = observed_map.get(key)
                snapshot = self._build_threshold_snapshot(rule)

                if obs is None or int(obs["sample_cnt"] or 0) <= 0:
                    last_seen = self._parse_maybe_dt(rule.get("last_ts"))
                    if last_seen is None:
                        self.channel_states.update_field(
                            key,
                            last_ts=serialize_val(window_end),
                            online_status="OFFLINE",
                        )
                        continue

                    silence_ms = (window_end - last_seen).total_seconds() * 1000.0
                    offline_timeout_ms = int(rule.get("offline_timeout_ms") or 10000)
                    if silence_ms <= offline_timeout_ms:
                        continue

                    expected_ms = int(rule.get("expected_interval_ms") or 1000)
                    offline_level = 1 if expected_ms <= 200 else 2
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
                        alert_type="OFFLINE",
                        active=True,
                        alert_level=offline_level,
                        direction="NONE",
                        trigger_val=float(offline_timeout_ms),
                        peak_val=round(silence_ms, 3),
                        fire_time=window_end,
                        remark=(
                            f"[传感器离线 OFFLINE] {rule.get('sensor_name')} "
                            f"{channel} 已连续 {silence_ms:.0f}ms 无数据，超过离线阈值 {offline_timeout_ms}ms"
                        ),
                        snapshot=snapshot,
                    )
                    self.channel_states.update_field(key, online_status="OFFLINE")
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
                strict_gap_ms = float((rule.get("ext_params") or {}).get("strict_gap_ms") or 1000.0)
                gap_cnt = int(obs["gap_cnt"] or 0)
                max_gap_ms = float(obs["max_gap_ms"] or 0.0)

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
                        gap_cnt,
                    ),
                )
                upserted_stats += 1

                self.channel_states.update_field(
                    key,
                    last_ts=serialize_val(last_ts),
                    last_val=round(val_max, 5),
                    online_status="ONLINE",
                )

                offline_active = active_map.get(self._alert_key(sensor_no, channel, "OFFLINE", "NONE"))
                if offline_active:
                    resolved = self._resolve_active(
                        cur,
                        offline_active,
                        resolve_time=last_ts,
                        value=float(sample_cnt),
                        remark=f"{rule.get('sensor_name')} {channel} 恢复上报，自动解除离线告警",
                    )
                    active_map.pop(self._alert_key(sensor_no, channel, "OFFLINE", "NONE"), None)
                    if resolved:
                        triggered_alerts.append(resolved)

                ext = rule.get("ext_params") or {}

                conflict_active = int(obs["distinct_case_cnt"] or 1) > 1
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type="DATA_CONFLICT",
                    active=conflict_active,
                    alert_level=3,
                    direction="NONE",
                    trigger_val=val_max,
                    peak_val=val_max,
                    fire_time=last_ts,
                    remark=(
                        f"[数据写入冲突 DATA_CONFLICT] {sensor_no} 在同一稳定键上检测到大小写混写，"
                        f"窗口样本 {sample_cnt} 条，已按小写键归并"
                    ),
                    snapshot=snapshot,
                )

                stuck_points = int(ext.get("stuck_points") or 30)
                stuck_active = sample_cnt >= stuck_points and val_std <= 1e-9
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type="STUCK",
                    active=stuck_active,
                    alert_level=3,
                    direction="NONE",
                    trigger_val=val_avg,
                    peak_val=val_avg,
                    fire_time=last_ts,
                    remark=(
                        f"[传感器死值 STUCK] {rule.get('sensor_name')} {channel} "
                        f"连续 {sample_cnt} 点读数恒为 {val_avg:.5f}，标准差为 0"
                    ),
                    snapshot=snapshot,
                )

                span_seconds = max(0.0, (last_ts - first_ts).total_seconds())
                rate_per_sec = abs(val_max - val_min) / span_seconds if span_seconds > 0 else 0.0
                max_rate = float(ext.get("max_rate_per_sec") or 0.0)
                rate_active = max_rate > 0 and rate_per_sec > max_rate
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type="RATE",
                    active=rate_active,
                    alert_level=2,
                    direction="NONE",
                    trigger_val=rate_per_sec,
                    peak_val=rate_per_sec,
                    fire_time=last_ts,
                    remark=(
                        f"[变化率超限 RATE] {rule.get('sensor_name')} {channel} "
                        f"窗口内变化率 {rate_per_sec:.5f}/s 超过配置上限 {max_rate:.5f}/s"
                    ),
                    snapshot=snapshot,
                )

                deviation = max(abs(val_max - val_avg), abs(val_min - val_avg))
                spike_sigma = deviation / val_std if val_std > 1e-12 else 0.0
                spike_active = sample_cnt >= 10 and spike_sigma > 6.0
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type="SPIKE",
                    active=spike_active,
                    alert_level=2,
                    direction="NONE",
                    trigger_val=val_max if abs(val_max - val_avg) >= abs(val_min - val_avg) else val_min,
                    peak_val=val_max if abs(val_max - val_avg) >= abs(val_min - val_avg) else val_min,
                    fire_time=max_time if abs(val_max - val_avg) >= abs(val_min - val_avg) else min_time,
                    remark=(
                        f"[毛刺 SPIKE] {rule.get('sensor_name')} {channel} "
                        f"极值偏离均值 {spike_sigma:.2f}σ，超过 6σ 毛刺判据"
                    ),
                    snapshot=snapshot,
                )

                physical_max = ext.get("physical_max")
                physical_min = ext.get("physical_min")
                oor_active = (
                    (physical_max is not None and val_max > float(physical_max))
                    or (physical_min is not None and val_min < float(physical_min))
                )
                oor_value = val_max
                if physical_min is not None and val_min < float(physical_min):
                    oor_value = val_min
                elif physical_max is None:
                    oor_value = val_min
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type="OOR",
                    active=oor_active,
                    alert_level=2,
                    direction="NONE",
                    trigger_val=oor_value,
                    peak_val=oor_value,
                    fire_time=last_ts,
                    remark=(
                        f"[超物理量程 OOR] {rule.get('sensor_name')} {channel} "
                        f"读数范围 [{val_min:.5f}, {val_max:.5f}] 超出 [{physical_min}, {physical_max}]"
                    ),
                    snapshot=snapshot,
                )

                gap_active = gap_cnt > 0 and max_gap_ms > strict_gap_ms
                self._apply_condition(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    alert_type="GAP_1000MS",
                    active=gap_active,
                    alert_level=3,
                    direction="NONE",
                    trigger_val=strict_gap_ms,
                    peak_val=max_gap_ms,
                    fire_time=last_ts,
                    remark=(
                        f"[采样断流 GAP_1000MS] {rule.get('sensor_name')} {channel} "
                        f"窗口内出现 {gap_cnt} 次超过 {strict_gap_ms:.0f}ms 的采样间隔，最大 {max_gap_ms:.0f}ms"
                    ),
                    snapshot=snapshot,
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
                    and alert_key[2] == "THRESHOLD"
                ]
                if aggregate_level is not None or active_thresholds:
                    samples = self._fetch_samples(cur, sensor_no, channel, window_start, window_end)
                    directions = ["UPPER"] if remove_dc else ["UPPER", "LOWER"]
                    for direction in directions:
                        threshold_key = self._alert_key(sensor_no, channel, "THRESHOLD", direction)
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
                                    "上限超限" if direction == "UPPER" else "下限超限"
                                )
                                self._apply_condition(
                                    cur,
                                    active_map,
                                    triggered_alerts,
                                    rule=rule,
                                    sensor_no=sensor_no,
                                    channel=channel,
                                    alert_type="THRESHOLD",
                                    active=True,
                                    alert_level=hit_level,
                                    direction=direction,
                                    trigger_val=hit_value,
                                    peak_val=hit_value,
                                    fire_time=last_ts,
                                    remark=(
                                        f"[{level_names[hit_level]}] {rule.get('sensor_name')} "
                                        f"{rule.get('physical_quantity')}{direction_cn}: "
                                        f"峰值 {hit_value:.4f} {rule.get('unit') or ''} "
                                        f"(阈值 {hit_limit:.4f} {rule.get('unit') or ''})"
                                    ),
                                    snapshot=snapshot,
                                )
                            else:
                                recover_points = max(1, int(rule.get("recover_points") or 3))
                                normal_run = self._count_normal_run(samples, rule, val_avg, direction)
                                if normal_run >= recover_points:
                                    resolved = self._resolve_active(
                                        cur,
                                        existing,
                                        resolve_time=last_ts,
                                        value=val_avg,
                                        remark=(
                                            f"{rule.get('sensor_name')} {channel} {direction} "
                                            f"连续 {normal_run} 点恢复正常，自动解除阈值告警"
                                        ),
                                    )
                                    active_map.pop(threshold_key, None)
                                    if resolved:
                                        triggered_alerts.append(resolved)
                        elif exceeded > 0 and hit_level is not None:
                            confirm_points = max(1, int(rule.get("confirm_points") or 3))
                            if exceeded >= confirm_points:
                                level_names = {1: "一级红色报警", 2: "二级橙色报警", 3: "三级黄色预警"}
                                direction_cn = "去直流幅值超限" if remove_dc else (
                                    "上限超限" if direction == "UPPER" else "下限超限"
                                )
                                self._apply_condition(
                                    cur,
                                    active_map,
                                    triggered_alerts,
                                    rule=rule,
                                    sensor_no=sensor_no,
                                    channel=channel,
                                    alert_type="THRESHOLD",
                                    active=True,
                                    alert_level=hit_level,
                                    direction=direction,
                                    trigger_val=hit_value,
                                    peak_val=hit_value,
                                    fire_time=last_ts,
                                    remark=(
                                        f"[{level_names[hit_level]}] {rule.get('sensor_name')} "
                                        f"{rule.get('physical_quantity')}{direction_cn}: "
                                        f"连续 {exceeded} 点超限，峰值 {hit_value:.4f} "
                                        f"{rule.get('unit') or ''} (阈值 {hit_limit:.4f} {rule.get('unit') or ''})"
                                    ),
                                    snapshot=snapshot,
                                )

            active_daq_instruments = {
                key[0]
                for key in active_map
                if key[2] == "DAQ_OFFLINE"
            }
            for instrument_id in sorted(set(offline_by_instrument) | active_daq_instruments):
                offline_rules = offline_by_instrument.get(instrument_id, [])
                total_channels = int(instrument_totals.get(instrument_id, 0))
                required_offline = max(2, int(math.ceil(total_channels * 0.5)))
                if total_channels <= 0 or len(offline_rules) < required_offline:
                    daq_key = self._alert_key(instrument_id, "__DAQ__", "DAQ_OFFLINE", "NONE")
                    active_daq = active_map.get(daq_key)
                    if active_daq:
                        resolved = self._resolve_active(
                            cur,
                            active_daq,
                            resolve_time=window_end,
                            value=float(len(offline_rules)),
                            remark=(
                                f"采集仪 {instrument_id} 在线通道恢复，离线通道 "
                                f"{len(offline_rules)}/{total_channels} 已低于聚合阈值 {required_offline}"
                            ),
                        )
                        active_map.pop(daq_key, None)
                        if resolved:
                            triggered_alerts.append(resolved)
                    continue

                daq_alert = self._upsert_alert_incident(
                    cur,
                    sensor_no=instrument_id,
                    channel="__DAQ__",
                    threshold_id=None,
                    alert_type="DAQ_OFFLINE",
                    alert_level=1,
                    direction="NONE",
                    trigger_val=float(required_offline),
                    peak_val=float(len(offline_rules)),
                    fire_time=window_end,
                    remark=(
                        f"[采集仪批量离线 DAQ_OFFLINE] 采集仪 {instrument_id} 下 "
                        f"{len(offline_rules)}/{total_channels} 个通道离线，达到聚合阈值 {required_offline}，"
                        f"已生成设备级主告警并抑制子通道告警"
                    ),
                    threshold_snapshot={
                        "instrument_id": instrument_id,
                        "offline_channels": len(offline_rules),
                        "total_channels": total_channels,
                        "required_offline": required_offline,
                    },
                )
                active_map[self._alert_key(instrument_id, "__DAQ__", "DAQ_OFFLINE", "NONE")] = daq_alert
                triggered_alerts.append(daq_alert)

                for rule in offline_rules:
                    child_key = self._alert_key(rule["sensor_no_lower"], rule.get("channel") or "", "OFFLINE", "NONE")
                    child = active_map.get(child_key)
                    if not child or int(child.get("alert_status") or 0) not in (0, 1):
                        continue
                    log_entry = {
                        "time": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                        "event": "SUPPRESSED_BY_DAQ",
                        "level": int(child.get("alert_level") or 3),
                        "msg": f"子通道离线告警被采集仪主告警 {instrument_id} 聚合抑制",
                    }
                    cur.execute(
                        """
                        UPDATE public.t_alert_record
                        SET alert_status = 4,
                            update_time = NOW(),
                            seq = nextval('public.seq_alert_record'),
                            timeline_log = COALESCE(timeline_log, '[]'::jsonb) || %s::jsonb
                        WHERE id = %s
                        RETURNING *
                        """,
                        (json_dumps([log_entry]), child["id"]),
                    )
                    suppressed = cur.fetchone()
                    if suppressed:
                        enriched = self.enrich_alert_row(suppressed)
                        active_map[child_key] = enriched
                        triggered_alerts.append(enriched)
                        self._notify_sse({"type": "ALERT_UPSERT", "alert": enriched})

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
                "alert_type": "THRESHOLD",
                "alert_level": 1,
                "direction": "LOWER",
                "trigger_val": -11.32,
                "peak_val": -11.584,
                "remark": "[一级红色报警] 光纤光栅应变计_25_YH (玉环站主桁架) 应力下限超限: 实时峰值 -11.5840 MPa < 一级下限 -11.20 MPa",
            },
            {
                "sensor_no": "accl-02",
                "channel": "y",
                "alert_type": "THRESHOLD",
                "alert_level": 2,
                "direction": "UPPER",
                "trigger_val": 1.542,
                "peak_val": 1.685,
                "remark": "[二级橙色报警] 三向加速度计_2_YH y轴去直流振动幅值超限: 1.6850 m/s² > 二级上限 1.52 m/s² (DC offset=20.0014)",
            },
            {
                "sensor_no": "wprs-03",
                "channel": "",
                "alert_type": "THRESHOLD",
                "alert_level": 1,
                "direction": "UPPER",
                "trigger_val": 392.5,
                "peak_val": 402.8,
                "remark": "[一级红色报警] 风压计_3_YH 屋面风压超限: 实时极值 402.80 kPa > 一级上限 390.00 kPa",
            },
            {
                "sensor_no": "accl-01",
                "channel": "x",
                "alert_type": "GAP_1000MS",
                "alert_level": 1,
                "direction": "NONE",
                "trigger_val": 1000.0,
                "peak_val": 2450.0,
                "remark": "[数据中断 >1000ms] 三向加速度计_1_YH (100Hz高频通道) 连续 2450ms 未收到采样数据，触发 1000ms 断流告警",
            },
            {
                "sensor_no": "wea-01",
                "channel": "temperature",
                "alert_type": "THRESHOLD",
                "alert_level": 1,
                "direction": "UPPER",
                "trigger_val": 55.6,
                "peak_val": 58.4,
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
            chosen["alert_type"] = alert_type

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
                peak_val=float(chosen["peak_val"]) + round((self._demo_counter % 5) * 0.03, 4),
                fire_time=now_ts,
                remark=chosen["remark"],
                rule_version=int(rule.get("version") or 1),
                threshold_snapshot={
                    "sensor_no": chosen["sensor_no"],
                    "channel": chosen["channel"],
                    "level1": [rule.get("level1_lower"), rule.get("level1_upper")],
                    "level2": [rule.get("level2_lower"), rule.get("level2_upper")],
                    "level3": [rule.get("level3_lower"), rule.get("level3_upper")],
                },
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


engine = AlertAnalyticsEngine()
