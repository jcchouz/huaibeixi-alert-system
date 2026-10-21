"""预警接口业务逻辑和响应信息补充。"""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import HTTPException

from app.domain.alert_types import (
    ALERT_TYPE_THRESHOLD,
    NON_THRESHOLD_ALERT_LEVEL,
    normalize_alert_type,
)
from app.domain.engine import AlertAnalyticsEngine
from app.repositories.alerts import AlertRepository
from app.schemas.alerts import AlertAckRequest, AlertCreateRequest, BatchAckRequest
from app.services.common import pick


class AlertService:
    """组织预警查询、处理操作和人工测试创建。"""

    def __init__(
        self,
        repository: AlertRepository,
        engine: AlertAnalyticsEngine,
    ):
        self.repository = repository
        self.engine = engine

    def poll(self, after_seq: int, limit: int) -> Dict[str, Any]:
        """返回轮询游标之后的增量预警。"""
        started = time.perf_counter()
        result = self.repository.poll(after_seq, limit)
        # 为数据库记录补充传感器、站点标签和展示字段。
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
        alert_category: Optional[int],
        sensor_no: Optional[str],
        keyword: Optional[str],
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        """返回经过筛选和分页的预警列表。"""
        rows, total, status_counts = self.repository.list(
            alert_level=alert_level,
            alert_status=alert_status,
            alert_type=alert_type,
            alert_category=alert_category,
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
            "statusCounts": status_counts,
        }

    def acknowledge(
        self,
        alert_id: str,
        body: AlertAckRequest,
    ) -> Dict[str, Any]:
        """将单条预警标记为已处理。"""
        target_status = int(pick(body.alertStatus, body.alert_status, 1))
        if target_status != 1:
            raise HTTPException(status_code=400, detail="alertStatus must be 1")

        handler = str(pick(body.handleBy, body.handle_by, "web") or "web").strip()
        handle_phone = str(
            pick(body.handlePhone, body.handle_phone, "") or ""
        ).strip() or None
        handle_opinion = pick(body.handleOpinion, body.handle_opinion)
        updated = self.repository.acknowledge(
            alert_id,
            target_status=target_status,
            handler=handler,
            handle_phone=handle_phone,
            handle_opinion=handle_opinion,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

        enriched = self.engine.enrich_alert_row(updated)
        return {"success": True, "alert": enriched}

    def acknowledge_all(
        self,
        body: Optional[BatchAckRequest],
    ) -> Dict[str, Any]:
        """将符合条件的未处理预警批量标记为已处理。"""
        request = body or BatchAckRequest()
        handler = str(
            pick(request.handleBy, request.handle_by, "web") or "web"
        ).strip()
        handle_phone = str(
            pick(request.handlePhone, request.handle_phone, "") or ""
        ).strip() or None
        alert_level = pick(request.alertLevel, request.alert_level)
        alert_category = pick(request.alertCategory, request.alert_category)
        updated_ids = self.repository.acknowledge_all(
            handler=handler,
            handle_phone=handle_phone,
            alert_level=alert_level,
            alert_category=alert_category,
        )
        return {
            "success": True,
            "ackedCount": len(updated_ids),
            "ackedIds": updated_ids,
        }

    def create_alert(self, body: AlertCreateRequest) -> Dict[str, Any]:
        """创建人工预警，主要用于本地开发和测试。"""
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
            alert_level = NON_THRESHOLD_ALERT_LEVEL

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
        return {"success": True, "alert": enriched}

    def delete_alert(self, alert_id: str) -> Dict[str, Any]:
        """删除人工维护的预警记录。"""
        if not self.repository.delete(alert_id):
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")
        return {"success": True, "alertId": alert_id}
