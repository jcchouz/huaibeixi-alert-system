from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.domain.alert_types import ALERT_TYPE_THRESHOLD, normalize_alert_type
from app.domain.engine import AlertAnalyticsEngine
from app.infrastructure.sse import SSEHub
from app.repositories.alerts import AlertRepository
from app.schemas.alerts import AlertAckRequest, AlertCreateRequest, BatchAckRequest
from app.services.common import pick


class AlertService:
    def __init__(
        self,
        repository: AlertRepository,
        engine: AlertAnalyticsEngine,
        sse: SSEHub,
    ):
        self.repository = repository
        self.engine = engine
        self.sse = sse

    def poll(self, after_seq: int, limit: int) -> Dict[str, Any]:
        started = time.perf_counter()
        result = self.repository.poll(after_seq, limit)
        items = [self.engine.enrich_alert_row(row) for row in result["rows"]]
        return {
            "items": items,
            "maxSeq": result["max_seq"],
            "max_seq": result["max_seq"],
            "latestSeq": result["latest_seq"],
            "latest_seq": result["latest_seq"],
            "hasMore": result["has_more"],
            "has_more": result["has_more"],
            "serverTime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
        }

    def list_alerts(
        self,
        *,
        alert_level: Optional[int],
        alert_status: Optional[int],
        alert_type: Optional[str],
        sensor_no: Optional[str],
        keyword: Optional[str],
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        rows, total = self.repository.list(
            alert_level=alert_level,
            alert_status=alert_status,
            alert_type=alert_type,
            sensor_no=sensor_no,
            keyword=keyword,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self.engine.enrich_alert_row(row) for row in rows],
            "total": total,
            "page": page,
            "pageSize": page_size,
            "totalPages": self.repository.total_pages(total, page_size),
        }

    def acknowledge(
        self,
        alert_id: str,
        body: AlertAckRequest,
        principal_username: str,
    ) -> Dict[str, Any]:
        target_status = int(pick(body.alertStatus, body.alert_status, 1))
        if target_status != 1:
            raise HTTPException(status_code=400, detail="alertStatus must be 1")

        updated = self.repository.acknowledge(
            alert_id,
            target_status=target_status,
            actor=principal_username,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

        enriched = self.engine.enrich_alert_row(updated)
        self.sse.publish_threadsafe({"type": "ALERT_UPSERT", "alert": enriched})
        return {"success": True, "alert": enriched}

    def acknowledge_all(
        self,
        body: Optional[BatchAckRequest],
        principal_username: str,
    ) -> Dict[str, Any]:
        request = body or BatchAckRequest()
        alert_level = pick(request.alertLevel, request.alert_level)
        updated_ids = self.repository.acknowledge_all(
            actor=principal_username,
            alert_level=alert_level,
        )
        return {
            "success": True,
            "ackedCount": len(updated_ids),
            "ackedIds": updated_ids,
        }

    def create_alert(self, body: AlertCreateRequest) -> Dict[str, Any]:
        sensor_no = str(pick(body.sensorNo, body.sensor_no, "")).strip().lower()
        if not sensor_no:
            raise HTTPException(status_code=400, detail="sensorNo is required")

        channel = str(body.channel or "").strip()
        alert_type = normalize_alert_type(
            pick(body.alertType, body.alert_type, ALERT_TYPE_THRESHOLD)
        ) or ALERT_TYPE_THRESHOLD
        alert_level = int(pick(body.alertLevel, body.alert_level, 1))
        if alert_level not in (1, 2, 3):
            raise HTTPException(status_code=400, detail="alertLevel must be 1, 2 or 3")
        if alert_type != ALERT_TYPE_THRESHOLD:
            alert_level = 3

        trigger_val = float(pick(body.triggerVal, body.trigger_val, 0.0))
        key = self.engine.make_key(sensor_no, channel)
        rule = self.engine.channel_states.get(key) or {}
        created = self.repository.create(
            alert_id=uuid.uuid4().hex,
            sensor_no=sensor_no,
            channel=channel,
            threshold_id=rule.get("id"),
            alert_type=alert_type,
            alert_level=alert_level,
            trigger_val=trigger_val,
        )
        enriched = self.engine.enrich_alert_row(created)
        self.sse.publish_threadsafe({"type": "ALERT_UPSERT", "alert": enriched})
        return {"success": True, "alert": enriched}

    def delete_alert(self, alert_id: str) -> Dict[str, Any]:
        if not self.repository.delete(alert_id):
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
        return {"success": True, "alertId": alert_id}
