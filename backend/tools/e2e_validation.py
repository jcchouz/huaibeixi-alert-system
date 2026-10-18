#!/usr/bin/env python
"""End-to-end validation harness for the bridge alert & analytics engine.

Phases
------
precheck : inspect DB state, rule set and wind-rose expectations (read-only)
seed     : write synthetic ``t_monitor`` traffic (default 4000 rows/s)
detect   : drive ``AlertAnalyticsEngine.step_detection`` window by window
verify   : assert produced alerts / 1m stats / 10m wind-rose
clean    : delete synthetic rows and restore the ``t_alert_record`` snapshot
all      : seed -> detect -> verify -> clean

The harness only touches:
  * ``public.t_monitor``      synthetic window rows (deleted in clean)
  * ``public.t_stat_record``  engine-produced rows for that window (deleted)
  * ``public.t_alert_record`` engine-produced rows (fully snapshotted/restored)
"""
from __future__ import annotations

import argparse
import io
import math
import sys
import time
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import psycopg2
import psycopg2.extras

from app.config import DSN, get_db_cursor
from app.engine import AlertAnalyticsEngine

# --------------------------------------------------------------------------- #
# Test plan constants
# --------------------------------------------------------------------------- #
STREAM_START = datetime(2026, 10, 18, 22, 0, 0)
WINDOW_SECONDS = 60
WIND_START = datetime(2026, 10, 18, 21, 50, 0)
WIND_END = datetime(2026, 10, 18, 22, 0, 0)
TEST_END = STREAM_START + timedelta(seconds=6 * WINDOW_SECONDS)

DEFAULT_TARGET_RATE = 4000.0
DEFAULT_WINDOWS = 4
INSERT_BATCH = 20000

# (lower sensor_no, channel) -> {window_index: spec}
ANOMALIES: Dict[Tuple[str, str], Dict[int, Dict[str, Any]]] = {
    ("wy-sj-01", "x"): {
        0: {"mode": "band", "lo": 36.5, "hi": 37.5},     # L3 (yellow)
        1: {"mode": "band", "lo": 39.5, "hi": 40.5},     # L2 (orange) escalation
        2: {"mode": "band", "lo": 43.5, "hi": 44.5},     # L1 (red) escalation
        3: {"mode": "band", "lo": 28.0, "hi": 34.0},     # back inside L3 -> recover
    },
    ("wy-sj-01", "y"): {0: {"mode": "const", "v": 30.0}},   # STUCK
    ("zd-wg-01", "x"): {1: {"mode": "skip"}, 2: {"mode": "skip"}},   # OFFLINE child
    ("zd-wg-01", "y"): {1: {"mode": "skip"}, 2: {"mode": "skip"}},   # -> DAQ_OFFLINE
    ("wprs-02", ""): {0: {"mode": "pairs", "points": [(0.0, 0.0), (1.0, 100.0)]}},  # RATE
    ("wlg-10", ""): {
        1: {"mode": "band", "lo": -260.0, "hi": -255.0},  # LOWER L3
        2: {"mode": "band", "lo": -330.0, "hi": -322.0},  # OOR + LOWER L1
    },
    ("yb-sx01-a", "stress"): {0: {"mode": "pairs", "points": [(0.0, 0.0), (1.0, 0.0), (5.0, 0.0)]}},  # GAP
    ("accl-02", "x"): {0: {"mode": "spike", "base": 0.0, "spike": 1.2}},  # SPIKE
    ("wlg-08", ""): {0: {"mode": "mixedcase"}},   # DATA_CONFLICT
}

# dedup_key -> (final alert_status, final alert_level)
# The device-level DAQ alert key is added dynamically in ``phase_verify`` because
# the engine keys it on the collect-instrument id, not on the sensor number.
EXPECTED_ALERTS: List[Tuple[str, int, int]] = [
    ("wy-sj-01:x:THRESHOLD:UPPER", 2, 1),
    ("wy-sj-01:y:STUCK:NONE", 2, 3),
    ("zd-wg-01:x:OFFLINE:NONE", 2, 1),
    ("zd-wg-01:y:OFFLINE:NONE", 2, 1),
    ("wprs-02::RATE:NONE", 2, 2),
    ("yb-sx01-a:stress:GAP_1000MS:NONE", 2, 3),
    ("accl-02:x:SPIKE:NONE", 2, 2),
    ("wlg-08::DATA_CONFLICT:NONE", 2, 3),
    ("wlg-10::THRESHOLD:LOWER", 2, 1),
    ("wlg-10::OOR:NONE", 2, 2),
]

DAQ_SENSOR = "zd-wg-01"

WIND_SENSORS = ("wea-01", "fhj-01")
WIND_EXPECT = {
    "pair_count": 600,
    "vector_speed": math.sqrt(1500.0 ** 2 + 900.0 ** 2) / 600.0,   # ~2.915475
    "vector_direction": math.degrees(math.atan2(1500.0, -900.0)),   # ~120.9638
    "scalar_speed": 4.0,
    "sector": 6,       # ESE
    "beaufort_level": 2,
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def seq_dt(i: int, win_start: datetime, n: int) -> datetime:
    return win_start + timedelta(seconds=60.0 * i / n)


def crc(key: str) -> int:
    return zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF


def rule_key(rule: Dict[str, Any]) -> Tuple[str, str]:
    return ((rule.get("sensor_no") or "").strip().lower(), (rule.get("channel") or "").strip())


def normal_band(rule: Dict[str, Any]) -> Tuple[float, float]:
    """Centre/amplitude of a benign signal that sits inside level-3 limits."""
    ext = rule.get("ext_params") or {}
    lo = rule.get("level3_lower")
    hi = rule.get("level3_upper")
    pmin = ext.get("physical_min")
    pmax = ext.get("physical_max")
    lo = float(lo) if lo is not None else (float(pmin) if pmin is not None else 0.0)
    hi = float(hi) if hi is not None else (float(pmax) if pmax is not None else 1.0)
    if pmin is not None and float(pmin) > lo:
        lo = float(pmin)
    if pmax is not None and float(pmax) < hi:
        hi = float(pmax)
    if hi <= lo:
        hi = lo + max(1.0, abs(lo) * 0.01)
    centre = (lo + hi) / 2.0
    amp = (hi - lo) * 0.02
    if amp <= 1e-6:
        amp = 0.01 * max(1.0, abs(centre))
    return centre, amp


def normal_value(rule: Dict[str, Any], i: int) -> float:
    centre, amp = normal_band(rule)
    phase = (crc(":".join(rule_key(rule))) % 360) * math.pi / 180.0
    return centre + amp * math.sin(phase + i * 0.7)


def window_points(rule: Dict[str, Any], n: int, spec: Optional[Dict[str, Any]]) -> List[Tuple[float, float]]:
    """Return [(offset_seconds, value), ...] for one rule in one window."""
    mode = spec.get("mode") if spec else None
    if mode == "pairs":
        return [(float(off), float(val)) for off, val in spec["points"]]
    pts: List[Tuple[float, float]] = []
    for i in range(n):
        off = 60.0 * i / n
        if mode == "band":
            lo, hi = float(spec["lo"]), float(spec["hi"])
            v = lo + (hi - lo) * (math.sin(i * 0.37) + 1.0) / 2.0
        elif mode == "const":
            v = float(spec["v"])
        elif mode == "spike":
            v = float(spec["base"])
            if i == n // 2:
                v = float(spec["spike"])
        else:
            v = normal_value(rule, i)
        pts.append((off, v))
    return pts


def load_rules() -> List[Dict[str, Any]]:
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM public.t_alert_threshold
            WHERE is_delete = false AND monitor_enabled = true
            ORDER BY sensor_no, channel
            """
        )
        return [dict(r) for r in cur.fetchall()]


def nominal_rate(rules: List[Dict[str, Any]]) -> float:
    total = 0.0
    for r in rules:
        iv = int(r.get("expected_interval_ms") or 1000)
        total += 1000.0 / max(1, iv)
    return total


def build_window_rows(
    rules: List[Dict[str, Any]],
    win_start: datetime,
    win_idx: int,
    scale: float,
) -> List[Tuple[str, str, float, datetime]]:
    rows: List[Tuple[str, str, float, datetime]] = []
    for rule in rules:
        sensor = rule.get("sensor_no") or ""
        ch = (rule.get("channel") or "").strip()
        spec = ANOMALIES.get(rule_key(rule), {}).get(win_idx)
        if spec and spec.get("mode") == "skip":
            continue
        iv = int(rule.get("expected_interval_ms") or 1000)
        hz = 1000.0 / max(1, iv)
        n = max(1, int(round(hz * WINDOW_SECONDS * scale)))
        for i, (off, val) in enumerate(window_points(rule, n, spec)):
            emit_sensor = sensor
            if spec and spec.get("mode") == "mixedcase":
                emit_sensor = sensor.upper() if i % 2 == 0 else sensor.lower()
            rows.append((emit_sensor, ch, float(val), win_start + timedelta(seconds=off)))
    return rows


def build_wind_rows() -> List[Tuple[str, str, float, datetime]]:
    rows: List[Tuple[str, str, float, datetime]] = []
    total = int((WIND_END - WIND_START).total_seconds())
    for i in range(total):
        t = WIND_START + timedelta(seconds=i)
        speed, direction = (5.0, 90.0) if i < total // 2 else (3.0, 180.0)
        for sensor in WIND_SENSORS:
            rows.append((sensor, "windSpeed", speed, t))
            rows.append((sensor, "windDirection", direction, t))
    return rows


def insert_rows(rows: List[Tuple[Any, ...]], target_rate: float, pace: bool = True) -> float:
    """Insert rows paced to ``target_rate`` rows/s; returns achieved rows/s."""
    if not rows:
        return 0.0
    conn = psycopg2.connect(DSN)
    started = time.perf_counter()
    sent = 0
    try:
        cur = conn.cursor()
        for start in range(0, len(rows), INSERT_BATCH):
            batch = rows[start:start + INSERT_BATCH]
            psycopg2.extras.execute_values(
                cur,
                "INSERT INTO public.t_monitor (sensor_no, channel, value, time) VALUES %s",
                batch,
                page_size=1000,
            )
            conn.commit()
            sent += len(batch)
            if pace and target_rate > 0:
                target_elapsed = sent / target_rate
                actual = time.perf_counter() - started
                if actual < target_elapsed:
                    time.sleep(target_elapsed - actual)
        elapsed = time.perf_counter() - started
    finally:
        conn.close()
    return sent / elapsed if elapsed > 0 else 0.0


def fetch_alert_map() -> Dict[str, Dict[str, Any]]:
    """dedup_key -> latest alert row (by seq)."""
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM public.t_alert_record
            WHERE dedup_key IS NOT NULL
            ORDER BY seq ASC
            """
        )
        out: Dict[str, Dict[str, Any]] = {}
        for r in cur.fetchall():
            out[r["dedup_key"]] = dict(r)
        return out


def events_of(row: Dict[str, Any]) -> List[str]:
    log = row.get("timeline_log") or []
    return [str(e.get("event")) for e in log if isinstance(e, dict)]


def snapshot_alert_record(conn: Any) -> bytes:
    buf = io.BytesIO()
    cur = conn.cursor()
    cur.copy_expert("COPY public.t_alert_record TO STDOUT WITH BINARY", buf)
    return buf.getvalue()


def restore_alert_record(conn: Any, payload: bytes) -> None:
    cur = conn.cursor()
    cur.execute("DELETE FROM public.t_alert_record")
    buf = io.BytesIO(payload)
    cur.copy_expert("COPY public.t_alert_record FROM STDIN WITH BINARY", buf)
    cur.execute(
        "SELECT setval('public.seq_alert_record', "
        "(SELECT COALESCE(MAX(seq), 0) FROM public.t_alert_record), true)"
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Phases
# --------------------------------------------------------------------------- #
def phase_precheck(args: argparse.Namespace) -> bool:
    rules = load_rules()
    rate = nominal_rate(rules)
    scale = args.target_rate / rate if rate else 1.0
    print(f"[precheck] enabled rules          : {len(rules)}")
    print(f"[precheck] nominal channel rate   : {rate:.1f} rows/s")
    print(f"[precheck] target rate            : {args.target_rate:.0f} rows/s")
    print(f"[precheck] scale factor           : {scale:.4f}")
    print(f"[precheck] stream window          : {STREAM_START} .. {STREAM_START + timedelta(seconds=args.windows * 60)}")
    print(f"[precheck] wind window            : {WIND_START} .. {WIND_END}")
    print(f"[precheck] wind expectation       : {WIND_EXPECT}")

    rows_per_window = 0
    for win_idx in range(args.windows):
        n = sum(
            max(1, int(round((1000.0 / max(1, int(r.get('expected_interval_ms') or 1000)))
                             * WINDOW_SECONDS * scale)))
            for r in rules
            if not (ANOMALIES.get(rule_key(r), {}).get(win_idx, {}).get("mode") == "skip")
        )
        rows_per_window += n
        print(f"[precheck] window {win_idx} rows       : {n}")
    print(f"[precheck] total synthetic rows   : {rows_per_window} "
          f"(~{rows_per_window / args.target_rate:.1f}s of traffic)")

    with get_db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM public.t_alert_record")
        print(f"[precheck] t_alert_record rows    : {cur.fetchone()['c']}")
        cur.execute(
            "SELECT COUNT(*) AS c FROM public.t_monitor "
            "WHERE time >= %s AND time < %s",
            (WIND_START, TEST_END),
        )
        print(f"[precheck] leftover test rows     : {cur.fetchone()['c']}")
    return True


def phase_seed(args: argparse.Namespace) -> Tuple[bool, Optional[bytes]]:
    rules = load_rules()
    rate = nominal_rate(rules)
    scale = args.target_rate / rate if rate else 1.0

    conn = psycopg2.connect(DSN)
    try:
        snapshot = snapshot_alert_record(conn)
        print(f"[seed] t_alert_record snapshot    : {len(snapshot)} bytes")
    finally:
        conn.close()

    wind_rows = build_wind_rows()
    print(f"[seed] wind rows                  : {len(wind_rows)}")
    pace = not args.no_pace
    achieved = insert_rows(wind_rows, args.target_rate, pace=pace)
    print(f"[seed] wind achieved rate         : {achieved:.0f} rows/s")

    total = 0
    for win_idx in range(args.windows):
        win_start = STREAM_START + timedelta(seconds=win_idx * WINDOW_SECONDS)
        rows = build_window_rows(rules, win_start, win_idx, scale)
        t0 = time.perf_counter()
        achieved = insert_rows(rows, args.target_rate, pace=pace)
        total += len(rows)
        print(
            f"[seed] window {win_idx} @ {win_start:%H:%M:%S}: "
            f"{len(rows)} rows, {achieved:.0f} rows/s, {time.perf_counter() - t0:.1f}s"
        )
    print(f"[seed] total synthetic rows       : {total}")
    return True, snapshot


def phase_detect(args: argparse.Namespace) -> bool:
    engine = AlertAnalyticsEngine()
    engine.mode = "REPLAY"
    engine.initialize()
    print(f"[detect] rules in memory          : {len(engine.channel_states)}")
    for win_idx in range(args.windows):
        ws = STREAM_START + timedelta(seconds=win_idx * WINDOW_SECONDS)
        we = ws + timedelta(seconds=WINDOW_SECONDS)
        res = engine.step_detection(ws, we)
        print(
            f"[detect] window {win_idx} @ {ws:%H:%M:%S}: raw={res['rawPointsProcessed']} "
            f"stats={res['upsertedStatsCount']} alerts={res['triggeredAlertsCount']} "
            f"elapsed={res['elapsedMs']}ms"
        )
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT dedup_key, alert_type, alert_level, alert_status, direction
                FROM public.t_alert_record
                WHERE is_delete = false AND peak_time >= %s AND peak_time < %s
                ORDER BY dedup_key
                """,
                (ws, we),
            )
            rows = cur.fetchall()
            if len(rows) > 40:
                print(f"          ({len(rows)} alerts in window, showing 40)")
            for r in rows[:40]:
                print(
                    f"          - {r['dedup_key']:<45} type={r['alert_type']:<14} "
                    f"L{r['alert_level']} status={r['alert_status']} dir={r['direction']}"
                )
    return True


class Checker:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        self.checks += 1
        if ok:
            print(f"[verify] PASS  {label}")
            return
        self.failures += 1
        print(f"[verify] FAIL  {label}  {detail}")


def phase_verify(args: argparse.Namespace) -> bool:
    c = Checker()
    alerts = fetch_alert_map()

    expected_list = list(EXPECTED_ALERTS)
    with get_db_cursor() as cur:
        cur.execute("SELECT instrument_id FROM public.t_sensor WHERE LOWER(sensor_no) = %s LIMIT 1", (DAQ_SENSOR,))
        r = cur.fetchone()
    if r and r["instrument_id"]:
        expected_list.append((f"{r['instrument_id'].lower()}:__DAQ__:DAQ_OFFLINE:NONE", 2, 1))

    for key, status, level in expected_list:
        row = alerts.get(key)
        if row is None:
            c.check(False, f"alert {key} exists", "not found")
            continue
        c.check(
            int(row["alert_status"]) == status,
            f"alert {key} status == {status}",
            f"got {row['alert_status']} (level {row['alert_level']}, tc {row['trigger_count']})",
        )
        c.check(int(row["alert_level"]) == level, f"alert {key} level == {level}", f"got {row['alert_level']}")

    # escalation / debounce specifics
    row = alerts.get("wy-sj-01:x:THRESHOLD:UPPER")
    if row:
        c.check(int(row["trigger_count"]) >= 3, "wy-sj-01/x THRESHOLD debounced across windows",
                f"trigger_count={row['trigger_count']}")
        c.check(row["end_ts"] is not None, "wy-sj-01/x THRESHOLD auto-recovered (end_ts set)")
        ev = events_of(row)
        c.check("TRIGGER" in ev and "RECOVER" in ev, "wy-sj-01/x THRESHOLD timeline has TRIGGER+RECOVER", str(ev))
        snap = row.get("threshold_snapshot") or {}
        c.check(bool(snap) and "level1" in snap, "wy-sj-01/x THRESHOLD snapshot persisted", str(snap)[:120])
        c.check(int(row.get("rule_version") or 0) >= 1, "wy-sj-01/x THRESHOLD rule_version set")

    row = alerts.get("zd-wg-01:x:OFFLINE:NONE")
    if row:
        ev = events_of(row)
        c.check("SUPPRESSED_BY_DAQ" in ev, "zd-wg-01/x child OFFLINE suppressed by DAQ", str(ev))
    row = alerts.get("zd-wg-01:y:OFFLINE:NONE")
    if row:
        ev = events_of(row)
        c.check("SUPPRESSED_BY_DAQ" in ev, "zd-wg-01/y child OFFLINE suppressed by DAQ", str(ev))
    daq_key = next((k for k, _, _ in expected_list if k.endswith(":__DAQ__:DAQ_OFFLINE:NONE")), None)
    row = alerts.get(daq_key) if daq_key else None
    if row:
        c.check(row["end_ts"] is not None, "DAQ_OFFLINE auto-recovered after instrument resumes")
        c.check(int(row["trigger_count"]) >= 2, "DAQ_OFFLINE aggregated over >=2 windows",
                f"trigger_count={row['trigger_count']}")

    # 1m statistics
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM public.t_stat_record
            WHERE period_type = '1m' AND is_delete = false
              AND stat_time >= %s AND stat_time < %s
            """,
            (STREAM_START, STREAM_START + timedelta(seconds=args.windows * 60)),
        )
        got = int(cur.fetchone()["c"])
        skipped = 0
        for key, spec in ANOMALIES.items():
            skipped += sum(1 for w, s in spec.items() if s.get("mode") == "skip" and w < args.windows)
        expected = len(load_rules()) * args.windows - skipped
        c.check(got == expected, f"1m stat rows == {expected}", f"got {got}")

        cur.execute(
            """
            SELECT val_max, val_min, val_avg, max_time, min_time, sample_count
            FROM public.t_stat_record
            WHERE period_type = '1m' AND sensor_no = 'wy-sj-01' AND channel = 'x'
              AND stat_time = %s
            """,
            (STREAM_START,),
        )
        r = cur.fetchone()
        if r is None:
            c.check(False, "1m stat row for wy-sj-01/x @22:00 exists", "missing")
        else:
            c.check(37.4 <= float(r["val_max"]) <= 37.6, "wy-sj-01/x 1m val_max ~= 37.5", f"got {r['val_max']}")
            c.check(36.4 <= float(r["val_min"]) <= 36.6, "wy-sj-01/x 1m val_min ~= 36.5", f"got {r['val_min']}")
            c.check(
                STREAM_START <= r["max_time"] < STREAM_START + timedelta(seconds=60),
                "wy-sj-01/x 1m max_time inside the window",
                str(r["max_time"]),
            )
            c.check(int(r["sample_count"]) > 0, "wy-sj-01/x 1m sample_count > 0", str(r["sample_count"]))

        cur.execute(
            """
            SELECT gap_count, sample_count
            FROM public.t_stat_record
            WHERE period_type = '1m' AND sensor_no = 'yb-sx01-a' AND channel = 'stress'
              AND stat_time = %s
            """,
            (STREAM_START,),
        )
        r = cur.fetchone()
        if r is None:
            c.check(False, "1m stat row for yb-sx01-a/stress @22:00 exists", "missing")
        else:
            c.check(int(r["gap_count"]) == 1, "yb-sx01-a/stress gap_count == 1", f"got {r['gap_count']}")
            c.check(int(r["sample_count"]) == 3, "yb-sx01-a/stress sample_count == 3", f"got {r['sample_count']}")

        # 10m wind rose
        for sensor in WIND_SENSORS:
            cur.execute(
                """
                SELECT ext_json, sample_count
                FROM public.t_stat_record
                WHERE period_type = '10m' AND channel = 'wind_rose'
                  AND sensor_no = %s AND stat_time = %s
                """,
                (sensor, WIND_START),
            )
            r = cur.fetchone()
            if r is None:
                c.check(False, f"10m wind_rose row for {sensor} @21:50 exists", "missing")
                continue
            ext = r["ext_json"] or {}
            c.check(int(ext.get("pair_count") or 0) == WIND_EXPECT["pair_count"],
                    f"{sensor} wind_rose pair_count == {WIND_EXPECT['pair_count']}", str(ext.get("pair_count")))
            c.check(abs(float(ext.get("vector_speed") or 0) - WIND_EXPECT["vector_speed"]) < 1e-3,
                    f"{sensor} wind_rose vector_speed ~= {WIND_EXPECT['vector_speed']:.4f}",
                    str(ext.get("vector_speed")))
            c.check(abs(float(ext.get("vector_direction") or 0) - WIND_EXPECT["vector_direction"]) < 1e-3,
                    f"{sensor} wind_rose vector_direction ~= {WIND_EXPECT['vector_direction']:.4f}",
                    str(ext.get("vector_direction")))
            c.check(abs(float(ext.get("scalar_speed") or 0) - WIND_EXPECT["scalar_speed"]) < 1e-3,
                    f"{sensor} wind_rose scalar_speed ~= {WIND_EXPECT['scalar_speed']}", str(ext.get("scalar_speed")))
            c.check(int(ext.get("sector") or 0) == WIND_EXPECT["sector"],
                    f"{sensor} wind_rose sector == {WIND_EXPECT['sector']}", str(ext.get("sector")))
            c.check(int(ext.get("beaufort_level") or 0) == WIND_EXPECT["beaufort_level"],
                    f"{sensor} wind_rose beaufort == {WIND_EXPECT['beaufort_level']}", str(ext.get("beaufort_level")))

    # unexpected alerts created in the synthetic window
    expected_keys = {k for k, _, _ in expected_list}
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT dedup_key
            FROM public.t_alert_record
            WHERE is_delete = false AND peak_time >= %s AND peak_time < %s
            """,
            (STREAM_START, STREAM_START + timedelta(seconds=args.windows * 60)),
        )
        seen = {r["dedup_key"] for r in cur.fetchall()}
    extra = sorted(seen - expected_keys)
    if extra:
        print(f"[verify] INFO  {len(extra)} additional alerts appeared in the window:")
        for k in extra[:20]:
            row = alerts.get(k)
            print(f"          ~ {k} status={row['alert_status'] if row else '?'}")
        if len(extra) > 20:
            print(f"          ... {len(extra) - 20} more")

    print(f"[verify] {c.checks - c.failures}/{c.checks} checks passed")
    return c.failures == 0


def phase_clean(args: argparse.Namespace, snapshot: Optional[bytes]) -> bool:
    conn = psycopg2.connect(DSN)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM public.t_monitor WHERE time >= %s AND time < %s", (WIND_START, TEST_END))
        print(f"[clean] deleted {cur.rowcount} t_monitor rows")
        cur.execute(
            "DELETE FROM public.t_stat_record WHERE stat_time >= %s AND stat_time < %s",
            (WIND_START, TEST_END),
        )
        print(f"[clean] deleted {cur.rowcount} t_stat_record rows")
        conn.commit()
        if snapshot is not None:
            cur.execute("SELECT COUNT(*) FROM public.t_alert_record")
            before = cur.fetchone()[0]
            restore_alert_record(conn, snapshot)
            cur.execute("SELECT COUNT(*) FROM public.t_alert_record")
            after = cur.fetchone()[0]
            print(f"[clean] restored t_alert_record: {before} -> {after} rows")
        else:
            cur.execute(
                "DELETE FROM public.t_alert_record WHERE peak_time >= %s AND peak_time < %s",
                (WIND_START, TEST_END),
            )
            conn.commit()
            print(f"[clean] deleted {cur.rowcount} synthetic alert rows (no snapshot)")
    finally:
        conn.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=["precheck", "seed", "detect", "verify", "clean", "all"],
        nargs="?",
        default="all",
    )
    parser.add_argument("--target-rate", type=float, default=DEFAULT_TARGET_RATE)
    parser.add_argument("--windows", type=int, default=DEFAULT_WINDOWS)
    parser.add_argument("--no-pace", action="store_true", help="insert as fast as possible (smoke test)")
    args = parser.parse_args()

    if args.phase == "precheck":
        return 0 if phase_precheck(args) else 1
    if args.phase == "seed":
        ok, _ = phase_seed(args)
        return 0 if ok else 1
    if args.phase == "detect":
        return 0 if phase_detect(args) else 1
    if args.phase == "verify":
        return 0 if phase_verify(args) else 1
    if args.phase == "clean":
        return 0 if phase_clean(args, None) else 1

    ok, snapshot = phase_seed(args)
    if not ok:
        return 1
    if not phase_detect(args):
        return 1
    passed = phase_verify(args)
    phase_clean(args, snapshot)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
