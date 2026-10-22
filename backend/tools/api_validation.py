#!/usr/bin/env python
"""验证运行在 8090 端口的 FastAPI 服务和 CRUD 接口。

工具会先将 ``t_alert_threshold`` 和 ``t_alert_record`` 快照为二进制文件，
验证结束后恢复，因此可以在活动数据库上运行。预警记录会在验证完成前恢复。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
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

from app.config import DSN, get_db_cursor

BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
SNAP_DIR = Path(__file__).resolve().parent / "_snap"


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


def snapshot_table(conn: Any, table: str, path: Path) -> int:
    buf = io.BytesIO()
    cur = conn.cursor()
    cur.copy_expert(f"COPY public.{table} TO STDOUT WITH BINARY", buf)
    payload = buf.getvalue()
    path.write_bytes(payload)
    return len(payload)


def restore_table(conn: Any, table: str, path: Path) -> int:
    cur = conn.cursor()
    cur.execute(f"DELETE FROM public.{table}")
    with path.open("rb") as fh:
        cur.copy_expert(f"COPY public.{table} FROM STDIN WITH BINARY", fh)
    if table == "t_alert_record":
        cur.execute(
            "SELECT setval('public.seq_alert_record', "
            "(SELECT COALESCE(MAX(seq), 0) FROM public.t_alert_record), true)"
        )
    conn.commit()
    cur.execute(f"SELECT COUNT(*) FROM public.{table}")
    return int(cur.fetchone()[0])


def run(args: argparse.Namespace) -> int:
    c = Checker()

    SNAP_DIR.mkdir(exist_ok=True)
    conn = psycopg2.connect(DSN)
    try:
        n_thr = snapshot_table(conn, "t_alert_threshold", SNAP_DIR / "threshold.bin")
        n_alert = snapshot_table(conn, "t_alert_record", SNAP_DIR / "alert.bin")
    finally:
        conn.close()
    print(f"[api] snapshot t_alert_threshold={n_thr}B t_alert_record={n_alert}B")

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
        if c.check(st == 200 and isinstance(thr, dict) and thr.get("items") and thr.get("total", 0) >= 185,
                   "GET /api/thresholds lists >=185 rules",
                   f"{st} total={thr.get('total') if isinstance(thr, dict) else thr}"):
            ids = [str(i["id"]) for i in thr["items"]]
            first = thr["items"][0]
            print(f"[api]   sample rule sensor={first.get('sensor_no')} channel={first.get('channel')} "
                  f"create_by={first.get('create_by')} "
                  f"l3=({first.get('level3_lower')},{first.get('level3_upper')})")

        st, wy = http("GET", "/api/thresholds?sensorNo=wy-sj-01")
        tid = args.threshold_id
        if not tid and isinstance(wy, dict):
            tid = next((str(r["id"]) for r in wy.get("items", []) if (r.get("channel") or "") == "x"), None)
        if not tid:
            tid = ids[0] if ids else None

        # ---------- PUT threshold ----------
        if tid:
            _, before = http("GET", "/api/thresholds?sensorNo=wy-sj-01")
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

        # ---------- statistics ----------
        st, sens = http("GET", "/api/stats/sensors")
        items = (sens.get("items") or sens.get("sensors") or []) if isinstance(sens, dict) else []
        c.check(st == 200 and len(items) >= 1, "GET /api/stats/sensors lists channels",
                f"{st} n={len(items)}")
        if items:
            print(f"[api]   stat sensors={len(items)} sample="
                  f"{json.dumps(items[0], ensure_ascii=False, default=str)[:180]}")

        st, ext = http("GET", "/api/stats/extremes?sensorNo=stn-12-b&periodType=1m&limit=50")
        has_series = isinstance(ext, dict) and (ext.get("items") or ext.get("points") or ext.get("series"))
        c.check(st == 200 and bool(has_series), "GET /api/stats/extremes returns 1m series",
                f"{st} {str(ext)[:150]}")

    finally:
        conn = psycopg2.connect(DSN)
        try:
            thr_rows = restore_table(conn, "t_alert_threshold", SNAP_DIR / "threshold.bin")
            alert_rows = restore_table(conn, "t_alert_record", SNAP_DIR / "alert.bin")
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
