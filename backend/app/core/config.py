"""从环境变量和 ``backend/.env`` 加载运行时配置。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """后端共享的已校验运行时配置。"""

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # 应用与 API 基本信息。
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

    # PostgreSQL 连接和连接池配置。
    db_host: str = Field(default="127.0.0.1", validation_alias="DB_HOST")
    db_port: int = Field(default=5432, validation_alias="DB_PORT")
    db_name: str = Field(default="huaibeixi_db", validation_alias="DB_NAME")
    db_user: str = Field(default="huaibeixi", validation_alias="DB_USER")
    db_password: str = Field(default="", validation_alias="DB_PASSWORD")
    db_connect_timeout: int = Field(default=10, validation_alias="DB_CONNECT_TIMEOUT")
    db_pool_minconn: int = Field(default=2, ge=1, validation_alias="DB_POOL_MINCONN")
    db_pool_maxconn: int = Field(default=20, ge=1, validation_alias="DB_POOL_MAXCONN")

    # 检测 Worker 启停和实时扫描周期。
    detection_enabled: bool = Field(default=True, validation_alias="DETECTION_ENABLED")
    # 实时预警扫描间隔，单位为秒。统计数据仍按完整窗口生成。
    detection_interval_seconds: int = Field(
        default=1,
        ge=1,
        le=60,
        validation_alias="DETECTION_INTERVAL_SECONDS",
    )
    # 实时扫描向前回看的时间，覆盖外部事务提交延迟；重复样本按 last_ts 去重。
    detection_alert_overlap_seconds: int = Field(
        default=5,
        ge=0,
        le=60,
        validation_alias="DETECTION_ALERT_OVERLAP_SECONDS",
    )
    # 统计窗口的落库延迟；实时预警扫描不使用该延迟。
    detection_lag_seconds: int = Field(
        default=5,
        ge=0,
        le=300,
        validation_alias="DETECTION_LAG_SECONDS",
    )
    # t_stat_record 统计窗口长度，单位为分钟。
    detection_window_minutes: int = Field(
        default=1,
        ge=1,
        le=1440,
        validation_alias="DETECTION_WINDOW_MINUTES",
    )
    # 连续命中单位：采样点数或样本持续时间。
    detection_condition_unit: Literal["points", "seconds"] = Field(
        default="seconds",
        validation_alias="DETECTION_CONDITION_UNIT",
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
    # 秒模式下的连续命中持续时间。
    detection_confirm_seconds: float = Field(
        default=3.0,
        gt=0,
        le=86400,
        validation_alias="DETECTION_CONFIRM_SECONDS",
    )
    detection_stuck_seconds: float = Field(
        default=100.0,
        gt=0,
        le=86400,
        validation_alias="DETECTION_STUCK_SECONDS",
    )
    detection_offline_seconds: float = Field(
        default=5.0,
        gt=0,
        le=86400,
        validation_alias="DETECTION_OFFLINE_SECONDS",
    )
    # 服务恢复后单轮最多补处理的统计窗口数，防止长时间停机的追帧压力。
    detection_max_catchup_windows: int = Field(
        default=5,
        ge=1,
        le=1440,
        validation_alias="DETECTION_MAX_CATCHUP_WINDOWS",
    )

    # HTTP 安全、跨域和日志配置。
    cors_origins: str = Field(
        default="http://localhost:5174,http://127.0.0.1:5174",
        validation_alias="CORS_ORIGINS",
    )
    trusted_hosts: str = Field(default="*", validation_alias="TRUSTED_HOSTS")

    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_json: bool = Field(default=True, validation_alias="LOG_JSON")

    @property
    def dsn(self) -> str:
        """生成 psycopg2 连接字符串。"""
        return (
            f"host={self.db_host} port={self.db_port} dbname={self.db_name} "
            f"user={self.db_user} password={self.db_password} "
            f"connect_timeout={self.db_connect_timeout}"
        )

    @property
    def allowed_origins(self) -> List[str]:
        """返回标准化后的跨域来源列表。"""
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def allowed_hosts(self) -> List[str]:
        """返回标准化后的可信主机列表。"""
        return [item.strip() for item in self.trusted_hosts.split(",") if item.strip()]

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        """拒绝生产环境中不安全的配置组合。"""
        if self.environment == "production" and "*" in self.allowed_origins:
            raise ValueError("Wildcard CORS origins are not allowed in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """每个进程返回一个缓存的已校验配置实例。"""
    return Settings()
