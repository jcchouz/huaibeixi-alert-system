from __future__ import annotations

import pytest
from fastapi import HTTPException

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


def test_stuck_time_must_be_positive() -> None:
    with pytest.raises(HTTPException) as error:
        ThresholdService._validate_thresholds({"stuck_time_ms": 0})
    assert error.value.status_code == 400


def test_range_min_must_not_exceed_range_max() -> None:
    with pytest.raises(HTTPException) as error:
        ThresholdService._validate_thresholds(
            {"range_min": 10.0, "range_max": -10.0}
        )
    assert error.value.status_code == 400
