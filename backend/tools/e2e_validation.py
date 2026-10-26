#!/usr/bin/env python
"""桥梁预警与统计分析引擎端到端验证工具。

执行阶段
--------
precheck : 只读检查数据库状态和规则集
seed     : 写入模拟 ``t_monitor`` 数据，默认每秒 4000 行
detect   : 按窗口分别驱动实时告警扫描和统计窗口写入
verify   : 校验生成的预警和一分钟统计
clean    : 删除模拟数据并恢复 ``t_alert_record`` 快照
all      : seed -> detect -> verify -> clean

本工具只操作以下数据：
  * ``public.t_monitor``      模拟窗口数据，在 clean 阶段删除
  * ``public.t_stat_record``  引擎为该窗口生成的统计，在 clean 阶段删除
  * ``public.t_alert_record`` 引擎生成的预警，执行前快照并在结束后恢复
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

from app.core.config import get_settings
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.database import get_db_cursor

DSN = get_settings().dsn

# --------------------------------------------------------------------------- #
# 测试计划常量
# --------------------------------------------------------------------------- #
STREAM_START = datetime(2026, 10, 18, 22, 0, 0)
WINDOW_SECONDS = 60
TEST_END = STREAM_START + timedelta(seconds=6 * WINDOW_SECONDS)

DEFAULT_TARGET_RATE = 4000.0
DEFAULT_WINDOWS = 4
INSERT_BATCH = 20000

# (小写 sensor_no, channel) -> {窗口序号: 场景配置}
ANOMALIES: Dict[Tuple[str, str], Dict[int, Dict[str, Any]]] = {
    ("wy-sj-01", "x"): {
        0: {"mode": "band", "lo": 36.5, "hi": 37.5},     # 三级黄色预警
        1: {"mode": "band", "lo": 39.5, "hi": 40.5},     # 升级为二级橙色预警
        2: {"mode": "band", "lo": 43.5, "hi": 44.5},     # 升级为一级红色预警
        3: {"mode": "band", "lo": 28.0, "hi": 34.0},     # 回到三级阈值内，模拟恢复
    },
    ("wy-sj-01", "y"): {0: {"mode": "const", "v": 30.0}},   # 传感器死值
    ("zd-wg-01", "x"): {1: {"mode": "skip"}, 2: {"mode": "skip"}},   # 传感器离线
    # 采集仪离线要求该采集仪下全部通道同时离线（ZD-WG-01 的 x/y/z + test-007）。
    ("zd-wg-01", "y"): {1: {"mode": "skip"}, 2: {"mode": "skip"}},   # 采集仪离线
    ("zd-wg-01", "z"): {1: {"mode": "skip"}, 2: {"mode": "skip"}},   # 采集仪离线
    ("test-007", ""): {1: {"mode": "skip"}, 2: {"mode": "skip"}},    # 采集仪离线
    ("wlg-10", ""): {
        1: {"mode": "band", "lo": -260.0, "hi": -255.0},  # 低于三级下限
        2: {"mode": "band", "lo": -330.0, "hi": -322.0},  # 超物理量程且低于一级下限
    },
    ("yb-sx01-a", "stress"): {0: {"mode": "pairs", "points": [(0.0, 0.0), (1.0, 0.0), (5.0, 0.0)]}},  # 采样间隔异常
}

# 传感器标识 -> (最终 alert_status, 最终 alert_level)
# 采集仪级预警的唯一键使用采集仪 id，而不是传感器编号，
# 因此会在 ``phase_verify`` 中动态加入预期结果。
EXPECTED_ALERTS: List[Tuple[str, int, int]] = [
    ("wy-sj-01:x:阈值超限", 0, 1),
    ("wy-sj-01:y:传感器死值", 0, 1),
    ("zd-wg-01:x:传感器离线", 0, 1),
    ("zd-wg-01:y:传感器离线", 0, 1),
    ("zd-wg-01:z:传感器离线", 0, 1),
    ("test-007::传感器离线", 0, 1),
    ("wlg-10::阈值超限", 0, 1),
    ("wlg-10::超物理量程", 0, 1),
]

DAQ_SENSOR = "zd-wg-01"

# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def crc(key: str) -> int:
    return zlib.crc32(key.encode("utf-8")) & 0xFFFFFFFF


def rule_key(rule: Dict[str, Any]) -> Tuple[str, str]:
    return ((rule.get("sensor_no") or "").strip().lower(), (rule.get("channel") or "").strip())


def normal_band(rule: Dict[str, Any]) -> Tuple[float, float]:
    """返回位于三级阈值内的正常信号中心值和幅值。"""
    lo = rule.get("level3_lower")
    hi = rule.get("level3_upper")
    lo = float(lo) if lo is not None else 0.0
    hi = float(hi) if hi is not None else 1.0
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
    """返回单条规则在一个窗口内的 (秒偏移, 数值) 列表。"""
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
        else:
            v = normal_value(rule, i)
        pts.append((off, v))
    return pts


def load_rules() -> List[Dict[str, Any]]:
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT t.*, s.frequency AS sensor_frequency
            FROM public.t_alert_threshold AS t
            LEFT JOIN public.t_sensor AS s
              ON LOWER(s.sensor_no) = LOWER(t.sensor_no)
            ORDER BY t.sensor_no, t.channel
            """
        )
        return [dict(r) for r in cur.fetchall()]


def rule_interval_ms(rule: Dict[str, Any]) -> int:
    frequency_hz = AlertAnalyticsEngine._parse_frequency_hz(
        rule.get("sensor_frequency")
    )
    return max(1, int(round(1000.0 / frequency_hz)))


def nominal_rate(rules: List[Dict[str, Any]]) -> float:
    total = 0.0
    for r in rules:
        total += 1000.0 / rule_interval_ms(r)
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
        hz = 1000.0 / rule_interval_ms(rule)
        n = max(1, int(round(hz * WINDOW_SECONDS * scale)))
        for off, val in window_points(rule, n, spec):
            rows.append((sensor, ch, float(val), win_start + timedelta(seconds=off)))
    return rows


def insert_rows(rows: List[Tuple[Any, ...]], target_rate: float, pace: bool = True) -> float:
    """按 ``target_rate`` 行/秒写入数据，并返回实际写入速率。"""
    if not rows:
        return 0.0
    conn = psycopg2.connect(DSN)
    started = time.perf_counter()
    sent = 0
    try:
        cur = conn.cursor()
        for start in range(0, len(rows), INSERT_BATCH):
            batch = rows[start:start + INSERT_BATCH]
            payload = io.StringIO()
            for sensor_no, channel, value, ts in batch:
                # t_monitor 的标识符和数值不包含 CSV 分隔符。
                payload.write(
                    f"{sensor_no},{channel},{value},{ts.strftime('%Y-%m-%d %H:%M:%S.%f')}\n"
                )
            payload.seek(0)
            cur.copy_expert(
                "COPY public.t_monitor (sensor_no, channel, value, time) "
                "FROM STDIN WITH (FORMAT csv)",
                payload,
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
    """返回预警标识到最新预警记录（按 seq）的映射。"""
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM public.t_alert_record
            ORDER BY seq ASC
            """
        )
        out: Dict[str, Dict[str, Any]] = {}
        for r in cur.fetchall():
            key = (
                f"{r['sensor_no'].lower()}:{r.get('channel') or ''}:"
                f"{r['alert_type']}"
            )
            out[key] = dict(r)
        return out


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
# 执行阶段
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

    rows_per_window = 0
    for win_idx in range(args.windows):
        n = sum(
            max(1, int(round((1000.0 / rule_interval_ms(r))
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
            (STREAM_START, TEST_END),
        )
        print(f"[precheck] leftover test rows     : {cur.fetchone()['c']}")
    return True


def phase_seed(args: argparse.Namespace) -> Tuple[bool, Optional[bytes]]:
    rules = load_rules()
    rate = nominal_rate(rules)
    scale = args.target_rate / rate if rate else 1.0

    conn = psycopg2.connect(DSN)
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM public.t_monitor WHERE time >= %s AND time < %s",
            (STREAM_START, TEST_END),
        )
        deleted_monitor = cur.rowcount
        cur.execute(
            "DELETE FROM public.t_stat_record WHERE stat_time >= %s AND stat_time < %s",
            (STREAM_START, TEST_END),
        )
        deleted_stats = cur.rowcount
        conn.commit()
        if deleted_monitor or deleted_stats:
            print(
                f"[seed] removed stale test data      : "
                f"monitor={deleted_monitor}, stats={deleted_stats}"
            )
        snapshot = snapshot_alert_record(conn)
        print(f"[seed] t_alert_record snapshot    : {len(snapshot)} bytes")
    finally:
        conn.close()

    pace = not args.no_pace

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
    engine.initialize()
    print(f"[detect] rules in memory          : {len(engine.channel_states)}")
    for win_idx in range(args.windows):
        ws = STREAM_START + timedelta(seconds=win_idx * WINDOW_SECONDS)
        we = ws + timedelta(seconds=WINDOW_SECONDS)
        alert_res = engine.scan_realtime_alerts(ws, we)
        statistics_res = engine.record_statistics_window(ws, we)
        print(
            f"[detect] window {win_idx} @ {ws:%H:%M:%S}: "
            f"alert_raw={alert_res['rawPointsProcessed']} "
            f"stats_raw={statistics_res['rawPointsProcessed']} "
            f"stats={statistics_res['upsertedStatsCount']} "
            f"alerts={alert_res['triggeredAlertsCount']} "
            f"elapsed={alert_res['elapsedMs'] + statistics_res['elapsedMs']}ms"
        )
        with get_db_cursor() as cur:
            cur.execute(
                """
                SELECT sensor_no, channel, alert_type, alert_level, alert_status
                FROM public.t_alert_record
                WHERE create_time >= %s AND create_time < %s
                ORDER BY sensor_no, channel, alert_type
                """,
                (ws, we),
            )
            rows = cur.fetchall()
            if len(rows) > 40:
                print(f"          ({len(rows)} alerts in window, showing 40)")
            for r in rows[:40]:
                identity = (
                    f"{r['sensor_no'].lower()}:{r.get('channel') or ''}:"
                    f"{r['alert_type']}"
                )
                print(
                    f"          - {identity:<45} type={r['alert_type']:<14} "
                    f"L{r['alert_level']} status={r['alert_status']}"
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
        expected_list.append(
            (
                f"{r['instrument_id'].lower()}:__DAQ__:采集仪离线",
                0,
                1,
            )
        )

    for key, _status, level in expected_list:
        row = alerts.get(key)
        if row is None:
            c.check(False, f"alert {key} exists", "not found")
            continue
        c.check(
            row["end_time"] is None,
            f"alert {key} is open",
            f"end_time={row['end_time']}",
        )
        c.check(int(row["alert_level"]) == level, f"alert {key} level == {level}", f"got {row['alert_level']}")

    row = alerts.get("zd-wg-01:x:传感器离线")
    if row:
        c.check(row["end_time"] is None, "zd-wg-01/x 传感器离线 remains active")
    row = alerts.get("zd-wg-01:y:传感器离线")
    if row:
        c.check(row["end_time"] is None, "zd-wg-01/y 传感器离线 remains active")
    daq_key = next(
        (
            k
            for k, _, _ in expected_list
            if k.endswith(":__DAQ__:采集仪离线")
        ),
        None,
    )
    row = alerts.get(daq_key) if daq_key else None
    if row:
        c.check(row["end_time"] is None, "采集仪离线 remains active")

    # 校验一分钟统计结果。
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM public.t_stat_record
            WHERE period_type = '1m'
              AND stat_time >= %s AND stat_time < %s
            """,
            (STREAM_START, STREAM_START + timedelta(seconds=args.windows * 60)),
        )
        got = int(cur.fetchone()["c"])
        skipped = 0
        for _, spec in ANOMALIES.items():
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
            SELECT sample_count
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
            c.check(int(r["sample_count"]) == 3, "yb-sx01-a/stress sample_count == 3", f"got {r['sample_count']}")

    # 检查模拟时间窗口内是否产生预期外的预警。
    expected_keys = {k for k, _, _ in expected_list}
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT sensor_no, channel, alert_type
            FROM public.t_alert_record
            WHERE create_time >= %s AND create_time < %s
            """,
            (STREAM_START, STREAM_START + timedelta(seconds=args.windows * 60)),
        )
        seen = {
            f"{r['sensor_no'].lower()}:{r.get('channel') or ''}:"
            f"{r['alert_type']}"
            for r in cur.fetchall()
        }
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
        cur.execute("DELETE FROM public.t_monitor WHERE time >= %s AND time < %s", (STREAM_START, TEST_END))
        print(f"[clean] deleted {cur.rowcount} t_monitor rows")
        cur.execute(
            "DELETE FROM public.t_stat_record WHERE stat_time >= %s AND stat_time < %s",
            (STREAM_START, TEST_END),
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
                "DELETE FROM public.t_alert_record WHERE create_time >= %s AND create_time < %s",
                (STREAM_START, TEST_END),
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
