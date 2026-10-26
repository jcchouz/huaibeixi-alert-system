from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.schemas.alerts import AlertAckRequest, AlertCreateRequest, BatchAckRequest
from app.services.alerts import AlertService


class FakeAlertRepository:
    def __init__(self) -> None:
        self.created_payload: dict = {}
        self.acknowledged_payload: dict = {}

    def list(self, **_: object) -> tuple[list[dict], int, dict[str, int]]:
        return (
            [
                {"id": "alert-1", "alert_status": 0},
                {"id": "alert-2", "alert_status": 1},
            ],
            1,
            {"total": 12, "unhandled": 7, "handled": 5},
        )

    @staticmethod
    def total_pages(total: int, page_size: int) -> int:
        return (total + page_size - 1) // page_size

    def acknowledge(
        self,
        alert_id: str,
        *,
        target_status: int,
        handler: str,
        handle_phone: str | None,
        handle_opinion: str | None,
    ) -> dict:
        self.acknowledged_payload = {
            "handler": handler,
            "handle_phone": handle_phone,
            "handle_opinion": handle_opinion,
        }
        return {
            "id": alert_id,
            "alert_status": target_status,
            "handle_by": handler,
            "handle_phone": handle_phone,
            "handle_opinion": handle_opinion,
        }

    def acknowledge_all(
        self,
        *,
        handler: str,
        handle_phone: str | None,
        alert_level: int | None,
        alert_category: int | None,
    ) -> list[str]:
        self.acknowledged_payload = {
            "handler": handler,
            "handle_phone": handle_phone,
            "alert_level": alert_level,
            "alert_category": alert_category,
        }
        return ["alert-1", "alert-2"]

    def create(self, **payload: object) -> dict:
        self.created_payload = payload
        return {
            "id": payload["alert_id"],
            "sensor_no": payload["sensor_no"],
            "channel": payload["channel"],
            "alert_type": payload["alert_type"],
            "alert_level": payload["alert_level"],
            "alert_status": 0,
        }


class FakeEngine:
    channel_states: dict = {}

    def make_key(self, sensor_no: str, channel: str) -> str:
        return f"{sensor_no}:{channel}"

    def build_rule_snapshot(self, rule: dict, alert_type: str) -> dict:
        return {"alert_type": alert_type, "rule_id": rule.get("id")}

    def enrich_alert_row(self, row: dict) -> dict:
        return row


def test_alert_status_two_is_not_supported() -> None:
    service = AlertService(
        FakeAlertRepository(),  # type: ignore[arg-type]
        FakeEngine(),  # type: ignore[arg-type]
    )

    with pytest.raises(HTTPException) as error:
        service.acknowledge(
            "alert-1",
            AlertAckRequest(alertStatus=2),
        )

    assert error.value.status_code == 400


def test_alert_acknowledgement_accepts_handler_and_opinion() -> None:
    repository = FakeAlertRepository()
    service = AlertService(
        repository,  # type: ignore[arg-type]
        FakeEngine(),  # type: ignore[arg-type]
    )

    service.acknowledge(
        "alert-1",
        AlertAckRequest(
            alertStatus=1,
            handleBy="operator-a",
            handlePhone="13800000000",
            handleOpinion="checked on site",
        ),
    )

    assert repository.acknowledged_payload == {
        "handler": "operator-a",
        "handle_phone": "13800000000",
        "handle_opinion": "checked on site",
    }


def test_batch_acknowledgement_filters_by_alert_category() -> None:
    repository = FakeAlertRepository()
    service = AlertService(
        repository,  # type: ignore[arg-type]
        FakeEngine(),  # type: ignore[arg-type]
    )

    result = service.acknowledge_all(
        BatchAckRequest(alertCategory=1),
    )

    assert repository.acknowledged_payload == {
        "handler": "web",
        "handle_phone": None,
        "alert_level": None,
        "alert_category": 1,
    }
    assert result["ackedCount"] == 2


def test_non_threshold_alert_is_forced_to_level_one() -> None:
    repository = FakeAlertRepository()
    service = AlertService(
        repository,  # type: ignore[arg-type]
        FakeEngine(),  # type: ignore[arg-type]
    )

    result = service.create_alert(
        AlertCreateRequest(
            sensorNo="sensor-1",
            channel="x",
            alertType="OFFLINE",
            alertLevel=1,
        )
    )

    assert result["alert"]["alert_level"] == 1
    assert repository.created_payload["alert_level"] == 1
    assert repository.created_payload["rule_snapshot"] == {
        "alert_type": "传感器离线",
        "rule_id": None,
    }


def test_alert_list_returns_counts_for_the_whole_filtered_scope() -> None:
    service = AlertService(
        FakeAlertRepository(),  # type: ignore[arg-type]
        FakeEngine(),  # type: ignore[arg-type]
    )

    result = service.list_alerts(
        alert_level=None,
        alert_status=0,
        alert_type=None,
        alert_category=0,
        sensor_no=None,
        keyword=None,
        page=1,
        page_size=20,
    )

    assert result["total"] == 1
    assert result["statusCounts"] == {
        "total": 12,
        "unhandled": 7,
        "handled": 5,
    }
