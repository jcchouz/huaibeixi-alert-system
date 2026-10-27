from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.deps import get_overview_service
from app.core.config import Settings
from app.domain.alert_types import ALERT_TYPE_STUCK, ALERT_TYPE_THRESHOLD
from app.domain.engine import AlertAnalyticsEngine


class FakeOverviewService:
    def get_overview(self) -> dict:
        return {
            "totalSensors": 1,
            "totalChannels": 1,
            "activeAlertsCount": 0,
            "alertsByType": [],
            "maxSeq": 0,
        }


def test_health_and_overview_are_open() -> None:
    settings = Settings(
        environment="development",
        trusted_hosts="*",
        cors_origins="http://testserver",
    )
    app = create_app(settings)
    assert app.state.engine.confirm_seconds == 3.0
    assert app.state.engine.stuck_seconds == 100.0
    assert app.state.engine.offline_seconds == 5.0
    assert app.state.engine.station_name == "玉环站"
    app.dependency_overrides[get_overview_service] = lambda: FakeOverviewService()
    client = TestClient(app)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["detection"]["running"] is False
    assert health.json()["engine"]["stationName"] == "玉环站"

    response = client.get("/api/overview")
    assert response.status_code == 200
    assert response.json()["totalSensors"] == 1


def test_station_name_is_configurable(monkeypatch) -> None:
    monkeypatch.setenv("STATION_NAME", "测试站")

    settings = Settings(_env_file=None, environment="development")
    app = create_app(settings)

    assert settings.station_name == "测试站"
    assert app.state.engine.station_name == "测试站"
    assert app.state.engine.get_status()["stationName"] == "测试站"


def test_engine_enriches_missing_station_with_configured_name() -> None:
    engine = AlertAnalyticsEngine(station_name="测试站")

    alert = engine.enrich_alert_row(
        {
            "sensor_no": "sensor-01",
            "channel": "channel-a",
            "alert_type": ALERT_TYPE_THRESHOLD,
            "alert_level": 1,
        }
    )

    assert alert["station_name"] == "测试站"


def test_alert_type_switches_are_wired_to_engine() -> None:
    settings = Settings(
        environment="development",
        trusted_hosts="*",
        cors_origins="http://testserver",
        detection_alert_stuck_enabled=False,
    )
    app = create_app(settings)

    assert app.state.engine.is_alert_type_enabled(ALERT_TYPE_STUCK) is False
    assert app.state.engine.is_alert_type_enabled(ALERT_TYPE_THRESHOLD) is True
    switches = app.state.engine.get_status()["alertTypeSwitches"]
    assert switches[ALERT_TYPE_STUCK] is False
    assert switches[ALERT_TYPE_THRESHOLD] is True


def test_validation_errors_use_stable_error_shape() -> None:
    settings = Settings(
        environment="development",
        trusted_hosts="*",
        cors_origins="http://testserver",
    )
    client = TestClient(create_app(settings))
    response = client.get("/api/alerts?page=0")
    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(payload["error"]["details"], list)


def test_alert_api_uses_polling_and_has_no_removed_features() -> None:
    settings = Settings(
        environment="development",
        trusted_hosts="*",
        cors_origins="http://testserver",
    )
    schema = create_app(settings).openapi()
    paths = {path for path, operations in schema["paths"].items() if operations}

    assert "/api/alerts/poll" in paths
    assert "/api/alerts/{alert_id}/ack" in paths
    assert "/api/alerts/stream" not in paths
    assert "/api/engine/replay" not in paths
    assert "/api/stats/wind-rose" not in paths
    assert "/api/auth/login" not in paths
    assert "/api/auth/me" not in paths
    assert not schema.get("components", {}).get("securitySchemes")

    ack_schema = schema["components"]["schemas"]["AlertAckRequest"]["properties"]
    assert "handlePhone" in ack_schema
    assert "handle_phone" in ack_schema

    threshold_schema = schema["components"]["schemas"]["ThresholdUpdateRequest"][
        "properties"
    ]
    assert "confirm_points" not in threshold_schema
    assert "stuck_points" not in threshold_schema
    assert "offline_points" not in threshold_schema
    assert "offline_timeout_ms" not in threshold_schema
    assert "stuck_time_ms" not in threshold_schema
