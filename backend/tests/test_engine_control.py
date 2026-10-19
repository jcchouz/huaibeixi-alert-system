from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.schemas.engine import EngineReplayRequest
from app.services.engine_control import EngineControlService


class FakeEngine:
    def __init__(self) -> None:
        self.mode = "REPLAY"
        self.is_running = False
        self.current_watermark = datetime(2026, 9, 28, 13, 55, 0)
        self.speed_multiplier = 1

    def step_detection(self, start: datetime, end: datetime) -> dict:
        self.current_watermark = end
        return {"triggeredAlertsCount": 0, "triggeredAlerts": []}

    def trigger_demo_alert(self, **_: object) -> dict:
        return {"id": "demo-alert"}

    def get_status(self) -> dict:
        return {
            "mode": self.mode,
            "isRunning": self.is_running,
            "currentWatermark": self.current_watermark.strftime("%Y-%m-%d %H:%M:%S"),
        }


def test_engine_control_start_and_stop() -> None:
    engine = FakeEngine()
    service = EngineControlService(
        engine,  # type: ignore[arg-type]
        Settings(environment="test", auth_enabled=False),
    )

    started = service.control(EngineReplayRequest(action="start"))
    assert started["success"] is True
    assert engine.is_running is True

    stopped = service.control(EngineReplayRequest(action="stop"))
    assert stopped["engineStatus"]["isRunning"] is False


def test_demo_actions_can_be_disabled() -> None:
    service = EngineControlService(
        FakeEngine(),  # type: ignore[arg-type]
        Settings(
            environment="test",
            auth_enabled=False,
            demo_endpoints_enabled=False,
        ),
    )
    with pytest.raises(HTTPException) as error:
        service.control(EngineReplayRequest(action="trigger_test_alert"))
    assert error.value.status_code == 404


def test_unknown_engine_action_is_rejected() -> None:
    service = EngineControlService(
        FakeEngine(),  # type: ignore[arg-type]
        Settings(environment="test", auth_enabled=False),
    )
    with pytest.raises(HTTPException) as error:
        service.control(EngineReplayRequest(action="unknown"))
    assert error.value.status_code == 400


@pytest.mark.parametrize("alert_type", ["RATE", "SPIKE"])
def test_retired_alert_types_are_rejected(alert_type: str) -> None:
    service = EngineControlService(
        FakeEngine(),  # type: ignore[arg-type]
        Settings(environment="test", auth_enabled=False),
    )
    with pytest.raises(HTTPException) as error:
        service.control(
            EngineReplayRequest(
                action="trigger_test_alert",
                alertType=alert_type,
            )
        )
    assert error.value.status_code == 400
