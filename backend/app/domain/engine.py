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
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from psycopg2.extras import execute_values

from app.core.serialization import serialize_row, serialize_val
from app.domain.alert_rules import (
    AlertRuleRegistry,
    DaqOfflineRule,
    OfflineAlertRule,
    OutOfRangeAlertRule,
    StuckAlertRule,
    ThresholdAlertRule,
)
from app.domain.alert_rules.base import (
    MIN_CONFIRM_SAMPLES,
    AlertRule,
    RuleContext,
    RuleEvaluation,
    max_sample_gap_ms,
    parse_datetime,
    required_sample_count,
    serialize_runtime_datetime,
)
from app.domain.alert_types import (
    ALERT_CATEGORY_LABELS,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
    NON_THRESHOLD_ALERT_LEVEL,
    alert_category_for_type,
    normalize_alert_type,
)
from app.infrastructure.database import get_db_cursor
from app.repositories.detection_state import load_rule_version

logger = logging.getLogger("alert_analysis.engine")

RUNTIME_STATE_FIELDS: Tuple[str, ...] = (
    "last_ts",
    "last_val",
    "online_status",
    "threshold_streak_count",
    "threshold_streak_started_at",
    "threshold_last_level",
    "threshold_last_value",
    "threshold_last_limit",
    "oor_streak_count",
    "oor_streak_started_at",
    "oor_last_value",
    "stuck_streak_count",
    "stuck_last_value",
    "stuck_since",
    "threshold_condition_active",
    "threshold_condition_level",
    "oor_condition_active",
    "stuck_condition_active",
    "offline_condition_active",
)

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
#   - 不聚合，保留每个点的时间，才能按连续点数和持续时长判断；
#   - value 为空的行直接排除，不参与阈值、量程和死值判定；
#   - 排序由数据库保证，下游 _evaluate_*_streak() 依赖这个顺序累计状态。
SAMPLE_BATCH_SQL = """
SELECT
    LOWER(sensor_no) AS s_no,
    COALESCE(channel, '') AS ch,
    COUNT(*) AS sample_cnt,
    ARRAY_AGG(time ORDER BY time) AS sample_times,
    ARRAY_AGG(value::double precision ORDER BY time) AS sample_values
FROM public.t_monitor
WHERE time >= %s AND time < %s
  AND value IS NOT NULL
  AND (LOWER(sensor_no), COALESCE(channel, '')) IN ({targets})
GROUP BY LOWER(sensor_no), COALESCE(channel, '')
ORDER BY s_no, ch
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
        confirm_seconds: float = 3.0,
        stuck_seconds: float = 100.0,
        offline_seconds: float = 5.0,
        station_name: str = "玉环站",
        alert_type_switches: Optional[Dict[str, bool]] = None,
        rule_refresh_seconds: float = 5.0,
        channel_rules: Optional[Iterable[AlertRule]] = None,
    ) -> None:
        # 检测、接口补充信息和总览统计共用这些元数据。
        self.channel_states = ConcurrentDict()
        self.sensor_meta: Dict[str, Dict[str, Any]] = {}
        self.instrument_meta: Dict[str, Dict[str, Any]] = {}
        self._daq_condition_active: Dict[str, bool] = {}
        self._daq_state_known: Set[str] = set()
        self.total_monitor_rows: int = 398059510
        self.last_reload_time: Optional[datetime] = None
        self.confirm_seconds = max(0.001, float(confirm_seconds))
        self.stuck_seconds = max(0.001, float(stuck_seconds))
        self.offline_seconds = max(0.001, float(offline_seconds))
        self.station_name = (station_name or "").strip() or "玉环站"
        self.station_code: Optional[str] = None
        self.detection_started_at: Optional[datetime] = None
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
        self._sample_query: Optional[Tuple[str, Tuple[Any, ...]]] = None
        self._threshold_rule = ThresholdAlertRule()
        self._out_of_range_rule = OutOfRangeAlertRule()
        self._stuck_rule = StuckAlertRule()
        self._offline_rule = OfflineAlertRule()
        self._daq_offline_rule = DaqOfflineRule()
        self.channel_rule_registry = AlertRuleRegistry(channel_rules)

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
        """加载规则和元数据，不恢复任何上次进程的运行状态。"""
        self.reload_thresholds()
        self.refresh_monitor_row_count()

    def begin_detection_session(self, started_at: datetime) -> None:
        """以启动时间为新基线重置内存状态。

        启动前未结束的预警统一在启动边界结束，避免旧事件与本次启动后的
        连续命中状态、去重缓存发生关联。
        """
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                """
                UPDATE public.t_alert_record
                SET end_time = %s
                WHERE end_time IS NULL
                """,
                (started_at,),
            )
            closed = int(cur.rowcount or 0)

        for rule in self.channel_states.values():
            self.channel_states.update_field(
                rule["key"],
                last_ts=self._serialize_runtime_dt(started_at),
                last_val=None,
                online_status="ONLINE",
                threshold_streak_count=0,
                threshold_streak_started_at=None,
                threshold_last_level=None,
                threshold_last_value=None,
                threshold_last_limit=None,
                threshold_condition_active=False,
                threshold_condition_level=None,
                oor_streak_count=0,
                oor_streak_started_at=None,
                oor_last_value=None,
                oor_condition_active=False,
                stuck_streak_count=0,
                stuck_last_value=None,
                stuck_since=None,
                stuck_condition_active=False,
                offline_condition_active=False,
            )
        self._daq_condition_active = {}
        self._daq_state_known = set()
        self._active_alert_cache = {}
        self.detection_started_at = started_at
        if closed:
            logger.info(
                "Closed %d open alert incidents at detection session boundary.",
                closed,
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
        """将配置的站点名称解析为对应的站点编码。"""
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
            cur.execute("SELECT LOCALTIMESTAMP AS now")
            runtime_now = cur.fetchone()["now"]
            # 1. 加载采集仪与站点映射。
            station_code = self._resolve_station_code(cur)
            self.station_code = station_code
            previous_states = {
                rule["key"]: rule for rule in self.channel_states.values()
            }
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
                confirm_seconds = self.confirm_seconds
                threshold_confirm_samples = (
                    required_sample_count(
                        frequency_hz,
                        confirm_seconds,
                    )
                )
                stuck_seconds = self.stuck_seconds
                stuck_confirm_samples = required_sample_count(
                    frequency_hz,
                    stuck_seconds,
                )
                oor_confirm_samples = required_sample_count(
                    frequency_hz,
                    confirm_seconds,
                )
                offline_seconds = self.offline_seconds
                offline_confirm_periods = required_sample_count(
                    frequency_hz,
                    offline_seconds,
                )
                offline_timeout_ms = max(
                    1.0,
                    offline_confirm_periods / frequency_hz * 1000.0,
                )

                # 合并数据库规则、派生元数据和默认检测参数。
                d.update(
                    {
                        "key": key,                                             # 通道唯一标识，格式为“传感器编号:通道”
                        "sensor_no_lower": sno_l,                               # 小写传感器编号，用于统一匹配
                        "sensor_name": sname,                                   # 传感器显示名称
                        "station_name": st_name,                                # 所属站点名称
                        "station_code": st_code,                                # 所属站点编码
                        "instrument_id": sm.get("instrument_id"),               # 采集仪主键
                        "instrument_name": sm.get("instrument_name") or "-",    # 采集仪名称，空值使用占位符
                        "range": sm.get("range") or "-",                        # 量程文本，空值使用占位符
                        "frequency": str(frequency_text),                       # 采样频率文本
                        "frequency_hz": frequency_hz,                           # 采样频率（Hz）
                        "sample_interval_ms": sample_interval_ms,               # 采样间隔（毫秒）
                        "position": sm.get("position") or "-",                  # 安装位置，空值使用占位符
                        "physical_quantity": physical_quantity,                 # 物理量名称
                        "unit": unit,                                           # 计量单位
                        "physical_min": range_min,                              # 物理量程下限
                        "physical_max": range_max,                              # 物理量程上限
                        "confirm_seconds": confirm_seconds,                     # 阈值确认基准时长（秒）
                        "threshold_confirm_samples": threshold_confirm_samples, # 按频率折算的阈值连续点数
                        "stuck_seconds": stuck_seconds,                         # 死值连续确认时长（秒）
                        "stuck_confirm_samples": stuck_confirm_samples,         # 按频率折算的死值连续点数
                        "oor_confirm_samples": oor_confirm_samples,             # 按频率折算的超量程连续点数
                        "offline_seconds": offline_seconds,                     # 离线确认基准时长（秒）
                        "offline_confirm_periods": offline_confirm_periods,     # 按频率折算的离线缺失周期数
                        "offline_timeout_ms": offline_timeout_ms,               # 离线超时阈值（毫秒）
                        "online_status": "ONLINE",                              # 初始在线状态
                        "last_ts": self._serialize_runtime_dt(runtime_now),     # 新规则的离线判断起点
                        "threshold_streak_count": 0,                            # 阈值连续命中计数
                        "threshold_streak_started_at": None,                    # 阈值连续命中开始时间
                        "threshold_condition_active": False,                    # 阈值预警条件是否已触发
                        "threshold_condition_level": None,                      # 当前阈值预警等级
                        "oor_streak_count": 0,                                  # 超量程连续命中计数
                        "oor_streak_started_at": None,                          # 超量程连续命中开始时间
                        "oor_condition_active": False,                          # 超量程预警条件是否已触发
                        "stuck_streak_count": 0,                                # 死值连续命中计数
                        "stuck_condition_active": False,                        # 死值预警条件是否已触发
                        "offline_condition_active": False,                      # 离线预警条件是否已触发
                    }
                )
                previous = previous_states.get(key)
                if previous:
                    for runtime_field in RUNTIME_STATE_FIELDS:
                        if runtime_field in previous:
                            d[runtime_field] = previous[runtime_field]
                new_states[key] = d

            # 完整构建后一次性替换，避免读取到半加载状态。
            self.channel_states.replace_all(new_states)
            self._cache_sample_query(new_states.values())
            self.rule_version = load_rule_version(cur)
            self._last_rule_check_monotonic = time.monotonic()
            self.last_reload_time = datetime.now()
            logger.info("Loaded %d threshold rules into memory.", len(new_states))
            return len(new_states)

    def _cache_sample_query(self, rules: Iterable[Dict[str, Any]]) -> None:
        """缓存实时扫描使用的已配置通道过滤条件。"""
        targets = [
            (rule["sensor_no_lower"], rule.get("channel") or "")
            for rule in rules
        ]
        if not targets:
            self._sample_query = None
            return
        placeholders = ", ".join(["(%s, %s)"] * len(targets))
        sample_sql = SAMPLE_BATCH_SQL.format(targets=placeholders)
        params: List[Any] = []
        for sensor_no, channel in targets:
            params.extend([sensor_no, channel])
        self._sample_query = (sample_sql, tuple(params))

    def refresh_rules_if_changed(self, force: bool = False) -> bool:
        """数据库中的规则版本变化时重新加载阈值规则。"""

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
        """按配置时长和采样频率返回离线超时时间，最少 3 个采样周期。"""
        configured = rule.get("offline_timeout_ms")
        if configured is not None:
            return max(1.0, float(configured))
        frequency_hz = cls._parse_frequency_hz(
            rule.get("frequency_hz") or rule.get("frequency") or 1
        )
        offline_seconds = float(rule.get("offline_seconds") or 5.0)
        missing_periods = required_sample_count(
            frequency_hz,
            offline_seconds,
        )
        return max(1.0, missing_periods / frequency_hz * 1000.0)

    # ------------------------------------------------------------------
    # 阈值、量程和死值连续命中判定
    # ------------------------------------------------------------------
    def _evaluate_threshold_streak(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        previous_count: Any,
        target_seconds: float,
        previous_started_at: Any = None,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[
        int,
        Optional[datetime],
        bool,
        Optional[Tuple[int, float, float, datetime]],
    ]:
        """兼容旧调用入口，实际逻辑由 ThresholdAlertRule 提供。"""
        target_count = required_sample_count(
            rule.get("frequency_hz") or rule.get("frequency"),
            target_seconds,
        )
        return self._threshold_rule.evaluate_streak(
            samples,
            rule,
            previous_count,
            target_count,
            previous_started_at=previous_started_at,
            previous_sample_time=previous_sample_time,
            max_gap_ms=max_gap_ms,
        )

    @staticmethod
    def _is_out_of_range(
        rule: Dict[str, Any],
        value: float,
    ) -> bool:
        return OutOfRangeAlertRule.is_out_of_range(rule, value)

    def _evaluate_out_of_range_streak(
        self,
        samples: List[Tuple[datetime, float]],
        rule: Dict[str, Any],
        previous_count: Any,
        target_seconds: float,
        previous_started_at: Any = None,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
    ) -> Tuple[
        int,
        Optional[datetime],
        bool,
        Optional[Tuple[float, datetime]],
    ]:
        """兼容旧调用入口，实际逻辑由 OutOfRangeAlertRule 提供。"""
        target_count = required_sample_count(
            rule.get("frequency_hz") or rule.get("frequency"),
            target_seconds,
        )
        return self._out_of_range_rule.evaluate_streak(
            samples,
            rule,
            previous_count,
            target_count,
            previous_started_at=previous_started_at,
            previous_sample_time=previous_sample_time,
            max_gap_ms=max_gap_ms,
        )

    @staticmethod
    def _evaluate_stuck_streak(
        samples: List[Tuple[datetime, float]],
        previous_value: Any,
        previous_count: Any,
        previous_since: Any,
        target_seconds: float,
        previous_sample_time: Any = None,
        max_gap_ms: Optional[float] = None,
        frequency_hz: Any = 1.0,
    ) -> Tuple[int, Optional[float], Optional[datetime], bool, Optional[Tuple[float, datetime]]]:
        """兼容旧调用入口，实际逻辑由 StuckAlertRule 提供。"""
        target_count = required_sample_count(
            frequency_hz,
            target_seconds,
        )
        return StuckAlertRule.evaluate_streak(
            samples,
            previous_value,
            previous_count,
            previous_since,
            target_count,
            previous_sample_time=previous_sample_time,
            max_gap_ms=max_gap_ms,
        )

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
        """预警条件恢复后关闭对应的未结束预警事件。"""

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
        return parse_datetime(value)

    @staticmethod
    def _serialize_runtime_dt(value: datetime) -> str:
        """保留运行时时间戳的完整微秒精度，供下一轮严格去重。"""
        return serialize_runtime_datetime(value)

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
        return self._threshold_rule.level_for(rule, eval_upper, eval_lower)

    def _sample_level(
        self,
        rule: Dict[str, Any],
        value: float,
    ) -> Tuple[Optional[int], Optional[float], Optional[float]]:
        """直接使用原始样本值评估阈值级别。"""
        return self._threshold_rule.sample_level(rule, value)

    @staticmethod
    def _max_sample_gap_ms(samples: List[Tuple[datetime, float]]) -> float:
        """返回相邻样本之间的最大时间间隔。"""
        return max_sample_gap_ms(samples)

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

    def _apply_rule_evaluation(
        self,
        cur: Any,
        active_map: Dict[Tuple[str, str, str], Dict[str, Any]],
        triggered: List[Dict[str, Any]],
        *,
        rule: Dict[str, Any],
        sensor_no: str,
        channel: str,
        evaluation: RuleEvaluation,
        window_end: datetime,
    ) -> None:
        """通过共享的预警事件写入逻辑持久化一次规则判定结果。"""
        trigger = evaluation.trigger
        if trigger is not None:
            self._apply_condition(
                cur,
                active_map,
                triggered,
                rule=rule,
                sensor_no=sensor_no,
                channel=channel,
                alert_type=evaluation.alert_type,
                active=True,
                alert_level=trigger.level,
                trigger_val=trigger.value,
                fire_time=trigger.fire_time,
                force_refresh=not evaluation.previous_active,
            )
        if evaluation.previous_active and not evaluation.active:
            self._close_alert_incident(
                cur,
                active_map,
                sensor_no=sensor_no,
                channel=channel,
                alert_type=evaluation.alert_type,
                end_time=window_end,
            )

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
        evaluation = self._offline_rule.evaluate(
            RuleContext(
                rule=rule,
                samples=[],
                previous_last_seen=last_seen,
                window_end=window_end,
                confirm_seconds=self.confirm_seconds,
                stuck_seconds=self.stuck_seconds,
                offline_timeout_ms=self._offline_timeout_ms(rule),
            )
        )
        if not evaluation.active:
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

        self._apply_rule_evaluation(
            cur,
            active_map,
            triggered,
            rule=rule,
            sensor_no=rule["sensor_no_lower"],
            channel=rule.get("channel") or "",
            evaluation=evaluation,
            window_end=window_end,
        )
        pending_state_updates.setdefault(key, {}).update(evaluation.state_updates)
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
                sample_query = self._sample_query
                if sample_query is None and rules:
                    self._cache_sample_query(rules)
                    sample_query = self._sample_query
                if sample_query is not None:
                    sample_sql, sample_params_tail = sample_query
                    cur.execute(
                        sample_sql,
                        (window_start, window_end, *sample_params_tail),
                    )
                    for row in cur.fetchall():
                        raw_points_processed += int(row["sample_cnt"] or 0)
                        key = self.make_key(row["s_no"], row["ch"])
                        if key not in rules_by_key:
                            continue
                        samples_by_key[key] = list(
                            zip(
                                row["sample_times"],
                                row["sample_values"],
                                strict=True,
                            )
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
                if self._active_alert_cache is not None:
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

                confirm_seconds = float(
                    rule.get("confirm_seconds") or self.confirm_seconds
                )
                stuck_seconds = float(
                    rule.get("stuck_seconds") or self.stuck_seconds
                )
                offline_timeout_ms = self._offline_timeout_ms(rule)
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

                context = RuleContext(
                    rule=rule,
                    samples=samples,
                    previous_last_seen=previous_last_seen,
                    window_end=window_end,
                    confirm_seconds=confirm_seconds,
                    stuck_seconds=stuck_seconds,
                    offline_timeout_ms=offline_timeout_ms,
                )
                offline_evaluation = self._offline_rule.evaluate(context)
                state_updates: Dict[str, Any] = {
                    "last_ts": self._serialize_runtime_dt(samples[-1][0]),
                    "last_val": round(samples[-1][1], 5),
                    "online_status": "ONLINE",
                }
                if offline_evaluation.suppress_other_alerts:
                    state_updates.update(offline_evaluation.state_updates)
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
                else:
                    for evaluation in self.channel_rule_registry.evaluate(context):
                        state_updates.update(evaluation.state_updates)
                        self._apply_rule_evaluation(
                            cur,
                            active_map,
                            triggered_alerts,
                            rule=rule,
                            sensor_no=sensor_no,
                            channel=channel,
                            evaluation=evaluation,
                            window_end=window_end,
                        )
                    state_updates.update(offline_evaluation.state_updates)

                if offline_evaluation.active:
                    instrument_id = (rule.get("instrument_id") or "").strip()
                    if instrument_id:
                        offline_by_instrument.setdefault(
                            instrument_id,
                            set(),
                        ).add(key)
                self._apply_rule_evaluation(
                    cur,
                    active_map,
                    triggered_alerts,
                    rule=rule,
                    sensor_no=sensor_no,
                    channel=channel,
                    evaluation=offline_evaluation,
                    window_end=window_end,
                )

                pending_state_updates.setdefault(key, {}).update(state_updates)

            # 统计数据和预警事件在同一事务中落库；运行时连续状态只保存在
            # 当前进程，事务成功提交后再发布到 channel_states。
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
                    daq_key = self._alert_key(
                        instrument_id,
                        "__DAQ__",
                        ALERT_TYPE_DAQ_OFFLINE,
                    )
                    evaluation = self._daq_offline_rule.evaluate(
                        total_channels=total_channels,
                        offline_channels=offline_keys,
                        previous_active=bool(
                            self._daq_condition_active.get(instrument_id)
                        ),
                        active_alert_exists=daq_key in active_map,
                        state_known=instrument_id in self._daq_state_known,
                    )
                    if evaluation.should_close:
                        self._close_alert_incident(
                            cur,
                            active_map,
                            sensor_no=instrument_id,
                            channel="__DAQ__",
                            alert_type=ALERT_TYPE_DAQ_OFFLINE,
                            end_time=window_end,
                        )
                    if not evaluation.active:
                        daq_condition_updates[instrument_id] = False
                        continue
                    if not evaluation.should_create:
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
        """返回引擎计数和连续命中秒数配置。"""
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
            "detectionStartedAt": serialize_val(self.detection_started_at),
            "stationName": self.station_name,
            "stationCode": self.station_code,
            "alertTypeSwitches": dict(self.alert_type_switches),
            "ruleVersion": self.rule_version,
            "ruleRefreshSeconds": self.rule_refresh_seconds,
            "confirmSeconds": self.confirm_seconds,
            "thresholdConfirmMode": "frequency_sample_count",
            "thresholdConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "stuckConfirmMode": "frequency_sample_count",
            "stuckConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "oorConfirmMode": "frequency_sample_count",
            "oorConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "offlineConfirmMode": "frequency_timeout",
            "offlineConfirmMinimumPeriods": MIN_CONFIRM_SAMPLES,
            "stuckSeconds": self.stuck_seconds,
            "offlineSeconds": self.offline_seconds,
        }

