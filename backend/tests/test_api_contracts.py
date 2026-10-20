from __future__ import annotations

import json

from fastapi.testclient import TestClient
from pwdlib import PasswordHash

from app.api.app import create_app
from app.api.deps import get_overview_service
from app.core.config import Settings


class FakeOverviewService:
    def get_overview(self) -> dict:
        return {
            "totalSensors": 1,
            "totalChannels": 1,
            "activeAlertsCount": 0,
            "alertsByType": [],
            "maxSeq": 0,
        }


def test_health_and_auth_me_are_available_in_development() -> None:
    settings = Settings(
        environment="test",
        auth_enabled=False,
        trusted_hosts="*",
        cors_origins="http://testserver",
    )
    client = TestClient(create_app(settings))

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    current_user = client.get("/api/auth/me")
    assert current_user.status_code == 200
    assert current_user.json()["role"] == "admin"
    assert current_user.json()["auth_mode"] == "disabled"


def test_protected_route_requires_bearer_token_when_auth_is_enabled() -> None:
    password_hash = PasswordHash.recommended().hash("secret")
    settings = Settings(
        environment="test",
        auth_enabled=True,
        jwt_secret="x" * 48,
        auth_users_json=json.dumps(
            [{"username": "viewer", "password_hash": password_hash, "role": "viewer"}]
        ),
        trusted_hosts="*",
        cors_origins="http://testserver",
    )
    app = create_app(settings)
    app.dependency_overrides[get_overview_service] = lambda: FakeOverviewService()
    client = TestClient(app)

    assert client.get("/api/overview").status_code == 401

    login = client.post(
        "/api/auth/login",
        json={"username": "viewer", "password": "secret"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    response = client.get(
        "/api/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["totalSensors"] == 1


def test_validation_errors_use_stable_error_shape() -> None:
    settings = Settings(
        environment="test",
        auth_enabled=False,
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
        environment="test",
        auth_enabled=False,
        trusted_hosts="*",
        cors_origins="http://testserver",
    )
    paths = {
        path
        for path, operations in create_app(settings).openapi()["paths"].items()
        if operations
    }

    assert "/api/alerts/poll" in paths
    assert "/api/alerts/{alert_id}/ack" in paths
    assert "/api/alerts/stream" not in paths
    assert "/api/engine/replay" not in paths
    assert "/api/stats/wind-rose" not in paths
