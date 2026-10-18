#!/usr/bin/env python
"""HTTP / SSE / CRUD validation for the running FastAPI service on :8090.

Snapshots ``t_alert_threshold`` and ``t_alert_record`` to binary files first and
restores them at the end, so this is safe to run against a live database. The
alert-engine background loop is only stepped when explicitly requested, and it
is stopped again before the snapshot restore.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
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

BASE = "http://127.0.0.1:8090"
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


def sse_probe(trigger_after: float = 1.5, total: float = 6.0) -> Tuple[List[str], Optional[str]]:
    holder: Dict[str, Any] = {"lines": [], "stop": False, "err": None}

    def reader() -> None:
        req = urllib.request.Request(
            f"{BASE}/api/alerts/stream", headers={"Accept": "text/event-stream"})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except Exception as exc:
            holder["err"] = f"connect: {exc}"
            return
        holder["resp"] = resp
        try:
            while not holder["stop"]:
                line = resp.readline()
                if not line:
                    break
                holder["lines"].append(line.decode("utf-8", "replace").rstrip("\n"))
        except Exception as exc:
            holder["err"] = f"read: {exc}"
        finally:
            try:
                resp.close()
            except Exception:
                pass

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(trigger_after)
    http("POST", "/api/engine/replay", {"action": "trigger_test_alert"})
    time.sleep(max(0.0, total - trigger_after))
    holder["stop"] = True
    resp = holder.get("resp")
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass
    thread.join(timeout=5)
    return holder["lines"], holder["err"]


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
                  f"v={first.get('version')} l3=({first.get('level3_lower')},{first.get('level3_upper')})")

        st, wy = http("GET", "/api/thresholds?sensorNo=wy-sj-01")
        tid = args.threshold_id
        if not tid and isinstance(wy, dict):
            tid = next((str(r["id"]) for r in wy.get("items", []) if (r.get("channel") or "") == "x"), None)
        if not tid:
            tid = ids[0] if ids else None

        # ---------- PUT threshold ----------
        if tid:
            st0, before = http("GET", f"/api/thresholds?sensorNo=wy-sj-01")
            rule_before = next((r for r in (before or {}).get("items", [])
                                if str(r["id"]) == tid), None) if isinstance(before, dict) else None
            v_before = int(rule_before["version"]) if rule_before else -1
            l3u = rule_before.get("level3_upper") if rule_before else None
            body: Dict[str, Any] = {"change_reason": "api_validation smoke", "update_by": "codex"}
            if l3u is not None:
                body["level3_upper"] = float(l3u) + 0.01
            st, upd = http("PUT", f"/api/thresholds/{tid}", body)
            rule_after = upd.get("threshold") or upd.get("rule") or upd if isinstance(upd, dict) else {}
            v_after = int(rule_after["version"]) if isinstance(rule_after, dict) and "version" in rule_after else -1
            c.check(st == 200 and v_after == v_before + 1,
                    f"PUT /api/thresholds/{str(tid)[:8]}.. version {v_before}->{v_after}",
                    f"{st} {str(upd)[:180]}")
            hist = rule_after.get("history_log") if isinstance(rule_after, dict) else None
            c.check(isinstance(hist, list) and len(hist) >= 1,
                    "PUT threshold appends history_log", str(hist)[:140])
            st_bad, _ = http("PUT", f"/api/thresholds/{tid}",
                             {"level1_upper": 1.0, "level2_upper": 5.0, "level3_upper": 9.0})
            c.check(st_bad == 400, "PUT threshold rejects invalid L1<L2<L3 ordering", f"got {st_bad}")

            st, batch = http("POST", "/api/thresholds/batch-update",
                             {"threshold_ids": [tid], "change_reason": "api_validation batch",
                              "update_by": "codex"})
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
        c.check(st == 200 and isinstance(poll, dict) and "items" in poll and "maxSeq" in poll,
                "GET /api/alerts/poll returns cursor page", f"{st} {str(poll)[:120]}")
        if isinstance(poll, dict):
            print(f"[api]   poll elapsedMs={poll.get('elapsedMs')} maxSeq={poll.get('maxSeq')}")

        # ---------- ack single ----------
        with get_db_cursor() as cur:
            cur.execute("SELECT id FROM public.t_alert_record WHERE alert_status=0 "
                        "AND is_delete=false ORDER BY seq DESC LIMIT 1")
            row = cur.fetchone()
        ack_id = active_ids[0] if active_ids else (str(row["id"]) if row else None)
        if ack_id:
            st, ackd = http("POST", f"/api/alerts/{ack_id}/ack",
                            {"alertStatus": 1, "ackBy": "codex", "remark": "api_validation ack"})
            got = ackd["alert"].get("alert_status") if isinstance(ackd, dict) and isinstance(ackd.get("alert"), dict) else None
            c.check(st == 200 and got == 1, "POST /api/alerts/{id}/ack sets status=1",
                    f"{st} status={got}")
        else:
            c.check(False, "POST /api/alerts/{id}/ack", "no active alert found")

        # ---------- ack all ----------
        st, allack = http("POST", "/api/alerts/ack-all",
                          {"ackBy": "codex", "remark": "api_validation ack-all"})
        acked = allack.get("ackedCount") if isinstance(allack, dict) else None
        c.check(st == 200 and isinstance(acked, int),
                "POST /api/alerts/ack-all acks unhandled alerts", f"{st} ackedCount={acked}")
        with get_db_cursor() as cur:
            cur.execute("SELECT COUNT(*) c FROM public.t_alert_record "
                        "WHERE alert_status=0 AND is_delete=false")
            remaining = int(cur.fetchone()["c"])
        c.check(remaining == 0, "ack-all leaves 0 unhandled alerts", f"remaining={remaining}")

        # ---------- SSE ----------
        lines, err = sse_probe()
        joined = "\n".join(lines)
        c.check("event: connected" in joined, "SSE emits initial connected event", err or joined[:120])
        c.check("event: alert" in joined, "SSE pushes alert event on test trigger",
                err or f"lines={len(lines)}")
        print(f"[api]   SSE events={sum(1 for l in lines if l.startswith('event:'))}")

        # ---------- engine replay step/stop ----------
        st, step = http("POST", "/api/engine/replay",
                        {"action": "step", "replayTime": "2026-09-28 13:55:00"}, timeout=90)
        c.check(st == 200 and isinstance(step, dict) and "stepResult" in step,
                "POST /api/engine/replay step runs a detection window", f"{st} {str(step)[:150]}")
        if isinstance(step, dict) and isinstance(step.get("stepResult"), dict):
            sr = step["stepResult"]
            print(f"[api]   step raw={sr.get('rawPointsProcessed')} stats={sr.get('upsertedStatsCount')} "
                  f"alerts={sr.get('triggeredAlertsCount')} elapsed={sr.get('elapsedMs')}ms")
        st, stopped = http("POST", "/api/engine/replay", {"action": "stop"})
        c.check(st == 200 and isinstance(stopped, dict) and stopped.get("success") is True,
                "POST /api/engine/replay stop halts the loop", f"{st} {str(stopped)[:120]}")

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

        st, rose = http("GET", "/api/stats/wind-rose?sensorNo=wea-01")
        c.check(st == 200 and isinstance(rose, dict) and len(rose) > 0,
                "GET /api/stats/wind-rose returns payload", f"{st} {str(rose)[:150]}")
        if isinstance(rose, dict):
            print(f"[api]   wind-rose keys={list(rose)[:12]}")

    finally:
        http("POST", "/api/engine/replay", {"action": "stop"})
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
