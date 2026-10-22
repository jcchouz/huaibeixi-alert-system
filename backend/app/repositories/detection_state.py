"""Persistence helpers for detection channel state and worker watermarks."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from psycopg2.extras import Json, execute_values

from app.core.serialization import serialize_val

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


def load_channel_states(
    cur: Any,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, bool]]:
    """Load ordinary channel state and instrument-level DAQ state."""

    cur.execute(
        """
        SELECT sensor_no, channel, state
        FROM public.t_detection_channel_state
        """
    )
    channel_states: Dict[str, Dict[str, Any]] = {}
    daq_states: Dict[str, bool] = {}
    for row in cur.fetchall():
        sensor_no = str(row["sensor_no"]).strip().lower()
        channel = str(row["channel"] or "").strip()
        state = dict(row["state"] or {})
        if channel == "__DAQ__":
            daq_states[sensor_no] = bool(state.get("daq_condition_active", False))
            continue
        channel_states[f"{sensor_no}:{channel}"] = state
    return channel_states, daq_states


def persist_channel_states(
    cur: Any,
    rows: Iterable[Tuple[str, str, Dict[str, Any]]],
) -> int:
    """Upsert runtime state rows inside the caller's transaction."""

    values = [
        (
            sensor_no,
            channel,
            Json(serialize_val(state)),
        )
        for sensor_no, channel, state in rows
    ]
    if not values:
        return 0
    execute_values(
        cur,
        """
        INSERT INTO public.t_detection_channel_state (
            sensor_no, channel, state, updated_at
        ) VALUES %s
        ON CONFLICT (sensor_no, channel)
        DO UPDATE SET
            state = EXCLUDED.state,
            updated_at = LOCALTIMESTAMP
        """,
        values,
        template="(%s, %s, %s, LOCALTIMESTAMP)",
        page_size=len(values),
    )
    return len(values)


def load_worker_watermark(cur: Any, worker_name: str) -> Optional[Dict[str, Any]]:
    """Return one worker's persisted cursor row."""

    cur.execute(
        """
        SELECT last_processed_time, last_completed_time
        FROM public.t_detection_worker_state
        WHERE worker_name = %s
        """,
        (worker_name,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def save_worker_watermark(
    cur: Any,
    *,
    worker_name: str,
    last_processed_time: datetime,
    last_completed_time: datetime,
) -> None:
    """Persist a worker cursor after successful processing."""

    cur.execute(
        """
        INSERT INTO public.t_detection_worker_state (
            worker_name, last_processed_time, last_completed_time, updated_at
        ) VALUES (%s, %s, %s, LOCALTIMESTAMP)
        ON CONFLICT (worker_name)
        DO UPDATE SET
            last_processed_time = EXCLUDED.last_processed_time,
            last_completed_time = EXCLUDED.last_completed_time,
            updated_at = LOCALTIMESTAMP
        """,
        (worker_name, last_processed_time, last_completed_time),
    )


def load_rule_version(cur: Any) -> Optional[int]:
    """Return the singleton threshold-rule version."""

    cur.execute(
        """
        SELECT version
        FROM public.t_detection_rule_version
        WHERE id = 1
        """
    )
    row = cur.fetchone()
    return int(row["version"]) if row else None


def state_rows_for_persistence(
    rules_by_key: Dict[str, Dict[str, Any]],
    pending_state_updates: Dict[str, Dict[str, Any]],
    daq_condition_updates: Dict[str, bool],
) -> list[Tuple[str, str, Dict[str, Any]]]:
    """Build compact JSON payloads for all state changes in one scan."""

    rows: list[Tuple[str, str, Dict[str, Any]]] = []
    for key, updates in pending_state_updates.items():
        rule = rules_by_key.get(key)
        if not rule:
            continue
        state = {
            field: rule.get(field)
            for field in RUNTIME_STATE_FIELDS
            if field in rule
        }
        state.update(updates)
        rows.append(
            (
                str(rule["sensor_no_lower"]),
                str(rule.get("channel") or ""),
                state,
            )
        )
    for instrument_id, condition_active in daq_condition_updates.items():
        rows.append(
            (
                str(instrument_id).strip().lower(),
                "__DAQ__",
                {"daq_condition_active": bool(condition_active)},
            )
        )
    return rows
