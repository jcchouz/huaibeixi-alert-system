"""实时预警扫描和完整统计窗口处理的后台 Worker。

实时预警线程直接处理 t_monitor 原始采样点，不等待统计窗口结束；统计线程
只负责生成已经到期的完整 t_stat_record 窗口。两条链路相互独立，并分别通过
PostgreSQL advisory lock 选主，避免多个后端实例重复处理。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import psycopg2

from app.core.config import Settings
from app.core.serialization import serialize_val
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor
from app.repositories.detection_state import (
    load_worker_watermark,
    save_worker_watermark,
)

logger = logging.getLogger("alert_analysis.detection")

ALERT_ADVISORY_LOCK_KEY = 0x414C5254
STATISTICS_ADVISORY_LOCK_KEY = 0x53544154


class AlertDetectionWorker:
    """运行实时预警和统计窗口两条独立后台链路。"""

    def __init__(
        self,
        engine: AlertAnalyticsEngine,
        settings: Settings,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.clock = clock
        self.interval_seconds = settings.detection_interval_seconds
        self.alert_overlap_seconds = settings.detection_alert_overlap_seconds
        self.lag_seconds = settings.detection_lag_seconds
        self.window_minutes = settings.detection_window_minutes
        self.max_catchup_windows = settings.detection_max_catchup_windows
        self.max_alert_catchup_seconds = settings.detection_max_alert_catchup_seconds
        self.alert_catchup_chunk_seconds = (
            settings.detection_alert_catchup_chunk_seconds
        )

        # 两条 Worker 线程共享的生命周期和并发状态。
        self._stop_event = threading.Event()
        self._alert_thread: Optional[threading.Thread] = None
        self._statistics_thread: Optional[threading.Thread] = None

        # advisory lock 是会话级锁，必须各自持有独立且持续打开的数据库连接。
        self._lock_connection: Optional[Any] = None
        self._statistics_lock_connection: Optional[Any] = None

        # 状态锁保护共享字段，链路锁防止手动调用与后台线程同时处理同一链路。
        self._state_lock = threading.Lock()
        self._alert_cycle_lock = threading.Lock()
        self._statistics_cycle_lock = threading.Lock()
        self._leader = False
        self._statistics_leader = False
        self._running = False
        self._last_alert_scan_at: Optional[datetime] = None
        self._last_window_end: Optional[datetime] = None
        self._last_run_at: Optional[datetime] = None
        self._last_completed_at: Optional[datetime] = None
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_alert_result: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._last_alert_error: Optional[str] = None
        self._last_statistics_error: Optional[str] = None
        self._alert_cycles = 0
        self._statistics_cycles = 0

    def start(self) -> None:
        """分别启动实时预警线程和统计线程。"""
        threads: List[threading.Thread] = []
        with self._state_lock:
            # 任一后台线程仍存活时，重复调用启动不会创建新线程。
            if (
                self._alert_thread
                and self._alert_thread.is_alive()
            ) or (
                self._statistics_thread
                and self._statistics_thread.is_alive()
            ):
                return
            self._stop_event.clear()
            self._running = True
            self._alert_thread = threading.Thread(
                target=self._run_alert_loop,
                name="alert-realtime-worker",
                daemon=True,
            )
            self._statistics_thread = threading.Thread(
                target=self._run_statistics_loop,
                name="alert-statistics-worker",
                daemon=True,
            )
            threads = [self._alert_thread, self._statistics_thread]
        for thread in threads:
            thread.start()
        logger.info(
            "Alert detection worker started "
            "(alerts=%ss, overlap=%ss, statistics=%sm, stats-lag=%ss, catchup=%s).",
            self.interval_seconds,
            self.alert_overlap_seconds,
            self.window_minutes,
            self.lag_seconds,
            self.max_catchup_windows,
        )

    def stop(self) -> None:
        """通知两条循环停止，释放选主锁并清理线程状态。"""
        self._stop_event.set()
        with self._state_lock:
            threads = [
                thread
                for thread in (self._alert_thread, self._statistics_thread)
                if thread and thread.is_alive()
            ]
        for thread in threads:
            thread.join(timeout=30)
        self._release_leader_lock()
        self._release_statistics_leader_lock()
        with self._state_lock:
            self._running = False
            self._leader = False
            self._statistics_leader = False
            self._alert_thread = None
            self._statistics_thread = None
        logger.info("Alert detection worker stopped.")

    def initialize(self) -> None:
        """启动前恢复告警扫描和统计窗口的持久化水位。"""

        if self.settings.environment == "test":
            # 测试环境使用注入时钟和内存状态，不访问真实水位表。
            return
        with get_db_cursor() as cur:
            alert_state = load_worker_watermark(cur, "alert")
            statistics_state = load_worker_watermark(cur, "statistics")
        with self._state_lock:
            if alert_state:
                self._last_alert_scan_at = alert_state["last_processed_time"]
            if statistics_state:
                self._last_window_end = statistics_state["last_processed_time"]
                self._last_completed_at = statistics_state["last_completed_time"]
        logger.info(
            "Restored detection watermarks (alert=%s, statistics=%s).",
            self._last_alert_scan_at,
            self._last_window_end,
        )

    def _persist_worker_watermark(
        self,
        worker_name: str,
        last_processed_time: datetime,
        last_completed_time: datetime,
    ) -> None:
        if self.settings.environment == "test":
            return
        # 水位单独提交，使进程重启后可以从最后完成的处理边界继续。
        with get_db_cursor(commit=True) as cur:
            save_worker_watermark(
                cur,
                worker_name=worker_name,
                last_processed_time=last_processed_time,
                last_completed_time=last_completed_time,
            )

    def _refresh_rules(self) -> None:
        # 引擎支持热更新时再刷新，便于规则开关修改后无需重启 Worker。
        refresh = getattr(self.engine, "refresh_rules_if_changed", None)
        if refresh is not None:
            refresh()

    def run_once(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """执行一次手动检测，用于测试或本地诊断。"""
        current_time = now or self._current_time()
        # 两条链路彼此独立，其中一条未取得 advisory lock 不影响另一条。
        if self._ensure_leader():
            self.scan_alerts_once(current_time)
        if self._ensure_statistics_leader():
            return self.process_statistics_once(current_time)
        return []

    def scan_alerts_once(self, current_time: datetime) -> Optional[Dict[str, Any]]:
        """从持久化水位开始扫描实时预警，并限制单次追赶的历史范围。

        每次扫描都会向前短回看一段重叠区间，以覆盖外部写入事务的提交延迟。
        重叠产生的重复样本由检索引擎依据原始采样时间去重。
        """

        with self._alert_cycle_lock:
            self._refresh_rules()
            with self._state_lock:
                self._last_run_at = current_time
                last_alert_scan_at = self._last_alert_scan_at

            started = time.perf_counter()
            if last_alert_scan_at is None:
                # 首次启动没有历史水位，只处理一个最小扫描区间，避免读取全部历史。
                window_start = current_time - timedelta(
                    seconds=max(
                        self.interval_seconds,
                        self.alert_overlap_seconds,
                    )
                )
                if current_time <= window_start:
                    return None
                first_start = window_start
                raw_points = 0
                triggered = 0
                result = self.engine.scan_realtime_alerts(
                    window_start,
                    current_time,
                )
                raw_points += int(result.get("rawPointsProcessed") or 0)
                triggered += int(result.get("triggeredAlertsCount") or 0)
                with self._state_lock:
                    self._last_alert_scan_at = current_time
                self._persist_worker_watermark(
                    "alert",
                    current_time,
                    current_time,
                )
            else:
                scan_cursor = last_alert_scan_at
                # 长时间停机后只追赶允许的最近一段历史，避免启动时长时间阻塞。
                earliest = current_time - timedelta(
                    seconds=self.max_alert_catchup_seconds
                )
                if scan_cursor < earliest:
                    logger.warning(
                        "Alert catch-up skipped %.1fs of history before %s.",
                        (earliest - scan_cursor).total_seconds(),
                        earliest,
                    )
                    scan_cursor = earliest
                if current_time <= scan_cursor:
                    return None
                first_start = scan_cursor - timedelta(
                    seconds=self.alert_overlap_seconds
                )
                raw_points = 0
                triggered = 0
                chunk_seconds = max(
                    1,
                    min(
                        self.alert_catchup_chunk_seconds,
                        self.interval_seconds * 5,
                    ),
                )
                # 将大段追赶拆成小块逐段提交，控制单次扫描的数据量和耗时。
                while scan_cursor < current_time:
                    chunk_end = min(
                        current_time,
                        scan_cursor + timedelta(seconds=chunk_seconds),
                    )
                    window_start = scan_cursor - timedelta(
                        seconds=self.alert_overlap_seconds
                    )
                    # 即使区间内没有采样点也照常扫描，使通道离线判定继续推进。
                    result = self.engine.scan_realtime_alerts(
                        window_start,
                        chunk_end,
                    )
                    raw_points += int(result.get("rawPointsProcessed") or 0)
                    triggered += int(result.get("triggeredAlertsCount") or 0)
                    scan_cursor = chunk_end
                    with self._state_lock:
                        self._last_alert_scan_at = scan_cursor
                    self._persist_worker_watermark(
                        "alert",
                        scan_cursor,
                        scan_cursor,
                    )

            summary = {
                "windowStart": serialize_val(first_start),
                "windowEnd": serialize_val(current_time),
                "rawPointsProcessed": raw_points,
                "triggeredAlertsCount": triggered,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
            }
            with self._state_lock:
                self._last_alert_result = summary
                self._last_alert_error = None
                self._last_error = None
                self._alert_cycles += 1
            return summary

    def process_statistics_once(self, current_time: datetime) -> List[Dict[str, Any]]:
        """按顺序处理当前所有已到期的统计窗口。"""
        with self._statistics_cycle_lock:
            self._refresh_rules()
            return self._process_statistics_once(current_time)

    def _process_statistics_once(
        self,
        current_time: datetime,
    ) -> List[Dict[str, Any]]:
        """查找已到期的完整窗口，并按顺序逐个持久化。"""
        # 只处理落库延迟之后已经闭合的窗口，避免统计到仍在写入的数据。
        eligible_end = self.latest_eligible_window_end(current_time)
        with self._state_lock:
            last_window_end = self._last_window_end

        if last_window_end is not None and eligible_end <= last_window_end:
            return []

        if last_window_end is None:
            # 首次运行从最近一个完整窗口开始。
            window_ends = [eligible_end]
        else:
            # 从上一次完成位置继续，并受最大补处理窗口数限制。
            first_end = last_window_end + timedelta(minutes=self.window_minutes)
            earliest_end = eligible_end - timedelta(
                minutes=max(0, self.max_catchup_windows - 1) * self.window_minutes
            )
            first_end = max(first_end, earliest_end)
            if first_end > eligible_end:
                return []
            count = (
                int(
                    (eligible_end - first_end).total_seconds()
                    // (self.window_minutes * 60)
                )
                + 1
            )
            window_ends = [
                first_end + timedelta(minutes=index * self.window_minutes)
                for index in range(count)
            ]

        results: List[Dict[str, Any]] = []
        for window_end in window_ends:
            window_start = window_end - timedelta(minutes=self.window_minutes)
            # 一个窗口成功后立即推进水位，异常时下次只重试尚未完成的窗口。
            result = self.engine.record_statistics_window(
                window_start,
                window_end,
                period_type=f"{self.window_minutes}m",
            )
            results.append(result)
            with self._state_lock:
                # 每完成一个窗口就推进一次统计水位。
                self._last_window_end = window_end
                self._last_result = {
                    "windowStart": serialize_val(window_start),
                    "windowEnd": serialize_val(window_end),
                    "rawPointsProcessed": int(result.get("rawPointsProcessed") or 0),
                    "upsertedStatsCount": int(result.get("upsertedStatsCount") or 0),
                    "elapsedMs": result.get("elapsedMs"),
                }
                self._last_completed_at = current_time
                self._last_statistics_error = None
                self._last_error = None
                self._statistics_cycles += 1
            self._persist_worker_watermark(
                "statistics",
                window_end,
                current_time,
            )

        return results

    def latest_eligible_window_end(self, now: datetime) -> datetime:
        """应用落库延迟和统计周期后，返回最近完整窗口结束时间。"""
        # 先减去允许的落库延迟，再按自然日锚点对齐到完整窗口边界。
        cutoff = now - timedelta(seconds=self.lag_seconds)
        day_start = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_minutes = int((cutoff - day_start).total_seconds() // 60)
        block_start = day_start + timedelta(
            minutes=(elapsed_minutes // self.window_minutes) * self.window_minutes
        )
        return block_start

    def _current_time(self) -> datetime:
        """优先使用注入时钟，否则使用数据库时钟确定扫描边界。"""
        if self.clock is not None:
            return self.clock()
        with get_db_cursor() as cur:
            cur.execute("SELECT LOCALTIMESTAMP AS now")
            row = cur.fetchone()
        return row["now"]

    def get_status(self) -> Dict[str, Any]:
        """返回可供健康检查和运维接口使用的 JSON 安全状态。"""
        with self._state_lock:
            return {
                "enabled": bool(
                    self.settings.detection_enabled
                    and self.settings.environment != "test"
                ),
                "running": self._running,
                "leader": self._leader,
                "statisticsLeader": self._statistics_leader,
                "intervalSeconds": self.interval_seconds,
                "alertOverlapSeconds": self.alert_overlap_seconds,
                "lagSeconds": self.lag_seconds,
                "windowMinutes": self.window_minutes,
                "detectionMode": "realtime_sample_groups",
                "conditionUnit": self.settings.detection_condition_unit,
                "confirmPoints": self.settings.detection_confirm_points,
                "stuckPoints": self.settings.detection_stuck_points,
                "offlinePoints": self.settings.detection_offline_points,
                "confirmSeconds": self.settings.detection_confirm_seconds,
                "stuckSeconds": self.settings.detection_stuck_seconds,
                "offlineSeconds": self.settings.detection_offline_seconds,
                "maxCatchupWindows": self.max_catchup_windows,
                "maxAlertCatchupSeconds": self.max_alert_catchup_seconds,
                "alertCatchupChunkSeconds": self.alert_catchup_chunk_seconds,
                "cycles": self._alert_cycles + self._statistics_cycles,
                "alertCycles": self._alert_cycles,
                "statisticsCycles": self._statistics_cycles,
                "lastRunAt": serialize_val(self._last_run_at),
                "lastCompletedAt": serialize_val(self._last_completed_at),
                "lastAlertScanAt": serialize_val(self._last_alert_scan_at),
                "lastAlertResult": (
                    dict(self._last_alert_result)
                    if self._last_alert_result
                    else None
                ),
                "lastWindowEnd": serialize_val(self._last_window_end),
                "lastResult": dict(self._last_result) if self._last_result else None,
                "lastError": self._last_error,
                "lastAlertError": self._last_alert_error,
                "lastStatisticsError": self._last_statistics_error,
            }

    def _run_alert_loop(self) -> None:
        """参与选主并持续执行实时扫描，直到收到停止信号。"""
        while not self._stop_event.is_set():
            started = time.monotonic()
            if self._ensure_leader():
                try:
                    self.scan_alerts_once(self._current_time())
                except Exception as exc:
                    logger.exception("Realtime alert scan failed")
                    with self._state_lock:
                        self._last_alert_error = str(exc)
                        self._last_error = str(exc)
            elapsed = time.monotonic() - started
            # 即使单次扫描耗时较长，也尽量保持稳定扫描节奏。
            wait_seconds = max(0.1, self.interval_seconds - elapsed)
            self._stop_event.wait(wait_seconds)

    def _run_statistics_loop(self) -> None:
        """参与选主并持续处理完整统计窗口，直到收到停止信号。"""
        while not self._stop_event.is_set():
            started = time.monotonic()
            if self._ensure_statistics_leader():
                try:
                    self.process_statistics_once(self._current_time())
                except Exception as exc:
                    logger.exception("Statistics window processing failed")
                    with self._state_lock:
                        self._last_statistics_error = str(exc)
                        self._last_error = str(exc)
            elapsed = time.monotonic() - started
            wait_seconds = max(0.1, self.interval_seconds - elapsed)
            self._stop_event.wait(wait_seconds)

    def _ensure_leader(self) -> bool:
        """尝试持有实时预警链路的会话级 PostgreSQL advisory lock。

        锁连接仅用于选主，检测业务使用其他数据库连接。只要本连接不关闭，
        会话级锁就会持续有效，业务事务提交不会释放该锁。
        """
        if (
            self._lock_connection is not None
            and not self._lock_connection.closed
            and self._leader
        ):
            return True

        try:
            if self._lock_connection is None or self._lock_connection.closed:
                self._lock_connection = psycopg2.connect(self.settings.dsn)
                # 使用自动提交，避免选主锁被挂在一个长期未结束的事务中。
                self._lock_connection.autocommit = True
            with self._lock_connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS locked",
                    (ALERT_ADVISORY_LOCK_KEY,),
                )
                row = cur.fetchone()
            is_leader = bool(row and row[0])
            if is_leader != self._leader:
                logger.info(
                    "Alert detection worker %s leader lock.",
                    "acquired" if is_leader else "is waiting for",
                )
            self._leader = is_leader
            return is_leader
        except Exception as exc:
            # 锁连接失败时退回从节点状态，并在后续循环重试。
            logger.warning("Could not acquire alert detection leader lock: %s", exc)
            self._close_lock_connection()
            self._leader = False
            return False

    def _release_leader_lock(self) -> None:
        """关闭时释放实时预警选主锁。"""
        if self._lock_connection is None:
            return
        try:
            if not self._lock_connection.closed:
                if self._leader:
                    with self._lock_connection.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_unlock(%s)",
                            (ALERT_ADVISORY_LOCK_KEY,),
                        )
        except Exception:
            logger.debug("Could not release alert detection leader lock", exc_info=True)
        finally:
            # 即使显式解锁失败，关闭专用连接也会释放会话级 advisory lock。
            self._close_lock_connection()

    def _ensure_statistics_leader(self) -> bool:
        """尝试持有统计链路的会话级 PostgreSQL advisory lock。"""
        if (
            self._statistics_lock_connection is not None
            and not self._statistics_lock_connection.closed
            and self._statistics_leader
        ):
            return True

        try:
            if (
                self._statistics_lock_connection is None
                or self._statistics_lock_connection.closed
            ):
                self._statistics_lock_connection = psycopg2.connect(
                    self.settings.dsn
                )
                # 统计选主连接同样保持自动提交，并由独立会话持有锁。
                self._statistics_lock_connection.autocommit = True
            with self._statistics_lock_connection.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS locked",
                    (STATISTICS_ADVISORY_LOCK_KEY,),
                )
                row = cur.fetchone()
            is_leader = bool(row and row[0])
            if is_leader != self._statistics_leader:
                logger.info(
                    "Statistics worker %s leader lock.",
                    "acquired" if is_leader else "is waiting for",
                )
            self._statistics_leader = is_leader
            return is_leader
        except Exception as exc:
            logger.warning(
                "Could not acquire statistics leader lock: %s",
                exc,
            )
            self._close_statistics_lock_connection()
            self._statistics_leader = False
            return False

    def _release_statistics_leader_lock(self) -> None:
        """关闭时释放统计链路选主锁。"""
        if self._statistics_lock_connection is None:
            return
        try:
            if not self._statistics_lock_connection.closed:
                if self._statistics_leader:
                    with self._statistics_lock_connection.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_unlock(%s)",
                            (STATISTICS_ADVISORY_LOCK_KEY,),
                        )
        except Exception:
            logger.debug(
                "Could not release statistics leader lock",
                exc_info=True,
            )
        finally:
            # 关闭会话即可释放统计选主锁；显式解锁仅用于尽快让出主节点。
            self._close_statistics_lock_connection()

    def _close_lock_connection(self) -> None:
        """关闭实时预警锁连接，避免关闭异常影响主流程。"""
        if self._lock_connection is not None:
            try:
                self._lock_connection.close()
            except Exception:
                pass
        self._lock_connection = None

    def _close_statistics_lock_connection(self) -> None:
        """关闭统计锁连接，避免关闭异常影响主流程。"""
        if self._statistics_lock_connection is not None:
            try:
                self._statistics_lock_connection.close()
            except Exception:
                pass
        self._statistics_lock_connection = None
