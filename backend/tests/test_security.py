from __future__ import annotations

import json

import pytest
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.security import SecurityService


def test_production_requires_authentication() -> None:
    with pytest.raises(ValueError, match="AUTH_ENABLED"):
        Settings(environment="production", auth_enabled=False)


def test_production_requires_strong_jwt_secret() -> None:
    password_hash = PasswordHash.recommended().hash("secret")
    users = json.dumps(
        [{"username": "admin", "password_hash": password_hash, "role": "admin"}]
    )
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(
            environment="production",
            auth_enabled=True,
            jwt_secret="short",
            auth_users_json=users,
            cors_origins="https://monitor.example.com",
        )


def test_password_authentication_and_jwt_round_trip() -> None:
    password = "correct-horse-battery-staple"
    password_hash = PasswordHash.recommended().hash(password)
    settings = Settings(
        environment="test",
        auth_enabled=True,
        jwt_secret="x" * 48,
        auth_users_json=json.dumps(
            [{"username": "engineer", "password_hash": password_hash, "role": "operator"}]
        ),
    )
    service = SecurityService(settings)

    principal = service.authenticate("engineer", password)
    assert principal is not None
    assert principal.role == "operator"

    token_response = service.create_token(principal)
    decoded = service.decode_token(token_response.access_token)
    assert decoded.username == "engineer"
    assert decoded.role == "operator"


def test_authentication_rejects_invalid_password() -> None:
    password_hash = PasswordHash.recommended().hash("correct")
    settings = Settings(
        environment="test",
        auth_enabled=True,
        jwt_secret="x" * 48,
        auth_users_json=json.dumps(
            [{"username": "viewer", "password_hash": password_hash, "role": "viewer"}]
        ),
    )
    service = SecurityService(settings)
    assert service.authenticate("viewer", "wrong") is None
