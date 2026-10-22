"""预警检测和统计分析核心引擎。

本模块负责加载检测元数据、分析原始采样点、维护通道连续命中状态，
写入预警事件并更新统计记录。
"""

import logging
import re
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from psycopg2.extras import execute_values

from app.core.serialization import serialize_row, serialize_val
from app.domain.alert_types import (
    ALERT_CATEGORY_LABELS,
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
from app.repositories.detection_state import (
    RUNTIME_STATE_FIELDS,
    load_channel_states,
    load_rule_version,
    persist_channel_states,
    state_rows_for_persistence,
)

logger = logging.getLogger("alert_analysis.engine")

# 在数据库内一次性聚合一个 t_monitor 窗口。
#   s_no / ch            -> 分组键，传感器编号统一转小写
#   max_time / min_time  -> 极值样本的真实时间，不是窗口边界
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

# 实时预警专用的原始样本查询：按 time 升序返回窗口内的全部原始点。
# 与 WINDOW_AGG_SQL 的区别：
#   - 不聚合，保留每个点的时间，才能按时间跨度判断连续命中；
#   - value 为空的行直接排除，不参与阈值、量程和死值判定；
#   - 排序由数据库保证，下游 _evaluate_*_streak() 依赖这个顺序累计状态。
SAMPLE_BATCH_SQL = """
SELECT
    LOWER(sensor_no) AS s_no,
    COALESCE(channel, '') AS ch,
    time,
    value
FROM public.t_monitor
WHERE time >= %s AND time < %s
  AND value IS NOT NULL
ORDER BY time ASC
"""

# 仅执行统计时使用的批量更新插入语句。
STAT_UPSERT_SQL = """
INSERT INTO public.t_stat_record (
    sensor_no, channel, period_type, stat_time,
    val_max, val_min, val_avg,
    max_time, min_time, sample_count,
    create_by, create_time
) VALUES %s
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
    """线程安全的通道规则和运行状态字典。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: Dict[str, Dict[str, Any]] = {}

    def get(self, key: str, default: Any = None) -> Any:
        """返回副本，避免调用方直接修改共享状态。"""
        with self._lock:
            val = self._data.get(key, default)
            return dict(val) if isinstance(val, dict) else val

    def set(self, key: str, value: Dict[str, Any]) -> None:
        """替换一个通道状态。"""
        with self._lock:
            self._data[key] = value

    def update_field(self, key: str, **kwargs: Any) -> None:
        """更新指定通道的部分运行字段。"""
        with self._lock:
            if key in self._data:
                self._data[key].update(kwargs)

    def replace_all(self, new_map: Dict[str, Dict[str, Any]]) -> None:
        """替换元数据，同时保留仍在使用的连续命中状态。"""
        with self._lock:
            # 已存在的运行状态字段优先保留。
            merged: Dict[str, Dict[str, Any]] = {}
            for k, v in new_map.items():
                old = self._data.get(k)
                if old:
                    for rt_field in RUNTIME_STATE_FIELDS:
                        if rt_field in old:
                            v[rt_field] = old[rt_field]
                merged[k] = v
            self._data = merged

    def values(self) -> List[Dict[str, Any]]:
        """返回全部通道状态的快照。"""
        with self._lock:
            return [dict(v) for v in self._data.values()]

    def items(self) -> List[Tuple[str, Dict[str, Any]]]:
        """返回全部键和状态的快照。"""
        with self._lock:
            return [(k, dict(v)) for k, v in self._data.items()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class AlertAnalyticsEngine:
    """从原始样本检测预警，并持久化完整统计窗口。"""

    def __init__(
        self,
        *,
        confirm_points: int = 3,
        stuck_points: int = 100,
        offline_points: int = 5,
        condition_unit: str = "seconds",
        confirm_seconds: float = 3.0,
        stuck_seconds: float = 100.0,
        offline_seconds: float = 5.0,
        station_name: str = "玉环站",
        alert_type_switches: Optional[Dict[str, bool]] = None,
        persist_state: bool = False,
        rule_refresh_seconds: float = 5.0,
    ) -> None:
        # 检测、接口补充信息和总览统计共用这些元数据。
        self.channel_states = ConcurrentDict()
        self.sensor_meta: Dict[str, Dict[str, Any]] = {}
        self.instrument_meta: Dict[str, Dict[str, Any]] = {}
        self._daq_condition_active: Dict[str, bool] = {}
        self._daq_state_known: Set[str] = set()
        self.total_monitor_rows: int = 398059510
        self.last_reload_time: Optional[datetime] = None
        self.confirm_points = max(1, int(confirm_points))
        self.stuck_points = max(1, int(stuck_points))
        self.offline_points = max(1, int(offline_points))
        if condition_unit not in {"points", "seconds"}:
            raise ValueError("condition_unit must be 'points' or 'seconds'")
        self.condition_unit = condition_unit
        self.confirm_seconds = max(0.001, float(confirm_seconds))
        self.stuck_seconds = max(0.001, float(stuck_seconds))
        self.offline_seconds = max(0.001, float(offline_seconds))
        self.station_name = (station_name or "").strip() or "玉环站"
        self.station_code: Optional[str] = None
        self.persist_state = bool(persist_state)
        self.rule_refresh_seconds = max(0.1, float(rule_refresh_seconds))
        self.rule_version: Optional[int] = None
        self._last_rule_check_monotonic = 0.0
        # 未显式配置的预警类型默认启用，避免新增类型被意外关闭。
        self.alert_type_switches: Dict[str, bool] = {
            (normalize_alert_type(name) or name): bool(enabled)
            for name, enabled in (alert_type_switches or {}).items()
        }
        self._alert_step_lock = threading.Lock()
        self._statistics_step_lock = threading.Lock()
        self._active_alert_cache: Optional[
            Dict[Tuple[str, str, str], Dict[str, Any]]
        ] = None

    @staticmethod
    def make_key(sensor_no: str, channel: Optional[str]) -> str:
        """生成标准化的传感器和通道组合键。"""
        return f"{(sensor_no or '').strip().lower()}:{(channel or '').strip()}"

    def is_alert_type_enabled(self, alert_type: str) -> bool:
        """返回某类预警的检测开关状态，未配置的类型默认启用。

        开关只在写预警时生效（_apply_condition / 采集仪离线聚合会调用它），
        关闭某类预警只会停止写入该类 t_alert_record，不影响：
          - t_monitor 样本读取和连续命中状态累计；
          - t_stat_record 统计窗口生成。
        开关值来自 backend/.env，只在进程启动时读取一次。
        """
        normalized = normalize_alert_type(alert_type) or alert_type
        return bool(self.alert_type_switches.get(normalized, True))

    # ------------------------------------------------------------------
    # 元数据初始化和热更新
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """加载规则、初始化水位并刷新监测数据总量。"""
        self.reload_thresholds()
        self.bootstrap_last_seen()
        self.reconcile_open_alerts()
        self.refresh_monitor_row_count()

    def reconcile_open_alerts(self) -> int:
        """Close persisted open alerts whose condition already recovered."""

        if not self.persist_state:
            return 0
        closed = 0
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                """
                SELECT *
                FROM public.t_alert_record
                WHERE end_time IS NULL
                """
            )
            for row in cur.fetchall():
                sensor_no = str(row["sensor_no"] or "").strip().lower()
                channel = str(row.get("channel") or "").strip()
                alert_type = normalize_alert_type(row["alert_type"]) or row["alert_type"]
                if alert_type == ALERT_TYPE_DAQ_OFFLINE:
                    condition_active = bool(
                        self._daq_condition_active.get(sensor_no)
                    )
                else:
                    state = self.channel_states.get(
                        self.make_key(sensor_no, channel)
                    )
                    if state is None:
                        continue
                    state_field = {
                        ALERT_TYPE_THRESHOLD: "threshold_condition_active",
                        ALERT_TYPE_OFFLINE: "offline_condition_active",
                        ALERT_TYPE_STUCK: "stuck_condition_active",
                        ALERT_TYPE_OOR: "oor_condition_active",
                    }.get(alert_type)
                    if state_field is None:
                        continue
                    condition_active = bool(state.get(state_field))
                if condition_active:
                    continue
                cur.execute(
                    """
                    UPDATE public.t_alert_record
                    SET end_time = LOCALTIMESTAMP
                    WHERE id = %s
                      AND end_time IS NULL
                    """,
                    (row["id"],),
                )
                closed += int(cur.rowcount or 0)
        if closed:
            logger.info("Closed %d recovered alert incidents during reconciliation.", closed)
        return closed

    def bootstrap_last_seen(self) -> None:
        """初始化最后上报时间，确保重启后仍能判断离线。"""
        rules = self.channel_states.values()
        if not rules:
            return

        with get_db_cursor() as cur:
            cur.execute("SELECT LOCALTIMESTAMP AS now")
            now = cur.fetchone()["now"]
        max_timeout_seconds = max(
            self._offline_timeout_ms(rule) / 1000.0 for rule in rules
        )
        # 至少回看一分钟，且不少于一个离线判定周期。
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
            if rule.get("last_ts") is not None:
                continue
            last_seen = self._parse_maybe_dt(latest_by_target.get(key))
            offline_timeout_ms = self._offline_timeout_ms(rule)
            timeout = timedelta(milliseconds=offline_timeout_ms)
            if last_seen is None:
                # 没有历史样本时，让通道初始状态具备离线判定条件。
                last_seen = now - timeout - timedelta(minutes=2)
            online_status = "ONLINE" if now - last_seen <= timeout else "OFFLINE"
            self.channel_states.update_field(
                key,
                last_ts=serialize_val(last_seen),
                online_status=online_status,
            )

    def refresh_monitor_row_count(self) -> int:
        """通过元数据刷新监测数据量，避免全表扫描。"""
        try:
            # 优先读取 TimescaleDB 元数据，失败后使用 PostgreSQL 估算值。
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

    def _resolve_station_code(self, cur: Any) -> str:
        """Resolve the configured station name to its station code."""
        cur.execute(
            """
            SELECT number
            FROM t_station
            WHERE BTRIM(name) = BTRIM(%s)
            ORDER BY sort NULLS LAST, number
            LIMIT 1
            """,
            (self.station_name,),
        )
        row = cur.fetchone()
        station_code = str(row["number"] or "").strip() if row else ""
        if not station_code:
            raise RuntimeError(
                f"Station {self.station_name!r} was not found in t_station"
            )
        return station_code

    def reload_thresholds(self) -> int:
        """热更新规则和元数据，并保留现有运行状态。"""
        with get_db_cursor() as cur:
            # 1. 加载采集仪与站点映射。
            station_code = self._resolve_station_code(cur)
            self.station_code = station_code
            persisted_states: Dict[str, Dict[str, Any]] = {}
            persisted_daq_states: Dict[str, bool] = {}
            if self.persist_state:
                persisted_states, persisted_daq_states = load_channel_states(cur)
            cur.execute(
                """
                SELECT ci.id, ci.number, ci.name, ci.model, ci.manufacturer, ci.box_id,
                       cb.name AS box_name, cb.station AS station_code,
                       COALESCE(NULLIF(BTRIM(st.name), ''), %s) AS station_name
                FROM t_collect_instrument ci
                LEFT JOIN t_collect_box cb ON ci.box_id = cb.id
                LEFT JOIN t_station st ON cb.station = st.number
                WHERE cb.station = %s
                """,
                (self.station_name, station_code),
            )
            inst_map: Dict[str, Dict[str, Any]] = {}
            for r in cur.fetchall():
                inst_map[r["id"]] = serialize_row(r)
            self.instrument_meta = inst_map

            # 2. 加载传感器和通道信息。
            cur.execute(
                """
                SELECT s.id AS sensor_id, s.sensor_no, s.name AS sensor_name, s.model,
                       s.manufacturer, s.range, s.precision, s.frequency, s.position,
                       s.instrument_id,
                       ci.name AS instrument_name,
                       cb.station AS station_code,
                       COALESCE(NULLIF(BTRIM(st.name), ''), %s) AS station_name
                FROM t_sensor s
                LEFT JOIN t_collect_instrument ci ON s.instrument_id = ci.id
                LEFT JOIN t_collect_box cb ON ci.box_id = cb.id
                LEFT JOIN t_station st ON cb.station = st.number
                WHERE cb.station = %s
                """,
                (self.station_name, station_code),
            )
            s_meta: Dict[str, Dict[str, Any]] = {}
            for r in cur.fetchall():
                sno_lower = (r["sensor_no"] or "").strip().lower()
                sname = r["sensor_name"] or r["sensor_no"]
                st_code = r["station_code"]
                st_name = r["station_name"] or self.station_name
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

            # 3. 加载通道元数据。
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

            # 4. 构建每轮检测使用的运行时规则表。
            cur.execute(
                """
                SELECT at.*
                FROM t_alert_threshold at
                WHERE EXISTS (
                    SELECT 1
                    FROM t_sensor s
                    JOIN t_collect_instrument ci ON s.instrument_id = ci.id
                    JOIN t_collect_box cb ON ci.box_id = cb.id
                    WHERE LOWER(BTRIM(s.sensor_no)) = LOWER(BTRIM(at.sensor_no))
                      AND cb.station = %s
                )
                ORDER BY at.sensor_no ASC, at.channel ASC
                """,
                (station_code,),
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
                st_name = sm.get("station_name") or self.station_name
                st_code = sm.get("station_code") or ""
                range_min = d.get("range_min")
                range_max = d.get("range_max")
                if range_min is None or range_max is None:
                    range_min, range_max = self._parse_physical_range(
                        sm.get("range"),
                        unit,
                        physical_quantity,
                        ch,
                    )
                frequency_text = sm.get("frequency") or "1"
                frequency_hz = self._parse_frequency_hz(frequency_text)
                sample_interval_ms = 1000.0 / frequency_hz
                confirm_points = self.confirm_points
                stuck_points = self.stuck_points
                offline_points = self.offline_points
                confirm_seconds = self.confirm_seconds
                stuck_seconds = self.stuck_seconds
                offline_seconds = self.offline_seconds
                offline_timeout_ms = (
                    offline_seconds * 1000.0
                    if self.condition_unit == "seconds"
                    else offline_points * sample_interval_ms
                )

                # 合并数据库规则、派生元数据和默认检测参数。
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
                        "position": sm.get("position") or "-",
                        "physical_quantity": physical_quantity,
                        "unit": unit,
                        "physical_min": range_min,
                        "physical_max": range_max,
                        "confirm_points": confirm_points,
                        "stuck_points": stuck_points,
                        "offline_points": offline_points,
                        "condition_unit": self.condition_unit,
                        "confirm_seconds": confirm_seconds,
                        "stuck_seconds": stuck_seconds,
                        "offline_seconds": offline_seconds,
                        "offline_timeout_ms": offline_timeout_ms,
                        "online_status": "ONLINE",
                        "threshold_streak_count": 0,
                        "threshold_streak_started_at": None,
                        "threshold_condition_active": False,
                        "threshold_condition_level": None,
                        "oor_streak_count": 0,
                        "oor_streak_started_at": None,
                        "oor_condition_active": False,
                        "stuck_streak_count": 0,
                        "stuck_condition_active": False,
                        "offline_condition_active": False,
                    }
                )
                persisted = persisted_states.get(key)
                if persisted:
                    for runtime_field in RUNTIME_STATE_FIELDS:
                        if runtime_field in persisted:
                            d[runtime_field] = persisted[runtime_field]
                new_states[key] = d

            # 完整构建后一次性替换，避免读取到半加载状态。
            self.channel_states.replace_all(new_states)
            if self.persist_state:
                self._daq_condition_active = dict(persisted_daq_states)
                self._daq_state_known = set(persisted_daq_states)
                self.rule_version = load_rule_version(cur)
            self._last_rule_check_monotonic = time.monotonic()
            self.last_reload_time = datetime.now()
            logger.info("Loaded %d threshold rules into memory.", len(new_states))
            return len(new_states)

    def refresh_rules_if_changed(self, force: bool = False) -> bool:
        """Reload threshold rules when the database version changes."""

        if not self.persist_state:
            return False
        now = time.monotonic()
        if (
            not force
            and now - self._last_rule_check_monotonic < self.rule_refresh_seconds
        ):
            return False
        with get_db_cursor() as cur:
            current_version = load_rule_version(cur)
        self._last_rule_check_monotonic = now
        if current_version is None:
            return False
        if self.rule_version is None:
            self.rule_version = current_version
            return False
        if current_version == self.rule_version:
            return False
        logger.info(
            "Threshold rule version changed from %s to %s; reloading.",
            self.rule_version,
            current_version,
        )
        self.reload_thresholds()
        return True

    @staticmethod
    def _parse_frequency_hz(value: Any) -> float:
        """解析 ``t_sensor.frequency``，例如 ``100`` 或 ``1/60``。"""
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
        """根据秒数或采样点数计算离线超时时间。"""
        configured = rule.get("offline_timeout_ms")
        if configured is not None:
            return max(1.0, float(configured))
        if rule.get("condition_unit") == "seconds":
            return max(1.0, float(rule.get("offline_seconds") or 5.0) * 1000.0)
        interval_ms = float(
            rule.get("sample_interval_ms")
            or 1000.0 / cls._parse_frequency_hz(rule.get("frequency"))
        )
        return max(1, int(rule.get("offline_points") or 5)) * interval_ms

    # ------------------------------------------------------------------
    # 阈值、量程和死值连续命中判定
    # ------------------------------------------------------------------
    def _evaluate_threshold_streak(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        previous_count: Any,
        target_points: int,
        target_seconds: Optional[float] = None,
        previous_started_at: Any = None,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[
        int,
        Optional[datetime],
        bool,
        Optional[Tuple[int, float, float, datetime]],
    ]:
        """结合历史状态判断连续阈值超限方向。

        ``previous_*`` 来自上一轮扫描保存在 channel_states 的状态，
        ``samples`` 是本轮新到达的样本（按时间升序），因此连续命中
        可以跨扫描轮次累计；后端重启后这些状态清零，需重新累计。

        逐点处理规则：
          - 与上一点间隔超过 ``max_gap_ms`` -> 视为采集中断，本段连续命中作废；
          - 样本未命中任何级别 -> 方向、计数和起点全部清零；
          - 方向与上一点相同 -> ``count += 1``，起点保持首次命中的时间；
          - 方向翻转 -> 以当前点作为新一段的起点，count 重新从 1 开始；
          - 同时记录这一段里最严重（level 数字最小）的命中，供写告警使用。

        达到条件即置 ``active=True``：``target_seconds`` 不为空时按时间跨度
        （``sample_time - started_at``）判断，为空时按点数 ``count >= target_points``
        判断，两者互斥。返回这一段连续命中中最严重的一次快照
        ``(level, value, limit, fire_time)``。调用方再用 active 的状态变化
        和级别是否升级决定要不要写 t_alert_record。
        """
        count = max(0, int(previous_count or 0))
        started_at = self._parse_maybe_dt(previous_started_at)
        active = False
        snapshot: Optional[Tuple[int, float, float, datetime]] = None
        run_best_level: Optional[int] = None
        run_best_value: Optional[float] = None
        run_best_limit: Optional[float] = None
        run_best_time: Optional[datetime] = None
        last_sample_time = self._parse_maybe_dt(previous_sample_time)

        for sample_time, value in samples:
            # 采样中断时结束当前连续命中，不能继续累计。
            if (
                max_gap_ms is not None
                and last_sample_time is not None
                and (sample_time - last_sample_time).total_seconds() * 1000.0
                > max_gap_ms
            ):
                count = 0
                started_at = None
                run_best_level = None
                run_best_value = None
                run_best_limit = None
                run_best_time = None
            last_sample_time = sample_time
            level, eval_value, limit = self._sample_level(
                rule,
                value,
            )
            if level is None:
                # 正常样本会清空所有阈值连续命中状态。
                count = 0
                started_at = None
                run_best_level = None
                run_best_value = None
                run_best_limit = None
                run_best_time = None
                continue
            count += 1
            if started_at is None:
                started_at = sample_time
            if run_best_level is None or level < run_best_level:
                # 记录本轮连续命中中出现的最严重级别。
                run_best_level = level
                run_best_value = eval_value
                run_best_limit = float(limit or eval_value)
                run_best_time = sample_time
            duration_reached = (
                started_at is not None
                and target_seconds is not None
                and (sample_time - started_at).total_seconds() >= target_seconds
            )
            if (target_seconds is not None and duration_reached) or (
                target_seconds is None and count >= target_points
            ):
                # 达到配置条件后返回最严重的一次命中快照。
                active = True
                snapshot = (
                    run_best_level or level,
                    run_best_value if run_best_value is not None else eval_value,
                    run_best_limit if run_best_limit is not None else eval_value,
                    run_best_time or sample_time,
                )

        return count, started_at, active, snapshot

    @staticmethod
    def _is_out_of_range(
        rule: Dict[str, Any],
        value: float,
    ) -> bool:
        physical_max = rule.get("physical_max")
        physical_min = rule.get("physical_min")
        upper_exceeded = physical_max is not None and value > float(physical_max)
        lower_exceeded = physical_min is not None and value < float(physical_min)
        return bool(upper_exceeded or lower_exceeded)

    def _evaluate_out_of_range_streak(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        previous_count: Any,
        target_points: int,
        target_seconds: Optional[float] = None,
        previous_started_at: Any = None,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[
        int,
        Optional[datetime],
        bool,
        Optional[Tuple[float, datetime]],
    ]:
        """判断连续超出物理量程的样本。"""
        count = max(0, int(previous_count or 0))
        started_at = self._parse_maybe_dt(previous_started_at)
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
                count = 0
                started_at = None
            last_sample_time = sample_time
            if not self._is_out_of_range(rule, value):
                # 样本回到物理量程内时清零连续状态。
                count = 0
                started_at = None
                continue
            count += 1
            if started_at is None:
                started_at = sample_time
            duration_reached = (
                started_at is not None
                and target_seconds is not None
                and (sample_time - started_at).total_seconds() >= target_seconds
            )
            if (target_seconds is not None and duration_reached) or (
                target_seconds is None and count >= target_points
            ):
                active = True
                snapshot = (value, sample_time)

        return count, started_at, active, snapshot

    @staticmethod
    def _evaluate_stuck_streak(
        samples: List[Tuple[datetime, float]],
        previous_value: Any,
        previous_count: Any,
        previous_since: Any,
        target_points: int,
        target_seconds: Optional[float] = None,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[int, Optional[float], Optional[datetime], bool, Optional[Tuple[float, datetime]]]:
        """判断相同读数是否连续达到点数或秒数条件。"""
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
                # 使用极小误差，把数据库浮点舍入差异视为相同值。
                count += 1
            else:
                count = 1
                since = sample_time
            last_value = value
            duration_reached = (
                since is not None
                and target_seconds is not None
                and (sample_time - since).total_seconds() >= target_seconds
            )
            if (target_seconds is not None and duration_reached) or (
                target_seconds is None and count >= target_points
            ):
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
        """解析传感器元数据中的常见量程文本。"""
        if not range_text:
            return None, None
        text = str(range_text).strip()
        # 提取数值前先统一常见分隔符和符号。
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
            # 将“±10”这类对称量程转换为 [-10, 10]。
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
            # 将以 g 表示的加速度量程转换为 m/s^2。
            lower *= 9.80665
            upper *= 9.80665
        return round(lower, 5), round(upper, 5)

    @staticmethod
    def _range_segment_for_channel(
        range_text: str,
        physical_quantity: str,
        channel: str,
    ) -> Optional[str]:
        """为多物理量传感器选择当前通道对应的量程片段。"""
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
            # 优先选择物理量和单位与当前通道匹配的片段。
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
        """通道元数据缺少单位时推断展示单位。"""
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
        """为预警记录补充传感器、站点、单位和级别展示信息。"""
        d = serialize_row(row)
        sno = d.get("sensor_no") or ""
        sno_l = sno.strip().lower()
        ch = (d.get("channel") or "").strip()
        key = self.make_key(sno_l, ch)
        rule = self.channel_states.get(key) or {}
        sm = self.sensor_meta.get(sno_l) or {}

        # 采集仪预警使用采集仪元数据，通道预警使用规则元数据。
        if ch == "__DAQ__" or d.get("alert_type") == ALERT_TYPE_DAQ_OFFLINE:
            inst = self.instrument_meta.get(sno) or {}
            sensor_name = inst.get("name") or f"采集仪({sno[:8]})"
            station_name = inst.get("station_name") or self.station_name
            physical_quantity = "采集仪连通状态"
            unit = "ms"
            position = inst.get("box_name") or "机柜层"
        else:
            sensor_name = d.get("sensor_name") or rule.get("sensor_name") or sm.get("sensor_name") or sno
            station_name = (
                d.get("station_name")
                or rule.get("station_name")
                or sm.get("station_name")
                or self.station_name
            )
            physical_quantity = d.get("physical_quantity") or rule.get("physical_quantity") or ch or "监测物理量"
            unit = d.get("unit") or rule.get("unit") or self._infer_unit(physical_quantity, ch)
            position = sm.get("position") or rule.get("position") or "-"

        # 为前端和 API 增加级别名称、颜色和可排序严重程度。
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

    # ------------------------------------------------------------------
    # 预警事件持久化和活动预警状态
    # ------------------------------------------------------------------
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
        """新增或刷新未处理预警事件。

        同键（sensor_no + channel + alert_type）且 ``end_time IS NULL``
        的记录视为同一条活动事件：

        - 已存在：执行 UPDATE，seq 重新取号、alert_level 取更严重的一侧
          （``LEAST``，1 级最严重），不新增行；
        - 不存在：INSERT 一条 status=0 的新事件，并分配新的轮询 seq。

        查询使用 ``FOR UPDATE`` 兜住并发实例同时写入的情况；
        正常情况下调用方已通过 active_map 在内存里去重。
        """
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
              AND end_time IS NULL
            ORDER BY seq DESC
            LIMIT 1
            FOR UPDATE
            """,
            (sensor_no.lower(), channel or "", alert_type),
        )
        existing = cur.fetchone()
        if existing:
            # 同一预警键只保留一个活动事件，并按需升级或刷新。
            cur.execute(
                """
                UPDATE public.t_alert_record
                SET seq = nextval('public.seq_alert_record'),
                    alert_category = %s,
                    threshold_id = COALESCE(%s, threshold_id),
                    alert_level = LEAST(alert_level, %s),
                    trigger_val = %s
                WHERE id = %s
                RETURNING *
                """,
                (alert_category, threshold_id, alert_level, trigger_val, existing["id"]),
            )
        else:
            # 新建事件，并分配新的轮询序列号。
            cur.execute(
                """
                INSERT INTO public.t_alert_record (
                    id, seq, sensor_no, channel, threshold_id,
                    alert_type, alert_category, alert_level,
                    trigger_val, create_time, alert_status, end_time
                ) VALUES (
                    %s, nextval('public.seq_alert_record'), %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, 0, NULL
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

    def _close_alert_incident(
        self,
        cur: Any,
        active_map: Dict[Tuple[str, str, str], Dict[str, Any]],
        *,
        sensor_no: str,
        channel: str,
        alert_type: str,
        end_time: datetime,
    ) -> Optional[Dict[str, Any]]:
        """Close the open alert incident when its condition recovers."""

        key = self._alert_key(sensor_no, channel, alert_type)
        existing = active_map.pop(key, None)
        alert_id = existing.get("id") if existing else None
        if alert_id is None:
            cur.execute(
                """
                SELECT id
                FROM public.t_alert_record
                WHERE LOWER(sensor_no) = %s
                  AND COALESCE(channel, '') = %s
                  AND alert_type = %s
                  AND end_time IS NULL
                ORDER BY seq DESC
                LIMIT 1
                FOR UPDATE
                """,
                (sensor_no.lower(), channel or "", alert_type),
            )
            row = cur.fetchone()
            alert_id = row["id"] if row else None
        if alert_id is None:
            return None
        cur.execute(
            """
            UPDATE public.t_alert_record
            SET end_time = %s
            WHERE id = %s
              AND end_time IS NULL
            RETURNING *
            """,
            (end_time, alert_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def _parse_maybe_dt(value: Any) -> Optional[datetime]:
        """解析数据库、JSON 或序列化状态中的日期时间。"""
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
        """生成用于内存去重的活动预警唯一键。"""
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
    ) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """返回单个评估值命中的最严重阈值级别。"""
        for level in (1, 2, 3):
            upper = rule.get(f"level{level}_upper")
            if upper is not None and eval_upper > float(upper):
                return level, eval_upper, float(upper)
            lower = rule.get(f"level{level}_lower")
            if lower is not None and eval_lower < float(lower):
                return level, eval_lower, float(lower)
        return None, None, None

    def _sample_level(
        self,
        rule: Dict[str, Any],
        value: float,
    ) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """评估单个样本，并对振动通道去除直流分量。"""
        return self._level_for(rule, value, value)

    @staticmethod
    def _max_sample_gap_ms(samples: List[Tuple[datetime, float]]) -> float:
        """返回相邻样本之间的最大时间间隔。"""
        return max(
            (
                (current[0] - previous[0]).total_seconds() * 1000.0
                for previous, current in zip(samples, samples[1:], strict=False)
            ),
            default=0.0,
        )

    def _load_active_alerts(self, cur: Any) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
        """按传感器、通道和预警类型加载未处理预警。

        每个扫描轮次只查一次，结果作为本轮内存去重表：
          - 键为 ``(sensor_no_lower, channel, alert_type)``；
          - 条件成立且键已存在时不再 INSERT，避免每秒扫描都产生新行；
          - 本轮新写入或升级的事件会回填到同一个 dict，
            同一轮内后续判定直接命中该键，不会重复写库。
        """
        cur.execute(
            """
            SELECT *
            FROM public.t_alert_record
            WHERE end_time IS NULL
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
        force_refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """条件成立时写入预警，已存在同键活动事件时避免重复创建。

        判定顺序：
          1. 条件未成立（active=False）或该类预警开关关闭 -> 直接返回 None；
          2. 已存在同键活动事件且新级别不更严重（级别数字 >= 原级别）
             -> 返回原事件，不写库；
          3. 其余情况（首次命中、级别升级）-> 交给 _upsert_alert_incident()
             执行 INSERT 或 UPDATE。

        写库成功后把事件回填到 active_map 并追加到 triggered，
        triggered 只用于接口展示和日志，不会重复写库。
        """
        if not active or not self.is_alert_type_enabled(alert_type):
            return None
        key = self._alert_key(sensor_no, channel, alert_type)
        existing = active_map.get(key)
        if existing is not None:
            existing_level = int(existing.get("alert_level") or 99)
            if alert_level >= existing_level and not force_refresh:
                return existing
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

    def _apply_offline_if_due(
        self,
        cur: Any,
        active_map: Dict[Tuple[str, str, str], Dict[str, Any]],
        triggered: List[Dict[str, Any]],
        offline_by_instrument: Dict[str, Set[str]],
        pending_state_updates: Dict[str, Dict[str, Any]],
        *,
        rule: Dict[str, Any],
        key: str,
        window_end: datetime,
    ) -> bool:
        """超过离线超时时间仍无样本时创建离线预警。"""
        last_seen = self._parse_maybe_dt(rule.get("last_ts"))
        silence_ms = (
            float("inf")
            if last_seen is None
            else (window_end - last_seen).total_seconds() * 1000.0
        )
        if silence_ms < self._offline_timeout_ms(rule):
            return False

        # 记录离线通道，供后续采集仪级离线聚合使用。
        instrument_id = (rule.get("instrument_id") or "").strip()
        if instrument_id:
            offline_by_instrument.setdefault(instrument_id, set()).add(key)

        for alert_type, state_field in (
            (ALERT_TYPE_THRESHOLD, "threshold_condition_active"),
            (ALERT_TYPE_STUCK, "stuck_condition_active"),
            (ALERT_TYPE_OOR, "oor_condition_active"),
        ):
            if bool(rule.get(state_field)):
                self._close_alert_incident(
                    cur,
                    active_map,
                    sensor_no=rule["sensor_no_lower"],
                    channel=rule.get("channel") or "",
                    alert_type=alert_type,
                    end_time=window_end,
                )

        if not bool(rule.get("offline_condition_active")):
            # 同一个连续离线周期只创建一个离线事件。
            self._apply_condition(
                cur,
                active_map,
                triggered,
                rule=rule,
                sensor_no=rule["sensor_no_lower"],
                channel=rule.get("channel") or "",
                alert_type=ALERT_TYPE_OFFLINE,
                active=True,
                alert_level=NON_THRESHOLD_ALERT_LEVEL,
                trigger_val=0.0,
                fire_time=window_end,
                force_refresh=True,
            )
        pending_state_updates.setdefault(key, {}).update(
            {
                # 离线状态会覆盖阈值、量程和死值连续状态。
                "online_status": "OFFLINE",
                "stuck_since": None,
                "stuck_streak_count": 0,
                "threshold_streak_count": 0,
                "threshold_streak_started_at": None,
                "oor_streak_count": 0,
                "oor_streak_started_at": None,
                "threshold_condition_active": False,
                "threshold_condition_level": None,
                "stuck_condition_active": False,
                "oor_condition_active": False,
                "offline_condition_active": True,
            }
        )
        return True

    @classmethod
    def _aggregate_samples(
        cls,
        samples: List[Tuple[datetime, float]],
    ) -> Dict[str, Any]:
        """将原始样本批次聚合成与 SQL 查询一致的数据结构。"""
        values = [value for _, value in samples]
        val_max = max(values)
        val_min = min(values)
        return {
            "sample_cnt": len(samples),
            "val_max": val_max,
            "val_min": val_min,
            "val_avg": sum(values) / len(values),
            "value_std": 0.0,
            "first_ts": samples[0][0],
            "last_ts": samples[-1][0],
            "max_gap_ms": cls._max_sample_gap_ms(samples),
            "min_time": next(
                sample_time
                for sample_time, value in samples
                if value == val_min
            ),
            "max_time": next(
                sample_time
                for sample_time, value in samples
                if value == val_max
            ),
        }

    def _run_detection_window(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str,
        *,
        write_statistics: bool = True,
        detect_alerts: bool = True,
        new_samples_only: bool = False,
    ) -> Dict[str, Any]:
        """在一个窗口中执行统计写入和预警检测。

        三种调用方式共用同一套逻辑，靠三个开关区分：

        - ``scan_realtime_alerts()``：write_statistics=False、
          detect_alerts=True、new_samples_only=True，只处理新到样本并写预警；
        - ``record_statistics_window()``：write_statistics=True、
          detect_alerts=False，只做 SQL 聚合并写 t_stat_record；
        - ``step_detection()``：两者都为 True，用于人工补算/联调。

        窗口是左闭右开的 ``[window_start, window_end)``。整个方法在一个
        数据库事务里完成“读样本 -> 判定 -> 写预警/统计”，事务提交后才把
        新的连续命中状态发布到 ``channel_states``，避免写库失败但内存已推进。
        """
        # 以下累加结果与预警写入在同一事务中提交。
        triggered_alerts: List[Dict[str, Any]] = []
        upserted_stats = 0
        raw_points_processed = 0
        stat_rows: List[Tuple[Any, ...]] = []
        pending_state_updates: Dict[str, Dict[str, Any]] = {}
        daq_condition_updates: Dict[str, bool] = {}

        with get_db_cursor(commit=True) as cur:
            observed_map: Dict[str, Dict[str, Any]] = {}
            rules = self.channel_states.values()
            rules_by_key = {rule["key"]: rule for rule in rules}
            samples_by_key: Dict[str, List[Tuple[datetime, float]]] = {}
            if detect_alerts:
                # 实时预警需要逐点样本；只加载已配置的传感器和通道，
                # 并保持数据库时间顺序。
                # 未在 t_alert_threshold 中配置的组合会被直接丢弃，
                # 因此 raw_points_processed 统计的是“读到的行数”，
                # 不等于参与判定的样本数。
                cur.execute(SAMPLE_BATCH_SQL, (window_start, window_end))
                for row in cur.fetchall():
                    raw_points_processed += 1
                    key = self.make_key(row["s_no"], row["ch"])
                    if key not in rules_by_key:
                        continue
                    samples_by_key.setdefault(key, []).append(
                        (row["time"], float(row["value"]))
                    )
                if new_samples_only:
                    # 回看查询可能返回旧数据，丢弃各通道水位之前或同一时刻的样本。
                    # 水位 last_ts 来自上一轮扫描结束后的 channel_states，
                    # 比较使用严格大于，保证同一个时间戳只被计数一次。
                    # 过滤后没有新样本的通道会从 dict 中移除，
                    # 后续仍会走“无样本 -> 离线判定”的分支。
                    for key, samples in list(samples_by_key.items()):
                        previous_last_seen = self._parse_maybe_dt(
                            rules_by_key[key].get("last_ts")
                        )
                        if previous_last_seen is not None:
                            samples = [
                                sample
                                for sample in samples
                                if sample[0] > previous_last_seen
                            ]
                        if samples:
                            samples_by_key[key] = samples
                        else:
                            del samples_by_key[key]
                # 把逐点样本聚合成与 WINDOW_AGG_SQL 相同的字段结构，
                # 让后续统计和预警逻辑无需区分数据来源。
                observed_map = {
                    key: self._aggregate_samples(samples)
                    for key, samples in samples_by_key.items()
                }
            else:
                # 只做统计时，将聚合计算下推到 PostgreSQL。
                cur.execute(WINDOW_AGG_SQL, (window_start, window_end))
                for row in cur.fetchall():
                    raw_points_processed += int(row["sample_cnt"] or 0)
                    observed_map[self.make_key(row["s_no"], row["ch"])] = row

            # 一次性加载活动预警，重复条件在原事件上更新。
            # 注意这里是“整个表里所有未处理记录”，不按窗口过滤，
            # 所以跨窗口的活动事件也能在同一轮里被识别出来。
            if detect_alerts:
                if self.persist_state and self._active_alert_cache is not None:
                    active_map = {
                        key: dict(value)
                        for key, value in self._active_alert_cache.items()
                    }
                else:
                    active_map = self._load_active_alerts(cur)
            else:
                active_map = {}
            # 采集仪级离线需要知道每台采集仪配置了多少个通道，
            # 只有全部通道都离线时才汇总为一条采集仪离线预警。
            instrument_totals = Counter(
                r.get("instrument_id")
                for r in rules
                if (r.get("instrument_id") or "").strip()
            )
            offline_by_instrument: Dict[str, Set[str]] = {}

            # 每个已配置通道都要评估，包括当前窗口没有数据的通道。
            # 没有样本不代表正常，可能是通道已经离线，必须继续判离线。
            for rule in rules:
                key = rule["key"]
                sensor_no = rule["sensor_no_lower"]
                channel = rule.get("channel") or ""
                obs = observed_map.get(key)
                previous_last_seen = self._parse_maybe_dt(rule.get("last_ts"))
                if obs is None or int(obs["sample_cnt"] or 0) <= 0:
                    if not detect_alerts:
                        continue
                    # 没有样本的通道仍需继续执行离线判定。
                    self._apply_offline_if_due(
                        cur,
                        active_map,
                        triggered_alerts,
                        offline_by_instrument,
                        pending_state_updates,
                        rule=rule,
                        key=key,
                        window_end=window_end,
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
                if write_statistics:
                    # 每个完整统计窗口生成一条 t_stat_record 记录。
                    stat_rows.append(
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
                        )
                    )
                    upserted_stats += 1
                if not detect_alerts:
                    continue

                # 回看重读的重复样本不能推进预警连续状态。
                if (
                    new_samples_only
                    and previous_last_seen is not None
                    and last_ts <= previous_last_seen
                ):
                    self._apply_offline_if_due(
                        cur,
                        active_map,
                        triggered_alerts,
                        offline_by_instrument,
                        pending_state_updates,
                        rule=rule,
                        key=key,
                        window_end=window_end,
                    )
                    continue

                confirm_points = max(1, int(rule.get("confirm_points") or 3))
                stuck_points = max(1, int(rule.get("stuck_points") or 100))
                condition_unit = rule.get("condition_unit", "points")
                confirm_seconds = (
                    float(rule.get("confirm_seconds") or self.confirm_seconds)
                    if condition_unit == "seconds"
                    else None
                )
                stuck_seconds = (
                    float(rule.get("stuck_seconds") or self.stuck_seconds)
                    if condition_unit == "seconds"
                    else None
                )
                samples = samples_by_key.get(key, [])
                if not samples:
                    if new_samples_only:
                        self._apply_offline_if_due(
                            cur,
                            active_map,
                            triggered_alerts,
                            offline_by_instrument,
                            pending_state_updates,
                            rule=rule,
                            key=key,
                            window_end=window_end,
                        )
                        continue
                    samples = [(last_ts, val_avg)]
                # 实时扫描使用最近统计均值作为直流分量基准。

                # 依次评估阈值、物理量程和传感器死值条件。
                # 上下文（上一轮方向、计数、起点、上一轮最后样本时间）
                # 全部来自 channel_states，因此连续命中可以跨扫描轮次累计。
                (
                    threshold_count,
                    threshold_started_at,
                    threshold_active,
                    threshold_snapshot,
                ) = self._evaluate_threshold_streak(
                    samples,
                    rule,
                    rule.get("threshold_streak_count"),
                    confirm_points,
                    target_seconds=confirm_seconds,
                    previous_started_at=rule.get("threshold_streak_started_at"),
                    previous_sample_time=previous_last_seen,
                    # 相邻样本间隔超过 1.5 个采样周期视为断流，
                    # 不再把断流两侧的点算作同一段连续命中。
                    max_gap_ms=float(rule.get("sample_interval_ms") or 1000) * 1.5,
                )
                # 本轮待发布的通道状态。先写入 pending_state_updates，
                # 事务提交成功后才真正更新 channel_states。
                state_updates: Dict[str, Any] = {
                    # last_ts 是下一轮去重的依据，必须取本轮最后一个样本时间。
                    "last_ts": serialize_val(samples[-1][0]),
                    "last_val": round(samples[-1][1], 5),
                    "online_status": "ONLINE",
                    "threshold_streak_count": threshold_count,
                    "threshold_streak_started_at": serialize_val(
                        threshold_started_at
                    ),
                    "threshold_condition_active": bool(threshold_active),
                    "threshold_condition_level": None,
                }
                previous_threshold_active = bool(
                    rule.get("threshold_condition_active")
                )
                if threshold_snapshot is not None:
                    # snapshot 是本段连续命中里最严重的 (级别, 实测值, 阈值, 触发时间)。
                    level, value, limit, fire_time = threshold_snapshot
                    previous_condition_level = rule.get("threshold_condition_level")
                    previous_condition_level_value = (
                        int(previous_condition_level)
                        if previous_condition_level is not None
                        else None
                    )
                    # 记录本段连续命中已经达到过的最严重级别，
                    # 级别从 3 升到 1 时才会再次写库。
                    active_condition_level = (
                        level
                        if previous_condition_level_value is None
                        else min(previous_condition_level_value, level)
                    )
                    state_updates.update(
                        {
                            "threshold_last_level": level,
                            "threshold_last_value": value,
                            "threshold_last_limit": limit,
                            "threshold_condition_level": active_condition_level,
                        }
                    )
                    # 只有以下三种情况才写预警：
                    #   1. 上一轮还没处于命中态（本段第一次达到确认条件）；
                    #   2. 上一轮没有记录级别（升级判定缺少基准）；
                    #   3. 本轮级别比上一轮更严重（数字更小）。
                    # 持续同级别超限不会重复写库，避免每秒扫描产生新行。
                    if (
                        not previous_threshold_active
                        or previous_condition_level_value is None
                        or level < previous_condition_level_value
                    ):
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
                            force_refresh=not previous_threshold_active,
                        )
                if previous_threshold_active and not threshold_active:
                    self._close_alert_incident(
                        cur,
                        active_map,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_THRESHOLD,
                        end_time=window_end,
                    )

                (
                    oor_count,
                    oor_started_at,
                    oor_active,
                    oor_snapshot,
                ) = self._evaluate_out_of_range_streak(
                    samples,
                    rule,
                    rule.get("oor_streak_count"),
                    confirm_points,
                    target_seconds=confirm_seconds,
                    previous_started_at=rule.get("oor_streak_started_at"),
                    previous_sample_time=previous_last_seen,
                    max_gap_ms=float(rule.get("sample_interval_ms") or 1000) * 1.5,
                )
                state_updates.update(
                    {
                        "oor_streak_count": oor_count,
                        "oor_streak_started_at": serialize_val(oor_started_at),
                        "oor_condition_active": bool(oor_active),
                    }
                )
                previous_oor_active = bool(rule.get("oor_condition_active"))
                if oor_snapshot is not None:
                    oor_value, fire_time = oor_snapshot
                    state_updates["oor_last_value"] = oor_value
                    if not previous_oor_active:
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
                            force_refresh=True,
                        )
                if previous_oor_active and not oor_active:
                    self._close_alert_incident(
                        cur,
                        active_map,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_OOR,
                        end_time=window_end,
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
                    target_seconds=stuck_seconds,
                    previous_sample_time=previous_last_seen,
                    max_gap_ms=float(rule.get("sample_interval_ms") or 1000) * 1.5,
                )
                state_updates.update(
                    {
                        "stuck_streak_count": stuck_count,
                        "stuck_last_value": stuck_last_value,
                        "stuck_since": serialize_val(stuck_since) if stuck_since else None,
                        "stuck_condition_active": bool(stuck_active),
                    }
                )
                previous_stuck_active = bool(rule.get("stuck_condition_active"))
                if stuck_snapshot is not None:
                    stuck_value, fire_time = stuck_snapshot
                    if not previous_stuck_active:
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
                            force_refresh=True,
                        )
                if previous_stuck_active and not stuck_active:
                    self._close_alert_incident(
                        cur,
                        active_map,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_STUCK,
                        end_time=window_end,
                    )

                last_sample_time = samples[-1][0]
                silence_ms = (window_end - last_sample_time).total_seconds() * 1000.0
                offline_timeout_ms = self._offline_timeout_ms(rule)
                boundary_gap_ms = (
                    (samples[0][0] - previous_last_seen).total_seconds() * 1000.0
                    if previous_last_seen is not None
                    else 0.0
                )
                largest_gap_ms = max(
                    boundary_gap_ms,
                    self._max_sample_gap_ms(samples),
                )
                currently_offline = silence_ms >= offline_timeout_ms
                offline_condition_hit = (
                    currently_offline or largest_gap_ms >= offline_timeout_ms
                )
                previous_offline_active = bool(
                    rule.get("offline_condition_active")
                )
                state_updates["offline_condition_active"] = bool(currently_offline)
                if currently_offline:
                    instrument_id = (rule.get("instrument_id") or "").strip()
                    if instrument_id:
                        offline_by_instrument.setdefault(
                            instrument_id,
                            set(),
                        ).add(key)
                if offline_condition_hit and not previous_offline_active:
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
                        force_refresh=True,
                    )
                if previous_offline_active and not currently_offline:
                    self._close_alert_incident(
                        cur,
                        active_map,
                        sensor_no=sensor_no,
                        channel=channel,
                        alert_type=ALERT_TYPE_OFFLINE,
                        end_time=window_end,
                    )
                if currently_offline:
                    for alert_type, state_field in (
                        (ALERT_TYPE_THRESHOLD, "threshold_condition_active"),
                        (ALERT_TYPE_STUCK, "stuck_condition_active"),
                        (ALERT_TYPE_OOR, "oor_condition_active"),
                    ):
                        if bool(rule.get(state_field)):
                            self._close_alert_incident(
                                cur,
                                active_map,
                                sensor_no=sensor_no,
                                channel=channel,
                                alert_type=alert_type,
                                end_time=window_end,
                            )
                    state_updates.update(
                        {
                            "online_status": "OFFLINE",
                            "stuck_since": None,
                            "stuck_streak_count": 0,
                            "threshold_streak_count": 0,
                            "threshold_streak_started_at": None,
                            "oor_streak_count": 0,
                            "oor_streak_started_at": None,
                            "threshold_condition_active": False,
                            "threshold_condition_level": None,
                            "stuck_condition_active": False,
                            "oor_condition_active": False,
                        }
                    )

                pending_state_updates.setdefault(key, {}).update(state_updates)

            if self.persist_state:
                state_rows = state_rows_for_persistence(
                    rules_by_key,
                    pending_state_updates,
                    daq_condition_updates,
                )
                persist_channel_states(cur, state_rows)
            if stat_rows:
                # 批量更新插入，确保一个完整窗口在同一事务中提交。
                execute_values(
                    cur,
                    STAT_UPSERT_SQL,
                    stat_rows,
                    template=(
                        "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "'system', NOW())"
                    ),
                    page_size=len(stat_rows),
                )
            if detect_alerts and self.is_alert_type_enabled(
                ALERT_TYPE_DAQ_OFFLINE
            ):
                # 采集仪下全部通道均离线时才汇总为采集仪级离线预警。
                for instrument_id in sorted(instrument_totals):
                    offline_keys = offline_by_instrument.get(instrument_id, set())
                    total_channels = int(instrument_totals.get(instrument_id, 0))
                    if total_channels <= 0 or len(offline_keys) < total_channels:
                        if self._daq_condition_active.get(instrument_id):
                            self._close_alert_incident(
                                cur,
                                active_map,
                                sensor_no=instrument_id,
                                channel="__DAQ__",
                                alert_type=ALERT_TYPE_DAQ_OFFLINE,
                                end_time=window_end,
                            )
                        daq_condition_updates[instrument_id] = False
                        continue
                    if self._daq_condition_active.get(instrument_id):
                        continue
                    daq_key = self._alert_key(
                        instrument_id,
                        "__DAQ__",
                        ALERT_TYPE_DAQ_OFFLINE,
                    )
                    if (
                        daq_key in active_map
                        and instrument_id not in self._daq_state_known
                    ):
                        daq_condition_updates[instrument_id] = True
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
                    active_map[daq_key] = daq_alert
                    triggered_alerts.append(daq_alert)
                    daq_condition_updates[instrument_id] = True

        # 仅在数据库事务成功后发布新的运行状态。
        # 这一步是“状态在内存里”的落点：下一轮扫描通过 channel_states
        # 读取 last_ts、连续命中计数和 condition_active，继续累计。
        for key, updates in pending_state_updates.items():
            self.channel_states.update_field(key, **updates)
        # 采集仪级离线状态按采集仪维度单独维护，没有对应的通道 key。
        self._daq_condition_active.update(daq_condition_updates)
        self._daq_state_known.update(daq_condition_updates)
        if detect_alerts:
            self._active_alert_cache = {
                key: dict(value) for key, value in active_map.items()
            }

        return {
            "windowStart": serialize_val(window_start),
            "windowEnd": serialize_val(window_end),
            "periodType": period_type,
            "rawPointsProcessed": raw_points_processed,
            "upsertedStatsCount": upserted_stats,
            "triggeredAlertsCount": len(triggered_alerts),
            "triggeredAlerts": triggered_alerts[:10],
        }

    def scan_realtime_alerts(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> Dict[str, Any]:
        """只对新到达的原始样本执行实时预警检测。

        生产环境由 AlertDetectionWorker 的实时线程按
        ``DETECTION_INTERVAL_SECONDS`` 调用；窗口由 Worker 计算，
        这里只负责：

        - ``write_statistics=False``：不写 t_stat_record，统计由另一条线程负责；
        - ``new_samples_only=True``：按通道 last_ts 丢弃回看带来的重复样本；
        - 用 ``_alert_step_lock`` 串行化，避免手工调用和后台线程同时改内存状态。
        """
        with self._alert_step_lock:
            t0 = time.perf_counter()
            if window_end <= window_start:
                raise ValueError("window_end must be later than window_start")
            result = self._run_detection_window(
                window_start,
                window_end,
                "realtime",
                write_statistics=False,
                detect_alerts=True,
                new_samples_only=True,
            )
            return {
                **result,
                "subWindowCount": 1,
                "elapsedMs": round((time.perf_counter() - t0) * 1000, 2),
            }

    def record_statistics_window(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str = "1m",
    ) -> Dict[str, Any]:
        """只持久化一个完整统计窗口，不执行预警检测。"""
        with self._statistics_step_lock:
            t0 = time.perf_counter()
            if window_end <= window_start:
                raise ValueError("window_end must be later than window_start")
            result = self._run_detection_window(
                window_start,
                window_end,
                period_type,
                write_statistics=True,
                detect_alerts=False,
            )
            return {
                **result,
                "subWindowCount": 1,
                "elapsedMs": round((time.perf_counter() - t0) * 1000, 2),
            }

    def step_detection(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str = "1m",
    ) -> Dict[str, Any]:
        """人工工作流中同时执行统计和预警检测。"""
        with self._alert_step_lock, self._statistics_step_lock:
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
        """返回引擎计数和当前连续命中单位配置。"""
        rules = self.channel_states.values()
        online_cnt = sum(1 for r in rules if r.get("online_status") == "ONLINE")
        offline_cnt = len(rules) - online_cnt
        return {
            "loadedRulesCount": len(rules),
            "onlineChannels": online_cnt,
            "offlineChannels": offline_cnt,
            "totalMonitorRows": self.total_monitor_rows,
            "lastReloadTime": serialize_val(self.last_reload_time),
            "detectionMode": "realtime_sample_groups",
            "conditionUnit": self.condition_unit,
            "stationName": self.station_name,
            "stationCode": self.station_code,
            "alertTypeSwitches": dict(self.alert_type_switches),
            "persistState": self.persist_state,
            "ruleVersion": self.rule_version,
            "ruleRefreshSeconds": self.rule_refresh_seconds,
            "confirmPoints": self.confirm_points,
            "stuckPoints": self.stuck_points,
            "offlinePoints": self.offline_points,
            "confirmSeconds": self.confirm_seconds,
            "stuckSeconds": self.stuck_seconds,
            "offlineSeconds": self.offline_seconds,
        }

