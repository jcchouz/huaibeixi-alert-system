#!/usr/bin/env python
"""验证运行在 8090 端口的 FastAPI 服务和 CRUD 接口。

工具会先将 ``t_alert_threshold`` 和 ``t_alert_record`` 快照为二进制文件，
验证结束后恢复，因此可以在活动数据库上运行。预警记录会在验证完成前恢复。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
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
from app.infrastructure.database import get_db_cursor

DSN = get_settings().dsn

BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
SNAP_DIR = Path(__file__).resolve().parent / "_snap"


@dataclass(frozen=True)
class TableSnapshot:
    """记录表的本地快照路径、行数和文件大小。"""

    path: Path
    row_count: int
    size_bytes: int


class Checker:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks += 1
        if ok:
            print(f"[api] PASS  {label}")
            return True
        self.failures += 1
        print(f"[api] FAIL  {label}  {detail}")
        return False


def http(method: str, path: str, body: Optional[Dict[str, Any]] = None,
         timeout: float = 30.0) -> Tuple[int, Any]:
    url = f"{BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _maybe_json(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _maybe_json(exc.read())
    except Exception as exc:
        return 0, {"error": str(exc)}


def _maybe_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return raw.decode("utf-8", "replace")


def count_csv_rows(payload: str) -> int:
    """统计 PostgreSQL CSV 快照的数据行数，并正确处理字段内换行。"""
    reader = csv.reader(io.StringIO(payload))
    next(reader, None)
    return sum(1 for _ in reader)


def snapshot_table(conn: Any, table: str, path: Path) -> TableSnapshot:
    """使用查询式 CSV 快照完整读取 TimescaleDB 超表。"""
    buf = io.StringIO()
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS count FROM public.{table}")
        expected_rows = int(cur.fetchone()[0])
        # 对压缩超表直接 COPY 表可能只输出空头，查询式 COPY 才会展开全部数据。
        cur.copy_expert(
            f"COPY (SELECT * FROM public.{table}) TO STDOUT "
            "WITH (FORMAT csv, HEADER true)",
            buf,
        )
    payload = buf.getvalue()
    actual_rows = count_csv_rows(payload)
    if actual_rows != expected_rows:
        raise RuntimeError(
            f"{table} snapshot row mismatch: expected {expected_rows}, got {actual_rows}"
        )
    path.write_text(payload, encoding="utf-8")
    return TableSnapshot(
        path=path,
        row_count=actual_rows,
        size_bytes=len(payload.encode("utf-8")),
    )


def restore_table(conn: Any, table: str, snapshot: TableSnapshot) -> int:
    """在调用方事务中恢复表，并在提交前校验行数。"""
    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM public.{table}")
        with snapshot.path.open("r", encoding="utf-8", newline="") as fh:
            cur.copy_expert(
                f"COPY public.{table} FROM STDIN WITH (FORMAT csv, HEADER true)",
                fh,
            )
        cur.execute(f"SELECT COUNT(*) AS count FROM public.{table}")
        restored_rows = int(cur.fetchone()[0])
        if restored_rows != snapshot.row_count:
            raise RuntimeError(
                f"{table} restore row mismatch: expected {snapshot.row_count}, "
                f"got {restored_rows}"
            )
        if table == "t_alert_record":
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('public.t_alert_record', 'seq'), "
                "GREATEST(COALESCE(MAX(seq), 1), 1), COALESCE(MAX(seq), 0) > 0) "
                "FROM public.t_alert_record"
            )
    return restored_rows


def run(args: argparse.Namespace) -> int:
    c = Checker()

    SNAP_DIR.mkdir(exist_ok=True)
    conn = psycopg2.connect(DSN)
    try:
        threshold_snapshot = snapshot_table(
            conn,
            "t_alert_threshold",
            SNAP_DIR / "threshold.csv",
        )
        alert_snapshot = snapshot_table(
            conn,
            "t_alert_record",
            SNAP_DIR / "alert.csv",
        )
    finally:
        conn.close()
    print(
        "[api] snapshot "
        f"t_alert_threshold={threshold_snapshot.row_count} rows/"
        f"{threshold_snapshot.size_bytes}B, "
        f"t_alert_record={alert_snapshot.row_count} rows/"
        f"{alert_snapshot.size_bytes}B"
    )

    try:
        # ---------- health ----------
        st, h = http("GET", "/api/health")
        c.check(st == 200 and isinstance(h, dict) and h.get("status") == "ok",
                "GET /api/health", f"{st} {str(h)[:120]}")

        # ---------- overview ----------
        st, ov = http("GET", "/api/overview")
        if c.check(st == 200 and isinstance(ov, dict) and "totalChannels" in ov
                   and "alertsByType" in ov and "maxSeq" in ov,
                   "GET /api/overview returns KPI + alertsByType",
                   f"{st} keys={list(ov)[:10] if isinstance(ov, dict) else ov}"):
            print(f"[api]   totalChannels={ov.get('totalChannels')} totalSensors={ov.get('totalSensors')} "
                  f"activeAlerts={ov.get('activeAlertsCount')} maxSeq={ov.get('maxSeq')}")
            print(f"[api]   byLevel={json.dumps(ov.get('alertsByLevel'), ensure_ascii=False, default=str)[:200]}")
            print(f"[api]   byType={json.dumps(ov.get('alertsByTypeMap'), ensure_ascii=False, default=str)[:240]}")

        # ---------- thresholds list ----------
        st, thr = http("GET", "/api/thresholds?pageSize=5")
        ids: List[str] = []
        if c.check(st == 200 and isinstance(thr, dict) and thr.get("items") and thr.get("total", 0) >= 1,
                   "GET /api/thresholds lists loaded rules",
                   f"{st} total={thr.get('total') if isinstance(thr, dict) else thr}"):
            ids = [str(i["id"]) for i in thr["items"]]
            first = thr["items"][0]
            print(f"[api]   sample rule sensor={first.get('sensor_no')} channel={first.get('channel')} "
                  f"create_by={first.get('create_by')} "
                  f"l3=({first.get('level3_lower')},{first.get('level3_upper')})")

        target_rule = None
        if isinstance(thr, dict):
            if args.threshold_id:
                target_rule = next(
                    (
                        item
                        for item in thr.get("items", [])
                        if str(item.get("id")) == args.threshold_id
                    ),
                    None,
                )
            if target_rule is None and thr.get("items"):
                target_rule = thr["items"][0]
        tid = str(target_rule["id"]) if target_rule else (ids[0] if ids else None)
        target_sensor = (
            str(target_rule.get("sensor_no") or "")
            if target_rule
            else ""
        )

        # ---------- PUT threshold ----------
        if tid:
            sensor_query = urllib.parse.urlencode({"sensorNo": target_sensor})
            _, before = http("GET", f"/api/thresholds?{sensor_query}")
            rule_before = next((r for r in (before or {}).get("items", [])
                                if str(r["id"]) == tid), None) if isinstance(before, dict) else None
            l3u = rule_before.get("level3_upper") if rule_before else None
            body: Dict[str, Any] = {}
            if l3u is not None:
                body["level3_upper"] = float(l3u) + 0.01
            st, upd = http("PUT", f"/api/thresholds/{tid}", body)
            rule_after = upd.get("threshold") or upd.get("rule") or upd if isinstance(upd, dict) else {}
            got_l3u = float(rule_after.get("level3_upper")) if isinstance(rule_after, dict) else None
            c.check(st == 200 and got_l3u == body.get("level3_upper"),
                    f"PUT /api/thresholds/{str(tid)[:8]}.. updates threshold",
                    f"{st} {str(upd)[:180]}")
            st_bad, _ = http("PUT", f"/api/thresholds/{tid}",
                             {"level1_upper": 1.0, "level2_upper": 5.0, "level3_upper": 9.0})
            c.check(st_bad == 400, "PUT threshold rejects invalid L1<L2<L3 ordering", f"got {st_bad}")

            st, batch = http("POST", "/api/thresholds/batch-update",
    {"threshold_ids": [tid]})
            bc = None
            if isinstance(batch, dict):
                bc = batch.get("updatedCount", batch.get("updated_count"))
            c.check(st == 200 and (bc or 0) >= 1,
                    "POST /api/thresholds/batch-update updates >=1 rule", f"{st} {str(batch)[:160]}")

        # ---------- simulate dry-run ----------
        st, sim = http("POST", "/api/thresholds/simulate",
                       {"sensor_no": "wy-sj-01", "channel": "x",
                        "level1_upper": 42.0, "level1_lower": -45.0,
                        "level2_upper": 39.0, "level2_lower": -42.0,
                        "level3_upper": 35.0, "level3_lower": -39.0})
        c.check(st == 200 and isinstance(sim, dict) and len(sim) > 0,
                "POST /api/thresholds/simulate (dry-run) returns comparison",
                f"{st} keys={list(sim)[:10] if isinstance(sim, dict) else sim}")
        if isinstance(sim, dict):
            print(f"[api]   simulate keys={list(sim)}")

        # ---------- alerts list ----------
        st, al = http("GET", "/api/alerts?pageSize=5")
        active_ids: List[str] = []
        if c.check(st == 200 and isinstance(al, dict) and al.get("items") and al.get("total", 0) >= 1,
                   "GET /api/alerts paginates",
                   f"{st} total={al.get('total') if isinstance(al, dict) else al}"):
            active_ids = [str(i["id"]) for i in al["items"] if int(i.get("alert_status") or 0) == 0]
            print(f"[api]   alerts total={al.get('total')} page statuses="
                  f"{[i.get('alert_status') for i in al['items']]}")

        # ---------- poll ----------
        st, poll = http("GET", "/api/alerts/poll?afterSeq=0&limit=3")
        c.check(st == 200 and isinstance(poll, dict) and "items" in poll
                and "maxSeq" in poll and "latestSeq" in poll,
                "GET /api/alerts/poll returns cursor page", f"{st} {str(poll)[:120]}")
        if isinstance(poll, dict):
            c.check(int(poll.get("latestSeq") or 0) >= int(poll.get("maxSeq") or 0),
                    "GET /api/alerts/poll latestSeq covers the full table",
                    f"maxSeq={poll.get('maxSeq')} latestSeq={poll.get('latestSeq')}")
            print(f"[api]   poll elapsedMs={poll.get('elapsedMs')} maxSeq={poll.get('maxSeq')} "
                  f"latestSeq={poll.get('latestSeq')}")

        # ---------- ack single ----------
        with get_db_cursor() as cur:
            cur.execute("SELECT id FROM public.t_alert_record WHERE alert_status=0 "
                        "ORDER BY seq DESC LIMIT 1")
            row = cur.fetchone()
        ack_id = active_ids[0] if active_ids else (str(row["id"]) if row else None)
        if ack_id:
            st, ackd = http("POST", f"/api/alerts/{ack_id}/ack",
                            {"alertStatus": 1, "ackBy": "codex"})
            got = ackd["alert"].get("alert_status") if isinstance(ackd, dict) and isinstance(ackd.get("alert"), dict) else None
            c.check(st == 200 and got == 1, "POST /api/alerts/{id}/ack sets status=1",
                    f"{st} status={got}")
        else:
            c.check(False, "POST /api/alerts/{id}/ack", "no active alert found")

        # ---------- ack all ----------
        st, allack = http("POST", "/api/alerts/ack-all",
                          {"ackBy": "codex"})
        acked = allack.get("ackedCount") if isinstance(allack, dict) else None
        c.check(st == 200 and isinstance(acked, int),
                "POST /api/alerts/ack-all acks unhandled alerts", f"{st} ackedCount={acked}")
        with get_db_cursor() as cur:
            cur.execute("SELECT COUNT(*) c FROM public.t_alert_record "
                        "WHERE alert_status=0")
            remaining = int(cur.fetchone()["c"])
        c.check(remaining == 0, "ack-all leaves 0 unhandled alerts", f"remaining={remaining}")

        # ---------- 人工创建与删除 ----------
        manual_sensor = "sensor-01"
        manual_channel = ""
        if isinstance(thr, dict) and thr.get("items"):
            manual_sensor = str(thr["items"][0].get("sensor_no") or manual_sensor)
            manual_channel = str(thr["items"][0].get("channel") or "")
        st, created = http(
            "POST",
            "/api/alerts",
            {
                "sensorNo": manual_sensor,
                "channel": manual_channel,
                "alertType": "阈值超限",
                "alertLevel": 3,
                "triggerVal": 1.0,
            },
        )
        created_id = None
        if isinstance(created, dict) and isinstance(created.get("alert"), dict):
            created_id = str(created["alert"].get("id") or "")
        c.check(
            st == 200 and bool(created_id),
            "POST /api/alerts creates a manual alert",
            f"{st} {str(created)[:180]}",
        )
        if created_id:
            st, deleted = http("DELETE", f"/api/alerts/{created_id}")
            c.check(
                st == 200 and isinstance(deleted, dict) and deleted.get("success") is True,
                "DELETE /api/alerts/{id} deletes the manual alert",
                f"{st} {str(deleted)[:180]}",
            )

        # ---------- statistics ----------
        st, sens = http("GET", "/api/stats/sensors")
        items = (sens.get("items") or sens.get("sensors") or []) if isinstance(sens, dict) else []
        c.check(st == 200 and len(items) >= 1, "GET /api/stats/sensors lists channels",
                f"{st} n={len(items)}")
        if items:
            print(f"[api]   stat sensors={len(items)} sample="
                  f"{json.dumps(items[0], ensure_ascii=False, default=str)[:180]}")

        stat_sensor = "sensor-01"
        stat_channel = ""
        if items:
            stat_sensor = str(items[0].get("sensorNo") or items[0].get("sensor_no") or stat_sensor)
            stat_channel = str(items[0].get("channel") or "")
        stat_query = urllib.parse.urlencode(
            {
                "sensorNo": stat_sensor,
                "channel": stat_channel,
                "periodType": "1m",
                "limit": 50,
            }
        )
        st, ext = http("GET", f"/api/stats/extremes?{stat_query}")
        has_series = isinstance(ext, dict) and (ext.get("items") or ext.get("points") or ext.get("series"))
        c.check(st == 200 and bool(has_series), "GET /api/stats/extremes returns 1m series",
                f"{st} {str(ext)[:150]}")

    finally:
        conn = psycopg2.connect(DSN)
        try:
            thr_rows = restore_table(conn, "t_alert_threshold", threshold_snapshot)
            alert_rows = restore_table(conn, "t_alert_record", alert_snapshot)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        print(f"[api] restored t_alert_threshold={thr_rows} rows, t_alert_record={alert_rows} rows")

    print(f"[api] {c.checks - c.failures}/{c.checks} checks passed")
    return 0 if c.failures == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threshold-id", help="specific threshold uuid to modify")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
