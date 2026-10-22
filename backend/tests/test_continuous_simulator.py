import sys

import pytest

from tools.continuous_simulator import parse_args


def test_parse_args_accepts_timestamp_lead(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["continuous_simulator.py", "--timestamp-lead-seconds", "3"],
    )

    args = parse_args()

    assert args.timestamp_lead_seconds == 3.0


def test_parse_args_rejects_negative_timestamp_lead(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["continuous_simulator.py", "--timestamp-lead-seconds", "-1"],
    )

    with pytest.raises(SystemExit):
        parse_args()
