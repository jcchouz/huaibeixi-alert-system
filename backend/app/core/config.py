from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Literal, Optional

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables and ``backend/.env``."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = Field(
        default="淮北西站/玉环站 结构健康监测实时报警与统计分析系统 API",
        validation_alias="APP_NAME",
    )
    environment: Literal["development", "test", "staging", "production"] = Field(
        default="development",
        validation_alias="ENVIRONMENT",
    )
    debug: bool = Field(default=False, validation_alias="DEBUG")
    api_prefix: str = Field(default="/api", validation_alias="API_PREFIX")
    service_version: str = Field(default="3.0.0", validation_alias="SERVICE_VERSION")

    db_host: str = Field(default="127.0.0.1", validation_alias="DB_HOST")
    db_port: int = Field(default=5432, validation_alias="DB_PORT")
    db_name: str = Field(default="huaibeixi_db", validation_alias="DB_NAME")
    db_user: str = Field(default="huaibeixi", validation_alias="DB_USER")
    db_password: str = Field(default="", validation_alias="DB_PASSWORD")
    db_connect_timeout: int = Field(default=10, validation_alias="DB_CONNECT_TIMEOUT")
    db_pool_minconn: int = Field(default=2, ge=1, validation_alias="DB_POOL_MINCONN")
    db_pool_maxconn: int = Field(default=20, ge=1, validation_alias="DB_POOL_MAXCONN")

    cors_origins: str = Field(
        default="http://localhost:5174,http://127.0.0.1:5174",
        validation_alias="CORS_ORIGINS",
    )
    trusted_hosts: str = Field(default="*", validation_alias="TRUSTED_HOSTS")

    auth_enabled: bool = Field(default=False, validation_alias="AUTH_ENABLED")
    jwt_secret: Optional[str] = Field(default=None, validation_alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    jwt_issuer: str = Field(default="huaibeixi-alert-backend", validation_alias="JWT_ISSUER")
    jwt_audience: str = Field(default="huaibeixi-alert-frontend", validation_alias="JWT_AUDIENCE")
    jwt_expire_minutes: int = Field(default=480, ge=5, validation_alias="JWT_EXPIRE_MINUTES")
    auth_users_json: str = Field(default="[]", validation_alias="AUTH_USERS_JSON")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=True, validation_alias="LOG_JSON")

    @property
    def dsn(self) -> str:
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password} "
            f"connect_timeout={self.db_connect_timeout}"
        )

    @property
    def allowed_origins(self) -> List[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def allowed_hosts(self) -> List[str]:
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @property
    def auth_users(self) -> List[dict[str, Any]]:
        try:
            payload = json.loads(self.auth_users_json or "[]")
            return payload if isinstance(payload, list) else []
        except json.JSONDecodeError as exc:
            raise ValueError("AUTH_USERS_JSON must be a valid JSON array") from exc

    @model_validator(mode="after")
    def validate_security(self) -> "Settings":
        if self.environment == "production":
            if not self.auth_enabled:
                raise ValueError("AUTH_ENABLED must be true in production")
            if not self.jwt_secret or len(self.jwt_secret) < 32:
                raise ValueError("JWT_SECRET must contain at least 32 characters in production")
            if "*" in self.allowed_origins:
                raise ValueError("Wildcard CORS origins are not allowed in production")
        if self.auth_enabled and not self.auth_users:
            raise ValueError("AUTH_USERS_JSON must define at least one user when authentication is enabled")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
