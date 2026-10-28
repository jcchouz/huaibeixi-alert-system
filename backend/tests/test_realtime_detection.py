from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

import pytest

import app.domain.engine as engine_module
from app.domain.alert_types import (
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
)
from app.domain.engine import AlertAnalyticsEngine


def test_realtime_scan_uses_alert_only_path(monkeypatch) -> None:
    engine = AlertAnalyticsEngine()
    captured: dict[str, Any] = {}

    def fake_alert_scan(window_start, window_end):
        captured.update(
            {
                "window_start": window_start,
                "window_end": window_end,
            }
        )
        return {
            "rawPointsProcessed": 3,
            "upsertedStatsCount": 0,
            "triggeredAlertsCount": 1,
            "triggeredAlerts": [],
        }

    def fail_statistics(*args, **kwargs):
        raise AssertionError("realtime scan must not run statistics")

    monkeypatch.setattr(engine, "_scan_realtime_alerts", fake_alert_scan)
    monkeypatch.setattr(engine, "_record_statistics_window", fail_statistics)

    result = engine.scan_realtime_alerts(
        datetime(2026, 10, 20, 12, 0, 1),
        datetime(2026, 10, 20, 12, 0, 2),
    )

    assert captured["window_start"] == datetime(2026, 10, 20, 12, 0, 1)
    assert result["triggeredAlertsCount"] == 1


def test_statistics_window_skips_alert_detection(monkeypatch) -> None:
    engine = AlertAnalyticsEngine()
    captured: dict[str, Any] = {}

    def fail_alert_scan(*args, **kwargs):
        raise AssertionError("statistics scan must not run alert detection")

    def fake_statistics(window_start, window_end, period_type):
        captured.update(
            {
                "window_start": window_start,
                "window_end": window_end,
                "period_type": period_type,
            }
        )
        return {
            "rawPointsProcessed": 100,
            "upsertedStatsCount": 2,
            "triggeredAlertsCount": 0,
            "triggeredAlerts": [],
        }

    monkeypatch.setattr(engine, "_scan_realtime_alerts", fail_alert_scan)
    monkeypatch.setattr(engine, "_record_statistics_window", fake_statistics)

    result = engine.record_statistics_window(
        datetime(2026, 10, 20, 12, 0),
        datetime(2026, 10, 20, 12, 1),
    )

    assert captured["window_start"] == datetime(2026, 10, 20, 12, 0)
    assert captured["period_type"] == "1m"
    assert result["upsertedStatsCount"] == 2


def test_statistics_batch_supplies_create_by_and_create_time(monkeypatch) -> None:
    engine = AlertAnalyticsEngine()
    key = engine.make_key("sensor-1", "x")
    engine.channel_states[key] = {
        "key": key,
        "sensor_no_lower": "sensor-1",
        "channel": "x",
    }

    class StatisticsCursor:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def execute(
            self,
            sql: str,
            params: tuple[Any, ...] | None = None,
        ) -> None:
            self.rows = [
                {
                    "s_no": "sensor-1",
                    "ch": "x",
                    "sample_cnt": 2,
                    "val_max": 2.0,
                    "val_min": 1.0,
                    "val_avg": 1.5,
                    "first_ts": params[0],
                    "last_ts": params[1],
                    "min_time": params[0],
                    "max_time": params[1],
                }
            ]

        def fetchall(self) -> list[dict[str, Any]]:
            return self.rows

    cursor = StatisticsCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    captured: dict[str, Any] = {}

    def fake_execute_values(cur, sql, rows, **kwargs):
        captured.update({"sql": sql, "rows": rows, **kwargs})

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(engine_module, "execute_values", fake_execute_values)

    engine.record_statistics_window(
        datetime(2026, 10, 20, 12, 0),
        datetime(2026, 10, 20, 12, 1),
    )

    assert len(captured["rows"]) == 1
    assert "'system', NOW()" in captured["template"]


def test_existing_same_level_alert_is_not_refreshed_on_every_scan() -> None:
    engine = AlertAnalyticsEngine()
    key = engine._alert_key("sensor-1", "x", ALERT_TYPE_THRESHOLD)
    existing = {
        "id": "alert-1",
        "sensor_no": "sensor-1",
        "channel": "x",
        "alert_type": ALERT_TYPE_THRESHOLD,
        "alert_level": 2,
    }
    active_map = {key: existing}
    triggered: list[dict[str, Any]] = []

    result = engine._apply_condition(
        object(),
        active_map,
        triggered,
        rule={},
        sensor_no="sensor-1",
        channel="x",
        alert_type=ALERT_TYPE_THRESHOLD,
        active=True,
        alert_level=2,
        trigger_val=12.0,
        fire_time=datetime(2026, 10, 20, 12, 0, 0),
    )

    assert result is existing
    assert triggered == []


class RealtimeCursor:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.window_start: datetime | None = None
        self.sql_calls: list[str] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.sql_calls.append(sql)
        if "value IS NOT NULL" in sql:
            self.window_start = params[0]
            value = 11.0 + float(self.window_start.second)
            self.rows = [
                {
                    "s_no": "sensor-1",
                    "ch": "x",
                    "sample_cnt": 1,
                    "sample_times": [self.window_start],
                    "sample_values": [value],
                }
            ]
        else:
            self.rows = []

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


class EmptyRealtimeCursor(RealtimeCursor):
    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.rows = []


def test_realtime_threshold_streak_continues_across_one_second_scans(
    monkeypatch,
) -> None:
    engine = AlertAnalyticsEngine()
    key = engine.make_key("sensor-1", "x")
    engine.channel_states[key] = {
        "id": "threshold-1",
        "key": key,
        "sensor_no_lower": "sensor-1",
        "channel": "x",
        "instrument_id": None,
        "offline_timeout_ms": 5_000.0,
        "level1_upper": 30.0,
        "level2_upper": 20.0,
        "level3_upper": 10.0,
        "level1_lower": None,
        "level2_lower": None,
        "level3_lower": None,
        "physical_min": -100.0,
        "physical_max": 100.0,
        "threshold_confirm_samples": 3,
        "threshold_streak_count": 0,
        "threshold_condition_active": False,
        "threshold_condition_level": None,
        "oor_confirm_samples": 3,
        "oor_streak_count": 0,
        "oor_condition_active": False,
        "stuck_confirm_samples": 100,
        "stuck_streak_count": 0,
        "stuck_condition_active": False,
        "offline_condition_active": False,
    }
    engine._cache_sample_query(engine.channel_states.values())
    cursor = RealtimeCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    applied: list[dict[str, Any]] = []

    def fake_apply(cur, active_map, triggered, **kwargs):
        applied.append(kwargs)
        return kwargs

    def fail_cache(*args, **kwargs):
        raise AssertionError("sample query must be prepared before scanning")

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(engine, "_apply_condition", fake_apply)
    monkeypatch.setattr(engine, "_cache_sample_query", fail_cache)

    base = datetime(2026, 10, 20, 12, 0, 0)
    for offset in range(4):
        engine.scan_realtime_alerts(
            base.replace(second=offset),
            base.replace(second=offset + 1),
        )

    assert len(applied) == 1
    assert applied[0]["alert_type"] == ALERT_TYPE_THRESHOLD
    assert applied[0]["alert_level"] == 3
    assert sum("value IS NOT NULL" in sql for sql in cursor.sql_calls) == 4
    assert not any("LOWER(sensor_no) = %s" in sql for sql in cursor.sql_calls)


def test_no_data_still_alerts_offline_once_per_unrecovered_incident(
    monkeypatch,
) -> None:
    engine = AlertAnalyticsEngine()
    key = engine.make_key("sensor-1", "x")
    engine.channel_states[key] = {
        "key": key,
        "sensor_no_lower": "sensor-1",
        "channel": "x",
        "instrument_id": None,
        "offline_timeout_ms": 5_000.0,
        "last_ts": "2026-10-20 11:59:50",
        "threshold_condition_active": False,
        "threshold_condition_level": None,
        "oor_condition_active": False,
        "stuck_condition_active": False,
    }
    cursor = EmptyRealtimeCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    applied: list[dict[str, Any]] = []

    def fake_apply(cur, active_map, triggered, **kwargs):
        applied.append(kwargs)
        return kwargs

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(engine, "_apply_condition", fake_apply)

    base = datetime(2026, 10, 20, 12, 0, 0)
    engine.scan_realtime_alerts(base, base.replace(second=1))
    engine.scan_realtime_alerts(base.replace(second=1), base.replace(second=2))

    assert len(applied) == 1
    assert applied[0]["alert_type"] == ALERT_TYPE_OFFLINE


def test_any_sample_in_current_scan_keeps_channel_online(monkeypatch) -> None:
    engine = AlertAnalyticsEngine()
    key = engine.make_key("sensor-1", "x")
    engine.channel_states[key] = {
        "key": key,
        "sensor_no_lower": "sensor-1",
        "channel": "x",
        "instrument_id": None,
        "offline_timeout_ms": 1000.0,
        "last_ts": "2026-10-20 11:59:00",
        "level1_upper": 30.0,
        "level2_upper": 20.0,
        "level3_upper": 10.0,
        "level1_lower": None,
        "level2_lower": None,
        "level3_lower": None,
        "physical_min": -100.0,
        "physical_max": 100.0,
        "threshold_confirm_samples": 3,
        "threshold_condition_active": False,
        "threshold_condition_level": None,
        "oor_confirm_samples": 3,
        "oor_condition_active": False,
        "stuck_confirm_samples": 100,
        "stuck_condition_active": False,
        "offline_condition_active": False,
    }
    engine._cache_sample_query(engine.channel_states.values())
    offline_key = engine._alert_key("sensor-1", "x", ALERT_TYPE_OFFLINE)
    engine._active_alert_cache = {
        offline_key: {
            "id": "offline-alert",
            "sensor_no": "sensor-1",
            "channel": "x",
            "alert_type": ALERT_TYPE_OFFLINE,
        }
    }
    cursor = RealtimeCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    applied: list[dict[str, Any]] = []

    def fake_apply(cur, active_map, triggered, **kwargs):
        applied.append(kwargs)
        return kwargs

    closed: list[dict[str, Any]] = []

    def fake_close(cur, active_map, **kwargs):
        closed.append(kwargs)
        active_map.pop(offline_key, None)

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(engine, "_apply_condition", fake_apply)
    monkeypatch.setattr(engine, "_close_alert_incident", fake_close)

    window_start = datetime(2026, 10, 20, 12, 0, 0)
    engine.scan_realtime_alerts(
        window_start,
        window_start + timedelta(seconds=10),
    )

    assert not any(
        item["alert_type"] == ALERT_TYPE_OFFLINE for item in applied
    )
    assert engine.channel_states[key]["online_status"] == "ONLINE"
    assert engine.channel_states[key]["offline_condition_active"] is False
    assert closed == [
        {
            "sensor_no": "sensor-1",
            "channel": "x",
            "alert_type": ALERT_TYPE_OFFLINE,
            "end_time": window_start + timedelta(seconds=10),
        }
    ]


def test_scan_does_not_advance_channel_state_when_commit_fails(
    monkeypatch,
) -> None:
    engine = AlertAnalyticsEngine()
    key = engine.make_key("sensor-1", "x")
    engine.channel_states[key] = {
        "key": key,
        "sensor_no_lower": "sensor-1",
        "channel": "x",
        "instrument_id": None,
        "offline_timeout_ms": 5_000.0,
        "level1_upper": 30.0,
        "level2_upper": 20.0,
        "level3_upper": 10.0,
        "level1_lower": None,
        "level2_lower": None,
        "level3_lower": None,
        "physical_min": -100.0,
        "physical_max": 100.0,
        "last_ts": None,
        "threshold_confirm_samples": 3,
        "threshold_streak_count": 0,
        "threshold_condition_active": False,
        "threshold_condition_level": None,
        "oor_confirm_samples": 3,
        "oor_streak_count": 0,
        "oor_condition_active": False,
        "stuck_confirm_samples": 100,
        "stuck_streak_count": 0,
        "stuck_condition_active": False,
        "offline_condition_active": False,
    }
    engine._cache_sample_query(engine.channel_states.values())
    cursor = RealtimeCursor()

    @contextmanager
    def failing_get_db_cursor(commit: bool = False):
        yield cursor
        raise RuntimeError("commit failed")

    monkeypatch.setattr(engine_module, "get_db_cursor", failing_get_db_cursor)
    window_start = datetime(2026, 10, 20, 12, 0, 0)

    with pytest.raises(RuntimeError, match="commit failed"):
        engine.scan_realtime_alerts(window_start, window_start + timedelta(seconds=1))

    assert engine.channel_states.get(key)["last_ts"] is None


def test_existing_daq_alert_is_not_refreshed_after_worker_restart(
    monkeypatch,
) -> None:
    engine = AlertAnalyticsEngine()
    instrument_id = "instrument-1"
    window_end = datetime(2026, 10, 20, 12, 0, 10)
    for channel in ("x", "y"):
        key = engine.make_key(f"sensor-{channel}", channel)
        engine.channel_states[key] = {
            "key": key,
            "sensor_no_lower": f"sensor-{channel}",
            "channel": channel,
            "instrument_id": instrument_id,
            "offline_timeout_ms": 5_000.0,
            "last_ts": window_end.replace(second=0),
            "offline_condition_active": False,
        }

    active_daq = {
        "id": "daq-alert",
        "sensor_no": instrument_id,
        "channel": "__DAQ__",
        "alert_type": ALERT_TYPE_DAQ_OFFLINE,
        "alert_level": 1,
    }

    class DaqCursor(EmptyRealtimeCursor):
        def execute(
            self,
            sql: str,
            params: tuple[Any, ...] | None = None,
        ) -> None:
            if "FROM public.t_alert_record" in sql:
                self.rows = [active_daq]
            else:
                self.rows = []

    cursor = DaqCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(
        engine,
        "_apply_condition",
        lambda cur, active_map, triggered, **kwargs: kwargs,
    )

    def fail_upsert(*args, **kwargs):
        raise AssertionError("existing DAQ incident must not be refreshed")

    monkeypatch.setattr(engine, "_upsert_alert_incident", fail_upsert)

    engine.scan_realtime_alerts(
        window_end.replace(second=0),
        window_end,
    )

    assert engine._daq_condition_active[instrument_id] is True


def test_statistics_window_does_not_block_realtime_alert_scan(
    monkeypatch,
) -> None:
    engine = AlertAnalyticsEngine()
    statistics_started = threading.Event()
    release_statistics = threading.Event()

    def fake_statistics(window_start, window_end, period_type):
        statistics_started.set()
        assert release_statistics.wait(2)
        return {
            "rawPointsProcessed": 1,
            "upsertedStatsCount": 1,
            "triggeredAlertsCount": 0,
            "triggeredAlerts": [],
        }

    def fake_alert_scan(window_start, window_end):
        return {
            "rawPointsProcessed": 1,
            "upsertedStatsCount": 0,
            "triggeredAlertsCount": 0,
            "triggeredAlerts": [],
        }

    monkeypatch.setattr(engine, "_record_statistics_window", fake_statistics)
    monkeypatch.setattr(engine, "_scan_realtime_alerts", fake_alert_scan)

    statistics_thread = threading.Thread(
        target=engine.record_statistics_window,
        args=(
            datetime(2026, 10, 20, 12, 0),
            datetime(2026, 10, 20, 12, 1),
        ),
    )
    statistics_thread.start()
    assert statistics_started.wait(1)

    alert_result = engine.scan_realtime_alerts(
        datetime(2026, 10, 20, 12, 1, 1),
        datetime(2026, 10, 20, 12, 1, 2),
    )

    assert alert_result["rawPointsProcessed"] == 1
    release_statistics.set()
    statistics_thread.join(timeout=2)
    assert not statistics_thread.is_alive()


def test_statistics_window_uses_one_batch_upsert(monkeypatch) -> None:
    engine = AlertAnalyticsEngine()
    window_start = datetime(2026, 10, 20, 12, 0)
    window_end = datetime(2026, 10, 20, 12, 1)
    for sensor_no, channel in (("sensor-1", "x"), ("sensor-2", "y")):
        key = engine.make_key(sensor_no, channel)
        engine.channel_states[key] = {
            "key": key,
            "sensor_no_lower": sensor_no,
            "channel": channel,
        }

    class StatisticsCursor:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []
            self.sql_calls: list[str] = []

        def execute(
            self,
            sql: str,
            params: tuple[Any, ...] | None = None,
        ) -> None:
            self.sql_calls.append(sql)
            assert "GROUP BY LOWER(sensor_no), COALESCE(channel, '')" in sql
            self.rows = [
                {
                    "s_no": sensor_no,
                    "ch": channel,
                    "sample_cnt": 10,
                    "val_max": 2.0,
                    "val_min": 1.0,
                    "val_avg": 1.5,
                    "first_ts": window_start,
                    "last_ts": window_end - timedelta(seconds=1),
                    "min_time": window_start,
                    "max_time": window_end - timedelta(seconds=1),
                }
                for sensor_no, channel in (("sensor-1", "x"), ("sensor-2", "y"))
            ]

        def fetchall(self) -> list[dict[str, Any]]:
            return self.rows

    cursor = StatisticsCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    batches: list[tuple[str, list[tuple[Any, ...]], int]] = []

    def fake_execute_values(
        cur,
        sql: str,
        rows: list[tuple[Any, ...]],
        **kwargs: Any,
    ) -> None:
        assert cur is cursor
        batches.append((sql, rows, int(kwargs["page_size"])))
        assert "'system', NOW()" in kwargs["template"]

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(engine_module, "execute_values", fake_execute_values)

    result = engine.record_statistics_window(window_start, window_end)

    assert result["upsertedStatsCount"] == 2
    assert len(cursor.sql_calls) == 1
    assert len(batches) == 1
    assert len(batches[0][1]) == 2
    assert batches[0][2] == 2


def _add_offline_channel(
    engine: AlertAnalyticsEngine,
    instrument_id: str,
    sensor_no: str,
    channel: str,
    last_ts: datetime,
) -> None:
    """登记一个采集仪下属通道，用于采集仪离线聚合测试。"""
    key = engine.make_key(sensor_no, channel)
    engine.channel_states[key] = {
        "key": key,
        "sensor_no_lower": sensor_no,
        "channel": channel,
        "instrument_id": instrument_id,
        "offline_timeout_ms": 5_000.0,
        "last_ts": last_ts,
        "offline_condition_active": False,
    }


def test_daq_offline_requires_every_channel_offline(monkeypatch) -> None:
    """采集仪下仍有通道在线时不得产生采集仪离线预警。"""
    engine = AlertAnalyticsEngine()
    instrument_id = "instrument-1"
    window_end = datetime(2026, 10, 20, 12, 0, 10)
    stale_ts = window_end - timedelta(seconds=10)

    _add_offline_channel(engine, instrument_id, "sensor-x", "x", stale_ts)
    _add_offline_channel(engine, instrument_id, "sensor-y", "y", stale_ts)
    _add_offline_channel(
        engine,
        instrument_id,
        "sensor-z",
        "z",
        window_end - timedelta(seconds=1),
    )

    cursor = EmptyRealtimeCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    upserted: list[dict[str, Any]] = []

    def fake_apply_condition(cur, active_map, triggered, **kwargs):
        return kwargs

    def fake_upsert(cur, **kwargs):
        upserted.append(kwargs)
        return {"id": "daq-alert", **kwargs}

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(engine, "_apply_condition", fake_apply_condition)
    monkeypatch.setattr(engine, "_upsert_alert_incident", fake_upsert)

    engine.scan_realtime_alerts(window_end - timedelta(seconds=1), window_end)

    assert upserted == []
    assert engine._daq_condition_active[instrument_id] is False

    # 最后一个通道也离线后才汇总为采集仪级预警。
    _add_offline_channel(engine, instrument_id, "sensor-z", "z", stale_ts)
    result = engine.scan_realtime_alerts(
        window_end,
        window_end + timedelta(seconds=1),
    )

    assert [item["alert_type"] for item in upserted] == [ALERT_TYPE_DAQ_OFFLINE]
    assert result["triggeredAlertsCount"] == 1
    assert engine._daq_condition_active[instrument_id] is True


def test_disabled_alert_type_switch_skips_alert_creation() -> None:
    """关闭某类预警后条件命中也不再写入预警，其他类型不受影响。"""
    engine = AlertAnalyticsEngine(
        alert_type_switches={ALERT_TYPE_THRESHOLD: False},
    )
    created: list[dict[str, Any]] = []

    def fake_upsert(cur, **kwargs):
        created.append(kwargs)
        return {"id": "alert", **kwargs}

    engine._upsert_alert_incident = fake_upsert  # type: ignore[method-assign]

    result = engine._apply_condition(
        None,
        {},
        [],
        rule={"id": "threshold-1"},
        sensor_no="sensor-1",
        channel="x",
        alert_type=ALERT_TYPE_THRESHOLD,
        active=True,
        alert_level=1,
        trigger_val=12.0,
        fire_time=datetime(2026, 10, 20, 12, 0, 0),
    )

    assert result is None
    assert created == []
    assert engine.is_alert_type_enabled(ALERT_TYPE_THRESHOLD) is False
    assert engine.is_alert_type_enabled(ALERT_TYPE_STUCK) is True

    # 未关闭的预警类型仍然正常创建。
    result = engine._apply_condition(
        None,
        {},
        [],
        rule={"id": "threshold-1"},
        sensor_no="sensor-1",
        channel="x",
        alert_type=ALERT_TYPE_STUCK,
        active=True,
        alert_level=1,
        trigger_val=0.0,
        fire_time=datetime(2026, 10, 20, 12, 0, 0),
    )

    assert result is not None
    assert [item["alert_type"] for item in created] == [ALERT_TYPE_STUCK]


def test_daq_offline_switch_disables_collector_alert(monkeypatch) -> None:
    """采集仪离线开关关闭后，即使全部通道离线也不写采集仪预警。"""
    engine = AlertAnalyticsEngine(
        alert_type_switches={ALERT_TYPE_DAQ_OFFLINE: False},
    )
    instrument_id = "instrument-1"
    window_end = datetime(2026, 10, 20, 12, 0, 10)
    stale_ts = window_end - timedelta(seconds=10)
    for channel in ("x", "y", "z"):
        _add_offline_channel(
            engine,
            instrument_id,
            f"sensor-{channel}",
            channel,
            stale_ts,
        )

    cursor = EmptyRealtimeCursor()

    @contextmanager
    def fake_get_db_cursor(commit: bool = False):
        yield cursor

    def fake_apply_condition(cur, active_map, triggered, **kwargs):
        return kwargs

    def fail_upsert(*args, **kwargs):
        raise AssertionError("disabled DAQ switch must not create alerts")

    monkeypatch.setattr(engine_module, "get_db_cursor", fake_get_db_cursor)
    monkeypatch.setattr(engine, "_apply_condition", fake_apply_condition)
    monkeypatch.setattr(engine, "_upsert_alert_incident", fail_upsert)

    result = engine.scan_realtime_alerts(
        window_end - timedelta(seconds=1),
        window_end,
    )

    assert result["triggeredAlertsCount"] == 0
    assert instrument_id not in engine._daq_condition_active
