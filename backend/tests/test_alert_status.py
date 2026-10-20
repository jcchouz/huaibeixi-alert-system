from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.schemas.alerts import AlertAckRequest, AlertCreateRequest
from app.services.alerts import AlertService


class FakeAlertRepository:
    def __init__(self) -> None:
        self.created_payload: dict = {}

    def acknowledge(self, alert_id: str, *, target_status: int, actor: str) -> dict:
        return {
            "id": alert_id,
            "alert_status": target_status,
            "ack_by": actor,
        }

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
            "tester",
        )

    assert error.value.status_code == 400


def test_non_threshold_alert_is_forced_to_level_three() -> None:
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

    assert result["alert"]["alert_level"] == 3
    assert repository.created_payload["alert_level"] == 3
