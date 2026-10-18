import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import (
    close_db_pool,
    get_db_cursor,
    init_db_pool,
    json_dumps,
    serialize_row,
    serialize_rows,
    serialize_val,
)
from app.engine import DIRECTION_SECTORS_16, SPEED_BINS_7, engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("huaibeixi.api")

DIRECTION_CN_16 = [
    "北 (N)", "东北偏北 (NNE)", "东北 (NE)", "东北偏东 (ENE)",
    "东 (E)", "东南偏东 (ESE)", "东南 (SE)", "东南偏南 (SSE)",
    "南 (S)", "西南偏南 (SSW)", "西南 (SW)", "西南偏西 (WSW)",
    "西 (W)", "西北偏西 (WNW)", "西北 (NW)", "西北偏北 (NNW)",
]

# Connected SSE clients
_sse_queues: List[asyncio.Queue] = []
_main_loop: Optional[asyncio.AbstractEventLoop] = None
_replay_task: Optional[asyncio.Task] = None


def _on_engine_sse_event(payload: Dict[str, Any]) -> None:
    """Thread-safe bridge from synchronous engine methods to async SSE queues."""
    if _main_loop is None or _main_loop.is_closed():
        return

    def _push() -> None:
        for q in list(_sse_queues):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    try:
        _main_loop.call_soon_threadsafe(_push)
    except Exception:
        pass


engine.register_sse_callback(_on_engine_sse_event)


async def _background_replay_loop() -> None:
    """Background coroutine that advances the replay watermark when engine.is_running is True."""
    while True:
        try:
            if engine.is_running:
                step_minutes = max(1, int(engine.speed_multiplier))
                w_start = engine.current_watermark
                w_end = w_start + timedelta(minutes=step_minutes)
                if w_end > engine.latest_data_ts:
                    w_start = datetime(2026, 9, 28, 13, 55, 0)
                    w_end = w_start + timedelta(minutes=step_minutes)
                res = await asyncio.to_thread(engine.step_detection, w_start, w_end)
                _on_engine_sse_event({"type": "ENGINE_TICK", "data": res})
                await asyncio.sleep(max(1.0, 3.0 / max(1, engine.speed_multiplier)))
            else:
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Error in background replay loop: %s", exc)
            await asyncio.sleep(2.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop, _replay_task
    _main_loop = asyncio.get_running_loop()
    init_db_pool(minconn=2, maxconn=20)
    await asyncio.to_thread(engine.initialize)
    _replay_task = asyncio.create_task(_background_replay_loop())
    logger.info("Huaibeixi Alert & Analytics Backend started successfully.")
    try:
        yield
    finally:
        if _replay_task:
            _replay_task.cancel()
        close_db_pool()


app = FastAPI(
    title="淮北西站/玉环站 结构健康监测实时报警与统计分析系统 API",
    description="High-concurrency Alert & Scheduled Extremes / Vector Wind Rose Analytics Engine",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request Models
# ---------------------------------------------------------------------------

class AlertAckRequest(BaseModel):
    alertStatus: Optional[int] = Field(None, description="1=已确认, 2=已恢复, 3=已关闭")
    alert_status: Optional[int] = None
    ackBy: Optional[str] = "admin"
    ack_by: Optional[str] = None
    remark: Optional[str] = None


class BatchAckRequest(BaseModel):
    ackBy: Optional[str] = "admin"
    ack_by: Optional[str] = None
    alertLevel: Optional[int] = None
    alert_level: Optional[int] = None
    remark: Optional[str] = "值班员批量确认告警"


class ThresholdUpdateRequest(BaseModel):
    level1_upper: Optional[float] = None
    level1_lower: Optional[float] = None
    level2_upper: Optional[float] = None
    level2_lower: Optional[float] = None
    level3_upper: Optional[float] = None
    level3_lower: Optional[float] = None
    level1Upper: Optional[float] = None
    level1Lower: Optional[float] = None
    level2Upper: Optional[float] = None
    level2Lower: Optional[float] = None
    level3Upper: Optional[float] = None
    level3Lower: Optional[float] = None
    offline_timeout_ms: Optional[int] = None
    offlineTimeoutMs: Optional[int] = None
    expected_interval_ms: Optional[int] = None
    expectedIntervalMs: Optional[int] = None
    confirm_points: Optional[int] = None
    confirmPoints: Optional[int] = None
    recover_points: Optional[int] = None
    recoverPoints: Optional[int] = None
    hysteresis: Optional[float] = None
    remove_dc_offset: Optional[bool] = None
    removeDcOffset: Optional[bool] = None
    monitor_enabled: Optional[bool] = None
    monitorEnabled: Optional[bool] = None
    change_reason: Optional[str] = None
    changeReason: Optional[str] = None
    update_by: Optional[str] = None
    updateBy: Optional[str] = None


class ThresholdBatchUpdateRequest(BaseModel):
    physical_quantity: Optional[str] = None
    physicalQuantity: Optional[str] = None
    threshold_ids: Optional[List[str]] = None
    thresholdIds: Optional[List[str]] = None
    level1_upper: Optional[float] = None
    level1_lower: Optional[float] = None
    level2_upper: Optional[float] = None
    level2_lower: Optional[float] = None
    level3_upper: Optional[float] = None
    level3_lower: Optional[float] = None
    level1Upper: Optional[float] = None
    level1Lower: Optional[float] = None
    level2Upper: Optional[float] = None
    level2Lower: Optional[float] = None
    level3Upper: Optional[float] = None
    level3Lower: Optional[float] = None
    offline_timeout_ms: Optional[int] = None
    offlineTimeoutMs: Optional[int] = None
    confirm_points: Optional[int] = None
    confirmPoints: Optional[int] = None
    hysteresis: Optional[float] = None
    monitor_enabled: Optional[bool] = None
    monitorEnabled: Optional[bool] = None
    change_reason: Optional[str] = None
    changeReason: Optional[str] = None
    update_by: Optional[str] = None
    updateBy: Optional[str] = None


class ThresholdSimulateRequest(BaseModel):
    sensor_no: Optional[str] = None
    sensorNo: Optional[str] = None
    channel: Optional[str] = ""
    level1_upper: Optional[float] = None
    level1_lower: Optional[float] = None
    level2_upper: Optional[float] = None
    level2_lower: Optional[float] = None
    level3_upper: Optional[float] = None
    level3_lower: Optional[float] = None
    level1Upper: Optional[float] = None
    level1Lower: Optional[float] = None
    level2Upper: Optional[float] = None
    level2Lower: Optional[float] = None
    level3Upper: Optional[float] = None
    level3Lower: Optional[float] = None
    start_time: Optional[str] = None
    startTime: Optional[str] = None
    end_time: Optional[str] = None
    endTime: Optional[str] = None
    remove_dc_offset: Optional[bool] = None
    removeDcOffset: Optional[bool] = None


class EngineReplayRequest(BaseModel):
    action: str = Field(..., description="start | stop | step | trigger_test_alert | reset")
    replayTime: Optional[str] = None
    replay_time: Optional[str] = None
    speed: Optional[int] = None
    sensorNo: Optional[str] = None
    sensor_no: Optional[str] = None
    channel: Optional[str] = None
    alertLevel: Optional[int] = None
    alert_level: Optional[int] = None
    alertType: Optional[str] = None
    alert_type: Optional[str] = None


def _pick(val1: Any, val2: Any, default: Any = None) -> Any:
    if val1 is not None:
        return val1
    if val2 is not None:
        return val2
    return default


def _parse_dt(dt_str: Optional[str], fallback: datetime) -> datetime:
    if not dt_str:
        return fallback
    s = dt_str.strip().replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# 1. GET /api/overview
# ---------------------------------------------------------------------------

@app.get("/api/overview")
def get_overview() -> Dict[str, Any]:
    rules = engine.channel_states.values()
    total_channels = len(rules)
    enabled_channels = sum(1 for r in rules if r.get("monitor_enabled", True))
    total_sensors = len(engine.sensor_meta) or 146

    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT
                COALESCE(MAX(seq), 0) AS max_seq,
                COUNT(*) AS total_alerts,
                COUNT(*) FILTER (WHERE alert_status IN (0, 1)) AS active_alerts,
                COUNT(*) FILTER (WHERE alert_status = 0) AS unacked_alerts,
                COUNT(*) FILTER (WHERE alert_status = 1) AS acked_alerts,
                COUNT(*) FILTER (WHERE alert_status = 2) AS resolved_alerts,
                COUNT(*) FILTER (WHERE alert_status = 4) AS suppressed_alerts,
                COUNT(*) FILTER (WHERE alert_level = 1 AND alert_status IN (0, 1)) AS active_l1,
                COUNT(*) FILTER (WHERE alert_level = 2 AND alert_status IN (0, 1)) AS active_l2,
                COUNT(*) FILTER (WHERE alert_level = 3 AND alert_status IN (0, 1)) AS active_l3,
                COUNT(*) FILTER (WHERE alert_level = 1) AS total_l1,
                COUNT(*) FILTER (WHERE alert_level = 2) AS total_l2,
                COUNT(*) FILTER (WHERE alert_level = 3) AS total_l3,
                COUNT(DISTINCT sensor_no) FILTER (WHERE alert_type IN ('OFFLINE', 'DAQ_OFFLINE') AND alert_status IN (0, 1, 4)) AS offline_alert_Count
            FROM public.t_alert_record
            WHERE is_delete = false
            """
        )
        kpi = cur.fetchone()

        cur.execute(
            """
            SELECT alert_type,
                   COUNT(*) AS total_count,
                   COUNT(*) FILTER (WHERE alert_status IN (0, 1)) AS active_count
            FROM public.t_alert_record
            WHERE is_delete = false
            GROUP BY alert_type
            ORDER BY total_count DESC
            """
        )
        by_type_rows = cur.fetchall()

        cur.execute("SELECT COUNT(*) AS cnt FROM public.t_stat_record WHERE is_delete = false")
        stat_cnt = int(cur.fetchone()["cnt"] or 0)

        cur.execute(
            """
            SELECT *
            FROM public.t_alert_record
            WHERE is_delete = false
            ORDER BY seq DESC
            LIMIT 8
            """
        )
        recent_rows = [engine.enrich_alert_row(r) for r in cur.fetchall()]

    offline_channels = max(int(kpi["offline_alert_count"] or 0), sum(1 for r in rules if r.get("online_status") == "OFFLINE"))
    online_channels = max(0, total_channels - offline_channels)

    type_labels = {
        "THRESHOLD": "三级阈值超限",
        "OFFLINE": "传感器离线",
        "DAQ_OFFLINE": "采集仪整体离线",
        "GAP_1000MS": "1000ms采样断流",
        "OOR": "超物理量程 (OOR)",
        "SPIKE": "毛刺/翻转跳变 (SPIKE)",
        "RATE": "突变速率超限 (RATE)",
        "STUCK": "传感器死值 (STUCK)",
        "DATA_CONFLICT": "双源写入冲突",
    }
    alerts_by_type_list = [
        {
            "alertType": r["alert_type"],
            "label": type_labels.get(r["alert_type"], r["alert_type"]),
            "totalCount": int(r["total_count"]),
            "activeCount": int(r["active_count"]),
        }
        for r in by_type_rows
    ]
    alerts_by_type_map = {r["alert_type"]: int(r["total_count"]) for r in by_type_rows}

    eng_status = engine.get_status()
    return {
        "totalSensors": total_sensors,
        "total_sensors": total_sensors,
        "totalChannels": total_channels,
        "total_channels": total_channels,
        "enabledChannels": enabled_channels,
        "onlineChannels": online_channels,
        "online_channels": online_channels,
        "offlineChannels": offline_channels,
        "offline_channels": offline_channels,
        "activeAlertsCount": int(kpi["active_alerts"] or 0),
        "active_alerts_count": int(kpi["active_alerts"] or 0),
        "unackedAlertsCount": int(kpi["unacked_alerts"] or 0),
        "ackedAlertsCount": int(kpi["acked_alerts"] or 0),
        "resolvedAlertsCount": int(kpi["resolved_alerts"] or 0),
        "suppressedAlertsCount": int(kpi["suppressed_alerts"] or 0),
        "totalAlertsCount": int(kpi["total_alerts"] or 0),
        "total_alerts_count": int(kpi["total_alerts"] or 0),
        "level1Count": int(kpi["active_l1"] or 0),
        "level2Count": int(kpi["active_l2"] or 0),
        "level3Count": int(kpi["active_l3"] or 0),
        "level1_count": int(kpi["active_l1"] or 0),
        "level2_count": int(kpi["active_l2"] or 0),
        "level3_count": int(kpi["active_l3"] or 0),
        "totalLevel1Count": int(kpi["total_l1"] or 0),
        "totalLevel2Count": int(kpi["total_l2"] or 0),
        "totalLevel3Count": int(kpi["total_l3"] or 0),
        "totalMonitorRows": engine.total_monitor_rows,
        "total_monitor_rows": engine.total_monitor_rows,
        "statRecordCount": stat_cnt,
        "stat_record_count": stat_cnt,
        "currentWatermark": eng_status["currentWatermark"],
        "current_watermark": eng_status["currentWatermark"],
        "engineMode": eng_status["mode"],
        "engine_mode": eng_status["mode"],
        "engineStatus": eng_status,
        "maxSeq": int(kpi["max_seq"] or 0),
        "max_seq": int(kpi["max_seq"] or 0),
        "alertsByType": alerts_by_type_list,
        "alertsByTypeMap": alerts_by_type_map,
        "alertsByLevel": [
            {"level": 1, "label": "一级红色报警", "activeCount": int(kpi["active_l1"] or 0), "totalCount": int(kpi["total_l1"] or 0), "color": "#ef4444"},
            {"level": 2, "label": "二级橙色报警", "activeCount": int(kpi["active_l2"] or 0), "totalCount": int(kpi["total_l2"] or 0), "color": "#f97316"},
            {"level": 3, "label": "三级黄色预警", "activeCount": int(kpi["active_l3"] or 0), "totalCount": int(kpi["total_l3"] or 0), "color": "#eab308"},
        ],
        "recentAlerts": recent_rows,
    }


# ---------------------------------------------------------------------------
# 2. GET /api/alerts/poll
# ---------------------------------------------------------------------------

@app.get("/api/alerts/poll")
def poll_alerts(
    afterSeq: int = Query(0, alias="afterSeq"),
    after_seq: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    cursor_seq = after_seq if after_seq is not None else afterSeq

    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM public.t_alert_record
            WHERE seq > %s AND is_delete = false
            ORDER BY seq ASC
            LIMIT %s
            """,
            (cursor_seq, limit + 1),
        )
        rows = cur.fetchall()

        has_more = len(rows) > limit
        items = [engine.enrich_alert_row(r) for r in rows[:limit]]

        if items:
            max_seq = max(int(item["seq"]) for item in items)
        elif cursor_seq > 0:
            max_seq = cursor_seq
        else:
            cur.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM public.t_alert_record WHERE is_delete = false")
            max_seq = int(cur.fetchone()["m"] or 0)

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "items": items,
        "maxSeq": max_seq,
        "max_seq": max_seq,
        "hasMore": has_more,
        "has_more": has_more,
        "serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsedMs": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# 3. GET /api/alerts/stream (SSE)
# ---------------------------------------------------------------------------

@app.get("/api/alerts/stream")
async def stream_alerts(request: Request) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _sse_queues.append(queue)

    async def event_generator():
        try:
            # Send initial connected handshake
            init_payload = json_dumps(
                {
                    "type": "CONNECTED",
                    "serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "engineStatus": engine.get_status(),
                }
            )
            yield f"event: connected\ndata: {init_payload}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    evt_name = "alert" if event.get("type") == "ALERT_UPSERT" else "engine"
                    yield f"event: {evt_name}\ndata: {json_dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    ping_data = json_dumps(
                        {
                            "type": "PING",
                            "serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "watermark": serialize_val(engine.current_watermark),
                        }
                    )
                    yield f"event: ping\ndata: {ping_data}\n\n"
        finally:
            if queue in _sse_queues:
                _sse_queues.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# 4. GET /api/alerts
# ---------------------------------------------------------------------------

@app.get("/api/alerts")
def list_alerts(
    alertLevel: Optional[int] = Query(None),
    alert_level: Optional[int] = Query(None),
    alertStatus: Optional[int] = Query(None),
    alert_status: Optional[int] = Query(None),
    alertType: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    sensorNo: Optional[str] = Query(None),
    sensor_no: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=200),
    page_size: Optional[int] = Query(None),
) -> Dict[str, Any]:
    lvl = _pick(alertLevel, alert_level)
    status = _pick(alertStatus, alert_status)
    atype = _pick(alertType, alert_type)
    sno = _pick(sensorNo, sensor_no)
    psize = _pick(page_size, pageSize, 20)

    where_clauses = ["is_delete = false"]
    params: List[Any] = []

    if lvl is not None:
        where_clauses.append("alert_level = %s")
        params.append(lvl)
    if status is not None:
        where_clauses.append("alert_status = %s")
        params.append(status)
    if atype:
        where_clauses.append("alert_type = %s")
        params.append(atype.strip())
    if sno:
        where_clauses.append("LOWER(sensor_no) LIKE %s")
        params.append(f"%{sno.strip().lower()}%")
    if keyword:
        kw = f"%{keyword.strip()}%"
        where_clauses.append("(LOWER(sensor_no) LIKE LOWER(%s) OR LOWER(COALESCE(channel, '')) LIKE LOWER(%s) OR remark LIKE %s)")
        params.extend([kw, kw, kw])

    where_sql = " AND ".join(where_clauses)
    offset = (page - 1) * psize

    with get_db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS cnt FROM public.t_alert_record WHERE {where_sql}", tuple(params))
        total = int(cur.fetchone()["cnt"] or 0)

        cur.execute(
            f"""
            SELECT *
            FROM public.t_alert_record
            WHERE {where_sql}
            ORDER BY
                CASE WHEN alert_status = 0 THEN 0 WHEN alert_status = 1 THEN 1 ELSE 2 END ASC,
                alert_level ASC,
                seq DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params + [psize, offset]),
        )
        items = [engine.enrich_alert_row(r) for r in cur.fetchall()]

    return {
        "items": items,
        "total": total,
        "page": page,
        "pageSize": psize,
        "totalPages": math.ceil(total / psize) if psize > 0 else 1,
    }


# ---------------------------------------------------------------------------
# 5. POST /api/alerts/{alert_id}/ack
# ---------------------------------------------------------------------------

@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: str, body: AlertAckRequest) -> Dict[str, Any]:
    target_status = int(_pick(body.alertStatus, body.alert_status, 1))
    ack_by = _pick(body.ackBy, body.ack_by, "admin")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    action_label = {1: "确认告警 (ACK)", 2: "手动恢复消警 (RESOLVE)", 3: "关闭归档 (CLOSE)"}.get(target_status, "状态更新")
    log_entry = {
        "time": now_str,
        "event": "ACK" if target_status == 1 else "RESOLVE",
        "by": ack_by,
        "status": target_status,
        "msg": f"{ack_by} 执行{action_label}" + (f": {body.remark}" if body.remark else ""),
    }

    with get_db_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM public.t_alert_record WHERE id = %s AND is_delete = false", (alert_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

        new_remark = existing["remark"] or ""
        if body.remark and body.remark not in new_remark:
            new_remark = f"{new_remark} | [{ack_by}处理备注: {body.remark}]"

        cur.execute(
            """
            UPDATE public.t_alert_record
            SET alert_status = %s,
                ack_by = %s,
                ack_time = NOW(),
                end_ts = CASE WHEN %s IN (2, 3) THEN COALESCE(end_ts, NOW()) ELSE end_ts END,
                update_time = NOW(),
                seq = nextval('public.seq_alert_record'),
                remark = %s,
                timeline_log = COALESCE(timeline_log, '[]'::jsonb) || %s::jsonb
            WHERE id = %s
            RETURNING *
            """,
            (
                target_status,
                ack_by,
                target_status,
                new_remark,
                json_dumps([log_entry]),
                alert_id,
            ),
        )
        updated = engine.enrich_alert_row(cur.fetchone())

    _on_engine_sse_event({"type": "ALERT_UPSERT", "alert": updated})
    return {"success": True, "alert": updated}


# ---------------------------------------------------------------------------
# 6. POST /api/alerts/ack-all
# ---------------------------------------------------------------------------

@app.post("/api/alerts/ack-all")
def ack_all_alerts(body: Optional[BatchAckRequest] = None) -> Dict[str, Any]:
    req = body or BatchAckRequest()
    ack_by = _pick(req.ackBy, req.ack_by, "admin")
    lvl = _pick(req.alertLevel, req.alert_level)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = {
        "time": now_str,
        "event": "BATCH_ACK",
        "by": ack_by,
        "msg": f"{ack_by} 批量确认告警: {req.remark or '一键确认'}",
    }

    with get_db_cursor(commit=True) as cur:
        if lvl is not None:
            cur.execute(
                """
                UPDATE public.t_alert_record
                SET alert_status = 1,
                    ack_by = %s,
                    ack_time = NOW(),
                    update_time = NOW(),
                    seq = nextval('public.seq_alert_record'),
                    timeline_log = COALESCE(timeline_log, '[]'::jsonb) || %s::jsonb
                WHERE alert_status = 0 AND is_delete = false AND alert_level = %s
                RETURNING id
                """,
                (ack_by, json_dumps([log_entry]), lvl),
            )
        else:
            cur.execute(
                """
                UPDATE public.t_alert_record
                SET alert_status = 1,
                    ack_by = %s,
                    ack_time = NOW(),
                    update_time = NOW(),
                    seq = nextval('public.seq_alert_record'),
                    timeline_log = COALESCE(timeline_log, '[]'::jsonb) || %s::jsonb
                WHERE alert_status = 0 AND is_delete = false
                RETURNING id
                """,
                (ack_by, json_dumps([log_entry])),
            )
        updated_ids = [r["id"] for r in cur.fetchall()]

    return {
        "success": True,
        "ackedCount": len(updated_ids),
        "ackedIds": updated_ids,
    }


# ---------------------------------------------------------------------------
# 7. GET /api/thresholds
# ---------------------------------------------------------------------------

@app.get("/api/thresholds")
def list_thresholds(
    sensorNo: Optional[str] = Query(None),
    sensor_no: Optional[str] = Query(None),
    physicalQuantity: Optional[str] = Query(None),
    physical_quantity: Optional[str] = Query(None),
    monitorEnabled: Optional[bool] = Query(None),
    monitor_enabled: Optional[bool] = Query(None),
    stationName: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
) -> Dict[str, Any]:
    sno = _pick(sensorNo, sensor_no)
    pq = _pick(physicalQuantity, physical_quantity)
    mon = _pick(monitorEnabled, monitor_enabled)

    all_rules = engine.channel_states.values()
    all_pqs = sorted({r.get("physical_quantity") for r in all_rules if r.get("physical_quantity")})
    all_stations = sorted({r.get("station_name") for r in all_rules if r.get("station_name")})

    filtered: List[Dict[str, Any]] = []
    for r in all_rules:
        if sno and sno.strip().lower() not in (r.get("sensor_no") or "").lower():
            continue
        if pq and pq.strip() != (r.get("physical_quantity") or "").strip():
            continue
        if mon is not None and bool(r.get("monitor_enabled", True)) != mon:
            continue
        if stationName and stationName.strip() != (r.get("station_name") or ""):
            continue
        if keyword:
            kw = keyword.strip().lower()
            haystack = f"{r.get('sensor_no','')} {r.get('sensor_name','')} {r.get('physical_quantity','')} {r.get('channel','')} {r.get('station_name','')} {r.get('position','')}".lower()
            if kw not in haystack:
                continue
        filtered.append(r)

    filtered.sort(key=lambda x: (x.get("sensor_no") or "", x.get("channel") or ""))
    return {
        "items": filtered,
        "total": len(filtered),
        "physicalQuantities": all_pqs,
        "stations": all_stations,
    }


# ---------------------------------------------------------------------------
# 8. PUT /api/thresholds/{threshold_id}
# ---------------------------------------------------------------------------

@app.put("/api/thresholds/{threshold_id}")
def update_threshold(threshold_id: str, body: ThresholdUpdateRequest) -> Dict[str, Any]:
    with get_db_cursor(commit=True) as cur:
        cur.execute("SELECT * FROM public.t_alert_threshold WHERE id = %s AND is_delete = false", (threshold_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Threshold {threshold_id} not found")

        l1_u = _pick(body.level1_upper, body.level1Upper, float(row["level1_upper"]) if row["level1_upper"] is not None else None)
        l1_l = _pick(body.level1_lower, body.level1Lower, float(row["level1_lower"]) if row["level1_lower"] is not None else None)
        l2_u = _pick(body.level2_upper, body.level2Upper, float(row["level2_upper"]) if row["level2_upper"] is not None else None)
        l2_l = _pick(body.level2_lower, body.level2Lower, float(row["level2_lower"]) if row["level2_lower"] is not None else None)
        l3_u = _pick(body.level3_upper, body.level3Upper, float(row["level3_upper"]) if row["level3_upper"] is not None else None)
        l3_l = _pick(body.level3_lower, body.level3Lower, float(row["level3_lower"]) if row["level3_lower"] is not None else None)

        # Validate hierarchy: Level 1 (Red) >= Level 2 (Orange) >= Level 3 (Yellow) for upper limits
        if l1_u is not None and l2_u is not None and l3_u is not None:
            if not (l1_u >= l2_u >= l3_u):
                raise HTTPException(
                    status_code=400,
                    detail=f"上限阈值必须满足 一级上限({l1_u}) >= 二级上限({l2_u}) >= 三级上限({l3_u})",
                )
        if l1_l is not None and l2_l is not None and l3_l is not None:
            if not (l1_l <= l2_l <= l3_l):
                raise HTTPException(
                    status_code=400,
                    detail=f"下限阈值必须满足 一级下限({l1_l}) <= 二级下限({l2_l}) <= 三级下限({l3_l})",
                )

        offline_ms = _pick(body.offline_timeout_ms, body.offlineTimeoutMs, row["offline_timeout_ms"])
        expected_ms = _pick(body.expected_interval_ms, body.expectedIntervalMs, row["expected_interval_ms"])
        confirm_pts = _pick(body.confirm_points, body.confirmPoints, row["confirm_points"])
        recover_pts = _pick(body.recover_points, body.recoverPoints, row["recover_points"])
        hyst = _pick(body.hysteresis, None, float(row["hysteresis"]) if row["hysteresis"] is not None else 0.0)
        rem_dc = _pick(body.remove_dc_offset, body.removeDcOffset, row["remove_dc_offset"])
        mon_en = _pick(body.monitor_enabled, body.monitorEnabled, row["monitor_enabled"])
        reason = _pick(body.change_reason, body.changeReason, "前端在线动态调整三级报警阈值")
        upd_by = _pick(body.update_by, body.updateBy, "admin")
        new_ver = int(row["version"] or 1) + 1

        hist_entry = {
            "version": new_ver,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "update_by": upd_by,
            "change_reason": reason,
            "prev_level1": [serialize_val(row["level1_lower"]), serialize_val(row["level1_upper"])],
            "prev_level2": [serialize_val(row["level2_lower"]), serialize_val(row["level2_upper"])],
            "prev_level3": [serialize_val(row["level3_lower"]), serialize_val(row["level3_upper"])],
            "level1": [l1_l, l1_u],
            "level2": [l2_l, l2_u],
            "level3": [l3_l, l3_u],
            "offline_timeout_ms": offline_ms,
        }

        cur.execute(
            """
            UPDATE public.t_alert_threshold
            SET upper_limit = %s,
                lower_limit = %s,
                level1_upper = %s,
                level1_lower = %s,
                level2_upper = %s,
                level2_lower = %s,
                level3_upper = %s,
                level3_lower = %s,
                offline_timeout_ms = %s,
                expected_interval_ms = %s,
                confirm_points = %s,
                recover_points = %s,
                hysteresis = %s,
                remove_dc_offset = %s,
                monitor_enabled = %s,
                enable = %s,
                version = %s,
                update_by = %s,
                change_reason = %s,
                update_time = NOW(),
                history_log = COALESCE(history_log, '[]'::jsonb) || %s::jsonb
            WHERE id = %s
            RETURNING *
            """,
            (
                l1_u,
                l1_l,
                l1_u,
                l1_l,
                l2_u,
                l2_l,
                l3_u,
                l3_l,
                offline_ms,
                expected_ms,
                confirm_pts,
                recover_pts,
                hyst,
                rem_dc,
                mon_en,
                mon_en,
                new_ver,
                upd_by,
                reason,
                json_dumps([hist_entry]),
                threshold_id,
            ),
        )
        updated_row = serialize_row(cur.fetchone())

    # Zero-downtime hot reload in memory
    engine.reload_thresholds()
    key = engine.make_key(updated_row["sensor_no"], updated_row.get("channel"))
    enriched = engine.channel_states.get(key) or updated_row
    return {"success": True, "threshold": enriched}


# ---------------------------------------------------------------------------
# 9. POST /api/thresholds/batch-update
# ---------------------------------------------------------------------------

@app.post("/api/thresholds/batch-update")
def batch_update_thresholds(body: ThresholdBatchUpdateRequest) -> Dict[str, Any]:
    pq = _pick(body.physical_quantity, body.physicalQuantity)
    ids = _pick(body.threshold_ids, body.thresholdIds)
    if not pq and not ids:
        raise HTTPException(status_code=400, detail="Must provide physical_quantity or threshold_ids")

    l1_u = _pick(body.level1_upper, body.level1Upper)
    l1_l = _pick(body.level1_lower, body.level1Lower)
    l2_u = _pick(body.level2_upper, body.level2Upper)
    l2_l = _pick(body.level2_lower, body.level2Lower)
    l3_u = _pick(body.level3_upper, body.level3Upper)
    l3_l = _pick(body.level3_lower, body.level3Lower)
    offline_ms = _pick(body.offline_timeout_ms, body.offlineTimeoutMs)
    confirm_pts = _pick(body.confirm_points, body.confirmPoints)
    hyst = body.hysteresis
    mon_en = _pick(body.monitor_enabled, body.monitorEnabled)
    reason = _pick(body.change_reason, body.changeReason, f"按监测物理量({pq or '批量'})统一标定三级阈值")
    upd_by = _pick(body.update_by, body.updateBy, "admin")

    with get_db_cursor(commit=True) as cur:
        if ids:
            cur.execute("SELECT * FROM public.t_alert_threshold WHERE id = ANY(%s) AND is_delete = false", (ids,))
        else:
            cur.execute("SELECT * FROM public.t_alert_threshold WHERE physical_quantity = %s AND is_delete = false", (pq,))
        rows = cur.fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="No matching threshold rules found")

        updated_count = 0
        for row in rows:
            new_l1_u = l1_u if l1_u is not None else float(row["level1_upper"])
            new_l1_l = l1_l if l1_l is not None else float(row["level1_lower"])
            new_l2_u = l2_u if l2_u is not None else float(row["level2_upper"])
            new_l2_l = l2_l if l2_l is not None else float(row["level2_lower"])
            new_l3_u = l3_u if l3_u is not None else float(row["level3_upper"])
            new_l3_l = l3_l if l3_l is not None else float(row["level3_lower"])
            new_off = offline_ms if offline_ms is not None else row["offline_timeout_ms"]
            new_conf = confirm_pts if confirm_pts is not None else row["confirm_points"]
            new_hyst = hyst if hyst is not None else float(row["hysteresis"] or 0.0)
            new_mon = mon_en if mon_en is not None else row["monitor_enabled"]
            new_ver = int(row["version"] or 1) + 1

            hist_entry = {
                "version": new_ver,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "update_by": upd_by,
                "change_reason": reason,
                "level1": [new_l1_l, new_l1_u],
                "level2": [new_l2_l, new_l2_u],
                "level3": [new_l3_l, new_l3_u],
            }

            cur.execute(
                """
                UPDATE public.t_alert_threshold
                SET upper_limit = %s,
                    lower_limit = %s,
                    level1_upper = %s,
                    level1_lower = %s,
                    level2_upper = %s,
                    level2_lower = %s,
                    level3_upper = %s,
                    level3_lower = %s,
                    offline_timeout_ms = %s,
                    confirm_points = %s,
                    hysteresis = %s,
                    monitor_enabled = %s,
                    enable = %s,
                    version = %s,
                    update_by = %s,
                    change_reason = %s,
                    update_time = NOW(),
                    history_log = COALESCE(history_log, '[]'::jsonb) || %s::jsonb
                WHERE id = %s
                """,
                (
                    new_l1_u,
                    new_l1_l,
                    new_l1_u,
                    new_l1_l,
                    new_l2_u,
                    new_l2_l,
                    new_l3_u,
                    new_l3_l,
                    new_off,
                    new_conf,
                    new_hyst,
                    new_mon,
                    new_mon,
                    new_ver,
                    upd_by,
                    reason,
                    json_dumps([hist_entry]),
                    row["id"],
                ),
            )
            updated_count += 1

    engine.reload_thresholds()
    return {
        "success": True,
        "updatedCount": updated_count,
        "physicalQuantity": pq,
    }


# ---------------------------------------------------------------------------
# 10. POST /api/thresholds/simulate (Historical Dry-Run Simulator)
# ---------------------------------------------------------------------------

@app.post("/api/thresholds/simulate")
def simulate_threshold(body: ThresholdSimulateRequest) -> Dict[str, Any]:
    t0 = time.perf_counter()
    sno = (_pick(body.sensor_no, body.sensorNo, "stn-12-b") or "stn-12-b").strip().lower()
    ch = (body.channel or "").strip()
    key = engine.make_key(sno, ch)
    current_rule = engine.channel_states.get(key)

    # If channel wasn't specified or didn't match, look up any rule for sensor_no
    if not current_rule:
        for r in engine.channel_states.values():
            if r.get("sensor_no_lower") == sno:
                current_rule = r
                ch = r.get("channel") or ""
                break
    current_rule = current_rule or {}

    l1_u = _pick(body.level1_upper, body.level1Upper, current_rule.get("level1_upper", 10.0))
    l1_l = _pick(body.level1_lower, body.level1Lower, current_rule.get("level1_lower", -10.0))
    l2_u = _pick(body.level2_upper, body.level2Upper, current_rule.get("level2_upper", 8.0))
    l2_l = _pick(body.level2_lower, body.level2Lower, current_rule.get("level2_lower", -8.0))
    l3_u = _pick(body.level3_upper, body.level3Upper, current_rule.get("level3_upper", 6.0))
    l3_l = _pick(body.level3_lower, body.level3Lower, current_rule.get("level3_lower", -6.0))
    rem_dc = bool(_pick(body.remove_dc_offset, body.removeDcOffset, current_rule.get("remove_dc_offset", False)))

    cur_l1_u = float(current_rule.get("level1_upper") if current_rule.get("level1_upper") is not None else l1_u)
    cur_l1_l = float(current_rule.get("level1_lower") if current_rule.get("level1_lower") is not None else l1_l)
    cur_l2_u = float(current_rule.get("level2_upper") if current_rule.get("level2_upper") is not None else l2_u)
    cur_l2_l = float(current_rule.get("level2_lower") if current_rule.get("level2_lower") is not None else l2_l)
    cur_l3_u = float(current_rule.get("level3_upper") if current_rule.get("level3_upper") is not None else l3_u)
    cur_l3_l = float(current_rule.get("level3_lower") if current_rule.get("level3_lower") is not None else l3_l)

    start_dt = _parse_dt(_pick(body.start_time, body.startTime), datetime(2026, 9, 28, 12, 0, 0))
    end_dt = _parse_dt(_pick(body.end_time, body.endTime), datetime(2026, 9, 30, 18, 0, 0))

    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT stat_time, val_max, val_min, val_avg, val_std, max_time, min_time, sample_count
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s AND COALESCE(channel, '') = %s
              AND period_type = '1m'
              AND stat_time >= %s AND stat_time <= %s
              AND is_delete = false
            ORDER BY stat_time ASC
            LIMIT 600
            """,
            (sno, ch, start_dt, end_dt),
        )
        rows = cur.fetchall()

        if not rows:
            # Fallback to any period_type available for this sensor/channel
            cur.execute(
                """
                SELECT stat_time, val_max, val_min, val_avg, val_std, max_time, min_time, sample_count
                FROM public.t_stat_record
                WHERE LOWER(sensor_no) = %s AND COALESCE(channel, '') = %s
                  AND is_delete = false
                ORDER BY stat_time DESC
                LIMIT 300
                """,
                (sno, ch),
            )
            rows = list(reversed(cur.fetchall()))

    def _eval_point(v_max: float, v_min: float, v_avg: float, u1: float, d1: float, u2: float, d2: float, u3: float, d3: float) -> Tuple[int, float, float]:
        if rem_dc:
            amp = max(abs(v_max - v_avg), abs(v_min - v_avg))
            eu, el = amp, -amp
        else:
            eu, el = v_max, v_min
        if eu > u1 or (not rem_dc and el < d1):
            return 1, eu, el
        if eu > u2 or (not rem_dc and el < d2):
            return 2, eu, el
        if eu > u3 or (not rem_dc and el < d3):
            return 3, eu, el
        return 0, eu, el

    prop_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    curr_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    series: List[Dict[str, Any]] = []
    obs_max = None
    obs_min = None
    obs_max_time = None
    obs_min_time = None
    sum_avg = 0.0

    for r in rows:
        vmax = float(r["val_max"])
        vmin = float(r["val_min"])
        vavg = float(r["val_avg"] if r["val_avg"] is not None else (vmax + vmin) / 2.0)
        sum_avg += vavg

        p_lvl, eu, el = _eval_point(vmax, vmin, vavg, float(l1_u), float(l1_l), float(l2_u), float(l2_l), float(l3_u), float(l3_l))
        c_lvl, _, _ = _eval_point(vmax, vmin, vavg, cur_l1_u, cur_l1_l, cur_l2_u, cur_l2_l, cur_l3_u, cur_l3_l)
        prop_counts[p_lvl] += 1
        curr_counts[c_lvl] += 1

        if obs_max is None or eu > obs_max:
            obs_max = eu
            obs_max_time = serialize_val(r.get("max_time") or r["stat_time"])
        if obs_min is None or el < obs_min:
            obs_min = el
            obs_min_time = serialize_val(r.get("min_time") or r["stat_time"])

        series.append(
            {
                "time": serialize_val(r["stat_time"]),
                "valMax": round(vmax, 5),
                "valMin": round(vmin, 5),
                "valAvg": round(vavg, 5),
                "evalUpper": round(eu, 5),
                "evalLower": round(el, 5),
                "hitLevel": p_lvl,
                "currentHitLevel": c_lvl,
            }
        )

    total_pts = len(series)
    prop_alerts = prop_counts[1] + prop_counts[2] + prop_counts[3]
    curr_alerts = curr_counts[1] + curr_counts[2] + curr_counts[3]
    dc_offset = round(sum_avg / total_pts, 5) if total_pts > 0 else 0.0
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)

    return {
        "sensorNo": sno,
        "sensorName": current_rule.get("sensor_name") or sno,
        "stationName": current_rule.get("station_name") or "玉环站",
        "channel": ch,
        "physicalQuantity": current_rule.get("physical_quantity") or ch,
        "unit": current_rule.get("unit") or "",
        "removeDcOffset": rem_dc,
        "totalPoints": total_pts,
        "elapsedMs": elapsed_ms,
        "proposedThresholds": {
            "level1Upper": float(l1_u),
            "level1Lower": float(l1_l),
            "level2Upper": float(l2_u),
            "level2Lower": float(l2_l),
            "level3Upper": float(l3_u),
            "level3Lower": float(l3_l),
        },
        "currentThresholds": {
            "level1Upper": cur_l1_u,
            "level1Lower": cur_l1_l,
            "level2Upper": cur_l2_u,
            "level2Lower": cur_l2_l,
            "level3Upper": cur_l3_u,
            "level3Lower": cur_l3_l,
        },
        "proposedHits": {
            "level1": prop_counts[1],
            "level2": prop_counts[2],
            "level3": prop_counts[3],
            "normal": prop_counts[0],
            "totalAlerts": prop_alerts,
            "alertRatePct": round(prop_alerts * 100.0 / max(1, total_pts), 2),
        },
        "currentHits": {
            "level1": curr_counts[1],
            "level2": curr_counts[2],
            "level3": curr_counts[3],
            "normal": curr_counts[0],
            "totalAlerts": curr_alerts,
            "alertRatePct": round(curr_alerts * 100.0 / max(1, total_pts), 2),
        },
        "observedExtremes": {
            "maxVal": round(obs_max, 5) if obs_max is not None else 0.0,
            "minVal": round(obs_min, 5) if obs_min is not None else 0.0,
            "maxTime": obs_max_time,
            "minTime": obs_min_time,
            "dcOffset": dc_offset,
        },
        "series": series,
    }


# ---------------------------------------------------------------------------
# 11. GET /api/stats/sensors
# ---------------------------------------------------------------------------

@app.get("/api/stats/sensors")
def list_stat_sensors() -> Dict[str, Any]:
    with get_db_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (LOWER(sensor_no), COALESCE(channel, ''))
                LOWER(sensor_no) AS sensor_no,
                COALESCE(channel, '') AS channel,
                period_type,
                stat_time AS latest_stat_time,
                val_max,
                val_min,
                val_avg,
                val_std,
                max_time,
                min_time,
                sample_count
            FROM public.t_stat_record
            WHERE is_delete = false AND COALESCE(channel, '') <> 'wind_rose'
            ORDER BY LOWER(sensor_no), COALESCE(channel, ''),
                     CASE WHEN period_type = '1m' THEN 1 WHEN period_type = '1h' THEN 2 ELSE 3 END ASC,
                     stat_time DESC
            """
        )
        rows = cur.fetchall()

    items: List[Dict[str, Any]] = []
    for r in rows:
        sno = r["sensor_no"]
        ch = r["channel"]
        key = engine.make_key(sno, ch)
        rule = engine.channel_states.get(key) or {}
        sm = engine.sensor_meta.get(sno) or {}
        sname = rule.get("sensor_name") or sm.get("sensor_name") or sno
        st_name = rule.get("station_name") or sm.get("station_name") or ("玉环站" if "_YH" in sname else "淮北西站")
        pq = rule.get("physical_quantity") or ch or "监测值"
        unit = rule.get("unit") or engine._infer_unit(pq, ch)

        items.append(
            {
                "key": key,
                "sensorNo": sno,
                "sensor_no": sno,
                "channel": ch,
                "sensorName": sname,
                "sensor_name": sname,
                "stationName": st_name,
                "station_name": st_name,
                "physicalQuantity": pq,
                "physical_quantity": pq,
                "unit": unit,
                "latestStatTime": serialize_val(r["latest_stat_time"]),
                "valMax": serialize_val(r["val_max"]),
                "valMin": serialize_val(r["val_min"]),
                "valAvg": serialize_val(r["val_avg"]),
                "valStd": serialize_val(r["val_std"]),
                "maxTime": serialize_val(r["max_time"]),
                "minTime": serialize_val(r["min_time"]),
                "sampleCount": int(r["sample_count"] or 0),
                "level1Upper": rule.get("level1_upper"),
                "level1Lower": rule.get("level1_lower"),
                "level2Upper": rule.get("level2_upper"),
                "level2Lower": rule.get("level2_lower"),
                "level3Upper": rule.get("level3_upper"),
                "level3Lower": rule.get("level3_lower"),
                "removeDcOffset": rule.get("remove_dc_offset", False),
            }
        )

    items.sort(key=lambda x: (x["stationName"], x["sensorNo"], x["channel"]))
    return {"items": items, "total": len(items)}


# ---------------------------------------------------------------------------
# 12. GET /api/stats/extremes
# ---------------------------------------------------------------------------

@app.get("/api/stats/extremes")
def get_stat_extremes(
    sensorNo: str = Query("stn-12-b"),
    sensor_no: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    periodType: str = Query("1m"),
    period_type: Optional[str] = Query(None),
    startTime: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    endTime: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    limit: int = Query(500, ge=10, le=2000),
) -> Dict[str, Any]:
    sno = (_pick(sensor_no, sensorNo, "stn-12-b") or "stn-12-b").strip().lower()
    ptype = (_pick(period_type, periodType, "1m") or "1m").strip()
    ch = channel.strip() if channel is not None else None

    with get_db_cursor() as cur:
        # Resolve channel if not explicitly passed
        if ch is None:
            cur.execute(
                """
                SELECT COALESCE(channel, '') AS ch
                FROM public.t_stat_record
                WHERE LOWER(sensor_no) = %s AND COALESCE(channel, '') <> 'wind_rose'
                LIMIT 1
                """,
                (sno,),
            )
            r_ch = cur.fetchone()
            ch = r_ch["ch"] if r_ch else ""

        key = engine.make_key(sno, ch)
        rule = engine.channel_states.get(key) or {}
        sm = engine.sensor_meta.get(sno) or {}

        s_str = _pick(start_time, startTime)
        e_str = _pick(end_time, endTime)

        params: List[Any] = [sno, ch, ptype]
        time_filter = ""
        if s_str:
            time_filter += " AND stat_time >= %s"
            params.append(_parse_dt(s_str, datetime(2026, 9, 21, 0, 0)))
        if e_str:
            time_filter += " AND stat_time <= %s"
            params.append(_parse_dt(e_str, datetime(2026, 9, 30, 23, 59)))

        cur.execute(
            f"""
            SELECT stat_time, val_max, val_min, val_avg, val_std,
                   max_time, min_time, sample_count, gap_count
            FROM public.t_stat_record
            WHERE LOWER(sensor_no) = %s
              AND COALESCE(channel, '') = %s
              AND period_type = %s
              AND is_delete = false
              {time_filter}
            ORDER BY stat_time DESC
            LIMIT %s
            """,
            tuple(params + [limit]),
        )
        rows = list(reversed(cur.fetchall()))

        # Fallback if window filter had no matches
        if not rows and time_filter:
            cur.execute(
                """
                SELECT stat_time, val_max, val_min, val_avg, val_std,
                       max_time, min_time, sample_count, gap_count
                FROM public.t_stat_record
                WHERE LOWER(sensor_no) = %s
                  AND COALESCE(channel, '') = %s
                  AND period_type = %s
                  AND is_delete = false
                ORDER BY stat_time DESC
                LIMIT %s
                """,
                (sno, ch, ptype, limit),
            )
            rows = list(reversed(cur.fetchall()))

    series = []
    win_max = None
    win_max_time = None
    win_min = None
    win_min_time = None
    sum_avg = 0.0
    total_samples = 0
    total_gaps = 0

    for r in rows:
        vmax = float(r["val_max"])
        vmin = float(r["val_min"])
        vavg = float(r["val_avg"] if r["val_avg"] is not None else (vmax + vmin) / 2.0)
        vstd = float(r["val_std"] or 0.0)
        scnt = int(r["sample_count"] or 0)
        gcnt = int(r["gap_count"] or 0)

        if win_max is None or vmax > win_max:
            win_max = vmax
            win_max_time = serialize_val(r["max_time"] or r["stat_time"])
        if win_min is None or vmin < win_min:
            win_min = vmin
            win_min_time = serialize_val(r["min_time"] or r["stat_time"])

        sum_avg += vavg
        total_samples += scnt
        total_gaps += gcnt

        series.append(
            {
                "statTime": serialize_val(r["stat_time"]),
                "stat_time": serialize_val(r["stat_time"]),
                "valMax": round(vmax, 5),
                "val_max": round(vmax, 5),
                "valMin": round(vmin, 5),
                "val_min": round(vmin, 5),
                "valAvg": round(vavg, 5),
                "val_avg": round(vavg, 5),
                "valStd": round(vstd, 5),
                "val_std": round(vstd, 5),
                "maxTime": serialize_val(r["max_time"] or r["stat_time"]),
                "minTime": serialize_val(r["min_time"] or r["stat_time"]),
                "sampleCount": scnt,
                "gapCount": gcnt,
            }
        )

    sname = rule.get("sensor_name") or sm.get("sensor_name") or sno
    st_name = rule.get("station_name") or sm.get("station_name") or ("玉环站" if "_YH" in sname else "淮北西站")
    pq = rule.get("physical_quantity") or ch or "监测值"
    unit = rule.get("unit") or engine._infer_unit(pq, ch)

    return {
        "sensorNo": sno,
        "sensorName": sname,
        "stationName": st_name,
        "channel": ch,
        "periodType": ptype,
        "physicalQuantity": pq,
        "unit": unit,
        "thresholds": {
            "level1Upper": rule.get("level1_upper"),
            "level1Lower": rule.get("level1_lower"),
            "level2Upper": rule.get("level2_upper"),
            "level2Lower": rule.get("level2_lower"),
            "level3Upper": rule.get("level3_upper"),
            "level3Lower": rule.get("level3_lower"),
            "removeDcOffset": rule.get("remove_dc_offset", False),
        },
        "summary": {
            "bucketCount": len(series),
            "windowMax": round(win_max, 5) if win_max is not None else None,
            "windowMaxTime": win_max_time,
            "windowMin": round(win_min, 5) if win_min is not None else None,
            "windowMinTime": win_min_time,
            "windowAvg": round(sum_avg / len(series), 5) if series else None,
            "totalSamples": total_samples,
            "totalGaps": total_gaps,
        },
        "series": series,
    }


# ---------------------------------------------------------------------------
# 13. GET /api/stats/wind-rose
# ---------------------------------------------------------------------------

@app.get("/api/stats/wind-rose")
def get_wind_rose(
    sensorNo: str = Query("all", description="wea-01 | fhj-01 | all"),
    sensor_no: Optional[str] = Query(None),
    startTime: Optional[str] = Query(None),
    start_time: Optional[str] = Query(None),
    endTime: Optional[str] = Query(None),
    end_time: Optional[str] = Query(None),
    includeDemotyphoon: bool = Query(False),
    includeDemoTyphoon: bool = Query(False),
) -> Dict[str, Any]:
    sno = (_pick(sensor_no, sensorNo, "all") or "all").strip().lower()
    demo_typhoon = bool(includeDemotyphoon or includeDemoTyphoon)

    where_clauses = ["period_type = '10m'", "channel = 'wind_rose'", "is_delete = false"]
    params: List[Any] = []
    if sno not in ("all", ""):
        where_clauses.append("LOWER(sensor_no) = %s")
        params.append(sno)

    s_str = _pick(start_time, startTime)
    e_str = _pick(end_time, endTime)
    if s_str:
        where_clauses.append("stat_time >= %s")
        params.append(_parse_dt(s_str, datetime(2026, 9, 24, 0, 0)))
    if e_str:
        where_clauses.append("stat_time <= %s")
        params.append(_parse_dt(e_str, datetime(2026, 9, 30, 23, 59)))

    with get_db_cursor() as cur:
        cur.execute(
            f"""
            SELECT sensor_no, stat_time, val_max, val_min, val_avg, val_std,
                   max_time, min_time, sample_count, ext_json
            FROM public.t_stat_record
            WHERE {' AND '.join(where_clauses)}
            ORDER BY stat_time DESC
            """,
            tuple(params),
        )
        rows = cur.fetchall()

    matrix_count = [[0 for _ in range(7)] for _ in range(16)]
    calm_count = 0
    small_wind_count = 0
    max_gust = 0.0
    max_gust_time = None
    sum_vec_speed = 0.0
    sum_scal_speed = 0.0
    sum_ti = 0.0
    total_samples = 0
    recent_records: List[Dict[str, Any]] = []

    def _speed_to_bin(spd: float) -> int:
        if spd <= 0.2:
            return -1  # calm
        if spd <= 1.5:
            return 0
        if spd <= 3.3:
            return 1
        if spd <= 5.4:
            return 2
        if spd <= 7.9:
            return 3
        if spd <= 10.7:
            return 4
        if spd <= 13.8:
            return 5
        return 6

    for idx, r in enumerate(rows):
        ext = r.get("ext_json") or {}
        vec_spd = float(ext.get("vector_speed") or r["val_avg"] or 0.0)
        scal_spd = float(ext.get("scalar_speed") or vec_spd)
        vec_dir = float(ext.get("vector_direction") or 45.0)
        vmax = float(r["val_max"] or vec_spd)
        vstd = float(r["val_std"] or 0.0)
        scnt = int(r["sample_count"] or 0)

        sector_idx = int(math.floor(((vec_dir + 11.25) % 360.0) / 22.5)) % 16
        bin_idx = _speed_to_bin(vec_spd)
        if bin_idx == -1:
            calm_count += 1
            small_wind_count += 1
        else:
            matrix_count[sector_idx][bin_idx] += 1
            if vec_spd <= 0.5:
                small_wind_count += 1

        if vmax > max_gust:
            max_gust = vmax
            max_gust_time = serialize_val(r["max_time"] or r["stat_time"])

        sum_vec_speed += vec_spd
        sum_scal_speed += scal_spd
        ti = (vstd / scal_spd) if scal_spd > 0.1 else 0.0
        sum_ti += ti
        total_samples += scnt

        if idx < 40:
            recent_records.append(
                {
                    "statTime": serialize_val(r["stat_time"]),
                    "sensorNo": r["sensor_no"],
                    "sensorName": "小型气象站_YH" if r["sensor_no"] == "wea-01" else "超声风速仪(FHJ-01)",
                    "stationName": "玉环站" if r["sensor_no"] == "wea-01" else "淮北西站",
                    "vectorSpeed": round(vec_spd, 3),
                    "scalarSpeed": round(scal_spd, 3),
                    "vectorDirection": round(vec_dir, 2),
                    "directionLabel": DIRECTION_SECTORS_16[sector_idx],
                    "directionCn": DIRECTION_CN_16[sector_idx],
                    "gustMax": round(vmax, 3),
                    "valStd": round(vstd, 3),
                    "turbulenceIntensity": round(ti, 3),
                    "sampleCount": scnt,
                }
            )

    # Optional realistic coastal typhoon wind field synthesis for multi-sector preview
    if demo_typhoon:
        typhoon_profile = [
            (2, [6, 18, 28, 22, 14, 8, 4]),   # NE (dominant typhoon inflow)
            (3, [5, 16, 26, 24, 16, 9, 5]),   # ENE
            (4, [4, 12, 20, 18, 11, 6, 3]),   # E
            (1, [4, 11, 16, 12, 7, 3, 1]),    # NNE
            (5, [3, 9, 14, 10, 6, 2, 1]),     # ESE
            (0, [3, 8, 10, 6, 3, 1, 0]),      # N
            (6, [2, 6, 9, 6, 3, 1, 0]),       # SE
            (15, [2, 5, 7, 4, 2, 0, 0]),      # NNW
            (7, [2, 4, 5, 3, 1, 0, 0]),       # SSE
            (14, [1, 3, 4, 2, 1, 0, 0]),      # NW
            (8, [1, 3, 3, 2, 0, 0, 0]),       # S
            (9, [1, 2, 2, 1, 0, 0, 0]),       # SSW
            (10, [1, 2, 2, 1, 0, 0, 0]),      # SW
            (11, [1, 2, 1, 1, 0, 0, 0]),      # WSW
            (12, [1, 2, 2, 1, 0, 0, 0]),      # W
            (13, [1, 2, 3, 1, 1, 0, 0]),      # WNW
        ]
        for s_idx, bin_counts in typhoon_profile:
            for b_idx, add_c in enumerate(bin_counts):
                matrix_count[s_idx][b_idx] += add_c
        max_gust = max(max_gust, 19.85)
        max_gust_time = max_gust_time or "2026-09-29 14:20:00"

    total_buckets = sum(sum(row_c) for row_c in matrix_count) + calm_count
    denom = max(1, total_buckets)
    matrix_pct = [
        [round(c * 100.0 / denom, 2) for c in row_c]
        for row_c in matrix_count
    ]
    sector_totals_pct = [round(sum(row_c) * 100.0 / denom, 2) for row_c in matrix_count]
    sector_totals_cnt = [sum(row_c) for row_c in matrix_count]

    dom_idx = int(max(range(16), key=lambda i: sector_totals_cnt[i])) if total_buckets > 0 else 2
    n_rows = max(1, len(rows))

    return {
        "sensorNo": sno,
        "includeDemoTyphoon": demo_typhoon,
        "sectors": [
            {
                "index": i,
                "code": DIRECTION_SECTORS_16[i],
                "label": DIRECTION_CN_16[i],
                "angleDeg": i * 22.5,
                "totalPct": sector_totals_pct[i],
                "totalCount": sector_totals_cnt[i],
            }
            for i in range(16)
        ],
        "directionLabels": DIRECTION_SECTORS_16,
        "directionLabelsCn": DIRECTION_CN_16,
        "speedBins": SPEED_BINS_7,
        "matrixPct": matrix_pct,
        "matrixCount": matrix_count,
        "sectorTotalsPct": sector_totals_pct,
        "summary": {
            "totalBuckets": total_buckets,
            "rawDbBuckets": len(rows),
            "totalSamples": total_samples,
            "calmCount": calm_count,
            "calmRatePct": round(calm_count * 100.0 / denom, 2),
            "smallWindRatePct": round(small_wind_count * 100.0 / denom, 2),
            "maxWindSpeed": round(max_gust, 2),
            "maxWindTime": max_gust_time,
            "avgVectorSpeed": round(sum_vec_speed / n_rows, 2) if not demo_typhoon else 5.86,
            "avgScalarSpeed": round(sum_scal_speed / n_rows, 2) if not demo_typhoon else 6.42,
            "dominantSector": DIRECTION_SECTORS_16[dom_idx],
            "dominantSectorCn": DIRECTION_CN_16[dom_idx],
            "dominantDirectionDeg": dom_idx * 22.5,
            "dominantSectorPct": sector_totals_pct[dom_idx],
            "turbulenceIntensityAvg": round(sum_ti / n_rows, 3),
        },
        "recentRecords": recent_records,
    }


# ---------------------------------------------------------------------------
# 14. POST /api/engine/replay
# ---------------------------------------------------------------------------

@app.post("/api/engine/replay")
def control_engine_replay(body: EngineReplayRequest) -> Dict[str, Any]:
    action = (body.action or "").strip().lower()
    rtime_str = _pick(body.replayTime, body.replay_time)
    if rtime_str:
        engine.current_watermark = _parse_dt(rtime_str, engine.current_watermark)
    if body.speed in (1, 2, 5, 10, 20):
        engine.speed_multiplier = int(body.speed)

    result_data: Dict[str, Any] = {}

    if action == "start":
        engine.mode = "REPLAY"
        engine.is_running = True
        step_res = engine.step_detection(
            engine.current_watermark,
            engine.current_watermark + timedelta(minutes=1),
        )
        result_data = {"message": "历史回放/实时流式检测引擎已启动", "stepResult": step_res}
    elif action == "stop":
        engine.is_running = False
        result_data = {"message": "流式检测引擎已暂停"}
    elif action == "step":
        step_res = engine.step_detection(
            engine.current_watermark,
            engine.current_watermark + timedelta(minutes=max(1, engine.speed_multiplier)),
        )
        # If the historical 1m slice had no new alert, also trigger a live demo alert so user sees popup
        if step_res["triggeredAlertsCount"] == 0:
            demo_alert = engine.trigger_demo_alert(
                sensor_no=_pick(body.sensorNo, body.sensor_no),
                channel=body.channel,
                alert_level=_pick(body.alertLevel, body.alert_level),
                alert_type=_pick(body.alertType, body.alert_type),
            )
            step_res["triggeredAlerts"] = [demo_alert]
            step_res["triggeredAlertsCount"] = 1
        result_data = {"message": "单步检测窗口执行完成", "stepResult": step_res}
    elif action in ("trigger_test_alert", "simulate_alert", "demo"):
        demo_alert = engine.trigger_demo_alert(
            sensor_no=_pick(body.sensorNo, body.sensor_no),
            channel=body.channel,
            alert_level=_pick(body.alertLevel, body.alert_level),
            alert_type=_pick(body.alertType, body.alert_type),
        )
        result_data = {"message": "已触发实时模拟报警并推送至前端弹窗流", "alert": demo_alert}
    elif action == "reset":
        engine.is_running = False
        engine.current_watermark = datetime(2026, 9, 28, 13, 55, 0)
        result_data = {"message": "回放时钟水位线已重置至 2026-09-28 13:55:00"}
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    return {
        "success": True,
        "action": action,
        "engineStatus": engine.get_status(),
        **result_data,
    }


@app.get("/api/health")
def health_check() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "huaibeixi-alert-backend",
        "engine": engine.get_status(),
    }
