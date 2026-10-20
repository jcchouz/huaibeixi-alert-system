from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Literal

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

    # 是否随 FastAPI 生命周期启动生产检测线程。
    detection_enabled: bool = Field(default=True, validation_alias="DETECTION_ENABLED")
    # 检测线程的扫描间隔，单位为秒。
    detection_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=60,
        validation_alias="DETECTION_INTERVAL_SECONDS",
    )
    # 等待数据落库的延迟，避免窗口结束瞬间读取到不完整数据。
    detection_lag_seconds: int = Field(
        default=5,
        ge=0,
        le=300,
        validation_alias="DETECTION_LAG_SECONDS",
    )
    # 统计和检测窗口长度，单位为分钟。
    detection_window_minutes: int = Field(
        default=1,
        ge=1,
        le=1440,
        validation_alias="DETECTION_WINDOW_MINUTES",
    )
    # 阈值超限或超物理量程需要连续命中的采样组数。
    detection_confirm_points: int = Field(
        default=3,
        ge=1,
        le=10000,
        validation_alias="DETECTION_CONFIRM_POINTS",
    )
    # 传感器读数连续相同多少组后判定为死值。
    detection_stuck_points: int = Field(
        default=100,
        ge=1,
        le=10000,
        validation_alias="DETECTION_STUCK_POINTS",
    )
    # 按传感器 frequency 换算后，连续缺失多少组采样判定为离线。
    detection_offline_points: int = Field(
        default=5,
        ge=1,
        le=10000,
        validation_alias="DETECTION_OFFLINE_POINTS",
    )
    # 服务恢复后单轮最多补处理的检测窗口数，防止长时间停机的追帧压力。
    detection_max_catchup_windows: int = Field(
        default=5,
        ge=1,
        le=1440,
        validation_alias="DETECTION_MAX_CATCHUP_WINDOWS",
    )

    cors_origins: str = Field(
        default="http://localhost:5174,http://127.0.0.1:5174",
        validation_alias="CORS_ORIGINS",
    )
    trusted_hosts: str = Field(default="*", validation_alias="TRUSTED_HOSTS")

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

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.environment == "production" and "*" in self.allowed_origins:
            raise ValueError("Wildcard CORS origins are not allowed in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
