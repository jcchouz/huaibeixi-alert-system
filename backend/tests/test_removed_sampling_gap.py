from __future__ import annotations

from app.domain.alert_types import ALERT_TYPE_VALUES


def test_sampling_gap_is_not_an_alert_type() -> None:
    assert "采样断流" not in ALERT_TYPE_VALUES
