"""实时预警扫描和完整统计窗口处理的后台 Worker。

实时预警线程直接处理 t_monitor 原始采样点，不等待统计窗口结束；统计线程
只负责生成已经到期的完整 t_stat_record 窗口。本服务按单实例部署，启动时
以数据库当前时间作为检测边界，不恢复历史水位或追赶启动前的数据。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app.core.config import Settings
from app.core.serialization import serialize_val
from app.domain.alert_rules.base import MIN_CONFIRM_SAMPLES
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor

logger = logging.getLogger("alert_analysis.detection")


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

        # 单实例下每条链路只有一个处理线程，不需要额外的重入或状态锁。
        self._stop_event = threading.Event()
        self._alert_thread: Optional[threading.Thread] = None
        self._statistics_thread: Optional[threading.Thread] = None

        self._detection_started_at: Optional[datetime] = None
        self._last_alert_scan_at: Optional[datetime] = None
        self._last_window_end: Optional[datetime] = None
        self._last_run_at: Optional[datetime] = None
        self._last_result: Optional[Dict[str, Any]] = None
        self._last_alert_result: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._last_alert_error: Optional[str] = None
        self._last_statistics_error: Optional[str] = None
        self._alert_cycles = 0
        self._statistics_cycles = 0

    def start(self) -> None:
        """分别启动实时预警线程和统计线程。"""
        if any(
            thread and thread.is_alive()
            for thread in (self._alert_thread, self._statistics_thread)
        ):
            return
        self._stop_event.clear()
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
        threads = (self._alert_thread, self._statistics_thread)
        for thread in threads:
            thread.start()
        logger.info(
            "Alert detection worker started "
            "(alerts=%ss, overlap=%ss, statistics=%sm, stats-lag=%ss).",
            self.interval_seconds,
            self.alert_overlap_seconds,
            self.window_minutes,
            self.lag_seconds,
        )

    def stop(self) -> None:
        """通知两条循环停止并清理线程状态。"""
        self._stop_event.set()
        threads = [
            thread
            for thread in (self._alert_thread, self._statistics_thread)
            if thread and thread.is_alive()
        ]
        for thread in threads:
            thread.join(timeout=30)
        self._alert_thread = None
        self._statistics_thread = None
        logger.info("Alert detection worker stopped.")

    def initialize(self, now: Optional[datetime] = None) -> None:
        """以当前时间为边界初始化检测，不读取或追赶历史状态。"""
        if now is None:
            now = self._current_time()

        begin_session = getattr(self.engine, "begin_detection_session", None)
        if begin_session is not None:
            begin_session(now)

        self._detection_started_at = now
        self._last_alert_scan_at = now
        self._last_window_end = self._first_statistics_boundary_after(now)
        logger.info(
            "Detection session initialized at %s; statistics starts after %s.",
            now,
            self._last_window_end,
        )

    def scan_alerts_once(self, current_time: datetime) -> Optional[Dict[str, Any]]:
        """扫描启动边界之后的实时样本。

        每次扫描都会向前短回看一段重叠区间，以覆盖外部写入事务的提交延迟。
        重叠不会越过启动边界，重复样本由检索引擎依据 ``last_ts`` 去重。
        当前时间超前于积压游标时，只推进一个检测周期；下一轮仍从上一轮
        ``scan_end`` 继续，不会跳到数据库当前时间而漏扫中间窗口。
        """
        self._last_run_at = current_time
        last_alert_scan_at = self._last_alert_scan_at
        detection_started_at = self._detection_started_at

        if last_alert_scan_at is None or detection_started_at is None:
            return None
        if current_time <= last_alert_scan_at:
            return None

        started = time.perf_counter()
        scan_end = min(
            current_time,
            last_alert_scan_at + timedelta(seconds=self.interval_seconds),
        )
        window_start = max(
            detection_started_at,
            last_alert_scan_at - timedelta(seconds=self.alert_overlap_seconds),
        )
        # 即使区间内没有采样点也照常扫描，使通道离线判定继续推进。
        result = self.engine.scan_realtime_alerts(
            window_start,
            scan_end,
        )
        self._last_alert_scan_at = scan_end

        # 汇总本轮实际覆盖范围、处理量和耗时，供运行状态查询使用。
        summary = {
            "windowStart": serialize_val(window_start),
            "windowEnd": serialize_val(scan_end),
            "rawPointsProcessed": int(result.get("rawPointsProcessed") or 0),
            "triggeredAlertsCount": int(
                result.get("triggeredAlertsCount") or 0
            ),
            "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
            "backlogSeconds": round(
                max(0.0, (current_time - scan_end).total_seconds()),
                3,
            ),
        }
        self._last_alert_result = summary
        self._last_alert_error = None
        self._last_error = None
        self._alert_cycles += 1
        return summary

    def process_statistics_once(self, current_time: datetime) -> List[Dict[str, Any]]:
        """按顺序处理当前所有已到期的统计窗口。"""
        # 只处理落库延迟之后已经闭合的窗口，避免统计到仍在写入的数据。
        eligible_end = self.latest_eligible_window_end(current_time)
        last_window_end = self._last_window_end

        if last_window_end is not None and eligible_end <= last_window_end:
            return []

        if last_window_end is None:
            # 未经初始化时不处理历史窗口。
            return []

        # 只处理本次启动之后尚未写入的完整窗口。
        first_end = last_window_end + timedelta(minutes=self.window_minutes)
        if first_end > eligible_end:
            return []

        results: List[Dict[str, Any]] = []
        window_end = first_end
        while window_end <= eligible_end:
            window_start = window_end - timedelta(minutes=self.window_minutes)
            # 一个窗口成功后立即推进水位，异常时下次只重试尚未完成的窗口。
            result = self.engine.record_statistics_window(
                window_start,
                window_end,
                period_type=f"{self.window_minutes}m",
            )
            results.append(result)
            # 每完成一个窗口就推进一次统计水位。
            self._last_window_end = window_end
            self._last_result = {
                "windowStart": serialize_val(window_start),
                "windowEnd": serialize_val(window_end),
                "rawPointsProcessed": int(result.get("rawPointsProcessed") or 0),
                "upsertedStatsCount": int(result.get("upsertedStatsCount") or 0),
                "elapsedMs": result.get("elapsedMs"),
            }
            self._last_statistics_error = None
            self._last_error = None
            self._statistics_cycles += 1
            window_end += timedelta(minutes=self.window_minutes)

        return results

    def _window_boundary(self, value: datetime) -> datetime:
        """Floor a datetime to the configured natural-day window boundary."""
        day_start = value.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_minutes = int((value - day_start).total_seconds() // 60)
        return day_start + timedelta(
            minutes=(elapsed_minutes // self.window_minutes) * self.window_minutes
        )

    def _first_statistics_boundary_after(self, started_at: datetime) -> datetime:
        """返回启动后第一个完整统计窗口的起始边界。"""
        block_start = self._window_boundary(started_at)
        # 启动时间落在窗口中间时，当前窗口包含启动前数据，跳过它。
        return (
            block_start
            if started_at == block_start
            else block_start + timedelta(minutes=self.window_minutes)
        )

    def latest_eligible_window_end(self, now: datetime) -> datetime:
        """应用落库延迟和统计周期后，返回最近完整窗口结束时间。"""
        # 先减去允许的落库延迟，再按自然日锚点对齐到完整窗口边界。
        cutoff = now - timedelta(seconds=self.lag_seconds)
        return self._window_boundary(cutoff)

    def _next_statistics_scan_at(self) -> Optional[datetime]:
        """返回下一轮统计窗口允许开始处理的时间。"""
        last_window_end = self._last_window_end
        if last_window_end is None:
            return None
        return last_window_end + timedelta(
            minutes=self.window_minutes,
            seconds=self.lag_seconds,
        )

    def _statistics_wait_seconds(
        self,
        current_time: datetime,
        elapsed_seconds: float = 0.0,
    ) -> float:
        """按统计窗口节奏计算等待时间，不在实时扫描周期内空转。"""
        next_scan_at = self._next_statistics_scan_at()
        if next_scan_at is None:
            return max(0.1, self.interval_seconds)
        remaining = (next_scan_at - current_time).total_seconds() - elapsed_seconds
        return max(0.1, remaining)

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
        running = any(
            thread and thread.is_alive()
            for thread in (self._alert_thread, self._statistics_thread)
        )
        return {
            "enabled": bool(self.settings.detection_enabled),
            "running": running,
            "intervalSeconds": self.interval_seconds,
            "alertOverlapSeconds": self.alert_overlap_seconds,
            "lagSeconds": self.lag_seconds,
            "windowMinutes": self.window_minutes,
            "detectionMode": "realtime_sample_groups",
            "confirmSeconds": self.settings.detection_confirm_seconds,
            "thresholdConfirmMode": "frequency_sample_count",
            "thresholdConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "stuckConfirmMode": "frequency_sample_count",
            "stuckConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "oorConfirmMode": "frequency_sample_count",
            "oorConfirmMinimumSamples": MIN_CONFIRM_SAMPLES,
            "offlineConfirmMode": "frequency_timeout",
            "offlineConfirmMinimumPeriods": MIN_CONFIRM_SAMPLES,
            "stuckSeconds": self.settings.detection_stuck_seconds,
            "offlineSeconds": self.settings.detection_offline_seconds,
            "cycles": self._alert_cycles + self._statistics_cycles,
            "alertCycles": self._alert_cycles,
            "statisticsCycles": self._statistics_cycles,
            "startedAt": serialize_val(self._detection_started_at),
            "lastRunAt": serialize_val(self._last_run_at),
            "lastAlertScanAt": serialize_val(self._last_alert_scan_at),
            "alertBacklogSeconds": round(
                max(
                    0.0,
                    (
                        self._last_run_at - self._last_alert_scan_at
                    ).total_seconds(),
                ),
                3,
            )
            if self._last_run_at is not None
            and self._last_alert_scan_at is not None
            else 0.0,
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
        """持续执行实时扫描，直到收到停止信号。"""
        while not self._stop_event.is_set():
            started = time.monotonic()
            backlog_seconds = 0.0
            try:
                # 即使当前没有新样本，也要执行扫描以持续推进通道离线判定。
                result = self.scan_alerts_once(self._current_time())
                if result is not None:
                    backlog_seconds = float(result.get("backlogSeconds") or 0.0)
            except Exception as exc:
                # 单次扫描失败不终止后台线程，记录异常后在下一轮继续重试。
                logger.exception("Realtime alert scan failed")
                self._last_alert_error = str(exc)
                self._last_error = str(exc)
            elapsed = time.monotonic() - started
            # 正常情况保持每秒一个窗口；存在积压时连续追赶。
            wait_seconds = (
                0.1
                if backlog_seconds > 0
                else max(0.1, self.interval_seconds - elapsed)
            )
            self._stop_event.wait(wait_seconds)

    def _run_statistics_loop(self) -> None:
        """持续处理完整统计窗口，直到收到停止信号。"""
        while not self._stop_event.is_set():
            started = time.monotonic()
            # 默认按失败重试间隔等待，成功后再切换到下一窗口的处理时间。
            wait_seconds = max(1.0, self.interval_seconds)
            try:
                current_time = self._current_time()
                self.process_statistics_once(current_time)
            except Exception as exc:
                logger.exception("Statistics window processing failed")
                self._last_statistics_error = str(exc)
                self._last_error = str(exc)
            else:
                elapsed = time.monotonic() - started
                wait_seconds = self._statistics_wait_seconds(
                    current_time,
                    elapsed_seconds=elapsed,
                )
            self._stop_event.wait(wait_seconds)
