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


def test_range_min_must_not_exceed_range_max() -> None:
    with pytest.raises(HTTPException) as error:
        ThresholdService._validate_thresholds(
            {"range_min": 10.0, "range_max": -10.0}
        )
    assert error.value.status_code == 400
