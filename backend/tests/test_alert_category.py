from __future__ import annotations

import pytest

from app.domain.alert_types import (
    ALERT_CATEGORY_DATA,
    ALERT_CATEGORY_DEVICE,
    ALERT_TYPE_DAQ_OFFLINE,
    ALERT_TYPE_OFFLINE,
    ALERT_TYPE_OOR,
    ALERT_TYPE_STUCK,
    ALERT_TYPE_THRESHOLD,
    alert_category_for_type,
)
from app.domain.engine import AlertAnalyticsEngine
from app.services.alerts import AlertService


@pytest.mark.parametrize(
    ("alert_type", "expected_category"),
    [
        (ALERT_TYPE_THRESHOLD, ALERT_CATEGORY_DATA),
        (ALERT_TYPE_STUCK, ALERT_CATEGORY_DATA),
        (ALERT_TYPE_OOR, ALERT_CATEGORY_DATA),
        (ALERT_TYPE_OFFLINE, ALERT_CATEGORY_DEVICE),
        (ALERT_TYPE_DAQ_OFFLINE, ALERT_CATEGORY_DEVICE),
        ("THRESHOLD", ALERT_CATEGORY_DATA),
        ("OFFLINE", ALERT_CATEGORY_DEVICE),
        ("DAQ_OFFLINE", ALERT_CATEGORY_DEVICE),
    ],
)
def test_alert_category_for_type(alert_type: str, expected_category: int) -> None:
    assert alert_category_for_type(alert_type) == expected_category


def test_enriched_alert_contains_category_and_label() -> None:
    engine = AlertAnalyticsEngine()
    enriched = engine.enrich_alert_row(
        {
            "id": "alert-1",
            "sensor_no": "zd-wg-01",
            "channel": "x",
            "alert_type": ALERT_TYPE_OFFLINE,
            "alert_level": 3,
            "alert_status": 0,
        }
    )

    assert enriched["alert_category"] == ALERT_CATEGORY_DEVICE
    assert enriched["alert_category_label"] == "设备预警"


class RecordingAlertRepository:
    def __init__(self) -> None:
        self.list_kwargs: dict = {}

    def list(self, **kwargs: object) -> tuple[list, int]:
        self.list_kwargs = kwargs
        return [], 0

    @staticmethod
    def total_pages(total: int, page_size: int) -> int:
        return 1


class EmptyEngine:
    def enrich_alert_row(self, row: dict) -> dict:
        return row


def test_list_alerts_forwards_category_filter() -> None:
    repository = RecordingAlertRepository()
    service = AlertService(
        repository,  # type: ignore[arg-type]
        EmptyEngine(),  # type: ignore[arg-type]
    )

    service.list_alerts(
        alert_level=None,
        alert_status=None,
        alert_type=None,
        alert_category=ALERT_CATEGORY_DEVICE,
        sensor_no=None,
        keyword=None,
        page=1,
        page_size=20,
    )

    assert repository.list_kwargs["alert_category"] == ALERT_CATEGORY_DEVICE
