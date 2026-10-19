from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.domain.alert_types import ALERT_TYPE_VALUES
from app.services.engine_control import EngineControlService


def test_sampling_gap_is_not_an_alert_type() -> None:
    assert "采样断流" not in ALERT_TYPE_VALUES


@pytest.mark.parametrize("alert_type", ["GAP_1000MS", "采样断流"])
def test_sampling_gap_cannot_be_triggered(alert_type: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        EngineControlService._reject_retired_alert_type(alert_type)

    assert exc_info.value.status_code == 400
