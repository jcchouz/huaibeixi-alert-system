"""预警检测和统计分析核心引擎。

本模块负责加载检测元数据、分析原始采样点、维护通道连续命中状态，
写入预警事件并更新统计记录。
"""

import logging
import re
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from psycopg2.extras import Json, execute_values

from app.core.serialization import serialize_row, serialize_val
from app.domain.alert_rules import (
    AlertRuleRegistry,
    DaqOfflineRule,
    OfflineAlertRule,
)
from app.domain.alert_rules.base import (
    MIN_CONFIRM_SAMPLES,
    MIN_OFFLINE_PERIODS,
    AlertRule,
    RuleContext,
    RuleEvaluation,
    parse_datetime,
    parse_frequency_hz,
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

logger = logging.getLogger("alert_analysis.engine")

# 在数据库内一次性聚合一个 t_monitor 窗口。
#   s_no / ch            -> 分组键，传感器编号统一转小写
#   max_time / min_time  -> 极值样本的真实时间，不是窗口边界
WINDOW_AGG_SQL = """
SELECT
    LOWER(sensor_no) AS s_no,
    COALESCE(channel, '') AS ch,
    COUNT(*) AS sample_cnt,
    MAX(value) AS val_max,
    MIN(value) AS val_min,
    AVG(value) AS val_avg,
    MIN(time) AS first_ts,
    MAX(time) AS last_ts,
    (ARRAY_AGG(time ORDER BY value ASC, time ASC))[1] AS min_time,
    (ARRAY_AGG(time ORDER BY value DESC, time ASC))[1] AS max_time
FROM public.t_monitor
WHERE time >= %s AND time < %s
GROUP BY LOWER(sensor_no), COALESCE(channel, '')
"""

# 实时预警专用的原始样本查询：按 time 升序返回窗口内的全部原始点。
# 与 WINDOW_AGG_SQL 的区别：
#   - 不聚合，保留每个点的时间，才能按连续点数和持续时长判断；
#   - value 为空的行直接排除，不参与阈值、量程和死值判定；
#   - 排序由数据库保证，下游 AlertRuleRegistry 依赖这个顺序累计状态。
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
        channel_rules: Optional[Iterable[AlertRule]] = None,
    ) -> None:
        # 检测、接口补充信息和总览统计共用这些元数据。
        # 单实例启动后只有实时预警线程写运行状态，其余线程只读。
        self.channel_states: Dict[str, Dict[str, Any]] = {}
        self.sensor_meta: Dict[str, Dict[str, Any]] = {}
        self.instrument_meta: Dict[str, Dict[str, Any]] = {}
        self._daq_condition_active: Dict[str, bool] = {}
        self._daq_state_known: Set[str] = set()
        self.last_rule_load_time: Optional[datetime] = None
        self.confirm_seconds = max(0.001, float(confirm_seconds))
        self.stuck_seconds = max(0.001, float(stuck_seconds))
        self.offline_seconds = max(0.001, float(offline_seconds))
        self.station_name = (station_name or "").strip() or "玉环站"
        self.station_code: Optional[str] = None
        self.detection_started_at: Optional[datetime] = None
        # 未显式配置的预警类型默认启用，避免新增类型被意外关闭。
        self.alert_type_switches: Dict[str, bool] = {
            (normalize_alert_type(name) or name): bool(enabled)
            for name, enabled in (alert_type_switches or {}).items()
        }
        self._active_alert_cache: Optional[
            Dict[Tuple[str, str, str], Dict[str, Any]]
        ] = None
        self._sample_query: Optional[Tuple[str, Tuple[Any, ...]]] = None
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
        self.load_thresholds()

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
            rule.update(
                last_ts=serialize_runtime_datetime(started_at),
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

    def load_thresholds(self) -> int:
        """在进程启动时加载一次规则和元数据。"""
        with get_db_cursor() as cur:
            cur.execute("SELECT LOCALTIMESTAMP AS now")
            runtime_now = cur.fetchone()["now"]
            # 1. 加载采集仪与站点映射。
            station_code = self._resolve_station_code(cur)
            self.station_code = station_code
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
                confirm_samples = required_sample_count(
                    frequency_hz,
                    confirm_seconds,
                )
                stuck_seconds = self.stuck_seconds
                stuck_confirm_samples = required_sample_count(
                    frequency_hz,
                    stuck_seconds,
                )
                offline_seconds = self.offline_seconds
                offline_confirm_periods = required_sample_count(
                    frequency_hz,
                    offline_seconds,
                    minimum_samples=MIN_OFFLINE_PERIODS,
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
                        "threshold_confirm_samples": confirm_samples,           # 按频率折算的阈值连续点数
                        "stuck_seconds": stuck_seconds,                         # 死值连续确认时长（秒）
                        "stuck_confirm_samples": stuck_confirm_samples,         # 按频率折算的死值连续点数
                        "oor_confirm_samples": confirm_samples,                 # 按频率折算的超量程连续点数
                        "offline_seconds": offline_seconds,                     # 离线确认基准时长（秒）
                        "offline_confirm_periods": offline_confirm_periods,     # 按频率折算的离线缺失周期数
                        "offline_timeout_ms": offline_timeout_ms,               # 离线超时阈值（毫秒）
                        "online_status": "ONLINE",                              # 初始在线状态
                        "last_ts": serialize_runtime_datetime(runtime_now),     # 新规则的离线判断起点
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
                new_states[key] = d

            # 规则只在进程启动时加载一次。
            self.channel_states = new_states
            self._cache_sample_query(new_states.values())
            self.last_rule_load_time = datetime.now()
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

    _parse_frequency_hz = staticmethod(parse_frequency_hz)

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
        rule_snapshot: Optional[Dict[str, Any]] = None,
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
            alert_id = uuid.uuid4().hex
            cur.execute(
                """
                INSERT INTO public.t_alert_record (
                    id, seq, sensor_no, channel, threshold_id,
                    alert_type, alert_category, alert_level,
                    trigger_val, create_time, alert_status, end_time,
                    rule_snapshot
                ) VALUES (
                    %s, nextval('public.seq_alert_record'), %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, 0, NULL, %s
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
                    Json(serialize_val(rule_snapshot or {})),
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
            rule_snapshot=self.build_rule_snapshot(rule, alert_type),
        )
        active_map[key] = alert
        triggered.append(alert)
        return alert

    @staticmethod
    def build_rule_snapshot(
        rule: Dict[str, Any],
        alert_type: str,
    ) -> Dict[str, Any]:
        """生成预警首次创建时保存的规则依据，不包含运行时状态。"""
        fields = (
            "id",
            "sensor_no",
            "channel",
            "physical_quantity",
            "unit",
            "range_min",
            "range_max",
            "level1_upper",
            "level1_lower",
            "level2_upper",
            "level2_lower",
            "level3_upper",
            "level3_lower",
            "frequency",
            "frequency_hz",
            "confirm_seconds",
            "threshold_confirm_samples",
            "stuck_seconds",
            "stuck_confirm_samples",
            "oor_confirm_samples",
            "offline_seconds",
            "offline_confirm_periods",
            "offline_timeout_ms",
        )
        snapshot = {"alert_type": alert_type}
        for field in fields:
            value = rule.get(field)
            if value is not None:
                snapshot[field] = serialize_val(value)
        return snapshot

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

    def _close_channel_data_alerts(
        self,
        cur: Any,
        active_map: Dict[Tuple[str, str, str], Dict[str, Any]],
        rule: Dict[str, Any],
        *,
        end_time: datetime,
    ) -> None:
        """Close threshold, stuck and out-of-range incidents for an offline channel."""
        for alert_type, state_field in (
            (ALERT_TYPE_THRESHOLD, "threshold_condition_active"),
            (ALERT_TYPE_STUCK, "stuck_condition_active"),
            (ALERT_TYPE_OOR, "oor_condition_active"),
        ):
            if rule.get(state_field):
                self._close_alert_incident(
                    cur,
                    active_map,
                    sensor_no=rule["sensor_no_lower"],
                    channel=rule.get("channel") or "",
                    alert_type=alert_type,
                    end_time=end_time,
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
        last_seen = parse_datetime(rule.get("last_ts"))
        evaluation = self._offline_rule.evaluate(
            RuleContext(
                rule=rule,
                samples=[],
                previous_last_seen=last_seen,
                window_end=window_end,
                confirm_seconds=self.confirm_seconds,
                stuck_seconds=self.stuck_seconds,
                offline_timeout_ms=rule["offline_timeout_ms"],
            )
        )
        if not evaluation.active:
            return False

        # 记录离线通道，供后续采集仪级离线聚合使用。
        instrument_id = (rule.get("instrument_id") or "").strip()
        if instrument_id:
            offline_by_instrument.setdefault(instrument_id, set()).add(key)

        self._close_channel_data_alerts(
            cur,
            active_map,
            rule,
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

    def _scan_realtime_alerts(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> Dict[str, Any]:
        """只扫描原始采样点并执行实时预警规则，不读写统计记录。"""
        # 本方法每轮按固定顺序判定五类预警：
        # 1. 无新样本：_apply_offline_if_due() 判通道离线；
        # 2. 有新样本：OfflineAlertRule 判通道离线或采样中断；
        # 3. 未离线：AlertRuleRegistry 依次判阈值超限、超物理量程、传感器死值；
        # 4. 本轮末尾：DaqOfflineRule 汇总全部通道均离线后的采集仪离线；
        # 5. 条件恢复时，_apply_rule_evaluation() 关闭对应的活动预警。
        triggered_alerts: List[Dict[str, Any]] = []
        raw_points_processed = 0
        pending_state_updates: Dict[str, Dict[str, Any]] = {}
        daq_condition_updates: Dict[str, bool] = {}

        with get_db_cursor(commit=True) as cur:
            rules = self.channel_states.values()
            samples_by_key: Dict[str, List[Tuple[datetime, float]]] = {}

            # 采样 SQL 在 load_thresholds() 中随规则集一次性生成，扫描路径只读。
            sample_query = self._sample_query
            if sample_query is not None:
                sample_sql, sample_params_tail = sample_query
                # 查询左闭右开窗口，并按通道返回按时间升序排列的原始点。
                cur.execute(
                    sample_sql,
                    (window_start, window_end, *sample_params_tail),
                )
                for row in cur.fetchall():
                    # 统计本窗口内参与实时检测的非空原始点数量。
                    raw_points_processed += int(row["sample_cnt"] or 0)
                    key = self.make_key(row["s_no"], row["ch"])
                    if key not in self.channel_states:
                        continue
                    # 复用同一组查询结果构造“采样时间-值”序列，供连续命中规则累计状态。
                    samples_by_key[key] = list(
                        zip(
                            row["sample_times"],
                            row["sample_values"],
                            strict=True,
                        )
                    )

            if self._active_alert_cache is not None:
                # 复用上一轮写库后维护的活动预警快照，避免每轮重复查询。
                active_map = dict(self._active_alert_cache)
            else:
                # 首次扫描时从数据库加载当前活动预警。
                active_map = self._load_active_alerts(cur)

            # 记录每个采集仪下的已配置通道数，作为采集仪离线判定的分母。
            instrument_totals = Counter(
                r.get("instrument_id")
                for r in rules
                if (r.get("instrument_id") or "").strip()
            )
            # 汇总本轮已判定离线的通道键，循环结束后据此聚合采集仪级离线预警。
            offline_by_instrument: Dict[str, Set[str]] = {}

            # 没有样本的通道也必须继续推进，否则无法判定通道离线。
            for rule in rules:
                key = rule["key"]
                sensor_no = rule["sensor_no_lower"]
                channel = rule.get("channel") or ""
                samples = samples_by_key.get(key, [])
                previous_last_seen = parse_datetime(rule.get("last_ts"))
                if not samples:
                    # 通道离线：本窗口没有该通道的新样本，按 last_ts 计算静默时长。
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
                context = RuleContext(
                    rule=rule,
                    samples=samples,
                    previous_last_seen=previous_last_seen,
                    window_end=window_end,
                    confirm_seconds=confirm_seconds,
                    stuck_seconds=stuck_seconds,
                    offline_timeout_ms=rule["offline_timeout_ms"],
                )
                # 通道离线或采样中断：使用原始样点时间检查静默时长和最大采样间隔。
                offline_evaluation = self._offline_rule.evaluate(context)
                state_updates: Dict[str, Any] = {
                    "last_ts": serialize_runtime_datetime(samples[-1][0]),
                    "last_val": round(samples[-1][1], 5),
                    "online_status": "ONLINE",
                }
                if offline_evaluation.suppress_other_alerts:
                    # 通道已离线时，离线预警优先，并关闭尚未结束的阈值、死值和超量程事件。
                    state_updates.update(offline_evaluation.state_updates)
                    self._close_channel_data_alerts(
                        cur,
                        active_map,
                        rule,
                        end_time=window_end,
                    )
                else:
                    # 通道在线时按注册表固定顺序判定：
                    # 1. ThresholdAlertRule   -> 阈值超限（level1/2/3）；
                    # 2. OutOfRangeAlertRule  -> 超物理量程（physical_min/max）；
                    # 3. StuckAlertRule       -> 传感器死值（连续相同读数）。
                    for evaluation in self.channel_rule_registry.evaluate(context):
                        state_updates.update(evaluation.state_updates)
                        # 新建、升级或关闭上述三类预警，并同步 t_alert_record。
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
                    # 记录真正离线的通道，供本轮采集仪离线聚合使用。
                    instrument_id = (rule.get("instrument_id") or "").strip()
                    if instrument_id:
                        offline_by_instrument.setdefault(
                            instrument_id,
                            set(),
                        ).add(key)
                # 通道离线预警同样通过统一写库入口创建或关闭。
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

            # 采集仪离线：同一采集仪下所有已配置通道都离线时，只生成一条设备级预警。
            if self.is_alert_type_enabled(ALERT_TYPE_DAQ_OFFLINE):
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
                        # 至少一个通道恢复在线时，关闭采集仪离线预警。
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
                    # 首次全部通道离线时创建采集仪离线预警。
                    daq_alert = self._upsert_alert_incident(
                        cur,
                        sensor_no=instrument_id,
                        channel="__DAQ__",
                        threshold_id=None,
                        alert_type=ALERT_TYPE_DAQ_OFFLINE,
                        alert_level=NON_THRESHOLD_ALERT_LEVEL,
                        trigger_val=0.0,
                        fire_time=window_end,
                        rule_snapshot={
                            "alert_type": ALERT_TYPE_DAQ_OFFLINE,
                            "offline_seconds": self.offline_seconds,
                            "required_channel_count": total_channels,
                        },
                    )
                    active_map[daq_key] = daq_alert
                    triggered_alerts.append(daq_alert)
                    daq_condition_updates[instrument_id] = True

        for key, updates in pending_state_updates.items():
            self.channel_states[key].update(updates)
        self._daq_condition_active.update(daq_condition_updates)
        self._daq_state_known.update(daq_condition_updates)
        self._active_alert_cache = active_map

        return {
            "windowStart": serialize_val(window_start),
            "windowEnd": serialize_val(window_end),
            "periodType": "realtime",
            "rawPointsProcessed": raw_points_processed,
            "upsertedStatsCount": 0,
            "triggeredAlertsCount": len(triggered_alerts),
            "triggeredAlerts": triggered_alerts[:10],
        }

    def _record_statistics_window(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str,
    ) -> Dict[str, Any]:
        """只聚合完整窗口并写入 t_stat_record，不执行预警规则。"""
        raw_points_processed = 0
        stat_rows: List[Tuple[Any, ...]] = []
        rules = self.channel_states.values()

        with get_db_cursor(commit=True) as cur:
            cur.execute(WINDOW_AGG_SQL, (window_start, window_end))
            observed_map: Dict[str, Dict[str, Any]] = {}
            for row in cur.fetchall():
                raw_points_processed += int(row["sample_cnt"] or 0)
                observed_map[self.make_key(row["s_no"], row["ch"])] = row

            # 保持原有范围：仅为已配置预警规则的通道生成统计记录。
            for rule in rules:
                key = rule["key"]
                obs = observed_map.get(key)
                if obs is None or int(obs["sample_cnt"] or 0) <= 0:
                    continue
                stat_rows.append(
                    (
                        rule["sensor_no_lower"],
                        rule.get("channel") or "",
                        period_type,
                        window_start,
                        float(obs["val_max"]),
                        float(obs["val_min"]),
                        float(obs["val_avg"]),
                        obs.get("max_time") or obs["last_ts"],
                        obs.get("min_time") or obs["first_ts"],
                        int(obs["sample_cnt"] or 0),
                    )
                )

            if stat_rows:
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

        return {
            "windowStart": serialize_val(window_start),
            "windowEnd": serialize_val(window_end),
            "periodType": period_type,
            "rawPointsProcessed": raw_points_processed,
            "upsertedStatsCount": len(stat_rows),
            "triggeredAlertsCount": 0,
            "triggeredAlerts": [],
        }

    def scan_realtime_alerts(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> Dict[str, Any]:
        """只对新到达的原始样本执行实时预警检测。"""
        t0 = time.perf_counter()
        if window_end <= window_start:
            raise ValueError("window_end must be later than window_start")
        result = self._scan_realtime_alerts(
            window_start,
            window_end,
        )
        return {
            **result,
            "elapsedMs": round((time.perf_counter() - t0) * 1000, 2),
        }

    def record_statistics_window(
        self,
        window_start: datetime,
        window_end: datetime,
        period_type: str = "1m",
    ) -> Dict[str, Any]:
        """只持久化一个完整统计窗口，不执行预警检测。"""
        t0 = time.perf_counter()
        if window_end <= window_start:
            raise ValueError("window_end must be later than window_start")
        result = self._record_statistics_window(
            window_start,
            window_end,
            period_type,
        )
        return {
            **result,
            "elapsedMs": round((time.perf_counter() - t0) * 1000, 2),
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
            "lastRuleLoadTime": serialize_val(self.last_rule_load_time),
            "detectionMode": "realtime_sample_groups",
            "detectionStartedAt": serialize_val(self.detection_started_at),
            "stationName": self.station_name,
            "stationCode": self.station_code,
            "alertTypeSwitches": dict(self.alert_type_switches),
            "confirmSeconds": self.confirm_seconds,
            "thresholdConfirmMode": "frequency_sample_count",
            "thresholdConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "stuckConfirmMode": "frequency_sample_count",
            "stuckConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "oorConfirmMode": "frequency_sample_count",
            "oorConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "offlineConfirmMode": "frequency_timeout",
            "offlineConfirmMinimumPeriods": MIN_OFFLINE_PERIODS,
            "stuckSeconds": self.stuck_seconds,
            "offlineSeconds": self.offline_seconds,
        }

