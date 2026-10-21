from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings
from app.services.thresholds import ThresholdService


def test_threshold_upper_hierarchy_is_validated() -> None:
    with pytest.raises(HTTPException) as error:
        ThresholdService._validate_thresholds(
            {
                "level1_upper": 5.0,
                "level2_upper": 8.0,
                "level3_upper": 10.0,
                "level1_lower": -10.0,
                "level2_lower": -8.0,
                "level3_lower": -5.0,
            }
        )
    assert error.value.status_code == 400


def test_threshold_lower_hierarchy_is_validated() -> None:
    with pytest.raises(HTTPException) as error:
        ThresholdService._validate_thresholds(
            {
                "level1_upper": 10.0,
                "level2_upper": 8.0,
                "level3_upper": 5.0,
                "level1_lower": -5.0,
                "level2_lower": -8.0,
                "level3_lower": -10.0,
            }
        )
    assert error.value.status_code == 400


def test_valid_threshold_hierarchy_is_accepted() -> None:
    ThresholdService._validate_thresholds(
        {
            "level1_upper": 10.0,
            "level2_upper": 8.0,
            "level3_upper": 5.0,
            "level1_lower": -10.0,
            "level2_lower": -8.0,
            "level3_lower": -5.0,
        }
    )


def test_detection_group_counts_are_configured_and_positive() -> None:
    settings = Settings(environment="test")
    assert settings.detection_confirm_points == 3
    assert settings.detection_stuck_points == 100
    assert settings.detection_offline_points == 5
    assert settings.detection_condition_unit == "seconds"
    assert settings.detection_confirm_seconds == 3.0
    assert settings.detection_stuck_seconds == 100.0
    assert settings.detection_offline_seconds == 5.0

    point_settings = Settings(
        environment="test",
        detection_condition_unit="points",
    )
    assert point_settings.detection_condition_unit == "points"
    assert point_settings.detection_confirm_points == 3
    assert point_settings.detection_stuck_points == 100
    assert point_settings.detection_offline_points == 5

    with pytest.raises(ValidationError):
        Settings(environment="test", detection_confirm_points=0)
    with pytest.raises(ValidationError):
        Settings(
            environment="test",
            detection_condition_unit="seconds",
            detection_confirm_seconds=0,
        )


def test_detection_condition_unit_can_be_switched_by_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("DETECTION_CONDITION_UNIT", "points")
    point_settings = Settings(environment="test")
    assert point_settings.detection_condition_unit == "points"

    monkeypatch.setenv("DETECTION_CONDITION_UNIT", "seconds")
    second_settings = Settings(environment="test")
    assert second_settings.detection_condition_unit == "seconds"


def test_alert_type_switches_default_to_enabled(monkeypatch) -> None:
    # 这里验证代码默认值，不能受开发机 backend/.env 的本地调试开关影响。
    for name in (
        "DETECTION_ALERT_THRESHOLD_ENABLED",
        "DETECTION_ALERT_OFFLINE_ENABLED",
        "DETECTION_ALERT_DAQ_OFFLINE_ENABLED",
        "DETECTION_ALERT_STUCK_ENABLED",
        "DETECTION_ALERT_OOR_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None, environment="test")
    assert settings.detection_alert_threshold_enabled is True
    assert settings.detection_alert_offline_enabled is True
    assert settings.detection_alert_daq_offline_enabled is True
    assert settings.detection_alert_stuck_enabled is True
    assert settings.detection_alert_oor_enabled is True

    monkeypatch.setenv("DETECTION_ALERT_STUCK_ENABLED", "false")
    disabled = Settings(_env_file=None, environment="test")
    assert disabled.detection_alert_stuck_enabled is False
    assert disabled.detection_alert_threshold_enabled is True
    assert disabled.detection_alert_offline_enabled is True
    assert disabled.detection_alert_daq_offline_enabled is True
    assert disabled.detection_alert_oor_enabled is True


def test_range_min_must_not_exceed_range_max() -> None:
    with pytest.raises(HTTPException) as error:
        ThresholdService._validate_thresholds(
            {"range_min": 10.0, "range_max": -10.0}
        )
    assert error.value.status_code == 400
